"""Lambda lifecycle protection is bounded, secret-safe, and independently armed."""

from __future__ import annotations

import errno
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.lambda_gpu_guard as guard

INSTANCE_ID = "a" * 32
API_KEY = "lambda-secret-api-key-value"
NOW = datetime(2026, 8, 18, 20, 0, tzinfo=UTC)


def _instance(
    *,
    status: str = "active",
    price_cents_per_hour: int | None = 129,
    instance_type_name: str = "gpu_1x_a10",
) -> dict[str, object]:
    value: dict[str, object] = {
        "hostname": "gpu-a10.example.test",
        "id": INSTANCE_ID,
        "ip": "203.0.113.10",
        "jupyter_token": "must-never-escape",
        "jupyter_url": "https://example.test/?token=must-never-escape",
        "status": status,
    }
    if price_cents_per_hour is not None:
        value["instance_type"] = {
            "name": instance_type_name,
            "price_cents_per_hour": price_cents_per_hour,
        }
    return value


class FakeTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, object | None, str]] = []

    def __call__(
        self,
        method: str,
        path: str,
        payload: object | None,
        api_key: str,
        _timeout: float,
    ) -> object:
        self.calls.append((method, path, payload, api_key))
        if not self.responses:
            raise AssertionError("unexpected Lambda API request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeProcess:
    def __init__(
        self,
        arguments: list[str],
        *,
        exit_code: int | None = None,
    ) -> None:
        self.args = arguments
        self.pid = 4321
        self.exit_code = exit_code

    def poll(self) -> int | None:
        return self.exit_code

    def wait(self, *, timeout: float) -> int:
        del timeout
        return 0


def _publish_ready_from_command(arguments: list[str]) -> None:
    ready_path = Path(arguments[arguments.index("--ready-path") + 1])
    readiness_token = arguments[arguments.index("--readiness-token") + 1]
    instance_id = arguments[arguments.index("--instance-id") + 1]
    guard._write_watchdog_ready(
        ready_path,
        instance_id=instance_id,
        readiness_token=readiness_token,
    )


def test_instance_list_discards_provider_secrets() -> None:
    transport = FakeTransport([{"data": [_instance()]}])
    client = guard.LambdaCloudClient(API_KEY, transport=transport)

    instances = client.list_instances()
    rendered = json.dumps([instance.public_record() for instance in instances])

    assert len(instances) == 1
    assert instances[0].hourly_rate_usd == Decimal("1.29")
    assert instances[0].instance_type_name == "gpu_1x_a10"
    assert "must-never-escape" not in rendered
    assert transport.calls[0][:3] == ("GET", "/instances", None)


def test_urllib_transport_sends_explicit_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"data": []}'

    class FakeOpener:
        def open(self, request: Any, *, timeout: float) -> FakeResponse:
            captured["headers"] = {
                key.lower(): value for key, value in request.header_items()
            }
            captured["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(
        guard.urllib.request,
        "build_opener",
        lambda *_handlers: FakeOpener(),
    )

    response = guard._urllib_transport(
        "GET",
        "/instances",
        None,
        API_KEY,
        20,
    )

    assert response == {"data": []}
    assert captured["timeout"] == 20
    assert captured["headers"] == {
        "accept": "application/json",
        "authorization": f"Bearer {API_KEY}",
        "user-agent": guard._API_USER_AGENT,
    }


def test_cost_window_uses_exact_decimal_flooring_and_safety_margin() -> None:
    window = guard.compute_cost_window(
        billing_started_at=NOW,
        hourly_rate_usd=Decimal("1.29"),
        max_cost_usd=Decimal("2.58"),
        now=NOW,
    )

    assert window.allowed_seconds == 7_200
    assert window.cost_limit_deadline == datetime(2026, 8, 18, 22, 0, tzinfo=UTC)
    assert window.deadline == datetime(2026, 8, 18, 21, 59, tzinfo=UTC)
    assert window.termination_safety_margin_seconds == 60


def test_cost_window_supports_qwen3_termination_confirmation_reserve() -> None:
    window = guard.compute_cost_window(
        billing_started_at=NOW,
        hourly_rate_usd=Decimal("1.29"),
        max_cost_usd=Decimal("0.75"),
        termination_safety_margin_seconds=300,
        now=NOW,
    )

    assert window.allowed_seconds == 2_093
    assert window.termination_safety_margin_seconds == 300
    assert window.cost_limit_deadline - window.deadline == timedelta(seconds=300)


