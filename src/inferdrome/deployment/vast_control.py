"""Bounded operator-side Vast control and durable exact-instance cleanup.

A separately hosted guard and provider transport must be supplied explicitly.
Neither a readiness record nor these local journals attest provider behavior.
Create is never retried. Unknown create identity requires operator reconciliation.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import FrameType
from typing import Annotated, Any, Literal, Protocol

from pydantic import Field, model_validator

from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.deployment.vast_process import ProviderId, parse_deadline
from inferdrome.errors import InferdromeError
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.contracts import Digest, ExecutionModel

CONFIRMED = "OPERATOR_REPORTED_DESTROY_AND_ABSENCE_NOT_INDEPENDENTLY_ATTESTED"
UNCONFIRMED = "CLEANUP_UNCONFIRMED"
UNKNOWN_CREATE = "CREATE_OUTCOME_UNRESOLVED"
_MAX_RECORD_BYTES = 131_072


class ControlFailure(InferdromeError):
    """A sanitized local control failure, without provider diagnostics."""


class ControlIntent(ExecutionModel):
    launch_request_sha256: Digest
    execution_deadline_utc: str
    cleanup_deadline_utc: str
    operation_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 20.0
    poll_seconds: Annotated[float, Field(gt=0, le=10)] = 1.0

    @model_validator(mode="after")
    def _deadlines(self) -> ControlIntent:
        reserve = (
            parse_deadline(self.cleanup_deadline_utc)
            - parse_deadline(self.execution_deadline_utc)
        ).total_seconds()
        if not 0 < reserve <= 600:
            raise ValueError("cleanup reserve must be positive and at most 600 seconds")
        return self

    @property
    def intent_sha256(self) -> str:
        return sha256_digest(canonical_json_bytes(self.model_dump(mode="json")))


class CreateResult(ExecutionModel):
    new_contract: ProviderId


class DestroyResult(ExecutionModel):
    instance_id: ProviderId
    acknowledged: bool


class AbsenceResult(ExecutionModel):
    query_instance_id: ProviderId
    succeeded: bool
    matching_instance_ids: Annotated[tuple[ProviderId, ...], Field(max_length=64)]
    pagination_exhausted: bool
    persistent_volume_ids: Annotated[tuple[ProviderId, ...], Field(max_length=64)]


class CleanupConfirmation(ExecutionModel):
    instance_id: ProviderId
    status: Literal["OPERATOR_REPORTED_DESTROY_AND_ABSENCE_NOT_INDEPENDENTLY_ATTESTED"]
    destroy: DestroyResult
    observation: AbsenceResult

    @model_validator(mode="after")
    def _complete(self) -> CleanupConfirmation:
        if not (
            self.instance_id
            == self.destroy.instance_id
            == self.observation.query_instance_id
            and self.destroy.acknowledged
            and self.observation.succeeded
            and self.observation.pagination_exhausted
            and not self.observation.matching_instance_ids
            and not self.observation.persistent_volume_ids
        ):
            raise ValueError(
                "cleanup confirmation must retain complete exact observations"
            )
        return self


class _CleanupWindow(ExecutionModel):
    intent_sha256: Digest
    started_monotonic: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    deadline_monotonic: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    deadline_unix: Annotated[float, Field(gt=0, allow_inf_nan=False)]


class GuardReady(ExecutionModel):
    intent_sha256: Digest
    process_id: Annotated[int, Field(gt=0)]
    journal_device: Annotated[int, Field(ge=0)]
    journal_inode: Annotated[int, Field(gt=0)]


class ControlOutcome(ExecutionModel):
    work_status: Literal[
        "RETRIEVED", "FAILED", "NOT_CREATED", "CREATE_OUTCOME_UNRESOLVED"
    ]
    instance_id: ProviderId | None
    cleanup_status: str


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class Provider(Protocol):
    """A reviewed adapter binds its exact request to launch_request_sha256.

    Adapters must bound network/pagination and clean up their owned subprocesses.
    The control boundary also enforces a POSIX main-thread alarm and rejects late
    returns. Concrete integrations live in vast_provider and vast_guard.
    """

    def create(self, intent: ControlIntent, *, seconds: float) -> CreateResult: ...

    def destroy(self, instance_id: int, *, seconds: float) -> DestroyResult: ...

    def observe_absence(self, instance_id: int, *, seconds: float) -> AbsenceResult: ...


class Workflow(Protocol):
    """Each phase must enforce its entire supplied budget and propagate failure."""

    @property
    def launch_request_sha256(self) -> str: ...

    @property
    def execution_deadline_utc(self) -> str: ...

    @property
    def cleanup_deadline_utc(self) -> str: ...

    def readiness(self, instance_id: int, *, seconds: float) -> None: ...

    def stage(self, instance_id: int, *, seconds: float) -> None: ...

    def approve(self, instance_id: int, *, seconds: float) -> None: ...

    def run(self, instance_id: int, *, seconds: float) -> None: ...

    def retrieve(self, instance_id: int, *, seconds: float) -> None: ...


class IndependentGuard(Protocol):
    """Host DeadlineGuard outside the work process; require its readiness ACK.

    is_alive must check that exact worker identity using a locally held handle,
    not merely trust a saved PID. Same-process synchronous guards are rejected.
    """

    def arm(
        self, intent: ControlIntent, journal: ControlJournal, *, seconds: float
    ) -> GuardReady: ...

    def is_alive(self, ready: GuardReady) -> bool: ...


def _validated[Model: ExecutionModel](model: type[Model], value: object) -> Model:
    if not isinstance(value, model):
        raise ControlFailure("VAST_CONTROL_RESPONSE_INVALID")
    return model.model_validate(value.model_dump(mode="python"), strict=True)


def _require_alarm_support() -> None:
    if threading.current_thread() is not threading.main_thread() or not all(
        hasattr(signal, name) for name in ("setitimer", "getitimer", "ITIMER_REAL")
    ):
        raise ControlFailure("VAST_CONTROL_MAIN_THREAD_ALARM_REQUIRED")


def _bounded_call[Result](call: Callable[[], Result], seconds: float) -> Result:
    """Interrupt blocking callbacks without renewing an enclosing alarm.

    This is a synchronous POSIX boundary, not a subprocess sandbox. Adapters
    remain responsible for children they own and must not suppress this alarm.
    """
    _require_alarm_support()
    if not math.isfinite(seconds) or seconds <= 0:
        raise ControlFailure("VAST_CONTROL_DEADLINE")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_number: int, _frame: FrameType | None) -> None:
        raise ControlFailure("VAST_CONTROL_CALL_TIMEOUT")

    limit = min(seconds, previous_timer[0]) if previous_timer[0] > 0 else seconds
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, limit)
    try:
        if time.monotonic() - started >= limit:
            raise ControlFailure("VAST_CONTROL_CALL_TIMEOUT")
        result = call()
        if time.monotonic() - started >= limit:
            raise ControlFailure("VAST_CONTROL_CALL_TIMEOUT")
        return result
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, previous_timer[0] - (time.monotonic() - started)),
                previous_timer[1],
            )


class _Budget:
    def __init__(self, deadline: str, clock: Clock) -> None:
        self.deadline = parse_deadline(deadline)
        self.clock = clock
        self.monotonic_deadline = clock.monotonic() + max(
            0.0, (self.deadline - clock.now()).total_seconds()
        )

    def remaining(self) -> float:
        value = min(
            (self.deadline - self.clock.now()).total_seconds(),
            self.monotonic_deadline - self.clock.monotonic(),
        )
        if not math.isfinite(value) or value <= 0:
            raise ControlFailure("VAST_CONTROL_DEADLINE")
        return value


class ControlJournal:
    """Create-only, fsynced records held under a private directory descriptor."""

    def __init__(self, directory: Path) -> None:
        self._initialize_root(SafeDirFD.open(directory))

    @classmethod
    def from_inherited_fd(cls, descriptor: int) -> ControlJournal:
        """Reopen the lock in a worker; never share a fork-inherited flock."""
        journal = cls.__new__(cls)
        journal._initialize_root(SafeDirFD.from_inherited_fd(descriptor))
        return journal

    def _initialize_root(self, root: SafeDirFD) -> None:
        self.root = root
        if os.fstat(self.root.fd).st_mode & 0o777 != 0o700:
            self.root.close()
            raise ControlFailure("VAST_CONTROL_JOURNAL_NOT_PRIVATE")
        try:
            self._lock_fd = self.root.open_child("lock", os.O_RDWR | os.O_CREAT)
        except BaseException:
            self.root.close()
            raise
        self._thread_lock = threading.RLock()

    def close(self) -> None:
        os.close(self._lock_fd)
        self.root.close()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self.root.validated_regular_child("lock", descriptor=self._lock_fd)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                self.root.validated_regular_child("lock", descriptor=self._lock_fd)
                yield
            finally:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def _read(self, name: str) -> dict[str, Any] | None:
        try:
            descriptor = self.root.open_child(name, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(descriptor)
            if before.st_size > _MAX_RECORD_BYTES:
                raise ControlFailure("VAST_CONTROL_JOURNAL_INVALID")
            content = os.read(descriptor, _MAX_RECORD_BYTES + 1)
            after = self.root.validated_regular_child(name, descriptor=descriptor)
            if (
                len(content) != before.st_size
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                raise ControlFailure("VAST_CONTROL_JOURNAL_CHANGED")
            value = json.loads(content)
            if not isinstance(value, dict) or canonical_json_bytes(value) != content:
                raise ControlFailure("VAST_CONTROL_JOURNAL_INVALID")
            return value
        finally:
            os.close(descriptor)

    def _write(self, name: str, value: dict[str, Any]) -> None:
        content = canonical_json_bytes(value)
        if len(content) > _MAX_RECORD_BYTES:
            raise ControlFailure("VAST_CONTROL_JOURNAL_INVALID")
        temporary = f"temporary-{uuid.uuid4().hex}"
        descriptor = self.root.open_child(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                self.root.validated_regular_child(temporary, descriptor=stream.fileno())
            # Hard-link publication is atomic and never replaces an existing record.
            os.link(
                temporary,
                name,
                src_dir_fd=self.root.fd,
                dst_dir_fd=self.root.fd,
                follow_symlinks=False,
            )
        finally:
            os.unlink(temporary, dir_fd=self.root.fd)
        published = self.root.validated_regular_child(name)
        try:
            self.root.fsync()
        except BaseException:
            # Do not expose a failed durable-ID publication to the guard after
            # releasing the journal lock. A filesystem that also refuses this
            # rollback is an unresolved operator filesystem failure.
            with suppress(OSError):
                self.root.unlink_child(name, expected=published)
                self.root.fsync()
            raise

    def initialize(self, intent: ControlIntent) -> None:
        with self._locked():
            value = intent.model_dump(mode="json")
            existing = self._read("intent.json")
            if existing is None:
                self._write("intent.json", value)
            elif existing != value:
                raise ControlFailure("VAST_CONTROL_INTENT_MISMATCH")

    def require_intent(self, intent: ControlIntent) -> None:
        with self._locked():
            if self._read("intent.json") != intent.model_dump(mode="json"):
                raise ControlFailure("VAST_CONTROL_INTENT_MISMATCH")

    def has(self, name: str) -> bool:
        with self._locked():
            return self._read(name) is not None

    def record(self, name: str, value: dict[str, Any]) -> None:
        with self._locked():
            self._write(name, value)

    def record_once(self, name: str, value: dict[str, Any]) -> None:
        with self._locked():
            if self._read(name) is None:
                self._write(name, value)

    def start_create(self, intent: ControlIntent) -> None:
        with self._locked():
            if self._read("intent.json") != intent.model_dump(mode="json"):
                raise ControlFailure("VAST_CONTROL_INTENT_MISMATCH")
            self._write("create-start.json", {"intent_sha256": intent.intent_sha256})

    def retain_created(self, result: CreateResult) -> None:
        with self._locked():
            if self._read("create-start.json") is None:
                raise ControlFailure("VAST_CONTROL_CREATE_NOT_STARTED")
            self._write("created.json", result.model_dump(mode="json"))

    def exact_instance_id(self) -> int | None:
        with self._locked():
            value = self._read("created.json")
            if value is None:
                return None
            if self._read("create-start.json") is None:
                raise ControlFailure("VAST_CONTROL_CREATE_NOT_STARTED")
            return CreateResult.model_validate_json(
                canonical_json_bytes(value)
            ).new_contract

    def request_cleanup(self) -> None:
        self.record_once("cleanup-requested.json", {"requested": True})

    def bound_execution(
        self, intent: ControlIntent, clock: Clock, budget: _Budget
    ) -> None:
        """Persist both clocks before arming; a later worker cannot renew them."""
        with self._locked():
            value = self._read("execution-window.json")
            if value is None:
                window = _CleanupWindow(
                    intent_sha256=intent.intent_sha256,
                    started_monotonic=clock.monotonic(),
                    deadline_monotonic=budget.monotonic_deadline,
                    deadline_unix=budget.deadline.timestamp(),
                )
                self._write("execution-window.json", window.model_dump(mode="json"))
            else:
                window = _CleanupWindow.model_validate_json(canonical_json_bytes(value))
            if (
                window.intent_sha256 != intent.intent_sha256
                or window.deadline_monotonic < window.started_monotonic
                or window.deadline_monotonic - window.started_monotonic > 7200
                or window.deadline_unix
                != parse_deadline(intent.execution_deadline_utc).timestamp()
                or clock.monotonic() < window.started_monotonic
            ):
                raise ControlFailure("VAST_CONTROL_EXECUTION_WINDOW_INVALID")
            budget.monotonic_deadline = min(
                budget.monotonic_deadline, window.deadline_monotonic
            )

    def bound_cleanup(
        self, intent: ControlIntent, clock: Clock, budget: _Budget
    ) -> None:
        """Keep one settlement window shared by controller and same-host guard."""
        reserve = (
            parse_deadline(intent.cleanup_deadline_utc)
            - parse_deadline(intent.execution_deadline_utc)
        ).total_seconds()
        with self._locked():
            value = self._read("cleanup-window.json")
            if value is None:
                started = clock.monotonic()
                window = _CleanupWindow(
                    intent_sha256=intent.intent_sha256,
                    started_monotonic=started,
                    deadline_monotonic=min(
                        budget.monotonic_deadline, started + reserve
                    ),
                    deadline_unix=min(
                        budget.deadline.timestamp(), clock.now().timestamp() + reserve
                    ),
                )
                self._write("cleanup-window.json", window.model_dump(mode="json"))
            else:
                window = _CleanupWindow.model_validate_json(canonical_json_bytes(value))
            if (
                window.intent_sha256 != intent.intent_sha256
                or window.deadline_monotonic - window.started_monotonic > reserve
                or window.deadline_unix
                > parse_deadline(intent.cleanup_deadline_utc).timestamp()
                or clock.monotonic() < window.started_monotonic
            ):
                raise ControlFailure("VAST_CONTROL_CLEANUP_WINDOW_INVALID")
            budget.monotonic_deadline = min(
                budget.monotonic_deadline, window.deadline_monotonic
            )
            budget.deadline = min(
                budget.deadline, datetime.fromtimestamp(window.deadline_unix, UTC)
            )

    def cleanup_status(self) -> str | None:
        if self.has("unexpected-volumes.json"):
            return UNCONFIRMED
        instance_id = self.exact_instance_id()
        with self._locked():
            for name, status in (
                ("cleanup-confirmed.json", CONFIRMED),
                ("cleanup-unconfirmed.json", UNCONFIRMED),
            ):
                record = self._read(name)
                if record is not None:
                    if status == CONFIRMED:
                        try:
                            confirmed = CleanupConfirmation.model_validate_json(
                                canonical_json_bytes(record)
                            )
                            valid = confirmed.instance_id == instance_id
                        except ValueError:
                            valid = False
                    else:
                        valid = record == {"instance_id": instance_id, "status": status}
                    if instance_id is None or not valid:
                        raise ControlFailure("VAST_CONTROL_CLEANUP_RECORD_INVALID")
                    return status
        return None


def _settle(
    intent: ControlIntent,
    journal: ControlJournal,
    provider: Provider,
    clock: Clock,
    budget: _Budget,
) -> str:
    journal.require_intent(intent)
    instance_id = journal.exact_instance_id()
    if instance_id is None:
        return UNKNOWN_CREATE if journal.has("create-start.json") else "NOT_CREATED"
    if journal.cleanup_status() == CONFIRMED:
        return CONFIRMED
    journal.bound_cleanup(intent, clock, budget)
    acknowledged = False
    destroy_result: DestroyResult | None = None
    while True:
        try:
            seconds = min(intent.operation_timeout_seconds, budget.remaining())
            if not acknowledged:
                result = _validated(
                    DestroyResult,
                    _bounded_call(
                        partial(provider.destroy, instance_id, seconds=seconds), seconds
                    ),
                )
                budget.remaining()
                acknowledged = result.instance_id == instance_id and result.acknowledged
                if acknowledged:
                    destroy_result = result
            seconds = min(intent.operation_timeout_seconds, budget.remaining())
            observed = _validated(
                AbsenceResult,
                _bounded_call(
                    partial(provider.observe_absence, instance_id, seconds=seconds),
                    seconds,
                ),
            )
            budget.remaining()
            if (
                observed.query_instance_id == instance_id
                and observed.persistent_volume_ids
            ):
                journal.record_once(
                    "unexpected-volumes.json",
                    {
                        "instance_id": instance_id,
                        "persistent_volume_ids": list(observed.persistent_volume_ids),
                        "status": UNCONFIRMED,
                    },
                )
            if (
                acknowledged
                and observed.query_instance_id == instance_id
                and observed.succeeded
                and observed.pagination_exhausted
                and not observed.matching_instance_ids
                and not observed.persistent_volume_ids
                and not journal.has("unexpected-volumes.json")
            ):
                assert destroy_result is not None
                confirmation = CleanupConfirmation(
                    instance_id=instance_id,
                    status="OPERATOR_REPORTED_DESTROY_AND_ABSENCE_NOT_INDEPENDENTLY_ATTESTED",
                    destroy=destroy_result,
                    observation=observed,
                )
                journal.record_once(
                    "cleanup-confirmed.json", confirmation.model_dump(mode="json")
                )
                return CONFIRMED
        except Exception:
            # A failed/malformed/late response is never affirmative evidence.
            pass
        try:
            clock.sleep(min(intent.poll_seconds, budget.remaining()))
        except ControlFailure:
            journal.record_once(
                "cleanup-unconfirmed.json",
                {
                    "instance_id": instance_id,
                    "status": UNCONFIRMED,
                },
            )
            return UNCONFIRMED


class DeadlineGuard:
    """Deadline worker service, hosted independently by vast_guard.DetachedGuard."""

    def __init__(
        self,
        intent: ControlIntent,
        journal: ControlJournal,
        provider: Provider,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.intent = _validated(ControlIntent, intent)
        self.journal = journal
        self.provider = provider
        self.clock = clock or SystemClock()
        self.execution = _Budget(intent.execution_deadline_utc, self.clock)
        self.cleanup = _Budget(intent.cleanup_deadline_utc, self.clock)
        self.journal.bound_execution(self.intent, self.clock, self.execution)
        reserve = (
            parse_deadline(intent.cleanup_deadline_utc)
            - parse_deadline(intent.execution_deadline_utc)
        ).total_seconds()
        self.cleanup.monotonic_deadline = min(
            self.cleanup.monotonic_deadline,
            self.execution.monotonic_deadline + reserve,
        )

    def readiness(self) -> GuardReady:
        _require_alarm_support()
        self.journal.require_intent(self.intent)
        self.execution.remaining()
        return GuardReady(
            intent_sha256=self.intent.intent_sha256,
            process_id=os.getpid(),
            journal_device=self.journal.root.device,
            journal_inode=self.journal.root.inode,
        )

    def run(self) -> str:
        _require_alarm_support()
        self.journal.require_intent(self.intent)
        while True:
            try:
                execution_remaining = self.execution.remaining()
            except ControlFailure:
                execution_remaining = 0.0
            if self.journal.has("cleanup-requested.json") or execution_remaining <= 0:
                if self.journal.exact_instance_id() is not None:
                    return _settle(
                        self.intent,
                        self.journal,
                        self.provider,
                        self.clock,
                        self.cleanup,
                    )
                if not self.journal.has("create-start.json"):
                    return "NOT_CREATED"
                # A late create response can still supply an exact ID within cleanup.
            try:
                self.clock.sleep(
                    min(self.intent.poll_seconds, self.cleanup.remaining())
                )
            except ControlFailure:
                self.journal.record_once(
                    "create-unresolved.json", {"status": UNKNOWN_CREATE}
                )
                return UNKNOWN_CREATE


def execute_control(
    intent: ControlIntent,
    journal: ControlJournal,
    *,
    approved_intent_sha256: str,
    provider: Provider,
    workflow: Workflow,
    guard: IndependentGuard,
    clock: Clock | None = None,
) -> ControlOutcome:
    """Execute one approved launch and settle its durably retained exact ID."""
    _require_alarm_support()
    intent = _validated(ControlIntent, intent)
    if approved_intent_sha256 != intent.intent_sha256:
        raise ControlFailure("VAST_CONTROL_APPROVAL_MISMATCH")
    selected_clock = clock or SystemClock()
    execution = _Budget(intent.execution_deadline_utc, selected_clock)
    cleanup = _Budget(intent.cleanup_deadline_utc, selected_clock)
    if execution.remaining() > 7200:
        raise ControlFailure("VAST_CONTROL_EXECUTION_WINDOW_TOO_LONG")
    matched_workflow = _bounded_call(
        lambda: (
            workflow.launch_request_sha256 == intent.launch_request_sha256
            and workflow.execution_deadline_utc == intent.execution_deadline_utc
            and workflow.cleanup_deadline_utc == intent.cleanup_deadline_utc
        ),
        min(intent.operation_timeout_seconds, execution.remaining()),
    )
    if not matched_workflow:
        raise ControlFailure("VAST_CONTROL_WORKFLOW_INTENT_MISMATCH")
    journal.initialize(intent)
    journal.bound_execution(intent, selected_clock, execution)
    cleanup.monotonic_deadline = min(
        cleanup.monotonic_deadline,
        execution.monotonic_deadline
        + (
            parse_deadline(intent.cleanup_deadline_utc)
            - parse_deadline(intent.execution_deadline_utc)
        ).total_seconds(),
    )
    if journal.has("create-start.json"):
        raise ControlFailure("VAST_CONTROL_CREATE_ALREADY_ATTEMPTED")
    seconds = min(intent.operation_timeout_seconds, execution.remaining())
    ready = _validated(
        GuardReady,
        _bounded_call(lambda: guard.arm(intent, journal, seconds=seconds), seconds),
    )
    execution.remaining()
    if (
        ready.intent_sha256 != intent.intent_sha256
        or ready.process_id == os.getpid()
        or ready.journal_device != journal.root.device
        or ready.journal_inode != journal.root.inode
        or _bounded_call(
            lambda: guard.is_alive(ready),
            min(intent.operation_timeout_seconds, execution.remaining()),
        )
        is not True
    ):
        raise ControlFailure("VAST_CONTROL_INDEPENDENT_GUARD_REQUIRED")
    journal.record("guard-ready.json", ready.model_dump(mode="json"))
    work_status: Literal[
        "RETRIEVED", "FAILED", "NOT_CREATED", "CREATE_OUTCOME_UNRESOLVED"
    ] = "FAILED"
    instance_id: int | None = None
    try:
        journal.start_create(intent)
        execution.remaining()
        if (
            _bounded_call(
                lambda: guard.is_alive(ready),
                min(intent.operation_timeout_seconds, execution.remaining()),
            )
            is not True
        ):
            raise ControlFailure("VAST_CONTROL_GUARD_LOST")
        seconds = min(intent.operation_timeout_seconds, execution.remaining())
        created = _validated(
            CreateResult,
            _bounded_call(lambda: provider.create(intent, seconds=seconds), seconds),
        )
        journal.retain_created(created)
        instance_id = journal.exact_instance_id()
        if instance_id is None:
            raise ControlFailure("VAST_CONTROL_CREATE_NOT_RETAINED")
        execution.remaining()
        for phase in (
            workflow.readiness,
            workflow.stage,
            workflow.approve,
            workflow.run,
            workflow.retrieve,
        ):
            if (
                _bounded_call(
                    lambda: guard.is_alive(ready),
                    min(intent.operation_timeout_seconds, execution.remaining()),
                )
                is not True
            ):
                raise ControlFailure("VAST_CONTROL_GUARD_LOST")
            seconds = execution.remaining()
            _bounded_call(partial(phase, instance_id, seconds=seconds), seconds)
            execution.remaining()
        work_status = "RETRIEVED"
    except Exception:
        if instance_id is None:
            journal.record_once("create-unresolved.json", {"status": UNKNOWN_CREATE})
            work_status = "CREATE_OUTCOME_UNRESOLVED"
    finally:
        journal.request_cleanup()
        cleanup_status = _settle(intent, journal, provider, selected_clock, cleanup)
    return ControlOutcome(
        work_status=work_status, instance_id=instance_id, cleanup_status=cleanup_status
    )
