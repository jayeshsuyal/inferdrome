"""Qualification of the local Docker Compose mock boundary.

The qualification command is a deliberately narrow execution boundary. It
runs the repository's accepted mock Compose file, checks one bounded synthetic
runner output, cleans one generated Compose project, verifies that project's
label-scoped residue, and publishes a local immutable report. It never starts
a provider, resolves credentials, uses a GPU, issues a deployment receipt, or
publishes evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from inferdrome import __version__
from inferdrome.compose_mock import mock_response_json_bytes
from inferdrome.deployment.spec import (
    DeploymentSpec,
    canonical_deployment_spec_bytes,
    deployment_spec_digest,
    parse_deployment_spec_json,
)
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes
from inferdrome.domain.ids import Sha256Digest, sha256_digest
from inferdrome.errors import CancellationRequested, SourceInputError
from inferdrome.execution.cancellation import CancellationToken
from inferdrome.immutable import publish_immutable_directory
from inferdrome.resolution.yaml_loader import load_strict_yaml
from inferdrome.vllm_compose import (
    SOURCE_REPOSITORY_URL,
    vllm_compose_contract,
    vllm_runtime_contract,
)

QUALIFICATION_SCHEMA_VERSION: Final = "inferdrome.deployment-qualification.v1"
QUALIFICATION_SCHEMA_ID: Final = "urn:inferdrome:deployment-qualification:v1"
QUALIFICATION_FILENAME: Final = "qualification.json"
QUALIFICATION_PROJECT_PREFIX: Final = "inferdrome-qual-"
QUALIFICATION_OUTPUT_SCHEMA_VERSION: Final = "inferdrome.runner-probe-output.v1"
QUALIFICATION_CLEANUP_ACTION: Final = "docker compose down --remove-orphans --volumes"
QUALIFICATION_SCOPE_LABEL: Final = "com.docker.compose.project"
QUALIFICATION_MAX_BYTES: Final = 262_144
QUALIFICATION_MAX_OUTPUT_BYTES: Final = 64 * 1024
QUALIFICATION_MAX_DIAGNOSTIC_BYTES: Final = 64 * 1024
QUALIFICATION_MAX_RESIDUAL_RESOURCES: Final = 100
QUALIFICATION_TIMEOUT_SECONDS: Final = 1_800.0
QUALIFICATION_PROJECT_PATTERN: Final = re.compile(
    r"^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$"
)
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:[.-][0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_RESOURCE_ID_PATTERN = re.compile(r"^[0-9a-f]{12,64}$")
_REPOSITORY_DIGEST_PATTERN = re.compile(r"^.+@(?P<digest>sha256:[0-9a-f]{64})$")
_SENSITIVE_KEYS = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "authorization",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "credential",
        "password",
        "privatekey",
        "secret",
        "secretvalue",
        "token",
    }
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
COMPOSE_FILE: Final = REPOSITORY_ROOT / "compose.yaml"
DEPLOYMENT_SPEC_FILE: Final = (
    REPOSITORY_ROOT / "deployments" / "v1" / "examples" / "local-mock.json"
)
COMPOSE_CONTRACT_FILE: Final = (
    REPOSITORY_ROOT / "compose" / "vllm-compose-contract.json"
)
RUNTIME_CONTRACT_FILE: Final = (
    REPOSITORY_ROOT / "compose" / "vllm-runtime-contract.json"
)
_MOCK_ENGINE_IMAGE = "inferdrome/compose-mock:development"
_RUNNER_IMAGE = "inferdrome/runner:development"
_MOCK_ENDPOINT = "http://mock-engine.internal:8000/v1/chat/completions"
_MOCK_MODEL = "inferdrome/mock-model"
_MOCK_PROMPT = "deterministic local Compose smoke"
_EXPECTED_COMPOSE_SHAPE = {"mock-engine", "synthetic-smoke"}


class QualificationError(Exception):
    """A bounded qualification failure with no command or secret payload."""

    def __init__(self, message: str, *, code: str = "QUALIFICATION_FAILED") -> None:
        self.code = code
        super().__init__(message)


class QualificationPublicationError(QualificationError):
    """An immutable qualification report failed publication or read-back."""


class QualificationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )


ProjectName = StringConstraints(
    min_length=1,
    max_length=63,
    pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$",
)
Commit = StringConstraints(min_length=40, max_length=64, pattern=r"^[0-9a-f]{40,64}$")
QualificationVersion = StringConstraints(
    min_length=5,
    max_length=64,
    pattern=(
        r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
        r"(?:[.-][0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
    ),
)


class QualificationImageObservation(QualificationModel):
    """Image identity returned by Docker, never copied from Compose config."""

    service: Literal["mock-engine", "synthetic-smoke"]
    observation_status: Literal["OBSERVED", "UNAVAILABLE"]
    image_id: Sha256Digest | None = None
    repository_digests: tuple[Sha256Digest, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.observation_status == "OBSERVED" and self.image_id is None:
            raise ValueError("observed image requires an image id")
        if self.observation_status == "UNAVAILABLE" and (
            self.image_id is not None or self.repository_digests
        ):
            raise ValueError("unavailable image cannot contain an identity")
        return self


class QualificationOutputObservation(QualificationModel):
    """Bounded facts about the exact runner-output bytes that were checked."""

    schema_version: Literal["inferdrome.runner-probe-output.v1"]
    sha256: Sha256Digest
    byte_length: int = Field(strict=True, ge=1, le=QUALIFICATION_MAX_OUTPUT_BYTES)
    synthetic_only: Literal[True]
    evidence_eligible: Literal[False]
    verified_bounded_output: Literal[True]


class QualificationCleanup(QualificationModel):
    """Successful exact-project cleanup and residual inspection."""

    action: Literal["docker compose down --remove-orphans --volumes"]
    scope_label: Literal["com.docker.compose.project"]
    project_name: Annotated[str, ProjectName]
    attempts: int = Field(strict=True, ge=1, le=3)
    exit_code: int = Field(strict=True, ge=0, le=255)
    residual_container_count: Literal[0]
    residual_network_count: Literal[0]
    residual_volume_count: Literal[0]
    cleanup_confirmed: Literal[True]


class QualificationPayload(QualificationModel):
    """Report fields excluding the domain-separated report identity."""

    schema_version: Literal["inferdrome.deployment-qualification.v1"]
    schema_id: Literal["urn:inferdrome:deployment-qualification:v1"]
    qualification_kind: Literal["local_compose_mock"]
    observation_status: Literal["COMPLETE"]
    qualification_mode: Literal["SYNTHETIC_ONLY"]
    synthetic_only: Literal[True]
    evidence_eligible: Literal[False]
    provider_execution: Literal["NOT_PERFORMED"]
    gpu_execution: Literal["NOT_PERFORMED"]
    deployment_receipt_issued: Literal[False]
    evidence_published: Literal[False]
    source_repository: Literal["https://github.com/jayeshsuyal/inferdrome"]
    source_revision: Annotated[str, Commit]
    source_worktree_clean: Literal[True]
    inferdrome_version: Annotated[str, QualificationVersion]
    deployment_spec_digest: Sha256Digest
    compose_file_sha256: Sha256Digest
    compose_contract_sha256: Sha256Digest
    runtime_contract_sha256: Sha256Digest
    compose_project: Annotated[str, ProjectName]
    endpoint: Literal["http://mock-engine.internal:8000/v1/chat/completions"]
    image_observations: tuple[QualificationImageObservation, ...] = Field(
        min_length=2,
        max_length=2,
    )
    output_observation: QualificationOutputObservation
    cleanup: QualificationCleanup

    @model_validator(mode="after")
    def validate_report_boundary(self) -> Self:
        if tuple(item.service for item in self.image_observations) != (
            "mock-engine",
            "synthetic-smoke",
        ):
            raise ValueError("qualification image observations are not ordered")
        if self.cleanup.project_name != self.compose_project:
            raise ValueError("qualification cleanup project disagrees")
        if (
            not self.output_observation.synthetic_only
            or self.output_observation.evidence_eligible
        ):
            raise ValueError("qualification output crossed the synthetic boundary")
        return self


class QualificationReport(QualificationPayload):
    """Strict immutable report with a non-recursive canonical identity."""

    qualification_id: Sha256Digest

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: Any | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        _preflight_json(json_data)
        return super().model_validate_json(
            json_data,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.qualification_id != qualification_id(self):
            raise ValueError("qualification identity does not match payload")
        return self


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class ProcessRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProcessResult: ...


class BoundedSubprocessRunner:
    """Run argv without a shell while retaining bounded diagnostics."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProcessResult:
        if (
            not argv
            or len(argv) > 64
            or any(
                not isinstance(item, str)
                or not item
                or len(item.encode("utf-8")) > 4_096
                or "\x00" in item
                for item in argv
            )
            or not 0.1 <= timeout_seconds <= QUALIFICATION_TIMEOUT_SECONDS
        ):
            raise QualificationError("qualification subprocess input is invalid")
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or "\x00" in key
            or "\x00" in value
            or len(key) > 256
            or len(value) > 4_096
            for key, value in env.items()
        ):
            raise QualificationError("qualification subprocess environment is invalid")

        try:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
                shell=False,
                start_new_session=True,
            )
        except (OSError, ValueError):
            raise QualificationError(
                "qualification subprocess could not start"
            ) from None

        stdout_bytes = bytearray()
        stderr_bytes = bytearray()

        def drain(stream: Any, destination: bytearray) -> None:
            try:
                while True:
                    chunk = stream.read(8_192)
                    if not chunk:
                        return
                    if len(destination) < QUALIFICATION_MAX_DIAGNOSTIC_BYTES:
                        remaining = (
                            QUALIFICATION_MAX_DIAGNOSTIC_BYTES - len(destination)
                        )
                        destination.extend(chunk[:remaining])
            except (AttributeError, OSError, TypeError, ValueError):
                return

        stdout_thread = threading.Thread(
            target=drain,
            args=(process.stdout, stdout_bytes),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=drain,
            args=(process.stderr, stderr_bytes),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(process)
        except BaseException:
            self._terminate(process)
            raise
        finally:
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
        return ProcessResult(
            124 if timed_out else process.returncode,
            bytes(stdout_bytes),
            bytes(stderr_bytes),
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(OSError):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            with contextlib.suppress(OSError):
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            with contextlib.suppress(OSError):
                process.wait(timeout=2)


@dataclass(frozen=True)
class PublishedQualificationReport:
    path: Path
    report: QualificationReport
    report_sha256: Sha256Digest


@dataclass(frozen=True)
class QualificationExecution:
    report: QualificationReport
    report_path: Path
    report_sha256: Sha256Digest


@dataclass(frozen=True)
class _ResidualResources:
    containers: int
    networks: int
    volumes: int


@dataclass(frozen=True)
class _CleanupObservation:
    attempts: int
    exit_code: int
    residual: _ResidualResources | None
    confirmed: bool
    failure_code: str


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("qualification JSON object keys must be unique")
        value[key] = child
    return value


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite qualification JSON value {value} is forbidden")


def _reject_sensitive_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("qualification object keys must be strings")
            normalized = "".join(
                character.lower() for character in key if character.isalnum()
            )
            if normalized in _SENSITIVE_KEYS:
                raise ValueError("credential-shaped qualification fields are forbidden")
            _reject_sensitive_fields(child)
    elif isinstance(value, list | tuple):
        for child in value:
            _reject_sensitive_fields(child)


def _preflight_json(payload: str | bytes | bytearray) -> bytes:
    if isinstance(payload, str):
        raw = payload.encode("utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    else:
        raise ValueError("qualification JSON input is invalid")
    if len(raw) > QUALIFICATION_MAX_BYTES:
        raise ValueError("qualification JSON exceeds its bound")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonfinite,
        )
    except (
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        if str(error) == "qualification JSON object keys must be unique":
            raise ValueError(str(error)) from None
        raise ValueError("qualification JSON is invalid") from None
    if not isinstance(value, dict):
        raise ValueError("qualification JSON root must be an object")
    _reject_sensitive_fields(value)
    return raw


def _json_value(model: BaseModel) -> dict[str, Any]:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("qualification model must serialize as an object")
    return value


def qualification_payload_json_value(
    report: QualificationReport | QualificationPayload,
) -> dict[str, Any]:
    value = _json_value(report)
    value.pop("qualification_id", None)
    return value


def canonical_qualification_payload_bytes(
    report: QualificationReport | QualificationPayload,
) -> bytes:
    return canonical_json_bytes(qualification_payload_json_value(report))


def qualification_id(report: QualificationReport | QualificationPayload) -> str:
    return digest_bytes(
        DigestDomain.DEPLOYMENT_QUALIFICATION,
        canonical_qualification_payload_bytes(report),
    )


def canonical_qualification_bytes(report: QualificationReport) -> bytes:
    return canonical_json_bytes(_json_value(report))


def qualification_sha256(report: QualificationReport) -> Sha256Digest:
    return sha256_digest(canonical_qualification_bytes(report))


def parse_qualification_json(payload: str | bytes | bytearray) -> QualificationReport:
    raw = _preflight_json(payload)
    try:
        return QualificationReport.model_validate_json(raw)
    except (TypeError, ValueError):
        raise ValueError("qualification JSON failed strict validation") from None


def verify_qualification_report_bytes(
    payload: str | bytes | bytearray,
    *,
    expected_project: str | None = None,
    expected_source_revision: str | None = None,
    expected_deployment_spec_digest: str | None = None,
    expected_compose_file_sha256: str | None = None,
    expected_compose_contract_sha256: str | None = None,
    expected_runtime_contract_sha256: str | None = None,
    expected_output_sha256: str | None = None,
) -> QualificationReport:
    raw = _preflight_json(payload)
    report = parse_qualification_json(raw)
    if raw != canonical_qualification_bytes(report):
        raise ValueError("qualification bytes are not canonical")
    expected_pairs = (
        (expected_project, report.compose_project),
        (expected_source_revision, report.source_revision),
        (expected_deployment_spec_digest, report.deployment_spec_digest),
        (expected_compose_file_sha256, report.compose_file_sha256),
        (expected_compose_contract_sha256, report.compose_contract_sha256),
        (expected_runtime_contract_sha256, report.runtime_contract_sha256),
        (expected_output_sha256, report.output_observation.sha256),
    )
    if any(
        expected is not None and expected != actual
        for expected, actual in expected_pairs
    ):
        raise ValueError("qualification cross-input binding failed")
    return report


def qualification_schema() -> dict[str, Any]:
    generated = QualificationReport.model_json_schema(
        by_alias=True,
        mode="validation",
        ref_template="#/$defs/{model}",
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": QUALIFICATION_SCHEMA_ID,
        "$comment": (
            "Additive local Docker Compose orchestration observation only. "
            "This report is synthetic, evidence-ineligible, and outside "
            "frozen public evidence schemas; it assigns no acceptance verdict."
        ),
        **generated,
    }


def _absolute_path(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise QualificationError(f"{label} path must be absolute", code="PATH_INVALID")
    selected = Path(os.path.abspath(path))
    if len(str(selected).encode("utf-8")) > 4_096 or ".." in selected.parts:
        raise QualificationError(f"{label} path is invalid", code="PATH_INVALID")
    current = Path(selected.anchor)
    for component in selected.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError:
            raise QualificationError(
                f"{label} path is unavailable", code="PATH_INVALID"
            ) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise QualificationError(f"{label} path is unsafe", code="PATH_INVALID")
    return selected


def _existing_directory(path: Path, label: str) -> Path:
    selected = _absolute_path(path, label)
    try:
        metadata = os.lstat(selected)
    except OSError:
        raise QualificationError(
            f"{label} is unavailable", code="PATH_INVALID"
        ) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not os.access(selected, os.R_OK | os.W_OK | os.X_OK)
    ):
        raise QualificationError(f"{label} is unsafe", code="PATH_INVALID")
    return selected


def _regular_file(path: Path, label: str, maximum: int) -> bytes:
    selected = _absolute_path(path, label)
    try:
        metadata = os.lstat(selected)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > maximum
            or metadata.st_nlink != 1
        ):
            raise QualificationError(f"{label} is unsafe", code="PATH_INVALID")
        descriptor = os.open(
            selected,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except QualificationError:
        raise
    except OSError:
        raise QualificationError(
            f"{label} is unavailable", code="PATH_INVALID"
        ) from None
    try:
        final_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(final_metadata.st_mode)
            or final_metadata.st_nlink != 1
            or final_metadata.st_size > maximum
        ):
            raise QualificationError(f"{label} is unsafe", code="PATH_INVALID")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > maximum:
            raise QualificationError(f"{label} exceeds its bound", code="INPUT_BOUNDED")
        return bytes(content)
    except QualificationError:
        raise
    except OSError:
        raise QualificationError(
            f"{label} could not be read", code="PATH_INVALID"
        ) from None
    finally:
        os.close(descriptor)


def _validate_project(value: str) -> str:
    if (
        not isinstance(value, str)
        or QUALIFICATION_PROJECT_PATTERN.fullmatch(value) is None
    ):
        raise QualificationError(
            "qualification project identity is invalid", code="PROJECT_INVALID"
        )
    return value


def _new_project() -> str:
    return _validate_project(QUALIFICATION_PROJECT_PREFIX + secrets.token_hex(16))


def _safe_environment(*, uid: int, gid: int, evidence_dir: Path) -> dict[str, str]:
    if not 1 <= uid <= 65_534 or not 1 <= gid <= 65_534:
        raise QualificationError(
            "qualification container identity is invalid", code="IDENTITY_INVALID"
        )
    result: dict[str, str] = {}
    for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"):
        value = os.environ.get(key)
        if value is not None and len(value) <= 4_096 and "\x00" not in value:
            result[key] = value
    result.update(
        {
            "INFERDROME_COMPOSE_UID": str(uid),
            "INFERDROME_COMPOSE_GID": str(gid),
            "INFERDROME_COMPOSE_EVIDENCE_DIR": str(evidence_dir),
        }
    )
    return result


def _result(
    runner: ProcessRunner,
    argv: Sequence[str],
    env: Mapping[str, str],
    timeout: float,
) -> ProcessResult:
    try:
        result = runner.run(argv, env=env, timeout_seconds=timeout)
    except QualificationError:
        raise
    except (CancellationRequested, KeyboardInterrupt):
        raise
    except BaseException:
        raise QualificationError(
            "qualification subprocess failed", code="SUBPROCESS_FAILED"
        ) from None
    if not isinstance(result, ProcessResult):
        raise QualificationError(
            "qualification subprocess result is invalid", code="SUBPROCESS_FAILED"
        )
    if (
        type(result.returncode) is not int
        or not -255 <= result.returncode <= 255
        or not isinstance(result.stdout, bytes)
        or not isinstance(result.stderr, bytes)
        or len(result.stdout) > QUALIFICATION_MAX_DIAGNOSTIC_BYTES
        or len(result.stderr) > QUALIFICATION_MAX_DIAGNOSTIC_BYTES
    ):
        raise QualificationError(
            "qualification subprocess result is unbounded", code="SUBPROCESS_FAILED"
        )
    return result


def _decode_ascii_line(raw: bytes, label: str) -> str:
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError:
        raise QualificationError(
            f"{label} is invalid", code="OBSERVATION_INVALID"
        ) from None
    lines = [line for line in value.splitlines() if line]
    if len(lines) != 1 or len(lines[0]) > 4_096:
        raise QualificationError(f"{label} is invalid", code="OBSERVATION_INVALID")
    return lines[0]


def _observe_source_revision(
    runner: ProcessRunner,
    *,
    repository_root: Path,
    env: Mapping[str, str],
) -> str:
    head = _result(
        runner,
        ["git", "-C", str(repository_root), "rev-parse", "--verify", "HEAD"],
        env,
        30.0,
    )
    status = _result(
        runner,
        [
            "git",
            "-C",
            str(repository_root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        env,
        30.0,
    )
    if head.returncode != 0 or status.returncode != 0 or status.stdout.strip():
        raise QualificationError(
            "qualification requires a clean source checkout", code="SOURCE_DIRTY"
        )
    value = _decode_ascii_line(head.stdout, "source revision")
    if _COMMIT_PATTERN.fullmatch(value) is None:
        raise QualificationError(
            "source revision is invalid", code="OBSERVATION_INVALID"
        )
    return value


def _validate_local_docker_context(
    runner: ProcessRunner,
    *,
    env: Mapping[str, str],
) -> None:
    context = _result(
        runner,
        [
            "docker",
            "context",
            "inspect",
            "--format",
            '{{json (index .Endpoints "docker").Host}}',
        ],
        env,
        30.0,
    )
    if context.returncode != 0:
        raise QualificationError(
            "Docker context could not be inspected", code="DOCKER_UNAVAILABLE"
        )
    try:
        host = json.loads(_decode_ascii_line(context.stdout, "Docker context"))
    except (ValueError, json.JSONDecodeError):
        raise QualificationError(
            "Docker context is invalid", code="REMOTE_DOCKER_DISALLOWED"
        ) from None
    if (
        not isinstance(host, str)
        or not (host.startswith("unix://") or host.startswith("npipe://"))
        or "@" in host
    ):
        raise QualificationError(
            "qualification requires a local Docker context",
            code="REMOTE_DOCKER_DISALLOWED",
        )


def _parse_json_object(raw: bytes, label: str) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonfinite,
        )
    except (
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ):
        raise QualificationError(
            f"{label} is invalid", code="CONTRACT_INVALID"
        ) from None


def _parse_contract(raw: bytes, expected: object, label: str) -> Sha256Digest:
    if _parse_json_object(raw, label) != expected:
        raise QualificationError(
            f"{label} is not the accepted contract", code="CONTRACT_INVALID"
        )
    return sha256_digest(raw)


def _validate_accepted_compose(raw: bytes) -> None:
    try:
        document = load_strict_yaml(raw)
    except (SourceInputError, TypeError, ValueError):
        raise QualificationError(
            "Compose file is not strict YAML", code="CONTRACT_INVALID"
        ) from None
    services = document.get("services")
    if not isinstance(services, dict) or set(services) != _EXPECTED_COMPOSE_SHAPE:
        raise QualificationError(
            "Compose file does not contain the accepted mock services",
            code="CONTRACT_INVALID",
        )
    text = raw.decode("utf-8", errors="replace").lower()
    if any(
        marker in text
        for marker in (
            "privileged: true",
            "network_mode: host",
            "/var/run/docker.sock",
            "runtime: nvidia",
            "deploy:",
            "ports:",
        )
    ):
        raise QualificationError(
            "Compose file contains a disallowed escape hatch", code="CONTRACT_INVALID"
        )
    mock = services.get("mock-engine")
    probe = services.get("synthetic-smoke")
    if not isinstance(mock, dict) or not isinstance(probe, dict):
        raise QualificationError(
            "Compose mock services are invalid", code="CONTRACT_INVALID"
        )
    if mock.get("image") != _MOCK_ENGINE_IMAGE or probe.get("image") != _RUNNER_IMAGE:
        raise QualificationError(
            "Compose mock images are not accepted", code="CONTRACT_INVALID"
        )
    if probe.get("entrypoint") != [
        "/opt/inferdrome-runtime/bin/inferdrome-runner-probe"
    ]:
        raise QualificationError(
            "Compose runner role is not the synthetic probe", code="CONTRACT_INVALID"
        )
    for service in (mock, probe):
        if (
            service.get("restart") != "no"
            or service.get("security_opt") != ["no-new-privileges:true"]
            or service.get("cap_drop") != ["ALL"]
            or "ports" in service
            or "deploy" in service
        ):
            raise QualificationError(
                "Compose service safety contract is invalid", code="CONTRACT_INVALID"
            )
    networks = document.get("networks")
    if (
        not isinstance(networks, dict)
        or not isinstance(networks.get("inferdrome-internal"), dict)
        or networks["inferdrome-internal"].get("internal") is not True
    ):
        raise QualificationError(
            "Compose network is not internal", code="CONTRACT_INVALID"
        )


def _load_inputs() -> tuple[
    DeploymentSpec,
    Sha256Digest,
    Sha256Digest,
    Sha256Digest,
    Sha256Digest,
]:
    spec_raw = _regular_file(DEPLOYMENT_SPEC_FILE, "deployment specification", 262_144)
    compose_raw = _regular_file(COMPOSE_FILE, "Compose file", 262_144)
    compose_contract_raw = _regular_file(
        COMPOSE_CONTRACT_FILE, "Compose contract", QUALIFICATION_MAX_BYTES
    )
    runtime_contract_raw = _regular_file(
        RUNTIME_CONTRACT_FILE, "runtime contract", QUALIFICATION_MAX_BYTES
    )
    try:
        spec = parse_deployment_spec_json(spec_raw)
    except (TypeError, ValueError):
        raise QualificationError(
            "deployment specification is invalid", code="CONTRACT_INVALID"
        ) from None
    if (
        spec.provider.provider_id != "local"
        or spec.execution_intent != "mock_only"
        or spec.mode != "development"
        or spec.resources.gpu_count != 0
    ):
        raise QualificationError(
            "deployment specification is not the local mock contract",
            code="CONTRACT_INVALID",
        )
    _validate_accepted_compose(compose_raw)
    compose_digest = _parse_contract(
        compose_contract_raw, vllm_compose_contract(), "Compose contract"
    )
    runtime_digest = _parse_contract(
        runtime_contract_raw, vllm_runtime_contract(), "runtime contract"
    )
    canonical_spec = canonical_deployment_spec_bytes(spec)
    if deployment_spec_digest(spec) != deployment_spec_digest(
        parse_deployment_spec_json(canonical_spec)
    ):
        raise QualificationError(
            "deployment specification canonicalization changed", code="CONTRACT_INVALID"
        )
    return (
        spec,
        deployment_spec_digest(spec),
        sha256_digest(compose_raw),
        compose_digest,
        runtime_digest,
    )


def _expected_runner_output() -> dict[str, object]:
    request = {
        "messages": [{"content": _MOCK_PROMPT, "role": "user"}],
        "max_tokens": 16,
        "model": _MOCK_MODEL,
        "temperature": 0,
    }
    response = mock_response_json_bytes()
    return {
        "schema_version": QUALIFICATION_OUTPUT_SCHEMA_VERSION,
        "runner_version": __version__,
        "execution_mode": "synthetic_endpoint_probe",
        "synthetic_only": True,
        "evidence_eligible": False,
        "status": "SUCCEEDED",
        "endpoint_sha256": sha256_digest(_MOCK_ENDPOINT.encode("utf-8")),
        "model": _MOCK_MODEL,
        "request_sha256": sha256_digest(canonical_json_bytes(request)),
        "response_sha256": sha256_digest(response),
        "response_status": 200,
        "response_bytes": len(response),
    }


def expected_compose_output_bytes() -> bytes:
    """Return the exact bounded synthetic output expected from compose.yaml."""

    return canonical_json_bytes(_expected_runner_output())


def _parse_output(raw: bytes) -> QualificationOutputObservation:
    if len(raw) == 0 or len(raw) > QUALIFICATION_MAX_OUTPUT_BYTES:
        raise QualificationError(
            "synthetic Compose output is outside its bound", code="OUTPUT_INVALID"
        )
    try:
        decoded_raw = _preflight_json(raw)
        decoded = json.loads(
            decoded_raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonfinite,
        )
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        raise QualificationError(
            "synthetic Compose output is invalid", code="OUTPUT_INVALID"
        ) from None
    if decoded != _expected_runner_output() or decoded_raw != canonical_json_bytes(
        decoded
    ):
        raise QualificationError(
            "synthetic Compose output does not match the bounded mock vector",
            code="OUTPUT_INVALID",
        )
    return QualificationOutputObservation(
        schema_version=QUALIFICATION_OUTPUT_SCHEMA_VERSION,
        sha256=sha256_digest(raw),
        byte_length=len(raw),
        synthetic_only=True,
        evidence_eligible=False,
        verified_bounded_output=True,
    )


def _parse_ids(raw: bytes, label: str) -> list[str]:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise QualificationError(
            f"{label} identifiers are invalid", code="RESIDUAL_INSPECTION_FAILED"
        ) from None
    lines = [line for line in text.splitlines() if line]
    if len(lines) > QUALIFICATION_MAX_RESIDUAL_RESOURCES:
        raise QualificationError(
            f"{label} residual count is outside its bound",
            code="RESIDUAL_INSPECTION_FAILED",
        )
    if any(_RESOURCE_ID_PATTERN.fullmatch(line) is None for line in lines):
        raise QualificationError(
            f"{label} identifiers are invalid", code="RESIDUAL_INSPECTION_FAILED"
        )
    return lines


def _inspect_labels(
    runner: ProcessRunner,
    *,
    ids: list[str],
    kind: Literal["container", "network", "volume"],
    project: str,
    env: Mapping[str, str],
) -> None:
    if not ids:
        return
    inspect_command = {
        "container": "container",
        "network": "network",
        "volume": "volume",
    }[kind]
    format_value = (
        "{{json .Config.Labels}}" if kind == "container" else "{{json .Labels}}"
    )
    result = _result(
        runner,
        ["docker", inspect_command, "inspect", "--format", format_value, *ids],
        env,
        30.0,
    )
    if result.returncode != 0:
        raise QualificationError(
            "scoped residual labels could not be inspected",
            code="RESIDUAL_INSPECTION_FAILED",
        )
    try:
        lines = result.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise QualificationError(
            "scoped residual labels are invalid", code="RESIDUAL_INSPECTION_FAILED"
        ) from None
    if (
        len(lines) != len(ids)
        or len(result.stdout) > QUALIFICATION_MAX_DIAGNOSTIC_BYTES
    ):
        raise QualificationError(
            "scoped residual labels are unbounded", code="RESIDUAL_INSPECTION_FAILED"
        )
    for line in lines:
        try:
            labels = json.loads(line, object_pairs_hook=_unique_json_object)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            raise QualificationError(
                "scoped residual labels are invalid", code="RESIDUAL_INSPECTION_FAILED"
            ) from None
        if (
            not isinstance(labels, dict)
            or labels.get(QUALIFICATION_SCOPE_LABEL) != project
        ):
            raise QualificationError(
                "scoped residual label disagrees", code="RESIDUAL_INSPECTION_FAILED"
            )


def _resource_ids(
    runner: ProcessRunner,
    *,
    kind: Literal["container", "network", "volume"],
    project: str,
    env: Mapping[str, str],
) -> list[str]:
    command = {
        "container": ["docker", "ps", "-aq"],
        "network": ["docker", "network", "ls", "-q"],
        "volume": ["docker", "volume", "ls", "-q"],
    }[kind]
    result = _result(
        runner,
        [*command, "--filter", f"label={QUALIFICATION_SCOPE_LABEL}={project}"],
        env,
        30.0,
    )
    if result.returncode != 0:
        raise QualificationError(
            "scoped residual resources could not be listed",
            code="RESIDUAL_INSPECTION_FAILED",
        )
    ids = _parse_ids(result.stdout, kind)
    _inspect_labels(runner, ids=ids, kind=kind, project=project, env=env)
    return ids


def _inspect_residuals(
    runner: ProcessRunner,
    *,
    project: str,
    env: Mapping[str, str],
) -> _ResidualResources:
    return _ResidualResources(
        containers=len(
            _resource_ids(runner, kind="container", project=project, env=env)
        ),
        networks=len(_resource_ids(runner, kind="network", project=project, env=env)),
        volumes=len(_resource_ids(runner, kind="volume", project=project, env=env)),
    )


def _cleanup_compose(
    runner: ProcessRunner,
    *,
    compose_file: Path,
    project: str,
    env: Mapping[str, str],
    attempts_allowed: int,
) -> _CleanupObservation:
    cleanup_argv = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(compose_file),
        "down",
        "--remove-orphans",
        "--volumes",
    ]
    attempts = 0
    exit_code = 125
    for _ in range(attempts_allowed):
        attempts += 1
        try:
            result = _result(runner, cleanup_argv, env, 120.0)
            exit_code = result.returncode if 0 <= result.returncode <= 255 else 125
        except BaseException:
            exit_code = 125
        if exit_code == 0:
            break
    try:
        residual = _inspect_residuals(runner, project=project, env=env)
    except BaseException:
        return _CleanupObservation(
            attempts, exit_code, None, False, "RESIDUAL_INSPECTION_FAILED"
        )
    confirmed = exit_code == 0 and (
        residual.containers,
        residual.networks,
        residual.volumes,
    ) == (0, 0, 0)
    return _CleanupObservation(
        attempts,
        exit_code,
        residual,
        confirmed,
        ""
        if confirmed
        else ("RESIDUAL_RESOURCES" if exit_code == 0 else "CLEANUP_FAILED"),
    )


def _observe_images(
    runner: ProcessRunner,
    *,
    env: Mapping[str, str],
) -> tuple[QualificationImageObservation, ...]:
    observations: list[QualificationImageObservation] = []
    image_specs: tuple[
        tuple[Literal["mock-engine", "synthetic-smoke"], str], ...
    ] = (
        ("mock-engine", _MOCK_ENGINE_IMAGE),
        ("synthetic-smoke", _RUNNER_IMAGE),
    )
    for service, image in image_specs:
        try:
            image_id_result = _result(
                runner,
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{json .Id}}",
                    image,
                ],
                env,
                30.0,
            )
            repo_digests_result = _result(
                runner,
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{json .RepoDigests}}",
                    image,
                ],
                env,
                30.0,
            )
            if (
                image_id_result.returncode != 0
                or repo_digests_result.returncode != 0
            ):
                raise ValueError
            image_id = json.loads(
                _decode_ascii_line(image_id_result.stdout, "Docker image identity")
            )
            repo_digests = json.loads(
                _decode_ascii_line(
                    repo_digests_result.stdout, "Docker image repository digests"
                )
            )
            if (
                not isinstance(image_id, str)
                or _DIGEST_PATTERN.fullmatch(image_id) is None
            ):
                raise ValueError
            if not isinstance(repo_digests, list) or len(repo_digests) > 8:
                raise ValueError
            parsed_digests: list[str] = []
            for item in repo_digests:
                if not isinstance(item, str):
                    raise ValueError
                match = _REPOSITORY_DIGEST_PATTERN.fullmatch(item)
                if match is None:
                    raise ValueError
                parsed_digests.append(match.group("digest"))
            observations.append(
                QualificationImageObservation(
                    service=service,
                    observation_status="OBSERVED",
                    image_id=image_id,
                    repository_digests=tuple(parsed_digests),
                )
            )
        except (QualificationError, TypeError, ValueError, UnicodeError):
            observations.append(
                QualificationImageObservation(
                    service=service,
                    observation_status="UNAVAILABLE",
                    image_id=None,
                    repository_digests=(),
                )
            )
    return tuple(observations)


