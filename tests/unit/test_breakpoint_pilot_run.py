"""Offline admission and owned-process boundaries for the pilot collector."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from inferdrome import breakpoint_pilot as preparation
from inferdrome import breakpoint_pilot_guard as guard
from inferdrome import breakpoint_pilot_run as pilot


def _billing(**updates: Any) -> dict[str, Any]:
    return {
        "billing_start_utc": (datetime.now(UTC) - timedelta(seconds=60)).isoformat(),
        "hourly_rate_usd": "2",
        "cap_usd": "9",
        "reserve_usd": "1",
        "max_session_s": 12600,
        "cleanup_s": 600 + pilot.EXPORT_RESERVE_S,
        **updates,
    }


def test_billing_origin_includes_setup_and_separates_guard_and_export() -> None:
    before = time.monotonic()
    deadline = pilot.billing_deadline(**_billing())
    remaining = deadline.stop_at - before
    assert 11339 < remaining < 11341
    # 3h30 - 10min provider cleanup - 10min local export - 1min elapsed.
    assert pilot.EXPORT_RESERVE_S == 600


@pytest.mark.parametrize(
    "updates",
    [
        {"hourly_rate_usd": "NaN"},
        {"cap_usd": "Infinity"},
        {"reserve_usd": "9"},
        {"max_session_s": True},
        {"max_session_s": 12601},
        {"cleanup_s": 12600},
        {"billing_start_utc": "2025-01-01T00:00:00"},
        {"billing_start_utc": "2999-01-01T00:00:00Z"},
    ],
)
def test_bad_billing_is_rejected(updates: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        pilot.billing_deadline(**_billing(**updates))


def test_expired_billing_and_insufficient_next_trial_fail_closed() -> None:
    with pytest.raises(pilot.gpu.StudyError, match="deadline"):
        pilot.billing_deadline(**_billing(billing_start_utc="2020-01-01T00:00:00Z"))
    deadline = pilot.Deadline(time.monotonic() + 1)
    with pytest.raises(pilot.gpu.StudyError, match="deadline"):
        deadline.require(2)


def test_source_revision_and_dirty_checkout_rejected_before_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path(pilot.__file__).resolve().parents[2]
    monkeypatch.setattr(pilot, "_command", lambda *args, **kwargs: "b" * 40)
    with pytest.raises(pilot.gpu.StudyError, match="revision"):
        pilot.verify_source(source, "a" * 40)
    answers = iter(["a" * 40, " M source.py"])
    monkeypatch.setattr(pilot, "_command", lambda *args, **kwargs: next(answers))
    with pytest.raises(pilot.gpu.StudyError, match="clean"):
        pilot.verify_source(source, "a" * 40)


def test_engine_environment_is_separate_and_partial_startup_cleans_owned_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/collector/overlay")
    monkeypatch.setenv("VIRTUAL_ENV", "/collector/env")
    monkeypatch.setenv("PYTHONHOME", "/collector/python")
    starts: list[dict[str, Any]] = []
    owned = object()
    cleaned: list[list[object]] = []

    def start(argv: list[str], **kwargs: Any) -> object:
        starts.append({"argv": argv, **kwargs})
        if len(starts) == 2:
            raise OSError("second replica refused to start")
        return owned

    def stop(processes: list[object], logs: list[Any]) -> None:
        cleaned.append(list(processes))
        for log in logs:
            log.close()

    monkeypatch.setattr(pilot.subprocess, "Popen", start)
    monkeypatch.setattr(pilot.gpu, "_stop_engines", stop)
    with pytest.raises(OSError, match="second replica"):
        pilot._start_engines(Path("/engine/bin/vllm"), tmp_path, tmp_path, 8192)
    assert cleaned == [[owned]]
    assert starts[0]["start_new_session"] is True
    for index, started in enumerate(starts):
        env = started["env"]
        assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} & env.keys()
        assert env["CUDA_VISIBLE_DEVICES"] == str(index)
        assert env["HF_HUB_OFFLINE"] == env["VLLM_SERVER_DEV_MODE"] == "1"
        assert env["PATH"].startswith("/engine/bin:")


def test_saving_is_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    pilot._save(path, {"status": "first"})
    with pytest.raises(FileExistsError):
        pilot._save(path, {"status": "replacement"})
    assert json.loads(path.read_text()) == {"status": "first"}


def test_failed_startup_cleanup_does_not_lose_owned_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = object()
    processes: list[Any] = []
    logs: list[Any] = []

    def start(*args: Any, **kwargs: Any) -> object:
        if processes:
            raise OSError("second start")
        return owned

    def fail_cleanup(*args: Any) -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(pilot.subprocess, "Popen", start)
    monkeypatch.setattr(pilot.gpu, "_stop_engines", fail_cleanup)
    try:
        with pytest.raises(pilot.gpu.StudyError, match="cleanup failed"):
            pilot._start_engines(
                Path("/engine/vllm"),
                tmp_path,
                tmp_path,
                8192,
                processes=processes,
                logs=logs,
            )
        assert processes == [owned]  # outer finally can retry/mark unconfirmed
        assert len(logs) == 2
    finally:
        for log in logs:
            log.close()


def test_cleanup_attempts_second_owned_group_after_first_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = object(), object()
    attempted: list[object] = []

    def stop(processes: list[object], logs: list[Any]) -> None:
        attempted.extend(processes)
        if processes == [first]:
            raise RuntimeError("first group refused cleanup")

    monkeypatch.setattr(pilot.gpu, "_stop_engines", stop)
    with pytest.raises(pilot.gpu.StudyError, match="first group"):
        pilot._stop_owned([first, second], [])
    assert attempted == [first, second]


def test_readiness_bounds_unresponsive_engine(tmp_path: Path) -> None:
    # Deadline rejects before any HTTP or external process is contacted.
    with pytest.raises(pilot.gpu.StudyError, match="deadline"):
        asyncio.run(pilot._ready([], pilot.Deadline(time.monotonic() - 1), "model"))


def test_transient_active_metrics_settle_with_all_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            {
                "status": 200,
                "body": "vllm:num_requests_running 1\nvllm:num_requests_waiting 0\n",
            },
            {
                "status": 200,
                "body": "vllm:num_requests_running 0\nvllm:num_requests_waiting 0\n",
            },
        ]
    )

    async def response(*args: object) -> dict[str, Any]:
        return next(responses)

    monkeypatch.setattr(pilot, "_response", response)
    retained: dict[str, Any] = {}
    result = asyncio.run(pilot._quiescent(None, 1, retained, "after_reset"))
    assert len(result["observations"]) == 2
    assert result["metrics"]["vllm:num_requests_running"] == 0
    assert retained["after_reset"] == result


def test_preflight_commands_have_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def command(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "0.26.0\n", "")

    monkeypatch.setattr(pilot.subprocess, "run", command)
    assert pilot._command(["vllm", "--version"]) == "0.26.0"
    assert seen["timeout"] == 30 and seen["check"] is True


def _mock_run_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stale: bool = False,
    mismatch: bool = False,
) -> argparse.Namespace:
    now = datetime.now(UTC).replace(microsecond=0)

    def stamp(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    approval = {
        "schema": "inferdrome.breakpoint-pilot-approval.v1",
        "execution_authorized": True,
        "provider": "vast",
        "instance_id": "123",
        "billing_start_utc": stamp(now - timedelta(seconds=300)),
        "max_session_s": 12600,
        "cleanup_reserve_s": 600,
        "hourly_rate_usd": "2",
        "cap_usd": "9",
        "reserve_usd": "1",
        "storage_allowance_usd": "0",
        "transfer_allowance_usd": "0",
        "plan_sha256": "sha256:" + ("c" if mismatch else "b") * 64,
        "source_revision": "a" * 40,
        "image_reference": pilot.gpu.IMAGE,
        "guardian_host_kind": "EXTERNAL_OPERATOR_HOST",
    }
    cleanup, hard = guard.deadlines(approval)
    receipt = {
        "schema": "inferdrome.breakpoint-pilot-guard-ready.v1",
        "status": "ARMED",
        "approval_sha256": guard.approval_sha256(approval),
        "instance_id_sha256": "sha256:" + hashlib.sha256(b"123").hexdigest(),
        "armed_at_utc": approval["billing_start_utc"],
        "observed_at_utc": stamp(now - timedelta(seconds=100 if stale else 0)),
        "cleanup_start_utc": stamp(cleanup),
        "hard_deadline_utc": stamp(hard),
        "guardian_pid": 999,
        "external_host_declaration": "EXTERNAL_OPERATOR_HOST",
    }
    bundle = {
        "plan": {
            "source_revision": "a" * 40,
            "plan_sha256": "sha256:" + "b" * 64,
            "config": {
                "pins": {"image_reference": pilot.gpu.IMAGE},
                "runtime": {
                    "max_session_s": 12600,
                    "teardown_reserve_s": 600,
                    "drain_allowance_s_per_trial": 60,
                    "warm_reset_setup_allowance_s_per_trial": 60,
                },
                "workload": {"duration_s": 120, "context_length": 8192},
            },
        }
    }
    monkeypatch.setattr(preparation, "verify", lambda *args: {})
    monkeypatch.setattr(preparation, "load_prepared", lambda *args: bundle)
    monkeypatch.setattr(pilot, "verify_source", lambda *args: None)
    monkeypatch.setattr(pilot.gpu, "_snapshot", lambda *args: {"sha256": "fixture"})
    monkeypatch.setattr(pilot, "_observed_gpus", lambda: [])
    monkeypatch.setattr(pilot.gpu, "_ports_closed", lambda: True)
    monkeypatch.setattr(
        pilot,
        "_command",
        lambda args: (
            "0.26.0"
            if args[-1] == "--version"
            else "--enable-prefix-caching --no-enable-log-requests --max-model-len"
        ),
    )
    (tmp_path / "approval.json").write_text(json.dumps(approval))
    (tmp_path / "guardian.json").write_text(json.dumps(receipt))
    return argparse.Namespace(
        prepared_dir=tmp_path,
        tokenizer_dir=tmp_path,
        source_dir=tmp_path,
        model_dir=tmp_path,
        vllm_executable=tmp_path / "vllm",
        output_dir=tmp_path / "output",
        approval=tmp_path / "approval.json",
        guard_receipt=tmp_path / "guardian.json",
    )


@pytest.mark.parametrize("stale,mismatch", [(True, False), (False, True)])
def test_bad_approval_or_stale_guard_cannot_launch_engines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stale: bool,
    mismatch: bool,
) -> None:
    args = _mock_run_inputs(tmp_path, monkeypatch, stale=stale, mismatch=mismatch)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("engine launch forbidden")

    monkeypatch.setattr(pilot, "_start_engines", forbidden)
    with pytest.raises((guard.GuardError, pilot.gpu.StudyError)):
        asyncio.run(pilot.run(args))
    if stale:
        terminal = json.loads((args.output_dir / "session-status.json").read_text())
        assert terminal["status"] == "FAILED"
        assert terminal["local_cleanup"] == "COMPLETED"
        assert "stale" in terminal["error"]


def test_run_does_not_claim_cleanup_after_partial_startup_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _mock_run_inputs(tmp_path, monkeypatch)

    def start(*args: Any, **kwargs: Any) -> None:
        kwargs["processes"].append(object())
        raise OSError("second engine failed")

    def cleanup(*args: Any) -> None:
        raise RuntimeError("owned process could not be stopped")

    monkeypatch.setattr(pilot, "_start_engines", start)
    monkeypatch.setattr(pilot.gpu, "_stop_engines", cleanup)
    with pytest.raises(pilot.gpu.StudyError, match="cleanup failed"):
        asyncio.run(pilot.run(args))
    terminal = json.loads((args.output_dir / "session-status.json").read_text())
    assert terminal["status"] == "CLEANUP_UNCONFIRMED"
    assert "could not be stopped" in terminal["local_cleanup"]


@pytest.mark.parametrize(
    "received_signal", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]
)
@pytest.mark.parametrize("stage", ["preflight", "startup", "collection"])
def test_shutdown_signal_cleans_owned_engines_and_restores_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    received_signal: signal.Signals,
    stage: str,
) -> None:
    from inferdrome import vllm_router_capacity

    args = _mock_run_inputs(tmp_path, monkeypatch)
    bundle = preparation.load_prepared(args.prepared_dir)
    bundle.update(protocol={"model": "fixture"}, plans=[{"workload": {}}])
    owned = [object(), object()]
    stopped: list[object] = []

    def previous_handler(signum: int, frame: object) -> None:
        raise AssertionError("collector did not handle the shutdown signal")

    old_handlers = {
        signum: signal.signal(signum, previous_handler)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }

    def start(*unused: Any, **kwargs: Any) -> tuple[list[Any], list[Any]]:
        assert stage != "preflight", "cancelled preflight must not launch engines"
        kwargs["processes"].extend(owned)
        return kwargs["processes"], kwargs["logs"]

    def send_signal() -> None:
        # Check before sending so a regression cannot terminate the test runner.
        assert signal.getsignal(received_signal) is not previous_handler
        os.kill(os.getpid(), received_signal)

    async def interrupt() -> None:
        send_signal()
        await asyncio.sleep(1)
        raise AssertionError("shutdown signal did not cancel the collector")

    def snapshot(*unused: Any) -> dict[str, Any]:
        if stage == "preflight":
            send_signal()
        return {"sha256": "fixture"}

    def stop(processes: list[Any], logs: list[Any]) -> None:
        stopped.extend(processes)
        # Repeated Ctrl-C during synchronous cleanup must not abandon replica 2.
        if received_signal == signal.SIGINT:
            send_signal()
            send_signal()

    async def ready(*unused: Any) -> None:
        if stage == "startup":
            await interrupt()

    async def collect(*unused: Any, **kwargs: Any) -> dict[str, Any]:
        await interrupt()
        return {}  # unreachable

    monkeypatch.setattr(pilot, "_start_engines", start)
    monkeypatch.setattr(pilot.gpu, "_snapshot", snapshot)
    monkeypatch.setattr(pilot, "_ready", ready)
    monkeypatch.setattr(pilot, "collect", collect)
    monkeypatch.setattr(pilot.gpu, "_stop_engines", stop)
    monkeypatch.setattr(
        vllm_router_capacity,
        "_cache_capacity",
        lambda *unused: {"candidate_valid": True},
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(pilot.run(args))
        assert stopped == ([] if stage == "preflight" else owned)
        terminal = json.loads((args.output_dir / "session-status.json").read_text())
        assert terminal["status"] == "INTERRUPTED"
        assert terminal["local_cleanup"] == "COMPLETED"
        for signum in old_handlers:
            assert signal.getsignal(signum) is previous_handler
    finally:
        for signum, old_handler in old_handlers.items():
            signal.signal(signum, old_handler)


def test_log_digest_failure_still_writes_terminal_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _mock_run_inputs(tmp_path, monkeypatch)

    def start(*args: Any, **kwargs: Any) -> None:
        (tmp_path / "output/engine-0.log").write_text("partial startup log")
        raise OSError("engine startup failed")

    def digest(path: Path) -> str:
        raise OSError("log cannot be read")

    monkeypatch.setattr(pilot, "_start_engines", start)
    monkeypatch.setattr(pilot.gpu, "_file_digest", digest)
    with pytest.raises(OSError, match="startup failed"):
        asyncio.run(pilot.run(args))
    terminal = json.loads((args.output_dir / "session-status.json").read_text())
    assert terminal["status"] == "FAILED"
    assert terminal["engine_log_errors"] == {"engine-0.log": "log cannot be read"}


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
@pytest.mark.parametrize("hash_error_type", [None, OSError, pilot.gpu.StudyError])
def test_completed_collection_propagates_log_integrity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
    hash_error_type: type[Exception] | None,
) -> None:
    from inferdrome import vllm_router_capacity

    args = _mock_run_inputs(tmp_path, monkeypatch)
    hash_failure = hash_error_type is not None
    bundle = preparation.load_prepared(args.prepared_dir)
    bundle.update(protocol={"model": "fixture"}, plans=[{"workload": {}}])
    lifecycle: list[str] = []
    original_digest = pilot.gpu._file_digest

    def start(*unused: Any, **kwargs: Any) -> tuple[list[Any], list[Any]]:
        for index in range(2):
            (args.output_dir / f"engine-{index}.log").write_text("fixture log")
        return kwargs["processes"], kwargs["logs"]

    async def ready(*unused: Any) -> None:
        return None

    async def collect(*unused: Any, **kwargs: Any) -> dict[str, Any]:
        lifecycle.append("collected")
        return {"supplied_trials": 32}

    def digest(path: Path) -> str:
        assert lifecycle == ["collected", "cleaned"]
        if hash_error_type is not None and path.name == "engine-0.log":
            raise hash_error_type("log cannot be read")
        return original_digest(path)

    monkeypatch.setattr(pilot, "_start_engines", start)
    monkeypatch.setattr(pilot, "_ready", ready)
    monkeypatch.setattr(pilot, "collect", collect)
    monkeypatch.setattr(
        pilot, "_stop_owned", lambda *unused: lifecycle.append("cleaned")
    )
    monkeypatch.setattr(pilot.gpu, "_file_digest", digest)
    monkeypatch.setattr(
        vllm_router_capacity,
        "_cache_capacity",
        lambda *unused: {"candidate_valid": True},
    )

    if entrypoint == "api":
        if hash_failure:
            with pytest.raises(pilot.gpu.StudyError, match="engine log hashing failed"):
                asyncio.run(pilot.run(args))
        else:
            assert asyncio.run(pilot.run(args)) == {"supplied_trials": 32}
    else:
        argv = ["run"]
        for name, value in vars(args).items():
            argv.extend(["--" + name.replace("_", "-"), str(value)])
        assert pilot.main(argv) == (2 if hash_failure else 0)
        if hash_failure:
            assert "engine log hashing failed" in capsys.readouterr().err

    terminal = json.loads((args.output_dir / "session-status.json").read_text())
    assert terminal["status"] == ("FAILED" if hash_failure else "COMPLETED")
    assert terminal["local_cleanup"] == "COMPLETED"
    assert terminal["supplied_trials"] == 32
    assert lifecycle == ["collected", "cleaned"]
    if hash_failure:
        assert "engine log hashing failed" in terminal["error"]
        assert terminal["engine_log_errors"] == {"engine-0.log": "log cannot be read"}
        assert set(terminal["engine_logs"]) == {"engine-1.log"}
    else:
        assert terminal["error"] is None
        assert terminal["engine_log_errors"] == {}
        assert set(terminal["engine_logs"]) == {"engine-0.log", "engine-1.log"}
