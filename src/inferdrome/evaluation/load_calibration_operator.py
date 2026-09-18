"""Host-local operator boundary for the bounded load-calibration rehearsal.

This module is not a provider client or deployment platform.  It validates a
two-engine host packet before it can construct the existing owned lifecycle.
External rental/guardian termination remains an external obligation.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import secrets
import stat
import tarfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic_ns
from typing import Annotated, Literal, cast

from pydantic import Field, ValidationError, model_validator

from inferdrome.deployment.gcp_securefs import SafeDirFD, SafeDirFSError
from inferdrome.deployment.manual_host_docker import (
    DOCKER_CONFIG_FILE,
    EMPTY_DOCKER_CONFIG,
    docker_argv,
    docker_environment,
)
from inferdrome.evaluation.contracts import ClosedModel, EndpointId, EvaluationError
from inferdrome.evaluation.files import OutputFile, read_input
from inferdrome.evaluation.load_calibration import LoadCalibrationProtocol
from inferdrome.evaluation.load_calibration_rehearsal import (
    AsyncioLocalSubprocessRunner,
    CandidateStudyRecipe,
    CompiledRehearsal,
    LocalSubprocessRunner,
    RehearsalResult,
    RehearsalSessionWindow,
    SubprocessResult,
    TwoEngineVllmSubprocessLifecycle,
    compile_rehearsal,
    run_rehearsal,
)
from inferdrome.evaluation.sglang_lifecycle import TwoEngineSGLangSubprocessLifecycle
from inferdrome.evaluation.sglang_profile import (
    SGLANG_IMAGE_REFERENCE,
    SglangServingConfig,
)
from inferdrome.evaluation.sglang_rehearsal import bind_sglang_rehearsal
from inferdrome.evaluation.study import execute_trial
from inferdrome.evaluation.study_config import SafeId, StudyConfig
from inferdrome.external_router.contracts import Digest, OpaqueId
from inferdrome.parsing import bounded_json_float, validate_json_structure
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.vllm_compose import VLLM_RUNTIME_IMAGE_REFERENCE

_UTC_TIMESTAMP = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
_OWNERSHIP_ID = r"^[a-z][a-z0-9-]{2,23}$"
_ENDPOINT_IDS: tuple[EndpointId, EndpointId] = ("endpoint-a", "endpoint-b")
_STUDY_FILE = re.compile(
    r"(?:plan\.json|engine-binding\.json|manifest\.json|trial-[0-9]{4}\.json)"
)
_REPORT_FILE = re.compile(r"report\.(?:json|md)")
_PHASE_ROOT_FILE = re.compile(
    r"(?:calibration|confirmation)-[a-z][a-z0-9_-]{0,31}-"
    r"(?:lifecycle|sglang-reset-[0-9]{4})\.json"
)
_STUDY_DIRECTORY = re.compile(r"(?:calibration|confirmation)-[a-z][a-z0-9_-]{0,31}")
_REPORT_DIRECTORY = re.compile(
    r"(?:calibration|confirmation)-[a-z][a-z0-9_-]{0,31}-report"
)
_ROOT_FILE_NAMES = frozenset(
    {
        "candidate-recipe-bindings.json",
        "engine-choice-bindings.json",
        "calibration-plan.json",
        "calibration-selection.json",
        "calibration-selection-binding.json",
        "confirmation-plan.json",
        "calibration-linkage.json",
        "operator-preflight.json",
        "operator-session.json",
        "operator-attempt-plan.json",
        "operator-docker-receipts.json",
        "operator-outcome.json",
    }
)
_MAX_EXPORT_FILE_BYTES = 64 * 1024 * 1024
_MAX_EXPORT_BYTES = 1024 * 1024 * 1024
_MAX_EXPORT_FILES = 576

RuntimeName = Literal["vllm", "sglang"]
UtcTimestamp = Annotated[str, Field(pattern=_UTC_TIMESTAMP)]


class OperatorRecipe(ClosedModel):
    """One complete pair of candidate study declarations, retained locally only."""

    schema_version: Literal["inferdrome.load-calibration-operator-recipe.v1"]
    level_id: SafeId
    calibration_config: StudyConfig
    confirmation_config: StudyConfig


class ExternalGuardianHandoff(ClosedModel):
    """A digest-bound external termination obligation, not a provider receipt."""

    provider: Literal["VAST_MANUAL_HOST"]
    instance_alias: OpaqueId
    instance_identity_sha256: Digest
    guardian_alias: OpaqueId
    guardian_handoff_sha256: Digest
    termination_deadline_utc: UtcTimestamp
    state: Literal["EXTERNALLY_ARMED_NOT_VERIFIED"]


class HostLocalRehearsalAuthorization(ClosedModel):
    """Exact external authorization facts required before local runtime work."""

    schema_version: Literal["inferdrome.load-calibration-host-authorization.v1"]
    confirmation: Literal["AUTHORIZE_VAST_TWO_A100_LOAD_CALIBRATION_V1"]
    authorization_id: OpaqueId
    approval_record_id: OpaqueId
    approved_at_utc: UtcTimestamp
    execute_not_after_utc: UtcTimestamp
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    runtime: RuntimeName
    serving_image_reference: Annotated[str, Field(min_length=1, max_length=256)]
    model_id: Literal["Qwen/Qwen3-8B"]
    model_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    model_snapshot_sha256: Digest
    protocol_sha256: Digest
    recipe_sha256s: Annotated[tuple[Digest, ...], Field(min_length=2, max_length=8)]
    model_snapshot_path_sha256: Digest
    docker_config_directory_sha256: Digest
    output_root_sha256: Digest
    ownership_id: Annotated[str, Field(pattern=_OWNERSHIP_ID)]
    maximum_runtime_seconds: Annotated[int, Field(ge=60, le=86_400)]
    termination_safety_margin_seconds: Annotated[int, Field(ge=30, le=3_600)]
    external_guardian: ExternalGuardianHandoff

    @model_validator(mode="after")
    def exact_declared_bounds(self) -> HostLocalRehearsalAuthorization:
        approved = _parse_utc(self.approved_at_utc)
        execute = _parse_utc(self.execute_not_after_utc)
        termination = _parse_utc(self.external_guardian.termination_deadline_utc)
        expected_image = (
            VLLM_RUNTIME_IMAGE_REFERENCE
            if self.runtime == "vllm"
            else SGLANG_IMAGE_REFERENCE
        )
        if (
            not approved < execute < termination
            or self.serving_image_reference != expected_image
            or self.model_revision != QWEN3_8B_REVISION
            or self.model_snapshot_sha256 != qwen3_expected_snapshot_sha256()
            or len(set(self.recipe_sha256s)) != len(self.recipe_sha256s)
        ):
            raise ValueError("host-local authorization bindings are invalid")
        return self


class CandidatePreflight(ClosedModel):
    level_id: SafeId
    calibration_recipe_sha256: Digest
    confirmation_recipe_sha256: Digest


class HostLocalRehearsalPreflight(ClosedModel):
    """Non-executing compilation output for one exact host-local session."""

    schema_version: Literal["inferdrome.load-calibration-host-preflight.v1"]
    runtime: RuntimeName
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    protocol_sha256: Digest
    candidate_recipes: Annotated[
        tuple[CandidatePreflight, ...], Field(min_length=2, max_length=8)
    ]
    execution_worst_case_duration_ns: Annotated[int, Field(ge=1)]
    final_cleanup_reserve_ns: Annotated[int, Field(ge=1)]
    retrieval_reserve_ns: Annotated[int, Field(ge=1)]
    worst_case_duration_ns: Annotated[int, Field(ge=1)]
    reserved_output_bytes: Annotated[int, Field(ge=1)]
    runtime_identity: Literal["DECLARED_NOT_OBSERVED"] = "DECLARED_NOT_OBSERVED"
    evidence_eligible: Literal[False] = False
    provider_action_performed: Literal[False] = False


@dataclass(frozen=True)
class PreparedHostLocalRehearsal:
    """Validated local inputs retained in memory until explicit execution."""

    rehearsal: CompiledRehearsal
    runtime: RuntimeName
    preflight: HostLocalRehearsalPreflight
    recipe_sha256s: tuple[str, ...]
    sglang_profiles: Mapping[EndpointId, SglangServingConfig] | None


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise EvaluationError("host-local timestamp is invalid") from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise EvaluationError("host-local timestamp is invalid")
    return parsed


def _strict_model[ModelT: ClosedModel](content: bytes, model: type[ModelT]) -> ModelT:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in items:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    try:
        text = content.decode("utf-8")
        validate_json_structure(text)
        json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError),
            parse_float=bounded_json_float,
        )
        return model.model_validate_json(content)
    except (UnicodeError, ValidationError, ValueError, RecursionError):
        raise EvaluationError(
            "host-local operator input violates its contract"
        ) from None


def _recipe(path: Path) -> tuple[OperatorRecipe, str]:
    content = read_input(path)
    value = _strict_model(content, OperatorRecipe)
    return value, sha256_digest(canonical_json_bytes(value.model_dump(mode="json")))


def _profile(path: Path) -> SglangServingConfig:
    return _strict_model(read_input(path), SglangServingConfig)


def _profiles(paths: Sequence[str]) -> Mapping[EndpointId, SglangServingConfig]:
    values: dict[EndpointId, SglangServingConfig] = {}
    for item in paths:
        endpoint, separator, value = item.partition("=")
        if (
            not separator
            or endpoint not in {"endpoint-a", "endpoint-b"}
            or endpoint in values
            or not value
        ):
            raise EvaluationError("SGLang profile mapping is invalid")
        values[cast(EndpointId, endpoint)] = _profile(Path(value))
    if set(values) != {"endpoint-a", "endpoint-b"}:
        raise EvaluationError("SGLang profile mapping is incomplete")
    return values


def prepare_host_local_rehearsal(
    *,
    protocol_path: Path,
    recipe_paths: Sequence[Path],
    runtime: RuntimeName,
    sglang_profile_paths: Sequence[str] = (),
) -> PreparedHostLocalRehearsal:
    """Compile every candidate with no process, transport, or provider action."""

    if runtime not in {"vllm", "sglang"} or not 2 <= len(recipe_paths) <= 8:
        raise EvaluationError("host-local rehearsal arguments are invalid")
    protocol = _strict_model(read_input(protocol_path), LoadCalibrationProtocol)
    loaded = tuple(_recipe(path) for path in recipe_paths)
    if len({item.level_id for item, _ in loaded}) != len(loaded):
        raise EvaluationError("host-local recipe identities are not unique")
    try:
        rehearsal = compile_rehearsal(
            protocol,
            tuple(
                CandidateStudyRecipe(
                    item.level_id, item.calibration_config, item.confirmation_config
                )
                for item, _ in loaded
            ),
        )
    except (TypeError, ValueError) as error:
        raise EvaluationError("host-local rehearsal preflight failed") from error
    profiles = _profiles(sglang_profile_paths) if runtime == "sglang" else None
    if runtime == "vllm" and sglang_profile_paths:
        raise EvaluationError("vLLM rehearsal does not accept SGLang profiles")
    if profiles is not None:
        try:
            bind_sglang_rehearsal(rehearsal, profiles, containerized=True)
        except (TypeError, ValueError) as error:
            raise EvaluationError("SGLang rehearsal preflight failed") from error
    candidates = tuple(
        CandidatePreflight(
            level_id=item.level.level_id,
            calibration_recipe_sha256=item.calibration_recipe_sha256,
            confirmation_recipe_sha256=item.confirmation_recipe_sha256,
        )
        for item in rehearsal.candidates
    )
    preflight = HostLocalRehearsalPreflight(
        schema_version="inferdrome.load-calibration-host-preflight.v1",
        runtime=runtime,
        source_commit=protocol.source_commit,
        protocol_sha256=rehearsal.calibration_plan.protocol_sha256,
        candidate_recipes=candidates,
        execution_worst_case_duration_ns=rehearsal.execution_worst_case_duration_ns,
        final_cleanup_reserve_ns=rehearsal.final_cleanup_reserve_ns,
        retrieval_reserve_ns=rehearsal.retrieval_reserve_ns,
        worst_case_duration_ns=rehearsal.worst_case_duration_ns,
        reserved_output_bytes=rehearsal.reserved_output_bytes,
    )
    return PreparedHostLocalRehearsal(
        rehearsal=rehearsal,
        runtime=runtime,
        preflight=preflight,
        recipe_sha256s=tuple(item[1] for item in loaded),
        sglang_profiles=profiles,
    )


def preflight_bytes(value: HostLocalRehearsalPreflight) -> bytes:
    return canonical_json_bytes(value.model_dump(mode="json")) + b"\n"


def write_preflight(path: Path, value: HostLocalRehearsalPreflight) -> None:
    with OutputFile(path) as destination:
        destination.write(preflight_bytes(value))


def _path_sha256(path: Path) -> str:
    return sha256_digest(os.fsencode(path.absolute()))


def _private_empty_docker_config(path: Path) -> None:
    try:
        root = SafeDirFD.open(path.absolute())
    except (OSError, SafeDirFSError):
        raise EvaluationError("host-local Docker configuration is unsafe") from None
    try:
        if os.listdir(root.fd) != [DOCKER_CONFIG_FILE]:
            raise EvaluationError("host-local Docker configuration is invalid")
        descriptor = root.open_child(DOCKER_CONFIG_FILE, os.O_RDONLY | os.O_NONBLOCK)
        try:
            metadata = root.validated_regular_child(
                DOCKER_CONFIG_FILE, descriptor=descriptor
            )
            if metadata.st_size != len(EMPTY_DOCKER_CONFIG):
                raise EvaluationError("host-local Docker configuration is invalid")
            content = os.read(descriptor, len(EMPTY_DOCKER_CONFIG) + 1)
            if content != EMPTY_DOCKER_CONFIG:
                raise EvaluationError("host-local Docker configuration is invalid")
        finally:
            os.close(descriptor)
    finally:
        root.close()


def _empty_output_root(path: Path) -> None:
    try:
        root = SafeDirFD.open(path.absolute())
    except (OSError, SafeDirFSError):
        raise EvaluationError("host-local output root is unsafe") from None
    try:
        if os.listdir(root.fd):
            raise EvaluationError("host-local output root must be empty")
    finally:
        root.close()


def _authorized(
    authorization: HostLocalRehearsalAuthorization,
    prepared: PreparedHostLocalRehearsal,
    *,
    model_snapshot_path: Path,
    docker_config_directory: Path,
    output_root: Path,
    now: datetime,
) -> None:
    protocol = prepared.rehearsal.calibration_plan.protocol
    if now.tzinfo is None:
        raise EvaluationError("host-local authorization clock is invalid")
    execute_not_after = _parse_utc(authorization.execute_not_after_utc)
    termination = _parse_utc(authorization.external_guardian.termination_deadline_utc)
    required_seconds = (
        prepared.rehearsal.worst_case_duration_ns // 1_000_000_000
        + authorization.termination_safety_margin_seconds
    )
    if (
        authorization.runtime != prepared.runtime
        or authorization.source_commit != protocol.source_commit
        or authorization.protocol_sha256
        != prepared.rehearsal.calibration_plan.protocol_sha256
        or authorization.recipe_sha256s != prepared.recipe_sha256s
        or authorization.maximum_runtime_seconds
        < (prepared.rehearsal.worst_case_duration_ns + 999_999_999) // 1_000_000_000
        or authorization.model_id != QWEN3_8B_MODEL_ID
        or authorization.model_snapshot_path_sha256 != _path_sha256(model_snapshot_path)
        or authorization.docker_config_directory_sha256
        != _path_sha256(docker_config_directory)
        or authorization.output_root_sha256 != _path_sha256(output_root)
        or now > execute_not_after
        or now.timestamp() + required_seconds >= execute_not_after.timestamp()
        or now.timestamp() + required_seconds >= termination.timestamp()
    ):
        raise EvaluationError("host-local authorization is unavailable or expired")
    if prepared.runtime == "sglang":
        profiles = prepared.sglang_profiles
        if (
            profiles is None
            or set(profiles) != set(_ENDPOINT_IDS)
            or tuple(profiles[endpoint].origin for endpoint in _ENDPOINT_IDS)
            != _origins(prepared)
            or any(
                profile.served_model_name != QWEN3_8B_MODEL_ID
                or profile.model_revision != QWEN3_8B_REVISION
                or profile.tokenizer_revision != QWEN3_8B_REVISION
                or profile.model_snapshot_sha256 != authorization.model_snapshot_sha256
                for profile in profiles.values()
            )
        ):
            raise EvaluationError("SGLang host-local authorization is unbound")
    _private_empty_docker_config(docker_config_directory)
    _empty_output_root(output_root)


@dataclass(frozen=True)
class DockerCommandReceipt:
    logical_argv_sha256: str
    constrained_argv_sha256: str
    state: Literal["RETURNED", "RUNNER_ERROR"]
    returncode: int | None


class PinnedLocalDockerRunner:
    """Inject explicit local Docker targeting without changing default lifecycle use."""

    def __init__(
        self,
        docker_config_directory: Path,
        *,
        runner: LocalSubprocessRunner | None = None,
    ) -> None:
        _private_empty_docker_config(docker_config_directory)
        self._directory = docker_config_directory.absolute()
        self._runner = runner or AsyncioLocalSubprocessRunner(
            environment=docker_environment(str(self._directory))
        )
        self._receipts: list[DockerCommandReceipt] = []

    @property
    def receipts(self) -> tuple[DockerCommandReceipt, ...]:
        return tuple(self._receipts)

    async def run(self, argv: tuple[str, ...], *, timeout_ns: int) -> SubprocessResult:
        if not argv:
            raise EvaluationError("host-local command is invalid")
        actual = argv
        if argv[0] == "docker":
            actual = tuple(docker_argv(str(self._directory), *argv[1:]))
        try:
            result = await self._runner.run(actual, timeout_ns=timeout_ns)
        except BaseException:
            if argv[0] == "docker":
                self._receipts.append(
                    DockerCommandReceipt(
                        logical_argv_sha256=sha256_digest(
                            canonical_json_bytes(list(argv))
                        ),
                        constrained_argv_sha256=sha256_digest(
                            canonical_json_bytes(list(actual))
                        ),
                        state="RUNNER_ERROR",
                        returncode=None,
                    )
                )
            raise
        if result.argv != actual:
            raise EvaluationError("host-local command identity changed")
        if argv[0] == "docker":
            self._receipts.append(
                DockerCommandReceipt(
                    logical_argv_sha256=sha256_digest(canonical_json_bytes(list(argv))),
                    constrained_argv_sha256=sha256_digest(
                        canonical_json_bytes(list(actual))
                    ),
                    state="RETURNED",
                    returncode=result.returncode,
                )
            )
        return replace(result, argv=argv)


def _session_bytes(
    authorization: HostLocalRehearsalAuthorization,
    prepared: PreparedHostLocalRehearsal,
    *,
    started_ns: int,
    hard_deadline_ns: int,
) -> bytes:
    return (
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.load-calibration-host-session.v1",
                "authorization_sha256": sha256_digest(
                    canonical_json_bytes(authorization.model_dump(mode="json"))
                ),
                "protocol_sha256": prepared.preflight.protocol_sha256,
                "runtime": prepared.runtime,
                "started_monotonic_ns": started_ns,
                "hard_deadline_monotonic_ns": hard_deadline_ns,
                "external_guardian_state": "EXTERNALLY_ARMED_NOT_VERIFIED",
                "provider_termination_verified": False,
                "evidence_eligible": False,
            }
        )
        + b"\n"
    )


def _outcome_bytes(*, state: Literal["COMPLETED", "FAILED"], reason: str) -> bytes:
    return (
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.load-calibration-host-outcome.v1",
                "state": state,
                "reason": reason,
                "provider_termination_verified": False,
                "evidence_eligible": False,
            }
        )
        + b"\n"
    )


def _command_receipt_bytes(receipts: Sequence[DockerCommandReceipt]) -> bytes:
    return (
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.load-calibration-host-docker.v1",
                "target": "EXPLICIT_LOCAL_UNIX_SOCKET",
                "commands": [
                    {
                        "logical_argv_sha256": receipt.logical_argv_sha256,
                        "constrained_argv_sha256": receipt.constrained_argv_sha256,
                        "state": receipt.state,
                        "returncode": receipt.returncode,
                    }
                    for receipt in receipts
                ],
                "evidence_eligible": False,
            }
        )
        + b"\n"
    )


def load_host_local_authorization(path: Path) -> HostLocalRehearsalAuthorization:
    return _strict_model(read_input(path), HostLocalRehearsalAuthorization)


def _origins(prepared: PreparedHostLocalRehearsal) -> tuple[str, str]:
    values = {
        tuple(
            endpoint.origin
            for endpoint in candidate.calibration_config.blocks[0].foreground.endpoints
        )
        for candidate in prepared.rehearsal.candidates
    }
    if len(values) != 1:
        raise EvaluationError("host-local candidate endpoint origins differ")
    result = values.pop()
    if len(result) != 2:
        raise EvaluationError("host-local candidate endpoint origins are invalid")
    return result[0], result[1]


def _attempt_plan_bytes(
    authorization: HostLocalRehearsalAuthorization,
    prepared: PreparedHostLocalRehearsal,
) -> bytes:
    attempts: list[dict[str, object]] = []
    for candidate in prepared.rehearsal.candidates:
        for phase, plan in (
            ("CALIBRATION", candidate.calibration_plan),
            ("CONFIRMATION", candidate.confirmation_plan),
        ):
            for trial in plan.trials:
                for endpoint_id in ("endpoint-a", "endpoint-b"):
                    binding = {
                        "ownership_id": authorization.ownership_id,
                        "runtime": prepared.runtime,
                        "phase": phase,
                        "source_trial_id": trial.trial_id,
                        "endpoint_id": endpoint_id,
                        "config_sha256": plan.config_sha256,
                        "plan_sha256": sha256_digest(
                            canonical_json_bytes(plan.to_dict())
                        ),
                    }
                    attempts.append(
                        {
                            **binding,
                            "attempt_identity_sha256": sha256_digest(
                                canonical_json_bytes(binding)
                            ),
                        }
                    )
    return (
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.load-calibration-host-attempt-plan.v1",
                "authorization_sha256": sha256_digest(
                    canonical_json_bytes(authorization.model_dump(mode="json"))
                ),
                "attempts": attempts,
                "evidence_eligible": False,
            }
        )
        + b"\n"
    )


def _write_root_sidecar(output_root: Path, name: str, content: bytes) -> None:
    with OutputFile(output_root / name) as destination:
        destination.write(content)


def _write_runtime_receipts(output_root: Path, runner: PinnedLocalDockerRunner) -> None:
    path = output_root / "operator-docker-receipts.json"
    if path.exists():
        return
    _write_root_sidecar(output_root, path.name, _command_receipt_bytes(runner.receipts))


def _write_outcome(
    output_root: Path, *, state: Literal["COMPLETED", "FAILED"], reason: str
) -> None:
    path = output_root / "operator-outcome.json"
    if path.exists():
        return
    _write_root_sidecar(
        output_root, path.name, _outcome_bytes(state=state, reason=reason)
    )


def _checked_utc(observed: object) -> datetime:
    if not isinstance(observed, datetime) or observed.tzinfo is None:
        raise EvaluationError("host-local authorization clock is invalid")
    return observed.astimezone(UTC)


def _duration_ns(start: datetime, end: datetime) -> int:
    value = int((end - start).total_seconds() * 1_000_000_000)
    if value < 1:
        raise EvaluationError("host-local session deadline is unavailable")
    return value


async def run_authorized_host_local_rehearsal(
    prepared: PreparedHostLocalRehearsal,
    *,
    authorization: HostLocalRehearsalAuthorization,
    model_snapshot_path: Path,
    docker_config_directory: Path,
    output_root: Path,
    stop: asyncio.Event | None = None,
    session_anchor_utc: datetime | None = None,
    session_started_ns: int | None = None,
    utc_clock: Callable[[], datetime] | None = None,
    monotonic_clock: Callable[[], int] = monotonic_ns,
    runner: LocalSubprocessRunner | None = None,
) -> RehearsalResult:
    """Run one explicit host-local session; this never creates or terminates a VM."""

    if stop is not None and type(stop) is not asyncio.Event:
        raise EvaluationError("host-local cancellation boundary is invalid")
    if not callable(monotonic_clock):
        raise EvaluationError("host-local session clock is invalid")
    stop = stop or asyncio.Event()
    # The paired wall/monotonic anchor precedes every path, approval, and
    # packet check.  Nothing that consumes preflight time may mint a later
    # full-duration execution window.
    if (session_anchor_utc is None) != (session_started_ns is None):
        raise EvaluationError("host-local session anchor is incomplete")
    if session_anchor_utc is None:
        observed_now = _checked_utc(
            datetime.now(UTC) if utc_clock is None else utc_clock()
        )
    else:
        observed_now = _checked_utc(session_anchor_utc)
    started_ns = monotonic_clock() if session_started_ns is None else session_started_ns
    if type(started_ns) is not int or started_ns < 0:
        raise EvaluationError("host-local session clock is invalid")
    termination = _parse_utc(authorization.external_guardian.termination_deadline_utc)
    execute_not_after = _parse_utc(authorization.execute_not_after_utc)
    maximum_ns = authorization.maximum_runtime_seconds * 1_000_000_000
    window_ns = min(
        prepared.rehearsal.calibration_plan.protocol.max_session_duration_ns,
        maximum_ns,
        _duration_ns(observed_now, execute_not_after),
        _duration_ns(observed_now, termination)
        - authorization.termination_safety_margin_seconds * 1_000_000_000,
    )
    if window_ns < 1:
        raise EvaluationError("host-local session deadline is unavailable")
    hard_deadline_ns = started_ns + window_ns
    _authorized(
        authorization,
        prepared,
        model_snapshot_path=model_snapshot_path,
        docker_config_directory=docker_config_directory,
        output_root=output_root,
        now=observed_now,
    )
    dispatch_now = _checked_utc(datetime.now(UTC) if utc_clock is None else utc_clock())
    dispatch_ns = monotonic_clock()
    if (
        dispatch_now < observed_now
        or dispatch_now > execute_not_after
        or dispatch_now >= termination
        or type(dispatch_ns) is not int
        or dispatch_ns < started_ns
        or dispatch_ns >= hard_deadline_ns
    ):
        raise EvaluationError("host-local session expired before dispatch")
    if stop.is_set():
        raise EvaluationError("host-local session was cancelled")
    session = RehearsalSessionWindow(started_ns, hard_deadline_ns)
    session.validated_for(
        prepared.rehearsal.calibration_plan.protocol, now_ns=dispatch_ns
    )
    _write_root_sidecar(
        output_root,
        "operator-session.json",
        _session_bytes(
            authorization,
            prepared,
            started_ns=started_ns,
            hard_deadline_ns=hard_deadline_ns,
        ),
    )
    _write_root_sidecar(
        output_root,
        "operator-attempt-plan.json",
        _attempt_plan_bytes(authorization, prepared),
    )
    pinned_runner = PinnedLocalDockerRunner(docker_config_directory, runner=runner)
    lifecycle: TwoEngineVllmSubprocessLifecycle | TwoEngineSGLangSubprocessLifecycle
    try:
        if prepared.runtime == "vllm":
            lifecycle = TwoEngineVllmSubprocessLifecycle(
                _origins(prepared),
                ownership_id=authorization.ownership_id,
                model_snapshot_path=model_snapshot_path,
                runner=pinned_runner,
            )
            result = await run_rehearsal(
                prepared.rehearsal,
                output_root,
                lifecycle=lifecycle,
                executor=execute_trial,
                session_window=session,
                stop=stop,
            )
        else:
            assert prepared.sglang_profiles is not None
            bindings = bind_sglang_rehearsal(
                prepared.rehearsal, prepared.sglang_profiles, containerized=True
            )
            lifecycle = TwoEngineSGLangSubprocessLifecycle(
                prepared.sglang_profiles,
                contexts=bindings.contexts,
                ownership_id=authorization.ownership_id,
                runner=pinned_runner,
            )
            result = await run_rehearsal(
                prepared.rehearsal,
                output_root,
                lifecycle=lifecycle,
                sglang_profiles=prepared.sglang_profiles,
                containerized=True,
                session_window=session,
                stop=stop,
            )
    except BaseException:
        _write_runtime_receipts(output_root, pinned_runner)
        _write_outcome(output_root, state="FAILED", reason="REHEARSAL_ABORTED")
        raise
    _write_runtime_receipts(output_root, pinned_runner)
    _write_outcome(output_root, state="COMPLETED", reason="REHEARSAL_FINISHED")
    return result


class ExportedArtifact(ClosedModel):
    path: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")]
    sha256: Digest
    bytes: Annotated[int, Field(ge=1, le=_MAX_EXPORT_FILE_BYTES)]


class HostLocalExportManifest(ClosedModel):
    """A digest inventory for bounded retrieval; it is not provider evidence."""

    schema_version: Literal["inferdrome.load-calibration-host-export.v1"]
    source_state: Literal["COMPLETE", "PARTIAL_OR_ABORTED"]
    artifacts: Annotated[
        tuple[ExportedArtifact, ...], Field(min_length=1, max_length=_MAX_EXPORT_FILES)
    ]
    total_bytes: Annotated[int, Field(ge=1, le=_MAX_EXPORT_BYTES)]
    evidence_eligible: Literal[False] = False
    provider_termination_verified: Literal[False] = False

    @model_validator(mode="after")
    def inventory_is_ordered_and_complete(self) -> HostLocalExportManifest:
        paths = tuple(item.path for item in self.artifacts)
        if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ValueError("host-local export inventory is invalid")
        if sum(item.bytes for item in self.artifacts) != self.total_bytes:
            raise ValueError("host-local export byte total is invalid")
        return self


@dataclass(frozen=True)
class ExportVerification:
    archive_sha256: str
    manifest_sha256: str
    source_state: Literal["COMPLETE", "PARTIAL_OR_ABORTED"]
    artifact_count: int
    total_bytes: int


def _nofollow() -> int:
    value = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(value, int) or value == 0:
        raise EvaluationError("secure export primitives are unavailable")
    return value


def _read_regular(root: SafeDirFD, name: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = root.open_child(name, os.O_RDONLY | os.O_NONBLOCK)
        before = root.validated_regular_child(name, descriptor=descriptor)
        if not 1 <= before.st_size <= _MAX_EXPORT_FILE_BYTES:
            raise EvaluationError("host-local export file exceeds its bound")
        content = bytearray()
        while len(content) < before.st_size:
            chunk = os.read(descriptor, min(65_536, before.st_size - len(content)))
            if not chunk:
                raise EvaluationError("host-local export file changed during read")
            content.extend(chunk)
        if os.read(descriptor, 1):
            raise EvaluationError("host-local export file changed during read")
        after = root.validated_regular_child(name, descriptor=descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise EvaluationError("host-local export file changed during read")
        return bytes(content)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_directory(parent: SafeDirFD, name: str) -> SafeDirFD:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | _nofollow(),
            dir_fd=parent.fd,
        )
        result = SafeDirFD.from_inherited_fd(descriptor)
        return result
    except (OSError, SafeDirFSError):
        raise EvaluationError("host-local export directory is unsafe") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _allowed_root_file(name: str) -> bool:
    return name in _ROOT_FILE_NAMES or _PHASE_ROOT_FILE.fullmatch(name) is not None


def _snapshot_output(root_path: Path) -> tuple[tuple[str, bytes], ...]:
    """Read only the finite rehearsal output grammar through held dir fds."""

    try:
        root = SafeDirFD.open(root_path.absolute())
    except (OSError, SafeDirFSError):
        raise EvaluationError("host-local export root is unsafe") from None
    try:
        values: list[tuple[str, bytes]] = []
        for name in sorted(os.listdir(root.fd)):
            try:
                metadata = root.stat_child(name)
            except OSError:
                raise EvaluationError("host-local export inventory changed") from None
            if stat.S_ISREG(metadata.st_mode):
                if not _allowed_root_file(name):
                    raise EvaluationError(
                        "host-local export contains an undeclared file"
                    )
                values.append((name, _read_regular(root, name)))
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise EvaluationError("host-local export contains an unsafe entry")
            report = _REPORT_DIRECTORY.fullmatch(name) is not None
            if not report and _STUDY_DIRECTORY.fullmatch(name) is None:
                raise EvaluationError(
                    "host-local export contains an undeclared directory"
                )
            child = _open_directory(root, name)
            try:
                allowed = _REPORT_FILE if report else _STUDY_FILE
                for child_name in sorted(os.listdir(child.fd)):
                    child_metadata = child.stat_child(child_name)
                    if (
                        not stat.S_ISREG(child_metadata.st_mode)
                        or allowed.fullmatch(child_name) is None
                    ):
                        raise EvaluationError("host-local export child is unsafe")
                    values.append(
                        (f"{name}/{child_name}", _read_regular(child, child_name))
                    )
            finally:
                child.close()
        if not values or len(values) > _MAX_EXPORT_FILES:
            raise EvaluationError("host-local export inventory exceeds its bound")
        if sum(len(content) for _, content in values) > _MAX_EXPORT_BYTES:
            raise EvaluationError("host-local export exceeds its byte bound")
        return tuple(sorted(values))
    finally:
        root.close()


def _source_state(
    values: Mapping[str, bytes],
) -> Literal["COMPLETE", "PARTIAL_OR_ABORTED"]:
    content = values.get("operator-outcome.json")
    if content is None:
        return "PARTIAL_OR_ABORTED"
    try:
        parsed = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "PARTIAL_OR_ABORTED"
    return "COMPLETE" if parsed.get("state") == "COMPLETED" else "PARTIAL_OR_ABORTED"


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    value = tarfile.TarInfo(name)
    value.size = size
    value.mode = 0o400
    value.uid = 0
    value.gid = 0
    value.uname = ""
    value.gname = ""
    value.mtime = 0
    return value


def _archive_sha256(path: Path) -> str:
    descriptor: int | None = None
    try:
        metadata = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 1
        ):
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | _nofollow())
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        while content := os.read(descriptor, 65_536):
            digest.update(content)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError
        return "sha256:" + digest.hexdigest()
    except (OSError, ValueError):
        raise EvaluationError("host-local export archive is unsafe") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def export_host_local_rehearsal(
    output_root: Path, archive_path: Path
) -> ExportVerification:
    """Create one no-replace regular-file allowlist archive for retrieval."""

    try:
        snapshots = _snapshot_output(output_root)
    except (OSError, SafeDirFSError):
        raise EvaluationError("host-local export source is unsafe") from None
    snapshot_map = dict(snapshots)
    artifacts = tuple(
        ExportedArtifact(path=name, sha256=sha256_digest(content), bytes=len(content))
        for name, content in snapshots
    )
    manifest = HostLocalExportManifest(
        schema_version="inferdrome.load-calibration-host-export.v1",
        source_state=_source_state(snapshot_map),
        artifacts=artifacts,
        total_bytes=sum(item.bytes for item in artifacts),
    )
    manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    target = archive_path.absolute()
    temporary: str | None = None
    root: SafeDirFD | None = None
    descriptor: int | None = None
    try:
        root = SafeDirFD.open(target.parent)
        temporary = f".{target.name}.partial-{secrets.token_hex(12)}"
        descriptor = root.open_child(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "wb", closefd=True) as destination:
            descriptor = None
            with tarfile.open(fileobj=destination, mode="w:") as archive:
                for name, content in snapshots:
                    archive.addfile(
                        _tarinfo(f"rehearsal/{name}", len(content)), io.BytesIO(content)
                    )
                archive.addfile(
                    _tarinfo("export-manifest.json", len(manifest_bytes)),
                    io.BytesIO(manifest_bytes),
                )
            destination.flush()
            os.fsync(destination.fileno())
            os.fchmod(destination.fileno(), 0o400)
        root.validated_regular_child(temporary)
        os.link(
            temporary,
            target.name,
            src_dir_fd=root.fd,
            dst_dir_fd=root.fd,
            follow_symlinks=False,
        )
        # After linking the temporary object has two names, so SafeDirFD's
        # one-link regular-file invariant intentionally no longer applies to
        # the source.  The held private directory and random temporary name
        # bind this direct unlink to the just-published object.
        os.unlink(temporary, dir_fd=root.fd)
        temporary = None
        root.validated_regular_child(target.name)
        root.fsync()
    except (OSError, SafeDirFSError):
        raise EvaluationError("host-local export could not be published") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if root is not None:
            if temporary is not None:
                with suppress(OSError):
                    os.unlink(temporary, dir_fd=root.fd)
            root.close()
    return verify_retrieved_host_local_export(target)


def verify_retrieved_host_local_export(
    archive_path: Path, *, expected_archive_sha256: str | None = None
) -> ExportVerification:
    """Verify retrieved archive bytes, pinned digest, path allowlist, and inventory.

    A retrieval caller supplies the producer-side digest printed by the export
    command.  The embedded inventory alone proves only internal consistency;
    it cannot identify which otherwise-well-formed archive was expected.
    """

    descriptor: int | None = None
    handle: io.BufferedReader | None = None
    try:
        target = archive_path.absolute()
        metadata = os.lstat(target)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not 1 <= metadata.st_size <= _MAX_EXPORT_BYTES + 8 * 1024 * 1024
        ):
            raise ValueError
        descriptor = os.open(target, os.O_RDONLY | os.O_NONBLOCK | _nofollow())
        before = os.fstat(descriptor)
        handle = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = None
        with tarfile.open(fileobj=handle, mode="r:") as archive:
            members = archive.getmembers()
            if not 2 <= len(members) <= _MAX_EXPORT_FILES + 1:
                raise ValueError
            if members[-1].name != "export-manifest.json":
                raise ValueError
            actual: list[ExportedArtifact] = []
            manifest_bytes: bytes | None = None
            for member in members:
                if (
                    member.isdir()
                    or not member.isfile()
                    or member.mode & 0o222
                    or member.uid != 0
                    or member.gid != 0
                    or member.name.startswith("/")
                    or ".." in Path(member.name).parts
                    or member.size < 1
                    or member.size > _MAX_EXPORT_FILE_BYTES
                ):
                    raise ValueError
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError
                with source:
                    digest = hashlib.sha256()
                    remaining = member.size
                    chunks: list[bytes] = []
                    while remaining:
                        content = source.read(min(65_536, remaining))
                        if not content:
                            raise ValueError
                        digest.update(content)
                        if member.name == "export-manifest.json":
                            chunks.append(content)
                        remaining -= len(content)
                    if source.read(1):
                        raise ValueError
                if member.name == "export-manifest.json":
                    manifest_bytes = b"".join(chunks)
                    continue
                if not member.name.startswith("rehearsal/"):
                    raise ValueError
                relative = member.name.removeprefix("rehearsal/")
                if not relative or not _export_relative_path(relative):
                    raise ValueError
                actual.append(
                    ExportedArtifact(
                        path=relative,
                        sha256="sha256:" + digest.hexdigest(),
                        bytes=member.size,
                    )
                )
        if manifest_bytes is None:
            raise ValueError
        manifest = _strict_model(manifest_bytes, HostLocalExportManifest)
        if tuple(actual) != manifest.artifacts:
            raise ValueError
        after = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError
        archive_sha256 = _archive_sha256(target)
        if expected_archive_sha256 is not None and (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_archive_sha256)
            or archive_sha256 != expected_archive_sha256
        ):
            raise ValueError
        return ExportVerification(
            archive_sha256=archive_sha256,
            manifest_sha256=sha256_digest(manifest_bytes),
            source_state=manifest.source_state,
            artifact_count=len(manifest.artifacts),
            total_bytes=manifest.total_bytes,
        )
    except (OSError, ValueError, tarfile.TarError, ValidationError):
        raise EvaluationError("host-local retrieved export is invalid") from None
    finally:
        if handle is not None:
            handle.close()
        elif descriptor is not None:
            os.close(descriptor)


def _export_relative_path(value: str) -> bool:
    if _allowed_root_file(value):
        return True
    if "/" not in value:
        return False
    parent, name = value.split("/", 1)
    if "/" in name:
        return False
    return (
        _STUDY_DIRECTORY.fullmatch(parent) is not None
        and _STUDY_FILE.fullmatch(name) is not None
    ) or (
        _REPORT_DIRECTORY.fullmatch(parent) is not None
        and _REPORT_FILE.fullmatch(name) is not None
    )