def _publish_report(
    root: Path, report: QualificationReport
) -> PublishedQualificationReport:
    raw = canonical_qualification_bytes(report)
    try:
        verify_qualification_report_bytes(raw, expected_project=report.compose_project)
        path = publish_immutable_directory(
            root=root,
            artifact_id=report.qualification_id,
            filename=QUALIFICATION_FILENAME,
            content=raw,
        )
        directory = path.absolute()
        metadata = os.lstat(directory)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise QualificationPublicationError(
                "qualification report directory is unsafe",
                code="REPORT_PUBLICATION_FAILED",
            )
        stored = directory / QUALIFICATION_FILENAME
        read_back = _regular_file(
            stored, "published qualification report", QUALIFICATION_MAX_BYTES
        )
        parsed = verify_qualification_report_bytes(
            read_back,
            expected_project=report.compose_project,
            expected_source_revision=report.source_revision,
            expected_deployment_spec_digest=report.deployment_spec_digest,
            expected_compose_file_sha256=report.compose_file_sha256,
            expected_compose_contract_sha256=report.compose_contract_sha256,
            expected_runtime_contract_sha256=report.runtime_contract_sha256,
            expected_output_sha256=report.output_observation.sha256,
        )
        if (
            read_back != raw
            or parsed != report
            or directory.name != report.qualification_id
        ):
            raise QualificationPublicationError(
                "qualification report read-back failed",
                code="REPORT_PUBLICATION_FAILED",
            )
    except QualificationPublicationError:
        raise
    except BaseException:
        raise QualificationPublicationError(
            "qualification report publication failed", code="REPORT_PUBLICATION_FAILED"
        ) from None
    return PublishedQualificationReport(
        path=directory,
        report=parsed,
        report_sha256=sha256_digest(raw),
    )


