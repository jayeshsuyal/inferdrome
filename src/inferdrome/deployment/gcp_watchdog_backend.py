"""Durable local-only cleanup backend for the fresh v2 watchdog worker.

The watchdog worker may never inherit a Python callable, cloud client,
credential, approval document, execution payload, or mutable pathname.  This
module provides the deliberately small *local fake* backend used by tests to
exercise that boundary across an exec/restart.  It is not a GCE backend and it
cannot construct one.

All mutable files live below the watchdog directory descriptor.  The persisted
worker spec binds that descriptor, a separately inherited core-journal
descriptor, the immutable lease anchor, the active watchdog event, and the
exact boot-disk binding.  A worker therefore fails closed if any durable
identity is absent, noncanonical, retargeted, or inconsistent.
"""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import Field, ValidationError, model_validator

from inferdrome.deployment.gcp_lifecycle import (
    _CONTROLLER_RE,
    GcpExecutionModel,
    GcpInstanceObservation,
    GcpLeaseIntentAnchor,
    GcpLeaseRecord,
    GcpOwnedResourceInventory,
    gcp_execution_request_digest,
)
from inferdrome.deployment.gcp_securefs import SafeDirFD, SafeDirFSError
from inferdrome.deployment.gcp_supervisor import (
    GcpFileWatchdogEvent,
    GcpSupervisorBinding,
    GcpWatchdogCleanupResult,
    GcpWatchdogPrebindCleanupResult,
)
from inferdrome.deployment.gcp_v2_disk_cleanup import (
    GCP_V2_DISK_ABSENCE_SCHEMA_VERSION,
    GCP_V2_DISK_CLEANUP_OUTCOME_SCHEMA_VERSION,
    GCP_V2_DISK_DELETE_OPERATION_SCHEMA_VERSION,
    GcpV2DiskAbsenceObservation,
    GcpV2DiskCleanupBinding,
    GcpV2DiskCleanupOutcome,
    GcpV2DiskDeleteOperation,
    gcp_v2_disk_delete_request_id,
)
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes
from inferdrome.domain.ids import Sha256Digest

GCP_FILE_WATCHDOG_WORKER_SPEC_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-file-watchdog-worker-spec.v2"
)
GCP_FILE_WATCHDOG_FAKE_BACKEND_STATE_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-file-watchdog-fake-cleanup-state.v2"
)
GCP_FILE_WATCHDOG_WORKER_SPEC_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-file-watchdog-worker-spec:v2"
)
GCP_FILE_WATCHDOG_FAKE_BACKEND_STATE_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-file-watchdog-fake-cleanup-state:v2"
)

_SPEC_SUFFIX: Final = ".file-watchdog-v2.worker-spec.json"
_STATE_SUFFIX: Final = ".file-watchdog-v2.fake-cleanup-state.json"
_LOCK_SUFFIX: Final = ".file-watchdog-v2.fake-cleanup.lock"
_MAX_FILE_BYTES: Final = 131_072


class GcpFileWatchdogBackendError(ValueError):
    """A non-provider, fail-closed worker backend failure."""


def _value(model: GcpExecutionModel) -> dict[str, Any]:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise GcpFileWatchdogBackendError("backend model serialization is invalid")
    return value


def _name(controller_id: str, suffix: str) -> str:
    if _CONTROLLER_RE.fullmatch(controller_id) is None:
        raise GcpFileWatchdogBackendError("backend controller identity is invalid")
    return f"{controller_id}{suffix}"


def _spec_name(controller_id: str) -> str:
    return _name(controller_id, _SPEC_SUFFIX)


def _state_name(controller_id: str) -> str:
    return _name(controller_id, _STATE_SUFFIX)


def _lock_name(controller_id: str) -> str:
    return _name(controller_id, _LOCK_SUFFIX)


