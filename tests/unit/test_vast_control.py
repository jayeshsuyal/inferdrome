"""Synthetic operator control tests: no provider, process, SSH or GPU actions."""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from inferdrome.deployment.vast_control import (
    CONFIRMED,
    UNCONFIRMED,
    UNKNOWN_CREATE,
    AbsenceResult,
    ControlFailure,
    ControlIntent,
    ControlJournal,
    CreateResult,
    DeadlineGuard,
    DestroyResult,
    GuardReady,
    execute_control,
)


class FakeClock:
    def __init__(self) -> None:
        self.start = datetime(2026, 9, 13, tzinfo=UTC)
        self.elapsed = 0.0
        self.wall_offset = 0.0

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.elapsed + self.wall_offset)

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.elapsed += seconds


def intent_for(clock: FakeClock) -> ControlIntent:
    return ControlIntent(
        launch_request_sha256="sha256:" + "a" * 64,
        execution_deadline_utc=(clock.start + timedelta(seconds=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        cleanup_deadline_utc=(clock.start + timedelta(seconds=15)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        operation_timeout_seconds=2.0,
        poll_seconds=1.0,
    )


class FakeProvider:
    def __init__(self, clock: FakeClock, journal: ControlJournal) -> None:
        self.clock = clock
        self.journal = journal
        self.calls: list[tuple[str, int | None, float]] = []
        self.create_error = False
        self.create_value: Any = CreateResult(new_contract=123)
        self.create_delay = 0.0
        self.destroy_error = False
        self.destroy_value: Any = DestroyResult(instance_id=123, acknowledged=True)
        self.observe_value: Any = AbsenceResult(
            query_instance_id=123,
            succeeded=True,
            matching_instance_ids=(),
            pagination_exhausted=True,
            persistent_volume_ids=(),
        )
        self.observe_delay = 0.0

    def create(self, intent: ControlIntent, *, seconds: float) -> CreateResult:
        assert self.journal.has("guard-ready.json")
        assert self.journal.has("create-start.json")
        self.calls.append(("create", None, seconds))
        self.clock.elapsed += self.create_delay
        if self.create_error:
            raise TimeoutError("synthetic lost create response with SECRET")
        return self.create_value

    def destroy(self, instance_id: int, *, seconds: float) -> DestroyResult:
        assert self.journal.exact_instance_id() == instance_id
        self.calls.append(("destroy", instance_id, seconds))
        if self.destroy_error:
            self.clock.elapsed += seconds
            raise TimeoutError("synthetic lost destroy response")
        return self.destroy_value

    def observe_absence(self, instance_id: int, *, seconds: float) -> AbsenceResult:
        self.calls.append(("observe", instance_id, seconds))
        self.clock.elapsed += self.observe_delay
        if isinstance(self.observe_value, Exception):
            raise self.observe_value
        return self.observe_value


class FakeGuard:
    def __init__(self) -> None:
        self.alive = True
        self.same_process = False
        self.wrong_identity = False
        self.arm_calls = 0

    def arm(
        self, intent: ControlIntent, journal: ControlJournal, *, seconds: float
    ) -> GuardReady:
        assert seconds > 0 and not journal.has("create-start.json")
        self.arm_calls += 1
        return GuardReady(
            intent_sha256=intent.intent_sha256,
            process_id=os.getpid() if self.same_process else os.getpid() + 1,
            journal_device=journal.root.device,
            journal_inode=journal.root.inode + int(self.wrong_identity),
        )

    def is_alive(self, ready: GuardReady) -> bool:
        return self.alive


class FakeWorkflow:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        intent = intent_for(clock)
        self.launch_request_sha256 = intent.launch_request_sha256
        self.execution_deadline_utc = intent.execution_deadline_utc
        self.cleanup_deadline_utc = intent.cleanup_deadline_utc
        self.phases: list[tuple[str, int, float]] = []
        self.fail: str | None = None
        self.late: str | None = None
        self.rollback = False

    def _phase(self, name: str, instance_id: int, seconds: float) -> None:
        self.phases.append((name, instance_id, seconds))
        if self.fail == name:
            raise TimeoutError("synthetic phase failure")
        self.clock.elapsed += seconds + 0.1 if self.late == name else 0.2
        if self.rollback:
            self.clock.wall_offset -= 50.0

    def readiness(self, instance_id: int, *, seconds: float) -> None:
        self._phase("readiness", instance_id, seconds)

    def stage(self, instance_id: int, *, seconds: float) -> None:
        self._phase("stage", instance_id, seconds)

    def approve(self, instance_id: int, *, seconds: float) -> None:
        self._phase("approve", instance_id, seconds)

    def run(self, instance_id: int, *, seconds: float) -> None:
        self._phase("run", instance_id, seconds)

    def retrieve(self, instance_id: int, *, seconds: float) -> None:
        self._phase("retrieve", instance_id, seconds)


class Harness:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(mode=0o700)
        self.directory = directory
        self.clock = FakeClock()
        self.intent = intent_for(self.clock)
        self.journal = ControlJournal(directory)
        self.provider = FakeProvider(self.clock, self.journal)
        self.guard = FakeGuard()
        self.workflow = FakeWorkflow(self.clock)

    def execute(self) -> Any:
        return execute_control(
            self.intent,
            self.journal,
            approved_intent_sha256=self.intent.intent_sha256,
            provider=self.provider,
            workflow=self.workflow,
            guard=self.guard,
            clock=self.clock,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Any:
    value = Harness(tmp_path / "control")
    try:
        yield value
    finally:
        value.journal.close()


def test_happy_path_persists_exact_returned_id_then_retrieves_and_destroys(
    harness: Harness,
) -> None:
    result = harness.execute()
    assert result.work_status == "RETRIEVED" and result.cleanup_status == CONFIRMED
    assert result.instance_id == 123
    assert [call[0] for call in harness.provider.calls] == [
        "create",
        "destroy",
        "observe",
    ]
    assert [call[0] for call in harness.workflow.phases] == [
        "readiness",
        "stage",
        "approve",
        "run",
        "retrieve",
    ]
    budgets = [call[2] for call in harness.workflow.phases]
    assert budgets == sorted(budgets, reverse=True)
    assert all(call[1] == 123 for call in harness.workflow.phases)
    assert json.loads((harness.directory / "created.json").read_bytes()) == {
        "new_contract": 123
    }
    with pytest.raises(ControlFailure, match="ALREADY_ATTEMPTED"):
        harness.execute()
    assert sum(call[0] == "create" for call in harness.provider.calls) == 1


def test_wrong_approval_has_no_guard_or_provider_side_effect(harness: Harness) -> None:
    with pytest.raises(ControlFailure, match="APPROVAL_MISMATCH"):
        execute_control(
            harness.intent,
            harness.journal,
            approved_intent_sha256="sha256:" + "b" * 64,
            provider=harness.provider,
            workflow=harness.workflow,
            guard=harness.guard,
            clock=harness.clock,
        )
    assert harness.guard.arm_calls == 0 and harness.provider.calls == []
    assert not harness.journal.has("intent.json")


@pytest.mark.parametrize("mode", ["dead", "same_process", "wrong_identity"])
def test_independent_guard_is_required_before_one_create(
    harness: Harness, mode: str
) -> None:
    if mode == "dead":
        harness.guard.alive = False
    else:
        setattr(harness.guard, mode, True)
    with pytest.raises(ControlFailure, match="INDEPENDENT_GUARD_REQUIRED"):
        harness.execute()
    assert harness.provider.calls == [] and not harness.journal.has("create-start.json")


@pytest.mark.parametrize("mode", ["timeout", "malformed", "boolean_id"])
def test_unknown_create_never_retries_infers_or_destroys(
    harness: Harness, mode: str
) -> None:
    if mode == "timeout":
        harness.provider.create_error = True
    elif mode == "malformed":
        harness.provider.create_value = {"offer_id": 123}
    else:
        harness.provider.create_value = CreateResult.model_construct(new_contract=True)
    result = harness.execute()
    assert (
        result.work_status == UNKNOWN_CREATE and result.cleanup_status == UNKNOWN_CREATE
    )
    assert result.instance_id is None and harness.workflow.phases == []
    assert [call[0] for call in harness.provider.calls] == ["create"]
    assert harness.journal.has("create-unresolved.json")
    assert "SECRET" not in "".join(
        path.read_text() for path in harness.directory.iterdir()
    )


def test_exact_id_is_not_used_if_retention_failed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(result: CreateResult) -> None:
        raise OSError("synthetic failed persistence")

    monkeypatch.setattr(harness.journal, "retain_created", fail)
    result = harness.execute()
    assert result.work_status == UNKNOWN_CREATE and result.instance_id is None
    assert [call[0] for call in harness.provider.calls] == ["create"]


@pytest.mark.parametrize("phase", ["readiness", "stage", "approve", "run", "retrieve"])
def test_each_work_failure_still_settles_exact_id(harness: Harness, phase: str) -> None:
    harness.workflow.fail = phase
    result = harness.execute()
    assert result.work_status == "FAILED" and result.cleanup_status == CONFIRMED
    assert harness.workflow.phases[-1][0] == phase
    assert [call[0] for call in harness.provider.calls] == [
        "create",
        "destroy",
        "observe",
    ]


@pytest.mark.parametrize("phase", ["readiness", "retrieve"])
def test_late_success_does_not_extend_work_budget(harness: Harness, phase: str) -> None:
    harness.workflow.late = phase
    result = harness.execute()
    assert result.work_status == "FAILED" and result.cleanup_status == CONFIRMED
    assert harness.workflow.phases[-1][0] == phase


def test_late_create_retains_exact_id_only_for_cleanup(harness: Harness) -> None:
    harness.provider.create_delay = 11.0
    result = harness.execute()
    assert result.work_status == "FAILED" and result.cleanup_status == CONFIRMED
    assert result.instance_id == 123 and harness.workflow.phases == []


@pytest.mark.parametrize(
    "mode",
    [
        "partial",
        "nonempty",
        "wrong_id",
        "failed",
        "error",
        "malformed",
        "unacknowledged",
    ],
)
def test_invalid_absence_never_confirms_cleanup(harness: Harness, mode: str) -> None:
    value = harness.provider.observe_value.model_dump(mode="json")
    if mode == "partial":
        value["pagination_exhausted"] = False
    elif mode == "nonempty":
        value["matching_instance_ids"] = [123]
    elif mode == "wrong_id":
        value["query_instance_id"] = 124
    elif mode == "failed":
        value["succeeded"] = False
    elif mode == "error":
        harness.provider.observe_value = PermissionError("synthetic auth error")
    elif mode == "malformed":
        harness.provider.observe_value = {}
    else:
        harness.provider.destroy_value = DestroyResult(
            instance_id=123, acknowledged=False
        )
    if mode not in {"error", "malformed"}:
        harness.provider.observe_value = AbsenceResult.model_validate_json(
            json.dumps(value)
        )
    result = harness.execute()
    assert result.cleanup_status == UNCONFIRMED
    assert not harness.journal.has("cleanup-confirmed.json")
    assert harness.clock.elapsed <= 15.0
    assert all(call[1] == 123 for call in harness.provider.calls if call[0] != "create")


def test_destroy_timeouts_are_clamped_and_do_not_invent_success(
    harness: Harness,
) -> None:
    harness.clock.elapsed = 0.5
    harness.intent = harness.intent.model_copy(
        update={"operation_timeout_seconds": 3.5}
    )
    harness.provider.destroy_error = True
    result = harness.execute()
    assert result.cleanup_status == UNCONFIRMED and harness.clock.elapsed == 6.5
    destroys = [call for call in harness.provider.calls if call[0] == "destroy"]
    assert destroys[-1][2] < destroys[0][2]
    assert not any(call[0] == "observe" for call in harness.provider.calls)


def test_late_absence_success_is_rejected(harness: Harness) -> None:
    harness.provider.observe_delay = 16.0
    assert harness.execute().cleanup_status == UNCONFIRMED
    assert not harness.journal.has("cleanup-confirmed.json")


def test_unexpected_volumes_are_retained_as_separate_unresolved_resources(
    harness: Harness,
) -> None:
    harness.provider.observe_value = AbsenceResult(
        query_instance_id=123,
        succeeded=True,
        matching_instance_ids=(),
        pagination_exhausted=True,
        persistent_volume_ids=(987,),
    )
    assert harness.execute().cleanup_status == UNCONFIRMED
    record = json.loads((harness.directory / "unexpected-volumes.json").read_bytes())
    assert record["persistent_volume_ids"] == [987]
    assert all(call[1] == 123 for call in harness.provider.calls if call[0] != "create")


def test_guard_cleans_never_started_guest_without_work_process(
    harness: Harness,
) -> None:
    harness.journal.initialize(harness.intent)
    harness.journal.start_create(harness.intent)
    harness.journal.retain_created(CreateResult(new_contract=123))
    worker = DeadlineGuard(
        harness.intent, harness.journal, harness.provider, clock=harness.clock
    )
    assert worker.readiness().process_id == os.getpid()
    assert worker.run() == CONFIRMED
    assert harness.clock.elapsed == 10.0
    assert [call[0] for call in harness.provider.calls] == ["destroy", "observe"]


def test_guard_unknown_id_never_discovers_or_creates(harness: Harness) -> None:
    harness.journal.initialize(harness.intent)
    harness.journal.start_create(harness.intent)
    worker = DeadlineGuard(
        harness.intent, harness.journal, harness.provider, clock=harness.clock
    )
    assert worker.run() == UNKNOWN_CREATE
    assert harness.clock.elapsed == 15.0 and harness.provider.calls == []


def test_wall_clock_rollback_does_not_renew_either_budget(harness: Harness) -> None:
    harness.workflow.rollback = True
    harness.workflow.late = "retrieve"
    harness.provider.destroy_error = True
    result = harness.execute()
    assert result.work_status == "FAILED" and result.cleanup_status == UNCONFIRMED
    assert harness.clock.elapsed == 15.0


def test_journal_records_fsync_and_reopen_without_replacement(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    original = os.fsync

    def fsync(descriptor: int) -> None:
        calls.append(descriptor)
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    harness.execute()
    assert len(calls) >= 12
    reopened = ControlJournal(harness.directory)
    try:
        reopened.require_intent(harness.intent)
        assert (
            reopened.exact_instance_id() == 123
            and reopened.cleanup_status() == CONFIRMED
        )
        with pytest.raises(FileExistsError):
            reopened.start_create(harness.intent)
    finally:
        reopened.close()


def test_journal_rejects_link_and_changed_intent(harness: Harness) -> None:
    harness.journal.initialize(harness.intent)
    changed = harness.intent.model_copy(
        update={"launch_request_sha256": "sha256:" + "b" * 64}
    )
    with pytest.raises(ControlFailure, match="INTENT_MISMATCH"):
        harness.journal.initialize(changed)
    (harness.directory / "created.json").symlink_to(harness.directory / "intent.json")
    with pytest.raises(OSError):
        harness.journal.exact_instance_id()


@pytest.mark.parametrize("phase", ["create", "readiness", "guard"])
def test_blocking_callback_is_interrupted_by_real_local_alarm(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    harness.clock.elapsed = 9.95

    def blocked(*args: Any, **kwargs: Any) -> Any:
        time.sleep(1.0)
        pytest.fail("blocking callback outlived its budget")

    if phase == "create":
        monkeypatch.setattr(harness.provider, "create", blocked)
    elif phase == "guard":
        monkeypatch.setattr(harness.guard, "arm", blocked)
    else:
        monkeypatch.setattr(harness.workflow, "readiness", blocked)
    started = time.monotonic()
    if phase == "guard":
        with pytest.raises(ControlFailure, match="CALL_TIMEOUT"):
            harness.execute()
        assert harness.provider.calls == []
    else:
        result = harness.execute()
        assert result.work_status == (UNKNOWN_CREATE if phase == "create" else "FAILED")
    assert time.monotonic() - started < 0.5


def test_existing_real_alarm_is_restored_without_renewal(harness: Harness) -> None:
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    try:
        signal.setitimer(signal.ITIMER_REAL, 5.0)
        harness.execute()
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert 0 < remaining < 5.0 and interval == 0.0
        assert signal.getsignal(signal.SIGALRM) == previous_handler
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def test_failed_created_directory_fsync_does_not_publish_an_exact_target(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = os.fsync
    failed = False

    def fail_once(descriptor: int) -> None:
        nonlocal failed
        if (
            descriptor == harness.journal.root.fd
            and (harness.directory / "created.json").exists()
            and not failed
        ):
            failed = True
            raise OSError("synthetic directory fsync failure")
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fail_once)
    result = harness.execute()
    assert failed and result.instance_id is None
    assert result.work_status == UNKNOWN_CREATE and harness.workflow.phases == []
    assert [call[0] for call in harness.provider.calls] == ["create"]
    assert not (harness.directory / "created.json").exists()


def test_malformed_retained_cleanup_receipt_cannot_be_positive(
    harness: Harness,
) -> None:
    harness.journal.initialize(harness.intent)
    harness.journal.start_create(harness.intent)
    harness.journal.retain_created(CreateResult(new_contract=123))
    harness.journal.record("cleanup-confirmed.json", {"status": CONFIRMED})
    with pytest.raises(ControlFailure, match="CLEANUP_RECORD_INVALID"):
        harness.journal.cleanup_status()


@pytest.mark.parametrize(
    "field",
    [
        "launch_request_sha256",
        "execution_deadline_utc",
        "cleanup_deadline_utc",
    ],
)
def test_workflow_must_bind_exact_launch_and_deadlines_before_any_journal_or_create(
    harness: Harness,
    field: str,
) -> None:
    setattr(harness.workflow, field, "mismatch")
    with pytest.raises(ControlFailure, match="WORKFLOW_INTENT_MISMATCH"):
        harness.execute()
    assert harness.guard.arm_calls == 0 and harness.provider.calls == []
    assert not harness.journal.has("intent.json")


def test_execution_window_cannot_exceed_two_hours(harness: Harness) -> None:
    harness.clock.elapsed = -7200.0
    with pytest.raises(ControlFailure, match="EXECUTION_WINDOW_TOO_LONG"):
        harness.execute()
    assert harness.provider.calls == [] and not harness.journal.has("intent.json")


@pytest.mark.parametrize("reserve", [0, 601])
def test_cleanup_reserve_is_separately_bounded(harness: Harness, reserve: int) -> None:
    value = harness.intent.model_dump(mode="json")
    value["cleanup_deadline_utc"] = (
        harness.clock.start + timedelta(seconds=10 + reserve)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(ValidationError):
        ControlIntent.model_validate_json(json.dumps(value))


def test_guard_does_not_repeat_settlement_after_controller_terminal_receipt(
    harness: Harness,
) -> None:
    assert harness.execute().cleanup_status == CONFIRMED
    calls = list(harness.provider.calls)
    worker = DeadlineGuard(
        harness.intent, harness.journal, harness.provider, clock=harness.clock
    )
    assert worker.run() == CONFIRMED and harness.provider.calls == calls
    record = json.loads((harness.directory / "cleanup-confirmed.json").read_bytes())
    assert record["destroy"] == {"instance_id": 123, "acknowledged": True}
    assert record["observation"]["pagination_exhausted"] is True
    assert record["observation"]["matching_instance_ids"] == []


def test_guard_never_renews_failed_early_cleanup_window(harness: Harness) -> None:
    harness.workflow.fail = "readiness"
    harness.provider.destroy_error = True
    assert harness.execute().cleanup_status == UNCONFIRMED
    assert harness.clock.elapsed == 5.0
    calls = list(harness.provider.calls)
    worker = DeadlineGuard(
        harness.intent, harness.journal, harness.provider, clock=harness.clock
    )
    assert worker.run() == UNCONFIRMED and harness.provider.calls == calls
    assert harness.clock.elapsed == 5.0


def test_guard_truthy_string_is_not_readiness(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness.guard, "is_alive", lambda ready: "true")
    with pytest.raises(ControlFailure, match="INDEPENDENT_GUARD_REQUIRED"):
        harness.execute()
    assert harness.provider.calls == []


def test_cleanup_cannot_be_positive_before_full_receipt_is_durable(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = os.fsync

    def fail_confirmed(descriptor: int) -> None:
        if (
            descriptor == harness.journal.root.fd
            and (harness.directory / "cleanup-confirmed.json").exists()
        ):
            raise OSError("synthetic receipt fsync failure")
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fail_confirmed)
    assert harness.execute().cleanup_status == UNCONFIRMED
    assert not (harness.directory / "cleanup-confirmed.json").exists()
