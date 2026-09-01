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
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Protocol, Self

from pydantic import Field, StringConstraints, ValidationError, model_validator

from inferdrome.deployment.gcp import (
    GcpProjectId,
    GcpRegion,
    GcpResourceName,
    GcpTimestamp,
    GcpZone,
)
from inferdrome.deployment.gcp_lifecycle import (
    _CONTROLLER_RE,
    GcpExecutionError,
    GcpExecutionLabels,
    GcpExecutionModel,
    GcpExecutionPreflight,
    GcpLeaseRecord,
    _parse_timestamp,
    _preflight_json,
    _timestamp,
    _write_all,
    gcp_cost_quote_digest,
    gcp_execution_request_digest,
    validate_gcp_a2_profile,
)
from inferdrome.deployment.spec import CostCeiling
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes
from inferdrome.domain.ids import Sha256Digest

GCP_EXECUTION_APPROVAL_SCHEMA_VERSION: Final = "inferdrome.gcp-execution-approval.v2"
GCP_SUPERVISOR_BINDING_SCHEMA_VERSION: Final = "inferdrome.gcp-supervisor-binding.v2"
GCP_SUPERVISOR_EVENT_SCHEMA_VERSION: Final = "inferdrome.gcp-supervisor-event.v2"
GCP_WATCHDOG_RECEIPT_SCHEMA_VERSION: Final = "inferdrome.gcp-watchdog-receipt.v2"
GCP_EXECUTION_APPROVAL_SCHEMA_ID: Final = "urn:inferdrome:gcp-execution-approval:v2"
GCP_SUPERVISOR_BINDING_SCHEMA_ID: Final = "urn:inferdrome:gcp-supervisor-binding:v2"
GCP_SUPERVISOR_EVENT_SCHEMA_ID: Final = "urn:inferdrome:gcp-supervisor-event:v2"
GCP_WATCHDOG_RECEIPT_SCHEMA_ID: Final = "urn:inferdrome:gcp-watchdog-receipt:v2"
GCP_SUPERVISOR_CONFIRMATION: Final = "APPROVE_GCP_V2_EXACT"
GCP_SUPERVISOR_MAX_EVENTS: Final = 64
GCP_SUPERVISOR_MAX_EVENT_BYTES: Final = 262_144

_OPERATOR_ID_RE = re.compile(r"^operator-[a-z0-9][a-z0-9_-]{2,61}$")
_WATCHDOG_ID_RE = re.compile(r"^watchdog-[a-z][a-z0-9-]{2,61}$")
_EVENT_PATH_RE = re.compile(
    r"^(ctl-[a-z0-9]{8,24})\.safety-v2\.events\.jsonl$"
)

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
    """Exact human approval inputs required before any future SDK gate."""

    schema_version: Literal["inferdrome.gcp-execution-approval.v2"]
    approval_kind: Literal["exact_operator_execution_approval"]
    operator_identity: GcpOperatorIdentity
    operator_confirmation: Literal["APPROVE_GCP_V2_EXACT"]
    provider: Literal["gcp-compute-engine"]
    plan_id: Sha256Digest
    plan_sha256: Sha256Digest
    arm_id: Sha256Digest
    request_digest: Sha256Digest
    quote_basis_digest: Sha256Digest
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
    controller_deadline_at: GcpTimestamp
    hard_cost_ceiling: CostCeiling
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
        deadline = _parse_timestamp(self.controller_deadline_at)
        if expires <= issued or deadline < expires:
            raise ValueError("approval times are inconsistent")
        if (
            self.hard_cost_ceiling.currency != "USD"
            or self.hard_cost_ceiling.hard_limit is not True
            or self.hard_cost_ceiling.estimate_basis != "controller_estimate"
        ):
            raise ValueError("approval hard cost ceiling is inconsistent")
        return self


class GcpExecutionApproval(GcpExecutionApprovalPayload):
    """An immutable exact approval with a domain-separated identity."""

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
    controller_id: Annotated[
        str, StringConstraints(pattern=r"^ctl-[a-z0-9]{8,24}$")
    ]
    arm_id: Sha256Digest
    plan_id: Sha256Digest
    request_digest: Sha256Digest
    project_id: GcpProjectId
    region: GcpRegion
    zone: GcpZone
    instance_name: Annotated[
        str, StringConstraints(pattern=r"^inferdrome-ctl-[a-z0-9]{8,24}$")
    ]
    labels: GcpExecutionLabels
    controller_deadline_at: GcpTimestamp


