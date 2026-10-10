from __future__ import annotations

import copy
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from inferdrome import breakpoint_pilot_guard as guard


def approval() -> dict[str, Any]:
    return {
        "schema": "inferdrome.breakpoint-pilot-approval.v1",
        "execution_authorized": True,
        "provider": "vast",
        "instance_id": "12345",
        "billing_start_utc": "2026-10-10T00:00:00Z",
        "max_session_s": 12_600,
        "cleanup_reserve_s": 600,
        "hourly_rate_usd": "2.50",
        "cap_usd": "12",
        "reserve_usd": "1.00",
        "storage_allowance_usd": "0.50",
        "transfer_allowance_usd": "0.50",
        "plan_sha256": "sha256:" + "a" * 64,
        "source_revision": "b" * 40,
        "image_reference": guard.IMAGE,
        "guardian_host_kind": "EXTERNAL_OPERATOR_HOST",
    }


class Clock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
        self.elapsed = 0.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds
        self.wall += timedelta(seconds=seconds)


class FakeProvider:
    def __init__(self, *, fails: bool = False, malformed: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.present = True
        self.fails = fails
        self.malformed = malformed

    def __call__(self, argv: list[str], timeout_s: float) -> guard.CommandResult:
        self.calls.append(argv)
        self.timeouts.append(timeout_s)
        if argv[1:3] == ["destroy", "instance"]:
            assert argv[3:] == ["12345", "-y", "--raw"]
            if self.fails:
                return guard.CommandResult(
                    1, b"secret provider error", "sha256:" + "c" * 64
                )
            self.present = False
            return guard.CommandResult(0, b"", "sha256:" + "c" * 64)
        assert argv[1:] == ["show", "instances", "--raw"]
        rows = [{"id": 77777, "private_account_detail": "DO_NOT_RETAIN"}]
        if self.present:
            rows.append({"id": 12345})
        data = (
            b'{"instances":[],"next_token":"more"}'
            if self.malformed
            else json.dumps(rows).encode()
        )
        return guard.CommandResult(0, data, "sha256:" + "c" * 64)


def make_guard(
    tmp_path: Path, clock: Clock, provider: FakeProvider, **kwargs: Any
) -> guard.Guardian:
    return guard.Guardian(
        approval(),
        Path(sys.executable),
        tmp_path / "guardian",
        runner=provider,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=kwargs.pop("sleep", clock.sleep),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("execution_authorized", False),
        ("execution_authorized", 1),
        ("provider", "other"),
        ("instance_id", "12345; rm -rf /"),
        ("instance_id", "--all"),
        ("instance_id", 12345),
        ("instance_id", "001"),
        ("max_session_s", True),
        ("max_session_s", 12_601),
        ("cleanup_reserve_s", 599),
        ("hourly_rate_usd", "NaN"),
        ("hourly_rate_usd", "Infinity"),
        ("hourly_rate_usd", "-1"),
        ("hourly_rate_usd", 2.5),
        ("cap_usd", None),
        ("cap_usd", "1"),
        ("billing_start_utc", "2026-10-10T00:00:00"),
        ("billing_start_utc", "2026-13-10T00:00:00Z"),
        ("plan_sha256", "a" * 64),
        ("source_revision", "main"),
        ("guardian_host_kind", "RENTED_GPU_HOST"),
        ("image_reference", "vastai/vllm:latest"),
    ],
)
def test_invalid_approval_rejected(key: str, value: Any) -> None:
    candidate = approval()
    candidate[key] = value
    with pytest.raises(guard.GuardError):
        guard.parse_approval(candidate)


def test_unknown_and_duplicate_approval_fields_fail(tmp_path: Path) -> None:
    value = approval()
    value["api_key"] = "PRIVATE"
    with pytest.raises(guard.GuardError):
        guard.parse_approval(value)
    target = tmp_path / "approval.json"
    target.write_text('{"instance_id":"1","instance_id":"2"}')
    with pytest.raises(guard.GuardError, match="ambiguous"):
        guard.load_approval(target)


def test_maximum_spend_includes_storage_transfer_and_reserve() -> None:
    value = approval()
    value["cap_usd"] = "10.75"
    assert guard.parse_approval(value) == value  # 3.5 * 2.50 + 0.50 + 0.50 + 1
    for key in ("storage_allowance_usd", "transfer_allowance_usd", "reserve_usd"):
        more = copy.deepcopy(value)
        more[key] = "2.00"
        with pytest.raises(guard.GuardError, match="exceed"):
            guard.parse_approval(more)


