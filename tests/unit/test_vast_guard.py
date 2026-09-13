"""Real detached cleanup processes; every provider request is a filesystem fake."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from inferdrome.deployment import vast_control as control
from inferdrome.deployment import vast_guard as guard_module
from inferdrome.deployment.vast_guard import DetachedGuard
from inferdrome.deployment.vast_provider import HttpReply, VastProvider
from inferdrome.routing_execution.canonical import canonical_json_bytes
from tests.unit.test_vast_bootstrap import launch_intent
from tests.unit.test_vast_control import FakeClock, FakeProvider, intent_for


class FileTransport:
    """No sockets: on-disk fake state is shared with the forked worker."""

    def __init__(
        self,
        directory: Path,
        *,
        destroy_fails: bool = False,
        missing_volume_info: bool = False,
    ) -> None:
        self.directory = directory
        self.destroy_fails = destroy_fails
        self.missing_volume_info = missing_volume_info

    def request(
        self, method: str, path: str, body: bytes | None, *, seconds: float
    ) -> HttpReply:
        assert seconds > 0
        descriptor = os.open(
            self.directory / "requests.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(
                descriptor,
                canonical_json_bytes({"method": method, "path": path}) + b"\n",
            )
        finally:
            os.close(descriptor)
        if method == "PUT" and path.startswith("/api/v0/asks/"):
            result: dict[str, object] = {"success": True, "new_contract": 101}
        elif method == "DELETE":
            assert path.rstrip("/") == "/api/v0/instances/101"
            if self.destroy_fails:
                raise TimeoutError("SYNTHETIC_SECRET_PROVIDER_DIAGNOSTIC")
            (self.directory / "destroyed").touch()
            result = {"success": True}
        elif method == "GET" and path.startswith("/api/v1/instances"):
            result = {
                "success": True,
                "instances_found": 0 if (self.directory / "destroyed").exists() else 1,
                "total_instances": 0 if (self.directory / "destroyed").exists() else 1,
                "instances": []
                if (self.directory / "destroyed").exists()
                else [
                    {"id": 101}
                    if self.missing_volume_info
                    else {"id": 101, "volume_info": []}
                ],
                "next_token": None,
            }
        else:
            raise AssertionError("unexpected fake request")
        return HttpReply(status=200, body=canonical_json_bytes(result))


def short_intent(*, execution_seconds: int = 3) -> control.ControlIntent:
    now = datetime.now(UTC)
    return control.ControlIntent(
        launch_request_sha256="sha256:" + "a" * 64,
        execution_deadline_utc=(now + timedelta(seconds=execution_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        cleanup_deadline_utc=(now + timedelta(seconds=execution_seconds + 2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        operation_timeout_seconds=0.5,
        poll_seconds=0.05,
    )


def provider_for(journal: control.ControlJournal, directory: Path) -> VastProvider:
    return VastProvider(
        launch_intent(),
        journal=journal,
        transport=FileTransport(directory),
    )


def retain(journal: control.ControlJournal, intent: control.ControlIntent) -> None:
    journal.start_create(intent)
    journal.retain_created(control.CreateResult(new_contract=101))


def wait_record(path: Path, *, seconds: float = 8) -> dict[str, object]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_bytes())  # type: ignore[no-any-return]
        time.sleep(0.02)
    pytest.fail("detached worker did not produce its bounded receipt")


def test_real_detached_worker_acks_and_settles_exact_id(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    journal = control.ControlJournal(tmp_path)
    intent = short_intent()
    journal.initialize(intent)
    guard = DetachedGuard(provider_for(journal, tmp_path))
    try:
        ready = guard.arm(intent, journal, seconds=1)
        assert ready.process_id != os.getpid()
        assert os.getsid(ready.process_id) == ready.process_id
        assert guard.is_alive(ready)
        retain(journal, intent)
        journal.request_cleanup()
        assert guard.wait(seconds=5)
        assert journal.cleanup_status() == control.CONFIRMED
        assert not guard.is_alive(ready)
        requests = [
            json.loads(line)
            for line in (tmp_path / "requests.jsonl").read_bytes().splitlines()
        ]
        assert any(item["method"] == "DELETE" for item in requests)
        assert all("asks" not in item["path"] for item in requests)
        with pytest.raises(control.ControlFailure, match="ARM_REFUSED"):
            guard.arm(intent, journal, seconds=1)
    finally:
        journal.request_cleanup()
        guard.wait(seconds=5)
        journal.close()


def test_cleanup_survives_controller_exit(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    controller_pid = os.fork()
    if controller_pid == 0:
        try:
            journal = control.ControlJournal(tmp_path)
            intent = short_intent()
            journal.initialize(intent)
            guard = DetachedGuard(provider_for(journal, tmp_path))
            ready = guard.arm(intent, journal, seconds=1)
            retain(journal, intent)
            journal.record("controller-ready.json", {"guard_pid": ready.process_id})
            # Abrupt exit: no cleanup request, finally, join, or provider DELETE.
            os._exit(0)
        except BaseException:
            os._exit(2)
    _, status = os.waitpid(controller_pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    result = wait_record(tmp_path / "cleanup-confirmed.json")
    assert result["instance_id"] == 101
    wait_record(tmp_path / "guard-finished.json")
    assert (tmp_path / "destroyed").exists()


def test_missing_volume_metadata_still_deletes_but_settles_unconfirmed(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    journal = control.ControlJournal(tmp_path)
    intent = short_intent()
    journal.initialize(intent)
    provider = VastProvider(
        launch_intent(),
        journal=journal,
        transport=FileTransport(tmp_path, missing_volume_info=True),
    )
    guard = DetachedGuard(provider)
    try:
        ready = guard.arm(intent, journal, seconds=1)
        retain(journal, intent)
        started = time.monotonic()
        journal.request_cleanup()
        assert guard.wait(seconds=4)
        assert time.monotonic() - started < 3
        assert not guard.is_alive(ready)
        requests = [
            json.loads(line)
            for line in (tmp_path / "requests.jsonl").read_bytes().splitlines()
        ]
        assert [item for item in requests if item["method"] == "DELETE"] == [
            {"method": "DELETE", "path": "/api/v0/instances/101/"}
        ]
        assert all("asks" not in item["path"] for item in requests)
        assert (tmp_path / "destroyed").exists()
        assert json.loads(
            (tmp_path / "provider-destroy-ack-101.json").read_bytes()
        ) == {"instance_id": 101, "acknowledged": True}
        assert not journal.has("provider-no-volumes-101.json")
        assert not journal.has("cleanup-confirmed.json")
        assert journal.cleanup_status() == control.UNCONFIRMED
        assert json.loads((tmp_path / "guard-finished.json").read_bytes()) == {
            "status": control.UNCONFIRMED
        }
        window = json.loads((tmp_path / "cleanup-window.json").read_bytes())
        assert window["deadline_monotonic"] - window["started_monotonic"] == (
            pytest.approx(2)
        )
        assert (
            window["deadline_unix"]
            <= datetime.strptime(intent.cleanup_deadline_utc, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=UTC)
            .timestamp()
        )
    finally:
        journal.request_cleanup()
        guard.wait(seconds=5)
        journal.close()


def test_unknown_create_never_guesses_cleanup_id(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    journal = control.ControlJournal(tmp_path)
    intent = short_intent(execution_seconds=2)
    journal.initialize(intent)
    guard = DetachedGuard(provider_for(journal, tmp_path))
    try:
        guard.arm(intent, journal, seconds=0.5)
        journal.start_create(intent)
        assert guard.wait(seconds=5)
        assert journal.has("create-unresolved.json")
        assert not (tmp_path / "requests.jsonl").exists()
    finally:
        guard.wait(seconds=5)
        journal.close()


def test_arm_refuses_other_threads_before_fork(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    journal = control.ControlJournal(tmp_path)
    intent = short_intent()
    journal.initialize(intent)
    finished = threading.Event()
    thread = threading.Thread(target=finished.wait)
    thread.start()
    try:
        guard = DetachedGuard(provider_for(journal, tmp_path))
        with pytest.raises(control.ControlFailure, match="ARM_REFUSED"):
            guard.arm(intent, journal, seconds=1)
        assert not journal.has("guard-launch.json")
    finally:
        finished.set()
        thread.join()
        journal.close()


def test_execution_deadline_survives_wall_clock_rollback(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    journal = control.ControlJournal(tmp_path)
    clock = FakeClock()
    intent = intent_for(clock)
    journal.initialize(intent)
    try:
        first = control._Budget(intent.execution_deadline_utc, clock)
        journal.bound_execution(intent, clock, first)
        clock.elapsed = 4
        clock.wall_offset = -100
        restarted = control._Budget(intent.execution_deadline_utc, clock)
        journal.bound_execution(intent, clock, restarted)
        assert restarted.remaining() == 6
        clock.elapsed = 10
        with pytest.raises(control.ControlFailure, match="DEADLINE"):
            restarted.remaining()
    finally:
        journal.close()


def test_dead_child_handle_does_not_accept_saved_pid(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    journal = control.ControlJournal(tmp_path)
    intent = short_intent()
    journal.initialize(intent)
    guard = DetachedGuard(provider_for(journal, tmp_path))
    try:
        ready = guard.arm(intent, journal, seconds=1)
        os.kill(ready.process_id, signal.SIGKILL)
        assert guard.wait(seconds=2)
        assert not guard.is_alive(ready)
        assert not (tmp_path / "requests.jsonl").exists()
    finally:
        guard.wait(seconds=5)
        journal.close()


def test_unresolved_create_budget_cannot_renew_after_clock_rollback(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    journal = control.ControlJournal(tmp_path)
    clock = FakeClock()
    intent = intent_for(clock)
    journal.initialize(intent)
    try:
        original = control._Budget(intent.execution_deadline_utc, clock)
        journal.bound_execution(intent, clock, original)
        journal.start_create(intent)
        clock.elapsed = 4
        clock.wall_offset = -100
        restarted = control.DeadlineGuard(
            intent, journal, FakeProvider(clock, journal), clock=clock
        )
        assert restarted.run() == control.UNKNOWN_CREATE
        assert clock.elapsed == 15
    finally:
        journal.close()


def test_wedged_journal_lock_cannot_leave_worker_unbounded(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    journal = control.ControlJournal(tmp_path)
    intent = short_intent(execution_seconds=2)
    journal.initialize(intent)
    guard = DetachedGuard(provider_for(journal, tmp_path))
    try:
        guard.arm(intent, journal, seconds=1)
        retain(journal, intent)
        fcntl.flock(journal._lock_fd, fcntl.LOCK_EX)
        try:
            # The child cannot read its journal while another live process has
            # wedged the lock. It must exit boundedly, never invent success.
            assert guard.wait(seconds=6)
        finally:
            fcntl.flock(journal._lock_fd, fcntl.LOCK_UN)
        assert journal.cleanup_status() != control.CONFIRMED
        assert not (tmp_path / "requests.jsonl").exists()
    finally:
        journal.request_cleanup()
        guard.wait(seconds=5)
        journal.close()


@pytest.mark.parametrize("disposition", [signal.SIG_IGN, lambda _number, _frame: None])
def test_arm_refuses_external_child_reaping(
    tmp_path: Path, disposition: object
) -> None:
    tmp_path.chmod(0o700)
    journal = control.ControlJournal(tmp_path)
    intent = short_intent()
    journal.initialize(intent)
    original = signal.signal(signal.SIGCHLD, disposition)  # type: ignore[arg-type]
    try:
        guard = DetachedGuard(provider_for(journal, tmp_path))
        with pytest.raises(control.ControlFailure, match="ARM_REFUSED"):
            guard.arm(intent, journal, seconds=1)
        assert not journal.has("guard-launch.json")
    finally:
        signal.signal(signal.SIGCHLD, original)
        journal.close()


def test_worker_hard_alarm_escapes_ordinary_retry_handlers() -> None:
    def retries() -> None:
        try:
            control._bounded_call(lambda: time.sleep(1), 1)
        except Exception:
            # A normal ControlFailure may be caught by a provider retry loop.
            # The restored hard worker alarm must still interrupt this sleep.
            time.sleep(1)

    started = time.monotonic()
    with pytest.raises(guard_module._WorkerDeadline):
        guard_module._hard_call(retries, started + 0.03)
    assert time.monotonic() - started < 0.5


def test_arm_refuses_blocked_deadline_signal(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    journal = control.ControlJournal(tmp_path)
    intent = short_intent()
    journal.initialize(intent)
    original = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        guard = DetachedGuard(provider_for(journal, tmp_path))
        with pytest.raises(control.ControlFailure, match="ARM_REFUSED"):
            guard.arm(intent, journal, seconds=1)
        assert not journal.has("guard-launch.json")
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, original)
        journal.close()


def test_child_setup_deadline_does_not_depend_on_parent_ack_cleanup(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    journal = control.ControlJournal(tmp_path)
    intent = short_intent()
    journal.initialize(intent)
    provider = provider_for(journal, tmp_path)
    reader, writer = os.pipe()
    fcntl.flock(journal._lock_fd, fcntl.LOCK_EX)
    pid = os.fork()
    if pid == 0:
        os.close(reader)
        guard_module._worker(
            intent, provider, journal.root.fd, writer, time.monotonic() + 0.1
        )
        os._exit(2)
    os.close(writer)
    reaped = False
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            observed, status = os.waitpid(pid, os.WNOHANG)
            if observed:
                reaped = True
                assert os.waitstatus_to_exitcode(status) == 1
                break
            time.sleep(0.01)
        assert reaped, "child setup depended on a live parent to enforce timeout"
        assert os.read(reader, 4096) == b""
    finally:
        if not reaped:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        fcntl.flock(journal._lock_fd, fcntl.LOCK_UN)
        os.close(reader)
        journal.close()


def test_arm_failure_masks_termination_through_child_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    journal = control.ControlJournal(tmp_path)
    intent = short_intent()
    journal.initialize(intent)
    guard = DetachedGuard(provider_for(journal, tmp_path))
    original_kill = os.kill
    signalled: list[int] = []

    def broken_ack(_reader: int, _seconds: float) -> bytes:
        raise control.ControlFailure("SYNTHETIC_ACK_FAILURE")

    def checked_kill(pid: int, number: int) -> None:
        active = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        assert {signal.SIGALRM, signal.SIGINT, signal.SIGTERM, signal.SIGCHLD} <= active
        signalled.append(pid)
        original_kill(pid, number)

    monkeypatch.setattr(guard, "_read_ack", broken_ack)
    monkeypatch.setattr(os, "kill", checked_kill)
    try:
        with pytest.raises(control.ControlFailure, match="ARM_FAILED"):
            guard.arm(intent, journal, seconds=1)
        assert signalled and guard._pid is None and guard._ready is None
        assert guard.wait(seconds=1)
        assert not journal.has("create-start.json")
    finally:
        journal.close()
