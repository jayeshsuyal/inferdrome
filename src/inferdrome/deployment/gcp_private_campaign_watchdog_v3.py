"""Independent cleanup watchdog for the exact two-A100 private campaign.

This module deliberately owns one narrow boundary only: after a local
two-A100 create intent is durable, a detached worker can reconcile and clean
that exact campaign if its parent controller dies, hangs, or loses the create
response before it has persisted provider identifiers.  The worker has no
create, runner, routing, or evidence-collection path.  It starts before the
optional Google SDK is reachable and receives only a sanitized, canonical
activation record plus a safe directory descriptor.

The activation digest is content addressing, *not* a signature or proof of
human authorship.  Human approval is separately verified by the lifecycle
controller before this watchdog is armed.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self

from pydantic import Field, StringConstraints, ValidationError, model_validator

from inferdrome.deployment.gcp import GcpTimestamp
from inferdrome.deployment.gcp_lifecycle import _parse_timestamp, _timestamp
from inferdrome.deployment.gcp_private_campaign_v2 import (
    GCP_PRIVATE_CAMPAIGN_MAX_JOURNAL_BYTES,
    GcpPrivateCampaignApproval,
    GcpPrivateCampaignCleanupAuthorization,
    GcpPrivateCampaignCreateRequest,
    GcpPrivateCampaignError,
    GcpPrivateCampaignJournal,
    GcpPrivateCampaignLifecycleController,
    GcpPrivateCampaignProposal,
    GcpPrivateCampaignWatchdogRecoveryProgress,
    PrecampaignControllerId,
    PrecampaignModel,
    _model_value,
    verify_gcp_private_campaign_approval,
    verify_gcp_private_campaign_cleanup_authorization,
)
from inferdrome.deployment.gcp_securefs import SafeDirFD, SafeDirFSError
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.ids import Sha256Digest
from inferdrome.routing_execution.canonical import sha256_digest

GCP_PRIVATE_CAMPAIGN_WATCHDOG_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-campaign-watchdog.v3"
)
GCP_PRIVATE_CAMPAIGN_WATCHDOG_RECORD_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-campaign-watchdog-record.v3"
)
GCP_PRIVATE_CAMPAIGN_WATCHDOG_EVENT_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-private-campaign-watchdog-event.v3"
)
_MAX_EVENTS: Final = 64
_MAX_EVENT_BYTES: Final = 262_144
_READY_TIMEOUT_SECONDS: Final = 3.0
_SOURCE_ROOT: Final = Path(__file__).resolve().parents[2]
_CAPABILITY_TOKEN: Final = object()
_WorkerBackend = Literal["google_cleanup_only_v1", "file_fake_cleanup_v1"]
_EventState = Literal[
    "WORKER_READY",
    "RECOVERY_ATTEMPT",
    "PREBIND_ABSENCE_PENDING",
    "CLEANUP_CONFIRMED",
    "CLEANUP_UNCONFIRMED",
    "STOPPED",
]


def _canonical(model: PrecampaignModel) -> bytes:
    return canonical_json_bytes(_model_value(model))


class GcpPrivateCampaignWatchdogBinding(PrecampaignModel):
    """The complete local scope that a detached worker may reconcile."""

    schema_version: Literal["inferdrome.gcp-private-campaign-watchdog.v3"]
    proposal: GcpPrivateCampaignProposal
    request: GcpPrivateCampaignCreateRequest
    cleanup_authorization: GcpPrivateCampaignCleanupAuthorization
    approval_sha256: Sha256Digest
    journal_root_sha256: Sha256Digest
    execution_deadline_at: GcpTimestamp
    cleanup_deadline_at: GcpTimestamp
    parent_process_id: Annotated[int, Field(ge=1, le=2_147_483_647)]
    worker_backend: _WorkerBackend
    binding_digest: Sha256Digest

    @model_validator(mode="after")
    def _binding_is_exact(self) -> Self:
        if self.request.proposal != self.proposal:
            raise ValueError("watchdog request does not match proposal")
        if self.cleanup_authorization.proposal != self.proposal:
            raise ValueError("watchdog cleanup authorization does not match proposal")
        if self.cleanup_authorization.proposal_digest != self.proposal.proposal_id:
            raise ValueError("watchdog cleanup authorization digest does not match")
        if (
            self.cleanup_authorization.cleanup_request_id
            != self.proposal.request_ids.delete_request_id
        ):
            raise ValueError("watchdog cleanup request identity does not match")
        if _parse_timestamp(self.cleanup_deadline_at) <= _parse_timestamp(
            self.execution_deadline_at
        ):
            raise ValueError("watchdog cleanup horizon must follow execution")
        if self.binding_digest != gcp_private_campaign_watchdog_binding_digest(self):
            raise ValueError("watchdog binding identity does not match")
        return self


def canonical_gcp_private_campaign_watchdog_binding_bytes(
    binding: GcpPrivateCampaignWatchdogBinding,
) -> bytes:
    value = _model_value(binding)
    value.pop("binding_digest", None)
    return canonical_json_bytes(value)


def gcp_private_campaign_watchdog_binding_digest(
    binding: GcpPrivateCampaignWatchdogBinding,
) -> Sha256Digest:
    return sha256_digest(canonical_gcp_private_campaign_watchdog_binding_bytes(binding))


class GcpPrivateCampaignWatchdogActivationRecord(PrecampaignModel):
    """Fsync-durable activation input, written before the worker is spawned."""

    schema_version: Literal["inferdrome.gcp-private-campaign-watchdog-record.v3"]
    binding: GcpPrivateCampaignWatchdogBinding
    activated_at: GcpTimestamp
    activation_id: Sha256Digest

    @model_validator(mode="after")
    def _activation_identity(self) -> Self:
        if self.activation_id != gcp_private_campaign_watchdog_activation_id(self):
            raise ValueError("watchdog activation identity does not match")
        return self


def canonical_gcp_private_campaign_watchdog_activation_bytes(
    record: GcpPrivateCampaignWatchdogActivationRecord,
) -> bytes:
    value = _model_value(record)
    value.pop("activation_id", None)
    return canonical_json_bytes(value)


def gcp_private_campaign_watchdog_activation_id(
    record: GcpPrivateCampaignWatchdogActivationRecord,
) -> Sha256Digest:
    return sha256_digest(
        canonical_gcp_private_campaign_watchdog_activation_bytes(record)
    )


class GcpPrivateCampaignWatchdogEvent(PrecampaignModel):
    """Hash-chained local progress record; it never contains provider IDs."""

    schema_version: Literal["inferdrome.gcp-private-campaign-watchdog-event.v3"]
    sequence: Annotated[int, Field(ge=0, le=_MAX_EVENTS - 1)]
    state: _EventState
    activation_id: Sha256Digest
    occurred_at: GcpTimestamp
    previous_event_digest: Sha256Digest | None
    worker_process_id: Annotated[int, Field(ge=1, le=2_147_483_647)]
    detail_code: Annotated[
        str | None, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,47}$")
    ] = None
    event_digest: Sha256Digest

    @model_validator(mode="after")
    def _event_identity(self) -> Self:
        if self.event_digest != gcp_private_campaign_watchdog_event_digest(self):
            raise ValueError("watchdog event identity does not match")
        return self


def canonical_gcp_private_campaign_watchdog_event_bytes(
    event: GcpPrivateCampaignWatchdogEvent,
) -> bytes:
    value = _model_value(event)
    value.pop("event_digest", None)
    return canonical_json_bytes(value)


def gcp_private_campaign_watchdog_event_digest(
    event: GcpPrivateCampaignWatchdogEvent,
) -> Sha256Digest:
    return sha256_digest(canonical_gcp_private_campaign_watchdog_event_bytes(event))


_TRANSITIONS: Final[dict[_EventState, frozenset[_EventState]]] = {
    "WORKER_READY": frozenset(
        {
            "WORKER_READY",
            "RECOVERY_ATTEMPT",
            "STOPPED",
            "CLEANUP_CONFIRMED",
            "CLEANUP_UNCONFIRMED",
        }
    ),
    "RECOVERY_ATTEMPT": frozenset(
        {
            "WORKER_READY",
            "RECOVERY_ATTEMPT",
            "PREBIND_ABSENCE_PENDING",
            "CLEANUP_CONFIRMED",
            "CLEANUP_UNCONFIRMED",
            "STOPPED",
        }
    ),
    "PREBIND_ABSENCE_PENDING": frozenset(
        {
            "WORKER_READY",
            "RECOVERY_ATTEMPT",
            "CLEANUP_CONFIRMED",
            "CLEANUP_UNCONFIRMED",
            "STOPPED",
        }
    ),
    "CLEANUP_CONFIRMED": frozenset({"CLEANUP_CONFIRMED"}),
    "CLEANUP_UNCONFIRMED": frozenset({"CLEANUP_UNCONFIRMED"}),
    "STOPPED": frozenset({"STOPPED"}),
}


class _WatchdogStore:
    """Private, no-follow activation and worker-event persistence."""

    def __init__(self, root: Path | SafeDirFD) -> None:
        self._root = root

    def _open_root(self) -> SafeDirFD:
        try:
            if isinstance(self._root, SafeDirFD):
                return SafeDirFD.from_inherited_fd(self._root.fd)
            return SafeDirFD.open(self._root)
        except (OSError, SafeDirFSError):
            raise GcpPrivateCampaignError("WATCHDOG_STORE_UNAVAILABLE") from None

    @staticmethod
    def _activation_name(controller_id: PrecampaignControllerId) -> str:
        return f"{controller_id}.gcp-private-watchdog-v3.activation.json"

    @staticmethod
    def _events_name(controller_id: PrecampaignControllerId) -> str:
        return f"{controller_id}.gcp-private-watchdog-v3.events.jsonl"

    @staticmethod
    def _lock_name(controller_id: PrecampaignControllerId) -> str:
        return f".{controller_id}.gcp-private-watchdog-v3.lock"

    @contextmanager
    def _exclusive(self, controller_id: PrecampaignControllerId) -> Iterator[SafeDirFD]:
        root = self._open_root()
        descriptor: int | None = None
        try:
            descriptor = root.open_child(
                self._lock_name(controller_id), os.O_RDWR | os.O_CREAT, 0o600
            )
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except (ImportError, OSError):
                raise GcpPrivateCampaignError("WATCHDOG_LOCK_UNAVAILABLE") from None
            yield root
        except GcpPrivateCampaignError:
            raise
        except (OSError, SafeDirFSError):
            raise GcpPrivateCampaignError("WATCHDOG_STORE_UNAVAILABLE") from None
        finally:
            if descriptor is not None:
                with suppress(ImportError, OSError):
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            root.close()

    @staticmethod
    def _write_all(descriptor: int, content: bytes) -> None:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]

    @staticmethod
    def _read_regular(root: SafeDirFD, name: str) -> bytes:
        descriptor: int | None = None
        try:
            descriptor = root.open_child(name, os.O_RDONLY)
            before = root.validated_regular_child(name, descriptor=descriptor)
            if not 1 <= before.st_size <= GCP_PRIVATE_CAMPAIGN_MAX_JOURNAL_BYTES:
                raise GcpPrivateCampaignError("WATCHDOG_RECORD_INVALID")
            content = os.read(descriptor, GCP_PRIVATE_CAMPAIGN_MAX_JOURNAL_BYTES + 1)
            after = root.validated_regular_child(name, descriptor=descriptor)
            if len(content) != before.st_size or after.st_size != before.st_size:
                raise GcpPrivateCampaignError("WATCHDOG_RECORD_CHANGED")
            return content
        except GcpPrivateCampaignError:
            raise
        except FileNotFoundError:
            raise GcpPrivateCampaignError("WATCHDOG_RECORD_MISSING") from None
        except (OSError, SafeDirFSError):
            raise GcpPrivateCampaignError("WATCHDOG_RECORD_UNAVAILABLE") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def write_activation(
        self, record: GcpPrivateCampaignWatchdogActivationRecord
    ) -> None:
        controller_id = record.binding.proposal.ownership_labels.controller_id
        content = _canonical(record)
        descriptor: int | None = None
        with self._exclusive(controller_id) as root:
            try:
                descriptor = root.open_child(
                    self._activation_name(controller_id),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                self._write_all(descriptor, content)
                os.fsync(descriptor)
                root.validated_regular_child(
                    self._activation_name(controller_id), descriptor=descriptor
                )
                root.fsync()
            except FileExistsError:
                raise GcpPrivateCampaignError("WATCHDOG_ACTIVATION_EXISTS") from None
            except GcpPrivateCampaignError:
                raise
            except (OSError, SafeDirFSError):
                raise GcpPrivateCampaignError("WATCHDOG_RECORD_UNAVAILABLE") from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def read_activation(
        self, controller_id: PrecampaignControllerId
    ) -> GcpPrivateCampaignWatchdogActivationRecord:
        root = self._open_root()
        try:
            raw = self._read_regular(root, self._activation_name(controller_id))
            record = GcpPrivateCampaignWatchdogActivationRecord.model_validate_json(raw)
            if _canonical(record) != raw:
                raise GcpPrivateCampaignError("WATCHDOG_RECORD_NONCANONICAL")
            if record.binding.proposal.ownership_labels.controller_id != controller_id:
                raise GcpPrivateCampaignError("WATCHDOG_RECORD_MISMATCH")
            return record
        except GcpPrivateCampaignError:
            raise
        except (ValidationError, ValueError, TypeError):
            raise GcpPrivateCampaignError("WATCHDOG_RECORD_INVALID") from None
        finally:
            root.close()

    @staticmethod
    def _parse_events(
        raw: bytes, *, activation_id: Sha256Digest
    ) -> tuple[GcpPrivateCampaignWatchdogEvent, ...]:
        if not raw:
            return ()
        if len(raw) > _MAX_EVENT_BYTES or not raw.endswith(b"\n"):
            raise GcpPrivateCampaignError("WATCHDOG_EVENTS_INVALID")
        rows = raw.splitlines()
        if not rows or len(rows) > _MAX_EVENTS:
            raise GcpPrivateCampaignError("WATCHDOG_EVENTS_INVALID")
        events: list[GcpPrivateCampaignWatchdogEvent] = []
        previous: Sha256Digest | None = None
        for sequence, row in enumerate(rows):
            try:
                event = GcpPrivateCampaignWatchdogEvent.model_validate_json(row)
            except (ValidationError, ValueError, TypeError):
                raise GcpPrivateCampaignError("WATCHDOG_EVENTS_INVALID") from None
            if (
                _canonical(event) != row
                or event.sequence != sequence
                or event.activation_id != activation_id
                or event.previous_event_digest != previous
            ):
                raise GcpPrivateCampaignError("WATCHDOG_EVENTS_INVALID")
            if events and event.state not in _TRANSITIONS[events[-1].state]:
                raise GcpPrivateCampaignError("WATCHDOG_EVENTS_INVALID")
            previous = event.event_digest
            events.append(event)
        return tuple(events)

    def load_events(
        self, record: GcpPrivateCampaignWatchdogActivationRecord
    ) -> tuple[GcpPrivateCampaignWatchdogEvent, ...]:
        controller_id = record.binding.proposal.ownership_labels.controller_id
        root = self._open_root()
        try:
            try:
                raw = self._read_regular(root, self._events_name(controller_id))
            except GcpPrivateCampaignError as error:
                if error.code == "WATCHDOG_RECORD_MISSING":
                    return ()
                raise
            return self._parse_events(raw, activation_id=record.activation_id)
        finally:
            root.close()

    def append_event(
        self,
        record: GcpPrivateCampaignWatchdogActivationRecord,
        *,
        state: _EventState,
        worker_process_id: int,
        occurred_at: datetime,
        detail_code: str | None = None,
    ) -> GcpPrivateCampaignWatchdogEvent:
        controller_id = record.binding.proposal.ownership_labels.controller_id
        descriptor: int | None = None
        with self._exclusive(controller_id) as root:
            try:
                try:
                    raw = self._read_regular(root, self._events_name(controller_id))
                except GcpPrivateCampaignError as error:
                    if error.code != "WATCHDOG_RECORD_MISSING":
                        raise
                    events: tuple[GcpPrivateCampaignWatchdogEvent, ...] = ()
                else:
                    events = self._parse_events(raw, activation_id=record.activation_id)
                if events and state not in _TRANSITIONS[events[-1].state]:
                    raise GcpPrivateCampaignError("WATCHDOG_EVENT_TRANSITION_INVALID")
                if not events and state != "WORKER_READY":
                    raise GcpPrivateCampaignError("WATCHDOG_EVENT_TRANSITION_INVALID")
                event_seed = GcpPrivateCampaignWatchdogEvent.model_construct(
                    schema_version=GCP_PRIVATE_CAMPAIGN_WATCHDOG_EVENT_SCHEMA_VERSION,
                    sequence=len(events),
                    state=state,
                    activation_id=record.activation_id,
                    occurred_at=_timestamp(occurred_at),
                    previous_event_digest=(events[-1].event_digest if events else None),
                    worker_process_id=worker_process_id,
                    detail_code=detail_code,
                    event_digest="sha256:" + ("0" * 64),
                )
                event = GcpPrivateCampaignWatchdogEvent(
                    **{
                        **event_seed.model_dump(mode="python"),
                        "event_digest": gcp_private_campaign_watchdog_event_digest(
                            event_seed
                        ),
                    }
                )
                payload = _canonical(event) + b"\n"
                descriptor = root.open_child(
                    self._events_name(controller_id),
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                    0o600,
                )
                root.validated_regular_child(
                    self._events_name(controller_id), descriptor=descriptor
                )
                self._write_all(descriptor, payload)
                os.fsync(descriptor)
                root.validated_regular_child(
                    self._events_name(controller_id), descriptor=descriptor
                )
                root.fsync()
                return event
            except GcpPrivateCampaignError:
                raise
            except (OSError, SafeDirFSError, ValidationError, ValueError):
                raise GcpPrivateCampaignError("WATCHDOG_EVENTS_UNAVAILABLE") from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def write_auxiliary(
        self, name: str, content: bytes, *, replace: bool
    ) -> None:
        """Persist local fake-worker state without a pathname race.

        This is intentionally internal test infrastructure.  Production
        records use only the activation and event files above.
        """

        root = self._open_root()
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT
            if replace:
                flags |= os.O_TRUNC
            else:
                flags |= os.O_EXCL
            descriptor = root.open_child(name, flags, 0o600)
            self._write_all(descriptor, content)
            os.fsync(descriptor)
            root.validated_regular_child(name, descriptor=descriptor)
            root.fsync()
        except FileExistsError:
            raise GcpPrivateCampaignError("WATCHDOG_AUXILIARY_EXISTS") from None
        except (OSError, SafeDirFSError):
            raise GcpPrivateCampaignError("WATCHDOG_AUXILIARY_UNAVAILABLE") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            root.close()

    def read_auxiliary(self, name: str) -> bytes:
        root = self._open_root()
        try:
            return self._read_regular(root, name)
        finally:
            root.close()


class GcpPrivateCampaignWatchdogCapability:
    """Opaque, one-process capability consumed by the live Google factory."""

    __slots__ = ("_activation_id", "_controller_id", "_watchdog", "_worker_pid")

    def __init__(
        self,
        token: object,
        *,
        watchdog: GcpPrivateCampaignWatchdog,
        activation_id: Sha256Digest,
        controller_id: PrecampaignControllerId,
        worker_pid: int,
    ) -> None:
        if token is not _CAPABILITY_TOKEN:
            raise GcpPrivateCampaignError("WATCHDOG_CAPABILITY_FORGED")
        self._watchdog = watchdog
        self._activation_id = activation_id
        self._controller_id = controller_id
        self._worker_pid = worker_pid

    def assert_active(self) -> None:
        self._watchdog._assert_active(
            activation_id=self._activation_id,
            controller_id=self._controller_id,
            worker_pid=self._worker_pid,
        )


@dataclass(frozen=True)
class _WorkerHandle:
    process: subprocess.Popen[bytes]
    activation_id: Sha256Digest
    controller_id: PrecampaignControllerId


class GcpPrivateCampaignWatchdog:
    """Launch and reverify one independently running cleanup-only worker."""

    def __init__(
        self,
        *,
        watchdog_root: Path,
        journal_root: Path,
        worker_backend: _WorkerBackend = "google_cleanup_only_v1",
        worker_poll_seconds: float = 0.1,
        _parent_process_id: int | None = None,
        _now: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not watchdog_root.is_absolute()
            or not journal_root.is_absolute()
            or worker_backend not in {"google_cleanup_only_v1", "file_fake_cleanup_v1"}
            or not 0.02 <= worker_poll_seconds <= 5.0
            or (_parent_process_id is not None and _parent_process_id < 1)
        ):
            raise GcpPrivateCampaignError("WATCHDOG_CONFIGURATION_INVALID")
        self._watchdog_root = watchdog_root
        self._journal_root = journal_root
        self._worker_backend = worker_backend
        self._worker_poll_seconds = worker_poll_seconds
        self._parent_process_id = _parent_process_id
        self._now = _now or (lambda: datetime.now(UTC))
        self._handle: _WorkerHandle | None = None
        self._capability: GcpPrivateCampaignWatchdogCapability | None = None

    @staticmethod
    def _worker_environment() -> dict[str, str]:
        """Do not pass approval, secret, proxy, or credential env state onward."""

        return {
            "PATH": os.defpath,
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONPATH": os.fspath(_SOURCE_ROOT),
        }

    @staticmethod
    def _worker_cwd() -> str:
        root: SafeDirFD | None = None
        try:
            root = SafeDirFD.open(_SOURCE_ROOT)
            return os.fspath(_SOURCE_ROOT)
        except (OSError, SafeDirFSError):
            raise GcpPrivateCampaignError(
                "WATCHDOG_WORKER_IMPORT_UNAVAILABLE"
            ) from None
        finally:
            if root is not None:
                root.close()

    @staticmethod
    def _kill_process(process: subprocess.Popen[bytes]) -> None:
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired, OSError):
            process.wait(timeout=0.2)

    def _spawn_worker(
        self, record: GcpPrivateCampaignWatchdogActivationRecord
    ) -> tuple[subprocess.Popen[bytes], int]:
        root: SafeDirFD | None = None
        ready_read: int | None = None
        ready_write: int | None = None
        process: subprocess.Popen[bytes] | None = None
        handed_off = False
        try:
            root = SafeDirFD.open(self._watchdog_root)
            ready_read, ready_write = os.pipe()
            command = [
                sys.executable,
                "-P",
                "-m",
                "inferdrome.deployment.gcp_private_campaign_watchdog_v3",
                "worker",
                "--watchdog-root-fd",
                str(root.fd),
                "--journal-root",
                os.fspath(self._journal_root),
                "--controller-id",
                record.binding.proposal.ownership_labels.controller_id,
                "--ready-fd",
                str(ready_write),
                "--poll-ms",
                str(max(20, int(self._worker_poll_seconds * 1000))),
            ]
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(root.fd, ready_write),
                start_new_session=True,
                env=self._worker_environment(),
                cwd=self._worker_cwd(),
            )
            os.close(ready_write)
            ready_write = None
            handed_off = True
            return process, ready_read
        except (OSError, ValueError):
            raise GcpPrivateCampaignError("WATCHDOG_WORKER_UNAVAILABLE") from None
        finally:
            if root is not None:
                root.close()
            if ready_write is not None:
                with suppress(OSError):
                    os.close(ready_write)
            if ready_read is not None and not handed_off:
                with suppress(OSError):
                    os.close(ready_read)
            if process is not None and not handed_off:
                self._kill_process(process)

    @staticmethod
    def _await_ready(descriptor: int) -> None:
        try:
            readable, _, _ = select.select([descriptor], [], [], _READY_TIMEOUT_SECONDS)
            if not readable or os.read(descriptor, 1) != b"A":
                raise GcpPrivateCampaignError("WATCHDOG_WORKER_UNAVAILABLE")
        except GcpPrivateCampaignError:
            raise
        except OSError:
            raise GcpPrivateCampaignError("WATCHDOG_WORKER_UNAVAILABLE") from None

    def activate(
        self,
        *,
        proposal: GcpPrivateCampaignProposal,
        approval: GcpPrivateCampaignApproval,
        cleanup_authorization: GcpPrivateCampaignCleanupAuthorization,
        request: GcpPrivateCampaignCreateRequest,
        execution_deadline: datetime,
        now: datetime,
    ) -> GcpPrivateCampaignWatchdogCapability:
        """Persist, start, and positively reverify the detached worker."""

        verify_gcp_private_campaign_approval(
            approval, expected_proposal=proposal, now=now
        )
        verify_gcp_private_campaign_cleanup_authorization(
            cleanup_authorization, expected_proposal=proposal, now=now
        )
        if request.proposal != proposal or now >= execution_deadline:
            raise GcpPrivateCampaignError("WATCHDOG_ACTIVATION_INVALID")
        cleanup_deadline = execution_deadline + timedelta(
            seconds=proposal.cleanup_horizon_seconds
        )
        parent_process_id = self._parent_process_id or os.getpid()
        binding_seed = GcpPrivateCampaignWatchdogBinding.model_construct(
            schema_version=GCP_PRIVATE_CAMPAIGN_WATCHDOG_SCHEMA_VERSION,
            proposal=proposal,
            request=request,
            cleanup_authorization=cleanup_authorization,
            approval_sha256=sha256_digest(_canonical(approval)),
            journal_root_sha256=sha256_digest(
                os.fspath(self._journal_root).encode("utf-8")
            ),
            execution_deadline_at=_timestamp(execution_deadline),
            cleanup_deadline_at=_timestamp(cleanup_deadline),
            parent_process_id=parent_process_id,
            worker_backend=self._worker_backend,
            binding_digest="sha256:" + ("0" * 64),
        )
        binding = GcpPrivateCampaignWatchdogBinding(
            **{
                **binding_seed.model_dump(mode="python"),
                "binding_digest": gcp_private_campaign_watchdog_binding_digest(
                    binding_seed
                ),
            }
        )
        record_seed = GcpPrivateCampaignWatchdogActivationRecord.model_construct(
            schema_version=GCP_PRIVATE_CAMPAIGN_WATCHDOG_RECORD_SCHEMA_VERSION,
            binding=binding,
            activated_at=_timestamp(now),
            activation_id="sha256:" + ("0" * 64),
        )
        record = GcpPrivateCampaignWatchdogActivationRecord(
            **{
                **record_seed.model_dump(mode="python"),
                "activation_id": gcp_private_campaign_watchdog_activation_id(
                    record_seed
                ),
            }
        )
        store = _WatchdogStore(self._watchdog_root)
        store.write_activation(record)
        process, ready_descriptor = self._spawn_worker(record)
        accepted = False
        try:
            self._await_ready(ready_descriptor)
            events = store.load_events(record)
            if (
                len(events) != 1
                or events[0].state != "WORKER_READY"
                or events[0].worker_process_id != process.pid
                or process.poll() is not None
            ):
                raise GcpPrivateCampaignError("WATCHDOG_WORKER_UNAVAILABLE")
            controller_id = record.binding.proposal.ownership_labels.controller_id
            self._handle = _WorkerHandle(
                process, record.activation_id, controller_id
            )
            capability = GcpPrivateCampaignWatchdogCapability(
                _CAPABILITY_TOKEN,
                watchdog=self,
                activation_id=record.activation_id,
                controller_id=controller_id,
                worker_pid=process.pid,
            )
            capability.assert_active()
            self._capability = capability
            accepted = True
            return capability
        finally:
            with suppress(OSError):
                os.close(ready_descriptor)
            if not accepted:
                self._kill_process(process)

    def _assert_active(
        self,
        *,
        activation_id: Sha256Digest,
        controller_id: PrecampaignControllerId,
        worker_pid: int,
    ) -> None:
        """Fail before SDK construction if the exact ready worker is not live."""

        handle = self._handle
        if (
            handle is None
            or handle.activation_id != activation_id
            or handle.controller_id != controller_id
            or handle.process.pid != worker_pid
        ):
            raise GcpPrivateCampaignError("WATCHDOG_WORKER_NOT_ACTIVE")
        store = _WatchdogStore(self._watchdog_root)
        record = store.read_activation(controller_id)
        events = store.load_events(record)
        if (
            record.activation_id != activation_id
            or record.binding.proposal.ownership_labels.controller_id != controller_id
            or not events
            or events[0].state != "WORKER_READY"
            or events[0].worker_process_id != worker_pid
        ):
            raise GcpPrivateCampaignError("WATCHDOG_ACTIVATION_MISMATCH")
        if self._now() >= _parse_timestamp(record.binding.execution_deadline_at):
            raise GcpPrivateCampaignError("WATCHDOG_ACTIVATION_EXPIRED")
        if handle.process.poll() is not None:
            raise GcpPrivateCampaignError("WATCHDOG_WORKER_NOT_ACTIVE")

    def assert_active_for_testing(self) -> None:
        """Exercise the same liveness gate without exposing capability fields."""

        if self._handle is None:
            raise GcpPrivateCampaignError("WATCHDOG_WORKER_NOT_ACTIVE")
        self._assert_active(
            activation_id=self._handle.activation_id,
            controller_id=self._handle.controller_id,
            worker_pid=self._handle.process.pid,
        )

    def live_capability(self) -> GcpPrivateCampaignWatchdogCapability:
        """Return the single capability only after a fresh liveness recheck."""

        capability = self._capability
        if capability is None:
            raise GcpPrivateCampaignError("WATCHDOG_CAPABILITY_UNAVAILABLE")
        capability.assert_active()
        return capability

    def resume_cleanup_only(self, *, controller_id: PrecampaignControllerId) -> int:
        """Restart a dead cleanup worker after the parent has exited.

        This method deliberately cannot restore a live-create capability.  It
        only reopens the durable activation record and starts the same
        cleanup-only child after confirming that the original controller PID
        is gone and the sidecar has not already reached a terminal result.
        """

        store = _WatchdogStore(self._watchdog_root)
        record = store.read_activation(controller_id)
        if _pid_alive(record.binding.parent_process_id):
            raise GcpPrivateCampaignError("WATCHDOG_PARENT_STILL_ACTIVE")
        events = store.load_events(record)
        if events and events[-1].state in {
            "CLEANUP_CONFIRMED",
            "CLEANUP_UNCONFIRMED",
            "STOPPED",
        }:
            raise GcpPrivateCampaignError("WATCHDOG_RECOVERY_ALREADY_TERMINAL")
        process, ready_descriptor = self._spawn_worker(record)
        accepted = False
        try:
            self._await_ready(ready_descriptor)
            refreshed = store.load_events(record)
            if (
                not refreshed
                or refreshed[-1].state != "WORKER_READY"
                or refreshed[-1].worker_process_id != process.pid
                or process.poll() is not None
            ):
                raise GcpPrivateCampaignError("WATCHDOG_WORKER_UNAVAILABLE")
            self._handle = _WorkerHandle(process, record.activation_id, controller_id)
            accepted = True
            return process.pid
        finally:
            with suppress(OSError):
                os.close(ready_descriptor)
            if not accepted:
                self._kill_process(process)


def _pid_alive(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class GcpPrivateCampaignFileFakeWorkerState(PrecampaignModel):
    """Cross-process local fake state used only by watchdog acceptance tests."""

    schema_version: Literal["inferdrome.gcp-private-watchdog-file-fake.v1"]
    proposal_id: Sha256Digest
    instance_present: bool
    boot_disk_present: bool
    invisible_observations_remaining: Annotated[int, Field(ge=0, le=8)] = 0
    delete_failures_remaining: Annotated[int, Field(ge=0, le=8)] = 0
    ambiguous_residuals: bool = False
    create_request_digest_mismatch: bool = False
    replacement_provider_instance_id: Annotated[
        str | None, StringConstraints(pattern=r"^[1-9][0-9]{0,18}$")
    ] = None
    provider_instance_id: Annotated[
        str, StringConstraints(pattern=r"^[1-9][0-9]{0,18}$")
    ] = "123456789"
    provider_boot_disk_id: Annotated[
        str, StringConstraints(pattern=r"^[1-9][0-9]{0,18}$")
    ] = "246813579"
    delete_attempts: Annotated[int, Field(ge=0, le=64)] = 0
    delete_calls: Annotated[int, Field(ge=0, le=64)] = 0
    boot_disk_delete_calls: Annotated[int, Field(ge=0, le=64)] = 0


def _fake_state_name(controller_id: PrecampaignControllerId) -> str:
    return f"{controller_id}.gcp-private-watchdog-v3.file-fake.json"


def write_gcp_private_campaign_file_fake_worker_state(
    *, watchdog_root: Path, controller_id: PrecampaignControllerId,
    state: GcpPrivateCampaignFileFakeWorkerState,
) -> None:
    """Install a no-provider test backend before starting a fake worker."""

    _WatchdogStore(watchdog_root).write_auxiliary(
        _fake_state_name(controller_id), _canonical(state), replace=False
    )


def read_gcp_private_campaign_file_fake_worker_state(
    *, watchdog_root: Path, controller_id: PrecampaignControllerId
) -> GcpPrivateCampaignFileFakeWorkerState:
    return _read_file_fake_state(
        _WatchdogStore(watchdog_root), controller_id=controller_id
    )


def _read_file_fake_state(
    store: _WatchdogStore, *, controller_id: PrecampaignControllerId
) -> GcpPrivateCampaignFileFakeWorkerState:
    raw = store.read_auxiliary(_fake_state_name(controller_id))
    try:
        state = GcpPrivateCampaignFileFakeWorkerState.model_validate_json(raw)
    except (ValidationError, ValueError, TypeError):
        raise GcpPrivateCampaignError("WATCHDOG_FAKE_STATE_INVALID") from None
    if _canonical(state) != raw:
        raise GcpPrivateCampaignError("WATCHDOG_FAKE_STATE_INVALID")
    return state


class _FileFakeCleanupTransport:
    """Exact cleanup-only adapter reconstructed by a fresh local worker."""

    def __init__(
        self,
        *,
        store: _WatchdogStore,
        record: GcpPrivateCampaignWatchdogActivationRecord,
    ) -> None:
        self._store = store
        self._record = record
        self._controller_id = record.binding.proposal.ownership_labels.controller_id
        self._bound_principal: str | None = None

    def _state(self) -> GcpPrivateCampaignFileFakeWorkerState:
        state = _read_file_fake_state(self._store, controller_id=self._controller_id)
        if state.proposal_id != self._record.binding.proposal.proposal_id:
            raise GcpPrivateCampaignError("WATCHDOG_FAKE_STATE_MISMATCH")
        return state

    def _write_state(self, state: GcpPrivateCampaignFileFakeWorkerState) -> None:
        self._store.write_auxiliary(
            _fake_state_name(self._controller_id), _canonical(state), replace=True
        )

    def bind_controller_principal(self, controller_principal: str) -> None:
        expected = self._record.binding.proposal.iap_connectivity.controller_principal
        if controller_principal != expected:
            raise GcpPrivateCampaignError("WATCHDOG_FAKE_PRINCIPAL_MISMATCH")
        self._bound_principal = controller_principal

    def _require_principal(self) -> None:
        if self._bound_principal is None:
            raise GcpPrivateCampaignError("WATCHDOG_FAKE_PRINCIPAL_UNBOUND")

    @staticmethod
    def _operation(
        kind: Literal["delete_instance", "delete_boot_disk"], request_id: str
    ) -> Any:
        from inferdrome.deployment.gcp_private_campaign_v2 import (
            GcpPrivateCampaignOperation,
        )

        suffix = sha256_digest(f"{kind}:{request_id}".encode())[7:23]
        return GcpPrivateCampaignOperation(
            operation_id=f"pcop-{suffix}",
            operation_kind=kind,
            request_id=request_id,
            provider_operation_name=f"file-fake-{kind}-{suffix}",
        )

    def observe_exact_instance(self, request: Any, *, timeout_seconds: int) -> Any:
        del timeout_seconds
        from inferdrome.deployment.gcp_private_campaign_v2 import (
            GcpPrivateCampaignInstanceObservation,
            GcpPrivateCampaignTransportError,
        )

        self._require_principal()
        state = self._state()
        if state.invisible_observations_remaining:
            self._write_state(
                state.model_copy(
                    update={
                        "invisible_observations_remaining": (
                            state.invisible_observations_remaining - 1
                        )
                    }
                )
            )
            raise GcpPrivateCampaignTransportError("INSTANCE_NOT_FOUND")
        if not state.instance_present:
            raise GcpPrivateCampaignTransportError("INSTANCE_NOT_FOUND")
        proposal = request.proposal
        provider_instance_id = (
            state.replacement_provider_instance_id or state.provider_instance_id
        )
        return GcpPrivateCampaignInstanceObservation(
            proposal_id=proposal.proposal_id,
            project_id=proposal.topology.project_id,
            zone=proposal.topology.zone,
            instance_name=proposal.instance_name,
            provider_instance_id=provider_instance_id,
            ownership_labels=proposal.ownership_labels,
            machine_type="a2-highgpu-2g",
            accelerator_model="NVIDIA A100-SXM4-40GB",
            accelerator_provider_type="nvidia-tesla-a100",
            accelerator_count=2,
            machine_fixed_local_ssd_count=2,
            state="RUNNING",
            external_access="ABSENT",
            boot_disk_name=proposal.boot_disk_name,
            boot_disk_provider_id_sha256=sha256_digest(
                state.provider_boot_disk_id.encode("ascii")
            ),
            boot_disk_auto_delete=True,
            persistent_disk_count=0,
            max_runtime_seconds=proposal.max_runtime_seconds,
            instance_termination_action="DELETE",
            startup_payload_digest=proposal.startup_payload_digest,
            create_request_digest=(
                request.create_request_digest
                if not state.create_request_digest_mismatch
                else "sha256:" + ("f" * 64)
            ),
            startup_script_sha256="sha256:" + ("5" * 64),
        )

    def wait_operation(self, operation: Any, *, timeout_seconds: int) -> Any:
        del timeout_seconds
        from inferdrome.deployment.gcp_private_campaign_v2 import (
            GcpPrivateCampaignOperationResult,
        )

        self._require_principal()
        return GcpPrivateCampaignOperationResult(operation=operation, status="DONE")

    def _disk(self, request: Any, state: GcpPrivateCampaignFileFakeWorkerState) -> Any:
        from inferdrome.deployment.gcp_private_campaign_v2 import (
            GcpPrivateCampaignDiskObservation,
        )

        proposal = request.proposal
        provider_instance_id = (
            state.replacement_provider_instance_id or state.provider_instance_id
        )
        return GcpPrivateCampaignDiskObservation(
            disk_name=proposal.boot_disk_name,
            provider_disk_id_sha256=sha256_digest(
                state.provider_boot_disk_id.encode("ascii")
            ),
            ownership_labels=proposal.ownership_labels,
            source_boot_image_identity=proposal.boot_image.boot_image_identity,
            attached_provider_instance_id=(
                provider_instance_id if state.instance_present else None
            ),
            attachment_state="ATTACHED" if state.instance_present else "DETACHED",
            boot_attachment=True,
        )

    def list_exact_owned_residuals(self, request: Any, *, timeout_seconds: int) -> Any:
        del timeout_seconds
        from inferdrome.deployment.gcp_private_campaign_v2 import (
            GcpPrivateCampaignOwnedResidualInventory,
        )

        self._require_principal()
        state = self._state()
        proposal = request.proposal
        disks: tuple[Any, ...] = (
            (self._disk(request, state),) if state.boot_disk_present else ()
        )
        if state.ambiguous_residuals and disks:
            disks = (*disks, disks[0])
        return GcpPrivateCampaignOwnedResidualInventory(
            proposal_id=proposal.proposal_id,
            project_id=proposal.topology.project_id,
            zone=proposal.topology.zone,
            ownership_labels=proposal.ownership_labels,
            pagination_complete=True,
            instance_state="PRESENT" if state.instance_present else "ABSENT",
            disks=disks,
        )

    def delete_exact_instance(
        self, request: Any, *, exact_resource_binding: Any, request_id: str,
        timeout_seconds: int,
    ) -> Any:
        del timeout_seconds
        from inferdrome.deployment.gcp_private_campaign_v2 import (
            GcpPrivateCampaignTransportError,
        )

        self._require_principal()
        state = self._state()
        provider_instance_id = (
            state.replacement_provider_instance_id or state.provider_instance_id
        )
        if (
            not state.instance_present
            or sha256_digest(provider_instance_id.encode("ascii"))
            != exact_resource_binding.provider_instance_id_sha256
        ):
            raise GcpPrivateCampaignTransportError("EXACT_RESOURCE_BINDING_MISMATCH")
        if state.delete_failures_remaining:
            self._write_state(
                state.model_copy(
                    update={
                        "delete_failures_remaining": (
                            state.delete_failures_remaining - 1
                        ),
                        "delete_attempts": state.delete_attempts + 1,
                    }
                )
            )
            raise GcpPrivateCampaignTransportError("FAKE_DELETE_RETRY")
        self._write_state(
            state.model_copy(
                update={
                    "instance_present": False,
                    "delete_attempts": state.delete_attempts + 1,
                    "delete_calls": state.delete_calls + 1,
                }
            )
        )
        return self._operation("delete_instance", request_id)

    def reconcile_delete_instance(self, *args: Any, **kwargs: Any) -> Any:
        return self.delete_exact_instance(*args, **kwargs)

    def delete_exact_owned_boot_disk(
        self, request: Any, disk: Any, *, exact_resource_binding: Any,
        request_id: str, timeout_seconds: int,
    ) -> Any:
        del disk, timeout_seconds
        from inferdrome.deployment.gcp_private_campaign_v2 import (
            GcpPrivateCampaignTransportError,
        )

        self._require_principal()
        state = self._state()
        if (
            not state.boot_disk_present
            or sha256_digest(state.provider_boot_disk_id.encode("ascii"))
            != exact_resource_binding.boot_disk_provider_id_sha256
        ):
            raise GcpPrivateCampaignTransportError("EXACT_RESOURCE_BINDING_MISMATCH")
        self._write_state(
            state.model_copy(
                update={
                    "boot_disk_present": False,
                    "boot_disk_delete_calls": state.boot_disk_delete_calls + 1,
                }
            )
        )
        return self._operation("delete_boot_disk", request_id)

    def reconcile_delete_exact_owned_boot_disk(self, *args: Any, **kwargs: Any) -> Any:
        return self.delete_exact_owned_boot_disk(*args, **kwargs)

    def confirm_exact_absence(self, request: Any, *, timeout_seconds: int) -> Any:
        del timeout_seconds
        from inferdrome.deployment.gcp_private_campaign_v2 import (
            GcpPrivateCampaignAbsenceObservation,
            GcpPrivateCampaignTransportError,
        )

        self._require_principal()
        state = self._state()
        if state.instance_present or state.boot_disk_present:
            raise GcpPrivateCampaignTransportError("FALSE_ABSENCE")
        proposal = request.proposal
        return GcpPrivateCampaignAbsenceObservation(
            proposal_id=proposal.proposal_id,
            project_id=proposal.topology.project_id,
            zone=proposal.topology.zone,
            ownership_labels=proposal.ownership_labels,
            pagination_complete=True,
            instance_absent=True,
            boot_disk_absent=True,
            no_other_owned_billable_residuals=True,
        )

    def discover_exact_owned(self, request: Any, *, timeout_seconds: int) -> Any:
        return self.list_exact_owned_residuals(request, timeout_seconds=timeout_seconds)

    def close(self) -> None:
        return None


def _worker_cleanup_factory(
    record: GcpPrivateCampaignWatchdogActivationRecord,
    *,
    store: _WatchdogStore,
) -> Any:
    if record.binding.worker_backend == "google_cleanup_only_v1":
        # This import is intentionally inside the independently running worker
        # and after it has decided cleanup is necessary.  Parent activation,
        # readiness, and local/fake tests never import the optional SDK.
        from inferdrome.deployment.gcp_private_campaign_google import (
            create_google_private_campaign_cleanup_transport,
        )

        return create_google_private_campaign_cleanup_transport
    if record.binding.worker_backend == "file_fake_cleanup_v1":
        return lambda: _FileFakeCleanupTransport(store=store, record=record)
    raise GcpPrivateCampaignError("WATCHDOG_FAKE_BACKEND_UNAVAILABLE")


def _worker_recover(
    record: GcpPrivateCampaignWatchdogActivationRecord,
    *,
    store: _WatchdogStore,
    journal_root: Path,
    prebind_absence_observed: bool,
) -> GcpPrivateCampaignWatchdogRecoveryProgress:
    factory = _worker_cleanup_factory(record, store=store)
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(journal_root),
        transport_factory=lambda: _raise_watchdog_create_forbidden(),
        cleanup_transport_factory=factory,
    )
    return controller.recover_watchdog_cleanup(
        proposal=record.binding.proposal,
        authorization=record.binding.cleanup_authorization,
        startup_payload=record.binding.request.startup_payload,
        prebind_absence_observed=prebind_absence_observed,
    )


def _raise_watchdog_create_forbidden() -> Any:
    raise GcpPrivateCampaignError("WATCHDOG_CREATE_FORBIDDEN")


def _worker_main(arguments: argparse.Namespace) -> int:
    root: SafeDirFD | None = None
    try:
        root = SafeDirFD.from_inherited_fd(arguments.watchdog_root_fd)
        store = _WatchdogStore(root)
        record = store.read_activation(arguments.controller_id)
        journal_root = Path(arguments.journal_root)
        if (
            not journal_root.is_absolute()
            or sha256_digest(os.fspath(journal_root).encode("utf-8"))
            != record.binding.journal_root_sha256
        ):
            return 2
        checked_journal_root = SafeDirFD.open(journal_root)
        checked_journal_root.close()
        worker_pid = os.getpid()
        store.append_event(
            record,
            state="WORKER_READY",
            worker_process_id=worker_pid,
            occurred_at=datetime.now(UTC),
        )
        os.write(arguments.ready_fd, b"A")
        # Give the parent a bounded opportunity to re-read the durable READY
        # receipt and gate the optional SDK before this child begins any
        # cleanup decision.  A controller crash in this interval is still
        # recovered on the next poll; the worker never grants a create edge.
        time.sleep(arguments.poll_ms / 1000)
        prebind_absence_observed = False
        recovery_announced = False
        while True:
            now = datetime.now(UTC)
            journal = GcpPrivateCampaignJournal(journal_root)
            try:
                core_events = journal.load(record.binding.proposal)
            except GcpPrivateCampaignError:
                core_events = ()
            if core_events and core_events[-1].state == "CLEANUP_CONFIRMED":
                store.append_event(
                    record,
                    state="STOPPED",
                    worker_process_id=worker_pid,
                    occurred_at=now,
                    detail_code="PARENT_CLEANUP_CONFIRMED",
                )
                return 0
            if (
                _pid_alive(record.binding.parent_process_id)
                and now < _parse_timestamp(record.binding.execution_deadline_at)
            ):
                time.sleep(arguments.poll_ms / 1000)
                continue
            # The evidence journal records state transitions, not every
            # bounded cleanup retry.  Otherwise a transient provider outage
            # could exhaust the fixed journal budget and kill the very actor
            # responsible for cleanup before its cleanup horizon expires.
            if not recovery_announced:
                store.append_event(
                    record,
                    state="RECOVERY_ATTEMPT",
                    worker_process_id=worker_pid,
                    occurred_at=now,
                )
                recovery_announced = True
            progress = _worker_recover(
                record,
                store=store,
                journal_root=journal_root,
                prebind_absence_observed=prebind_absence_observed,
            )
            if progress.disposition == "CLEANUP_CONFIRMED":
                store.append_event(
                    record,
                    state="CLEANUP_CONFIRMED",
                    worker_process_id=worker_pid,
                    occurred_at=datetime.now(UTC),
                )
                return 0
            if (
                progress.retry_code == "PREBIND_ABSENCE_PENDING"
                and not prebind_absence_observed
            ):
                prebind_absence_observed = True
                store.append_event(
                    record,
                    state="PREBIND_ABSENCE_PENDING",
                    worker_process_id=worker_pid,
                    occurred_at=datetime.now(UTC),
                )
            if now >= _parse_timestamp(record.binding.cleanup_deadline_at):
                try:
                    controller = GcpPrivateCampaignLifecycleController(
                        journal=journal,
                        transport_factory=lambda: _raise_watchdog_create_forbidden(),
                    )
                    controller._ensure_cleanup_intent(record.binding.proposal)
                    controller._record_cleanup_unconfirmed(record.binding.proposal)
                except GcpPrivateCampaignError:
                    pass
                store.append_event(
                    record,
                    state="CLEANUP_UNCONFIRMED",
                    worker_process_id=worker_pid,
                    occurred_at=datetime.now(UTC),
                    detail_code=progress.retry_code or "WATCHDOG_RECOVERY_RETRY",
                )
                return 2
            time.sleep(arguments.poll_ms / 1000)
    except (GcpPrivateCampaignError, OSError, SafeDirFSError, ValueError):
        return 2
    finally:
        with suppress(OSError):
            os.close(arguments.ready_fd)
        if root is not None:
            root.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("worker", add_help=False)
    worker.add_argument("--watchdog-root-fd", type=int, required=True)
    worker.add_argument("--journal-root", type=str, required=True)
    worker.add_argument("--controller-id", type=str, required=True)
    worker.add_argument("--ready-fd", type=int, required=True)
    worker.add_argument("--poll-ms", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command != "worker" or not 20 <= arguments.poll_ms <= 5_000:
        return 2
    return _worker_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