def test_cost_window_rejects_materially_future_billing_start() -> None:
    with pytest.raises(guard.LambdaGuardError, match="future"):
        guard.compute_cost_window(
            billing_started_at=NOW + timedelta(seconds=31),
            hourly_rate_usd=Decimal("1.29"),
            max_cost_usd=Decimal("2.58"),
            now=NOW,
        )


def test_cost_window_clamps_small_positive_clock_skew() -> None:
    window = guard.compute_cost_window(
        billing_started_at=NOW + timedelta(seconds=30),
        hourly_rate_usd=Decimal("1.29"),
        max_cost_usd=Decimal("2.58"),
        now=NOW,
    )

    assert window.billing_started_at == NOW


def test_termination_is_confirmed_by_provider_absence() -> None:
    transport = FakeTransport(
        [
            {"data": [_instance()]},
            {
                "data": {
                    "terminated_instances": [_instance(status="terminating")],
                }
            },
            {"data": []},
        ]
    )
    sleeps: list[float] = []
    client = guard.LambdaCloudClient(
        API_KEY,
        transport=transport,
        sleeper=sleeps.append,
        monotonic=lambda: 0,
        now=lambda: NOW,
    )

    result = client.terminate_and_wait(INSTANCE_ID)

    assert result.final_status == "absent"
    assert result.request_sent is True
    assert sleeps == [2]
    assert transport.calls[1][:3] == (
        "POST",
        "/instance-operations/terminate",
        {"instance_ids": [INSTANCE_ID]},
    )


def test_failed_termination_request_is_rechecked_before_failure() -> None:
    transport = FakeTransport(
        [
            {"data": [_instance()]},
            guard.LambdaCloudApiError("request failed"),
            {"data": []},
        ]
    )
    client = guard.LambdaCloudClient(
        API_KEY,
        transport=transport,
        sleeper=lambda _seconds: None,
        now=lambda: NOW,
    )

    result = client.terminate_and_wait(INSTANCE_ID)

    assert result.final_status == "absent"
    assert result.request_sent is False