class GcpWatchdogReceipt(GcpExecutionModel):
    """A future external watchdog's exact, independently-ready receipt."""

    schema_version: Literal["inferdrome.gcp-watchdog-receipt.v2"]
    watchdog_id: GcpWatchdogId
    approval_digest: Sha256Digest
    request_digest: Sha256Digest
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


class GcpSupervisorJournalEvent(GcpExecutionModel):
    """One bounded, hash-linked v0.2 supervisor transition."""

    schema_version: Literal["inferdrome.gcp-supervisor-event.v2"]
    sequence: int = Field(strict=True, ge=0, le=GCP_SUPERVISOR_MAX_EVENTS - 1)
    state: GcpSupervisorState
    binding: GcpSupervisorBinding
    occurred_at: GcpTimestamp
    previous_event_digest: Sha256Digest | None
    watchdog_receipt_digest: Sha256Digest | None = None
    error_code: GcpSupervisorErrorCode | None = None
    event_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_event_identity(self) -> Self:
        if self.state == "WATCHDOG_READY" and self.watchdog_receipt_digest is None:
            raise ValueError("watchdog-ready event requires a receipt digest")
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
    """Parse one strict, self-authenticating v0.2 approval artifact."""

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
        quote_basis_digest=gcp_cost_quote_digest(preflight.quote),
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
        hard_cost_ceiling=preflight.arm.cost_ceiling,
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
    now: datetime,
) -> GcpExecutionApproval:
    """Fail closed unless one approval binds every current preflight fact."""

    approval = _strict_approval(approval)
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
    request = preflight.request
    expected = {
        "provider": "gcp-compute-engine",
        "plan_id": preflight.arm.plan_id,
        "plan_sha256": preflight.arm.plan_sha256,
        "arm_id": preflight.arm.arm_id,
        "request_digest": gcp_execution_request_digest(request),
        "quote_basis_digest": gcp_cost_quote_digest(preflight.quote),
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
        "hard_cost_ceiling": preflight.arm.cost_ceiling,
    }
    if any(getattr(approval, key) != value for key, value in expected.items()):
        raise GcpSupervisorError("APPROVAL_BINDING_MISMATCH")
    if _parse_timestamp(approval.expires_at) > _parse_timestamp(
        preflight.arm.expires_at
    ):
        raise GcpSupervisorError("APPROVAL_EXPIRY_EXCEEDS_ARM")
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
        controller_id=record.controller_id,
        arm_id=record.arm_id,
        plan_id=record.plan_id,
        request_digest=record.request_digest,
        project_id=record.project_id,
        region=record.region,
        zone=record.zone,
        instance_name=record.instance_name,
        labels=record.labels,
        controller_deadline_at=approval.controller_deadline_at,
    )


def _event(
    *,
    sequence: int,
    state: GcpSupervisorState,
    binding: GcpSupervisorBinding,
    occurred_at: str,
    previous_event_digest: Sha256Digest | None,
    watchdog_receipt_digest: Sha256Digest | None,
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
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
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

    def _repair_partial_tail_locked(self, path: Path) -> None:
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
            ):
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNSAFE")
            raw = os.read(descriptor, GCP_SUPERVISOR_MAX_EVENT_BYTES + 1)
            if len(raw) > GCP_SUPERVISOR_MAX_EVENT_BYTES:
                raise GcpSupervisorError("SUPERVISOR_JOURNAL_UNSAFE")
            if not raw.endswith(b"\n"):
                newline = raw.rfind(b"\n")
                if newline < 0:
                    raise GcpSupervisorError("SUPERVISOR_JOURNAL_INVALID")
                os.ftruncate(descriptor, newline + 1)
                os.fsync(descriptor)
                self._fsync_directory()
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
            "PREPARED": {"ARM_CONSUMED", "CLEANUP_CONFIRMED", "BLOCKED"},
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
            self._repair_partial_tail_locked(entry)
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

    def load(self, controller_id: str) -> GcpSupervisorJournalEvent:
        with self._exclusive() as root:
            path = self._path_for(root, controller_id)
            self._repair_partial_tail_locked(path)
            return self._read_events_locked(path)[-1]

    def advance(
        self,
        binding: GcpSupervisorBinding,
        *,
        state: GcpSupervisorState,
        now: datetime,
        watchdog_receipt_digest: Sha256Digest | None = None,
        error_code: str | None = None,
    ) -> GcpSupervisorJournalEvent:
        """Append exactly one validated deterministic state transition."""

        occurred_at = _timestamp(now)
        with self._exclusive() as root:
            path = self._path_for(root, binding.controller_id)
            self._repair_partial_tail_locked(path)
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


