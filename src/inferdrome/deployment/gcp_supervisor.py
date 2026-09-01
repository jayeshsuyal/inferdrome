"""Additive v0.2 approval, watchdog, and recovery supervision for GCP.

This module deliberately layers on top of the frozen v0.1 lease/request
contracts.  It has no Google SDK dependency and cannot create provider
resources.  A future, separately reviewed activation path may inject an
independent watchdog implementation only after this layer has bound and
persisted an exact operator approval.
"""

from __future__ import annotations

import os
import re
import select
import signal
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Protocol, Self, cast

from pydantic import Field, StringConstraints, ValidationError, model_validator

from inferdrome.deployment.gcp import (
    GcpProjectId,
    GcpRegion,
    GcpResourceName,
    GcpTimestamp,
    GcpZone,
)
from inferdrome.deployment.gcp_cost_guard import (
    GcpCostCleanupGuard,
    GcpReadOnlyQuoteBasis,
    gcp_cost_cleanup_guard_digest,
    gcp_hard_usd_ceiling_microusd,
    gcp_read_only_quote_basis_digest,
    validate_gcp_cost_cleanup_guard,
)
from inferdrome.deployment.gcp_lifecycle import (
    _CONTROLLER_RE,
    GcpComputeTransport,
    GcpExecutionError,
    GcpExecutionLabels,
    GcpExecutionModel,
    GcpExecutionPreflight,
    GcpInstanceObservation,
    GcpLeaseJournal,
    GcpLeaseRecord,
    GcpOperationHandle,
    GcpOperationResult,
    GcpOwnedResourceInventory,
    _parse_timestamp,
    _preflight_json,
    _timestamp,
    _write_all,
    gcp_capacity_digest,
    gcp_cost_quote_digest,
    gcp_execution_request_digest,
    validate_gcp_a2_profile,
    validate_gcp_execution_preflight_freshness,
)
from inferdrome.deployment.gcp_v2_contracts import (
    GcpCleanupRecoveryAuthorization,
    GcpV2ActivationDeadline,
    GcpV2MutationCapability,
    GcpV2StartupProjection,
    gcp_v2_activation_deadline_digest,
    gcp_v2_startup_projection_digest,
    issue_gcp_v2_mutation_capability,
    validate_gcp_cleanup_recovery_authorization,
    validate_gcp_v2_activation_deadline,
    validate_gcp_v2_startup_projection,
)
from inferdrome.deployment.gcp_v2_disk_cleanup import (
    GcpV2DiskCleanupBinding,
    GcpV2DiskCleanupError,
    GcpV2DiskCleanupOutcome,
    GcpV2ExactDiskCleanupCoordinator,
    GcpV2LocalDiskCleanupFactory,
)
from inferdrome.deployment.spec import CostCeiling
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes
from inferdrome.domain.ids import Sha256Digest

GCP_EXECUTION_APPROVAL_SCHEMA_VERSION: Final = "inferdrome.gcp-execution-approval.v2"
GCP_SUPERVISOR_BINDING_SCHEMA_VERSION: Final = "inferdrome.gcp-supervisor-binding.v2"
GCP_SUPERVISOR_EVENT_SCHEMA_VERSION: Final = "inferdrome.gcp-supervisor-event.v2"
GCP_WATCHDOG_RECEIPT_SCHEMA_VERSION: Final = "inferdrome.gcp-watchdog-receipt.v2"
GCP_KILL_SWITCH_SCHEMA_VERSION: Final = "inferdrome.gcp-kill-switch.v2"
GCP_EXACT_ORPHAN_REPORT_SCHEMA_VERSION: Final = "inferdrome.gcp-exact-orphan-report.v2"
GCP_WATCHDOG_ACTIVATION_RECEIPT_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-watchdog-activation-receipt.v2"
)
GCP_FILE_WATCHDOG_EVENT_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-file-watchdog-event.v2"
)
GCP_WATCHDOG_CLEANUP_RESULT_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-watchdog-cleanup-result.v2"
)
GCP_EXECUTION_APPROVAL_SCHEMA_ID: Final = "urn:inferdrome:gcp-execution-approval:v2"
GCP_SUPERVISOR_BINDING_SCHEMA_ID: Final = "urn:inferdrome:gcp-supervisor-binding:v2"
GCP_SUPERVISOR_EVENT_SCHEMA_ID: Final = "urn:inferdrome:gcp-supervisor-event:v2"
GCP_WATCHDOG_RECEIPT_SCHEMA_ID: Final = "urn:inferdrome:gcp-watchdog-receipt:v2"
GCP_KILL_SWITCH_SCHEMA_ID: Final = "urn:inferdrome:gcp-kill-switch:v2"
GCP_EXACT_ORPHAN_REPORT_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-exact-orphan-report:v2"
)
GCP_WATCHDOG_ACTIVATION_RECEIPT_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-watchdog-activation-receipt:v2"
)
GCP_FILE_WATCHDOG_EVENT_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-file-watchdog-event:v2"
)
GCP_WATCHDOG_CLEANUP_RESULT_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-watchdog-cleanup-result:v2"
)
GCP_SUPERVISOR_CONFIRMATION: Final = "APPROVE_GCP_V2_EXACT"
GCP_KILL_SWITCH_CONFIRMATION: Final = "KILL_GCP_V2_EXACT"
GCP_SUPERVISOR_MAX_EVENTS: Final = 64
GCP_SUPERVISOR_MAX_EVENT_BYTES: Final = 262_144
GCP_KILL_SWITCH_MAX_BYTES: Final = 8_192
GCP_FILE_WATCHDOG_MAX_EVENTS: Final = 32
GCP_FILE_WATCHDOG_MAX_EVENT_BYTES: Final = 131_072
GCP_FILE_WATCHDOG_MAX_RESULT_BYTES: Final = 32_768
# Keep enough journal capacity to durably settle an active task instead of
# letting adversarial restart churn exhaust the append-only sidecar first.
# These are deliberately independent of the provider-facing cleanup retry
# budget below: they bound only replacement of the local runner process.
GCP_FILE_WATCHDOG_MAX_RUNNER_RESUMES: Final = 3
GCP_FILE_WATCHDOG_TERMINAL_EVENT_RESERVE: Final = 1

_OPERATOR_ID_RE = re.compile(r"^operator-[a-z0-9][a-z0-9_-]{2,61}$")
_WATCHDOG_ID_RE = re.compile(r"^watchdog-[a-z][a-z0-9-]{2,61}$")
_EVENT_PATH_RE = re.compile(
    r"^(ctl-[a-z0-9]{8,24})\.safety-v2\.events\.jsonl$"
)
_WATCHDOG_EVENT_PATH_RE = re.compile(
    r"^(ctl-[a-z0-9]{8,24})\.file-watchdog-v2\.events\.jsonl$"
)


def _same_managed_root(left: Path, right: Path) -> bool:
    """Return true for equal, nested, aliased, or unsafe managed roots.

    Independent journals must never share a directory, child directory, or
    symlink alias.  Treat an unreadable/non-directory/symlinked root as an
    overlap as well: callers use this predicate only to reject an unsafe
    configuration before mutation authority could exist.
    """

    def checked(value: Path) -> str | None:
        raw = os.path.abspath(os.fspath(value))
        try:
            metadata = os.lstat(raw)
            resolved = os.path.realpath(raw)
        except OSError:
            return None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o022
            or os.path.normcase(resolved) != os.path.normcase(raw)
        ):
            return None
        return raw

    left_root = checked(left)
    right_root = checked(right)
    if left_root is None or right_root is None:
        return True
    try:
        common = os.path.commonpath((left_root, right_root))
    except ValueError:
        return True
    return common in {left_root, right_root}


GcpSupervisorState = Literal[
    "PREPARED",
    "ARM_CONSUMED",
    "WATCHDOG_READY",
    "CREATE_INTENT",
    "WORKING",
    "CLEANUP_PENDING",
    "CLEANUP_CONFIRMED",
    "ORPHANED",
    "KILLED",
    "BLOCKED",
]
GcpSupervisorErrorCode = Annotated[
    str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,47}$")
]
GcpOperatorIdentity = Annotated[
    str,
    StringConstraints(min_length=12, max_length=70, pattern=_OPERATOR_ID_RE.pattern),
]
GcpWatchdogId = Annotated[
    str,
    StringConstraints(min_length=12, max_length=70, pattern=_WATCHDOG_ID_RE.pattern),
]


class GcpSupervisorError(GcpExecutionError):
    """A bounded local approval/watchdog/journal failure."""

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,47}", code) is None:
            code = "SUPERVISOR_ERROR"
        self.code = code
        super().__init__(code)


class GcpExecutionApprovalPayload(GcpExecutionModel):
    """Exact asserted operator-confirmation inputs for a future SDK gate."""

    schema_version: Literal["inferdrome.gcp-execution-approval.v2"]
    approval_kind: Literal["exact_operator_execution_approval"]
    operator_identity: GcpOperatorIdentity
    operator_confirmation: Literal["APPROVE_GCP_V2_EXACT"]
    provider: Literal["gcp-compute-engine"]
    plan_id: Sha256Digest
    plan_sha256: Sha256Digest
    arm_id: Sha256Digest
    request_digest: Sha256Digest
    startup_projection_digest: Sha256Digest
    execution_payload_digest: Sha256Digest
    quote_digest: Sha256Digest
    capacity_digest: Sha256Digest
    rate_basis_digest: Sha256Digest
    cost_guard_digest: Sha256Digest
    activation_deadline_digest: Sha256Digest
    quote_currency: Literal["USD"]
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    machine_type: GcpResourceName
    accelerator_model: Literal["NVIDIA A100-SXM4-40GB"]
    accelerator_provider_type: GcpResourceName
    accelerator_count: Literal[1]
    accelerator_attachment_mode: Literal["a2_fixed_gpu"]
    boot_image_name: Annotated[
        str,
        StringConstraints(
            pattern=(
                r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/global/images/"
                r"[a-z][a-z0-9-]{0,61}[a-z0-9]$"
            )
        ),
    ]
    boot_image_provider_id: int = Field(strict=True, ge=1, le=10**19)
    boot_image_digest: Sha256Digest
    runner_image_digest: Sha256Digest
    serving_runtime_image_digest: Sha256Digest
    max_runtime_seconds: int = Field(strict=True, ge=1, le=86_400)
    # ``controller_deadline_at`` remains the frozen-arm observation retained
    # for audit compatibility.  v2 launch timing is governed exclusively by
    # the separately versioned activation deadline contract below.
    controller_deadline_at: GcpTimestamp
    authorization_expires_at: GcpTimestamp
    setup_deadline_at: GcpTimestamp
    setup_margin_seconds: int = Field(strict=True, ge=1, le=3_600)
    provider_runtime_seconds: int = Field(strict=True, ge=1, le=86_400)
    watchdog_cleanup_horizon_seconds: int = Field(strict=True, ge=1, le=7_200)
    watchdog_deadline_at: GcpTimestamp
    hard_cost_ceiling: CostCeiling
    hard_ceiling_microusd: int = Field(strict=True, ge=0, le=10**12)
    estimated_max_microusd: int = Field(strict=True, ge=0, le=10**12)
    issued_at: GcpTimestamp
    expires_at: GcpTimestamp

    @model_validator(mode="after")
    def validate_approval_payload(self) -> Self:
        validate_gcp_a2_profile(
            self.machine_type,
            self.accelerator_model,
            self.accelerator_provider_type,
            self.accelerator_count,
        )
        issued = _parse_timestamp(self.issued_at)
        expires = _parse_timestamp(self.expires_at)
        controller_deadline = _parse_timestamp(self.controller_deadline_at)
        authorization_deadline = _parse_timestamp(self.authorization_expires_at)
        setup_deadline = _parse_timestamp(self.setup_deadline_at)
        watchdog_deadline = _parse_timestamp(self.watchdog_deadline_at)
        if expires != authorization_deadline or not (
            issued < setup_deadline <= authorization_deadline
        ):
            raise ValueError("approval times are inconsistent")
        if controller_deadline <= issued or watchdog_deadline <= setup_deadline:
            raise ValueError("approval deadlines are inconsistent")
        if self.provider_runtime_seconds != self.max_runtime_seconds:
            raise ValueError("approval runtime binding is inconsistent")
        if (
            self.hard_cost_ceiling.currency != "USD"
            or self.hard_cost_ceiling.hard_limit is not True
            or self.hard_cost_ceiling.estimate_basis != "controller_estimate"
        ):
            raise ValueError("approval hard cost ceiling is inconsistent")
        try:
            hard_ceiling = gcp_hard_usd_ceiling_microusd(self.hard_cost_ceiling)
        except GcpExecutionError:
            raise ValueError("approval hard cost ceiling is invalid") from None
        if self.hard_ceiling_microusd != hard_ceiling:
            raise ValueError("approval fixed-point cost ceiling is inconsistent")
        if self.estimated_max_microusd > self.hard_ceiling_microusd:
            raise ValueError("approval estimate exceeds its hard cost ceiling")
        return self


class GcpExecutionApproval(GcpExecutionApprovalPayload):
    """An immutable exact content-addressed local approval binding, not a signature."""

    approval_id: Sha256Digest

    @model_validator(mode="after")
    def validate_approval_identity(self) -> Self:
        if self.approval_id != gcp_execution_approval_id(self):
            raise ValueError("approval identity does not match its payload")
        return self


class GcpSupervisorBinding(GcpExecutionModel):
    """Immutable exact resource/approval binding repeated in every event."""

    schema_version: Literal["inferdrome.gcp-supervisor-binding.v2"]
    approval_id: Sha256Digest
    approval_digest: Sha256Digest
    activation_deadline_digest: Sha256Digest
    controller_id: Annotated[
        str, StringConstraints(pattern=r"^ctl-[a-z0-9]{8,24}$")
    ]
    arm_id: Sha256Digest
    plan_id: Sha256Digest
    request_digest: Sha256Digest
    startup_projection_digest: Sha256Digest
    execution_payload_digest: Sha256Digest
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    instance_name: Annotated[
        str, StringConstraints(pattern=r"^inferdrome-ctl-[a-z0-9]{8,24}$")
    ]
    labels: GcpExecutionLabels
    setup_deadline_at: GcpTimestamp
    provider_runtime_seconds: int = Field(strict=True, ge=1, le=86_400)
    watchdog_cleanup_horizon_seconds: int = Field(strict=True, ge=1, le=7_200)
    max_cleanup_attempts: int = Field(strict=True, ge=1, le=3)
    cleanup_timeout_seconds: int = Field(strict=True, ge=1, le=3_600)
    controller_deadline_at: GcpTimestamp
    watchdog_deadline_at: GcpTimestamp


class GcpExactOrphanReport(GcpExecutionModel):
    """Durable proof that one exact owned resource set remains unconfirmed."""

    schema_version: Literal["inferdrome.gcp-exact-orphan-report.v2"]
    approval_digest: Sha256Digest
    request_digest: Sha256Digest
    controller_id: Annotated[
        str, StringConstraints(pattern=r"^ctl-[a-z0-9]{8,24}$")
    ]
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    instance_name: Annotated[
        str, StringConstraints(pattern=r"^inferdrome-ctl-[a-z0-9]{8,24}$")
    ]
    labels: GcpExecutionLabels
    cleanup_state: Literal["unconfirmed_exact_owned_resource"]
    reason_code: GcpSupervisorErrorCode
    reported_at: GcpTimestamp
    report_id: Sha256Digest

    @model_validator(mode="after")
    def validate_orphan_identity(self) -> Self:
        if self.report_id != gcp_exact_orphan_report_id(self):
            raise ValueError("orphan report identity does not match its payload")
        return self


class GcpKillSwitchRecord(GcpExecutionModel):
    """One exact local kill marker; it carries no provider credential."""

    schema_version: Literal["inferdrome.gcp-kill-switch.v2"]
    action: Literal["KILL"]
    operator_identity: GcpOperatorIdentity
    operator_confirmation: Literal["KILL_GCP_V2_EXACT"]
    approval_digest: Sha256Digest
    request_digest: Sha256Digest
    controller_id: Annotated[
        str, StringConstraints(pattern=r"^ctl-[a-z0-9]{8,24}$")
    ]
    issued_at: GcpTimestamp
    kill_id: Sha256Digest

    @model_validator(mode="after")
    def validate_kill_identity(self) -> Self:
        if self.kill_id != gcp_kill_switch_id(self):
            raise ValueError("kill-switch identity does not match its payload")
        return self


class GcpWatchdogReceipt(GcpExecutionModel):
    """A future external watchdog's exact, independently-ready receipt."""

    schema_version: Literal["inferdrome.gcp-watchdog-receipt.v2"]
    watchdog_id: GcpWatchdogId
    approval_digest: Sha256Digest
    activation_deadline_digest: Sha256Digest
    request_digest: Sha256Digest
    startup_projection_digest: Sha256Digest
    execution_payload_digest: Sha256Digest
    controller_id: Annotated[
        str, StringConstraints(pattern=r"^ctl-[a-z0-9]{8,24}$")
    ]
    armed_at: GcpTimestamp
    expires_at: GcpTimestamp
    ready: Literal[True]
    independently_durable: Literal[True]
    controller_death_coverage: Literal[True]
    hung_work_coverage: Literal[True]

    @model_validator(mode="after")
    def validate_watchdog_times(self) -> Self:
        if _parse_timestamp(self.expires_at) <= _parse_timestamp(self.armed_at):
            raise ValueError("watchdog expiry must follow arming")
        return self


class GcpWatchdogActivationReceipt(GcpExecutionModel):
    """Durable acknowledgement of the actual bounded runtime window."""

    schema_version: Literal["inferdrome.gcp-watchdog-activation-receipt.v2"]
    watchdog_id: GcpWatchdogId
    approval_digest: Sha256Digest
    activation_deadline_digest: Sha256Digest
    request_digest: Sha256Digest
    startup_projection_digest: Sha256Digest
    execution_payload_digest: Sha256Digest
    controller_id: Annotated[
        str, StringConstraints(pattern=r"^ctl-[a-z0-9]{8,24}$")
    ]
    capability_id: Sha256Digest
    activated_at: GcpTimestamp
    provider_runtime_deadline_at: GcpTimestamp
    watchdog_cleanup_deadline_at: GcpTimestamp
    ready: Literal[True]
    independently_durable: Literal[True]
    receipt_id: Sha256Digest

    @model_validator(mode="after")
    def validate_receipt_identity(self) -> Self:
        if not (
            _parse_timestamp(self.activated_at)
            < _parse_timestamp(self.provider_runtime_deadline_at)
            < _parse_timestamp(self.watchdog_cleanup_deadline_at)
        ):
            raise ValueError("watchdog activation horizons are inconsistent")
        value = _model_value(self)
        value.pop("receipt_id", None)
        expected = digest_bytes(
            DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(value)
        )
        if self.receipt_id != expected:
            raise ValueError("watchdog activation receipt identity is invalid")
        return self


_MUTATION_ACTIVATION_PROOF_SEAL = object()