def test_guard_destroys_only_target_and_reads_back_absence(tmp_path: Path) -> None:
    clock, provider = Clock(), FakeProvider()
    guardian = make_guard(tmp_path, clock, provider)
    result = guardian.watch()
    assert result["status"] == "ABSENCE_CONFIRMED"
    assert result["deadline_met"] is True
    assert result["provider_invoice_verified"] is False
    assert result["other_resources_covered"] is False
    assert provider.calls[-2][1:] == ["destroy", "instance", "12345", "-y", "--raw"]
    assert provider.calls[-1][1:] == ["show", "instances", "--raw"]
    assert all(0 < timeout <= 30 for timeout in provider.timeouts)
    assert not (guardian.output_dir / "ready.json").exists()
    retained = "\n".join(p.read_text() for p in guardian.output_dir.iterdir())
    assert "DO_NOT_RETAIN" not in retained
    assert "secret provider error" not in retained
    assert (guardian.output_dir.stat().st_mode & 0o777) == 0o700
    assert all(
        (p.stat().st_mode & 0o777) == 0o600 for p in guardian.output_dir.iterdir()
    )


def test_failed_destroy_is_not_claimed_as_closed(tmp_path: Path) -> None:
    clock, provider = Clock(), FakeProvider(fails=True)
    guardian = make_guard(tmp_path, clock, provider)
    result = guardian.watch()
    assert result["status"] == "CLEANUP_UNCONFIRMED"
    assert result["deadline_met"] is False
    assert clock.elapsed == 12_600
    assert len(provider.calls) > 4
    assert (guardian.output_dir / "result.json").is_file()
    assert "secret provider error" not in "".join(
        p.read_text() for p in guardian.output_dir.iterdir()
    )


def test_destroy_failure_followed_by_independent_absence_is_confirmed(
    tmp_path: Path,
) -> None:
    clock, provider = Clock(), FakeProvider()

    def runner(argv: list[str], timeout_s: float) -> guard.CommandResult:
        result = provider(argv, timeout_s)
        if argv[1] == "destroy":
            return guard.CommandResult(1, b"request timed out", "sha256:" + "c" * 64)
        return result

    guardian = make_guard(tmp_path, clock, provider)
    guardian.runner = runner
    assert guardian.watch()["status"] == "ABSENCE_CONFIRMED"


def test_partial_inventory_never_arms_or_confirms_absence(tmp_path: Path) -> None:
    clock, provider = Clock(), FakeProvider(malformed=True)
    # Begin close to the deadline to avoid thousands of irrelevant fake retries.
    clock.sleep(12_550)
    guardian = make_guard(tmp_path, clock, provider)
    result = guardian.watch()
    assert result["status"] == "CLEANUP_UNCONFIRMED"
    assert not (guardian.output_dir / "ready.json").exists()


def test_interruption_attempts_immediate_cleanup(tmp_path: Path) -> None:
    clock, provider = Clock(), FakeProvider()

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    guardian = make_guard(tmp_path, clock, provider, sleep=interrupted)
    result = guardian.watch()
    assert result["status"] == "ABSENCE_CONFIRMED"
    assert result["trigger"] == "INTERRUPTED"
    assert clock.elapsed == 0


def test_signal_flag_attempts_cleanup(tmp_path: Path) -> None:
    clock, provider = Clock(), FakeProvider()
    guardian = make_guard(tmp_path, clock, provider)
    guardian.interrupted = True
    result = guardian.watch()
    assert result["status"] == "ABSENCE_CONFIRMED"
    assert result["trigger"] == "SIGNAL"


def test_second_interruption_retains_cleanup_uncertainty(tmp_path: Path) -> None:
    clock, provider = Clock(), FakeProvider(fails=True)

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    guardian = make_guard(tmp_path, clock, provider, sleep=interrupted)
    result = guardian.watch()
    assert result["status"] == "CLEANUP_UNCONFIRMED"
    assert result["trigger"] == "CLEANUP_INTERRUPTED_OR_FAILED"
    assert (guardian.output_dir / "result.json").is_file()
    assert any(argv[1] == "destroy" for argv in provider.calls)


def test_unexpected_wait_failure_still_destroys_exact_instance(tmp_path: Path) -> None:
    clock, provider = Clock(), FakeProvider()

    def broken_wait(_seconds: float) -> None:
        raise RuntimeError("private diagnostic detail")

    result = make_guard(tmp_path, clock, provider, sleep=broken_wait).watch()
    assert result["status"] == "ABSENCE_CONFIRMED"
    assert result["trigger"] == "GUARD_FAILED"
    assert "private diagnostic detail" not in json.dumps(result)