def _read_all(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise GcpFileWatchdogBackendError("backend file exceeds bound")


def _assert_child_matches(
    root: SafeDirFD, name: str, descriptor: int, *, require_size: bool = False
) -> os.stat_result:
    """Reject a replacement or unsafe child before/after descriptor I/O.

    The parent holds a verified directory FD, but an attacker who can rename a
    child must not make the descriptor and the name refer to different files.
    Checking both the descriptor and ``statat`` identity at each boundary
    turns that race into a local fail-closed result.  ``nlink == 1`` also
    rejects hard-link replacement tricks for these private state files.
    """

    try:
        descriptor_stat = os.fstat(descriptor)
        child_stat = root.validated_regular_child(name, descriptor=descriptor)
    except (OSError, SafeDirFSError) as error:
        raise GcpFileWatchdogBackendError(
            "backend file identity unavailable"
        ) from error
    if (
        not stat.S_ISREG(descriptor_stat.st_mode)
        or descriptor_stat.st_mode & 0o022
        or descriptor_stat.st_uid != os.getuid()
        or descriptor_stat.st_nlink != 1
        or child_stat.st_dev != descriptor_stat.st_dev
        or child_stat.st_ino != descriptor_stat.st_ino
        or child_stat.st_uid != descriptor_stat.st_uid
        or child_stat.st_mode != descriptor_stat.st_mode
        or child_stat.st_nlink != descriptor_stat.st_nlink
        or (require_size and child_stat.st_size != descriptor_stat.st_size)
    ):
        raise GcpFileWatchdogBackendError("backend file identity changed")
    return descriptor_stat


def _read_json(root: SafeDirFD, name: str, model: type[GcpExecutionModel]) -> Any:
    descriptor: int | None = None
    try:
        descriptor = root.open_child(name, os.O_RDONLY | os.O_NONBLOCK)
        initial = _assert_child_matches(root, name, descriptor, require_size=True)
        if initial.st_size <= 0 or initial.st_size > _MAX_FILE_BYTES:
            raise GcpFileWatchdogBackendError("backend file is unsafe")
        raw = _read_all(descriptor, _MAX_FILE_BYTES)
        final = _assert_child_matches(root, name, descriptor, require_size=True)
        if final.st_size != len(raw):
            raise GcpFileWatchdogBackendError("backend file changed while read")
        parsed = model.model_validate_json(raw)
        if canonical_json_bytes(_value(parsed)) != raw:
            raise GcpFileWatchdogBackendError("backend file is noncanonical")
        return parsed
    except GcpFileWatchdogBackendError:
        raise
    except (OSError, SafeDirFSError, ValidationError, ValueError) as error:
        raise GcpFileWatchdogBackendError("backend file is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("backend write failed")
        view = view[written:]


def _write_json(root: SafeDirFD, name: str, model: GcpExecutionModel) -> None:
    """Atomically replace one private canonical file through the held root FD."""

    raw = canonical_json_bytes(_value(model))
    if len(raw) > _MAX_FILE_BYTES:
        raise GcpFileWatchdogBackendError("backend file exceeds bound")
    stage = f".{name}.{uuid.uuid4().hex}.stage"
    descriptor: int | None = None
    stage_stat: os.stat_result | None = None
    destination_stat: os.stat_result | None = None
    try:
        try:
            destination_stat = root.validated_regular_child(name)
        except FileNotFoundError:
            destination_stat = None
        descriptor = root.open_child(
            stage,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        stage_stat = _assert_child_matches(root, stage, descriptor)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        stage_stat = _assert_child_matches(root, stage, descriptor, require_size=True)
        os.close(descriptor)
        descriptor = None
        root.replace_child(
            stage,
            name,
            expected_source=stage_stat,
            expected_destination=destination_stat,
        )
        # Re-open the published object through the same descriptor-root and
        # validate it once more before returning a durable acknowledgement.
        verification = root.open_child(name, os.O_RDONLY | os.O_NONBLOCK)
        try:
            published = _assert_child_matches(
                root, name, verification, require_size=True
            )
            if (
                published.st_size != len(raw)
                or _read_all(verification, _MAX_FILE_BYTES) != raw
            ):
                raise GcpFileWatchdogBackendError("backend publish changed")
            _assert_child_matches(root, name, verification, require_size=True)
        finally:
            os.close(verification)
        root.fsync()
    except GcpFileWatchdogBackendError:
        raise
    except (OSError, SafeDirFSError) as error:
        raise GcpFileWatchdogBackendError("backend file write failed") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            stage_stat = root.validated_regular_child(stage)
        except (FileNotFoundError, OSError, SafeDirFSError):
            stage_stat = None
        if stage_stat is not None:
            if not stat.S_ISREG(stage_stat.st_mode) or stage_stat.st_nlink != 1:
                raise GcpFileWatchdogBackendError("backend stage is unsafe")
            try:
                root.unlink_child(stage, expected=stage_stat)
                root.fsync()
            except (OSError, SafeDirFSError) as error:
                raise GcpFileWatchdogBackendError(
                    "backend stage cleanup failed"
                ) from error


@contextmanager
def _exclusive(root: SafeDirFD, controller_id: str) -> Iterator[None]:
    descriptor: int | None = None
    name = _lock_name(controller_id)
    try:
        descriptor = root.open_child(name, os.O_RDWR | os.O_CREAT, 0o600)
        _assert_child_matches(root, name, descriptor)
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _assert_child_matches(root, name, descriptor)
        yield
    except GcpFileWatchdogBackendError:
        raise
    except (ImportError, OSError, SafeDirFSError) as error:
        raise GcpFileWatchdogBackendError("backend lock unavailable") from error
    finally:
        if descriptor is not None:
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            os.close(descriptor)


class GcpFileWatchdogWorkerSpec(GcpExecutionModel):
    """No-secret, descriptor-bound reconstruction input for one worker."""

    schema_version: Literal["inferdrome.gcp-file-watchdog-worker-spec.v2"]
    backend_kind: Literal["file_backed_fake_exact_cleanup"]
    controller_id: str
    binding: GcpSupervisorBinding
    lease_intent_anchor_digest: Sha256Digest
    executor_config_digest: Sha256Digest
    capability_id: Sha256Digest
    activation_receipt_id: Sha256Digest
    provider_runtime_deadline_at: str
    watchdog_cleanup_deadline_at: str
    watchdog_root_device: int = Field(strict=True, ge=1)
    watchdog_root_inode: int = Field(strict=True, ge=1)
    core_root_device: int = Field(strict=True, ge=1)
    core_root_inode: int = Field(strict=True, ge=1)
    disk_cleanup_binding: GcpV2DiskCleanupBinding | None = None
    spec_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_spec(self) -> GcpFileWatchdogWorkerSpec:
        if _CONTROLLER_RE.fullmatch(self.controller_id) is None:
            raise ValueError("worker spec controller is invalid")
        if self.binding.controller_id != self.controller_id:
            raise ValueError("worker spec binding is invalid")
        if self.disk_cleanup_binding is not None and (
            self.disk_cleanup_binding.controller_id != self.controller_id
            or self.disk_cleanup_binding.request_digest != self.binding.request_digest
            or self.disk_cleanup_binding.project_id != self.binding.project_id
            or self.disk_cleanup_binding.zone != self.binding.zone
            or self.disk_cleanup_binding.instance_name != self.binding.instance_name
            or self.disk_cleanup_binding.labels != self.binding.labels
        ):
            raise ValueError("worker spec disk binding is invalid")
        value = _value(self)
        value.pop("spec_digest", None)
        expected = digest_bytes(
            DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(value)
        )
        if self.spec_digest != expected:
            raise ValueError("worker spec identity is invalid")
        return self


class GcpFileBackedFakeCleanupState(GcpExecutionModel):
    """Fsync-backed local fake instance/disk cleanup state.

    This records actual test-fake mutations, not a precomputed result.  The
    exact operation and absence fields make a lost parent/process restart
    observable and reconstructible without any provider client.
    """

    schema_version: Literal["inferdrome.gcp-file-watchdog-fake-cleanup-state.v2"]
    spec_digest: Sha256Digest
    lease_intent_anchor_digest: Sha256Digest
    binding: GcpSupervisorBinding
    disk_cleanup_binding: GcpV2DiskCleanupBinding | None = None
    instance_present: bool
    disk_present: bool
    instance_delete_attempts: int = Field(strict=True, ge=0, le=3)
    disk_delete_attempts: int = Field(strict=True, ge=0, le=3)
    disk_delete_operation: GcpV2DiskDeleteOperation | None = None
    disk_absence: GcpV2DiskAbsenceObservation | None = None

    @model_validator(mode="after")
    def validate_state(self) -> GcpFileBackedFakeCleanupState:
        if self.disk_cleanup_binding is None:
            if (
                self.disk_present
                or self.disk_delete_attempts
                or self.disk_delete_operation
                or self.disk_absence
            ):
                raise ValueError("unbound fake state carries disk facts")
        else:
            binding = self.disk_cleanup_binding
            if (
                binding.controller_id != self.binding.controller_id
                or binding.request_digest != self.binding.request_digest
                or binding.project_id != self.binding.project_id
                or binding.zone != self.binding.zone
                or binding.instance_name != self.binding.instance_name
                or binding.labels != self.binding.labels
            ):
                raise ValueError("fake state disk binding is invalid")
            if self.disk_delete_operation is not None and (
                self.disk_delete_operation.request_digest != binding.request_digest
                or self.disk_delete_operation.project_id != binding.project_id
                or self.disk_delete_operation.zone != binding.zone
                or self.disk_delete_operation.instance_name != binding.instance_name
                or self.disk_delete_operation.disk_name != binding.disk.disk_name
                or self.disk_delete_operation.labels != binding.labels
            ):
                raise ValueError("fake state disk operation is invalid")
            if self.disk_absence is not None and (
                self.disk_absence.request_digest != binding.request_digest
                or self.disk_absence.project_id != binding.project_id
                or self.disk_absence.zone != binding.zone
                or self.disk_absence.instance_name != binding.instance_name
                or self.disk_absence.disk_name != binding.disk.disk_name
                or self.disk_absence.labels != binding.labels
                or self.disk_absence.state != "NOT_FOUND"
            ):
                raise ValueError("fake state disk absence is invalid")
        # An exact boot disk may remain as a detached residual after instance
        # deletion. Its original immutable attachment provenance stays in the
        # bound disk contract, so this transition is expected rather than a
        # licence to retarget a different disk.
        return self


def _spec_payload(
    *,
    binding: GcpSupervisorBinding,
    record: GcpLeaseRecord,
    event: GcpFileWatchdogEvent,
    watchdog_root: SafeDirFD,
    core_root: SafeDirFD,
    disk_cleanup_binding: GcpV2DiskCleanupBinding | None,
) -> dict[str, Any]:
    if (
        event.binding != binding
        or event.executor_config_digest is None
        or event.capability_id is None
        or event.activation_receipt_id is None
        or event.provider_runtime_deadline_at is None
        or event.watchdog_cleanup_deadline_at is None
    ):
        raise GcpFileWatchdogBackendError("active event is incomplete")
    if (
        record.controller_id != binding.controller_id
        or record.intent_anchor_digest is None
        or record.request_digest != binding.request_digest
        or record.project_id != binding.project_id
        or record.region != binding.region
        or record.zone != binding.zone
        or record.instance_name != binding.instance_name
        or record.labels != binding.labels
    ):
        raise GcpFileWatchdogBackendError("lease does not match worker binding")
    if (
        disk_cleanup_binding is not None
        and disk_cleanup_binding != event.disk_cleanup_binding
    ):
        raise GcpFileWatchdogBackendError("worker disk event does not match spec")
    return {
        "schema_version": GCP_FILE_WATCHDOG_WORKER_SPEC_SCHEMA_VERSION,
        "backend_kind": "file_backed_fake_exact_cleanup",
        "controller_id": binding.controller_id,
        "binding": _value(binding),
        "lease_intent_anchor_digest": record.intent_anchor_digest,
        "executor_config_digest": event.executor_config_digest,
        "capability_id": event.capability_id,
        "activation_receipt_id": event.activation_receipt_id,
        "provider_runtime_deadline_at": event.provider_runtime_deadline_at,
        "watchdog_cleanup_deadline_at": event.watchdog_cleanup_deadline_at,
        "watchdog_root_device": watchdog_root.device,
        "watchdog_root_inode": watchdog_root.inode,
        "core_root_device": core_root.device,
        "core_root_inode": core_root.inode,
        "disk_cleanup_binding": (
            _value(disk_cleanup_binding) if disk_cleanup_binding is not None else None
        ),
    }


def _issue_spec(payload: dict[str, Any]) -> GcpFileWatchdogWorkerSpec:
    value = dict(payload)
    value["spec_digest"] = digest_bytes(
        DigestDomain.GCP_EXECUTION_WATCHDOG, canonical_json_bytes(value)
    )
    return GcpFileWatchdogWorkerSpec.model_validate_json(canonical_json_bytes(value))


def _read_lease(
    core_root: SafeDirFD, controller_id: str
) -> tuple[GcpLeaseRecord, GcpLeaseIntentAnchor]:
    record = _read_json(
        core_root,
        _name(controller_id, ".lease.json"),
        GcpLeaseRecord,
    )
    anchor = _read_json(
        core_root,
        _name(controller_id, ".intent.json"),
        GcpLeaseIntentAnchor,
    )
    if (
        record.controller_id != controller_id
        or record.intent_anchor_digest != anchor.anchor_digest
        or anchor.controller_id != controller_id
        or anchor.request_digest != record.request_digest
        or anchor.request != record.request
        or anchor.labels != record.labels
    ):
        raise GcpFileWatchdogBackendError("core lease anchor is inconsistent")
    return record, anchor


def _validate_event_and_spec(
    *,
    root: SafeDirFD,
    core_root: SafeDirFD,
    event: GcpFileWatchdogEvent,
    require_disk: bool | None,
) -> tuple[GcpFileWatchdogWorkerSpec, GcpLeaseRecord]:
    spec = _read_json(
        root, _spec_name(str(event.binding.controller_id)), GcpFileWatchdogWorkerSpec
    )
    if (
        spec.watchdog_root_device != root.device
        or spec.watchdog_root_inode != root.inode
        or spec.core_root_device != core_root.device
        or spec.core_root_inode != core_root.inode
        or spec.binding != event.binding
        or spec.executor_config_digest != event.executor_config_digest
        or spec.capability_id != event.capability_id
        or spec.activation_receipt_id != event.activation_receipt_id
        or spec.provider_runtime_deadline_at != event.provider_runtime_deadline_at
        or spec.watchdog_cleanup_deadline_at != event.watchdog_cleanup_deadline_at
    ):
        raise GcpFileWatchdogBackendError("worker spec/event identity mismatch")
    record, anchor = _read_lease(core_root, str(event.binding.controller_id))
    if (
        spec.lease_intent_anchor_digest != anchor.anchor_digest
        or record.intent_anchor_digest != spec.lease_intent_anchor_digest
        or record.request_digest != spec.binding.request_digest
        or record.project_id != spec.binding.project_id
        or record.region != spec.binding.region
        or record.zone != spec.binding.zone
        or record.instance_name != spec.binding.instance_name
        or record.labels != spec.binding.labels
    ):
        raise GcpFileWatchdogBackendError("worker spec/lease identity mismatch")
    event_has_disk = event.disk_cleanup_binding is not None
    if require_disk is not None and event_has_disk is not require_disk:
        raise GcpFileWatchdogBackendError("worker cleanup kind is inconsistent")
    if spec.disk_cleanup_binding != event.disk_cleanup_binding:
        raise GcpFileWatchdogBackendError("worker disk binding is inconsistent")
    return spec, record


def _not_found(record: GcpLeaseRecord) -> GcpInstanceObservation:
    request = record.request
    return GcpInstanceObservation(
        instance_name=request.instance_name,
        project_id=request.project_id,
        zone=request.zone,
        machine_type=request.machine_type,
        accelerator_model=request.accelerator_model,
        accelerator_count=request.accelerator_count,
        state="NOT_FOUND",
        labels=None,
        external_access_config="absent",
        ip_forwarding=False,
    )


def _empty_inventory(record: GcpLeaseRecord) -> GcpOwnedResourceInventory:
    request = record.request
    return GcpOwnedResourceInventory(
        schema_version="inferdrome.gcp-owned-resource-inventory.v2",
        request_digest=gcp_execution_request_digest(request),
        project_id=request.project_id,
        region=request.region,
        zone=request.zone,
        labels=request.labels,
        instances=(),
        pagination_complete=True,
    )


def _disk_operation(binding: GcpV2DiskCleanupBinding) -> GcpV2DiskDeleteOperation:
    request_id = gcp_v2_disk_delete_request_id(binding)
    # Keep this synthetic, deterministic operation identity below the generic
    # credential-shape threshold enforced for every persisted GCP model.  The
    # full binding digest remains in the durable binding/outcome; this local
    # fake identifier is only a bounded operation handle, never a secret.
    operation_id = "disk-op-" + binding.binding_id.removeprefix("sha256:")[:24]
    return GcpV2DiskDeleteOperation(
        schema_version=GCP_V2_DISK_DELETE_OPERATION_SCHEMA_VERSION,
        operation_id=operation_id,
        operation_name=(
            f"projects/{binding.project_id}/zones/{binding.zone}/operations/"
            f"{operation_id}"
        ),
        request_id=request_id,
        request_digest=binding.request_digest,
        project_id=binding.project_id,
        zone=binding.zone,
        instance_name=binding.instance_name,
        disk_name=binding.disk.disk_name,
        labels=binding.labels,
    )


def _disk_absence(binding: GcpV2DiskCleanupBinding) -> GcpV2DiskAbsenceObservation:
    return GcpV2DiskAbsenceObservation(
        schema_version=GCP_V2_DISK_ABSENCE_SCHEMA_VERSION,
        request_digest=binding.request_digest,
        project_id=binding.project_id,
        zone=binding.zone,
        instance_name=binding.instance_name,
        disk_name=binding.disk.disk_name,
        labels=binding.labels,
        state="NOT_FOUND",
    )


class FileBackedFakeWatchdogBackend:
    """One reconstructible local exact-cleanup backend.

    The backend is intentionally only a test fake.  It makes a real durable
    state transition for an exact fake instance and exact fake disk so the
    exec worker can demonstrate independent cleanup/restart behavior without
    a GCP SDK, an injected callable, or any cloud authority.
    """

    def __init__(self, *, core_journal_root: os.PathLike[str] | str) -> None:
        self._core_journal_root = os.fspath(core_journal_root)
        if not os.path.isabs(self._core_journal_root):
            raise GcpFileWatchdogBackendError("backend core root is invalid")
        self._record: GcpLeaseRecord | None = None

    @property
    def core_journal_root(self) -> str:
        return self._core_journal_root

    def matches_core_journal_root(self, root: os.PathLike[str] | str) -> bool:
        try:
            return os.path.samefile(self._core_journal_root, os.fspath(root))
        except OSError:
            return False

    def configure_lease(self, record: GcpLeaseRecord) -> None:
        raw = canonical_json_bytes(_value(record))
        parsed = GcpLeaseRecord.model_validate_json(raw)
        if canonical_json_bytes(_value(parsed)) != raw:
            raise GcpFileWatchdogBackendError("backend lease is noncanonical")
        self._record = parsed

    def _open_core_root(self) -> SafeDirFD:
        try:
            return SafeDirFD.open(Path(os.path.abspath(self._core_journal_root)))
        except (OSError, SafeDirFSError) as error:
            raise GcpFileWatchdogBackendError(
                "backend core root is unavailable"
            ) from error

    def _record_for(
        self, binding: GcpSupervisorBinding, core_root: SafeDirFD
    ) -> GcpLeaseRecord:
        record, _anchor = _read_lease(core_root, str(binding.controller_id))
        expected = self._record
        if expected is not None and (
            expected.intent_anchor_digest != record.intent_anchor_digest
            or expected.request_digest != record.request_digest
            or expected.controller_id != record.controller_id
        ):
            raise GcpFileWatchdogBackendError("backend lease changed")
        if (
            record.request_digest != binding.request_digest
            or record.project_id != binding.project_id
            or record.region != binding.region
            or record.zone != binding.zone
            or record.instance_name != binding.instance_name
            or record.labels != binding.labels
        ):
            raise GcpFileWatchdogBackendError("backend lease binding is invalid")
        return record

    def prepare_active(
        self, root: SafeDirFD, event: GcpFileWatchdogEvent
    ) -> GcpFileWatchdogWorkerSpec:
        if event.state not in {"ACTIVE", "RUNNER_READY"}:
            raise GcpFileWatchdogBackendError("backend activation state is invalid")
        core_root = self._open_core_root()
        try:
            record = self._record_for(event.binding, core_root)
            spec = _issue_spec(
                _spec_payload(
                    binding=event.binding,
                    record=record,
                    event=event,
                    watchdog_root=root,
                    core_root=core_root,
                    disk_cleanup_binding=event.disk_cleanup_binding,
                )
            )
            state = GcpFileBackedFakeCleanupState(
                schema_version=GCP_FILE_WATCHDOG_FAKE_BACKEND_STATE_SCHEMA_VERSION,
                spec_digest=spec.spec_digest,
                lease_intent_anchor_digest=record.intent_anchor_digest,
                binding=event.binding,
                disk_cleanup_binding=event.disk_cleanup_binding,
                instance_present=event.disk_cleanup_binding is not None,
                disk_present=event.disk_cleanup_binding is not None,
                instance_delete_attempts=0,
                disk_delete_attempts=0,
                disk_delete_operation=None,
                disk_absence=None,
            )
            with _exclusive(root, str(event.binding.controller_id)):
                _write_json(root, _spec_name(str(event.binding.controller_id)), spec)
                _write_json(root, _state_name(str(event.binding.controller_id)), state)
            return spec
        finally:
            core_root.close()

    def bind_disk_cleanup(
        self, root: SafeDirFD, event: GcpFileWatchdogEvent
    ) -> GcpFileWatchdogWorkerSpec:
        if event.disk_cleanup_binding is None:
            raise GcpFileWatchdogBackendError("backend disk binding is missing")
        core_root = self._open_core_root()
        try:
            record = self._record_for(event.binding, core_root)
            spec = _issue_spec(
                _spec_payload(
                    binding=event.binding,
                    record=record,
                    event=event,
                    watchdog_root=root,
                    core_root=core_root,
                    disk_cleanup_binding=event.disk_cleanup_binding,
                )
            )
            state = GcpFileBackedFakeCleanupState(
                schema_version=GCP_FILE_WATCHDOG_FAKE_BACKEND_STATE_SCHEMA_VERSION,
                spec_digest=spec.spec_digest,
                lease_intent_anchor_digest=record.intent_anchor_digest,
                binding=event.binding,
                disk_cleanup_binding=event.disk_cleanup_binding,
                instance_present=True,
                disk_present=True,
                instance_delete_attempts=0,
                disk_delete_attempts=0,
                disk_delete_operation=None,
                disk_absence=None,
            )
            with _exclusive(root, str(event.binding.controller_id)):
                _write_json(root, _spec_name(str(event.binding.controller_id)), spec)
                _write_json(root, _state_name(str(event.binding.controller_id)), state)
            return spec
        finally:
            core_root.close()

    def validate_ready(self, root: SafeDirFD, event: GcpFileWatchdogEvent) -> None:
        core_root = self._open_core_root()
        try:
            spec, record = _validate_event_and_spec(
                root=root,
                core_root=core_root,
                event=event,
                require_disk=None,
            )
            state = _read_json(
                root,
                _state_name(str(event.binding.controller_id)),
                GcpFileBackedFakeCleanupState,
            )
            if (
                state.spec_digest != spec.spec_digest
                or state.lease_intent_anchor_digest != record.intent_anchor_digest
                or state.binding != event.binding
                or state.disk_cleanup_binding != spec.disk_cleanup_binding
            ):
                raise GcpFileWatchdogBackendError("backend state is inconsistent")
        finally:
            core_root.close()


def worker_validate_ready(
    root: SafeDirFD, core_root: SafeDirFD, event: GcpFileWatchdogEvent
) -> None:
    """Validate all durable identities before a fresh worker says ready."""

    spec, record = _validate_event_and_spec(
        root=root, core_root=core_root, event=event, require_disk=None
    )
    state = _read_json(
        root,
        _state_name(str(event.binding.controller_id)),
        GcpFileBackedFakeCleanupState,
    )
    if (
        state.spec_digest != spec.spec_digest
        or state.lease_intent_anchor_digest != record.intent_anchor_digest
        or state.binding != event.binding
        or state.disk_cleanup_binding != spec.disk_cleanup_binding
    ):
        raise GcpFileWatchdogBackendError("worker fake state is inconsistent")


def worker_cleanup(
    root: SafeDirFD,
    core_root: SafeDirFD,
    event: GcpFileWatchdogEvent,
    *,
    prebind: bool,
) -> GcpWatchdogCleanupResult | GcpWatchdogPrebindCleanupResult:
    """Perform one actual exact fake cleanup after all durable checks pass."""

    spec, record = _validate_event_and_spec(
        root=root,
        core_root=core_root,
        event=event,
        require_disk=not prebind,
    )
    controller_id = str(event.binding.controller_id)
    with _exclusive(root, controller_id):
        # Re-read all identities while holding the fake-backend lock.  A
        # concurrent writer may not turn an already-validated result into a
        # different exact target between readiness and deletion.
        spec, record = _validate_event_and_spec(
            root=root,
            core_root=core_root,
            event=event,
            require_disk=not prebind,
        )
        state = _read_json(
            root, _state_name(controller_id), GcpFileBackedFakeCleanupState
        )
        if (
            state.spec_digest != spec.spec_digest
            or state.lease_intent_anchor_digest != record.intent_anchor_digest
            or state.binding != event.binding
            or state.disk_cleanup_binding != spec.disk_cleanup_binding
        ):
            raise GcpFileWatchdogBackendError("worker fake state identity mismatch")

        # Exact fake instance termination: its only identity is the immutable
        # lease/request/binding, never a hostname lookup or broad inventory.
        if state.instance_present:
            state = state.model_copy(
                update={
                    "instance_present": False,
                    "instance_delete_attempts": state.instance_delete_attempts + 1,
                }
            )
            _write_json(root, _state_name(controller_id), state)
        observation = _not_found(record)
        inventory = _empty_inventory(record)
        if prebind:
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

        binding = spec.disk_cleanup_binding
        if binding is None:
            raise GcpFileWatchdogBackendError("worker disk binding is missing")
        # Exact fake disk delete -> operation reconciliation -> absence.  Each
        # substep is fsync-backed so restart reuses the same deterministic
        # operation identity and never broadens to a label scan.
        operation = state.disk_delete_operation or _disk_operation(binding)
        if state.disk_present:
            state = state.model_copy(
                update={
                    "disk_present": False,
                    "disk_delete_attempts": state.disk_delete_attempts + 1,
                    "disk_delete_operation": operation,
                }
            )
            _write_json(root, _state_name(controller_id), state)
        absence = state.disk_absence or _disk_absence(binding)
        if state.disk_absence is None:
            state = state.model_copy(update={"disk_absence": absence})
            _write_json(root, _state_name(controller_id), state)
        outcome = GcpV2DiskCleanupOutcome(
            schema_version=GCP_V2_DISK_CLEANUP_OUTCOME_SCHEMA_VERSION,
            binding_id=binding.binding_id,
            state="ABSENCE_CONFIRMED",
            delete_attempts=state.disk_delete_attempts,
            operation_id=operation.operation_id,
            absence=absence,
            error_code=None,
        )
        return GcpWatchdogCleanupResult(
            schema_version="inferdrome.gcp-watchdog-cleanup-result.v2",
            controller_id=event.binding.controller_id,
            request_digest=event.binding.request_digest,
            project_id=event.binding.project_id,
            region=event.binding.region,
            zone=event.binding.zone,
            instance_name=event.binding.instance_name,
            labels=event.binding.labels,
            disk_cleanup_binding=binding,
            disk_cleanup_outcome=outcome,
            instance_observation=observation,
            owned_inventory=inventory,
        )


def gcp_file_watchdog_backend_contract_schemas() -> dict[str, dict[str, Any]]:
    """Return the generated schemas for the durable local worker records.

    These records are a narrow, no-secret v2 persistence contract for the
    reconstructible local fake backend.  They do not describe a GCE payload,
    provider request, credential, or general worker-execution interface.
    """

    models: tuple[tuple[str, type[GcpExecutionModel], str], ...] = (
        (
            "gcp-file-watchdog-worker-spec.schema.json",
            GcpFileWatchdogWorkerSpec,
            GCP_FILE_WATCHDOG_WORKER_SPEC_SCHEMA_ID,
        ),
        (
            "gcp-file-watchdog-fake-cleanup-state.schema.json",
            GcpFileBackedFakeCleanupState,
            GCP_FILE_WATCHDOG_FAKE_BACKEND_STATE_SCHEMA_ID,
        ),
    )
    output: dict[str, dict[str, Any]] = {}
    for filename, model, schema_id in models:
        schema = model.model_json_schema()
        schema["$id"] = schema_id
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        output[filename] = schema
    return output