def test_detached_watchdog_keeps_api_key_out_of_command_and_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport([{"data": [_instance()]}])
    client = guard.LambdaCloudClient(API_KEY, transport=transport)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(arguments: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((arguments, kwargs))
        if "--ready-path" in arguments:
            _publish_ready_from_command(arguments)
        return FakeProcess(arguments)

    monkeypatch.setenv(guard.API_KEY_ENVIRONMENT_VARIABLE, API_KEY)
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-enter-watchdog")
    monkeypatch.setattr(guard.platform, "system", lambda: "Linux")
    handle = guard.arm_watchdog(
        "203.0.113.10",
        hourly_rate_usd=Decimal("1.29"),
        max_cost_usd=Decimal("2.58"),
        billing_started_at=NOW,
        state_root=tmp_path / "guards",
        expected_endpoint="203.0.113.10",
        client=client,
        popen=fake_popen,
        now=lambda: NOW,
    )

    assert len(calls) == 1
    watchdog_arguments, watchdog_options = calls[0]
    command = " ".join(watchdog_arguments)
    records = "".join(
        path.read_text(encoding="utf-8")
        for path in handle.state_directory.glob("*.json")
    )
    assert watchdog_arguments[0] == sys.executable
    assert watchdog_options["executable"] == str(
        guard.resolve_executable_identity(sys.executable).path
    )
    assert API_KEY not in command
    assert API_KEY not in records
    assert watchdog_options["env"][guard.API_KEY_ENVIRONMENT_VARIABLE] == API_KEY
    assert "UNRELATED_SECRET" not in watchdog_options["env"]
    assert watchdog_options["env"]["HOME"] == "/nonexistent"
    assert "PATH" not in watchdog_options["env"]
    assert watchdog_options["env"]["TMPDIR"] == "/nonexistent"
    assert watchdog_options["env"]["XDG_CONFIG_HOME"] == "/nonexistent"
    assert watchdog_options["stdout"] == subprocess.DEVNULL
    assert watchdog_options["stderr"] == subprocess.DEVNULL
    assert handle.sleep_inhibitor is None
    assert handle.receipt_path.name == "termination-receipt.json"
    assert handle.ready_path.name == "watchdog-ready.json"
    armed = json.loads(
        (handle.state_directory / "guard-armed.json").read_text(encoding="utf-8")
    )
    assert armed["watchdog_ready"] is True


def test_macos_sleep_inhibitor_uses_a_fixed_credential_free_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport([{"data": [_instance()]}])
    client = guard.LambdaCloudClient(API_KEY, transport=transport)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    resolver_calls: list[str] = []
    inhibitor = tmp_path / "synthetic-caffeinate"
    inhibitor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    inhibitor.chmod(0o700)
    real_resolver = guard.resolve_executable_identity
    inhibitor_identity = real_resolver(str(inhibitor))

    def fake_resolver(executable: str) -> Any:
        resolver_calls.append(executable)
        if executable == "/usr/bin/caffeinate":
            return inhibitor_identity
        return real_resolver(executable)

    def fake_popen(arguments: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((arguments, kwargs))
        if "--ready-path" in arguments:
            _publish_ready_from_command(arguments)
        return FakeProcess(arguments)

    monkeypatch.setenv(guard.API_KEY_ENVIRONMENT_VARIABLE, API_KEY)
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-enter-watchdog")
    monkeypatch.setattr(guard.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(guard, "resolve_executable_identity", fake_resolver)

    handle = guard.arm_watchdog(
        "203.0.113.10",
        hourly_rate_usd=Decimal("1.29"),
        max_cost_usd=Decimal("2.58"),
        billing_started_at=NOW,
        state_root=tmp_path / "guards",
        expected_endpoint="203.0.113.10",
        client=client,
        popen=fake_popen,
        now=lambda: NOW,
    )

    assert len(calls) == 2
    _watchdog_arguments, watchdog_options = calls[0]
    inhibitor_arguments, inhibitor_options = calls[1]
    records = "".join(
        path.read_text(encoding="utf-8")
        for path in handle.state_directory.glob("*.json")
    )
    assert resolver_calls == [sys.executable, "/usr/bin/caffeinate"]
    assert inhibitor_arguments == [str(inhibitor_identity.path), "-w", "4321"]
    assert inhibitor_options["executable"] == str(inhibitor_identity.path)
    assert watchdog_options["env"][guard.API_KEY_ENVIRONMENT_VARIABLE] == API_KEY
    assert guard.API_KEY_ENVIRONMENT_VARIABLE not in inhibitor_options["env"]
    assert "UNRELATED_SECRET" not in watchdog_options["env"]
    assert "UNRELATED_SECRET" not in inhibitor_options["env"]
    assert all(
        API_KEY not in argument
        for arguments, _options in calls
        for argument in arguments
    )
    assert API_KEY not in records
    assert inhibitor_options["stdout"] == subprocess.DEVNULL
    assert inhibitor_options["stderr"] == subprocess.DEVNULL
    assert handle.sleep_inhibitor is not None


def test_watchdog_rejects_missing_provider_rate_before_spawning(
    tmp_path: Path,
) -> None:
    transport = FakeTransport([{"data": [_instance(price_cents_per_hour=None)]}])
    client = guard.LambdaCloudClient(API_KEY, transport=transport)

    with pytest.raises(guard.LambdaGuardError, match="did not report"):
        guard.arm_watchdog(
            INSTANCE_ID,
            hourly_rate_usd=Decimal("0.01"),
            max_cost_usd=Decimal("2.58"),
            billing_started_at=NOW,
            state_root=tmp_path / "guards",
            client=client,
            popen=lambda *_args, **_kwargs: pytest.fail("must not spawn"),
            now=lambda: NOW,
        )


def test_watchdog_rejects_endpoint_mismatch_before_spawning(
    tmp_path: Path,
) -> None:
    transport = FakeTransport([{"data": [_instance()]}])
    client = guard.LambdaCloudClient(API_KEY, transport=transport)

    with pytest.raises(guard.LambdaGuardError, match="SSH destination"):
        guard.arm_watchdog(
            INSTANCE_ID,
            hourly_rate_usd=Decimal("1.29"),
            max_cost_usd=Decimal("2.58"),
            billing_started_at=NOW,
            state_root=tmp_path / "guards",
            expected_endpoint="another-host.example.test",
            client=client,
            popen=lambda *_args, **_kwargs: pytest.fail("must not spawn"),
            now=lambda: NOW,
        )


def test_watchdog_rejects_dead_child_and_removes_false_armed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport([{"data": [_instance()]}])
    client = guard.LambdaCloudClient(API_KEY, transport=transport)
    state_root = tmp_path / "guards"
    monkeypatch.setenv(guard.API_KEY_ENVIRONMENT_VARIABLE, API_KEY)

    with pytest.raises(guard.LambdaGuardError, match="exited before readiness"):
        guard.arm_watchdog(
            INSTANCE_ID,
            hourly_rate_usd=Decimal("1.29"),
            max_cost_usd=Decimal("2.58"),
            billing_started_at=NOW,
            state_root=state_root,
            client=client,
            popen=lambda arguments, **_kwargs: FakeProcess(
                arguments,
                exit_code=2,
            ),
            now=lambda: NOW,
        )

    assert list(state_root.iterdir()) == []


def test_watchdog_spawn_failure_removes_false_armed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport([{"data": [_instance()]}])
    client = guard.LambdaCloudClient(API_KEY, transport=transport)
    state_root = tmp_path / "guards"
    monkeypatch.setenv(guard.API_KEY_ENVIRONMENT_VARIABLE, API_KEY)

    def fail_spawn(*_args: object, **_kwargs: object) -> FakeProcess:
        raise OSError("spawn failed")

    with pytest.raises(guard.LambdaGuardError, match="could not start"):
        guard.arm_watchdog(
            INSTANCE_ID,
            hourly_rate_usd=Decimal("1.29"),
            max_cost_usd=Decimal("2.58"),
            billing_started_at=NOW,
            state_root=state_root,
            client=client,
            popen=fail_spawn,
            now=lambda: NOW,
        )

    assert list(state_root.iterdir()) == []


def test_watchdog_rejects_rate_drift_before_spawning(tmp_path: Path) -> None:
    transport = FakeTransport([{"data": [_instance(price_cents_per_hour=150)]}])
    client = guard.LambdaCloudClient(API_KEY, transport=transport)

    with pytest.raises(guard.LambdaGuardError, match="hourly rate"):
        guard.arm_watchdog(
            INSTANCE_ID,
            hourly_rate_usd=Decimal("1.29"),
            max_cost_usd=Decimal("2.58"),
            billing_started_at=NOW,
            state_root=tmp_path / "guards",
            client=client,
            popen=lambda *_args, **_kwargs: pytest.fail("must not spawn"),
            now=lambda: NOW,
        )


def test_exhausted_cost_window_terminates_before_watchdog_spawn(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        [
            {"data": [_instance()]},
            {"data": [_instance()]},
            {
                "data": {
                    "terminated_instances": [_instance(status="terminating")],
                }
            },
            {"data": []},
        ]
    )
    client = guard.LambdaCloudClient(
        API_KEY,
        transport=transport,
        sleeper=lambda _seconds: None,
        now=lambda: NOW,
    )

    with pytest.raises(guard.LambdaGuardError, match="termination confirmed"):
        guard.arm_watchdog(
            INSTANCE_ID,
            hourly_rate_usd=Decimal("1.29"),
            max_cost_usd=Decimal("2.58"),
            billing_started_at=NOW - timedelta(hours=2),
            state_root=tmp_path / "guards",
            client=client,
            popen=lambda *_args, **_kwargs: pytest.fail("must not spawn"),
            now=lambda: NOW,
        )

    assert transport.calls[2][:3] == (
        "POST",
        "/instance-operations/terminate",
        {"instance_ids": [INSTANCE_ID]},
    )


def test_deadline_watch_writes_operational_termination_receipt(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        [
            {"data": [_instance()]},
            {
                "data": {
                    "terminated_instances": [_instance(status="terminating")],
                }
            },
            {"data": []},
        ]
    )
    client = guard.LambdaCloudClient(
        API_KEY,
        transport=transport,
        sleeper=lambda _seconds: None,
        monotonic=lambda: 0,
        now=lambda: NOW,
    )
    window = guard.compute_cost_window(
        billing_started_at=NOW,
        hourly_rate_usd=Decimal("1.29"),
        max_cost_usd=Decimal("0.001"),
        now=NOW,
    )
    receipt = tmp_path / "termination-receipt.json"
    ready = tmp_path / "watchdog-ready.json"

    result = guard.watch_until_deadline(
        INSTANCE_ID,
        cost_window=window,
        receipt_path=receipt,
        client=client,
        poll_seconds=2,
        termination_timeout_seconds=300,
        retry_window_seconds=1_800,
        ready_path=ready,
        readiness_token="b" * 32,
        sleeper=lambda _seconds: None,
        now=lambda: window.deadline,
        monotonic=lambda: 0,
    )

    value = json.loads(receipt.read_text(encoding="utf-8"))
    assert result.final_status == "absent"
    assert value["trigger"] == "cost-deadline"
    assert value["schema_version"] == "inferdrome.lambda-termination-receipt.v2"
    assert value["record_kind"] == "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
    assert API_KEY not in receipt.read_text(encoding="utf-8")
    ready_value = json.loads(ready.read_text(encoding="utf-8"))
    assert ready_value["instance_id"] == INSTANCE_ID
    assert ready_value["readiness_token"] == "b" * 32


def test_deadline_watch_wall_clock_rollback_cannot_extend_deadline(
    tmp_path: Path,
) -> None:
    window = guard.CostWindow(
        billing_started_at=NOW,
        deadline=NOW + timedelta(seconds=5),
        cost_limit_deadline=NOW + timedelta(seconds=65),
        allowed_seconds=65,
        termination_safety_margin_seconds=60,
        hourly_rate_usd=Decimal("1.29"),
        max_cost_usd=Decimal("0.03"),
    )
    elapsed = 0.0
    sleeps: list[float] = []
    wall_calls = 0

    def wall_now() -> datetime:
        nonlocal wall_calls
        wall_calls += 1
        return NOW if wall_calls == 1 else NOW - timedelta(hours=1)

    def monotonic() -> float:
        return elapsed

    def sleeper(seconds: float) -> None:
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds

    result = guard.TerminationResult(
        instance_id=INSTANCE_ID,
        final_status="absent",
        request_sent=True,
        confirmed_at=NOW + timedelta(seconds=5),
    )
    client = SimpleNamespace(
        terminate_and_wait=lambda *_args, **_kwargs: result,
    )

    observed = guard.watch_until_deadline(
        INSTANCE_ID,
        cost_window=window,
        receipt_path=tmp_path / "termination-receipt.json",
        client=client,
        poll_seconds=2,
        termination_timeout_seconds=300,
        retry_window_seconds=1_800,
        sleeper=sleeper,
        now=wall_now,
        monotonic=monotonic,
    )

    assert observed == result
    assert sleeps == [5.0]


def test_concurrent_termination_receipt_first_writer_wins(tmp_path: Path) -> None:
    window = guard.compute_cost_window(
        billing_started_at=NOW,
        hourly_rate_usd=Decimal("1.29"),
        max_cost_usd=Decimal("2.58"),
        now=NOW,
    )
    result = guard.TerminationResult(
        instance_id=INSTANCE_ID,
        final_status="absent",
        request_sent=True,
        confirmed_at=NOW,
    )
    receipt = tmp_path / "termination-receipt.json"

    guard._write_termination_receipt(
        receipt,
        result=result,
        trigger="cost-deadline",
        cost_window=window,
    )
    guard._write_termination_receipt(
        receipt,
        result=result,
        trigger="controller-finally",
        cost_window=window,
    )

    value = json.loads(receipt.read_text(encoding="utf-8"))
    assert value["trigger"] == "cost-deadline"


def test_confirmed_termination_without_inhibitor_stops_watchdog_on_receipt_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = guard.TerminationResult(
        instance_id=INSTANCE_ID,
        final_status="absent",
        request_sent=True,
        confirmed_at=NOW,
    )
    process = FakeProcess([])
    stopped: list[FakeProcess] = []
    handle = SimpleNamespace(
        client=SimpleNamespace(terminate_and_wait=lambda _instance_id: result),
        cost_window=object(),
        instance=SimpleNamespace(instance_id=INSTANCE_ID),
        process=process,
        receipt_path=Path("/unused/termination-receipt.json"),
        sleep_inhibitor=None,
    )
    monkeypatch.setattr(
        guard,
        "_write_termination_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            guard.LambdaGuardError("receipt failed")
        ),
    )
    monkeypatch.setattr(guard, "_stop_watchdog", stopped.append)

    with pytest.raises(
        guard.LambdaGuardFinalizationError,
        match="termination was confirmed",
    ):
        guard.terminate_guarded_instance(handle)

    assert stopped == [process]


def test_unconfirmed_provider_termination_keeps_watchdog_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess([])
    handle = SimpleNamespace(
        client=SimpleNamespace(
            terminate_and_wait=lambda _instance_id: (_ for _ in ()).throw(
                guard.LambdaGuardError("synthetic provider failure")
            )
        ),
        instance=SimpleNamespace(instance_id=INSTANCE_ID),
        process=process,
    )
    stopped: list[FakeProcess] = []
    monkeypatch.setattr(guard, "_stop_watchdog", stopped.append)

    with pytest.raises(guard.LambdaGuardError, match="synthetic provider failure"):
        guard.terminate_guarded_instance(handle)

    assert stopped == []


def test_watchdog_shutdown_treats_process_lookup_race_as_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess([])

    def missing_process(_pid: int, _signal: int) -> None:
        raise ProcessLookupError(errno.ESRCH, "process is gone")

    monkeypatch.setattr(guard.os, "killpg", missing_process)

    guard._stop_watchdog(process)