def _build_report(
    *,
    source_revision: str,
    project: str,
    deployment_digest: Sha256Digest,
    compose_file_digest: Sha256Digest,
    compose_contract_digest: Sha256Digest,
    runtime_contract_digest: Sha256Digest,
    image_observations: tuple[QualificationImageObservation, ...],
    output_observation: QualificationOutputObservation,
    cleanup: _CleanupObservation,
) -> QualificationReport:
    if (
        not _COMMIT_PATTERN.fullmatch(source_revision)
        or not cleanup.confirmed
        or cleanup.residual is None
    ):
        raise QualificationError(
            "qualification observations are incomplete", code="OBSERVATION_INVALID"
        )
    residual = cleanup.residual
    if (residual.containers, residual.networks, residual.volumes) != (0, 0, 0):
        raise QualificationError(
            "qualification residuals are not empty", code="RESIDUAL_RESOURCES"
        )
    payload = QualificationPayload(
        schema_version=QUALIFICATION_SCHEMA_VERSION,
        schema_id=QUALIFICATION_SCHEMA_ID,
        qualification_kind="local_compose_mock",
        observation_status="COMPLETE",
        qualification_mode="SYNTHETIC_ONLY",
        synthetic_only=True,
        evidence_eligible=False,
        provider_execution="NOT_PERFORMED",
        gpu_execution="NOT_PERFORMED",
        deployment_receipt_issued=False,
        evidence_published=False,
        source_repository=SOURCE_REPOSITORY_URL,
        source_revision=source_revision,
        source_worktree_clean=True,
        inferdrome_version=__version__,
        deployment_spec_digest=deployment_digest,
        compose_file_sha256=compose_file_digest,
        compose_contract_sha256=compose_contract_digest,
        runtime_contract_sha256=runtime_contract_digest,
        compose_project=project,
        endpoint=cast(
            Literal["http://mock-engine.internal:8000/v1/chat/completions"],
            _MOCK_ENDPOINT,
        ),
        image_observations=image_observations,
        output_observation=output_observation,
        cleanup=QualificationCleanup(
            action=QUALIFICATION_CLEANUP_ACTION,
            scope_label=QUALIFICATION_SCOPE_LABEL,
            project_name=project,
            attempts=cleanup.attempts,
            exit_code=cleanup.exit_code,
            residual_container_count=0,
            residual_network_count=0,
            residual_volume_count=0,
            cleanup_confirmed=True,
        ),
    )
    return QualificationReport(
        **payload.model_dump(),
        qualification_id=qualification_id(payload),
    )


