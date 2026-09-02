"""Fresh-interpreter entrypoint for the v2 local watchdog.

The parent watchdog never forks Python and never passes an approval, payload,
credential, supplier, or provider object to this process.  It inherits only a
verified directory descriptor plus a controller identifier and reopens the
fsync-backed event journal relative to that descriptor before it can report
readiness or accept one bounded cleanup work item.

For the capability-only v2 route, a second inherited core-journal descriptor
lets the worker validate a no-secret durable spec, exact lease anchor, root
identities, and current watchdog event before readiness. It then performs
actual local fake instance/disk cleanup through that descriptor-bound state.
The legacy result-fixture path remains offline-test-only and cannot mint a
future mutation handoff. Neither path may import an SDK or smuggle an
in-memory supplier through argv, environment, or an exec-isolated process.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import ValidationError

from inferdrome.deployment.gcp_lifecycle import (
    _CONTROLLER_RE,
    _parse_timestamp,
    _timestamp,
)
from inferdrome.deployment.gcp_securefs import SafeDirFD, SafeDirFSError
from inferdrome.deployment.gcp_supervisor import (
    GCP_FILE_WATCHDOG_MAX_EVENTS,
    GCP_FILE_WATCHDOG_MAX_RESULT_BYTES,
    FileGcpWatchdog,
    GcpFileWatchdogEvent,
    GcpFileWatchdogState,
    GcpWatchdogCleanupResult,
    GcpWatchdogPrebindCleanupResult,
    _file_watchdog_event,
    _model_value,
)
from inferdrome.deployment.gcp_watchdog_backend import (
    GcpFileWatchdogBackendError,
    worker_cleanup,
    worker_validate_ready,
)
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.ids import Sha256Digest

_EVENT_SUFFIX: Final = ".file-watchdog-v2.events.jsonl"
_FIXTURE_SUFFIX: Final = ".file-watchdog-v2.cleanup-result.json"
_MAX_EVENT_BYTES: Final = 131_072


def _event_name(controller_id: str) -> str:
    if _CONTROLLER_RE.fullmatch(controller_id) is None:
        raise ValueError("invalid controller identity")
    return f"{controller_id}{_EVENT_SUFFIX}"


def _fixture_name(controller_id: str) -> str:
    if _CONTROLLER_RE.fullmatch(controller_id) is None:
        raise ValueError("invalid controller identity")
    return f"{controller_id}{_FIXTURE_SUFFIX}"


def _validated_child(root: SafeDirFD, name: str, descriptor: int) -> os.stat_result:
    """Fail closed if a worker journal child was replaced during descriptor I/O."""

    try:
        return root.validated_regular_child(name, descriptor=descriptor)
    except (OSError, SafeDirFSError):
        raise ValueError("worker journal child is unsafe") from None


def _read_all(descriptor: int, maximum: int) -> bytes:
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
        if not chunk:
            return b"".join(parts)
        parts.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise ValueError("worker input exceeds bound")


def _read_events(
    root: SafeDirFD, controller_id: str
) -> tuple[GcpFileWatchdogEvent, ...]:
    """Open and validate the complete bounded durable event chain.

    Readiness cannot be derived from a convenient final line: a truncated,
    substituted, or chain-broken prefix is ambiguous and must fail closed.
    The inherited directory descriptor is fsynced after validation so the
    worker refuses to acknowledge a root it cannot durably reopen.
    """

    descriptor: int | None = None
    try:
        descriptor = root.open_child(
            _event_name(controller_id), os.O_RDONLY | os.O_NONBLOCK
        )
        initial = _validated_child(root, _event_name(controller_id), descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_size <= 0
            or initial.st_size > _MAX_EVENT_BYTES
            or initial.st_mode & 0o022
        ):
            raise ValueError("unsafe worker journal")
        raw = _read_all(descriptor, _MAX_EVENT_BYTES)
        final = _validated_child(root, _event_name(controller_id), descriptor)
        if (
            final.st_ino != initial.st_ino
            or final.st_size != initial.st_size
            or final.st_size != len(raw)
            or not raw.endswith(b"\n")
        ):
            raise ValueError("changed worker journal")
        events: list[GcpFileWatchdogEvent] = []
        previous: Sha256Digest | None = None
        binding = None
        for sequence, line in enumerate(raw.splitlines()):
            if sequence >= GCP_FILE_WATCHDOG_MAX_EVENTS:
                raise ValueError("worker journal exceeds event bound")
            event = GcpFileWatchdogEvent.model_validate_json(line)
            if (
                canonical_json_bytes(_model_value(event)) != line
                or event.sequence != sequence
                or event.previous_event_digest != previous
            ):
                raise ValueError("noncanonical worker event")
            if binding is not None and event.binding != binding:
                raise ValueError("worker binding changed")
            previous = str(event.event_digest)
            binding = event.binding
            events.append(event)
        if not events:
            raise ValueError("empty worker journal")
        FileGcpWatchdog._validate_transitions(events)
        root.fsync()
        return tuple(events)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_latest_event(root: SafeDirFD, controller_id: str) -> GcpFileWatchdogEvent:
    return _read_events(root, controller_id)[-1]


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("worker pipe write failed")
        view = view[written:]


@contextmanager
def _exclusive(root: SafeDirFD) -> Iterator[None]:
    """Lock the inherited root without reopening its mutable pathname."""

    descriptor: int | None = None
    try:
        descriptor = root.open_child(
            ".file-watchdog-v2.lock",
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        metadata = _validated_child(root, ".file-watchdog-v2.lock", descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o022
            or metadata.st_uid != os.getuid()
        ):
            raise ValueError("unsafe worker lock")
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _validated_child(root, ".file-watchdog-v2.lock", descriptor)
        yield
    finally:
        if descriptor is not None:
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            os.close(descriptor)


def _append_event(
    root: SafeDirFD,
    events: tuple[GcpFileWatchdogEvent, ...],
    *,
    state: GcpFileWatchdogState,
    now: datetime,
    cleanup_attempts: int | None = None,
    cleanup_result: GcpWatchdogCleanupResult | None = None,
    prebind_cleanup_result: GcpWatchdogPrebindCleanupResult | None = None,
    error_code: str | None = None,
) -> GcpFileWatchdogEvent:
    """Append one exact canonical worker transition through the held dirfd."""

    if not events or len(events) >= GCP_FILE_WATCHDOG_MAX_EVENTS:
        raise ValueError("worker journal append bound")
    prior = events[-1]
    event = _file_watchdog_event(
        sequence=len(events),
        state=state,
        binding=prior.binding,
        watchdog_id=prior.watchdog_id,
        armed_at=prior.armed_at,
        expires_at=prior.expires_at,
        capability_id=prior.capability_id,
        mutation_capability=prior.mutation_capability,
        activation_receipt_id=prior.activation_receipt_id,
        provider_runtime_deadline_at=prior.provider_runtime_deadline_at,
        watchdog_cleanup_deadline_at=prior.watchdog_cleanup_deadline_at,
        runner_process_id=prior.runner_process_id,
        runner_ready_at=prior.runner_ready_at,
        executor_config_digest=prior.executor_config_digest,
        disk_cleanup_binding=prior.disk_cleanup_binding,
        cleanup_result=cleanup_result,
        prebind_cleanup_result=prebind_cleanup_result,
        core_terminal_fenced_at=prior.core_terminal_fenced_at,
        cleanup_attempts=(
            prior.cleanup_attempts if cleanup_attempts is None else cleanup_attempts
        ),
        max_cleanup_attempts=prior.max_cleanup_attempts,
        cleanup_timeout_seconds=prior.cleanup_timeout_seconds,
        occurred_at=_timestamp(now),
        error_code=error_code,
        previous_event_digest=prior.event_digest,
    )
    candidate = [*events, event]
    FileGcpWatchdog._validate_transitions(candidate)
    raw = canonical_json_bytes(_model_value(event)) + b"\n"
    descriptor: int | None = None
    try:
        descriptor = root.open_child(
            _event_name(str(prior.binding.controller_id)),
            os.O_WRONLY | os.O_APPEND,
        )
        metadata = _validated_child(
            root, _event_name(str(prior.binding.controller_id)), descriptor
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o022
            or metadata.st_uid != os.getuid()
        ):
            raise ValueError("unsafe worker journal")
        _validated_child(
            root, _event_name(str(prior.binding.controller_id)), descriptor
        )
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        final = _validated_child(
            root, _event_name(str(prior.binding.controller_id)), descriptor
        )
        if final.st_size != metadata.st_size + len(raw):
            raise ValueError("worker journal append changed")
        root.fsync()
        return event
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_fixture(
    root: SafeDirFD,
    controller_id: str,
    *,
    prebind: bool,
) -> GcpWatchdogCleanupResult | GcpWatchdogPrebindCleanupResult | None:
    """Read one bounded, canonical local fake response or fail closed."""

    descriptor: int | None = None
    try:
        descriptor = root.open_child(
            _fixture_name(controller_id), os.O_RDONLY | os.O_NONBLOCK
        )
        metadata = _validated_child(root, _fixture_name(controller_id), descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > GCP_FILE_WATCHDOG_MAX_RESULT_BYTES
            or metadata.st_mode & 0o022
            or metadata.st_uid != os.getuid()
        ):
            return None
        raw = _read_all(descriptor, GCP_FILE_WATCHDOG_MAX_RESULT_BYTES)
        final = _validated_child(root, _fixture_name(controller_id), descriptor)
        if (
            final.st_ino != metadata.st_ino
            or final.st_size != metadata.st_size
            or final.st_size != len(raw)
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
    except (FileNotFoundError, OSError, SafeDirFSError, ValidationError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _worker_now(args: argparse.Namespace) -> datetime:
    if args.clock_epoch is not None:
        return datetime.fromtimestamp(args.clock_epoch, UTC)
    return datetime.now(UTC)


def _settle_due_once(
    root: SafeDirFD,
    controller_id: str,
    *,
    now: datetime,
    core_root: SafeDirFD | None,
) -> GcpFileWatchdogEvent:
    """Advance exactly one durable local cleanup task when it becomes due.

        A descriptor-bound fake backend performs actual local exact instance and
        disk cleanup when its separately inherited core root is available.  The
        fixture branch remains only for legacy offline test doubles which cannot
        mint a future mutation guard.  Neither branch imports an SDK or accepts a
    callable/provider object from the parent.
    """

    with _exclusive(root):
        events = _read_events(root, controller_id)
        latest = events[-1]
        if latest.state in {"CLEANUP_CONFIRMED", "CLEANUP_NOT_REQUIRED", "ORPHANED"}:
            return latest
        if latest.core_terminal_fenced_at is not None:
            return latest
        if (
            latest.provider_runtime_deadline_at is None
            or latest.watchdog_cleanup_deadline_at is None
        ):
            return latest
        runtime_deadline = _parse_timestamp(latest.provider_runtime_deadline_at)
        cleanup_deadline = _parse_timestamp(latest.watchdog_cleanup_deadline_at)
        if now >= cleanup_deadline:
            return _append_event(
                root,
                events,
                state="ORPHANED",
                now=now,
                error_code="WATCHDOG_CLEANUP_HORIZON_EXPIRED",
            )
        prebind = latest.disk_cleanup_binding is None
        intent_state: GcpFileWatchdogState = (
            "PREBIND_CLEANUP_INTENT" if prebind else "CLEANUP_INTENT"
        )
        retry_state: GcpFileWatchdogState = (
            "PREBIND_RETRY_PENDING" if prebind else "RETRY_PENDING"
        )
        if latest.state in {"PREBIND_CLEANUP_INTENT", "CLEANUP_INTENT"}:
            lease_until = _parse_timestamp(latest.occurred_at) + timedelta(
                seconds=latest.cleanup_timeout_seconds
            )
            if now < lease_until:
                return latest
            if latest.cleanup_attempts >= latest.max_cleanup_attempts:
                return _append_event(
                    root,
                    events,
                    state="ORPHANED",
                    now=now,
                    error_code="WATCHDOG_ATTEMPTS_EXHAUSTED",
                )
            latest = _append_event(
                root,
                events,
                state=retry_state,
                now=now,
                error_code="WATCHDOG_INTENT_LEASE_EXPIRED",
            )
            events = (*events, latest)
        allowed = (
            {"RUNNER_READY", "PREBIND_RETRY_PENDING"}
            if prebind
            else {"DISK_BOUND", "RETRY_PENDING"}
        )
        if latest.state not in allowed or now < runtime_deadline:
            return latest
        if latest.cleanup_attempts >= latest.max_cleanup_attempts:
            return _append_event(
                root,
                events,
                state="ORPHANED",
                now=now,
                error_code="WATCHDOG_ATTEMPTS_EXHAUSTED",
            )
        intent = _append_event(
            root,
            events,
            state=intent_state,
            now=now,
            cleanup_attempts=latest.cleanup_attempts + 1,
        )

    if core_root is not None:
        try:
            result = worker_cleanup(
                root,
                core_root,
                intent,
                prebind=prebind,
            )
        except GcpFileWatchdogBackendError:
            result = None
    else:
        result = _read_fixture(root, controller_id, prebind=prebind)
    with _exclusive(root):
        events = _read_events(root, controller_id)
        latest = events[-1]
        if latest.event_digest != intent.event_digest:
            return latest
        valid_bound = isinstance(result, GcpWatchdogCleanupResult) and (
            FileGcpWatchdog._cleanup_result_matches(result, intent)
        )
        valid_prebind = isinstance(result, GcpWatchdogPrebindCleanupResult) and (
            FileGcpWatchdog._prebind_cleanup_result_matches(result, intent)
        )
        if valid_bound and isinstance(result, GcpWatchdogCleanupResult):
            return _append_event(
                root,
                events,
                state="CLEANUP_CONFIRMED",
                now=now,
                cleanup_result=result,
            )
        if valid_prebind and isinstance(result, GcpWatchdogPrebindCleanupResult):
            return _append_event(
                root,
                events,
                state="ORPHANED",
                now=now,
                prebind_cleanup_result=result,
                error_code="WATCHDOG_DISK_BINDING_UNRESOLVED",
            )
        return _append_event(
            root,
            events,
            state=(
                "ORPHANED"
                if latest.cleanup_attempts >= latest.max_cleanup_attempts
                else retry_state
            ),
            now=now,
            error_code=(
                "WATCHDOG_PREBIND_CLEANUP_UNCONFIRMED"
                if prebind
                else "WATCHDOG_EXACT_CLEANUP_UNCONFIRMED"
            ),
        )


def _runner(args: argparse.Namespace) -> int:
    root = SafeDirFD.from_inherited_fd(args.root_fd)
    core_root: SafeDirFD | None = None
    try:
        if args.core_root_fd is not None:
            core_root = SafeDirFD.from_inherited_fd(args.core_root_fd)
        deadline = time.monotonic() + 2.0
        while True:
            try:
                event = _read_latest_event(root, args.controller_id)
            except (OSError, SafeDirFSError, ValidationError, ValueError):
                return 2
            if (
                event.state
                in {
                    "RUNNER_READY",
                    "DISK_BOUND",
                    "PREBIND_CLEANUP_INTENT",
                    "PREBIND_RETRY_PENDING",
                    "CLEANUP_INTENT",
                    "RETRY_PENDING",
                }
                and event.runner_process_id == os.getpid()
                and event.executor_config_digest is not None
            ):
                if core_root is not None:
                    try:
                        # A concrete backend must validate the fsync-backed
                        # spec, exact core lease/anchor, and both inherited
                        # root identities *before* readiness is acknowledged.
                        worker_validate_ready(root, core_root, event)
                    except GcpFileWatchdogBackendError:
                        return 2
                _write_all(args.ready_fd, b"A")
                break
            if time.monotonic() >= deadline:
                return 3
            time.sleep(0.01)
        # This worker remains independent after controller death.  It can
        # advance only the durable fake-result reconciliation state above; it
        # never imports an SDK or accepts an in-memory provider object.
        while True:
            try:
                event = _settle_due_once(
                    root,
                    args.controller_id,
                    now=_worker_now(args),
                    core_root=core_root,
                )
            except (
                GcpFileWatchdogBackendError,
                OSError,
                SafeDirFSError,
                ValidationError,
                ValueError,
            ):
                return 4
            if event.state in {
                "CLEANUP_CONFIRMED",
                "CLEANUP_NOT_REQUIRED",
                "ORPHANED",
            }:
                return 0
            time.sleep(max(0.01, args.poll_ms / 1000.0))
    finally:
        if core_root is not None:
            core_root.close()
        root.close()


def _cleanup(args: argparse.Namespace) -> int:
    root = SafeDirFD.from_inherited_fd(args.root_fd)
    core_root: SafeDirFD | None = None
    try:
        if args.core_root_fd is not None:
            core_root = SafeDirFD.from_inherited_fd(args.core_root_fd)
        if args.test_hang_milliseconds:
            # Private injected-fake coverage for the parent watchdog's hard
            # process-group timeout.  It is not durable configuration and
            # cannot carry provider, approval, or payload material.
            time.sleep(args.test_hang_milliseconds / 1000.0)
        try:
            event = _read_latest_event(root, args.controller_id)
            if event.event_digest != args.intent_digest or event.state not in {
                "PREBIND_CLEANUP_INTENT",
                "CLEANUP_INTENT",
            }:
                return 2
            if bool(args.prebind) != (event.state == "PREBIND_CLEANUP_INTENT"):
                return 2
        except (OSError, SafeDirFSError, ValidationError, ValueError):
            return 2
        result: GcpWatchdogCleanupResult | GcpWatchdogPrebindCleanupResult | None
        if core_root is not None:
            try:
                result = worker_cleanup(
                    root,
                    core_root,
                    event,
                    prebind=bool(args.prebind),
                )
            except GcpFileWatchdogBackendError:
                return 4
        else:
            # A fixture is a legacy local injected-fake response, never a
            # provider credential or execution payload. It cannot mint the
            # future mutation guard because it has no reconstructible spec.
            result = _read_fixture(
                root,
                args.controller_id,
                prebind=bool(args.prebind),
            )
        if result is None:
            return 4
        raw = canonical_json_bytes(_model_value(result))
        _write_all(args.result_fd, raw)
        return 0
    finally:
        if core_root is not None:
            core_root.close()
        root.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inferdrome-gcp-watchdog-worker")
    sub = parser.add_subparsers(dest="mode", required=True)
    runner = sub.add_parser("runner")
    runner.add_argument("--root-fd", type=int, required=True)
    runner.add_argument("--controller-id", required=True)
    runner.add_argument("--ready-fd", type=int, required=True)
    runner.add_argument("--poll-ms", type=int, required=True)
    runner.add_argument("--core-root-fd", type=int)
    # A caller may supply a fixed non-secret clock only for injected local
    # fake tests.  Production omits it and the worker reads wall time itself.
    runner.add_argument("--clock-epoch", type=float)
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--root-fd", type=int, required=True)
    cleanup.add_argument("--controller-id", required=True)
    cleanup.add_argument("--intent-digest", required=True)
    cleanup.add_argument("--result-fd", type=int, required=True)
    cleanup.add_argument("--prebind", type=int, choices=(0, 1), required=True)
    cleanup.add_argument("--core-root-fd", type=int)
    cleanup.add_argument("--test-hang-milliseconds", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.mode == "cleanup" and not 0 <= args.test_hang_milliseconds <= 60_000:
        return 2
    if args.mode == "runner":
        return _runner(args)
    return _cleanup(args)


if __name__ == "__main__":
    sys.exit(main())
