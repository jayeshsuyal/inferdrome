"""Pinned, non-shell SGLang 0.5.18 producer capability boundary.

This module builds and verifies an invocation contract only.  It does not
start SGLang, contact an endpoint, resolve credentials, or produce evidence.
The native result parser and additive normalization report live in
``inferdrome.normalization.sglang_0_5``.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.ids import sha256_digest
from inferdrome.errors import AdapterError
from inferdrome.execution.cancellation import TerminationPolicy
from inferdrome.execution.subprocess_runner import (
    ProcessCapture,
    ProcessTermination,
    run_captured_process,
)


class _SglangModel(FrozenModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )


SGLANG_SOURCE_REPOSITORY: Final = "https://github.com/sgl-project/sglang"
SGLANG_VERSION: Final[Literal["0.5.18"]] = "0.5.18"
SGLANG_RELEASE_COMMIT: Final[Literal["71de97b264b04dcd514cf904003028aefe9775c8"]] = (
    "71de97b264b04dcd514cf904003028aefe9775c8"
)
SGLANG_BACKEND: Final[Literal["sglang"]] = "sglang"
SGLANG_BENCHMARK_MODULE: Final[Literal["sglang.benchmark.serving"]] = (
    "sglang.benchmark.serving"
)
SGLANG_INVOCATION_SCHEMA_VERSION: Final[Literal["inferdrome.sglang-invocation.v1"]] = (
    "inferdrome.sglang-invocation.v1"
)
SGLANG_REFERENCE_ADAPTER_ID: Final[Literal["sglang_reference_v1"]] = (
    "sglang_reference_v1"
)
SGLANG_DEFAULT_ENDPOINT: Final[Literal["http://127.0.0.1:30000"]] = (
    "http://127.0.0.1:30000"
)
SGLANG_NATIVE_ARTIFACT_NAME: Final = "sglang-detailed.jsonl"
SGLANG_DATASET_NAME: Final[Literal["random-ids"]] = "random-ids"
SGLANG_OFFLINE_ENVIRONMENT_POLICY: Final = {
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}
SGLANG_FIXED_TAG: Final[None] = None
SGLANG_FIXED_SHAREGPT_OUTPUT_LEN: Final[None] = None
SGLANG_FIXED_RANDOM_RANGE_RATIO: Final[Literal["0"]] = "0"

_MAX_INVOCATION_BYTES = 2_097_152
_MAX_VERSION_BYTES = 65_536
_MAX_ARGUMENTS = 128
_MAX_ARGUMENT_LENGTH = 8_192
_MAX_REQUESTS = 10_000
_MAX_CONCURRENCY = 1_024
_MAX_PATH_LENGTH = 4_096
_MAX_MODEL_LENGTH = 256
_MAX_REQUEST_RATE = Decimal("100000")
_ABSOLUTE_PATH = re.compile(r"^/[^\x00-\x1f]{1,4095}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,254}$")
_EXECUTABLE = re.compile(r"^(?:[A-Za-z0-9._+-]{1,128}|/[^\x00-\x1f]{1,4095})$")
_SECRET_SHAPES = (
    re.compile(r"^(?:sk|rk)-[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^(?:gh[pousr]_)[A-Za-z0-9_]{16,}$"),
    re.compile(r"^github_pat_[A-Za-z0-9_]{16,}$"),
    re.compile(r"^[A-Za-z0-9_-]{40,}$"),
)


def _looks_like_secret(value: str) -> bool:
    return any(pattern.fullmatch(value) is not None for pattern in _SECRET_SHAPES)


def _validate_endpoint(value: str) -> str:
    if any(ord(character) < 0x20 for character in value):
        raise ValueError("endpoint is outside the private endpoint contract")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
    except ValueError:
        host = None
        parsed = None
    if (
        parsed is None
        or parsed.scheme != "http"
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or value.endswith("/")
    ):
        raise ValueError("endpoint is outside the private endpoint contract")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if address.version != 4 or not (
            address.is_loopback
            or address in ipaddress.ip_network("10.0.0.0/8")
            or address in ipaddress.ip_network("172.16.0.0/12")
            or address in ipaddress.ip_network("192.168.0.0/16")
        ):
            raise ValueError("endpoint is outside the private endpoint contract")
    elif not host.endswith((".internal", ".local", ".private")):
        raise ValueError("endpoint is outside the private endpoint contract")
    if parsed.port is None or not 1 <= parsed.port <= 65_535:
        raise ValueError("endpoint port is outside the contract")
    return value


def _validate_output_path_syntax(value: str) -> str:
    if not _ABSOLUTE_PATH.fullmatch(value) or len(value) > _MAX_PATH_LENGTH:
        raise ValueError("output path is outside the bounded path contract")
    path = Path(value)
    if any(part in (".", "..") for part in path.parts):
        raise ValueError("output path contains traversal")
    return value


def _validate_safe_output_destination(value: str) -> str:
    _validate_output_path_syntax(value)
    path = Path(value)
    try:
        parent = path.parent
        current = Path(path.anchor)
        for part in parent.parts[1:]:
            current /= part
            metadata = os.lstat(current)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("output path has an unsafe ancestor")
        parent_metadata = os.lstat(parent)
        if parent.is_symlink() or not stat.S_ISDIR(parent_metadata.st_mode):
            raise ValueError("output parent is not a real directory")
        try:
            output_metadata = os.lstat(path)
        except FileNotFoundError:
            return value
        if stat.S_ISLNK(output_metadata.st_mode) or stat.S_ISREG(
            output_metadata.st_mode
        ):
            raise ValueError("output path already exists")
        raise ValueError("output path is not a regular absent file")
    except FileNotFoundError:
        raise ValueError("output parent is unavailable") from None
    except OSError:
        raise ValueError("output path cannot be inspected safely") from None


def _validate_safe_input_directory(value: str, *, label: str) -> str:
    _validate_output_path_syntax(value)
    path = Path(value)
    try:
        metadata = os.lstat(path)
    except OSError:
        raise ValueError(f"{label} is unavailable") from None
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a real directory")
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            ancestor = os.lstat(current)
            if stat.S_ISLNK(ancestor.st_mode):
                raise ValueError(f"{label} has an unsafe ancestor")
    except OSError:
        raise ValueError(f"{label} cannot be inspected safely") from None
    return value


def _decimal_cli(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


BoundedExecutable = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4_096),
]
BoundedModelId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=_MAX_MODEL_LENGTH),
]


class SglangInvocationConfig(_SglangModel):
    """Closed, deterministic inputs to the native SGLang benchmark module."""

    schema_version: Literal["inferdrome.sglang-invocation.v1"] = (
        SGLANG_INVOCATION_SCHEMA_VERSION
    )
    python_executable: BoundedExecutable
    endpoint: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    model: BoundedModelId
    tokenizer_path: Annotated[str, StringConstraints(min_length=1, max_length=4_096)]
    output_path: Annotated[str, StringConstraints(min_length=1, max_length=4_096)]
    request_count: Annotated[int, Field(strict=True, ge=1, le=_MAX_REQUESTS)]
    concurrency: Annotated[int, Field(strict=True, ge=1, le=_MAX_CONCURRENCY)]
    seed: Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]
    request_rate: Annotated[Decimal, Field(strict=True, gt=0, le=_MAX_REQUEST_RATE)]
    input_tokens: Annotated[int, Field(strict=True, ge=1, le=32_768)]
    output_tokens: Annotated[int, Field(strict=True, ge=1, le=32_768)]

    @model_validator(mode="after")
    def validate_closed_inputs(self) -> SglangInvocationConfig:
        if not _EXECUTABLE.fullmatch(self.python_executable):
            raise ValueError("python executable is outside the bounded contract")
        if not _MODEL_ID.fullmatch(self.model) or _looks_like_secret(self.model):
            raise ValueError("model identity is outside the bounded contract")
        _validate_endpoint(self.endpoint)
        _validate_output_path_syntax(self.tokenizer_path)
        _validate_output_path_syntax(self.output_path)
        if self.concurrency > self.request_count:
            raise ValueError("concurrency cannot exceed attempted request count")
        return self


class SglangInvocationDocument(_SglangModel):
    schema_version: Literal["inferdrome.sglang-invocation.v1"]
    source_repository: Literal["https://github.com/sgl-project/sglang"]
    producer_version: Literal["0.5.18"]
    release_commit: Literal["71de97b264b04dcd514cf904003028aefe9775c8"]
    benchmark_module: Literal["sglang.benchmark.serving"]
    backend: Literal["sglang"]
    streaming: Literal[True]
    environment_policy: dict[str, Literal["1"]]
    config: SglangInvocationConfig
    argv: tuple[Annotated[str, StringConstraints(min_length=1, max_length=8192)], ...]

    @model_validator(mode="after")
    def validate_argv_bounds(self) -> SglangInvocationDocument:
        if not 1 <= len(self.argv) <= _MAX_ARGUMENTS:
            raise ValueError("argument vector length is outside the contract")
        if self.environment_policy != SGLANG_OFFLINE_ENVIRONMENT_POLICY:
            raise ValueError("SGLang offline environment policy is not exact")
        return self


@dataclass(frozen=True)
class SglangInvocation:
    argv: tuple[str, ...]
    config: SglangInvocationConfig
    evidence_bytes: bytes
    evidence_sha256: str


@dataclass(frozen=True)
class SglangVersionProbeCapture:
    process: ProcessCapture
    observed_version: str


ProcessRunner = Callable[..., ProcessCapture]


def _build_argv(config: SglangInvocationConfig) -> tuple[str, ...]:
    return (
        config.python_executable,
        "-m",
        SGLANG_BENCHMARK_MODULE,
        "--backend",
        SGLANG_BACKEND,
        "--base-url",
        config.endpoint,
        "--model",
        config.model,
        "--tokenizer",
        config.tokenizer_path,
        "--dataset-name",
        SGLANG_DATASET_NAME,
        "--tokenize-prompt",
        "--random-input-len",
        str(config.input_tokens),
        "--random-output-len",
        str(config.output_tokens),
        "--num-prompts",
        str(config.request_count),
        "--max-concurrency",
        str(config.concurrency),
        "--request-rate",
        _decimal_cli(config.request_rate),
        "--random-range-ratio",
        SGLANG_FIXED_RANDOM_RANGE_RATIO,
        "--seed",
        str(config.seed),
        "--output-file",
        config.output_path,
        "--output-details",
        "--disable-tqdm",
    )


def _invocation_payload(
    config: SglangInvocationConfig,
    argv: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "backend": SGLANG_BACKEND,
        "benchmark_module": SGLANG_BENCHMARK_MODULE,
        "config": config.model_dump(mode="json", by_alias=True),
        "environment_policy": dict(SGLANG_OFFLINE_ENVIRONMENT_POLICY),
        "producer_version": SGLANG_VERSION,
        "release_commit": SGLANG_RELEASE_COMMIT,
        "schema_version": SGLANG_INVOCATION_SCHEMA_VERSION,
        "source_repository": SGLANG_SOURCE_REPOSITORY,
        "argv": list(argv),
        "streaming": True,
    }


def build_sglang_invocation(config: SglangInvocationConfig) -> SglangInvocation:
    """Build one canonical, shell-free SGLang invocation.

    This is a pure contract builder.  It deliberately does not inspect the
    tokenizer or output paths; callers that are about to execute the producer
    must call :func:`preflight_sglang_invocation` separately.
    """

    if not isinstance(config, SglangInvocationConfig):
        raise AdapterError("SGLang invocation configuration is invalid")
    try:
        # A model_copy/model_construct instance is not trusted at this boundary.
        validated = SglangInvocationConfig.model_validate_json(
            canonical_json_bytes(config.model_dump(mode="json"))
        )
        argv = _build_argv(validated)
        payload = _invocation_payload(validated, argv)
        evidence = canonical_json_bytes(payload)
    except (ValidationError, TypeError, ValueError):
        raise AdapterError("SGLang invocation configuration is invalid") from None
    return SglangInvocation(
        argv=argv,
        config=validated,
        evidence_bytes=evidence,
        evidence_sha256=sha256_digest(evidence),
    )


def preflight_sglang_invocation(invocation: SglangInvocation) -> SglangInvocation:
    """Validate filesystem inputs immediately before a future execution.

    The returned object is the strictly reparsed invocation.  No output file
    or parent directory is created by this check.
    """

    if not isinstance(invocation, SglangInvocation):
        raise AdapterError("SGLang invocation contract is invalid")
    try:
        validated = validate_sglang_invocation(invocation.evidence_bytes)
        if validated != invocation:
            raise AdapterError("SGLang invocation contract is inconsistent")
        _validate_safe_input_directory(
            validated.config.tokenizer_path,
            label="SGLang tokenizer snapshot",
        )
        _validate_safe_output_destination(validated.config.output_path)
    except (AdapterError, TypeError, ValueError):
        raise AdapterError("SGLang invocation filesystem preflight failed") from None
    return validated


def _strict_invocation_object(content: bytes) -> dict[str, Any]:
    if len(content) > _MAX_INVOCATION_BYTES:
        raise AdapterError("SGLang invocation contract exceeds its size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise AdapterError("SGLang invocation contract is not valid UTF-8") from None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AdapterError("SGLang invocation contract has duplicate keys")
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        raise AdapterError("SGLang invocation contract has a non-finite number")

    try:
        raw = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_float=Decimal,
            parse_constant=reject_constant,
        )
    except AdapterError:
        raise
    except (json.JSONDecodeError, RecursionError):
        raise AdapterError("SGLang invocation contract is not valid JSON") from None
    if not isinstance(raw, dict):
        raise AdapterError("SGLang invocation contract must be one object")
    return raw


def validate_sglang_invocation(content: bytes) -> SglangInvocation:
    """Strictly reparse and reproduce an invocation contract."""

    value = _strict_invocation_object(content)
    if set(value) != {
        "argv",
        "backend",
        "benchmark_module",
        "config",
        "environment_policy",
        "producer_version",
        "release_commit",
        "schema_version",
        "source_repository",
        "streaming",
    }:
        raise AdapterError("SGLang invocation contract has an unknown field set")
    raw_argv = value.get("argv")
    if not isinstance(raw_argv, list):
        raise AdapterError("SGLang invocation argument vector is invalid")
    value["argv"] = tuple(raw_argv)
    raw_config = value.get("config")
    if isinstance(raw_config, dict) and isinstance(
        raw_config.get("request_rate"), str
    ):
        try:
            raw_config["request_rate"] = Decimal(raw_config["request_rate"])
        except (InvalidOperation, ValueError):
            raise AdapterError("SGLang invocation request rate is invalid") from None
    try:
        document = SglangInvocationDocument.model_validate(value)
        expected_argv = _build_argv(document.config)
        expected_payload = _invocation_payload(document.config, expected_argv)
        expected_bytes = canonical_json_bytes(expected_payload)
    except (ValidationError, TypeError, ValueError):
        raise AdapterError("SGLang invocation contract is invalid") from None
    if expected_argv != document.argv or expected_bytes != content:
        raise AdapterError("SGLang invocation contract is not canonical")
    return SglangInvocation(
        argv=expected_argv,
        config=document.config,
        evidence_bytes=expected_bytes,
        evidence_sha256=sha256_digest(expected_bytes),
    )


def parse_sglang_version_output(content: bytes) -> str:
    if len(content) > _MAX_VERSION_BYTES:
        raise AdapterError("SGLang version output exceeds its size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise AdapterError("SGLang version output is not valid UTF-8") from None
    if text.endswith("\n"):
        text = text[:-1]
    if "\n" in text or "\r" in text or text != SGLANG_VERSION:
        raise AdapterError("SGLang version output is not the pinned version")
    return text


def probe_sglang_version(
    *,
    cwd: Path,
    python_executable: str = "python3",
    process_runner: ProcessRunner = run_captured_process,
    termination_policy: TerminationPolicy | None = None,
) -> SglangVersionProbeCapture:
    """Probe exact package version through an injected no-shell runner."""

    if not cwd.is_absolute() or cwd.is_symlink() or not cwd.is_dir():
        raise AdapterError("SGLang version probe directory is invalid")
    if not _EXECUTABLE.fullmatch(python_executable):
        raise AdapterError("SGLang version probe executable is invalid")
    process = process_runner(
        (
            python_executable,
            "-c",
                (
                    "import importlib.metadata,sys; "
                    'sys.stdout.write(importlib.metadata.version("sglang"))'
                ),
        ),
        cwd=cwd,
        max_runtime_seconds=30,
        output_limit_bytes=_MAX_VERSION_BYTES,
        termination_policy=termination_policy,
        environment={
            **SGLANG_OFFLINE_ENVIRONMENT_POLICY,
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
        },
        merge_stderr=True,
    )
    if process.termination is not ProcessTermination.EXITED or process.exit_status != 0:
        raise AdapterError("SGLang version probe did not complete successfully")
    return SglangVersionProbeCapture(
        process=process,
        observed_version=parse_sglang_version_output(process.stdout),
    )


__all__ = [
    "SGLANG_BACKEND",
    "SGLANG_BENCHMARK_MODULE",
    "SGLANG_DATASET_NAME",
    "SGLANG_DEFAULT_ENDPOINT",
    "SGLANG_FIXED_RANDOM_RANGE_RATIO",
    "SGLANG_FIXED_SHAREGPT_OUTPUT_LEN",
    "SGLANG_INVOCATION_SCHEMA_VERSION",
    "SGLANG_NATIVE_ARTIFACT_NAME",
    "SGLANG_OFFLINE_ENVIRONMENT_POLICY",
    "SGLANG_REFERENCE_ADAPTER_ID",
    "SGLANG_RELEASE_COMMIT",
    "SGLANG_SOURCE_REPOSITORY",
    "SGLANG_VERSION",
    "SglangInvocation",
    "SglangInvocationConfig",
    "SglangVersionProbeCapture",
    "build_sglang_invocation",
    "parse_sglang_version_output",
    "preflight_sglang_invocation",
    "probe_sglang_version",
    "validate_sglang_invocation",
]