def _check_cancellation(cancellation: CancellationToken | None) -> None:
    if cancellation is not None:
        cancellation.raise_if_requested()


def qualify_compose_mock(
    *,
    output_root: Path,
    runner: ProcessRunner | None = None,
    project_name: str | None = None,
    uid: int | None = None,
    gid: int | None = None,
    expected_output_sha256: str | None = None,
    cancellation: CancellationToken | None = None,
    source_revision: str | None = None,
    compose_file: Path | None = None,
) -> QualificationExecution:
    """Run the guarded mock workflow and publish only a complete report.

    ``compose_file`` and ``source_revision`` are compatibility inputs for
    injected tests; both are checked against the repository's actual observed
    values. They cannot redirect the command to another Compose file or make
    an arbitrary revision appear observed.
    """

    selected_root = _existing_directory(output_root, "qualification output root")
    if (
        compose_file is not None
        and _absolute_path(compose_file, "Compose file") != COMPOSE_FILE
    ):
        raise QualificationError(
            "qualification Compose file is not approved", code="PATH_INVALID"
        )
    (
        spec,
        deployment_digest,
        compose_file_digest,
        compose_contract_digest,
        runtime_contract_digest,
    ) = _load_inputs()
    process_runner = runner or BoundedSubprocessRunner()
    selected_uid = os.getuid() if uid is None else uid
    selected_gid = os.getgid() if gid is None else gid
    project = _validate_project(project_name or _new_project())
    work_parent = _existing_directory(
        selected_root.parent, "qualification temporary root"
    )
    try:
        work = Path(
            tempfile.mkdtemp(prefix=".inferdrome-qualification-", dir=work_parent)
        )
    except OSError:
        raise QualificationError(
            "qualification temporary directory could not be created",
            code="PATH_INVALID",
        ) from None
    if not work.is_absolute() or work.is_symlink():
        with contextlib.suppress(OSError):
            shutil.rmtree(work)
        raise QualificationError(
            "qualification temporary directory is unsafe", code="PATH_INVALID"
        )

    workflow_started = False
    primary_failure: QualificationError | None = None
    cleanup_observation: _CleanupObservation | None = None
    host_cleanup_failure: QualificationError | None = None
    output_observation: QualificationOutputObservation | None = None
    image_observations: tuple[QualificationImageObservation, ...] = ()
    env: Mapping[str, str] = {}
    try:
        evidence = work / "evidence"
        evidence.mkdir(mode=0o700)
        env = _safe_environment(
            uid=selected_uid, gid=selected_gid, evidence_dir=evidence
        )
        observed_revision = _observe_source_revision(
            process_runner,
            repository_root=REPOSITORY_ROOT,
            env=env,
        )
        if source_revision is not None and source_revision != observed_revision:
            raise QualificationError(
                "source revision binding failed", code="OBSERVATION_INVALID"
            )
        _check_cancellation(cancellation)
        _validate_local_docker_context(process_runner, env=env)
        version = _result(
            process_runner, ["docker", "compose", "version", "--short"], env, 30.0
        )
        if version.returncode != 0:
            raise QualificationError(
                "Docker Compose v2 is unavailable", code="DOCKER_UNAVAILABLE"
            )
        preexisting = _inspect_residuals(process_runner, project=project, env=env)
        if (preexisting.containers, preexisting.networks, preexisting.volumes) != (
            0,
            0,
            0,
        ):
            raise QualificationError(
                "generated Compose project name is not unique",
                code="PROJECT_NOT_UNIQUE",
            )
        config = _result(
            process_runner,
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(COMPOSE_FILE),
                "config",
                "--quiet",
            ],
            env,
            60.0,
        )
        if config.returncode != 0:
            raise QualificationError(
                "accepted Compose file failed configuration", code="CONTRACT_INVALID"
            )
        _check_cancellation(cancellation)
        workflow_started = True
        up = _result(
            process_runner,
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(COMPOSE_FILE),
                "up",
                "--build",
                "--no-color",
                "--abort-on-container-exit",
                "--exit-code-from",
                "synthetic-smoke",
                "synthetic-smoke",
            ],
            env,
            QUALIFICATION_TIMEOUT_SECONDS,
        )
        if up.returncode != 0:
            raise QualificationError(
                "Compose mock workflow failed", code="COMPOSE_RUN_FAILED"
            )
        _check_cancellation(cancellation)
        output_raw = _regular_file(
            work / "evidence" / "runner-output.json",
            "synthetic Compose output",
            QUALIFICATION_MAX_OUTPUT_BYTES,
        )
        output_observation = _parse_output(output_raw)
        if expected_output_sha256 is not None:
            if _DIGEST_PATTERN.fullmatch(expected_output_sha256) is None:
                raise QualificationError(
                    "expected output identity is invalid", code="OUTPUT_INVALID"
                )
            if output_observation.sha256 != expected_output_sha256:
                raise QualificationError(
                    "synthetic output identity did not match", code="OUTPUT_MISMATCH"
                )
        image_observations = _observe_images(process_runner, env=env)
    except CancellationRequested:
        primary_failure = QualificationError(
            "qualification was cancelled", code="CANCELLED"
        )
    except KeyboardInterrupt:
        primary_failure = QualificationError(
            "qualification was interrupted", code="INTERRUPTED"
        )
    except QualificationError as error:
        primary_failure = error
    except BaseException:
        primary_failure = QualificationError(
            "qualification failed at a bounded boundary", code="BASE_EXCEPTION"
        )
    finally:
        if workflow_started:
            cleanup_observation = _cleanup_compose(
                process_runner,
                compose_file=COMPOSE_FILE,
                project=project,
                env=env,
                attempts_allowed=spec.cleanup_policy.max_cleanup_attempts,
            )
        try:
            shutil.rmtree(work)
            if work.exists() or work.is_symlink():
                raise OSError
        except BaseException:
            host_cleanup_failure = QualificationError(
                "qualification temporary directory cleanup was not confirmed",
                code="CLEANUP_FAILED",
            )

    if cleanup_observation is not None and not cleanup_observation.confirmed:
        raise QualificationError(
            "Compose cleanup or scoped residual confirmation failed",
            code=cleanup_observation.failure_code or "CLEANUP_FAILED",
        )
    if host_cleanup_failure is not None:
        raise host_cleanup_failure
    if primary_failure is not None:
        raise primary_failure
    if cleanup_observation is None or output_observation is None:
        raise QualificationError(
            "qualification observations are incomplete", code="OBSERVATION_INVALID"
        )
    report = _build_report(
        source_revision=observed_revision,
        project=project,
        deployment_digest=deployment_digest,
        compose_file_digest=compose_file_digest,
        compose_contract_digest=compose_contract_digest,
        runtime_contract_digest=runtime_contract_digest,
        image_observations=image_observations,
        output_observation=output_observation,
        cleanup=cleanup_observation,
    )
    published = _publish_report(selected_root, report)
    return QualificationExecution(
        report=published.report,
        report_path=published.path,
        report_sha256=published.report_sha256,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inferdrome-deployment-qualification",
        description="Run the guarded local synthetic Docker Compose qualification",
    )
    parser.add_argument("--confirm-synthetic-compose", action="store_true")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--project")
    parser.add_argument("--expect-output-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.confirm_synthetic_compose:
        print(
            "deployment qualification requires --confirm-synthetic-compose",
            file=sys.stderr,
        )
        return 2
    try:
        execution = qualify_compose_mock(
            output_root=arguments.output_root,
            project_name=arguments.project,
            expected_output_sha256=arguments.expect_output_sha256,
        )
    except QualificationError as error:
        exit_code = (
            70
            if error.code
            in {
                "CLEANUP_FAILED",
                "RESIDUAL_RESOURCES",
                "RESIDUAL_INSPECTION_FAILED",
                "CLEANUP_UNCONFIRMED",
            }
            else (130 if error.code == "INTERRUPTED" else 2)
        )
        print(f"deployment qualification failed: {error.code}", file=sys.stderr)
        return exit_code
    except (OSError, ValueError):
        print(
            "deployment qualification failed: bounded preflight failure",
            file=sys.stderr,
        )
        return 2
    print("deployment qualification: COMPLETE (SYNTHETIC_ONLY; no acceptance verdict)")
    print(f"qualification report: {execution.report_path}")
    print(f"qualification report sha256: {execution.report_sha256}")
    return 0


__all__ = [
    "COMPOSE_CONTRACT_FILE",
    "COMPOSE_FILE",
    "DEPLOYMENT_SPEC_FILE",
    "QUALIFICATION_CLEANUP_ACTION",
    "QUALIFICATION_FILENAME",
    "QUALIFICATION_SCHEMA_ID",
    "QUALIFICATION_SCHEMA_VERSION",
    "BoundedSubprocessRunner",
    "ProcessResult",
    "ProcessRunner",
    "PublishedQualificationReport",
    "QualificationCleanup",
    "QualificationError",
    "QualificationExecution",
    "QualificationImageObservation",
    "QualificationModel",
    "QualificationOutputObservation",
    "QualificationPayload",
    "QualificationPublicationError",
    "QualificationReport",
    "canonical_qualification_bytes",
    "canonical_qualification_payload_bytes",
    "expected_compose_output_bytes",
    "main",
    "parse_qualification_json",
    "qualification_id",
    "qualification_payload_json_value",
    "qualification_schema",
    "qualification_sha256",
    "qualify_compose_mock",
    "verify_qualification_report_bytes",
]


if __name__ == "__main__":
    raise SystemExit(main())