class GcpKillSwitch(Protocol):
    """A read-only kill-switch observation boundary."""

    def is_killed(self, binding: GcpSupervisorBinding) -> bool: ...


class FakeGcpWatchdog:
    """Deterministic local-only watchdog fake for adversarial lifecycle tests."""

    def __init__(self, *, arm_error: str | None = None, ready: bool = True) -> None:
        self.arm_error = arm_error
        self.ready = ready
        self.arm_calls = 0

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
            request_digest=binding.request_digest,
            controller_id=binding.controller_id,
            armed_at=_timestamp(now),
            expires_at=approval.controller_deadline_at,
            ready=True,
            independently_durable=True,
            controller_death_coverage=True,
            hung_work_coverage=True,
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


class GcpLifecycleSupervisor:
    """Bridge exact approval/watchdog state into the existing core controller."""

    def __init__(
        self,
        *,
        approval: GcpExecutionApproval,
        journal: GcpSupervisorJournal,
        watchdog: GcpWatchdog,
        kill_switch: GcpKillSwitch,
    ) -> None:
        self.approval = _strict_approval(approval)
        self.journal = journal
        self.watchdog = watchdog
        self.kill_switch = kill_switch

    def _binding(self, record: GcpLeaseRecord) -> GcpSupervisorBinding:
        return gcp_supervisor_binding(self.approval, record)

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
        validate_gcp_execution_approval(self.approval, preflight=preflight, now=now)
        self.journal.reserve(self._binding(record), now=now)

    def arm_watchdog(
        self,
        record: GcpLeaseRecord,
        *,
        preflight: GcpExecutionPreflight,
        now: datetime,
    ) -> None:
        validate_gcp_execution_approval(self.approval, preflight=preflight, now=now)
        binding = self._binding(record)
        self.journal.advance(binding, state="ARM_CONSUMED", now=now)
        self._assert_not_killed(binding, now=now)
        try:
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
            or receipt.request_digest != binding.request_digest
            or receipt.controller_id != binding.controller_id
            or receipt.armed_at != _timestamp(now)
            or receipt.expires_at != binding.controller_deadline_at
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
        self, record: GcpLeaseRecord, *, now: datetime
    ) -> None:
        binding = self._binding(record)
        event = self.journal.load(binding.controller_id)
        if event.binding != binding or event.state != "WATCHDOG_READY":
            raise GcpSupervisorError("WATCHDOG_NOT_READY")
        if event.watchdog_receipt_digest is None:
            raise GcpSupervisorError("WATCHDOG_RECEIPT_MISSING")
        if _parse_timestamp(binding.controller_deadline_at) <= _parse_timestamp(
            _timestamp(now)
        ):
            raise GcpSupervisorError("CONTROLLER_DEADLINE_EXPIRED")
        self._assert_not_killed(binding, now=now)

    def create_intent(self, record: GcpLeaseRecord, *, now: datetime) -> None:
        self.assert_create_permitted(record, now=now)
        self.journal.advance(self._binding(record), state="CREATE_INTENT", now=now)

    def before_work(self, record: GcpLeaseRecord, *, now: datetime) -> None:
        binding = self._binding(record)
        event = self.journal.load(binding.controller_id)
        if event.binding != binding or event.state != "CREATE_INTENT":
            raise GcpSupervisorError("SUPERVISOR_WORK_STATE_INVALID")
        self._assert_not_killed(binding, now=now)
        self.journal.advance(binding, state="WORKING", now=now)

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
        self.journal.advance(
            self._binding(record),
            state="CLEANUP_CONFIRMED" if confirmed else "ORPHANED",
            now=now,
            error_code=error_code,
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
        if event.state != "CLEANUP_CONFIRMED":
            self.journal.advance(binding, state="CLEANUP_PENDING", now=now)


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
    )
    output: dict[str, dict[str, Any]] = {}
    for filename, model, schema_id in models:
        schema = model.model_json_schema()
        schema["$id"] = schema_id
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        output[filename] = schema
    return output