class _MutationProofUse:
    """Private, purpose-scoped uses retained with an activation proof."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.consumed: set[Literal["transport", "disk_cleanup"]] = set()


@dataclass(frozen=True)
class GcpV2ActivatedMutationProof:
    """Opaque controller-bound proof produced only after watchdog activation.

    This is deliberately an in-memory handoff, not another approval artifact.
    The private object-identity seal prevents a structurally forged capability
    from being accepted by the capability-only transport factory.  Its nested
    contracts are still canonically revalidated at that factory boundary.
    """

    capability: GcpV2MutationCapability
    receipt: GcpWatchdogActivationReceipt
    _seal: object
    _use: _MutationProofUse


def _strict_mutation_capability(
    capability: GcpV2MutationCapability,
) -> GcpV2MutationCapability:
    try:
        raw = canonical_json_bytes(_model_value(capability))
        parsed = GcpV2MutationCapability.model_validate_json(raw)
    except (AttributeError, ValidationError, ValueError, TypeError):
        raise GcpSupervisorError("MUTATION_CAPABILITY_INVALID") from None
    if canonical_json_bytes(_model_value(parsed)) != raw:
        raise GcpSupervisorError("MUTATION_CAPABILITY_INVALID")
    return parsed


def _strict_watchdog_activation_receipt(
    receipt: GcpWatchdogActivationReceipt,
) -> GcpWatchdogActivationReceipt:
    try:
        raw = canonical_gcp_watchdog_activation_receipt_bytes(receipt)
        parsed = GcpWatchdogActivationReceipt.model_validate_json(raw)
    except (AttributeError, ValidationError, ValueError, TypeError):
        raise GcpSupervisorError("WATCHDOG_ACTIVATION_RECEIPT_INVALID") from None
    if canonical_gcp_watchdog_activation_receipt_bytes(parsed) != raw:
        raise GcpSupervisorError("WATCHDOG_ACTIVATION_RECEIPT_INVALID")
    return parsed


def _strict_disk_cleanup_binding(
    binding: GcpV2DiskCleanupBinding,
) -> GcpV2DiskCleanupBinding:
    try:
        raw = canonical_json_bytes(_model_value(binding))
        parsed = GcpV2DiskCleanupBinding.model_validate_json(raw)
    except (AttributeError, ValidationError, ValueError, TypeError):
        raise GcpSupervisorError("WATCHDOG_DISK_BINDING_INVALID") from None
    if canonical_json_bytes(_model_value(parsed)) != raw:
        raise GcpSupervisorError("WATCHDOG_DISK_BINDING_INVALID")
    return parsed


def validate_gcp_v2_activated_mutation_proof(
    proof: object, *, now: datetime
) -> GcpV2ActivatedMutationProof:
    """Validate the sealed post-watchdog proof at a future factory edge."""

    if (
        not isinstance(proof, GcpV2ActivatedMutationProof)
        or proof._seal is not _MUTATION_ACTIVATION_PROOF_SEAL
    ):
        raise GcpSupervisorError("MUTATION_PROOF_INVALID")
    capability = _strict_mutation_capability(proof.capability)
    receipt = _strict_watchdog_activation_receipt(proof.receipt)
    if (
        receipt.capability_id != capability.capability_id
        or receipt.approval_digest != capability.approval_digest
        or receipt.activation_deadline_digest
        != capability.activation_contract_digest
        or receipt.request_digest != capability.request_digest
        or receipt.startup_projection_digest
        != capability.startup_projection_digest
        or receipt.execution_payload_digest != capability.execution_payload_digest
        or receipt.controller_id != capability.controller_id
        or receipt.activated_at != capability.activated_at
        or receipt.provider_runtime_deadline_at
        != capability.provider_runtime_deadline_at
        or receipt.watchdog_cleanup_deadline_at
        != capability.watchdog_cleanup_deadline_at
    ):
        raise GcpSupervisorError("MUTATION_PROOF_BINDING_MISMATCH")
    try:
        now_value = _parse_timestamp(_timestamp(now))
    except ValueError:
        raise GcpSupervisorError("MUTATION_PROOF_TIME_INVALID") from None
    if not (
        _parse_timestamp(capability.activated_at)
        <= now_value
        < _parse_timestamp(capability.provider_runtime_deadline_at)
    ):
        raise GcpSupervisorError("MUTATION_PROOF_EXPIRED")
    return proof


def consume_gcp_v2_activated_mutation_proof(
    proof: object,
    *,
    now: datetime,
    purpose: Literal["transport", "disk_cleanup"] = "transport",
) -> GcpV2ActivatedMutationProof:
    """Consume one sealed proof for one narrow future factory edge.

    The capability itself is already consumed in the supervisor journal path.
    Separate one-shot purposes permit the controller to make its exact
    create-bound transport and its exact disk-cleanup adapter from the same
    already-activated watchdog proof.  Neither purpose can be replayed, and
    a failed factory invocation remains consumed rather than granting a retry
    that could race an ambiguous future client construction.
    """

    parsed = validate_gcp_v2_activated_mutation_proof(proof, now=now)
    if not isinstance(parsed._use, _MutationProofUse):
        raise GcpSupervisorError("MUTATION_PROOF_INVALID")
    with parsed._use.lock:
        if purpose in parsed._use.consumed:
            raise GcpSupervisorError("MUTATION_PROOF_ALREADY_CONSUMED")
        parsed._use.consumed.add(purpose)
    return parsed


GcpFileWatchdogState = Literal[
    "ARMED",
    "READY",
    "ACTIVE",
    "RUNNER_READY",
    "DISK_BOUND",
    "PREBIND_CLEANUP_INTENT",
    "PREBIND_RETRY_PENDING",
    "CLEANUP_INTENT",
    "RETRY_PENDING",
    "CLEANUP_CONFIRMED",
    "CLEANUP_NOT_REQUIRED",
    "ORPHANED",
]


class GcpFileWatchdogEvent(GcpExecutionModel):
    """One bounded, fsync-backed local watchdog transition."""

    schema_version: Literal["inferdrome.gcp-file-watchdog-event.v2"]
    sequence: int = Field(strict=True, ge=0, le=GCP_FILE_WATCHDOG_MAX_EVENTS - 1)
    state: GcpFileWatchdogState
    binding: GcpSupervisorBinding
    watchdog_id: GcpWatchdogId
    armed_at: GcpTimestamp
    expires_at: GcpTimestamp
    capability_id: Sha256Digest | None = None
    mutation_capability: GcpV2MutationCapability | None = None
    activation_receipt_id: Sha256Digest | None = None
    provider_runtime_deadline_at: GcpTimestamp | None = None
    watchdog_cleanup_deadline_at: GcpTimestamp | None = None
    # This is a local process identity, not an authority or provider fact.  It
    # lets a restart diagnose an interrupted runner without treating a stale
    # PID as proof that cleanup completed.
    runner_process_id: int | None = Field(default=None, strict=True, ge=1)
    runner_ready_at: GcpTimestamp | None = None
    executor_config_digest: Sha256Digest | None = None
    # A boot disk does not exist before insert.  The watchdog is therefore
    # durably ready on the exact lease first, then records a separately
    # observed exact disk binding before work may begin.
    disk_cleanup_binding: GcpV2DiskCleanupBinding | None = None
    cleanup_result: GcpWatchdogCleanupResult | None = None
    prebind_cleanup_result: GcpWatchdogPrebindCleanupResult | None = None
    # A core journal can authoritatively confirm this exact lease absent while
    # a locally forked cleanup child still owns an intent.  This fsync-backed
    # fence prevents a later runner restart from launching that stale intent;
    # it is not a provider result or an authority expansion.
    core_terminal_fenced_at: GcpTimestamp | None = None
    cleanup_attempts: int = Field(strict=True, ge=0, le=3)
    max_cleanup_attempts: int = Field(strict=True, ge=1, le=3)
    cleanup_timeout_seconds: int = Field(strict=True, ge=1, le=3_600)
    occurred_at: GcpTimestamp
    error_code: GcpSupervisorErrorCode | None = None
    previous_event_digest: Sha256Digest | None
    event_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_event_identity(self) -> Self:
        requires_runtime_binding = self.state in {
            "ACTIVE",
            "RUNNER_READY",
            "DISK_BOUND",
            "PREBIND_CLEANUP_INTENT",
            "PREBIND_RETRY_PENDING",
            "CLEANUP_INTENT",
            "RETRY_PENDING",
            "CLEANUP_CONFIRMED",
            "ORPHANED",
        }
        carries_runtime_binding = (
            self.capability_id is not None
            or self.activation_receipt_id is not None
            or self.provider_runtime_deadline_at is not None
            or self.watchdog_cleanup_deadline_at is not None
        )
        if requires_runtime_binding and (
            self.capability_id is None
            or self.mutation_capability is None
            or self.activation_receipt_id is None
            or self.provider_runtime_deadline_at is None
            or self.watchdog_cleanup_deadline_at is None
        ):
            raise ValueError("active watchdog event lacks exact runtime binding")
        if self.state in {"ARMED", "READY"} and carries_runtime_binding:
            raise ValueError("inactive watchdog event carries runtime binding")
        if self.state != "CLEANUP_NOT_REQUIRED" and self.executor_config_digest is None:
            raise ValueError("watchdog event lacks sealed executor configuration")
        if self.mutation_capability is not None and (
            self.capability_id != self.mutation_capability.capability_id
            or self.mutation_capability.approval_digest
            != self.binding.approval_digest
            or self.mutation_capability.activation_contract_digest
            != self.binding.activation_deadline_digest
            or self.mutation_capability.request_digest != self.binding.request_digest
            or self.mutation_capability.startup_projection_digest
            != self.binding.startup_projection_digest
            or self.mutation_capability.execution_payload_digest
            != self.binding.execution_payload_digest
            or self.mutation_capability.controller_id != self.binding.controller_id
            or self.mutation_capability.project_id != self.binding.project_id
            or self.mutation_capability.region != self.binding.region
            or self.mutation_capability.zone != self.binding.zone
            or self.mutation_capability.instance_name != self.binding.instance_name
            or self.mutation_capability.labels != self.binding.labels
        ):
            raise ValueError("watchdog capability binding is inconsistent")
        requires_runner = self.state in {
            "RUNNER_READY",
            "DISK_BOUND",
            "PREBIND_CLEANUP_INTENT",
            "PREBIND_RETRY_PENDING",
            "CLEANUP_INTENT",
            "RETRY_PENDING",
            "CLEANUP_CONFIRMED",
            "ORPHANED",
        }
        if requires_runner and (
            self.runner_process_id is None
            or self.runner_ready_at is None
            or self.executor_config_digest is None
        ):
            raise ValueError("active watchdog event lacks independent runner receipt")
        if self.state in {"ARMED", "READY", "ACTIVE"} and (
            self.runner_process_id is not None
            or self.runner_ready_at is not None
        ):
            raise ValueError("unready watchdog event carries a runner receipt")
        if self.state == "CLEANUP_NOT_REQUIRED" and carries_runtime_binding and (
            self.capability_id is None
            or self.activation_receipt_id is None
            or self.provider_runtime_deadline_at is None
            or self.watchdog_cleanup_deadline_at is None
        ):
            raise ValueError("terminal watchdog event has partial runtime binding")
        if self.state in {"ARMED", "READY"} and self.cleanup_attempts != 0:
            raise ValueError("unactivated watchdog cannot consume cleanup attempts")
        requires_disk_binding = self.state in {
            "DISK_BOUND",
            "CLEANUP_INTENT",
            "RETRY_PENDING",
            "CLEANUP_CONFIRMED",
        }
        disk_binding = self.disk_cleanup_binding
        if requires_disk_binding and disk_binding is None:
            raise ValueError("watchdog cleanup state lacks exact disk binding")
        if disk_binding is not None and (
            disk_binding.controller_id != self.binding.controller_id
            or disk_binding.request_digest != self.binding.request_digest
            or disk_binding.project_id != self.binding.project_id
            or disk_binding.zone != self.binding.zone
            or disk_binding.instance_name != self.binding.instance_name
            or disk_binding.labels != self.binding.labels
        ):
            raise ValueError("watchdog exact disk binding is inconsistent")
        if self.state == "CLEANUP_CONFIRMED":
            if self.cleanup_result is None or disk_binding is None:
                raise ValueError("confirmed watchdog cleanup lacks exact outcome")
            if (
                self.cleanup_result.disk_cleanup_binding != disk_binding
                or self.cleanup_result.controller_id != self.binding.controller_id
                or self.cleanup_result.request_digest != self.binding.request_digest
            ):
                raise ValueError("confirmed watchdog cleanup result is inconsistent")
        elif self.cleanup_result is not None:
            raise ValueError("watchdog cleanup result is only valid at confirmation")
        if self.prebind_cleanup_result is not None:
            if self.state != "ORPHANED" or disk_binding is not None:
                raise ValueError(
                    "prebind result is only valid for unresolved disk cleanup"
                )
            if (
                self.prebind_cleanup_result.controller_id != self.binding.controller_id
                or self.prebind_cleanup_result.request_digest
                != self.binding.request_digest
            ):
                raise ValueError("prebind watchdog result is inconsistent")
        if self.core_terminal_fenced_at is not None and self.state not in {
            "PREBIND_CLEANUP_INTENT",
            "CLEANUP_INTENT",
            "CLEANUP_NOT_REQUIRED",
        }:
            raise ValueError("core terminal fence has an invalid watchdog state")
        if self.cleanup_attempts > self.max_cleanup_attempts:
            raise ValueError("watchdog cleanup attempt bound is inconsistent")
        if self.provider_runtime_deadline_at is not None and (
            _parse_timestamp(self.provider_runtime_deadline_at)
            >= _parse_timestamp(self.watchdog_cleanup_deadline_at or self.armed_at)
        ):
            raise ValueError("watchdog cleanup deadline is inconsistent")
        value = _model_value(self)
        value.pop("event_digest", None)
        expected = digest_bytes(
            DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(value)
        )
        if self.event_digest != expected:
            raise ValueError("file watchdog event identity is invalid")
        return self


class GcpWatchdogCleanupResult(GcpExecutionModel):
    """Exact result returned by an injected cleanup-only executor."""

    schema_version: Literal["inferdrome.gcp-watchdog-cleanup-result.v2"]
    controller_id: Annotated[
        str, StringConstraints(pattern=r"^ctl-[a-z0-9]{8,24}$")
    ]
    request_digest: Sha256Digest
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    instance_name: Annotated[
        str, StringConstraints(pattern=r"^inferdrome-ctl-[a-z0-9]{8,24}$")
    ]
    labels: GcpExecutionLabels
    disk_cleanup_binding: GcpV2DiskCleanupBinding
    disk_cleanup_outcome: GcpV2DiskCleanupOutcome
    instance_observation: GcpInstanceObservation
    owned_inventory: GcpOwnedResourceInventory

    @model_validator(mode="after")
    def validate_exact_identity(self) -> Self:
        disk_binding = self.disk_cleanup_binding
        if (
            disk_binding.controller_id != self.controller_id
            or disk_binding.request_digest != self.request_digest
            or disk_binding.project_id != self.project_id
            or disk_binding.zone != self.zone
            or disk_binding.instance_name != self.instance_name
            or disk_binding.labels != self.labels
            or self.disk_cleanup_outcome.binding_id != disk_binding.binding_id
            or self.disk_cleanup_outcome.state != "ABSENCE_CONFIRMED"
            or self.disk_cleanup_outcome.absence is None
            or self.disk_cleanup_outcome.absence.request_digest != self.request_digest
            or self.disk_cleanup_outcome.absence.project_id != self.project_id
            or self.disk_cleanup_outcome.absence.zone != self.zone
            or self.disk_cleanup_outcome.absence.instance_name != self.instance_name
            or self.disk_cleanup_outcome.absence.disk_name
            != disk_binding.disk.disk_name
            or self.disk_cleanup_outcome.absence.labels != self.labels
            or self.instance_observation.state != "NOT_FOUND"
            or self.instance_observation.instance_name != self.instance_name
            or self.instance_observation.project_id != self.project_id
            or self.instance_observation.zone != self.zone
            or self.owned_inventory.request_digest != self.request_digest
            or self.owned_inventory.project_id != self.project_id
            or self.owned_inventory.region != self.region
            or self.owned_inventory.zone != self.zone
            or self.owned_inventory.labels != self.labels
            or self.owned_inventory.instances != ()
        ):
            raise ValueError("watchdog cleanup disk identity is inconsistent")
        return self


class GcpWatchdogPrebindCleanupResult(GcpExecutionModel):
    """Exact instance cleanup result when no boot-disk binding was recorded.

    This proves only that the exact instance and complete label-scoped
    inventory are absent.  It intentionally cannot confirm cleanup: the
    unbound disk remains an explicit orphan for later exact recovery.
    """

    schema_version: Literal["inferdrome.gcp-watchdog-prebind-cleanup-result.v2"]
    controller_id: Annotated[
        str, StringConstraints(pattern=r"^ctl-[a-z0-9]{8,24}$")
    ]
    request_digest: Sha256Digest
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    instance_name: Annotated[
        str, StringConstraints(pattern=r"^inferdrome-ctl-[a-z0-9]{8,24}$")
    ]
    labels: GcpExecutionLabels
    instance_observation: GcpInstanceObservation
    owned_inventory: GcpOwnedResourceInventory

    @model_validator(mode="after")
    def validate_exact_instance_absence(self) -> Self:
        if (
            self.instance_observation.state != "NOT_FOUND"
            or self.instance_observation.instance_name != self.instance_name
            or self.instance_observation.project_id != self.project_id
            or self.instance_observation.zone != self.zone
            or self.owned_inventory.request_digest != self.request_digest
            or self.owned_inventory.project_id != self.project_id
            or self.owned_inventory.region != self.region
            or self.owned_inventory.zone != self.zone
            or self.owned_inventory.labels != self.labels
            or self.owned_inventory.instances != ()
        ):
            raise ValueError("prebind watchdog instance identity is inconsistent")
        return self


GcpFileWatchdogEvent.model_rebuild()


class GcpSupervisorJournalEvent(GcpExecutionModel):
    """One bounded, hash-linked v0.2 supervisor transition."""

    schema_version: Literal["inferdrome.gcp-supervisor-event.v2"]
    sequence: int = Field(strict=True, ge=0, le=GCP_SUPERVISOR_MAX_EVENTS - 1)
    state: GcpSupervisorState
    binding: GcpSupervisorBinding
    occurred_at: GcpTimestamp
    previous_event_digest: Sha256Digest | None
    watchdog_receipt_digest: Sha256Digest | None = None
    orphan_report: GcpExactOrphanReport | None = None
    error_code: GcpSupervisorErrorCode | None = None
    event_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_event_identity(self) -> Self:
        if self.state == "WATCHDOG_READY" and self.watchdog_receipt_digest is None:
            raise ValueError("watchdog-ready event requires a receipt digest")
        if self.state == "ORPHANED" and self.orphan_report is None:
            raise ValueError("orphaned event requires an exact orphan report")
        if self.state != "ORPHANED" and self.orphan_report is not None:
            raise ValueError("orphan report is only valid for orphaned state")
        value = _model_value(self)
        value.pop("event_digest", None)
        expected = digest_bytes(
            DigestDomain.GCP_EXECUTION_SUPERVISOR, canonical_json_bytes(value)
        )
        if self.event_digest != expected:
            raise ValueError("supervisor event identity is invalid")
        return self


def _model_value(model: GcpExecutionModel) -> dict[str, Any]:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("supervisor contract must serialize as an object")
    return value


def canonical_gcp_execution_approval_payload_bytes(
    approval: GcpExecutionApproval | GcpExecutionApprovalPayload,
) -> bytes:
    value = _model_value(approval)
    value.pop("approval_id", None)
    return canonical_json_bytes(value)


def gcp_execution_approval_id(
    approval: GcpExecutionApproval | GcpExecutionApprovalPayload,
) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_APPROVAL,
        canonical_gcp_execution_approval_payload_bytes(approval),
    )


def canonical_gcp_execution_approval_bytes(approval: GcpExecutionApproval) -> bytes:
    return canonical_json_bytes(_model_value(approval))


def gcp_execution_approval_digest(approval: GcpExecutionApproval) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_APPROVAL,
        canonical_gcp_execution_approval_bytes(approval),
    )


def parse_gcp_execution_approval_json(payload: str | bytes) -> GcpExecutionApproval:
    """Parse one strict content-addressed local v0.2 approval binding."""

    try:
        _preflight_json(payload, kind="GCP execution approval")
        return GcpExecutionApproval.model_validate_json(payload)
    except (ValidationError, ValueError):
        raise GcpSupervisorError("APPROVAL_INVALID") from None


def canonical_gcp_watchdog_receipt_bytes(receipt: GcpWatchdogReceipt) -> bytes:
    return canonical_json_bytes(_model_value(receipt))


def gcp_watchdog_receipt_digest(receipt: GcpWatchdogReceipt) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_WATCHDOG,
        canonical_gcp_watchdog_receipt_bytes(receipt),
    )


def canonical_gcp_watchdog_activation_receipt_bytes(
    receipt: GcpWatchdogActivationReceipt,
) -> bytes:
    return canonical_json_bytes(_model_value(receipt))


def gcp_watchdog_activation_receipt_id(
    receipt: GcpWatchdogActivationReceipt,
) -> Sha256Digest:
    value = _model_value(receipt)
    value.pop("receipt_id", None)
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(value)
    )


def _strict_approval(approval: GcpExecutionApproval) -> GcpExecutionApproval:
    raw = canonical_gcp_execution_approval_bytes(approval)
    try:
        parsed = GcpExecutionApproval.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise GcpSupervisorError("APPROVAL_INVALID") from None
    if canonical_gcp_execution_approval_bytes(parsed) != raw:
        raise GcpSupervisorError("APPROVAL_NONCANONICAL")
    return parsed


def issue_gcp_execution_approval(
    *,
    preflight: GcpExecutionPreflight,
    quote_basis: GcpReadOnlyQuoteBasis,
    cost_guard: GcpCostCleanupGuard,
    activation_deadline: GcpV2ActivationDeadline,
    startup_projection: GcpV2StartupProjection,
    operator_identity: str,
    issued_at: datetime,
    expires_at: datetime,
    confirmation: str,
) -> GcpExecutionApproval:
    """Create a local exact approval; this never initializes SDK/ADC."""

    if confirmation != GCP_SUPERVISOR_CONFIRMATION:
        raise GcpSupervisorError("OPERATOR_CONFIRMATION_REQUIRED")
    if _OPERATOR_ID_RE.fullmatch(operator_identity) is None:
        raise GcpSupervisorError("OPERATOR_IDENTITY_INVALID")
    try:
        issued_text = _timestamp(issued_at)
        expires_text = _timestamp(expires_at)
    except ValueError:
        raise GcpSupervisorError("APPROVAL_TIME_INVALID") from None
    request = preflight.request
    if request.accelerator_count != 1:
        raise GcpSupervisorError("APPROVAL_ACCELERATOR_PROFILE_INVALID")
    try:
        activation_deadline = validate_gcp_v2_activation_deadline(
            activation_deadline,
            preflight=preflight,
            startup_projection=startup_projection,
            rate_basis_digest=gcp_read_only_quote_basis_digest(quote_basis),
            cost_guard_digest=gcp_cost_cleanup_guard_digest(cost_guard),
        )
        startup_projection = validate_gcp_v2_startup_projection(
            startup_projection,
            request=request,
            execution_payload_digest=activation_deadline.execution_payload_digest,
        )
        validate_gcp_cost_cleanup_guard(
            cost_guard,
            preflight=preflight,
            quote_basis=quote_basis,
            now=issued_at,
        )
    except GcpExecutionError as error:
        raise GcpSupervisorError(str(error)) from None
    if (
        issued_text != activation_deadline.issued_at
        or expires_text != activation_deadline.authorization_expires_at
    ):
        raise GcpSupervisorError("APPROVAL_ACTIVATION_TIME_MISMATCH")
    payload = GcpExecutionApprovalPayload(
        schema_version=GCP_EXECUTION_APPROVAL_SCHEMA_VERSION,
        approval_kind="exact_operator_execution_approval",
        operator_identity=operator_identity,
        operator_confirmation=GCP_SUPERVISOR_CONFIRMATION,
        provider="gcp-compute-engine",
        plan_id=preflight.arm.plan_id,
        plan_sha256=preflight.arm.plan_sha256,
        arm_id=preflight.arm.arm_id,
        request_digest=gcp_execution_request_digest(request),
        startup_projection_digest=gcp_v2_startup_projection_digest(
            startup_projection
        ),
        execution_payload_digest=startup_projection.execution_payload_digest,
        quote_digest=gcp_cost_quote_digest(preflight.quote),
        capacity_digest=gcp_capacity_digest(preflight.capacity),
        rate_basis_digest=gcp_read_only_quote_basis_digest(quote_basis),
        cost_guard_digest=gcp_cost_cleanup_guard_digest(cost_guard),
        activation_deadline_digest=gcp_v2_activation_deadline_digest(
            activation_deadline
        ),
        quote_currency=preflight.quote.currency,
        project_id=request.project_id,
        region=request.region,
        zone=request.zone,
        machine_type=request.machine_type,
        accelerator_model=request.accelerator_model,
        accelerator_provider_type=request.accelerator_provider_type,
        # The v0.2 approval is deliberately only for the single A100 profile;
        # do not widen its Literal contract if a future request shape changes.
        accelerator_count=1,
        accelerator_attachment_mode=request.accelerator_attachment_mode,
        boot_image_name=request.boot_image.image_name,
        boot_image_provider_id=request.boot_image.provider_image_id,
        boot_image_digest=request.boot_image.digest,
        runner_image_digest=request.runner_image.digest,
        serving_runtime_image_digest=request.serving_runtime_image.digest,
        max_runtime_seconds=request.provider_max_runtime_seconds,
        controller_deadline_at=preflight.arm.expires_at,
        authorization_expires_at=activation_deadline.authorization_expires_at,
        setup_deadline_at=activation_deadline.setup_deadline_at,
        setup_margin_seconds=activation_deadline.setup_margin_seconds,
        provider_runtime_seconds=activation_deadline.provider_runtime_seconds,
        watchdog_cleanup_horizon_seconds=(
            activation_deadline.watchdog_cleanup_horizon_seconds
        ),
        watchdog_deadline_at=activation_deadline.watchdog_deadline_at,
        hard_cost_ceiling=preflight.arm.cost_ceiling,
        hard_ceiling_microusd=gcp_hard_usd_ceiling_microusd(
            preflight.arm.cost_ceiling
        ),
        estimated_max_microusd=preflight.quote.worst_case_microusd,
        issued_at=issued_text,
        expires_at=expires_text,
    )
    return GcpExecutionApproval(
        **_model_value(payload), approval_id=gcp_execution_approval_id(payload)
    )


def validate_gcp_execution_approval(
    approval: GcpExecutionApproval,
    *,
    preflight: GcpExecutionPreflight,
    quote_basis: GcpReadOnlyQuoteBasis,
    cost_guard: GcpCostCleanupGuard,
    activation_deadline: GcpV2ActivationDeadline,
    startup_projection: GcpV2StartupProjection,
    now: datetime,
    enforce_freshness: bool = True,
    enforce_controller_deadline: bool = True,
) -> GcpExecutionApproval:
    """Fail closed unless one approval binds every current preflight fact."""

    approval = _strict_approval(approval)
    if enforce_freshness:
        try:
            now_text = _timestamp(now)
        except ValueError:
            raise GcpSupervisorError("APPROVAL_TIME_INVALID") from None
        now_value = _parse_timestamp(now_text)
        if not (
            _parse_timestamp(approval.issued_at)
            <= now_value
            < _parse_timestamp(approval.expires_at)
        ):
            raise GcpSupervisorError("APPROVAL_EXPIRED")
    try:
        activation_deadline = validate_gcp_v2_activation_deadline(
            activation_deadline,
            preflight=preflight,
            startup_projection=startup_projection,
            rate_basis_digest=gcp_read_only_quote_basis_digest(quote_basis),
            cost_guard_digest=gcp_cost_cleanup_guard_digest(cost_guard),
        )
        startup_projection = validate_gcp_v2_startup_projection(
            startup_projection,
            request=preflight.request,
            execution_payload_digest=activation_deadline.execution_payload_digest,
        )
    except GcpExecutionError as error:
        raise GcpSupervisorError(str(error)) from None
    request = preflight.request
    expected = {
        "provider": "gcp-compute-engine",
        "plan_id": preflight.arm.plan_id,
        "plan_sha256": preflight.arm.plan_sha256,
        "arm_id": preflight.arm.arm_id,
        "request_digest": gcp_execution_request_digest(request),
        "startup_projection_digest": gcp_v2_startup_projection_digest(
            startup_projection
        ),
        "execution_payload_digest": startup_projection.execution_payload_digest,
        "quote_digest": gcp_cost_quote_digest(preflight.quote),
        "capacity_digest": gcp_capacity_digest(preflight.capacity),
        "rate_basis_digest": gcp_read_only_quote_basis_digest(quote_basis),
        "cost_guard_digest": gcp_cost_cleanup_guard_digest(cost_guard),
        "activation_deadline_digest": gcp_v2_activation_deadline_digest(
            activation_deadline
        ),
        "quote_currency": preflight.quote.currency,
        "project_id": request.project_id,
        "region": request.region,
        "zone": request.zone,
        "machine_type": request.machine_type,
        "accelerator_model": request.accelerator_model,
        "accelerator_provider_type": request.accelerator_provider_type,
        "accelerator_count": request.accelerator_count,
        "accelerator_attachment_mode": request.accelerator_attachment_mode,
        "boot_image_name": request.boot_image.image_name,
        "boot_image_provider_id": request.boot_image.provider_image_id,
        "boot_image_digest": request.boot_image.digest,
        "runner_image_digest": request.runner_image.digest,
        "serving_runtime_image_digest": request.serving_runtime_image.digest,
        "max_runtime_seconds": request.provider_max_runtime_seconds,
        "controller_deadline_at": preflight.arm.expires_at,
        "authorization_expires_at": activation_deadline.authorization_expires_at,
        "setup_deadline_at": activation_deadline.setup_deadline_at,
        "setup_margin_seconds": activation_deadline.setup_margin_seconds,
        "provider_runtime_seconds": activation_deadline.provider_runtime_seconds,
        "watchdog_cleanup_horizon_seconds": (
            activation_deadline.watchdog_cleanup_horizon_seconds
        ),
        "watchdog_deadline_at": activation_deadline.watchdog_deadline_at,
        "hard_cost_ceiling": preflight.arm.cost_ceiling,
        "hard_ceiling_microusd": gcp_hard_usd_ceiling_microusd(
            preflight.arm.cost_ceiling
        ),
        "estimated_max_microusd": preflight.quote.worst_case_microusd,
    }
    if any(getattr(approval, key) != value for key, value in expected.items()):
        raise GcpSupervisorError("APPROVAL_BINDING_MISMATCH")
    try:
        validate_gcp_cost_cleanup_guard(
            cost_guard,
            preflight=preflight,
            quote_basis=quote_basis,
            now=now,
            enforce_freshness=enforce_freshness,
            enforce_controller_deadline=enforce_controller_deadline,
        )
    except GcpExecutionError as error:
        raise GcpSupervisorError(str(error)) from None
    return approval


def gcp_supervisor_binding(
    approval: GcpExecutionApproval, record: GcpLeaseRecord
) -> GcpSupervisorBinding:
    """Bind a sidecar journal to the exact core lease, not a hostname guess."""

    approval = _strict_approval(approval)
    if approval.arm_id != record.arm_id:
        raise GcpSupervisorError("APPROVAL_ARM_MISMATCH")
    if (
        approval.plan_id != record.plan_id
        or approval.request_digest != record.request_digest
        or approval.project_id != record.project_id
        or approval.region != record.region
        or approval.zone != record.zone
    ):
        raise GcpSupervisorError("APPROVAL_LEASE_MISMATCH")
    return GcpSupervisorBinding(
        schema_version=GCP_SUPERVISOR_BINDING_SCHEMA_VERSION,
        approval_id=approval.approval_id,
        approval_digest=gcp_execution_approval_digest(approval),
        activation_deadline_digest=approval.activation_deadline_digest,
        controller_id=record.controller_id,
        arm_id=record.arm_id,
        plan_id=record.plan_id,
        request_digest=record.request_digest,
        startup_projection_digest=approval.startup_projection_digest,
        execution_payload_digest=approval.execution_payload_digest,
        project_id=record.project_id,
        region=record.region,
        zone=record.zone,
        instance_name=record.instance_name,
        labels=record.labels,
        setup_deadline_at=approval.setup_deadline_at,
        provider_runtime_seconds=approval.provider_runtime_seconds,
        watchdog_cleanup_horizon_seconds=(
            approval.watchdog_cleanup_horizon_seconds
        ),
        max_cleanup_attempts=record.max_cleanup_attempts,
        cleanup_timeout_seconds=record.cleanup_timeout_seconds,
        controller_deadline_at=approval.controller_deadline_at,
        watchdog_deadline_at=approval.watchdog_deadline_at,
    )


def canonical_gcp_exact_orphan_report_payload_bytes(
    report: GcpExactOrphanReport,
) -> bytes:
    value = _model_value(report)
    value.pop("report_id", None)
    return canonical_json_bytes(value)


def gcp_exact_orphan_report_id(report: GcpExactOrphanReport) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXACT_ORPHAN_REPORT,
        canonical_gcp_exact_orphan_report_payload_bytes(report),
    )


def issue_gcp_exact_orphan_report(
    binding: GcpSupervisorBinding,
    *,
    reason_code: str,
    now: datetime,
) -> GcpExactOrphanReport:
    """Record only the exact, immutable identity that needs recovery."""

    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,47}", reason_code) is None:
        reason_code = "CLEANUP_UNCONFIRMED"
    payload: dict[str, Any] = {
        "schema_version": GCP_EXACT_ORPHAN_REPORT_SCHEMA_VERSION,
        "approval_digest": binding.approval_digest,
        "request_digest": binding.request_digest,
        "controller_id": binding.controller_id,
        "project_id": binding.project_id,
        "region": binding.region,
        "zone": binding.zone,
        "instance_name": binding.instance_name,
        "labels": _model_value(binding.labels),
        "cleanup_state": "unconfirmed_exact_owned_resource",
        "reason_code": reason_code,
        "reported_at": _timestamp(now),
    }
    payload["report_id"] = digest_bytes(
        DigestDomain.GCP_EXACT_ORPHAN_REPORT, canonical_json_bytes(payload)
    )
    return GcpExactOrphanReport.model_validate_json(canonical_json_bytes(payload))


def canonical_gcp_kill_switch_payload_bytes(
    record: GcpKillSwitchRecord,
) -> bytes:
    """Return the exact local kill-marker payload excluding its identity."""

    value = _model_value(record)
    value.pop("kill_id", None)
    return canonical_json_bytes(value)


def gcp_kill_switch_id(record: GcpKillSwitchRecord) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_EXECUTION_KILL_SWITCH,
        canonical_gcp_kill_switch_payload_bytes(record),
    )


def canonical_gcp_kill_switch_bytes(record: GcpKillSwitchRecord) -> bytes:
    return canonical_json_bytes(_model_value(record))


def parse_gcp_kill_switch_json(payload: str | bytes) -> GcpKillSwitchRecord:
    """Parse one strict exact-bound local kill marker."""

    try:
        _preflight_json(payload, kind="GCP kill switch")
        return GcpKillSwitchRecord.model_validate_json(payload)
    except (ValidationError, ValueError):
        raise GcpSupervisorError("KILL_SWITCH_INVALID") from None


def issue_gcp_kill_switch_record(
    binding: GcpSupervisorBinding,
    *,
    operator_identity: str,
    now: datetime,
    confirmation: str,
) -> GcpKillSwitchRecord:
    """Issue an exact local marker without touching a provider boundary."""

    if confirmation != GCP_KILL_SWITCH_CONFIRMATION:
        raise GcpSupervisorError("KILL_CONFIRMATION_REQUIRED")
    if _OPERATOR_ID_RE.fullmatch(operator_identity) is None:
        raise GcpSupervisorError("KILL_OPERATOR_IDENTITY_INVALID")
    try:
        issued_at = _timestamp(now)
    except ValueError:
        raise GcpSupervisorError("KILL_TIME_INVALID") from None
    payload: dict[str, Any] = {
        "schema_version": GCP_KILL_SWITCH_SCHEMA_VERSION,
        "action": "KILL",
        "operator_identity": operator_identity,
        "operator_confirmation": GCP_KILL_SWITCH_CONFIRMATION,
        "approval_digest": binding.approval_digest,
        "request_digest": binding.request_digest,
        "controller_id": binding.controller_id,
        "issued_at": issued_at,
    }
    payload["kill_id"] = digest_bytes(
        DigestDomain.GCP_EXECUTION_KILL_SWITCH, canonical_json_bytes(payload)
    )
    return GcpKillSwitchRecord.model_validate_json(canonical_json_bytes(payload))


def _event(
    *,
    sequence: int,
    state: GcpSupervisorState,
    binding: GcpSupervisorBinding,
    occurred_at: str,
    previous_event_digest: Sha256Digest | None,
    watchdog_receipt_digest: Sha256Digest | None,
    orphan_report: GcpExactOrphanReport | None,
    error_code: str | None,
) -> GcpSupervisorJournalEvent:
    payload: dict[str, Any] = {
        "schema_version": GCP_SUPERVISOR_EVENT_SCHEMA_VERSION,
        "sequence": sequence,
        "state": state,
        "binding": _model_value(binding),
        "occurred_at": occurred_at,
        "previous_event_digest": previous_event_digest,
        "watchdog_receipt_digest": watchdog_receipt_digest,
        "orphan_report": (
            _model_value(orphan_report) if orphan_report is not None else None
        ),
        "error_code": error_code,
    }
    payload["event_digest"] = digest_bytes(
        DigestDomain.GCP_EXECUTION_SUPERVISOR, canonical_json_bytes(payload)
    )
    return GcpSupervisorJournalEvent.model_validate_json(canonical_json_bytes(payload))


class GcpSupervisorJournal:
    """A bounded fsync-backed v0.2 sidecar event journal.

    The core v1 lease remains its own authoritative journal. This independent
    sidecar only records approval/watchdog supervision, so no frozen v1 shape
    changes and no broad resource discovery are required.
    """

    def __init__(
        self,
        root: Path,
        *,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._crash_hook = crash_hook

    def _crash_point(self, point: str) -> None:
        if self._crash_hook is not None:
            self._crash_hook(point)

    def _checked_root(self) -> Path:
        if not self.root.is_absolute():
            raise GcpSupervisorError("SUPERVISOR_JOURNAL_PATH_INVALID")
        try:
            metadata = self.root.lstat()
        except OSError:
            raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNAVAILABLE") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mode & 0o022
        ):
            raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNSAFE")
        return self.root

    @staticmethod
    def _path_for(root: Path, controller_id: str) -> Path:
        if _CONTROLLER_RE.fullmatch(controller_id) is None:
            raise GcpSupervisorError("SUPERVISOR_CONTROLLER_INVALID")
        return root / f"{controller_id}.safety-v2.events.jsonl"

    @contextmanager
    def _exclusive(self) -> Iterator[Path]:
        root = self._checked_root()
        descriptor: int | None = None
        try:
            descriptor = os.open(
                root / ".safety-v2.lock",
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except (ImportError, OSError):
                raise GcpSupervisorError("SUPERVISOR_LOCK_UNAVAILABLE") from None
            with self._lock:
                yield root
        finally:
            if descriptor is not None:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                os.close(descriptor)

    def _fsync_directory(self) -> None:
        descriptor = os.open(
            self._checked_root(), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_events_locked(self, path: Path) -> tuple[GcpSupervisorJournalEvent, ...]:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > GCP_SUPERVISOR_MAX_EVENT_BYTES
                or metadata.st_mode & 0o022
            ):
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNSAFE")
            raw = os.read(descriptor, GCP_SUPERVISOR_MAX_EVENT_BYTES + 1)
            final = os.fstat(descriptor)
            if (
                len(raw) > GCP_SUPERVISOR_MAX_EVENT_BYTES
                or final.st_ino != metadata.st_ino
                or final.st_size != len(raw)
            ):
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_CHANGED")
            if not raw.endswith(b"\n"):
                newline = raw.rfind(b"\n")
                raw = raw[: newline + 1] if newline >= 0 else b""
            lines = raw.splitlines()
            if not lines or len(lines) > GCP_SUPERVISOR_MAX_EVENTS:
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_INVALID")
            previous: Sha256Digest | None = None
            binding: GcpSupervisorBinding | None = None
            events: list[GcpSupervisorJournalEvent] = []
            for sequence, line in enumerate(lines):
                _preflight_json(line, kind="GCP supervisor event")
                event = GcpSupervisorJournalEvent.model_validate_json(line)
                if canonical_json_bytes(_model_value(event)) != line:
                    raise GcpSupervisorError("SUPERVISOR_JOURNAL_NONCANONICAL")
                if (
                    event.sequence != sequence
                    or event.previous_event_digest != previous
                ):
                    raise GcpSupervisorError("SUPERVISOR_JOURNAL_CHAIN_INVALID")
                if binding is not None and event.binding != binding:
                    raise GcpSupervisorError("SUPERVISOR_BINDING_CHANGED")
                previous = event.event_digest
                binding = event.binding
                events.append(event)
            self._validate_transitions(events)
            return tuple(events)
        except GcpSupervisorError:
            raise
        except (OSError, ValidationError, ValueError):
            raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNAVAILABLE") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _repair_partial_tail_locked(self, path: Path) -> bool:
        """Repair one incomplete tail and report whether an event history remains.

        A process can die after creating its exact controller path but before a
        first event line is complete.  That prefix establishes no durable
        lease/authority, so it is removed under the sidecar lock and no-follow
        checks.  Once a complete event exists, only a final incomplete tail is
        truncated; completed malformed or tampered history still fails closed.
        """

        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > GCP_SUPERVISOR_MAX_EVENT_BYTES
                or metadata.st_mode & 0o022
            ):
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNSAFE")
            raw = os.read(descriptor, GCP_SUPERVISOR_MAX_EVENT_BYTES + 1)
            if len(raw) > GCP_SUPERVISOR_MAX_EVENT_BYTES:
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNSAFE")
            if not raw.endswith(b"\n"):
                newline = raw.rfind(b"\n")
                if newline < 0:
                    os.ftruncate(descriptor, 0)
                    os.fsync(descriptor)
                    os.unlink(path)
                    self._fsync_directory()
                    return False
                os.ftruncate(descriptor, newline + 1)
                os.fsync(descriptor)
                self._fsync_directory()
            return True
        except FileNotFoundError:
            return False
        except GcpSupervisorError:
            raise
        except OSError:
            raise GcpSupervisorError("SUPERVISOR_JOURNAL_REPAIR_FAILED") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _validate_transitions(events: list[GcpSupervisorJournalEvent]) -> None:
        allowed: dict[GcpSupervisorState, set[GcpSupervisorState]] = {
            "PREPARED": {
                "ARM_CONSUMED",
                "CLEANUP_PENDING",
                "CLEANUP_CONFIRMED",
                "BLOCKED",
            },
            "ARM_CONSUMED": {
                "WATCHDOG_READY",
                "CLEANUP_PENDING",
                "CLEANUP_CONFIRMED",
                "KILLED",
                "BLOCKED",
            },
            "WATCHDOG_READY": {
                "CREATE_INTENT",
                "CLEANUP_PENDING",
                "CLEANUP_CONFIRMED",
                "KILLED",
                "BLOCKED",
            },
            "CREATE_INTENT": {
                "WORKING",
                "CLEANUP_PENDING",
                "KILLED",
                "ORPHANED",
                "BLOCKED",
            },
            "WORKING": {"CLEANUP_PENDING", "KILLED", "ORPHANED", "BLOCKED"},
            "KILLED": {
                "CLEANUP_PENDING",
                "CLEANUP_CONFIRMED",
                "ORPHANED",
                "BLOCKED",
            },
            "CLEANUP_PENDING": {
                "CLEANUP_PENDING",
                "CLEANUP_CONFIRMED",
                "ORPHANED",
                "BLOCKED",
            },
            "ORPHANED": {"CLEANUP_PENDING", "CLEANUP_CONFIRMED", "BLOCKED"},
            "BLOCKED": {"CLEANUP_PENDING", "CLEANUP_CONFIRMED", "ORPHANED"},
            "CLEANUP_CONFIRMED": {"CLEANUP_CONFIRMED"},
        }
        for previous, current in pairwise(events):
            if current.state not in allowed[previous.state]:
                raise GcpSupervisorError("SUPERVISOR_TRANSITION_INVALID")
            if _parse_timestamp(current.occurred_at) < _parse_timestamp(
                previous.occurred_at
            ):
                raise GcpSupervisorError("SUPERVISOR_TIMESTAMP_REGRESSED")

    def _scan_locked(self, root: Path) -> tuple[GcpSupervisorJournalEvent, ...]:
        try:
            entries = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError:
            raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNAVAILABLE") from None
        latest: list[GcpSupervisorJournalEvent] = []
        for entry in entries:
            if entry.name == ".safety-v2.lock":
                continue
            match = _EVENT_PATH_RE.fullmatch(entry.name)
            if match is None:
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNEXPECTED_ENTRY")
            if stat.S_ISLNK(entry.lstat().st_mode):
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNSAFE")
            if self._repair_partial_tail_locked(entry):
                latest.append(self._read_events_locked(entry)[-1])
        return tuple(latest)

    def reserve(self, binding: GcpSupervisorBinding, *, now: datetime) -> None:
        """Persist exact approval intent and block every unresolved next run."""

        occurred_at = _timestamp(now)
        with self._exclusive() as root:
            for existing in self._scan_locked(root):
                if existing.state != "CLEANUP_CONFIRMED":
                    raise GcpSupervisorError("SUPERVISOR_UNRESOLVED_LEASE")
            path = self._path_for(root, binding.controller_id)
            event = _event(
                sequence=0,
                state="PREPARED",
                binding=binding,
                occurred_at=occurred_at,
                previous_event_digest=None,
                watchdog_receipt_digest=None,
                orphan_report=None,
                error_code=None,
            )
            raw = canonical_json_bytes(_model_value(event)) + b"\n"
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                try:
                    _write_all(descriptor, raw)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._crash_point("reserve_after_event_fsync")
                self._fsync_directory()
                self._crash_point("reserve_after_directory_fsync")
            except FileExistsError:
                raise GcpSupervisorError("SUPERVISOR_LEASE_EXISTS") from None
            except OSError:
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_RESERVE_FAILED") from None

    def _reserve_exact_for_reconciliation(
        self, binding: GcpSupervisorBinding, *, now: datetime
    ) -> None:
        """Reserve only one known core-terminal lease, without global scanning.

        A core journal that already proves exact absence must be able to settle
        its matching sidecar even when a different lease remains blocked.  This
        method does not authorize a new run and is never used by ``reserve``.
        """

        occurred_at = _timestamp(now)
        with self._exclusive() as root:
            path = self._path_for(root, binding.controller_id)
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                metadata = None
            except OSError:
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNAVAILABLE") from None
            if metadata is not None:
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNSAFE")
                if self._repair_partial_tail_locked(path):
                    return
            event = _event(
                sequence=0,
                state="PREPARED",
                binding=binding,
                occurred_at=occurred_at,
                previous_event_digest=None,
                watchdog_receipt_digest=None,
                orphan_report=None,
                error_code="CORE_TERMINAL_RECONCILIATION",
            )
            raw = canonical_json_bytes(_model_value(event)) + b"\n"
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                try:
                    _write_all(descriptor, raw)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._fsync_directory()
            except FileExistsError:
                return
            except OSError:
                raise GcpSupervisorError(
                    "SUPERVISOR_JOURNAL_RESERVE_FAILED"
                ) from None

    def reconcile_core_terminal(
        self,
        binding: GcpSupervisorBinding,
        *,
        now: datetime,
        reason_code: str,
    ) -> None:
        """Idempotently settle an exact sidecar after core absence proof.

        This is deliberately journal-only: its caller has already durably
        recorded core ``CLEANUP_CONFIRMED``.  It makes a crash between the
        core and sidecar writes recoverable without a provider read or a broad
        inventory inference.
        """

        self._reserve_exact_for_reconciliation(binding, now=now)
        event = self.load(binding.controller_id)
        if event.binding != binding:
            raise GcpSupervisorError("SUPERVISOR_BINDING_MISMATCH")
        if event.state == "CLEANUP_CONFIRMED":
            return
        if event.state != "CLEANUP_PENDING":
            self.advance(
                binding,
                state="CLEANUP_PENDING",
                now=now,
                error_code=reason_code,
            )
            event = self.load(binding.controller_id)
        if event.state != "CLEANUP_CONFIRMED":
            self.advance(
                binding,
                state="CLEANUP_CONFIRMED",
                now=now,
                error_code=reason_code,
            )

    def load(self, controller_id: str) -> GcpSupervisorJournalEvent:
        with self._exclusive() as root:
            path = self._path_for(root, controller_id)
            if not self._repair_partial_tail_locked(path):
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_MISSING")
            return self._read_events_locked(path)[-1]

    def advance(
        self,
        binding: GcpSupervisorBinding,
        *,
        state: GcpSupervisorState,
        now: datetime,
        watchdog_receipt_digest: Sha256Digest | None = None,
        orphan_report: GcpExactOrphanReport | None = None,
        error_code: str | None = None,
    ) -> GcpSupervisorJournalEvent:
        """Append exactly one validated deterministic state transition."""

        occurred_at = _timestamp(now)
        with self._exclusive() as root:
            path = self._path_for(root, binding.controller_id)
            if not self._repair_partial_tail_locked(path):
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_MISSING")
            events = self._read_events_locked(path)
            previous = events[-1]
            if previous.binding != binding:
                raise GcpSupervisorError("SUPERVISOR_BINDING_MISMATCH")
            receipt_digest = watchdog_receipt_digest
            if receipt_digest is None:
                receipt_digest = previous.watchdog_receipt_digest
            event = _event(
                sequence=previous.sequence + 1,
                state=state,
                binding=binding,
                occurred_at=occurred_at,
                previous_event_digest=previous.event_digest,
                watchdog_receipt_digest=receipt_digest,
                orphan_report=orphan_report,
                error_code=error_code,
            )
            candidate = [*events, event]
            self._validate_transitions(candidate)
            raw = canonical_json_bytes(_model_value(event)) + b"\n"
            try:
                size = path.stat().st_size
            except OSError:
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNAVAILABLE") from None
            if (
                len(candidate) > GCP_SUPERVISOR_MAX_EVENTS
                or size + len(raw) > GCP_SUPERVISOR_MAX_EVENT_BYTES
            ):
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_BOUND_EXCEEDED")
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY
                    | os.O_APPEND
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
                try:
                    _write_all(descriptor, raw)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._crash_point("advance_after_event_fsync")
                self._fsync_directory()
                self._crash_point("advance_after_directory_fsync")
            except OSError:
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_APPEND_FAILED") from None
            return event


class GcpWatchdog(Protocol):
    """An independent future cleanup watchdog, injected after approval only."""

    def arm(
        self,
        binding: GcpSupervisorBinding,
        *,
        approval: GcpExecutionApproval,
        now: datetime,
    ) -> GcpWatchdogReceipt: ...

    def activate(
        self,
        binding: GcpSupervisorBinding,
        *,
        approval: GcpExecutionApproval,
        capability: GcpV2MutationCapability,
        now: datetime,
    ) -> GcpWatchdogActivationReceipt: ...


class GcpWatchdogCleanupExecutor(Protocol):
    """Injected exact cleanup executor; no discovery or create authority."""

    def cleanup_exact(
        self,
        event: GcpFileWatchdogEvent,
        *,
        timeout_seconds: int,
    ) -> GcpWatchdogCleanupResult: ...

    def cleanup_prebind(
        self,
        event: GcpFileWatchdogEvent,
        *,
        timeout_seconds: int,
    ) -> GcpWatchdogPrebindCleanupResult: ...


class GcpV2ExactWatchdogCleanupExecutor:
    """Concrete cleanup-only executor for one exact v2 lease.

    This deliberately composes the existing exact instance transport boundary
    with the separately journaled named-disk coordinator.  It never exposes or
    invokes ``insert`` and it validates the persisted watchdog event before
    every provider-facing read/delete/reconcile.  Callers must supply an
    already-bound cleanup transport; this class has no SDK, ADC, factory, or
    credential path.
    """

    def __init__(
        self,
        *,
        record: GcpLeaseRecord,
        supervisor_binding: GcpSupervisorBinding,
        transport: GcpComputeTransport,
        disk_cleanup: GcpV2ExactDiskCleanupCoordinator | None,
        now_fn: Callable[[], datetime],
    ) -> None:
        # This executor is a recovery/cleanup surface, never a create
        # surface.  Do not accept an arbitrary transport just because it
        # happens to satisfy the structural protocol.
        try:
            from inferdrome.deployment.gcp_compute_transport import (
                is_gcp_v2_cleanup_authorization_bound_transport,
                is_gcp_v2_watchdog_cleanup_bound_transport,
            )
        except ImportError:
            raise GcpSupervisorError("WATCHDOG_EXECUTOR_TRANSPORT_INVALID") from None
        cleanup_authorized = is_gcp_v2_cleanup_authorization_bound_transport(
            transport
        )
        watchdog_authorized = is_gcp_v2_watchdog_cleanup_bound_transport(transport)
        if not cleanup_authorized and not watchdog_authorized:
            raise GcpSupervisorError("WATCHDOG_EXECUTOR_TRANSPORT_INVALID")
        self._record = record
        self._binding = supervisor_binding
        self._transport = transport
        self._disk_cleanup = disk_cleanup
        self._now_fn = now_fn
        if cleanup_authorized:
            authorization = getattr(transport, "authorization", None)
            if (
                type(disk_cleanup) is not GcpV2ExactDiskCleanupCoordinator
                or authorization is None
                or getattr(authorization, "approval_digest", None)
                != supervisor_binding.approval_digest
                or getattr(authorization, "lease_intent_anchor_digest", None)
                != record.intent_anchor_digest
                or getattr(authorization, "arm_id", None) != record.arm_id
                or getattr(authorization, "plan_id", None) != record.plan_id
                or getattr(authorization, "create_authority", None) is not False
                or getattr(authorization, "allowed_actions", None)
                != (
                    "reconcile_operation",
                    "get_exact_instance",
                    "list_exact_label_inventory",
                    "delete_exact_instance",
                    "get_exact_disk",
                    "delete_exact_disk",
                    "confirm_absence",
                )
                or getattr(authorization, "request_digest", None)
                != record.request_digest
                or getattr(authorization, "startup_projection_digest", None)
                != supervisor_binding.startup_projection_digest
                or getattr(authorization, "execution_payload_digest", None)
                != supervisor_binding.execution_payload_digest
                or getattr(authorization, "project_id", None) != record.project_id
                or getattr(authorization, "region", None) != record.region
                or getattr(authorization, "zone", None) != record.zone
                or getattr(authorization, "instance_name", None) != record.instance_name
                or getattr(authorization, "labels", None) != record.labels
                or getattr(authorization, "disk_cleanup_binding", None)
                != disk_cleanup.binding
            ):
                raise GcpSupervisorError("WATCHDOG_EXECUTOR_TRANSPORT_MISMATCH")
        else:
            capability = getattr(transport, "capability", None)
            if (
                capability is None
                or getattr(capability, "request_digest", None) != record.request_digest
                or getattr(capability, "startup_projection_digest", None)
                != supervisor_binding.startup_projection_digest
                or getattr(capability, "execution_payload_digest", None)
                != supervisor_binding.execution_payload_digest
                or getattr(capability, "project_id", None) != record.project_id
                or getattr(capability, "region", None) != record.region
                or getattr(capability, "zone", None) != record.zone
                or getattr(capability, "instance_name", None) != record.instance_name
                or getattr(capability, "labels", None) != record.labels
            ):
                raise GcpSupervisorError("WATCHDOG_EXECUTOR_TRANSPORT_MISMATCH")

    def _validate_event(self, event: GcpFileWatchdogEvent) -> None:
        record = self._record
        if (
            event.binding != self._binding
            or event.binding.request_digest != record.request_digest
            or event.binding.project_id != record.project_id
            or event.binding.region != record.region
            or event.binding.zone != record.zone
            or event.binding.instance_name != record.instance_name
            or event.binding.labels != record.labels
        ):
            raise GcpSupervisorError("WATCHDOG_EXECUTOR_BINDING_MISMATCH")

    @property
    def configuration_digest(self) -> Sha256Digest:
        """Return the durable local identity of this cleanup-only executor.

        This is deliberately a content-addressed configuration binding, not a
        signature and not a credential.  A restarted file watchdog compares
        it with the digest persisted before activation, so an arbitrary new
        executor cannot inherit a ready task and reach a provider boundary.
        """

        authorization = getattr(self._transport, "authorization", None)
        capability = getattr(self._transport, "capability", None)
        authority_id = getattr(authorization, "authorization_id", None) or getattr(
            capability, "capability_id", None
        )
        if authority_id is None:
            raise GcpSupervisorError("WATCHDOG_EXECUTOR_TRANSPORT_INVALID")
        payload = {
            "kind": "gcp_v2_exact_cleanup_executor",
            "lease_intent_anchor_digest": self._record.intent_anchor_digest,
            "request_digest": self._record.request_digest,
            "controller_id": self._record.controller_id,
            "project_id": self._record.project_id,
            "region": self._record.region,
            "zone": self._record.zone,
            "instance_name": self._record.instance_name,
            "labels": _model_value(self._record.labels),
            "supervisor_binding": _model_value(self._binding),
            "disk_cleanup_binding_id": (
                self._disk_cleanup.binding.binding_id
                if self._disk_cleanup is not None
                else None
            ),
            "disk_cleanup_configuration_digest": (
                self._disk_cleanup.configuration_digest
                if self._disk_cleanup is not None
                else None
            ),
            "cleanup_authority_id": authority_id,
        }
        return digest_bytes(
            DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(payload)
        )

    def matches_watchdog_binding(self, binding: GcpSupervisorBinding) -> bool:
        """Check the exact persisted controller binding without I/O."""

        try:
            return (
                binding == self._binding
                and self.configuration_digest.startswith("sha256:")
            )
        except (GcpSupervisorError, ValueError, TypeError):
            return False

    def _validate_exact_instance_and_inventory(
        self,
        observation: object,
        inventory: object,
    ) -> tuple[GcpInstanceObservation, GcpOwnedResourceInventory]:
        """Canonical-parse provider reads before an exact delete can occur."""

        try:
            parsed_observation = GcpInstanceObservation.model_validate(observation)
            parsed_inventory = GcpOwnedResourceInventory.model_validate(inventory)
        except (ValidationError, ValueError, TypeError):
            raise GcpSupervisorError("WATCHDOG_INSTANCE_INVENTORY_INVALID") from None
        request = self._record.request
        if (
            parsed_observation.instance_name != request.instance_name
            or parsed_observation.project_id != request.project_id
            or parsed_observation.zone != request.zone
            or parsed_inventory.request_digest != self._record.request_digest
            or parsed_inventory.project_id != request.project_id
            or parsed_inventory.region != request.region
            or parsed_inventory.zone != request.zone
            or parsed_inventory.labels != request.labels
            or parsed_inventory.pagination_complete is not True
        ):
            raise GcpSupervisorError("WATCHDOG_INSTANCE_INVENTORY_MISMATCH")
        if parsed_observation.state == "NOT_FOUND":
            if parsed_inventory.instances:
                raise GcpSupervisorError("WATCHDOG_INSTANCE_INVENTORY_AMBIGUOUS")
            return parsed_observation, parsed_inventory
        if (
            parsed_observation.labels != request.labels
            or parsed_observation.machine_type != request.machine_type
            or parsed_observation.accelerator_model != request.accelerator_model
            or parsed_observation.accelerator_provider_type
            != request.accelerator_provider_type
            or parsed_observation.accelerator_count != request.accelerator_count
            or parsed_inventory.instances != (parsed_observation,)
        ):
            raise GcpSupervisorError("WATCHDOG_INSTANCE_INVENTORY_AMBIGUOUS")
        return parsed_observation, parsed_inventory

    def _exact_absence(
        self, event: GcpFileWatchdogEvent, *, timeout_seconds: int
    ) -> GcpWatchdogPrebindCleanupResult:
        self._validate_event(event)
        request = self._record.request
        try:
            observation, inventory = self._validate_exact_instance_and_inventory(
                self._transport.get_instance(
                request, timeout_seconds=timeout_seconds
                ),
                self._transport.list_owned_complete(
                request, timeout_seconds=timeout_seconds
                ),
            )
            if observation.state != "NOT_FOUND" or inventory.instances:
                if observation.state not in {"RUNNING", "TERMINATED"}:
                    raise GcpSupervisorError("WATCHDOG_INSTANCE_STATE_UNSUPPORTED")
                operation = self._transport.delete(
                    request,
                    timeout_seconds=timeout_seconds,
                    request_id=request.delete_request_id,
                )
                parsed_operation = GcpOperationHandle.model_validate(
                    _model_value(operation)
                )
                if (
                    parsed_operation.operation_id is None
                    or parsed_operation.operation_name is None
                    or parsed_operation.operation_kind != "delete"
                    or parsed_operation.instance_name != request.instance_name
                    or parsed_operation.project_id != request.project_id
                    or parsed_operation.zone != request.zone
                ):
                    raise GcpSupervisorError("WATCHDOG_DELETE_OPERATION_INVALID")
                result = GcpOperationResult.model_validate(
                    _model_value(
                        self._transport.wait_operation(
                            parsed_operation, timeout_seconds=timeout_seconds
                        )
                    )
                )
                if (
                    result.operation_id != parsed_operation.operation_id
                    or result.operation_name != parsed_operation.operation_name
                    or result.status != "DONE"
                ):
                    raise GcpSupervisorError("WATCHDOG_DELETE_UNCONFIRMED")
                observation, inventory = self._validate_exact_instance_and_inventory(
                    self._transport.get_instance(
                    request, timeout_seconds=timeout_seconds
                    ),
                    self._transport.list_owned_complete(
                    request, timeout_seconds=timeout_seconds
                    ),
                )
        except (GcpExecutionError, ValidationError, ValueError, TypeError) as error:
            if isinstance(error, GcpSupervisorError):
                raise
            raise GcpSupervisorError("WATCHDOG_INSTANCE_CLEANUP_UNAVAILABLE") from None
        return GcpWatchdogPrebindCleanupResult(
            schema_version="inferdrome.gcp-watchdog-prebind-cleanup-result.v2",
            controller_id=event.binding.controller_id,
            request_digest=event.binding.request_digest,
            project_id=event.binding.project_id,
            region=event.binding.region,
            zone=event.binding.zone,
            instance_name=event.binding.instance_name,
            labels=event.binding.labels,
            instance_observation=observation,
            owned_inventory=inventory,
        )

    def cleanup_prebind(
        self, event: GcpFileWatchdogEvent, *, timeout_seconds: int
    ) -> GcpWatchdogPrebindCleanupResult:
        """End only the exact instance; intentionally leave an unbound disk orphaned."""

        return self._exact_absence(event, timeout_seconds=timeout_seconds)

    def cleanup_exact(
        self, event: GcpFileWatchdogEvent, *, timeout_seconds: int
    ) -> GcpWatchdogCleanupResult:
        """Delete/reconcile the exact bound disk after exact instance absence."""

        self._validate_event(event)
        disk_binding = event.disk_cleanup_binding
        if (
            self._disk_cleanup is None
            or disk_binding is None
            or disk_binding != self._disk_cleanup.binding
        ):
            raise GcpSupervisorError("WATCHDOG_DISK_BINDING_MISMATCH")
        prebind = self._exact_absence(event, timeout_seconds=timeout_seconds)
        try:
            outcome = self._disk_cleanup(self._record, self._now_fn())
        except GcpV2DiskCleanupError as error:
            raise GcpSupervisorError(error.code) from None
        if outcome.state != "ABSENCE_CONFIRMED":
            raise GcpSupervisorError("WATCHDOG_DISK_CLEANUP_UNCONFIRMED")
        return GcpWatchdogCleanupResult(
            schema_version="inferdrome.gcp-watchdog-cleanup-result.v2",
            controller_id=event.binding.controller_id,
            request_digest=event.binding.request_digest,
            project_id=event.binding.project_id,
            region=event.binding.region,
            zone=event.binding.zone,
            instance_name=event.binding.instance_name,
            labels=event.binding.labels,
            disk_cleanup_binding=disk_binding,
            disk_cleanup_outcome=outcome,
            instance_observation=prebind.instance_observation,
            owned_inventory=prebind.owned_inventory,
        )


class GcpV2LocalWatchdogExecutorFactory:
    """Nominal local-only builder for a restartable v2 watchdog executor.

    The factory is configured before arming but creates no transport until the
    file watchdog has recorded a validated activation capability.  Its durable
    configuration digest binds the original lease, supervisor binding, and
    disk-journal configuration so a restarted watchdog cannot substitute an
    arbitrary cleanup object.
    """

    def __init__(
        self,
        *,
        transport_factory: object,
        disk_cleanup_factory: GcpV2LocalDiskCleanupFactory,
        core_journal: GcpLeaseJournal,
        now_fn: Callable[[], datetime],
    ) -> None:
        self._transport_factory = transport_factory
        self._disk_cleanup_factory = disk_cleanup_factory
        self._core_journal = core_journal
        self._now_fn = now_fn
        self._record: GcpLeaseRecord | None = None

    def configure_lease(self, record: GcpLeaseRecord) -> None:
        self._record = record

    @property
    def disk_cleanup_factory(self) -> GcpV2LocalDiskCleanupFactory:
        """Expose the nominal disk sidecar configuration for root checks."""

        return self._disk_cleanup_factory

    @property
    def core_journal_root(self) -> Path:
        """Return the core journal root used for fresh-process reconstruction."""

        return self._core_journal.root

    def _record_for(self, binding: GcpSupervisorBinding) -> GcpLeaseRecord:
        record = self._record
        if record is None:
            # A restarted watchdog receives no controller memory.  Reload the
            # durable core lease by its exact controller ID, then validate it
            # against the sidecar binding before any cleanup transport can be
            # reconstructed.
            try:
                record = self._core_journal.load(binding.controller_id)
            except GcpExecutionError:
                raise GcpSupervisorError(
                    "FILE_WATCHDOG_EXECUTOR_LEASE_MISMATCH"
                ) from None
            self._record = record
        if record is None or (
            record.controller_id != binding.controller_id
            or record.request_digest != binding.request_digest
            or record.project_id != binding.project_id
            or record.region != binding.region
            or record.zone != binding.zone
            or record.instance_name != binding.instance_name
            or record.labels != binding.labels
        ):
            raise GcpSupervisorError("FILE_WATCHDOG_EXECUTOR_LEASE_MISMATCH")
        return record

    def configuration_digest(self, binding: GcpSupervisorBinding) -> Sha256Digest:
        record = self._record_for(binding)
        payload = {
            "kind": "gcp_v2_local_watchdog_executor_factory",
            "lease_intent_anchor_digest": record.intent_anchor_digest,
            "request_digest": record.request_digest,
            "controller_id": record.controller_id,
            "project_id": record.project_id,
            "region": record.region,
            "zone": record.zone,
            "instance_name": record.instance_name,
            "labels": _model_value(record.labels),
            "supervisor_binding": _model_value(binding),
            "core_journal_root": os.path.realpath(
                os.fspath(self._core_journal.root)
            ),
            "disk_cleanup_factory": self._disk_cleanup_factory.configuration_digest,
        }
        return digest_bytes(
            DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(payload)
        )

    def _transport_for(
        self, capability: GcpV2MutationCapability
    ) -> GcpComputeTransport:
        factory = self._transport_factory
        try:
            from inferdrome.deployment.gcp_compute_transport import (
                GcpV2LocalTransportFactory,
            )
        except ImportError:
            raise GcpSupervisorError("FILE_WATCHDOG_EXECUTOR_FACTORY_INVALID") from None
        if type(factory) is not GcpV2LocalTransportFactory:
            raise GcpSupervisorError("FILE_WATCHDOG_EXECUTOR_FACTORY_INVALID")
        try:
            return factory.bind_watchdog_cleanup(capability)
        except GcpExecutionError as error:
            raise GcpSupervisorError(str(error)) from None
        except BaseException:
            raise GcpSupervisorError("FILE_WATCHDOG_EXECUTOR_FACTORY_INVALID") from None

    def bind_for_capability(
        self,
        *,
        binding: GcpSupervisorBinding,
        capability: GcpV2MutationCapability,
    ) -> GcpV2ExactWatchdogCleanupExecutor:
        record = self._record_for(binding)
        return GcpV2ExactWatchdogCleanupExecutor(
            record=record,
            supervisor_binding=binding,
            transport=self._transport_for(capability),
            disk_cleanup=None,
            now_fn=self._now_fn,
        )

    def bind_for_event(
        self, event: GcpFileWatchdogEvent
    ) -> GcpV2ExactWatchdogCleanupExecutor:
        capability = event.mutation_capability
        if capability is None:
            raise GcpSupervisorError("FILE_WATCHDOG_CAPABILITY_MISSING")
        record = self._record_for(event.binding)
        disk_cleanup = (
            self._disk_cleanup_factory.bind(
                record=record,
                binding=event.disk_cleanup_binding,
                authority=capability,
                now=self._now_fn(),
            )
            if event.disk_cleanup_binding is not None
            else None
        )
        return GcpV2ExactWatchdogCleanupExecutor(
            record=record,
            supervisor_binding=event.binding,
            transport=self._transport_for(capability),
            disk_cleanup=disk_cleanup,
            now_fn=self._now_fn,
        )


class GcpKillSwitch(Protocol):
    """A read-only kill-switch observation boundary."""

    def is_killed(self, binding: GcpSupervisorBinding) -> bool: ...


class FakeGcpWatchdog:
    """Deterministic local-only watchdog fake for adversarial lifecycle tests."""

    def __init__(self, *, arm_error: str | None = None, ready: bool = True) -> None:
        self.arm_error = arm_error
        self.ready = ready
        self.arm_calls = 0
        self.activation_calls = 0

    def arm(
        self,
        binding: GcpSupervisorBinding,
        *,
        approval: GcpExecutionApproval,
        now: datetime,
    ) -> GcpWatchdogReceipt:
        self.arm_calls += 1
        if self.arm_error is not None:
            raise GcpSupervisorError(self.arm_error)
        if not self.ready:
            raise GcpSupervisorError("WATCHDOG_NOT_READY")
        return GcpWatchdogReceipt(
            schema_version=GCP_WATCHDOG_RECEIPT_SCHEMA_VERSION,
            watchdog_id=(
                f"watchdog-wd-{binding.controller_id.removeprefix('ctl-')}"
            ),
            approval_digest=gcp_execution_approval_digest(approval),
            activation_deadline_digest=binding.activation_deadline_digest,
            request_digest=binding.request_digest,
            startup_projection_digest=binding.startup_projection_digest,
            execution_payload_digest=binding.execution_payload_digest,
            controller_id=binding.controller_id,
            armed_at=_timestamp(now),
            expires_at=approval.watchdog_deadline_at,
            ready=True,
            independently_durable=True,
            controller_death_coverage=True,
            hung_work_coverage=True,
        )

    def activate(
        self,
        binding: GcpSupervisorBinding,
        *,
        approval: GcpExecutionApproval,
        capability: GcpV2MutationCapability,
        now: datetime,
    ) -> GcpWatchdogActivationReceipt:
        self.activation_calls += 1
        if not self.ready:
            raise GcpSupervisorError("WATCHDOG_NOT_READY")
        payload: dict[str, Any] = {
            "schema_version": GCP_WATCHDOG_ACTIVATION_RECEIPT_SCHEMA_VERSION,
            "watchdog_id": (
                f"watchdog-wd-{binding.controller_id.removeprefix('ctl-')}"
            ),
            "approval_digest": gcp_execution_approval_digest(approval),
            "activation_deadline_digest": binding.activation_deadline_digest,
            "request_digest": binding.request_digest,
            "startup_projection_digest": binding.startup_projection_digest,
            "execution_payload_digest": binding.execution_payload_digest,
            "controller_id": binding.controller_id,
            "capability_id": capability.capability_id,
            "activated_at": capability.activated_at,
            "provider_runtime_deadline_at": capability.provider_runtime_deadline_at,
            "watchdog_cleanup_deadline_at": (
                capability.watchdog_cleanup_deadline_at
            ),
            "ready": True,
            "independently_durable": True,
        }
        provisional = GcpWatchdogActivationReceipt.model_construct(
            **{**payload, "receipt_id": "sha256:" + "0" * 64}
        )
        payload["receipt_id"] = gcp_watchdog_activation_receipt_id(provisional)
        return GcpWatchdogActivationReceipt.model_validate_json(
            canonical_json_bytes(payload)
        )


def _file_watchdog_event(
    *,
    sequence: int,
    state: GcpFileWatchdogState,
    binding: GcpSupervisorBinding,
    watchdog_id: GcpWatchdogId,
    armed_at: GcpTimestamp,
    expires_at: GcpTimestamp,
    capability_id: Sha256Digest | None,
    mutation_capability: GcpV2MutationCapability | None,
    activation_receipt_id: Sha256Digest | None,
    provider_runtime_deadline_at: GcpTimestamp | None,
    watchdog_cleanup_deadline_at: GcpTimestamp | None,
    runner_process_id: int | None,
    runner_ready_at: GcpTimestamp | None,
    executor_config_digest: Sha256Digest | None,
    disk_cleanup_binding: GcpV2DiskCleanupBinding | None,
    cleanup_result: GcpWatchdogCleanupResult | None,
    core_terminal_fenced_at: GcpTimestamp | None,
    cleanup_attempts: int,
    max_cleanup_attempts: int,
    cleanup_timeout_seconds: int,
    occurred_at: GcpTimestamp,
    error_code: str | None,
    previous_event_digest: Sha256Digest | None,
    prebind_cleanup_result: GcpWatchdogPrebindCleanupResult | None = None,
) -> GcpFileWatchdogEvent:
    value: dict[str, Any] = {
        "schema_version": GCP_FILE_WATCHDOG_EVENT_SCHEMA_VERSION,
        "sequence": sequence,
        "state": state,
        "binding": _model_value(binding),
        "watchdog_id": watchdog_id,
        "armed_at": armed_at,
        "expires_at": expires_at,
        "capability_id": capability_id,
        "mutation_capability": (
            _model_value(mutation_capability)
            if mutation_capability is not None
            else None
        ),
        "activation_receipt_id": activation_receipt_id,
        "provider_runtime_deadline_at": provider_runtime_deadline_at,
        "watchdog_cleanup_deadline_at": watchdog_cleanup_deadline_at,
        "runner_process_id": runner_process_id,
        "runner_ready_at": runner_ready_at,
        "executor_config_digest": executor_config_digest,
        "disk_cleanup_binding": (
            _model_value(disk_cleanup_binding)
            if disk_cleanup_binding is not None
            else None
        ),
        "cleanup_result": (
            _model_value(cleanup_result) if cleanup_result is not None else None
        ),
        "prebind_cleanup_result": (
            _model_value(prebind_cleanup_result)
            if prebind_cleanup_result is not None
            else None
        ),
        "core_terminal_fenced_at": core_terminal_fenced_at,
        "cleanup_attempts": cleanup_attempts,
        "max_cleanup_attempts": max_cleanup_attempts,
        "cleanup_timeout_seconds": cleanup_timeout_seconds,
        "occurred_at": occurred_at,
        "error_code": error_code,
        "previous_event_digest": previous_event_digest,
    }
    value["event_digest"] = digest_bytes(
        DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(value)
    )
    return GcpFileWatchdogEvent.model_validate_json(canonical_json_bytes(value))


class FileGcpWatchdog:
    """A concrete fsync-backed, restartable exact-cleanup watchdog.

    It records a controller-addressed task before returning ``READY`` and can
    be reopened by an independent local process after the controller dies or
    hangs.  The injected executor is intentionally cleanup-only: this class
    has no Google SDK, credential, create, hostname, or account-discovery
    path.  When ``runner_enabled`` is selected, ``activate`` starts a detached
    local runner and returns only after the runner has acknowledged readiness
    and that receipt is fsync-persisted.  The runner may be reconstructed from
    the same root after a process restart; malformed state or an ambiguous
    executor result remains orphaned.
    """

    def __init__(
        self,
        root: Path,
        *,
        executor: GcpWatchdogCleanupExecutor | GcpV2LocalWatchdogExecutorFactory,
        crash_hook: Callable[[str], None] | None = None,
        runner_enabled: bool = False,
        runner_poll_seconds: float = 0.05,
        runner_now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = root
        if type(executor) is GcpV2LocalWatchdogExecutorFactory:
            self._executor_factory: GcpV2LocalWatchdogExecutorFactory | None = (
                executor
            )
            # The nominal factory is deliberately not itself executable.  It
            # is replaced by an exact executor only after a durable event
            # validates its sealed configuration.
            self.executor = cast(GcpWatchdogCleanupExecutor, executor)
        else:
            self._executor_factory = None
            self.executor = cast(GcpWatchdogCleanupExecutor, executor)
        self._lock = threading.Lock()
        self._crash_hook = crash_hook
        self._runner_enabled = runner_enabled
        if not 0.01 <= runner_poll_seconds <= 5.0:
            raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_INTERVAL_INVALID")
        self._runner_poll_seconds = runner_poll_seconds
        self._runner_now_fn = runner_now_fn or (lambda: datetime.now(UTC))

    @property
    def independently_running(self) -> bool:
        """Whether this instance can launch an autonomous local runner."""

        return self._runner_enabled and hasattr(os, "fork")

    def configure_lease(self, record: GcpLeaseRecord) -> None:
        """Bind a nominal executor factory to the core lease before arming."""

        if self._executor_factory is None:
            return
        self._executor_factory.configure_lease(record)

    @property
    def v2_disk_cleanup_factory(self) -> GcpV2LocalDiskCleanupFactory | None:
        """Return the restartable executor's nominal disk factory, if any."""

        if self._executor_factory is None:
            return None
        return self._executor_factory.disk_cleanup_factory

    @property
    def v2_core_journal_root(self) -> Path | None:
        """Return the durable core source used by a fresh watchdog process."""

        if self._executor_factory is None:
            return None
        return self._executor_factory.core_journal_root

    def _crash_point(self, point: str) -> None:
        """Test-only process-death injection; production leaves this unset."""

        if self._crash_hook is not None:
            self._crash_hook(point)

    def _checked_root(self) -> Path:
        if not self.root.is_absolute():
            raise GcpSupervisorError("FILE_WATCHDOG_PATH_INVALID")
        try:
            metadata = self.root.lstat()
            resolved = os.path.realpath(os.fspath(self.root))
        except OSError:
            raise GcpSupervisorError("FILE_WATCHDOG_UNAVAILABLE") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mode & 0o022
            or os.path.normcase(os.path.abspath(os.fspath(self.root)))
            != os.path.normcase(resolved)
        ):
            raise GcpSupervisorError("FILE_WATCHDOG_PATH_UNSAFE")
        return self.root

    @staticmethod
    def _path_for(root: Path, controller_id: str) -> Path:
        if _CONTROLLER_RE.fullmatch(controller_id) is None:
            raise GcpSupervisorError("FILE_WATCHDOG_CONTROLLER_INVALID")
        return root / f"{controller_id}.file-watchdog-v2.events.jsonl"

    @contextmanager
    def _exclusive(self) -> Iterator[Path]:
        root = self._checked_root()
        descriptor: int | None = None
        try:
            descriptor = os.open(
                root / ".file-watchdog-v2.lock",
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except (ImportError, OSError):
                raise GcpSupervisorError("FILE_WATCHDOG_LOCK_UNAVAILABLE") from None
            with self._lock:
                yield root
        finally:
            if descriptor is not None:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                os.close(descriptor)

    @staticmethod
    def _watchdog_id(binding: GcpSupervisorBinding) -> GcpWatchdogId:
        return f"watchdog-file-{binding.controller_id.removeprefix('ctl-')}"

    @staticmethod
    def _validate_disk_binding_for_supervisor(
        binding: GcpSupervisorBinding, disk_binding: GcpV2DiskCleanupBinding
    ) -> GcpV2DiskCleanupBinding:
        disk_binding = _strict_disk_cleanup_binding(disk_binding)
        if (
            disk_binding.controller_id != binding.controller_id
            or disk_binding.request_digest != binding.request_digest
            or disk_binding.project_id != binding.project_id
            or disk_binding.zone != binding.zone
            or disk_binding.instance_name != binding.instance_name
            or disk_binding.labels != binding.labels
        ):
            raise GcpSupervisorError("FILE_WATCHDOG_DISK_BINDING_MISMATCH")
        return disk_binding

    def _executor_config_for_binding(
        self, binding: GcpSupervisorBinding
    ) -> Sha256Digest:
        """Accept only the sealed exact cleanup executor for runner mode."""

        if self._executor_factory is not None:
            return self._executor_factory.configuration_digest(binding)
        executor = self.executor
        if (
            type(executor) is not GcpV2ExactWatchdogCleanupExecutor
            or not executor.matches_watchdog_binding(binding)
        ):
            raise GcpSupervisorError("FILE_WATCHDOG_EXECUTOR_INVALID")
        return executor.configuration_digest

    def _validate_executor_for_event(self, event: GcpFileWatchdogEvent) -> None:
        self._validate_executor_config_for_event(event)
        if self._executor_factory is not None:
            self.executor = self._executor_factory.bind_for_event(event)

    def _validate_executor_config_for_event(self, event: GcpFileWatchdogEvent) -> None:
        """Validate a persisted executor identity without constructing it.

        This is used while holding the journal lock before an intent is
        recorded.  In particular it must not lazily initialize a future
        provider boundary merely to inspect a local sidecar event.
        """

        expected = self._executor_config_for_binding(event.binding)
        if event.executor_config_digest != expected:
            raise GcpSupervisorError("FILE_WATCHDOG_EXECUTOR_MISMATCH")

    @staticmethod
    def _runner_resume_count(events: tuple[GcpFileWatchdogEvent, ...]) -> int:
        """Count durable replacement-runner transitions, not process liveness.

        A stale PID proves neither death nor cleanup.  We therefore count only
        append-only same-state transitions which replace an already recorded
        runner identity.  The count is deterministic after crash/restart and
        cannot be reset by a new watchdog process.
        """

        return sum(
            1
            for previous, current in pairwise(events)
            if previous.runner_process_id is not None
            and current.runner_process_id is not None
            and previous.runner_process_id != current.runner_process_id
            and previous.state == current.state
        )

    def _orphan_for_journal_budget_locked(
        self,
        root: Path,
        events: tuple[GcpFileWatchdogEvent, ...],
        *,
        binding: GcpSupervisorBinding,
        now: datetime,
        error_code: str,
    ) -> GcpFileWatchdogEvent:
        """Persist an explicit terminal state before append capacity is lost."""

        latest = events[-1]
        if latest.state in {
            "CLEANUP_CONFIRMED",
            "CLEANUP_NOT_REQUIRED",
            "ORPHANED",
        }:
            return latest
        if len(events) >= GCP_FILE_WATCHDOG_MAX_EVENTS:
            # This branch is only reachable for a corrupt/legacy exhausted
            # journal.  It is already not restartable, so do not pretend a
            # caller may safely continue.
            raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_BOUND_EXCEEDED")
        return self._append_locked(
            root,
            events,
            state="ORPHANED",
            binding=binding,
            now=now,
            error_code=error_code,
        )

    def _settle_core_terminal_fence_locked(
        self,
        root: Path,
        events: tuple[GcpFileWatchdogEvent, ...],
        *,
        now: datetime,
    ) -> GcpFileWatchdogEvent:
        """Settle a core-terminal fence without constructing an executor.

        A core record may become authoritative after the sidecar has durably
        claimed an exact cleanup attempt but before the isolated worker can
        finish. The fence grants that already-recorded attempt at most its
        existing bounded cleanup lease. No restart can acquire an
        executor/provider while the fence is present; after the lease, the
        sidecar records that cleanup is no longer required.
        """

        latest = events[-1]
        if latest.core_terminal_fenced_at is None:
            return latest
        if latest.state == "CLEANUP_NOT_REQUIRED":
            return latest
        if latest.state not in {"CLEANUP_INTENT", "PREBIND_CLEANUP_INTENT"}:
            raise GcpSupervisorError("FILE_WATCHDOG_CORE_FENCE_INVALID")
        fence_until = _parse_timestamp(latest.core_terminal_fenced_at) + timedelta(
            seconds=latest.cleanup_timeout_seconds
        )
        if latest.watchdog_cleanup_deadline_at is not None:
            fence_until = min(
                fence_until, _parse_timestamp(latest.watchdog_cleanup_deadline_at)
            )
        if _parse_timestamp(_timestamp(now)) < fence_until:
            return latest
        return self._append_locked(
            root,
            events,
            state="CLEANUP_NOT_REQUIRED",
            binding=latest.binding,
            now=now,
            error_code="CORE_TERMINAL_FENCE_SETTLED",
        )

    def _activate_executor(
        self,
        binding: GcpSupervisorBinding,
        capability: GcpV2MutationCapability,
    ) -> Sha256Digest:
        """Build the no-create executor only after activation is validated."""

        config_digest = self._executor_config_for_binding(binding)
        if self._executor_factory is not None:
            self.executor = self._executor_factory.bind_for_capability(
                binding=binding, capability=capability
            )
        if (
            type(self.executor) is not GcpV2ExactWatchdogCleanupExecutor
            or not self.executor.matches_watchdog_binding(binding)
        ):
            raise GcpSupervisorError("FILE_WATCHDOG_EXECUTOR_INVALID")
        return config_digest

    def _fsync_directory(self, root: Path) -> None:
        descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _repair_partial_tail_locked(self, path: Path, root: Path) -> bool:
        """Repair only an incomplete tail; remove a zero-event crash prefix."""

        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > GCP_FILE_WATCHDOG_MAX_EVENT_BYTES
                or metadata.st_mode & 0o022
            ):
                raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_UNSAFE")
            raw = os.read(descriptor, GCP_FILE_WATCHDOG_MAX_EVENT_BYTES + 1)
            if len(raw) > GCP_FILE_WATCHDOG_MAX_EVENT_BYTES:
                raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_UNSAFE")
            if not raw.endswith(b"\n"):
                newline = raw.rfind(b"\n")
                if newline < 0:
                    os.ftruncate(descriptor, 0)
                    os.fsync(descriptor)
                    os.unlink(path)
                    self._fsync_directory(root)
                    return False
                os.ftruncate(descriptor, newline + 1)
                os.fsync(descriptor)
                self._fsync_directory(root)
            return True
        except FileNotFoundError:
            return False
        except GcpSupervisorError:
            raise
        except OSError:
            raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_REPAIR_FAILED") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _validate_transitions(events: list[GcpFileWatchdogEvent]) -> None:
        allowed: dict[GcpFileWatchdogState, set[GcpFileWatchdogState]] = {
            "ARMED": {"READY", "CLEANUP_NOT_REQUIRED"},
            "READY": {"ACTIVE", "ORPHANED", "CLEANUP_NOT_REQUIRED"},
            "ACTIVE": {"RUNNER_READY", "ORPHANED", "CLEANUP_NOT_REQUIRED"},
            "RUNNER_READY": {
                "RUNNER_READY",
                "DISK_BOUND",
                "PREBIND_CLEANUP_INTENT",
                "CLEANUP_INTENT",
                "ORPHANED",
                "CLEANUP_NOT_REQUIRED",
            },
            "DISK_BOUND": {
                "DISK_BOUND",
                "CLEANUP_INTENT",
                "ORPHANED",
                "CLEANUP_NOT_REQUIRED",
            },
            "PREBIND_CLEANUP_INTENT": {
                "PREBIND_CLEANUP_INTENT",
                "PREBIND_RETRY_PENDING",
                "ORPHANED",
                "CLEANUP_NOT_REQUIRED",
            },
            "PREBIND_RETRY_PENDING": {
                "PREBIND_RETRY_PENDING",
                "PREBIND_CLEANUP_INTENT",
                "ORPHANED",
                "CLEANUP_NOT_REQUIRED",
            },
            "CLEANUP_INTENT": {
                "CLEANUP_INTENT",
                "RETRY_PENDING",
                "CLEANUP_CONFIRMED",
                "CLEANUP_NOT_REQUIRED",
                "ORPHANED",
            },
            "RETRY_PENDING": {
                "RETRY_PENDING",
                "CLEANUP_INTENT",
                "CLEANUP_NOT_REQUIRED",
                "ORPHANED",
            },
            "CLEANUP_CONFIRMED": {"CLEANUP_CONFIRMED"},
            "CLEANUP_NOT_REQUIRED": {"CLEANUP_NOT_REQUIRED"},
            "ORPHANED": {"CLEANUP_NOT_REQUIRED", "ORPHANED"},
        }
        if events[0].core_terminal_fenced_at is not None:
            raise GcpSupervisorError("FILE_WATCHDOG_CORE_FENCE_INVALID")
        for previous, current in pairwise(events):
            if current.state not in allowed[previous.state]:
                raise GcpSupervisorError("FILE_WATCHDOG_TRANSITION_INVALID")
            if _parse_timestamp(current.occurred_at) < _parse_timestamp(
                previous.occurred_at
            ):
                raise GcpSupervisorError("FILE_WATCHDOG_TIMESTAMP_REGRESSED")
            if (
                current.cleanup_attempts < previous.cleanup_attempts
                or current.cleanup_attempts > previous.cleanup_attempts + 1
            ):
                raise GcpSupervisorError("FILE_WATCHDOG_ATTEMPTS_INVALID")
            if current.state in {"CLEANUP_INTENT", "PREBIND_CLEANUP_INTENT"}:
                same_intent = current.state == previous.state
                expected_attempts = (
                    previous.cleanup_attempts
                    if same_intent
                    else previous.cleanup_attempts + 1
                )
                if current.cleanup_attempts != expected_attempts:
                    raise GcpSupervisorError("FILE_WATCHDOG_INTENT_INVALID")
            fence_changed = (
                current.core_terminal_fenced_at != previous.core_terminal_fenced_at
            )
            if fence_changed and not (
                previous.core_terminal_fenced_at is None
                and current.core_terminal_fenced_at is not None
                and previous.state in {"CLEANUP_INTENT", "PREBIND_CLEANUP_INTENT"}
                and current.state == previous.state
                and current.cleanup_attempts == previous.cleanup_attempts
            ):
                raise GcpSupervisorError("FILE_WATCHDOG_CORE_FENCE_INVALID")
            if previous.capability_id is not None and (
                current.capability_id != previous.capability_id
                or current.activation_receipt_id != previous.activation_receipt_id
                or current.provider_runtime_deadline_at
                != previous.provider_runtime_deadline_at
                or current.watchdog_cleanup_deadline_at
                != previous.watchdog_cleanup_deadline_at
            ):
                raise GcpSupervisorError("FILE_WATCHDOG_RUNTIME_BINDING_CHANGED")
            if previous.executor_config_digest is not None and (
                current.executor_config_digest != previous.executor_config_digest
            ):
                raise GcpSupervisorError("FILE_WATCHDOG_EXECUTOR_CHANGED")
            if previous.runner_process_id is not None and (
                current.runner_process_id != previous.runner_process_id
                or current.runner_ready_at != previous.runner_ready_at
            ) and current.state != previous.state:
                raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_BINDING_CHANGED")

    def _read_locked(
        self, root: Path, controller_id: str
    ) -> tuple[GcpFileWatchdogEvent, ...]:
        path = self._path_for(root, controller_id)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except FileNotFoundError:
            return ()
        except OSError:
            raise GcpSupervisorError("FILE_WATCHDOG_UNAVAILABLE") from None
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > GCP_FILE_WATCHDOG_MAX_EVENT_BYTES
                or metadata.st_mode & 0o022
            ):
                raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_UNSAFE")
            raw = os.read(descriptor, GCP_FILE_WATCHDOG_MAX_EVENT_BYTES + 1)
            final = os.fstat(descriptor)
            if (
                len(raw) > GCP_FILE_WATCHDOG_MAX_EVENT_BYTES
                or final.st_ino != metadata.st_ino
                or final.st_size != len(raw)
            ):
                raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_CHANGED")
            if not raw.endswith(b"\n"):
                raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_PARTIAL")
            events: list[GcpFileWatchdogEvent] = []
            previous: Sha256Digest | None = None
            binding: GcpSupervisorBinding | None = None
            disk_binding: GcpV2DiskCleanupBinding | None = None
            for sequence, line in enumerate(raw.splitlines()):
                _preflight_json(line, kind="file GCP watchdog event")
                event = GcpFileWatchdogEvent.model_validate_json(line)
                if (
                    canonical_json_bytes(_model_value(event)) != line
                    or event.sequence != sequence
                    or event.previous_event_digest != previous
                    or len(events) >= GCP_FILE_WATCHDOG_MAX_EVENTS
                ):
                    raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_INVALID")
                if binding is not None and event.binding != binding:
                    raise GcpSupervisorError("FILE_WATCHDOG_BINDING_CHANGED")
                if event.disk_cleanup_binding is not None:
                    self._validate_disk_binding_for_supervisor(
                        event.binding, event.disk_cleanup_binding
                    )
                    if (
                        disk_binding is not None
                        and event.disk_cleanup_binding != disk_binding
                    ):
                        raise GcpSupervisorError("FILE_WATCHDOG_DISK_BINDING_CHANGED")
                    disk_binding = event.disk_cleanup_binding
                elif disk_binding is not None:
                    raise GcpSupervisorError("FILE_WATCHDOG_DISK_BINDING_CHANGED")
                previous = event.event_digest
                binding = event.binding
                events.append(event)
            if not events:
                raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_INVALID")
            self._validate_transitions(events)
            return tuple(events)
        except GcpSupervisorError:
            raise
        except (OSError, ValidationError, ValueError):
            raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_INVALID") from None
        finally:
            os.close(descriptor)

    def _append_locked(
        self,
        root: Path,
        events: tuple[GcpFileWatchdogEvent, ...],
        *,
        state: GcpFileWatchdogState,
        binding: GcpSupervisorBinding,
        now: datetime,
        capability: GcpV2MutationCapability | None = None,
        activation_receipt_id: Sha256Digest | None = None,
        runner_process_id: int | None = None,
        runner_ready_at: GcpTimestamp | None = None,
        executor_config_digest: Sha256Digest | None = None,
        disk_cleanup_binding: GcpV2DiskCleanupBinding | None = None,
        cleanup_result: GcpWatchdogCleanupResult | None = None,
        prebind_cleanup_result: GcpWatchdogPrebindCleanupResult | None = None,
        core_terminal_fenced_at: GcpTimestamp | None = None,
        cleanup_attempts: int | None = None,
        error_code: str | None = None,
    ) -> GcpFileWatchdogEvent:
        prior = events[-1] if events else None
        if prior is not None and prior.binding != binding:
            raise GcpSupervisorError("FILE_WATCHDOG_BINDING_MISMATCH")
        prior_disk_binding = (
            prior.disk_cleanup_binding if prior is not None else None
        )
        if prior_disk_binding is not None and (
            disk_cleanup_binding is not None
            and disk_cleanup_binding != prior_disk_binding
        ):
            raise GcpSupervisorError("FILE_WATCHDOG_DISK_BINDING_CHANGED")
        effective_disk_binding = (
            disk_cleanup_binding
            if disk_cleanup_binding is not None
            else prior_disk_binding
        )
        if effective_disk_binding is not None:
            effective_disk_binding = self._validate_disk_binding_for_supervisor(
                binding, effective_disk_binding
            )
        if len(events) >= GCP_FILE_WATCHDOG_MAX_EVENTS:
            raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_BOUND_EXCEEDED")
        capability_id = (
            capability.capability_id
            if capability is not None
            else (prior.capability_id if prior is not None else None)
        )
        effective_mutation_capability = (
            capability
            if capability is not None
            else (prior.mutation_capability if prior is not None else None)
        )
        receipt_id = (
            activation_receipt_id
            if activation_receipt_id is not None
            else (prior.activation_receipt_id if prior is not None else None)
        )
        provider_runtime_deadline_at = (
            capability.provider_runtime_deadline_at
            if capability is not None
            else (prior.provider_runtime_deadline_at if prior is not None else None)
        )
        watchdog_cleanup_deadline_at = (
            capability.watchdog_cleanup_deadline_at
            if capability is not None
            else (prior.watchdog_cleanup_deadline_at if prior is not None else None)
        )
        effective_runner_process_id = (
            runner_process_id
            if runner_process_id is not None
            else (prior.runner_process_id if prior is not None else None)
        )
        effective_runner_ready_at = (
            runner_ready_at
            if runner_ready_at is not None
            else (prior.runner_ready_at if prior is not None else None)
        )
        effective_executor_config_digest = (
            executor_config_digest
            if executor_config_digest is not None
            else (prior.executor_config_digest if prior is not None else None)
        )
        effective_core_terminal_fenced_at = (
            core_terminal_fenced_at
            if core_terminal_fenced_at is not None
            else (prior.core_terminal_fenced_at if prior is not None else None)
        )
        event = _file_watchdog_event(
            sequence=len(events),
            state=state,
            binding=binding,
            watchdog_id=self._watchdog_id(binding),
            armed_at=(prior.armed_at if prior is not None else _timestamp(now)),
            expires_at=(
                prior.expires_at if prior is not None else binding.watchdog_deadline_at
            ),
            capability_id=capability_id,
            mutation_capability=effective_mutation_capability,
            activation_receipt_id=receipt_id,
            provider_runtime_deadline_at=provider_runtime_deadline_at,
            watchdog_cleanup_deadline_at=watchdog_cleanup_deadline_at,
            runner_process_id=effective_runner_process_id,
            runner_ready_at=effective_runner_ready_at,
            executor_config_digest=effective_executor_config_digest,
            disk_cleanup_binding=effective_disk_binding,
            cleanup_result=cleanup_result,
            prebind_cleanup_result=prebind_cleanup_result,
            core_terminal_fenced_at=effective_core_terminal_fenced_at,
            cleanup_attempts=(
                cleanup_attempts
                if cleanup_attempts is not None
                else (prior.cleanup_attempts if prior is not None else 0)
            ),
            max_cleanup_attempts=(
                prior.max_cleanup_attempts
                if prior is not None
                else binding.max_cleanup_attempts
            ),
            cleanup_timeout_seconds=(
                prior.cleanup_timeout_seconds
                if prior is not None
                else binding.cleanup_timeout_seconds
            ),
            occurred_at=_timestamp(now),
            error_code=error_code,
            previous_event_digest=(prior.event_digest if prior is not None else None),
        )
        raw = canonical_json_bytes(_model_value(event)) + b"\n"
        path = self._path_for(root, binding.controller_id)
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                _write_all(descriptor, raw)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._crash_point("file_watchdog_after_event_fsync")
            self._fsync_directory(root)
            self._crash_point("file_watchdog_after_directory_fsync")
        except OSError:
            raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_APPEND_FAILED") from None
        return event

    @staticmethod
    def _receipt(
        binding: GcpSupervisorBinding, *, armed_at: GcpTimestamp
    ) -> GcpWatchdogReceipt:
        return GcpWatchdogReceipt(
            schema_version=GCP_WATCHDOG_RECEIPT_SCHEMA_VERSION,
            watchdog_id=FileGcpWatchdog._watchdog_id(binding),
            approval_digest=binding.approval_digest,
            activation_deadline_digest=binding.activation_deadline_digest,
            request_digest=binding.request_digest,
            startup_projection_digest=binding.startup_projection_digest,
            execution_payload_digest=binding.execution_payload_digest,
            controller_id=binding.controller_id,
            armed_at=armed_at,
            expires_at=binding.watchdog_deadline_at,
            ready=True,
            independently_durable=True,
            controller_death_coverage=True,
            hung_work_coverage=True,
        )

    @staticmethod
    def _activation_receipt(
        binding: GcpSupervisorBinding, capability: GcpV2MutationCapability
    ) -> GcpWatchdogActivationReceipt:
        value: dict[str, Any] = {
            "schema_version": GCP_WATCHDOG_ACTIVATION_RECEIPT_SCHEMA_VERSION,
            "watchdog_id": FileGcpWatchdog._watchdog_id(binding),
            "approval_digest": binding.approval_digest,
            "activation_deadline_digest": binding.activation_deadline_digest,
            "request_digest": binding.request_digest,
            "startup_projection_digest": binding.startup_projection_digest,
            "execution_payload_digest": binding.execution_payload_digest,
            "controller_id": binding.controller_id,
            "capability_id": capability.capability_id,
            "activated_at": capability.activated_at,
            "provider_runtime_deadline_at": capability.provider_runtime_deadline_at,
            "watchdog_cleanup_deadline_at": capability.watchdog_cleanup_deadline_at,
            "ready": True,
            "independently_durable": True,
        }
        provisional = GcpWatchdogActivationReceipt.model_construct(
            **{**value, "receipt_id": "sha256:" + "0" * 64}
        )
        value["receipt_id"] = gcp_watchdog_activation_receipt_id(provisional)
        return GcpWatchdogActivationReceipt.model_validate_json(
            canonical_json_bytes(value)
        )

    @staticmethod
    def _validate_capability_for_binding(
        capability: GcpV2MutationCapability,
        binding: GcpSupervisorBinding,
        *,
        now: datetime,
    ) -> GcpV2MutationCapability:
        capability = _strict_mutation_capability(capability)
        try:
            now_value = _parse_timestamp(_timestamp(now))
        except ValueError:
            raise GcpSupervisorError("FILE_WATCHDOG_TIME_INVALID") from None
        if (
            capability.approval_digest != binding.approval_digest
            or capability.activation_contract_digest
            != binding.activation_deadline_digest
            or capability.controller_id != binding.controller_id
            or capability.request_digest != binding.request_digest
            or capability.startup_projection_digest
            != binding.startup_projection_digest
            or capability.execution_payload_digest
            != binding.execution_payload_digest
            or capability.project_id != binding.project_id
            or capability.region != binding.region
            or capability.zone != binding.zone
            or capability.instance_name != binding.instance_name
            or capability.labels != binding.labels
            or _parse_timestamp(capability.activated_at) > now_value
            or now_value >= _parse_timestamp(capability.provider_runtime_deadline_at)
            or _parse_timestamp(capability.watchdog_cleanup_deadline_at)
            > _parse_timestamp(binding.watchdog_deadline_at)
        ):
            raise GcpSupervisorError("FILE_WATCHDOG_CAPABILITY_MISMATCH")
        return capability

    def arm(
        self,
        binding: GcpSupervisorBinding,
        *,
        approval: GcpExecutionApproval,
        now: datetime,
    ) -> GcpWatchdogReceipt:
        approval = _strict_approval(approval)
        # A READY receipt is a promise that an independent child can take over
        # after controller death.  Do not mint that promise for the useful
        # direct-call/test mode where no local runner can be started.
        if not self.independently_running:
            raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_DISABLED")
        executor_config_digest = self._executor_config_for_binding(binding)
        if (
            gcp_execution_approval_digest(approval) != binding.approval_digest
            or approval.activation_deadline_digest != binding.activation_deadline_digest
            or approval.request_digest != binding.request_digest
            or approval.startup_projection_digest != binding.startup_projection_digest
            or approval.execution_payload_digest != binding.execution_payload_digest
            or approval.watchdog_deadline_at != binding.watchdog_deadline_at
        ):
            raise GcpSupervisorError("FILE_WATCHDOG_APPROVAL_MISMATCH")
        with self._exclusive() as root:
            path = self._path_for(root, binding.controller_id)
            events = (
                self._read_locked(root, binding.controller_id)
                if self._repair_partial_tail_locked(path, root)
                else ()
            )
            if events:
                latest = events[-1]
                if latest.binding != binding:
                    raise GcpSupervisorError("FILE_WATCHDOG_TASK_UNAVAILABLE")
                if latest.state == "ARMED":
                    latest = self._append_locked(
                        root,
                        events,
                        state="READY",
                        binding=binding,
                        now=now,
                        executor_config_digest=executor_config_digest,
                    )
                elif latest.state not in {"READY", "ACTIVE"}:
                    raise GcpSupervisorError("FILE_WATCHDOG_TASK_UNAVAILABLE")
                return self._receipt(binding, armed_at=latest.armed_at)
            events = (
                self._append_locked(
                    root,
                    (),
                    state="ARMED",
                    binding=binding,
                    now=now,
                    executor_config_digest=executor_config_digest,
                ),
            )
            self._append_locked(
                root,
                events,
                state="READY",
                binding=binding,
                now=now,
                executor_config_digest=executor_config_digest,
            )
        return self._receipt(binding, armed_at=_timestamp(now))

    def activate(
        self,
        binding: GcpSupervisorBinding,
        *,
        approval: GcpExecutionApproval,
        capability: GcpV2MutationCapability,
        now: datetime,
    ) -> GcpWatchdogActivationReceipt:
        approval = _strict_approval(approval)
        if (
            gcp_execution_approval_digest(approval) != binding.approval_digest
            or approval.activation_deadline_digest != binding.activation_deadline_digest
            or approval.request_digest != binding.request_digest
            or approval.startup_projection_digest != binding.startup_projection_digest
            or approval.execution_payload_digest != binding.execution_payload_digest
            or approval.watchdog_deadline_at != binding.watchdog_deadline_at
        ):
            raise GcpSupervisorError("FILE_WATCHDOG_APPROVAL_MISMATCH")
        capability = self._validate_capability_for_binding(
            capability, binding, now=now
        )
        executor_config_digest = self._activate_executor(binding, capability)
        receipt = self._activation_receipt(binding, capability)

        # First make the capability binding durable.  A crash here is safe:
        # the core has no usable proof and recovery can settle the no-mutation
        # journal state without ever constructing a transport.
        existing_ready = False
        with self._exclusive() as root:
            path = self._path_for(root, binding.controller_id)
            if not self._repair_partial_tail_locked(path, root):
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_MISSING")
            events = self._read_locked(root, binding.controller_id)
            if not events or events[-1].binding != binding:
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_MISSING")
            latest = events[-1]
            if latest.state in {"ACTIVE", "RUNNER_READY"}:
                if (
                    latest.capability_id != capability.capability_id
                    or latest.activation_receipt_id != receipt.receipt_id
                ):
                    raise GcpSupervisorError("FILE_WATCHDOG_CAPABILITY_MISMATCH")
                if latest.state == "RUNNER_READY":
                    self._validate_executor_for_event(latest)
                    existing_ready = True
            elif latest.state == "READY":
                self._append_locked(
                    root,
                    events,
                    state="ACTIVE",
                    binding=binding,
                    now=now,
                    capability=capability,
                    activation_receipt_id=receipt.receipt_id,
                )
            else:
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_UNAVAILABLE")

        if existing_ready:
            self.resume_runner(controller_id=binding.controller_id)
            return receipt

        # The child starts in a new session and is held behind a one-byte gate
        # until its ready acknowledgement is itself fsync-recorded.  It has no
        # create authority and receives only this watchdog object/executor.
        runner_pid, runner_gate, runner_ready = self._spawn_runner(
            binding.controller_id
        )
        launched = False
        try:
            with self._exclusive() as root:
                path = self._path_for(root, binding.controller_id)
                if not self._repair_partial_tail_locked(path, root):
                    raise GcpSupervisorError("FILE_WATCHDOG_TASK_MISSING")
                events = self._read_locked(root, binding.controller_id)
                latest = events[-1]
                if (
                    latest.binding != binding
                    or latest.state != "ACTIVE"
                    or latest.capability_id != capability.capability_id
                    or latest.activation_receipt_id != receipt.receipt_id
                ):
                    raise GcpSupervisorError("FILE_WATCHDOG_CONCURRENT_UPDATE")
                self._append_locked(
                    root,
                    events,
                    state="RUNNER_READY",
                    binding=binding,
                    now=now,
                    runner_process_id=runner_pid,
                    runner_ready_at=_timestamp(now),
                    executor_config_digest=executor_config_digest,
                )
                self._crash_point("file_watchdog_after_runner_ready")
            _write_all(runner_gate, b"G")
            self._await_runner_ready(runner_ready)
            launched = True
            return receipt
        finally:
            with suppress(OSError):
                os.close(runner_gate)
            with suppress(OSError):
                os.close(runner_ready)
            if not launched:
                self._kill_runner(runner_pid)

    def _kill_runner(self, runner_pid: int) -> None:
        try:
            os.killpg(runner_pid, signal.SIGKILL)
        except (AttributeError, OSError):
            with suppress(OSError):
                os.kill(runner_pid, signal.SIGKILL)
        with suppress(OSError):
            os.waitpid(runner_pid, os.WNOHANG)

    def _runner_loop(self, controller_id: str) -> None:
        """Run the detached exact task until a durable terminal event exists."""

        while True:
            try:
                event = self.run_due(
                    controller_id=controller_id, now=self._runner_now_fn()
                )
            except (GcpSupervisorError, ValueError):
                # The journal itself remains the recovery source of truth.  A
                # future independently started runner may retry an incomplete
                # intent; this worker never broadens scope after an ambiguity.
                return
            if event.state in {
                "CLEANUP_CONFIRMED",
                "CLEANUP_NOT_REQUIRED",
                "ORPHANED",
            }:
                return
            time.sleep(self._runner_poll_seconds)

    def _runner_post_gate_ready(self, controller_id: str, *, runner_pid: int) -> None:
        """Open and validate the durable task before acknowledging readiness."""

        with self._exclusive() as root:
            path = self._path_for(root, controller_id)
            if not self._repair_partial_tail_locked(path, root):
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_MISSING")
            latest = self._read_locked(root, controller_id)[-1]
            if latest.state not in {
                "RUNNER_READY",
                "DISK_BOUND",
                "PREBIND_RETRY_PENDING",
                "RETRY_PENDING",
                "PREBIND_CLEANUP_INTENT",
                "CLEANUP_INTENT",
            } or latest.runner_process_id != runner_pid:
                raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_BINDING_MISMATCH")
            # Do not construct a future executor/provider merely because this
            # sidecar is due.  The sealed configuration can be checked locally
            # here; construction waits until a durable intent owns the task.
            self._validate_executor_config_for_event(latest)

    @staticmethod
    def _await_runner_ready(ready_read: int) -> None:
        try:
            readable, _, _ = select.select([ready_read], [], [], 2.0)
            if not readable or os.read(ready_read, 1) != b"A":
                raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_UNAVAILABLE")
        except OSError:
            raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_UNAVAILABLE") from None

    def _spawn_runner(self, controller_id: str) -> tuple[int, int, int]:
        """Fork a detached runner and retain its post-gate readiness channel."""

        if not self.independently_running:
            raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_DISABLED")
        ready_read: int | None = None
        ready_write: int | None = None
        gate_read: int | None = None
        gate_write: int | None = None
        runner_pid: int | None = None
        handed_off = False
        try:
            ready_read, ready_write = os.pipe()
            gate_read, gate_write = os.pipe()
            runner_pid = os.fork()
            if runner_pid == 0:
                try:
                    os.close(ready_read)
                    os.close(gate_write)
                    self._lock = threading.Lock()
                    os.setsid()
                    null_descriptor = os.open(os.devnull, os.O_RDWR)
                    try:
                        for target in (0, 1, 2):
                            os.dup2(null_descriptor, target)
                    finally:
                        if null_descriptor > 2:
                            os.close(null_descriptor)
                    # Session creation is only the first acknowledgement; it
                    # does not prove the child can read its exact task.
                    _write_all(ready_write, b"R")
                    readable, _, _ = select.select([gate_read], [], [], 2.0)
                    if not readable or os.read(gate_read, 1) != b"G":
                        os._exit(0)
                    os.close(gate_read)
                    gate_read = None
                    self._runner_post_gate_ready(
                        controller_id, runner_pid=os.getpid()
                    )
                    self._crash_point("file_watchdog_child_after_post_gate_validation")
                    _write_all(ready_write, b"A")
                    os.close(ready_write)
                    ready_write = None
                    self._runner_loop(controller_id)
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            os.close(ready_write)
            ready_write = None
            os.close(gate_read)
            gate_read = None
            readable, _, _ = select.select([ready_read], [], [], 2.0)
            if not readable or os.read(ready_read, 1) != b"R":
                raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_UNAVAILABLE")
            handed_off = True
            return runner_pid, gate_write, ready_read
        except GcpSupervisorError:
            raise
        except OSError:
            raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_UNAVAILABLE") from None
        finally:
            if ready_read is not None and not handed_off:
                with suppress(OSError):
                    os.close(ready_read)
            if ready_write is not None:
                with suppress(OSError):
                    os.close(ready_write)
            if gate_read is not None:
                with suppress(OSError):
                    os.close(gate_read)
            if not handed_off and gate_write is not None:
                with suppress(OSError):
                    os.close(gate_write)
            if runner_pid is not None and not handed_off:
                self._kill_runner(runner_pid)

    def resume_runner(self, *, controller_id: str) -> int:
        """Durably replace a dead runner only with the same sealed executor."""

        with self._exclusive() as root:
            path = self._path_for(root, controller_id)
            if not self._repair_partial_tail_locked(path, root):
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_MISSING")
            events = self._read_locked(root, controller_id)
            latest = events[-1]
            if latest.state not in {
                "RUNNER_READY",
                "DISK_BOUND",
                "PREBIND_RETRY_PENDING",
                "RETRY_PENDING",
                "PREBIND_CLEANUP_INTENT",
                "CLEANUP_INTENT",
            }:
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_UNAVAILABLE")
            now = self._runner_now_fn()
            if latest.core_terminal_fenced_at is not None:
                settled = self._settle_core_terminal_fence_locked(
                    root, events, now=now
                )
                if settled.state == "CLEANUP_NOT_REQUIRED":
                    raise GcpSupervisorError("FILE_WATCHDOG_TASK_UNAVAILABLE")
                raise GcpSupervisorError("FILE_WATCHDOG_CORE_TERMINAL_PENDING")
            if latest.watchdog_cleanup_deadline_at is not None and (
                now >= _parse_timestamp(latest.watchdog_cleanup_deadline_at)
            ):
                self._orphan_for_journal_budget_locked(
                    root,
                    events,
                    binding=latest.binding,
                    now=now,
                    error_code="WATCHDOG_CLEANUP_HORIZON_EXPIRED",
                )
                raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_EXPIRED")
            self._validate_executor_config_for_event(latest)
            if (
                self._runner_resume_count(events)
                >= GCP_FILE_WATCHDOG_MAX_RUNNER_RESUMES
            ):
                self._orphan_for_journal_budget_locked(
                    root,
                    events,
                    binding=latest.binding,
                    now=now,
                    error_code="WATCHDOG_RUNNER_RESTART_EXHAUSTED",
                )
                raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_RESTART_EXHAUSTED")
            # A resume writes a same-state event.  Do not use the very last
            # journal slot for that nonterminal progress marker: reserve it
            # for an explicit fail-closed settlement.
            if len(events) >= (
                GCP_FILE_WATCHDOG_MAX_EVENTS
                - GCP_FILE_WATCHDOG_TERMINAL_EVENT_RESERVE
            ):
                self._orphan_for_journal_budget_locked(
                    root,
                    events,
                    binding=latest.binding,
                    now=now,
                    error_code="WATCHDOG_JOURNAL_HEADROOM_EXHAUSTED",
                )
                raise GcpSupervisorError("FILE_WATCHDOG_JOURNAL_HEADROOM_EXHAUSTED")
            expected_event_digest = latest.event_digest
        runner_pid, runner_gate, runner_ready = self._spawn_runner(controller_id)
        launched = False
        try:
            with self._exclusive() as root:
                path = self._path_for(root, controller_id)
                if not self._repair_partial_tail_locked(path, root):
                    raise GcpSupervisorError("FILE_WATCHDOG_TASK_MISSING")
                events = self._read_locked(root, controller_id)
                latest = events[-1]
                if latest.event_digest != expected_event_digest:
                    raise GcpSupervisorError("FILE_WATCHDOG_CONCURRENT_UPDATE")
                now = self._runner_now_fn()
                if latest.watchdog_cleanup_deadline_at is not None and (
                    now >= _parse_timestamp(latest.watchdog_cleanup_deadline_at)
                ):
                    self._orphan_for_journal_budget_locked(
                        root,
                        events,
                        binding=latest.binding,
                        now=now,
                        error_code="WATCHDOG_CLEANUP_HORIZON_EXPIRED",
                    )
                    raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_EXPIRED")
                if (
                    self._runner_resume_count(events)
                    >= GCP_FILE_WATCHDOG_MAX_RUNNER_RESUMES
                ):
                    self._orphan_for_journal_budget_locked(
                        root,
                        events,
                        binding=latest.binding,
                        now=now,
                        error_code="WATCHDOG_RUNNER_RESTART_EXHAUSTED",
                    )
                    raise GcpSupervisorError("FILE_WATCHDOG_RUNNER_RESTART_EXHAUSTED")
                self._append_locked(
                    root,
                    events,
                    state=latest.state,
                    binding=latest.binding,
                    now=now,
                    runner_process_id=runner_pid,
                    runner_ready_at=_timestamp(self._runner_now_fn()),
                    executor_config_digest=latest.executor_config_digest,
                )
            _write_all(runner_gate, b"G")
            self._await_runner_ready(runner_ready)
            launched = True
            return runner_pid
        finally:
            for descriptor in (runner_gate, runner_ready):
                with suppress(OSError):
                    os.close(descriptor)
            if not launched:
                self._kill_runner(runner_pid)

    def bind_disk_cleanup(
        self,
        binding: GcpSupervisorBinding,
        *,
        disk_cleanup_binding: GcpV2DiskCleanupBinding,
        now: datetime,
    ) -> GcpFileWatchdogEvent:
        """Durably attach one provider-observed disk before workload begins.

        Arming intentionally precedes create and therefore has no disk fact to
        record.  This one-way transition is the only place a full exact disk
        binding can enter the watchdog journal.  It may not replace a binding,
        and it cannot run once cleanup has begun.
        """

        disk_cleanup_binding = self._validate_disk_binding_for_supervisor(
            binding, disk_cleanup_binding
        )
        with self._exclusive() as root:
            path = self._path_for(root, binding.controller_id)
            if not self._repair_partial_tail_locked(path, root):
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_MISSING")
            events = self._read_locked(root, binding.controller_id)
            latest = events[-1]
            if latest.binding != binding:
                raise GcpSupervisorError("FILE_WATCHDOG_BINDING_MISMATCH")
            if latest.disk_cleanup_binding is not None:
                if latest.disk_cleanup_binding != disk_cleanup_binding:
                    raise GcpSupervisorError("FILE_WATCHDOG_DISK_BINDING_CHANGED")
                if latest.state not in {"DISK_BOUND", "RUNNER_READY"}:
                    raise GcpSupervisorError("FILE_WATCHDOG_TASK_UNAVAILABLE")
                return latest
            if latest.state != "RUNNER_READY":
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_UNAVAILABLE")
            return self._append_locked(
                root,
                events,
                state="DISK_BOUND",
                binding=binding,
                now=now,
                disk_cleanup_binding=disk_cleanup_binding,
            )

    @staticmethod
    def _cleanup_result_matches_binding(
        result: GcpWatchdogCleanupResult,
        binding: GcpSupervisorBinding,
    ) -> bool:
        disk_cleanup_binding = result.disk_cleanup_binding
        return (
            result.controller_id == binding.controller_id
            and result.request_digest == binding.request_digest
            and result.project_id == binding.project_id
            and result.region == binding.region
            and result.zone == binding.zone
            and result.instance_name == binding.instance_name
            and result.labels == binding.labels
            and result.disk_cleanup_binding == disk_cleanup_binding
            and result.disk_cleanup_outcome.binding_id
            == disk_cleanup_binding.binding_id
            and result.disk_cleanup_outcome.state == "ABSENCE_CONFIRMED"
            and result.instance_observation.state == "NOT_FOUND"
            and result.owned_inventory.instances == ()
        )

    @staticmethod
    def _cleanup_result_matches(
        result: GcpWatchdogCleanupResult, intent: GcpFileWatchdogEvent
    ) -> bool:
        if intent.disk_cleanup_binding is None:
            return False
        return (
            result.disk_cleanup_binding == intent.disk_cleanup_binding
            and FileGcpWatchdog._cleanup_result_matches_binding(
                result, intent.binding
            )
        )

    @staticmethod
    def _prebind_cleanup_result_matches(
        result: GcpWatchdogPrebindCleanupResult, event: GcpFileWatchdogEvent
    ) -> bool:
        binding = event.binding
        return (
            event.disk_cleanup_binding is None
            and result.controller_id == binding.controller_id
            and result.request_digest == binding.request_digest
            and result.project_id == binding.project_id
            and result.region == binding.region
            and result.zone == binding.zone
            and result.instance_name == binding.instance_name
            and result.labels == binding.labels
            and result.instance_observation.state == "NOT_FOUND"
            and result.owned_inventory.instances == ()
        )

    def _run_executor_bounded(
        self,
        intent: GcpFileWatchdogEvent,
        *,
        timeout_seconds: int,
        prebind: bool = False,
    ) -> GcpWatchdogCleanupResult | GcpWatchdogPrebindCleanupResult | None:
        """Run cleanup in a local child so a hung executor cannot strand it.

        The child receives no credentials from this class.  It receives only a
        canonical exact event and must persist any provider-facing result via
        its own injected cleanup boundary.  A timeout kills that child and the
        durable ``CLEANUP_INTENT`` remains recoverable on the next watchdog
        process.
        """

        if timeout_seconds < 1 or not hasattr(os, "fork"):
            return None
        read_descriptor: int | None = None
        write_descriptor: int | None = None
        session_read: int | None = None
        session_write: int | None = None
        child_pid: int | None = None
        worker_process_group: int | None = None

        def kill_direct(pid: int) -> None:
            with suppress(OSError):
                os.kill(pid, signal.SIGKILL)

        def kill_worker(pid: int) -> None:
            """End a known private session before the leader can be reaped."""

            nonlocal worker_process_group
            group = worker_process_group
            if group is None:
                kill_direct(pid)
                return
            try:
                os.killpg(group, signal.SIGKILL)
            except (AttributeError, OSError):
                kill_direct(pid)
            # Do not retain a PGID after its leader is reaped: a later
            # killpg could target an unrelated process if the numeric ID were
            # reused.  Every path below kills this private session first.
            worker_process_group = None

        try:
            # The watchdog's one bounded cleanup budget includes child setup;
            # a delayed session handshake must not buy the executor a second
            # full timeout window.
            deadline = time.monotonic() + float(timeout_seconds)

            def reap_bounded(pid: int) -> int | None:
                """Return a child status without extending the watchdog bound.

                SIGKILL normally makes a direct child immediately reapable,
                but that is not an authority to block forever in ``waitpid``.
                A non-reapable child remains a recoverable local timeout; its
                process group has already been killed and the parent can
                return to the durable journal on schedule.
                """

                while True:
                    try:
                        reaped_pid, status = os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        return None
                    except OSError:
                        return None
                    if reaped_pid == pid:
                        return status
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    time.sleep(min(0.01, remaining))

            read_descriptor, write_descriptor = os.pipe()
            session_read, session_write = os.pipe()
            child_pid = os.fork()
            if child_pid == 0:
                try:
                    # A separate session lets the parent bound grandchildren
                    # spawned by a misbehaving executor, not merely its direct
                    # child process.
                    os.setsid()
                    os.close(read_descriptor)
                    os.close(session_read)
                    _write_all(session_write, b"S")
                    os.close(session_write)
                    session_write = None
                    raw_result = (
                        self.executor.cleanup_prebind(
                            intent, timeout_seconds=timeout_seconds
                        )
                        if prebind
                        else self.executor.cleanup_exact(
                            intent, timeout_seconds=timeout_seconds
                        )
                    )
                    result = (
                        GcpWatchdogPrebindCleanupResult.model_validate(raw_result)
                        if prebind
                        else GcpWatchdogCleanupResult.model_validate(raw_result)
                    )
                    raw = canonical_json_bytes(_model_value(result))
                    if len(raw) > GCP_FILE_WATCHDOG_MAX_RESULT_BYTES:
                        os._exit(2)
                    _write_all(write_descriptor, raw)
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            os.close(write_descriptor)
            write_descriptor = None
            os.close(session_write)
            session_write = None
            setup_remaining = deadline - time.monotonic()
            if setup_remaining <= 0:
                kill_direct(child_pid)
                reap_bounded(child_pid)
                child_pid = None
                return None
            readable, _, _ = select.select(
                [session_read], [], [], setup_remaining
            )
            if not readable or os.read(session_read, 1) != b"S":
                kill_direct(child_pid)
                reap_bounded(child_pid)
                child_pid = None
                return None
            os.close(session_read)
            session_read = None
            # The child has acknowledged ``setsid``.  It is now safe to use
            # this exact PGID for the rest of this bounded invocation.
            worker_process_group = child_pid
            chunks: list[bytes] = []
            total = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    kill_worker(child_pid)
                    reap_bounded(child_pid)
                    child_pid = None
                    return None
                readable, _, _ = select.select(
                    [read_descriptor], [], [], remaining
                )
                if not readable:
                    kill_worker(child_pid)
                    reap_bounded(child_pid)
                    child_pid = None
                    return None
                chunk = os.read(
                    read_descriptor, GCP_FILE_WATCHDOG_MAX_RESULT_BYTES + 1 - total
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > GCP_FILE_WATCHDOG_MAX_RESULT_BYTES:
                    kill_worker(child_pid)
                    reap_bounded(child_pid)
                    child_pid = None
                    return None
            # A valid direct-child result is not enough: an executor can fork
            # a descendant, close the result pipe, and return immediately.
            # End that known private process group *before* reaping its
            # leader, which keeps the PGID unambiguous.
            kill_worker(child_pid)
            status = reap_bounded(child_pid)
            child_pid = None
            raw = b"".join(chunks)
            if (
                status is None
                or not isinstance(status, int)
                or not os.WIFEXITED(status)
                or os.WEXITSTATUS(status) != 0
                or not raw
            ):
                return None
            result = (
                GcpWatchdogPrebindCleanupResult.model_validate_json(raw)
                if prebind
                else GcpWatchdogCleanupResult.model_validate_json(raw)
            )
            if canonical_json_bytes(_model_value(result)) != raw:
                return None
            return result
        except (OSError, ValidationError, ValueError):
            return None
        finally:
            if child_pid is not None:
                kill_worker(child_pid)
                with suppress(OSError):
                    os.waitpid(child_pid, os.WNOHANG)
            if read_descriptor is not None:
                with suppress(OSError):
                    os.close(read_descriptor)
            if write_descriptor is not None:
                with suppress(OSError):
                    os.close(write_descriptor)
            if session_read is not None:
                with suppress(OSError):
                    os.close(session_read)
            if session_write is not None:
                with suppress(OSError):
                    os.close(session_write)

    def run_due(
        self, *, controller_id: str, now: datetime
    ) -> GcpFileWatchdogEvent:
        """Run one exact overdue cleanup task from an independent process."""

        with self._exclusive() as root:
            path = self._path_for(root, controller_id)
            if not self._repair_partial_tail_locked(path, root):
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_MISSING")
            events = self._read_locked(root, controller_id)
            latest = events[-1]
            if latest.state in {
                "CLEANUP_CONFIRMED",
                "CLEANUP_NOT_REQUIRED",
                "ORPHANED",
            }:
                return latest
            if latest.core_terminal_fenced_at is not None:
                return self._settle_core_terminal_fence_locked(root, events, now=now)
            if (
                latest.provider_runtime_deadline_at is None
                or latest.watchdog_cleanup_deadline_at is None
            ):
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_NOT_ACTIVE")
            now_value = _parse_timestamp(_timestamp(now))
            cleanup_deadline = _parse_timestamp(latest.watchdog_cleanup_deadline_at)
            # The persisted capability authorizes only cleanup *before* this
            # exact horizon.  Settle expiry from the journal before rebuilding
            # a factory-bound transport: at/after the deadline, construction
            # itself must not become a permanently blocking error.
            if now_value >= cleanup_deadline:
                return self._append_locked(
                    root,
                    events,
                    state="ORPHANED",
                    binding=latest.binding,
                    now=now,
                    error_code="WATCHDOG_CLEANUP_HORIZON_EXPIRED",
                )
            self._validate_executor_for_event(latest)
            prebind = latest.disk_cleanup_binding is None
            intent_state: GcpFileWatchdogState = (
                "PREBIND_CLEANUP_INTENT" if prebind else "CLEANUP_INTENT"
            )
            retry_state: GcpFileWatchdogState = (
                "PREBIND_RETRY_PENDING" if prebind else "RETRY_PENDING"
            )
            if latest.state in {"CLEANUP_INTENT", "PREBIND_CLEANUP_INTENT"}:
                # Another worker may still own this durable intent.  Do not
                # launch a second exact delete/reconcile before its bounded
                # lease expires; a crash is retried only after that horizon.
                intent_lease_until = _parse_timestamp(
                    latest.occurred_at
                ) + timedelta(seconds=latest.cleanup_timeout_seconds)
                if now_value < intent_lease_until:
                    return latest
                if now_value >= cleanup_deadline or (
                    latest.cleanup_attempts >= latest.max_cleanup_attempts
                ):
                    return self._append_locked(
                        root,
                        events,
                        state="ORPHANED",
                        binding=latest.binding,
                        now=now,
                        error_code=(
                            "WATCHDOG_CLEANUP_HORIZON_EXPIRED"
                            if now_value >= cleanup_deadline
                            else "WATCHDOG_ATTEMPTS_EXHAUSTED"
                        ),
                    )
                latest = self._append_locked(
                    root,
                    events,
                    state=retry_state,
                    binding=latest.binding,
                    now=now,
                    error_code="WATCHDOG_INTENT_LEASE_EXPIRED",
                )
                events = (*events, latest)
            allowed_states = (
                {"RUNNER_READY", "PREBIND_RETRY_PENDING"}
                if prebind
                else {"DISK_BOUND", "RETRY_PENDING"}
            )
            if latest.state not in allowed_states:
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_NOT_ACTIVE")
            provider_runtime_deadline_at = latest.provider_runtime_deadline_at
            if provider_runtime_deadline_at is None:
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_NOT_ACTIVE")
            if now_value < _parse_timestamp(provider_runtime_deadline_at):
                return latest
            if latest.cleanup_attempts >= latest.max_cleanup_attempts:
                return self._append_locked(
                    root,
                    events,
                    state="ORPHANED",
                    binding=latest.binding,
                    now=now,
                    error_code="WATCHDOG_ATTEMPTS_EXHAUSTED",
                )
            # An attempt needs one intent event and at least one durable
            # result/retry settlement.  If the bounded journal cannot hold
            # both, settle explicitly rather than leaving a live task which
            # no restart can advance.
            if len(events) >= GCP_FILE_WATCHDOG_MAX_EVENTS - 2:
                return self._orphan_for_journal_budget_locked(
                    root,
                    events,
                    binding=latest.binding,
                    now=now,
                    error_code="WATCHDOG_JOURNAL_HEADROOM_EXHAUSTED",
                )
            remaining_seconds = int((cleanup_deadline - now_value).total_seconds())
            timeout_seconds = min(latest.cleanup_timeout_seconds, remaining_seconds)
            if timeout_seconds < 1:
                return self._append_locked(
                    root,
                    events,
                    state="ORPHANED",
                    binding=latest.binding,
                    now=now,
                    error_code="WATCHDOG_CLEANUP_HORIZON_EXPIRED",
                )
            intent = self._append_locked(
                root,
                events,
                state=intent_state,
                binding=latest.binding,
                now=now,
                cleanup_attempts=latest.cleanup_attempts + 1,
            )
            self._crash_point("file_watchdog_after_cleanup_intent")
        # Re-read immediately before a worker can be forked.  Core terminal
        # reconciliation treats a durable intent as an exclusion lease (see
        # ``reconcile_core_terminal``), so this local check plus that durable
        # state prevents a concurrent core confirmation from racing a raw
        # provider call after it has won the journal.
        with self._exclusive() as root:
            path = self._path_for(root, controller_id)
            if not self._repair_partial_tail_locked(path, root):
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_MISSING")
            events = self._read_locked(root, controller_id)
            latest = events[-1]
            if latest.event_digest != intent.event_digest:
                if latest.state in {
                    "CLEANUP_CONFIRMED",
                    "CLEANUP_NOT_REQUIRED",
                    "ORPHANED",
                }:
                    return latest
                raise GcpSupervisorError("FILE_WATCHDOG_CONCURRENT_UPDATE")
            self._validate_executor_for_event(intent)
        result = self._run_executor_bounded(
            intent, timeout_seconds=timeout_seconds, prebind=prebind
        )
        valid_bound = isinstance(result, GcpWatchdogCleanupResult) and (
            self._cleanup_result_matches(result, intent)
        )
        valid_prebind = isinstance(result, GcpWatchdogPrebindCleanupResult) and (
            self._prebind_cleanup_result_matches(result, intent)
        )
        with self._exclusive() as root:
            path = self._path_for(root, controller_id)
            if not self._repair_partial_tail_locked(path, root):
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_MISSING")
            events = self._read_locked(root, controller_id)
            latest = events[-1]
            if latest.event_digest != intent.event_digest:
                # A core-side terminal reconciliation may have won while the
                # isolated worker completed.  Its authoritative terminal
                # event takes precedence; never overwrite it or relaunch.
                if latest.state in {
                    "CLEANUP_CONFIRMED",
                    "CLEANUP_NOT_REQUIRED",
                    "ORPHANED",
                }:
                    return latest
                raise GcpSupervisorError("FILE_WATCHDOG_CONCURRENT_UPDATE")
            if valid_bound and isinstance(result, GcpWatchdogCleanupResult):
                return self._append_locked(
                    root,
                    events,
                    state="CLEANUP_CONFIRMED",
                    binding=latest.binding,
                    now=now,
                    cleanup_result=result,
                )
            if valid_prebind and isinstance(result, GcpWatchdogPrebindCleanupResult):
                # Exact instance absence is useful recovery evidence, but it
                # cannot prove an unobserved disk absent.  Preserve that
                # unresolved state explicitly rather than inferring a name.
                return self._append_locked(
                    root,
                    events,
                    state="ORPHANED",
                    binding=latest.binding,
                    now=now,
                    prebind_cleanup_result=result,
                    error_code="WATCHDOG_DISK_BINDING_UNRESOLVED",
                )
            state: GcpFileWatchdogState = (
                "ORPHANED"
                if latest.cleanup_attempts >= latest.max_cleanup_attempts
                else (
                    "PREBIND_RETRY_PENDING"
                    if prebind
                    else "RETRY_PENDING"
                )
            )
            return self._append_locked(
                root,
                events,
                state=state,
                binding=latest.binding,
                now=now,
                error_code=(
                    "WATCHDOG_PREBIND_CLEANUP_UNCONFIRMED"
                    if prebind
                    else "WATCHDOG_EXACT_CLEANUP_UNCONFIRMED"
                ),
            )

    def reconcile_core_terminal(
        self,
        binding: GcpSupervisorBinding,
        *,
        now: datetime,
        reason_code: str,
    ) -> GcpFileWatchdogEvent | None:
        """Stop an armed task after authoritative core-side absence.

        This is a local journal-only convergence path.  It never invokes the
        executor, constructs a transport, or fabricates a disk absence result.
        A terminal core record wins over a still-active watchdog task.
        """

        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,47}", reason_code) is None:
            reason_code = "CORE_CLEANUP_CONFIRMED"
        with self._exclusive() as root:
            path = self._path_for(root, binding.controller_id)
            if not self._repair_partial_tail_locked(path, root):
                return None
            events = self._read_locked(root, binding.controller_id)
            latest = events[-1]
            if latest.binding != binding:
                raise GcpSupervisorError("FILE_WATCHDOG_BINDING_MISMATCH")
            if latest.state in {
                "CLEANUP_INTENT",
                "PREBIND_CLEANUP_INTENT",
            }:
                # The bounded worker owns this exact durable intent. Record a
                # local terminal fence instead of overwriting it: a worker
                # already past its provider boundary gets at most this intent
                # lease, while any restart is prevented from acquiring a new
                # executor/provider.
                if latest.core_terminal_fenced_at is None:
                    latest = self._append_locked(
                        root,
                        events,
                        state=latest.state,
                        binding=binding,
                        now=now,
                        core_terminal_fenced_at=_timestamp(now),
                        error_code=reason_code,
                    )
                    events = (*events, latest)
                return self._settle_core_terminal_fence_locked(
                    root, events, now=now
                )
            if latest.state in {"CLEANUP_CONFIRMED", "CLEANUP_NOT_REQUIRED"}:
                return latest
            return self._append_locked(
                root,
                events,
                state="CLEANUP_NOT_REQUIRED",
                binding=binding,
                now=now,
                error_code=reason_code,
            )

    def confirmed_cleanup_result(
        self, binding: GcpSupervisorBinding
    ) -> GcpWatchdogCleanupResult | None:
        """Read one persisted exact cleanup result for core-journal recovery."""

        with self._exclusive() as root:
            path = self._path_for(root, binding.controller_id)
            if not self._repair_partial_tail_locked(path, root):
                return None
            events = self._read_locked(root, binding.controller_id)
            latest = events[-1]
            if latest.binding != binding:
                raise GcpSupervisorError("FILE_WATCHDOG_BINDING_MISMATCH")
            if latest.state != "CLEANUP_CONFIRMED":
                return None
            if latest.cleanup_result is None or not self._cleanup_result_matches(
                latest.cleanup_result, latest
            ):
                raise GcpSupervisorError("FILE_WATCHDOG_CLEANUP_RESULT_INVALID")
            return latest.cleanup_result

    def durable_disk_cleanup_binding(
        self, binding: GcpSupervisorBinding
    ) -> GcpV2DiskCleanupBinding:
        """Load the one durable post-create disk binding for exact recovery.

        This is intentionally a journal read, not a provider discovery.  A
        recovery authorization can target only the binding that the active
        watchdog fsync-recorded before work began; a merely label-matching
        alternate disk never becomes eligible.
        """

        with self._exclusive() as root:
            path = self._path_for(root, binding.controller_id)
            if not self._repair_partial_tail_locked(path, root):
                raise GcpSupervisorError("FILE_WATCHDOG_TASK_MISSING")
            events = self._read_locked(root, binding.controller_id)
            latest = events[-1]
            if latest.binding != binding or latest.disk_cleanup_binding is None:
                raise GcpSupervisorError("FILE_WATCHDOG_DISK_BINDING_MISSING")
            return self._validate_disk_binding_for_supervisor(
                binding, latest.disk_cleanup_binding
            )