def test_ready_receipt_is_bound_and_recent(tmp_path: Path) -> None:
    clock, provider = Clock(), FakeProvider()
    captured: list[dict[str, Any]] = []

    def inspect(_seconds: float) -> None:
        captured.append(json.loads((tmp_path / "guardian" / "ready.json").read_text()))
        raise KeyboardInterrupt

    guardian = make_guard(tmp_path, clock, provider, sleep=inspect)
    guardian.watch()
    receipt = captured[0]
    guard.validate_ready_receipt(approval(), receipt, now=clock.now())
    with pytest.raises(guard.GuardError, match="stale"):
        guard.validate_ready_receipt(
            approval(), receipt, now=clock.now() + timedelta(seconds=61)
        )
    changed = copy.deepcopy(receipt)
    changed["instance_id_sha256"] = "sha256:" + "9" * 64
    with pytest.raises(guard.GuardError, match="bound"):
        guard.validate_ready_receipt(approval(), changed, now=clock.now())


def test_wall_clock_rollback_cannot_extend_wait(tmp_path: Path) -> None:
    clock, provider = Clock(), FakeProvider()
    rolled_back = False

    def sleep(seconds: float) -> None:
        nonlocal rolled_back
        clock.sleep(seconds)
        if not rolled_back:
            clock.wall -= timedelta(hours=1)
            rolled_back = True

    guardian = make_guard(tmp_path, clock, provider, sleep=sleep)
    result = guardian.watch()
    assert result["status"] == "ABSENCE_CONFIRMED"
    assert clock.elapsed == 12_000


def test_already_expired_approval_still_attempts_cleanup(tmp_path: Path) -> None:
    clock, provider = Clock(), FakeProvider()
    clock.sleep(13_000)
    result = make_guard(tmp_path, clock, provider).watch()
    assert result["status"] == "ABSENCE_CONFIRMED"
    assert result["deadline_met"] is False
    assert result["trigger"] == "EXPIRED_AT_ARM"


def test_future_billing_and_existing_directory_fail_before_provider_calls(
    tmp_path: Path,
) -> None:
    clock, provider = Clock(), FakeProvider()
    clock.wall -= timedelta(seconds=1)
    with pytest.raises(guard.GuardError, match="future"):
        make_guard(tmp_path, clock, provider).watch()
    assert provider.calls == []
    clock.wall += timedelta(seconds=1)
    (tmp_path / "guardian").mkdir()
    with pytest.raises(FileExistsError):
        make_guard(tmp_path, clock, provider).watch()
    assert provider.calls == []


@pytest.mark.parametrize(
    "data",
    [b'[{"id":true}]', b'[{"id":1},{"id":1}]', b'[{"id":"12345"}]', b"{}", b"null"],
)
def test_inventory_ambiguity_never_means_absence(data: bytes) -> None:
    with pytest.raises(guard.GuardError):
        guard._instance_present(
            guard.CommandResult(0, data, "sha256:" + "c" * 64), "12345"
        )


def test_real_command_runner_is_bounded_and_does_not_echo_stderr() -> None:
    result = guard.run_command(
        [
            sys.executable,
            "-c",
            "import sys; print('[]'); print('private key', file=sys.stderr)",
        ],
        5,
    )
    assert result.stdout.strip() == b"[]"
    assert "private" not in result.stderr_sha256
    with pytest.raises(guard.GuardError, match="timed out"):
        guard.run_command([sys.executable, "-c", "import time; time.sleep(5)"], 0.05)
    with pytest.raises(guard.GuardError, match="output exceeded"):
        guard.run_command([sys.executable, "-c", "print('x' * 5000000)"], 5)


def test_no_approval_cannot_invoke_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "undecided.json"
    target.write_text(
        json.dumps({"execution_authorized": False, "spending": "UNDECIDED"})
    )

    def prohibited(*args: Any, **kwargs: Any) -> None:
        pytest.fail("provider command reached before authorization validation")

    monkeypatch.setattr(guard.subprocess, "Popen", prohibited)
    assert (
        guard.main(
            [
                "--approval",
                str(target),
                "--vast-executable",
                sys.executable,
                "--output-dir",
                str(tmp_path / "state"),
                "--confirm",
                "DESTROY_EXACT_INSTANCE_AT_DEADLINE",
            ]
        )
        == 2
    )
