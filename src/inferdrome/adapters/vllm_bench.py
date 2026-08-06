"""Pinned vLLM 0.26.0 invocation and attached-endpoint preflight."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import stat
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.experiment import (
    AttachedVllmTarget,
    ConcurrentTraffic,
    ExperimentSpec,
    RequestRateTraffic,
    VllmExecution,
)
from inferdrome.domain.ids import OpaqueName, Sha256Digest, sha256_digest
from inferdrome.domain.request_plan import InlinePrompt, RequestPlan
from inferdrome.errors import AdapterError, SourceInputError
from inferdrome.execution.cancellation import CancellationToken, TerminationPolicy
from inferdrome.execution.subprocess_runner import (
    ProcessCapture,
    ProcessTermination,
    run_captured_process,
)
from inferdrome.gpu_proof import LocalGpuProof, validate_local_gpu_proof
from inferdrome.normalization.vllm_0_26 import (
    VLLM_ADAPTER_VERSION,
    VLLM_VERSION,
)
from inferdrome.resolution.workload import parse_custom_workload

_MAX_PREFLIGHT_RESPONSE_BYTES = 1_048_576
_MAX_INVOCATION_BYTES = 2_097_152
_MAX_VERSION_BYTES = 65_536
_MAX_DATASET_BYTES = 134_217_728
_PINNED_VERSION_LINE = re.compile(r"^0\.26\.0(?:\+[0-9A-Za-z.-]+)?$")
_VERSION_ONLY_LINE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_RESERVED_NATIVE_KEYS = {
    "backend",
    "burstiness",
    "completed",
    "date",
    "duration",
    "endpoint_type",
    "errors",
    "failed",
    "generated_texts",
    "input_lens",
    "itls",
    "label",
    "max_concurrency",
    "max_concurrent_requests",
    "max_output_tokens_per_s",
    "mean_e2el_ms",
    "median_e2el_ms",
    "model_id",
    "num_prompts",
    "output_lens",
    "output_throughput",
    "p50_e2el_ms",
    "p95_e2el_ms",
    "p99_e2el_ms",
    "request_goodput",
    "request_rate",
    "request_throughput",
    "rtfx",
    "start_times",
    "std_e2el_ms",
    "tokenizer_id",
    "total_input_tokens",
    "total_output_tokens",
    "total_token_throughput",
    "ttfts",
}


@dataclass(frozen=True)
class VllmInvocationPaths:
    dataset_path: Path
    tokenizer_path: Path
    result_directory: Path


@dataclass(frozen=True)
class VllmInvocation:
    argv: tuple[str, ...]
    metadata: Mapping[str, str]
    paths: VllmInvocationPaths
    preflight: EndpointPreflightCapture
    evidence_bytes: bytes
    local_gpu_proof: LocalGpuProof | None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


HttpTransport = Callable[[str, float, int], HttpResponse]
ProcessRunner = Callable[..., ProcessCapture]


class EndpointPreflightResult(FrozenModel):
    schema_version: Literal["inferdrome.endpoint-preflight.v1"]
    api: Literal["openai_chat_completions"]
    status: Literal[200]
    target_model: OpaqueName
    server_reported_models: Annotated[tuple[OpaqueName, ...], Field(min_length=1)]
    response_sha256: Sha256Digest

    @model_validator(mode="after")
    def target_must_be_reported(self) -> EndpointPreflightResult:
        if self.target_model not in self.server_reported_models:
            raise ValueError("target model is absent from endpoint model list")
        if len(self.server_reported_models) != len(set(self.server_reported_models)):
            raise ValueError("endpoint model IDs must be unique")
        return self


@dataclass(frozen=True)
class EndpointPreflightCapture:
    result: EndpointPreflightResult
    response_bytes: bytes


@dataclass(frozen=True)
class VllmVersionProbeCapture:
    process: ProcessCapture
    observed_version: str


@dataclass(frozen=True)
class VllmBenchmarkCapture:
    invocation: VllmInvocation
    process: ProcessCapture
    native_result_bytes: bytes | None


@dataclass(frozen=True)
class _DatasetSnapshot:
    identity: tuple[int, ...]
    sha256: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def _default_transport(url: str, timeout: float, limit: int) -> HttpResponse:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(limit + 1)
            status = response.status
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        raise AdapterError("attached endpoint preflight request failed") from None
    if len(body) > limit:
        raise AdapterError("attached endpoint preflight response exceeds limit")
    return HttpResponse(status=status, body=body)


def _strict_json_object(content: bytes) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise AdapterError("endpoint preflight response is not valid UTF-8") from None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AdapterError("endpoint preflight response has duplicate keys")
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        raise AdapterError("endpoint preflight response has non-finite numbers")

    try:
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except AdapterError:
        raise
    except (json.JSONDecodeError, RecursionError):
        raise AdapterError("endpoint preflight response is not valid JSON") from None
    if not isinstance(value, dict):
        raise AdapterError("endpoint preflight response must be a JSON object")
    return value


def preflight_attached_endpoint(
    target: AttachedVllmTarget,
    *,
    timeout_seconds: float = 5.0,
    transport: HttpTransport | None = None,
) -> EndpointPreflightCapture:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 60
    ):
        raise AdapterError("endpoint preflight timeout is outside limits")
    endpoint = str(target.endpoint).rstrip("/")
    url = f"{endpoint}/v1/models"
    try:
        response = (transport or _default_transport)(
            url,
            float(timeout_seconds),
            _MAX_PREFLIGHT_RESPONSE_BYTES,
        )
    except AdapterError:
        raise
    except OSError:
        raise AdapterError("attached endpoint preflight request failed") from None
    if not isinstance(response, HttpResponse) or not isinstance(response.body, bytes):
        raise AdapterError("attached endpoint preflight transport result is invalid")
    if response.status != 200:
        raise AdapterError("attached endpoint model-list request was not successful")
    if len(response.body) > _MAX_PREFLIGHT_RESPONSE_BYTES:
        raise AdapterError("attached endpoint preflight response exceeds limit")
    value = _strict_json_object(response.body)
    data = value.get("data")
    if not isinstance(data, list) or not data:
        raise AdapterError("endpoint model-list response has no model entries")
    model_ids = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise AdapterError("endpoint model-list entry is invalid")
        model_ids.append(item["id"])
    try:
        result = EndpointPreflightResult(
            schema_version="inferdrome.endpoint-preflight.v1",
            api="openai_chat_completions",
            status=200,
            target_model=target.model,
            server_reported_models=tuple(model_ids),
            response_sha256=sha256_digest(response.body),
        )
    except ValueError:
        raise AdapterError("resolved target model is absent from endpoint") from None
    return EndpointPreflightCapture(result=result, response_bytes=response.body)


def _validate_preflight_capture(
    target: AttachedVllmTarget,
    capture: EndpointPreflightCapture,
) -> EndpointPreflightCapture:
    if not isinstance(capture, EndpointPreflightCapture):
        raise AdapterError("attached endpoint preflight capture is invalid")
    if len(capture.response_bytes) > _MAX_PREFLIGHT_RESPONSE_BYTES:
        raise AdapterError("attached endpoint preflight response exceeds limit")
    replayed = preflight_attached_endpoint(
        target,
        transport=lambda _url, _timeout, _limit: HttpResponse(
            status=200,
            body=capture.response_bytes,
        ),
    )
    if replayed != capture:
        raise AdapterError("attached endpoint preflight evidence is inconsistent")
    return capture


def _require_directory_no_follow(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise AdapterError(f"{label} must be an absolute resolved path")
    try:
        file_stat = path.stat(follow_symlinks=False)
    except OSError:
        raise AdapterError(f"{label} is unavailable") from None
    if path.is_symlink() or not stat.S_ISDIR(file_stat.st_mode):
        raise AdapterError(f"{label} must be a real directory")


def _dataset_identity(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mode,
        file_stat.st_nlink,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _read_dataset_snapshot(path: Path) -> tuple[bytes, _DatasetSnapshot]:
    if not path.is_absolute():
        raise AdapterError("vLLM dataset must be an absolute resolved path")
    try:
        path_stat = os.lstat(path)
    except OSError:
        raise AdapterError("vLLM dataset is unavailable") from None
    if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1:
        raise AdapterError("vLLM dataset must be one regular non-symlink file")
    if path_stat.st_size > _MAX_DATASET_BYTES:
        raise AdapterError("vLLM dataset exceeds its size limit")
    expected_identity = _dataset_identity(path_stat)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise AdapterError("vLLM dataset cannot be opened safely") from None
    try:
        if _dataset_identity(os.fstat(descriptor)) != expected_identity:
            raise AdapterError("vLLM dataset changed before capture")
        content = bytearray()
        remaining = path_stat.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                raise AdapterError("vLLM dataset was truncated")
            content.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AdapterError("vLLM dataset grew during capture")
        if _dataset_identity(os.fstat(descriptor)) != expected_identity:
            raise AdapterError("vLLM dataset changed during capture")
        try:
            final_path_stat = os.lstat(path)
        except OSError:
            raise AdapterError("vLLM dataset changed during capture") from None
        if _dataset_identity(final_path_stat) != expected_identity:
            raise AdapterError("vLLM dataset changed during capture")
        dataset_bytes = bytes(content)
        return dataset_bytes, _DatasetSnapshot(
            identity=expected_identity,
            sha256=sha256_digest(dataset_bytes),
        )
    finally:
        os.close(descriptor)


def _validate_dataset(
    path: Path,
    spec: ExperimentSpec,
    plan: RequestPlan,
) -> _DatasetSnapshot:
    content, snapshot = _read_dataset_snapshot(path)
    if snapshot.sha256 != spec.workload.sha256:
        raise AdapterError("vLLM dataset bytes differ from resolved workload")
    try:
        prompts = parse_custom_workload(content)
    except SourceInputError as error:
        raise AdapterError("vLLM dataset is not valid custom JSONL") from error
    if len(prompts) < len(plan.requests):
        raise AdapterError("vLLM dataset has fewer prompts than request plan")
    for prompt, planned in zip(prompts, plan.requests, strict=False):
        prompt_digest = sha256_digest(prompt.encode("utf-8"))
        if prompt_digest != planned.prompt.sha256:
            raise AdapterError("vLLM dataset prompt differs from request plan")
        if isinstance(planned.prompt, InlinePrompt) and prompt != planned.prompt.text:
            raise AdapterError("vLLM inline prompt differs from request plan")
    return snapshot


def _require_dataset_unchanged(path: Path, snapshot: _DatasetSnapshot) -> None:
    try:
        current = os.lstat(path)
    except OSError:
        raise AdapterError("vLLM dataset changed during execution") from None
    if _dataset_identity(current) != snapshot.identity:
        raise AdapterError("vLLM dataset changed during execution")


def _validate_invocation_context(
    spec: ExperimentSpec,
    plan: RequestPlan,
    execution_fingerprint: str,
) -> Sha256Digest:
    if not isinstance(spec.execution, VllmExecution) or not isinstance(
        spec.target, AttachedVllmTarget
    ):
        raise AdapterError("vLLM invocation requires an attached vLLM experiment")
    if plan.experiment_id != spec.experiment.id or plan.traffic != spec.traffic:
        raise AdapterError("vLLM invocation request plan disagrees with experiment")
    if any(
        request.sampling.requested_output_tokens
        != spec.workload.requested_output_tokens
        or request.sampling.temperature != spec.workload.temperature
        or request.sampling.seed != spec.workload.seed
        for request in plan.requests
    ):
        raise AdapterError("vLLM request sampling differs from resolved workload")
    try:
        return TypeAdapter(Sha256Digest).validate_python(
            execution_fingerprint,
            strict=True,
        )
    except ValidationError:
        raise AdapterError("execution fingerprint is invalid") from None


def _invocation_metadata(
    spec: ExperimentSpec,
    plan: RequestPlan,
    execution_fingerprint: Sha256Digest,
) -> dict[str, str]:
    metadata = {
        "inferdrome_adapter_version": VLLM_ADAPTER_VERSION,
        "inferdrome_execution_fingerprint": execution_fingerprint,
        "inferdrome_producer_version": VLLM_VERSION,
        "inferdrome_run_id": plan.run_id,
        "inferdrome_workload_sha256": spec.workload.sha256,
    }
    if set(metadata).intersection(_RESERVED_NATIVE_KEYS):
        raise AdapterError("vLLM metadata collides with native result fields")
    return metadata


def _build_argv(
    spec: ExperimentSpec,
    plan: RequestPlan,
    paths: VllmInvocationPaths,
    metadata: Mapping[str, str],
    *,
    executable: str,
) -> tuple[str, ...]:
    if not isinstance(spec.target, AttachedVllmTarget):
        raise AdapterError("vLLM invocation requires an attached target")
    endpoint = str(spec.target.endpoint).rstrip("/")
    argv = [
        executable,
        "bench",
        "serve",
        "--backend",
        "openai-chat",
        "--base-url",
        endpoint,
        "--endpoint",
        "/v1/chat/completions",
        "--model",
        spec.target.model,
        "--tokenizer",
        str(paths.tokenizer_path),
        "--dataset-name",
        "custom",
        "--dataset-path",
        str(paths.dataset_path),
        "--custom-output-len",
        str(spec.workload.requested_output_tokens),
        "--num-prompts",
        str(len(plan.requests)),
        "--disable-shuffle",
        "--skip-chat-template",
    ]
    if isinstance(spec.traffic, ConcurrentTraffic):
        argv.extend(
            [
                "--request-rate",
                "inf",
                "--burstiness",
                "1",
                "--max-concurrency",
                str(spec.traffic.concurrency),
            ]
        )
    elif isinstance(spec.traffic, RequestRateTraffic):
        argv.extend(
            [
                "--request-rate",
                spec.traffic.requests_per_second,
                "--burstiness",
                spec.traffic.burstiness,
            ]
        )
        if spec.traffic.max_concurrency is not None:
            argv.extend(
                ["--max-concurrency", str(spec.traffic.max_concurrency)]
            )
    argv.extend(
        [
            "--num-warmups",
            str(spec.traffic.warmup_requests),
            "--ready-check-timeout-sec",
            "5",
            "--temperature",
            spec.workload.temperature,
            "--seed",
            str(spec.workload.seed),
            "--request-id-prefix",
            plan.producer_request_id_prefix,
            "--percentile-metrics",
            "e2el",
            "--metric-percentiles",
            "50,95,99",
            "--save-result",
            "--save-detailed",
            "--result-dir",
            str(paths.result_directory),
            "--result-filename",
            "benchmark-result.json",
            "--metadata",
            *(f"{key}={value}" for key, value in metadata.items()),
        ]
    )
    return tuple(argv)


def _canonical_invocation_bytes(
    argv: tuple[str, ...],
    metadata: Mapping[str, str],
    preflight: EndpointPreflightCapture,
    local_gpu_proof: LocalGpuProof | None,
) -> bytes:
    value = {
        "argv": argv,
        "endpoint_preflight": {
            "response_base64": base64.b64encode(preflight.response_bytes).decode(
                "ascii"
            ),
            "result": preflight.result.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            ),
        },
        "metadata": dict(metadata),
        "schema_version": "inferdrome.producer-invocation.v1",
    }
    if local_gpu_proof is not None:
        value["local_gpu_proof"] = local_gpu_proof.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
    return canonical_json_bytes(value)


def build_vllm_invocation(
    spec: ExperimentSpec,
    plan: RequestPlan,
    paths: VllmInvocationPaths,
    *,
    execution_fingerprint: str,
    preflight: EndpointPreflightCapture,
    local_gpu_proof: LocalGpuProof | None = None,
) -> VllmInvocation:
    """Build a secret-free argument vector for exactly vLLM 0.26.0."""

    validated_fingerprint = _validate_invocation_context(
        spec,
        plan,
        execution_fingerprint,
    )
    if not isinstance(spec.target, AttachedVllmTarget):
        raise AdapterError("vLLM invocation requires an attached target")
    validated_preflight = _validate_preflight_capture(spec.target, preflight)
    _validate_dataset(paths.dataset_path, spec, plan)
    _require_directory_no_follow(paths.tokenizer_path, label="vLLM tokenizer")
    _require_directory_no_follow(paths.result_directory, label="vLLM result directory")
    metadata = _invocation_metadata(spec, plan, validated_fingerprint)
    validated_gpu_proof: LocalGpuProof | None = None
    executable = "vllm"
    if local_gpu_proof is not None:
        validated_gpu_proof = validate_local_gpu_proof(
            spec,
            local_gpu_proof,
            run_id=plan.run_id,
        )
        if str(paths.tokenizer_path) != validated_gpu_proof.tokenizer_snapshot.root:
            raise AdapterError("local GPU proof tokenizer path disagrees")
        executable = validated_gpu_proof.producer_distribution.executable_path
    argv = _build_argv(
        spec,
        plan,
        paths,
        metadata,
        executable=executable,
    )
    evidence = _canonical_invocation_bytes(
        argv,
        metadata,
        validated_preflight,
        validated_gpu_proof,
    )
    return VllmInvocation(
        argv=argv,
        metadata=MappingProxyType(metadata),
        paths=paths,
        preflight=validated_preflight,
        evidence_bytes=evidence,
        local_gpu_proof=validated_gpu_proof,
    )


def _strict_invocation_object(content: bytes) -> dict[str, Any]:
    if len(content) > _MAX_INVOCATION_BYTES:
        raise AdapterError("vLLM invocation evidence exceeds its size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise AdapterError("vLLM invocation evidence is not valid UTF-8") from None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AdapterError("vLLM invocation evidence has duplicate keys")
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        raise AdapterError("vLLM invocation evidence has a non-finite number")

    try:
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except AdapterError:
        raise
    except (json.JSONDecodeError, RecursionError):
        raise AdapterError("vLLM invocation evidence is not valid JSON") from None
    if not isinstance(value, dict):
        raise AdapterError("vLLM invocation evidence must be a JSON object")
    return value


def _invocation_path(argv: tuple[str, ...], option: str) -> Path:
    positions = tuple(index for index, value in enumerate(argv) if value == option)
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise AdapterError("vLLM invocation path option is missing or duplicated")
    value = argv[positions[0] + 1]
    if not value or "\x00" in value:
        raise AdapterError("vLLM invocation path is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise AdapterError("vLLM invocation paths must be absolute")
    return path


def validate_vllm_invocation_evidence(
    content: bytes,
    spec: ExperimentSpec,
    plan: RequestPlan,
    *,
    execution_fingerprint: str,
) -> VllmInvocation:
    """Verify stored arguments are exactly reproducible from frozen inputs."""

    validated_fingerprint = _validate_invocation_context(
        spec,
        plan,
        execution_fingerprint,
    )
    value = _strict_invocation_object(content)
    base_fields = {
        "argv",
        "endpoint_preflight",
        "metadata",
        "schema_version",
    }
    accepted_field_sets = {
        frozenset(base_fields),
        frozenset(base_fields | {"local_gpu_proof"}),
    }
    if set(value) not in accepted_field_sets:
        raise AdapterError("vLLM invocation evidence has an unknown field set")
    if value["schema_version"] != "inferdrome.producer-invocation.v1":
        raise AdapterError("vLLM invocation evidence has an unsupported version")
    raw_argv = value["argv"]
    raw_preflight = value["endpoint_preflight"]
    raw_metadata = value["metadata"]
    raw_gpu_proof = value.get("local_gpu_proof")
    if (
        not isinstance(raw_argv, list)
        or not raw_argv
        or len(raw_argv) > 2_048
        or any(
            not isinstance(argument, str)
            or not argument
            or len(argument) > 8_192
            for argument in raw_argv
        )
    ):
        raise AdapterError("vLLM invocation argument vector is invalid")
    if not isinstance(raw_metadata, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in raw_metadata.items()
    ):
        raise AdapterError("vLLM invocation metadata is invalid")
    if not isinstance(raw_preflight, dict) or set(raw_preflight) != {
        "response_base64",
        "result",
    }:
        raise AdapterError("vLLM invocation preflight evidence is invalid")
    encoded_response = raw_preflight["response_base64"]
    if not isinstance(encoded_response, str):
        raise AdapterError("vLLM invocation preflight response is invalid")
    try:
        response_bytes = base64.b64decode(encoded_response, validate=True)
    except (binascii.Error, ValueError):
        raise AdapterError("vLLM invocation preflight response is invalid") from None
    if not isinstance(spec.target, AttachedVllmTarget):
        raise AdapterError("vLLM invocation requires an attached target")
    preflight = _validate_preflight_capture(
        spec.target,
        EndpointPreflightCapture(
            result=preflight_attached_endpoint(
                spec.target,
                transport=lambda _url, _timeout, _limit: HttpResponse(
                    status=200,
                    body=response_bytes,
                ),
            ).result,
            response_bytes=response_bytes,
        ),
    )
    expected_preflight_result = preflight.result.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=False,
    )
    if raw_preflight["result"] != expected_preflight_result:
        raise AdapterError("vLLM invocation preflight result is inconsistent")

    local_gpu_proof: LocalGpuProof | None = None
    executable = "vllm"
    if raw_gpu_proof is not None:
        try:
            parsed_gpu_proof = LocalGpuProof.model_validate_json(
                canonical_json_bytes(raw_gpu_proof)
            )
        except (TypeError, ValueError):
            raise AdapterError("vLLM local GPU proof is invalid") from None
        local_gpu_proof = validate_local_gpu_proof(
            spec,
            parsed_gpu_proof,
            run_id=plan.run_id,
        )
        executable = local_gpu_proof.producer_distribution.executable_path

    argv = tuple(raw_argv)
    expected_metadata = _invocation_metadata(spec, plan, validated_fingerprint)
    if raw_metadata != expected_metadata:
        raise AdapterError("vLLM invocation metadata differs from frozen inputs")
    paths = VllmInvocationPaths(
        dataset_path=_invocation_path(argv, "--dataset-path"),
        tokenizer_path=_invocation_path(argv, "--tokenizer"),
        result_directory=_invocation_path(argv, "--result-dir"),
    )
    if (
        local_gpu_proof is not None
        and str(paths.tokenizer_path) != local_gpu_proof.tokenizer_snapshot.root
    ):
        raise AdapterError("vLLM local GPU proof tokenizer path disagrees")
    expected_argv = _build_argv(
        spec,
        plan,
        paths,
        expected_metadata,
        executable=executable,
    )
    if argv != expected_argv:
        raise AdapterError("vLLM invocation arguments differ from frozen inputs")
    expected_bytes = _canonical_invocation_bytes(
        expected_argv,
        expected_metadata,
        preflight,
        local_gpu_proof,
    )
    if content != expected_bytes:
        raise AdapterError("vLLM invocation evidence is not canonical JSON")
    return VllmInvocation(
        argv=argv,
        metadata=MappingProxyType(expected_metadata),
        paths=paths,
        preflight=preflight,
        evidence_bytes=content,
        local_gpu_proof=local_gpu_proof,
    )


def parse_vllm_version_output(content: bytes) -> str:
    """Validate raw ``vllm --version`` output and return its observed version."""

    if len(content) > _MAX_VERSION_BYTES:
        raise AdapterError("vLLM version output exceeds its size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise AdapterError("vLLM version output is not valid UTF-8") from None
    version_lines = tuple(
        line.strip()
        for line in text.splitlines()
        if _VERSION_ONLY_LINE.fullmatch(line.strip())
    )
    if len(version_lines) != 1 or not _PINNED_VERSION_LINE.fullmatch(
        version_lines[0]
    ):
        raise AdapterError("vLLM version output does not identify pinned 0.26.0")
    return version_lines[0]


def probe_vllm_version(
    *,
    cwd: Path,
    executable: str = "vllm",
    process_runner: ProcessRunner = run_captured_process,
    termination_policy: TerminationPolicy | None = None,
    environment: Mapping[str, str] | None = None,
) -> VllmVersionProbeCapture:
    """Capture and validate the pinned producer identity before benchmarking."""

    process = process_runner(
        (executable, "--version"),
        cwd=cwd,
        max_runtime_seconds=30,
        output_limit_bytes=_MAX_VERSION_BYTES,
        termination_policy=termination_policy,
        environment=environment,
        merge_stderr=True,
    )
    if (
        process.termination is not ProcessTermination.EXITED
        or process.exit_status != 0
    ):
        raise AdapterError("vLLM version probe did not complete successfully")
    observed_version = parse_vllm_version_output(process.stdout)
    return VllmVersionProbeCapture(
        process=process,
        observed_version=observed_version,
    )


def _read_optional_native_result(path: Path, *, limit: int) -> bytes | None:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise AdapterError("vLLM native result cannot be inspected") from None
    if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1:
        raise AdapterError("vLLM native result must be one regular file")
    if path_stat.st_size > limit:
        raise AdapterError("vLLM native result exceeds its size limit")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise AdapterError("vLLM native result cannot be opened safely") from None
    try:
        opened_stat = os.fstat(descriptor)
        expected_identity = (
            path_stat.st_dev,
            path_stat.st_ino,
            path_stat.st_size,
            path_stat.st_mode,
            path_stat.st_nlink,
            path_stat.st_mtime_ns,
            path_stat.st_ctime_ns,
        )
        opened_identity = (
            opened_stat.st_dev,
            opened_stat.st_ino,
            opened_stat.st_size,
            opened_stat.st_mode,
            opened_stat.st_nlink,
            opened_stat.st_mtime_ns,
            opened_stat.st_ctime_ns,
        )
        if opened_identity != expected_identity or not stat.S_ISREG(
            opened_stat.st_mode
        ):
            raise AdapterError("vLLM native result changed before capture")
        content = bytearray()
        remaining = opened_stat.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                raise AdapterError("vLLM native result was truncated")
            content.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AdapterError("vLLM native result grew during capture")
        final_stat = os.fstat(descriptor)
        final_identity = (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mode,
            final_stat.st_nlink,
            final_stat.st_mtime_ns,
            final_stat.st_ctime_ns,
        )
        if final_identity != expected_identity:
            raise AdapterError("vLLM native result changed during capture")
        try:
            final_path_stat = os.lstat(path)
        except OSError:
            raise AdapterError(
                "vLLM native result changed during capture"
            ) from None
        final_path_identity = (
            final_path_stat.st_dev,
            final_path_stat.st_ino,
            final_path_stat.st_size,
            final_path_stat.st_mode,
            final_path_stat.st_nlink,
            final_path_stat.st_mtime_ns,
            final_path_stat.st_ctime_ns,
        )
        if final_path_identity != expected_identity:
            raise AdapterError("vLLM native result changed during capture")
        return bytes(content)
    finally:
        os.close(descriptor)


def execute_vllm_benchmark(
    invocation: VllmInvocation,
    spec: ExperimentSpec,
    plan: RequestPlan,
    *,
    execution_fingerprint: str,
    cancellation: CancellationToken | None = None,
    process_runner: ProcessRunner = run_captured_process,
    termination_policy: TerminationPolicy | None = None,
    environment: Mapping[str, str] | None = None,
    output_limit_bytes: int = 67_108_864,
    native_result_limit_bytes: int = 268_435_456,
) -> VllmBenchmarkCapture:
    """Run once, preserve diagnostics, and read the untouched native result once."""

    validated_invocation = validate_vllm_invocation_evidence(
        invocation.evidence_bytes,
        spec,
        plan,
        execution_fingerprint=execution_fingerprint,
    )
    if validated_invocation != invocation:
        raise AdapterError("in-memory vLLM invocation differs from its evidence")
    if not isinstance(spec.execution, VllmExecution):
        raise AdapterError("vLLM execution requires pinned execution settings")
    dataset_snapshot = _validate_dataset(
        invocation.paths.dataset_path,
        spec,
        plan,
    )
    _require_directory_no_follow(
        invocation.paths.tokenizer_path,
        label="vLLM tokenizer",
    )
    _require_directory_no_follow(
        invocation.paths.result_directory,
        label="vLLM result directory",
    )
    native_path = invocation.paths.result_directory / "benchmark-result.json"
    try:
        os.lstat(native_path)
    except FileNotFoundError:
        pass
    except OSError:
        raise AdapterError(
            "vLLM native result precondition cannot be checked"
        ) from None
    else:
        raise AdapterError("vLLM native result path already exists")

    process = process_runner(
        invocation.argv,
        cwd=invocation.paths.result_directory,
        max_runtime_seconds=spec.execution.max_runtime_seconds,
        output_limit_bytes=output_limit_bytes,
        cancellation=cancellation,
        termination_policy=termination_policy,
        environment=environment,
        merge_stderr=False,
    )
    _require_dataset_unchanged(
        invocation.paths.dataset_path,
        dataset_snapshot,
    )
    native_bytes = _read_optional_native_result(
        native_path,
        limit=native_result_limit_bytes,
    )
    if (
        process.termination is ProcessTermination.EXITED
        and process.exit_status == 0
        and native_bytes is None
    ):
        raise AdapterError("successful vLLM process did not produce native output")
    return VllmBenchmarkCapture(
        invocation=invocation,
        process=process,
        native_result_bytes=native_bytes,
    )