class InMemoryGcpKillSwitch:
    """A local test double; durable file-backed control is added separately."""

    def __init__(self, *, killed: bool = False) -> None:
        self.killed = killed
        self.checks = 0

    def is_killed(self, binding: GcpSupervisorBinding) -> bool:
        del binding
        self.checks += 1
        return self.killed


class FileGcpKillSwitch:
    """Read one exact, no-follow local kill marker without directory discovery.

    The file is deliberately controller-addressed rather than discovered by
    hostname, glob, or account-wide state.  Any malformed, retargeted, or
    concurrently changed marker is an unavailable safety control and must
    therefore fail closed in ``GcpLifecycleSupervisor``.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _checked_root(self) -> Path:
        if not self.root.is_absolute():
            raise GcpSupervisorError("KILL_SWITCH_PATH_INVALID")
        try:
            metadata = self.root.lstat()
        except OSError:
            raise GcpSupervisorError("KILL_SWITCH_UNAVAILABLE") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mode & 0o022
        ):
            raise GcpSupervisorError("KILL_SWITCH_PATH_UNSAFE")
        return self.root

    @staticmethod
    def _path_for(root: Path, controller_id: str) -> Path:
        if _CONTROLLER_RE.fullmatch(controller_id) is None:
            raise GcpSupervisorError("KILL_SWITCH_CONTROLLER_INVALID")
        return root / f"{controller_id}.kill-v2.json"

    def _read_marker(self, path: Path) -> GcpKillSwitchRecord | None:
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    path,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
            except FileNotFoundError:
                return None
            initial = os.fstat(descriptor)
            if (
                not stat.S_ISREG(initial.st_mode)
                or initial.st_size <= 0
                or initial.st_size > GCP_KILL_SWITCH_MAX_BYTES
                or initial.st_mode & 0o022
            ):
                raise GcpSupervisorError("KILL_SWITCH_MARKER_UNSAFE")
            raw = os.read(descriptor, GCP_KILL_SWITCH_MAX_BYTES + 1)
            final = os.fstat(descriptor)
            if (
                len(raw) > GCP_KILL_SWITCH_MAX_BYTES
                or final.st_ino != initial.st_ino
                or final.st_size != initial.st_size
                or final.st_size != len(raw)
                or final.st_mtime_ns != initial.st_mtime_ns
            ):
                raise GcpSupervisorError("KILL_SWITCH_MARKER_CHANGED")
            marker = parse_gcp_kill_switch_json(raw)
            if canonical_gcp_kill_switch_bytes(marker) != raw:
                raise GcpSupervisorError("KILL_SWITCH_MARKER_NONCANONICAL")
            return marker
        except GcpSupervisorError:
            raise
        except (OSError, ValueError):
            raise GcpSupervisorError("KILL_SWITCH_UNAVAILABLE") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def is_killed(self, binding: GcpSupervisorBinding) -> bool:
        marker = self._read_marker(
            self._path_for(self._checked_root(), binding.controller_id)
        )
        if marker is None:
            return False
        if (
            marker.approval_digest != binding.approval_digest
            or marker.request_digest != binding.request_digest
            or marker.controller_id != binding.controller_id
        ):
            raise GcpSupervisorError("KILL_SWITCH_BINDING_MISMATCH")
        return True


class GcpLifecycleSupervisor:
    """Bridge exact approval/watchdog state into the existing core controller."""

    # The controller reads this marker to prohibit its generic factory route
    # for the real v2 supervisor.  Legacy fake-only supervisors intentionally
    # do not carry it, preserving frozen offline regression coverage.
    requires_v2_capability_factories = True

    def __init__(
        self,
        *,
        approval: GcpExecutionApproval,
        quote_basis: GcpReadOnlyQuoteBasis,
        cost_guard: GcpCostCleanupGuard,
        activation_deadline: GcpV2ActivationDeadline,
        startup_projection: GcpV2StartupProjection,
        journal: GcpSupervisorJournal,
        watchdog: GcpWatchdog,
        kill_switch: GcpKillSwitch,
    ) -> None:
        self.approval = _strict_approval(approval)
        self.quote_basis = quote_basis
        self.cost_guard = cost_guard
        self.activation_deadline = activation_deadline
        self.startup_projection = startup_projection
        try:
            projection_digest = gcp_v2_startup_projection_digest(startup_projection)
        except (AttributeError, TypeError, ValueError):
            raise GcpSupervisorError("STARTUP_PROJECTION_INVALID") from None
        if (
            projection_digest != self.approval.startup_projection_digest
            or startup_projection.execution_payload_digest
            != self.approval.execution_payload_digest
        ):
            raise GcpSupervisorError("STARTUP_PROJECTION_BINDING_MISMATCH")
        self.journal = journal
        self.watchdog = watchdog
        if type(watchdog) is FileGcpWatchdog and _same_managed_root(
            journal.root, watchdog.root
        ):
            raise GcpSupervisorError("WATCHDOG_JOURNAL_ROOT_OVERLAP")
        self.kill_switch = kill_switch
        self._issued_capabilities: dict[str, GcpV2MutationCapability] = {}
        self._consumed_capabilities: set[str] = set()

    def _binding(self, record: GcpLeaseRecord) -> GcpSupervisorBinding:
        return gcp_supervisor_binding(self.approval, record)

    def _file_watchdog(self) -> FileGcpWatchdog | None:
        """Return only the concrete durable watchdog, never a protocol fake."""

        return self.watchdog if type(self.watchdog) is FileGcpWatchdog else None

    def _reconcile_file_watchdog_terminal(
        self, record: GcpLeaseRecord, *, now: datetime, reason_code: str
    ) -> None:
        watchdog = self._file_watchdog()
        if watchdog is not None:
            event = watchdog.reconcile_core_terminal(
                self._binding(record), now=now, reason_code=reason_code
            )
            if event is not None and event.state not in {
                "CLEANUP_CONFIRMED",
                "CLEANUP_NOT_REQUIRED",
            }:
                # The core is authoritative, but a previously spawned exact
                # cleanup worker may still be inside its one bounded intent
                # lease. Do not report cross-journal convergence until the
                # durable fence has settled that sidecar without another
                # provider boundary.
                raise GcpSupervisorError("FILE_WATCHDOG_CORE_TERMINAL_PENDING")

    def _validate_bound_inputs(
        self,
        record: GcpLeaseRecord,
        *,
        preflight: GcpExecutionPreflight,
        now: datetime,
        activation_edge: bool,
    ) -> None:
        """Validate exact bindings, with freshness only at the activation edge.

        The frozen v1 arm/quote timestamps are canonical preflight evidence and
        must still be fresh before activation.  Once a capability has been
        durably activated, its explicit v2 provider-runtime horizon—not the
        historical arm deadline—bounds post-create work.
        """

        try:
            if activation_edge:
                validate_gcp_execution_preflight_freshness(preflight, now=now)
            validate_gcp_execution_approval(
                self.approval,
                preflight=preflight,
                quote_basis=self.quote_basis,
                cost_guard=self.cost_guard,
                activation_deadline=self.activation_deadline,
                startup_projection=self.startup_projection,
                now=now,
                enforce_freshness=activation_edge,
                enforce_controller_deadline=False,
            )
            validate_gcp_cost_cleanup_guard(
                self.cost_guard,
                preflight=preflight,
                quote_basis=self.quote_basis,
                now=now,
                record=record,
                enforce_freshness=activation_edge,
                enforce_controller_deadline=False,
            )
        except GcpExecutionError as error:
            raise GcpSupervisorError(str(error)) from None

    def _assert_not_killed(
        self, binding: GcpSupervisorBinding, *, now: datetime
    ) -> None:
        try:
            killed = self.kill_switch.is_killed(binding)
        except GcpSupervisorError:
            raise
        except BaseException:
            raise GcpSupervisorError("KILL_SWITCH_UNAVAILABLE") from None
        if killed:
            self.journal.advance(
                binding,
                state="KILLED",
                now=now,
                error_code="KILL_SWITCH_ACTIVE",
            )
            raise GcpSupervisorError("KILL_SWITCH_ACTIVE")

    def reserve(
        self,
        record: GcpLeaseRecord,
        *,
        preflight: GcpExecutionPreflight,
        now: datetime,
    ) -> None:
        self._validate_bound_inputs(
            record, preflight=preflight, now=now, activation_edge=True
        )
        self.journal.reserve(self._binding(record), now=now)

    def arm_watchdog(
        self,
        record: GcpLeaseRecord,
        *,
        preflight: GcpExecutionPreflight,
        now: datetime,
    ) -> None:
        self._validate_bound_inputs(
            record, preflight=preflight, now=now, activation_edge=True
        )
        binding = self._binding(record)
        self.journal.advance(binding, state="ARM_CONSUMED", now=now)
        self._assert_not_killed(binding, now=now)
        try:
            configure_lease = getattr(self.watchdog, "configure_lease", None)
            if callable(configure_lease):
                configure_lease(record)
            raw_receipt = self.watchdog.arm(binding, approval=self.approval, now=now)
            receipt = GcpWatchdogReceipt.model_validate_json(
                canonical_gcp_watchdog_receipt_bytes(raw_receipt)
            )
        except GcpSupervisorError:
            raise
        except (AttributeError, ValidationError, ValueError, TypeError):
            raise GcpSupervisorError("WATCHDOG_RECEIPT_INVALID") from None
        if (
            receipt.approval_digest != binding.approval_digest
            or receipt.activation_deadline_digest
            != binding.activation_deadline_digest
            or receipt.request_digest != binding.request_digest
            or receipt.startup_projection_digest
            != binding.startup_projection_digest
            or receipt.execution_payload_digest != binding.execution_payload_digest
            or receipt.controller_id != binding.controller_id
            or receipt.armed_at != _timestamp(now)
            or receipt.expires_at != binding.watchdog_deadline_at
            or _parse_timestamp(receipt.armed_at) > _parse_timestamp(
                receipt.expires_at
            )
        ):
            raise GcpSupervisorError("WATCHDOG_BINDING_MISMATCH")
        self.journal.advance(
            binding,
            state="WATCHDOG_READY",
            now=now,
            watchdog_receipt_digest=gcp_watchdog_receipt_digest(receipt),
        )

    def assert_create_permitted(
        self,
        record: GcpLeaseRecord,
        *,
        preflight: GcpExecutionPreflight,
        now: datetime,
    ) -> GcpV2MutationCapability:
        self._validate_bound_inputs(
            record, preflight=preflight, now=now, activation_edge=True
        )
        if self._consumed_capabilities:
            raise GcpSupervisorError("MUTATION_CAPABILITY_ALREADY_CONSUMED")
        binding = self._binding(record)
        event = self.journal.load(binding.controller_id)
        if event.binding != binding or event.state not in {
            "WATCHDOG_READY",
            "CREATE_INTENT",
        }:
            raise GcpSupervisorError("WATCHDOG_NOT_READY")
        if event.watchdog_receipt_digest is None:
            raise GcpSupervisorError("WATCHDOG_RECEIPT_MISSING")
        self._assert_not_killed(binding, now=now)
        try:
            capability = issue_gcp_v2_mutation_capability(
                contract=self.activation_deadline,
                approval_digest=binding.approval_digest,
                now=now,
            )
        except GcpExecutionError as error:
            raise GcpSupervisorError(str(error)) from None
        self._issued_capabilities[capability.capability_id] = capability
        return capability

    def consume_mutation_capability(
        self,
        record: GcpLeaseRecord,
        *,
        capability: object,
        now: datetime,
    ) -> GcpV2ActivatedMutationProof:
        """Consume one exact capability immediately before a transport edge."""

        try:
            parsed = _strict_mutation_capability(
                GcpV2MutationCapability.model_validate(capability)
            )
        except (GcpSupervisorError, ValidationError):
            raise GcpSupervisorError("MUTATION_CAPABILITY_INVALID") from None
        binding = self._binding(record)
        expected = self._issued_capabilities.get(parsed.capability_id)
        if expected != parsed or parsed.capability_id in self._consumed_capabilities:
            raise GcpSupervisorError("MUTATION_CAPABILITY_UNAVAILABLE")
        if (
            parsed.approval_digest != binding.approval_digest
            or parsed.activation_contract_digest
            != binding.activation_deadline_digest
            or parsed.request_digest != binding.request_digest
            or parsed.startup_projection_digest
            != binding.startup_projection_digest
            or parsed.execution_payload_digest != binding.execution_payload_digest
            or parsed.project_id != binding.project_id
            or parsed.region != binding.region
            or parsed.zone != binding.zone
            or parsed.instance_name != binding.instance_name
            or parsed.labels != binding.labels
        ):
            raise GcpSupervisorError("MUTATION_CAPABILITY_BINDING_MISMATCH")
        event = self.journal.load(binding.controller_id)
        if event.binding != binding or event.state != "WATCHDOG_READY":
            raise GcpSupervisorError("MUTATION_CAPABILITY_STATE_INVALID")
        now_value = _parse_timestamp(_timestamp(now))
        if now_value >= _parse_timestamp(parsed.provider_runtime_deadline_at):
            raise GcpSupervisorError("MUTATION_CAPABILITY_EXPIRED")
        try:
            raw_receipt = self.watchdog.activate(
                binding,
                approval=self.approval,
                capability=parsed,
                now=now,
            )
            receipt = GcpWatchdogActivationReceipt.model_validate_json(
                canonical_gcp_watchdog_activation_receipt_bytes(raw_receipt)
            )
        except GcpSupervisorError:
            raise
        except (AttributeError, ValidationError, ValueError, TypeError):
            raise GcpSupervisorError("WATCHDOG_ACTIVATION_RECEIPT_INVALID") from None
        if (
            receipt.approval_digest != binding.approval_digest
            or receipt.activation_deadline_digest
            != binding.activation_deadline_digest
            or receipt.request_digest != binding.request_digest
            or receipt.startup_projection_digest
            != binding.startup_projection_digest
            or receipt.execution_payload_digest != binding.execution_payload_digest
            or receipt.controller_id != binding.controller_id
            or receipt.capability_id != parsed.capability_id
            or receipt.activated_at != parsed.activated_at
            or receipt.provider_runtime_deadline_at
            != parsed.provider_runtime_deadline_at
            or receipt.watchdog_cleanup_deadline_at
            != parsed.watchdog_cleanup_deadline_at
        ):
            raise GcpSupervisorError("WATCHDOG_ACTIVATION_BINDING_MISMATCH")
        self._consumed_capabilities.add(parsed.capability_id)
        return GcpV2ActivatedMutationProof(
            capability=parsed,
            receipt=receipt,
            _seal=_MUTATION_ACTIVATION_PROOF_SEAL,
            _use=_MutationProofUse(),
        )

    def _active_capability(
        self, record: GcpLeaseRecord, *, now: datetime
    ) -> GcpV2MutationCapability:
        binding = self._binding(record)
        if len(self._consumed_capabilities) != 1:
            raise GcpSupervisorError("MUTATION_CAPABILITY_UNAVAILABLE")
        capability_id = next(iter(self._consumed_capabilities))
        capability = self._issued_capabilities.get(capability_id)
        if (
            capability is None
            or capability.controller_id != binding.controller_id
            or capability.request_digest != binding.request_digest
            or capability.startup_projection_digest
            != binding.startup_projection_digest
            or capability.execution_payload_digest != binding.execution_payload_digest
        ):
            raise GcpSupervisorError("MUTATION_CAPABILITY_UNAVAILABLE")
        now_value = _parse_timestamp(_timestamp(now))
        if now_value >= _parse_timestamp(capability.provider_runtime_deadline_at):
            raise GcpSupervisorError("PROVIDER_RUNTIME_DEADLINE_EXPIRED")
        if now_value >= _parse_timestamp(capability.watchdog_cleanup_deadline_at):
            raise GcpSupervisorError("WATCHDOG_DEADLINE_EXPIRED")
        return capability

    def create_intent(
        self,
        record: GcpLeaseRecord,
        *,
        preflight: GcpExecutionPreflight,
        now: datetime,
    ) -> None:
        self._validate_bound_inputs(
            record, preflight=preflight, now=now, activation_edge=True
        )
        self._active_capability(record, now=now)
        self.journal.advance(self._binding(record), state="CREATE_INTENT", now=now)

    def before_work(
        self,
        record: GcpLeaseRecord,
        *,
        preflight: GcpExecutionPreflight,
        now: datetime,
    ) -> None:
        binding = self._binding(record)
        self._active_capability(record, now=now)
        self._validate_bound_inputs(
            record, preflight=preflight, now=now, activation_edge=False
        )
        event = self.journal.load(binding.controller_id)
        if event.binding != binding or event.state != "CREATE_INTENT":
            raise GcpSupervisorError("SUPERVISOR_WORK_STATE_INVALID")
        self._assert_not_killed(binding, now=now)
        self.journal.advance(binding, state="WORKING", now=now)

    def bind_exact_disk_cleanup(
        self,
        record: GcpLeaseRecord,
        *,
        disk_cleanup_binding: object,
        now: datetime,
    ) -> None:
        """Persist the post-create exact disk fact before workload execution.

        The concrete file watchdog is already durable and active at this
        point.  A protocol fake deliberately has no activation-factory route,
        so direct fake-only controller tests retain their offline behavior.
        """

        watchdog = self._file_watchdog()
        if watchdog is None:
            return
        try:
            parsed = GcpV2DiskCleanupBinding.model_validate(disk_cleanup_binding)
            parsed = _strict_disk_cleanup_binding(parsed)
        except (GcpSupervisorError, ValidationError):
            raise GcpSupervisorError("WATCHDOG_DISK_BINDING_INVALID") from None
        watchdog.bind_disk_cleanup(
            self._binding(record), disk_cleanup_binding=parsed, now=now
        )

    def cleanup_pending(self, record: GcpLeaseRecord, *, now: datetime) -> None:
        self.journal.advance(self._binding(record), state="CLEANUP_PENDING", now=now)

    def cleanup_terminal(
        self,
        record: GcpLeaseRecord,
        *,
        confirmed: bool,
        error_code: str | None,
        now: datetime,
    ) -> None:
        binding = self._binding(record)
        orphan_report = None
        if not confirmed:
            orphan_report = issue_gcp_exact_orphan_report(
                binding,
                reason_code=error_code or "CLEANUP_UNCONFIRMED",
                now=now,
            )
        self.journal.advance(
            binding,
            state="CLEANUP_CONFIRMED" if confirmed else "ORPHANED",
            now=now,
            orphan_report=orphan_report,
            error_code=error_code,
        )
        if confirmed:
            self._reconcile_file_watchdog_terminal(
                record,
                now=now,
                reason_code="CORE_CLEANUP_CONFIRMED",
            )

    def block(
        self, record: GcpLeaseRecord, *, error_code: str, now: datetime
    ) -> None:
        self.journal.advance(
            self._binding(record), state="BLOCKED", now=now, error_code=error_code
        )

    def resume_cleanup(self, record: GcpLeaseRecord, *, now: datetime) -> None:
        binding = self._binding(record)
        event = self.journal.load(binding.controller_id)
        if event.binding != binding:
            raise GcpSupervisorError("SUPERVISOR_BINDING_MISMATCH")
        if record.state == "CLEANUP_CONFIRMED":
            self.journal.reconcile_core_terminal(
                binding,
                now=now,
                reason_code="CORE_CLEANUP_CONFIRMED",
            )
            return
        if event.state != "CLEANUP_CONFIRMED":
            self.journal.advance(binding, state="CLEANUP_PENDING", now=now)

    def validate_cleanup_recovery_authorization(
        self,
        record: GcpLeaseRecord,
        *,
        authorization: object,
        now: datetime,
    ) -> GcpCleanupRecoveryAuthorization:
        """Validate narrowly scoped cleanup authority without reusing create auth."""

        try:
            parsed = GcpCleanupRecoveryAuthorization.model_validate(authorization)
            validated = validate_gcp_cleanup_recovery_authorization(
                parsed,
                record=record,
                approval_digest=gcp_execution_approval_digest(self.approval),
                startup_projection_digest=self.approval.startup_projection_digest,
                execution_payload_digest=self.approval.execution_payload_digest,
                now=now,
                watchdog_cleanup_deadline_at=_parse_timestamp(
                    self.approval.watchdog_deadline_at
                ),
            )
            watchdog = self._file_watchdog()
            if watchdog is not None:
                durable_binding = watchdog.durable_disk_cleanup_binding(
                    self._binding(record)
                )
                if validated.disk_cleanup_binding != durable_binding:
                    raise GcpSupervisorError(
                        "CLEANUP_RECOVERY_DURABLE_DISK_BINDING_MISMATCH"
                    )
            return validated
        except (GcpExecutionError, ValidationError) as error:
            raise GcpSupervisorError(str(error)) from None

    def reconcile_no_provider_mutation(
        self, record: GcpLeaseRecord, *, now: datetime
    ) -> None:
        """Settle PREPARED/ARM_CONSUMED crashes without a transport boundary."""

        if (
            record.state
            not in {"PREPARED", "ARM_CONSUMED", "CLEANUP_CONFIRMED"}
            or record.provider_mutation_attempted
            or record.provider_mutation_ambiguous
            or record.provider_operation_id is not None
            or record.provider_operation_name is not None
            or record.provider_operation_kind is not None
        ):
            raise GcpSupervisorError("NO_PROVIDER_RECONCILIATION_INELIGIBLE")
        self.journal.reconcile_core_terminal(
            self._binding(record),
            now=now,
            reason_code="NO_PROVIDER_MUTATION",
        )
        self._reconcile_file_watchdog_terminal(
            record,
            now=now,
            reason_code="NO_PROVIDER_MUTATION",
        )

    def reconcile_core_terminal_sidecar(
        self, record: GcpLeaseRecord, *, now: datetime
    ) -> None:
        """Finish a sidecar left behind after authoritative core absence."""

        if record.state != "CLEANUP_CONFIRMED" or not record.cleanup_confirmed:
            raise GcpSupervisorError("CORE_TERMINAL_RECONCILIATION_INELIGIBLE")
        self.journal.reconcile_core_terminal(
            self._binding(record),
            now=now,
            reason_code="CORE_CLEANUP_CONFIRMED",
        )
        self._reconcile_file_watchdog_terminal(
            record,
            now=now,
            reason_code="CORE_CLEANUP_CONFIRMED",
        )

    def confirmed_watchdog_cleanup(
        self, record: GcpLeaseRecord
    ) -> GcpWatchdogCleanupResult | None:
        """Return a durable exact watchdog result suitable for local recovery."""

        watchdog = self._file_watchdog()
        if watchdog is None:
            return None
        result = watchdog.confirmed_cleanup_result(self._binding(record))
        if result is None:
            return None
        binding = self._binding(record)
        if not FileGcpWatchdog._cleanup_result_matches_binding(result, binding):
            raise GcpSupervisorError("WATCHDOG_CLEANUP_RESULT_MISMATCH")
        return result


def gcp_execution_supervisor_contract_schemas() -> dict[str, dict[str, Any]]:
    """Return additive v0.2 schemas without rewriting frozen v1 assets."""

    models: tuple[tuple[str, type[GcpExecutionModel], str], ...] = (
        (
            "gcp-execution-approval.schema.json",
            GcpExecutionApproval,
            GCP_EXECUTION_APPROVAL_SCHEMA_ID,
        ),
        (
            "gcp-supervisor-binding.schema.json",
            GcpSupervisorBinding,
            GCP_SUPERVISOR_BINDING_SCHEMA_ID,
        ),
        (
            "gcp-supervisor-event.schema.json",
            GcpSupervisorJournalEvent,
            GCP_SUPERVISOR_EVENT_SCHEMA_ID,
        ),
        (
            "gcp-watchdog-receipt.schema.json",
            GcpWatchdogReceipt,
            GCP_WATCHDOG_RECEIPT_SCHEMA_ID,
        ),
        (
            "gcp-watchdog-activation-receipt.schema.json",
            GcpWatchdogActivationReceipt,
            GCP_WATCHDOG_ACTIVATION_RECEIPT_SCHEMA_ID,
        ),
        (
            "gcp-file-watchdog-event.schema.json",
            GcpFileWatchdogEvent,
            GCP_FILE_WATCHDOG_EVENT_SCHEMA_ID,
        ),
        (
            "gcp-watchdog-cleanup-result.schema.json",
            GcpWatchdogCleanupResult,
            GCP_WATCHDOG_CLEANUP_RESULT_SCHEMA_ID,
        ),
        (
            "gcp-exact-orphan-report.schema.json",
            GcpExactOrphanReport,
            GCP_EXACT_ORPHAN_REPORT_SCHEMA_ID,
        ),
        (
            "gcp-kill-switch.schema.json",
            GcpKillSwitchRecord,
            GCP_KILL_SWITCH_SCHEMA_ID,
        ),
    )
    output: dict[str, dict[str, Any]] = {}
    for filename, model, schema_id in models:
        schema = model.model_json_schema()
        schema["$id"] = schema_id
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        output[filename] = schema
    return output
