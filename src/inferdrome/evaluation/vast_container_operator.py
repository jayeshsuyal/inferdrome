"""Manual ordinary-container operator for the load-calibrated routing rehearsal.

This narrow operator is intentionally not a Vast client.  It validates one
already-authorized, already-rented container contract and starts two local,
owned engine process groups through :mod:`direct_process_lifecycle`.  Rental,
outer-image verification, data retrieval and host destruction remain external
operator obligations.  Offline tests exercise only injected CPU/fake seams.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic_ns
from typing import Annotated, Literal

from pydantic import Field, model_validator

from inferdrome.deployment.gcp_securefs import SafeDirFD, SafeDirFSError
from inferdrome.evaluation.contracts import ClosedModel, EndpointId, EvaluationError
from inferdrome.evaluation.direct_process_lifecycle import (
    REAL_GPU_STARTUP_TIMEOUT_NS,
    DirectProcessRunner,
    TwoEngineSglangDirectProcessLifecycle,
    TwoEngineVllmDirectProcessLifecycle,
    executable_identity_sha256,
    resolve_direct_runtime,
)
from inferdrome.evaluation.files import OutputFile, read_input
from inferdrome.evaluation.load_calibration_operator import (
    ExternalGuardianHandoff,
    PreparedHostLocalRehearsal,
    _checked_utc,
    _duration_ns,
    _origins,
    _parse_utc,
    _path_sha256,
    _strict_model,
    prepare_host_local_rehearsal,
)
from inferdrome.evaluation.load_calibration_rehearsal import (
    LocalSubprocessRunner,
    RehearsalResult,
    RehearsalSessionWindow,
    run_rehearsal,
)
from inferdrome.evaluation.sglang_profile import SGLANG_IMAGE_REFERENCE
from inferdrome.evaluation.sglang_rehearsal import bind_sglang_rehearsal
from inferdrome.evaluation.study import execute_trial
from inferdrome.evaluation.vast_startup_ready import (
    VastStartupReadyImageProfile,
    is_vast_startup_ready_image_reference,
)
from inferdrome.external_router.contracts import Digest, OpaqueId
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.vllm_compose import VLLM_RUNTIME_IMAGE_REFERENCE

_UTC_TIMESTAMP = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
_OWNERSHIP_ID = r"^[a-z][a-z0-9-]{2,23}$"
_ENDPOINTS: tuple[EndpointId, EndpointId] = ("endpoint-a", "endpoint-b")
_SESSION_SCHEMA = "inferdrome.load-calibration-vast-container-session.v1"
_ATTEMPT_PLAN_SCHEMA = "inferdrome.load-calibration-vast-container-attempt-plan.v1"
_PROCESS_RECEIPTS_SCHEMA = (
    "inferdrome.load-calibration-vast-container-process-receipts.v1"
)
_OUTCOME_SCHEMA = "inferdrome.load-calibration-vast-container-outcome.v1"
RuntimeName = Literal["vllm", "sglang"]
UtcTimestamp = Annotated[str, Field(pattern=_UTC_TIMESTAMP)]


class VastContainerRehearsalAuthorization(ClosedModel):
    """Exact manual contract before direct engines may be invoked.

    ``outer_image_state`` is intentionally declared/unverified: an ordinary
    host can expose a local executable but cannot prove offline that its outer
    container was started from this OCI reference.  The observed executable
    metadata is separately bound and rechecked immediately before spawning.
    """

    schema_version: Literal[
        "inferdrome.load-calibration-vast-container-authorization.v1",
        "inferdrome.load-calibration-vast-container-authorization.v2",
    ]
    confirmation: Literal["AUTHORIZE_VAST_CONTAINER_TWO_A100_LOAD_CALIBRATION_V1"]
    authorization_id: OpaqueId
    approval_record_id: OpaqueId
    approved_at_utc: UtcTimestamp
    execute_not_after_utc: UtcTimestamp
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    runtime: RuntimeName
    outer_image_reference: Annotated[str, Field(min_length=1, max_length=256)]
    outer_image_state: Literal["DECLARED_BY_OPERATOR_UNVERIFIED"]
    startup_ready_image: VastStartupReadyImageProfile | None = None
    runtime_executable_identity_sha256: Digest
    model_id: Literal["Qwen/Qwen3-8B"]
    model_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    model_snapshot_sha256: Digest
    protocol_sha256: Digest
    recipe_sha256s: Annotated[tuple[Digest, ...], Field(min_length=2, max_length=8)]
    model_snapshot_path_sha256: Digest
    output_root_sha256: Digest
    ownership_id: Annotated[str, Field(pattern=_OWNERSHIP_ID)]
    maximum_runtime_seconds: Annotated[int, Field(ge=60, le=86_400)]
    termination_safety_margin_seconds: Annotated[int, Field(ge=30, le=3_600)]
    external_guardian: ExternalGuardianHandoff

    @model_validator(mode="after")
    def exact_declared_bounds(self) -> VastContainerRehearsalAuthorization:
        approved = _parse_utc(self.approved_at_utc)
        execute = _parse_utc(self.execute_not_after_utc)
        termination = _parse_utc(self.external_guardian.termination_deadline_utc)
        startup = self.startup_ready_image
        startup_invalid = (
            self.schema_version
            == "inferdrome.load-calibration-vast-container-authorization.v2"
            and (
                self.runtime != "vllm"
                or startup is None
                or startup.source_commit != self.source_commit
                or startup.runtime != self.runtime
                or startup.model_id != self.model_id
                or startup.model_revision != self.model_revision
            )
        ) or (
            self.schema_version
            == "inferdrome.load-calibration-vast-container-authorization.v1"
            and startup is not None
        )
        if (
            not approved < execute < termination
            or (
                self.schema_version
                == "inferdrome.load-calibration-vast-container-authorization.v1"
                and self.outer_image_reference
                != (
                    VLLM_RUNTIME_IMAGE_REFERENCE
                    if self.runtime == "vllm"
                    else SGLANG_IMAGE_REFERENCE
                )
            )
            or (
                self.schema_version
                == "inferdrome.load-calibration-vast-container-authorization.v2"
                and (
                    startup is None
                    or self.outer_image_reference != startup.image_reference
                    or startup.base_image_reference != VLLM_RUNTIME_IMAGE_REFERENCE
                )
            )
            or self.model_revision != QWEN3_8B_REVISION
            or self.model_snapshot_sha256 != qwen3_expected_snapshot_sha256()
            or len(set(self.recipe_sha256s)) != len(self.recipe_sha256s)
            or startup_invalid
        ):
            raise ValueError("vast-container authorization bindings are invalid")
        return self


class VastContainerRuntimeIdentity(ClosedModel):
    """One observed local executable plus an honest unverified outer image claim."""

    outer_image_reference: Annotated[str, Field(min_length=1, max_length=256)]
    base_image_reference: Annotated[str, Field(min_length=1, max_length=256)]
    outer_image_state: Literal["DECLARED_BY_OPERATOR_UNVERIFIED"]
    executable_identity_sha256: Digest
    executable_observation: Literal["LOCAL_METADATA_OBSERVED"]
    runtime_verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    evidence_eligible: Literal[False] = False


class VastContainerRehearsalPreflight(ClosedModel):
    """Non-executing compilation and local-executable identity for one session."""

    schema_version: Literal["inferdrome.load-calibration-vast-container-preflight.v1"]
    runtime: RuntimeName
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    protocol_sha256: Digest
    recipe_sha256s: Annotated[tuple[Digest, ...], Field(min_length=2, max_length=8)]
    execution_worst_case_duration_ns: Annotated[int, Field(ge=1)]
    final_cleanup_reserve_ns: Annotated[int, Field(ge=1)]
    retrieval_reserve_ns: Annotated[int, Field(ge=1)]
    worst_case_duration_ns: Annotated[int, Field(ge=1)]
    reserved_output_bytes: Annotated[int, Field(ge=1)]
    runtime_identity: VastContainerRuntimeIdentity
    provider_action_performed: Literal[False] = False
    evidence_eligible: Literal[False] = False


class PreparedVastContainerRehearsal:
    """In-memory compiled packet; construction has no GPU/process/provider edge."""

    def __init__(
        self,
        prepared: PreparedHostLocalRehearsal,
        *,
        preflight: VastContainerRehearsalPreflight,
        executable: str,
    ) -> None:
        self.prepared = prepared
        self.preflight = preflight
        self.executable = executable


def _strict_authorization(content: bytes) -> VastContainerRehearsalAuthorization:
    try:
        return _strict_model(content, VastContainerRehearsalAuthorization)
    except EvaluationError:
        raise EvaluationError(
            "vast-container authorization input violates its contract"
        ) from None


def load_vast_container_authorization(
    path: Path,
) -> VastContainerRehearsalAuthorization:
    return _strict_authorization(read_input(path))


def prepare_vast_container_rehearsal(
    *,
    protocol_path: Path,
    recipe_paths: Sequence[Path],
    runtime: RuntimeName,
    outer_image_reference: str,
    runtime_executable: str,
    sglang_profile_paths: Sequence[str] = (),
) -> PreparedVastContainerRehearsal:
    """Compile and observe a local executable without starting any runtime."""

    prepared = prepare_host_local_rehearsal(
        protocol_path=protocol_path,
        recipe_paths=recipe_paths,
        runtime=runtime,
        sglang_profile_paths=sglang_profile_paths,
    )
    if runtime == "vllm":
        valid_image = is_vast_startup_ready_image_reference(outer_image_reference)
        base_image_reference = VLLM_RUNTIME_IMAGE_REFERENCE
    else:
        valid_image = outer_image_reference == SGLANG_IMAGE_REFERENCE
        base_image_reference = SGLANG_IMAGE_REFERENCE
    if not valid_image:
        raise EvaluationError("vast-container outer image is not the pinned runtime")
    if runtime == "sglang":
        assert prepared.sglang_profiles is not None
        try:
            bind_sglang_rehearsal(
                prepared.rehearsal, prepared.sglang_profiles, containerized=False
            )
        except (TypeError, ValueError) as error:
            raise EvaluationError("direct SGLang rehearsal preflight failed") from error
    executable = resolve_direct_runtime(runtime_executable)
    runtime_identity = VastContainerRuntimeIdentity(
        outer_image_reference=outer_image_reference,
        base_image_reference=base_image_reference,
        outer_image_state="DECLARED_BY_OPERATOR_UNVERIFIED",
        executable_identity_sha256=executable_identity_sha256(executable),
        executable_observation="LOCAL_METADATA_OBSERVED",
    )
    rehearsal = prepared.rehearsal
    return PreparedVastContainerRehearsal(
        prepared,
        executable=runtime_executable,
        preflight=VastContainerRehearsalPreflight(
            schema_version="inferdrome.load-calibration-vast-container-preflight.v1",
            runtime=runtime,
            source_commit=rehearsal.calibration_plan.protocol.source_commit,
            protocol_sha256=rehearsal.calibration_plan.protocol_sha256,
            recipe_sha256s=prepared.recipe_sha256s,
            execution_worst_case_duration_ns=rehearsal.execution_worst_case_duration_ns,
            final_cleanup_reserve_ns=rehearsal.final_cleanup_reserve_ns,
            retrieval_reserve_ns=rehearsal.retrieval_reserve_ns,
            worst_case_duration_ns=rehearsal.worst_case_duration_ns,
            reserved_output_bytes=rehearsal.reserved_output_bytes,
            runtime_identity=runtime_identity,
        ),
    )


def vast_container_preflight_bytes(value: VastContainerRehearsalPreflight) -> bytes:
    return canonical_json_bytes(value.model_dump(mode="json")) + b"\n"


def write_vast_container_preflight(
    path: Path, value: VastContainerRehearsalPreflight
) -> None:
    with OutputFile(path) as destination:
        destination.write(vast_container_preflight_bytes(value))


def _empty_output_root(path: Path) -> None:
    try:
        root = SafeDirFD.open(path.absolute())
    except (OSError, SafeDirFSError):
        raise EvaluationError("vast-container output root is unsafe") from None
    try:
        if os.listdir(root.fd):
            raise EvaluationError("vast-container output root must be empty")
    finally:
        root.close()


def _authorized(
    authorization: VastContainerRehearsalAuthorization,
    prepared: PreparedVastContainerRehearsal,
    *,
    model_snapshot_path: Path,
    output_root: Path,
    now: datetime,
) -> None:
    if now.tzinfo is None:
        raise EvaluationError("vast-container authorization clock is invalid")
    current = prepared.preflight
    execute_not_after = _parse_utc(authorization.execute_not_after_utc)
    termination = _parse_utc(authorization.external_guardian.termination_deadline_utc)
    required_seconds = (
        current.worst_case_duration_ns // 1_000_000_000
        + authorization.termination_safety_margin_seconds
    )
    if (
        authorization.runtime != current.runtime
        or (
            authorization.runtime == "vllm"
            and (
                authorization.schema_version
                != "inferdrome.load-calibration-vast-container-authorization.v2"
                or authorization.startup_ready_image is None
            )
        )
        or authorization.source_commit != current.source_commit
        or authorization.protocol_sha256 != current.protocol_sha256
        or authorization.recipe_sha256s != current.recipe_sha256s
        or authorization.runtime_executable_identity_sha256
        != current.runtime_identity.executable_identity_sha256
        or authorization.outer_image_reference
        != current.runtime_identity.outer_image_reference
        or authorization.outer_image_state != current.runtime_identity.outer_image_state
        or authorization.maximum_runtime_seconds
        < (current.worst_case_duration_ns + 999_999_999) // 1_000_000_000
        or authorization.model_id != QWEN3_8B_MODEL_ID
        or authorization.model_snapshot_path_sha256 != _path_sha256(model_snapshot_path)
        or authorization.output_root_sha256 != _path_sha256(output_root)
        or now > execute_not_after
        or now.timestamp() + required_seconds >= execute_not_after.timestamp()
        or now.timestamp() + required_seconds >= termination.timestamp()
    ):
        raise EvaluationError("vast-container authorization is unavailable or expired")
    if prepared.preflight.runtime == "sglang":
        profiles = prepared.prepared.sglang_profiles
        if (
            profiles is None
            or set(profiles) != set(_ENDPOINTS)
            or tuple(profiles[endpoint].origin for endpoint in _ENDPOINTS)
            != _origins(prepared.prepared)
            or any(
                profile.served_model_name != QWEN3_8B_MODEL_ID
                or profile.model_revision != QWEN3_8B_REVISION
                or profile.tokenizer_revision != QWEN3_8B_REVISION
                or profile.model_snapshot_sha256 != authorization.model_snapshot_sha256
                for profile in profiles.values()
            )
        ):
            raise EvaluationError("direct SGLang authorization is unbound")
    _empty_output_root(output_root)


def _sidecar(path: Path, content: bytes) -> None:
    with OutputFile(path) as destination:
        destination.write(content)


def _session_bytes(
    authorization: VastContainerRehearsalAuthorization,
    prepared: PreparedVastContainerRehearsal,
    *,
    started_ns: int,
    hard_deadline_ns: int,
) -> bytes:
    return (
        canonical_json_bytes(
            {
                "schema_version": _SESSION_SCHEMA,
                "authorization_sha256": sha256_digest(
                    canonical_json_bytes(authorization.model_dump(mode="json"))
                ),
                "preflight_sha256": sha256_digest(
                    vast_container_preflight_bytes(prepared.preflight)
                ),
                "runtime": prepared.preflight.runtime,
                "runtime_identity": prepared.preflight.runtime_identity.model_dump(
                    mode="json"
                ),
                "started_monotonic_ns": started_ns,
                "hard_deadline_monotonic_ns": hard_deadline_ns,
                "external_guardian_state": "EXTERNALLY_ARMED_NOT_VERIFIED",
                "provider_termination_verified": False,
                "evidence_eligible": False,
            }
        )
        + b"\n"
    )


def _attempt_plan_bytes(
    authorization: VastContainerRehearsalAuthorization,
    prepared: PreparedVastContainerRehearsal,
) -> bytes:
    attempts: list[dict[str, object]] = []
    for candidate in prepared.prepared.rehearsal.candidates:
        for phase, plan in (
            ("CALIBRATION", candidate.calibration_plan),
            ("CONFIRMATION", candidate.confirmation_plan),
        ):
            for trial in plan.trials:
                for endpoint_id in _ENDPOINTS:
                    binding = {
                        "ownership_id": authorization.ownership_id,
                        "runtime": prepared.preflight.runtime,
                        "phase": phase,
                        "source_trial_id": trial.trial_id,
                        "endpoint_id": endpoint_id,
                        "config_sha256": plan.config_sha256,
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
                "schema_version": _ATTEMPT_PLAN_SCHEMA,
                "authorization_sha256": sha256_digest(
                    canonical_json_bytes(authorization.model_dump(mode="json"))
                ),
                "attempts": attempts,
                "evidence_eligible": False,
            }
        )
        + b"\n"
    )


def _process_receipts_bytes(lifecycle: object) -> bytes:
    receipts = getattr(lifecycle, "receipts", ())
    return (
        canonical_json_bytes(
            {
                "schema_version": _PROCESS_RECEIPTS_SCHEMA,
                "processes": [item.__dict__ for item in receipts],
                "outer_image_state": "DECLARED_BY_OPERATOR_UNVERIFIED",
                "runtime_verification": "UNVERIFIED",
                "evidence_eligible": False,
            }
        )
        + b"\n"
    )


def _outcome_bytes(*, state: Literal["COMPLETED", "FAILED"], reason: str) -> bytes:
    return (
        canonical_json_bytes(
            {
                "schema_version": _OUTCOME_SCHEMA,
                "state": state,
                "reason": reason,
                "provider_termination_verified": False,
                "evidence_eligible": False,
            }
        )
        + b"\n"
    )


async def run_authorized_vast_container_rehearsal(
    prepared: PreparedVastContainerRehearsal,
    *,
    authorization: VastContainerRehearsalAuthorization,
    model_snapshot_path: Path,
    output_root: Path,
    stop: asyncio.Event | None = None,
    session_anchor_utc: datetime | None = None,
    session_started_ns: int | None = None,
    utc_clock: Callable[[], datetime] | None = None,
    monotonic_clock: Callable[[], int] = monotonic_ns,
    command_runner: LocalSubprocessRunner | None = None,
    process_runner: DirectProcessRunner | None = None,
) -> RehearsalResult:
    """Run only an exact manual packet; this never rents, publishes or destroys."""

    if stop is not None and type(stop) is not asyncio.Event:
        raise EvaluationError("vast-container cancellation boundary is invalid")
    if not callable(monotonic_clock):
        raise EvaluationError("vast-container session clock is invalid")
    stop = stop or asyncio.Event()
    if (session_anchor_utc is None) != (session_started_ns is None):
        raise EvaluationError("vast-container session anchor is incomplete")
    if session_anchor_utc is not None:
        observed_now = _checked_utc(session_anchor_utc)
    else:
        observed_now = _checked_utc(
            datetime.now(UTC) if utc_clock is None else utc_clock()
        )
    started_ns = monotonic_clock() if session_started_ns is None else session_started_ns
    if type(started_ns) is not int or started_ns < 0:
        raise EvaluationError("vast-container session clock is invalid")
    termination = _parse_utc(authorization.external_guardian.termination_deadline_utc)
    execute_not_after = _parse_utc(authorization.execute_not_after_utc)
    window_ns = min(
        prepared.prepared.rehearsal.calibration_plan.protocol.max_session_duration_ns,
        authorization.maximum_runtime_seconds * 1_000_000_000,
        _duration_ns(observed_now, execute_not_after),
        _duration_ns(observed_now, termination)
        - authorization.termination_safety_margin_seconds * 1_000_000_000,
    )
    if window_ns < 1:
        raise EvaluationError("vast-container session deadline is unavailable")
    hard_deadline_ns = started_ns + window_ns
    _authorized(
        authorization,
        prepared,
        model_snapshot_path=model_snapshot_path,
        output_root=output_root,
        now=observed_now,
    )
    # Re-resolve immediately before any engine object is constructed.  A
    # changed script/binary must fail before GPU idle inspection or spawning.
    executable = resolve_direct_runtime(prepared.executable)
    if (
        executable_identity_sha256(executable)
        != prepared.preflight.runtime_identity.executable_identity_sha256
    ):
        raise EvaluationError("direct-process runtime identity changed")
    dispatch_now = _checked_utc(datetime.now(UTC) if utc_clock is None else utc_clock())
    dispatch_ns = monotonic_clock()
    if (
        dispatch_now < observed_now
        or dispatch_now > execute_not_after
        or dispatch_now >= termination
        or type(dispatch_ns) is not int
        or dispatch_ns < started_ns
        or dispatch_ns >= hard_deadline_ns
        or stop.is_set()
    ):
        raise EvaluationError("vast-container session expired before dispatch")
    session = RehearsalSessionWindow(started_ns, hard_deadline_ns)
    session.validated_for(
        prepared.prepared.rehearsal.calibration_plan.protocol, now_ns=dispatch_ns
    )
    _sidecar(
        output_root / "operator-vast-container-session.json",
        _session_bytes(
            authorization,
            prepared,
            started_ns=started_ns,
            hard_deadline_ns=hard_deadline_ns,
        ),
    )
    _sidecar(
        output_root / "operator-vast-container-attempt-plan.json",
        _attempt_plan_bytes(authorization, prepared),
    )
    lifecycle: (
        TwoEngineVllmDirectProcessLifecycle | TwoEngineSglangDirectProcessLifecycle
    )
    try:
        origins = _origins(prepared.prepared)
        if prepared.preflight.runtime == "vllm":
            lifecycle = TwoEngineVllmDirectProcessLifecycle(
                origins,
                ownership_id=authorization.ownership_id,
                model_snapshot_path=model_snapshot_path,
                executable=executable,
                command_runner=command_runner,
                process_runner=process_runner,
                startup_timeout_ns=REAL_GPU_STARTUP_TIMEOUT_NS,
            )
            result = await run_rehearsal(
                prepared.prepared.rehearsal,
                output_root,
                lifecycle=lifecycle,
                executor=execute_trial,
                session_window=session,
                stop=stop,
            )
        else:
            profiles = prepared.prepared.sglang_profiles
            if profiles is None:
                raise EvaluationError("direct SGLang profiles are unavailable")
            bindings = bind_sglang_rehearsal(
                prepared.prepared.rehearsal, profiles, containerized=False
            )
            lifecycle = TwoEngineSglangDirectProcessLifecycle(
                profiles,
                contexts=bindings.contexts,
                ownership_id=authorization.ownership_id,
                executable=executable,
                command_runner=command_runner,
                process_runner=process_runner,
                startup_timeout_ns=REAL_GPU_STARTUP_TIMEOUT_NS,
            )
            result = await run_rehearsal(
                prepared.prepared.rehearsal,
                output_root,
                lifecycle=lifecycle,
                sglang_profiles=profiles,
                containerized=False,
                session_window=session,
                stop=stop,
            )
    except BaseException:
        _sidecar(
            output_root / "operator-vast-container-process-receipts.json",
            _process_receipts_bytes(locals().get("lifecycle")),
        )
        _sidecar(
            output_root / "operator-vast-container-outcome.json",
            _outcome_bytes(state="FAILED", reason="REHEARSAL_ABORTED"),
        )
        raise
    _sidecar(
        output_root / "operator-vast-container-process-receipts.json",
        _process_receipts_bytes(lifecycle),
    )
    _sidecar(
        output_root / "operator-vast-container-outcome.json",
        _outcome_bytes(state="COMPLETED", reason="REHEARSAL_FINISHED"),
    )
    return result
