"""Real HTTP/router/client loopback, with explicitly synthetic SSE engines."""

from __future__ import annotations

import asyncio
import json
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from inferdrome import breakpoint_pilot_run as pilot
from inferdrome.vllm_paired_comparison import _load_inputs, compare


def test_full_loopback_replays_32_trials_and_cleans_up(tmp_path: Path) -> None:
    output = tmp_path / "loopback"
    report = asyncio.run(pilot.rehearse(output))
    protocol = json.loads((output / "protocol.json").read_text())
    plans, inputs = _load_inputs(output / "inputs.json", protocol)
    assert report == compare(protocol, plans, inputs)
    assert report["supplied_trials"] == report["planned_trials"] == 32
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert report["evidence_eligible"] is False
    assert report["status"] == "INCONCLUSIVE"
    assert report["ineligibility_reasons"] == []
    assert [item["trial_id"] for item in inputs] == [
        trial["trial_id"] for trial in protocol["trials"]
    ]
    assert len(list(output.glob("*-reset.json"))) == 32
    assert len(list(output.glob("*-ledger.jsonl"))) == 32
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for trial in protocol["trials"]:
        receipt = json.loads((output / f"{trial['trial_id']}-reset.json").read_text())
        assert receipt["status"] == "COMPLETED"
        assert len(receipt["replicas"]) == 2
        assert all(
            replica["reset_response"] == {"status": 200, "body": '{"success": true}'}
            for replica in receipt["replicas"]
        )
    status = json.loads((output / "session-status.json").read_text())
    assert status["local_cleanup"] == status["status"] == "COMPLETED"
    before = (output / "session-status.json").read_bytes()
    with pytest.raises(FileExistsError):
        asyncio.run(pilot.rehearse(output))
    assert (output / "session-status.json").read_bytes() == before


@pytest.mark.parametrize("success", [False, 1, "true", None])
def test_failed_or_nonboolean_reset_retains_receipt_and_stops(
    tmp_path: Path,
    success: object,
) -> None:
    output = tmp_path / "failed"
    with pytest.raises(pilot.gpu.StudyError, match="exact success"):
        asyncio.run(pilot.rehearse(output, reset_success=success))
    receipts = list(output.glob("*-reset.json"))
    assert len(receipts) == 1
    record = json.loads(receipts[0].read_text())
    assert record["status"] == "INCOMPLETE"
    assert json.loads(record["replicas"][0]["reset_response"]["body"]) == {
        "success": success
    }
    assert not list(output.glob("*-result.json"))
    assert json.loads((output / "inputs.json").read_text())["trials"] == []
    status = json.loads((output / "session-status.json").read_text())
    assert status["status"] == "FAILED" and status["local_cleanup"] == "COMPLETED"


@pytest.mark.parametrize(
    "metrics",
    [
        "vllm:num_requests_running 1\nvllm:num_requests_waiting 0\n",
        "vllm:num_requests_running 0\n",
        "vllm:num_requests_running 1e\nvllm:num_requests_waiting 0\n",
        "vllm:num_requests_running 1e999\nvllm:num_requests_waiting 0\n",
    ],
)
def test_failed_metrics_are_retained_before_abort(tmp_path: Path, metrics: str) -> None:
    output = tmp_path / "metrics"
    with pytest.raises((pilot.gpu.StudyError, ValueError, TimeoutError)):
        asyncio.run(pilot.rehearse(output, metrics_text=metrics))
    record = json.loads(next(output.glob("*-reset.json")).read_text())
    assert record["replicas"][0]["before_reset"]["body"] == metrics
    assert "reset_response" not in record["replicas"][0]
    assert (
        json.loads((output / "session-status.json").read_text())["local_cleanup"]
        == "COMPLETED"
    )


def test_early_client_return_holds_full_offered_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def instant(*args: object, **kwargs: object) -> dict[str, Any]:
        assert kwargs["correlate_requests"] is True and kwargs["timing"] is not None
        return {
            "measurement": {
                "started_unix_ns": str(time.time_ns()),
                "status": "COMPLETED",
            }
        }

    monkeypatch.setattr(pilot, "run_trial", instant)
    bundle = pilot._fixture_bundle()
    started = time.monotonic()
    asyncio.run(
        pilot._trial(
            bundle,
            bundle["protocol"]["trials"][0],
            tmp_path,
            (pilot._free_port(), pilot._free_port()),
            pilot._free_port(),
            pilot.Deadline(time.monotonic() + 10),
        )
    )
    assert time.monotonic() - started >= 0.1


def test_expired_deadline_retains_partial_and_no_traffic(tmp_path: Path) -> None:
    bundle = pilot._fixture_bundle()
    with pytest.raises(pilot.gpu.StudyError, match="deadline"):
        asyncio.run(
            pilot.collect(
                bundle,
                tmp_path,
                ports=(1, 2),
                router_port=3,
                deadline=pilot.Deadline(time.monotonic() + 0.01),
            )
        )
    assert json.loads((tmp_path / "inputs.json").read_text())["trials"] == []
    assert not list(tmp_path.glob("*-reset.json"))


def test_control_replies_read_all_chunks_before_parsing() -> None:
    async def exercise() -> None:
        async def handler(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(b'{"success":')
            await asyncio.sleep(0.01)
            await response.write(b"true}")
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post("/reset_prefix_cache", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        port = pilot._free_port()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        try:
            async with aiohttp.ClientSession(trust_env=False) as session:
                result = await pilot._response(
                    session, "POST", f"http://127.0.0.1:{port}/reset_prefix_cache"
                )
            assert result == {"status": 200, "body": '{"success":true}'}
        finally:
            await runner.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize("reject_window_wait", [False, True])
def test_interrupted_client_retains_result_and_closes_router(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reject_window_wait: bool,
) -> None:
    original = pilot.run_trial

    async def interrupted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        current = asyncio.current_task()
        assert current is not None
        asyncio.get_running_loop().call_later(0.02, current.cancel)
        result = await original(*args, **kwargs)
        if reject_window_wait:

            async def unexpected_sleep(delay: float) -> None:
                pytest.fail("interrupted trial waited out its offered window")

            # Scope these overrides to the collector after the real client has
            # retained its interruption. A fixed wall clock ensures a pre-fix
            # implementation requests a wait even on a slow test machine.
            monkeypatch.setattr(
                pilot,
                "asyncio",
                SimpleNamespace(**{**vars(asyncio), "sleep": unexpected_sleep}),
            )
            monkeypatch.setattr(
                pilot,
                "time",
                SimpleNamespace(
                    **{
                        **vars(time),
                        "time_ns": lambda: int(
                            result["measurement"]["started_unix_ns"]
                        ),
                    }
                ),
            )
        return result

    monkeypatch.setattr(pilot, "run_trial", interrupted)
    output = tmp_path / "interrupted"
    with pytest.raises(pilot.gpu.TrialInterrupted):
        asyncio.run(pilot.rehearse(output))
    result = json.loads(next(output.glob("*-result.json")).read_text())
    assert result["measurement"]["status"] == "INTERRUPTED"
    status = json.loads((output / "session-status.json").read_text())
    assert status["status"] == "INTERRUPTED"
    assert status["local_cleanup"] == "COMPLETED"
    assert len(list(output.glob("*-reset.json"))) == 1
    protocol = json.loads((output / "protocol.json").read_text())
    plans, inputs = _load_inputs(output / "inputs.json", protocol)
    assert len(inputs) == 1
    assert inputs[0]["trial_id"] == protocol["trials"][0]["trial_id"]
    assert inputs[0]["result"] == result
    report = compare(protocol, plans, inputs)
    assert report["status"] == "INELIGIBLE"
    reasons = report["ineligibility_reasons"]
    assert {
        "trial_id": inputs[0]["trial_id"],
        "reason": "MEASUREMENT_NOT_COMPARISON_VALID",
    } in reasons
    assert sum(item["reason"] == "MISSING_TRIAL" for item in reasons) == 31


def test_deadline_after_saved_measurement_retains_replay_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "deadline-after-result"
    original = pilot.Deadline.require

    def expire_after_save(self: pilot.Deadline, seconds: float = 0) -> None:
        if list(output.glob("*-result.json")):
            raise pilot.gpu.StudyError("injected deadline after measurement save")
        original(self, seconds)

    monkeypatch.setattr(pilot.Deadline, "require", expire_after_save)
    with pytest.raises(pilot.gpu.StudyError, match="deadline after measurement"):
        asyncio.run(pilot.rehearse(output))
    protocol = json.loads((output / "protocol.json").read_text())
    plans, inputs = _load_inputs(output / "inputs.json", protocol)
    assert len(inputs) == 1
    assert inputs[0]["trial_id"] == protocol["trials"][0]["trial_id"]
    assert inputs[0]["result"]["measurement"]["status"] == "COMPLETED"
    report = compare(protocol, plans, inputs)
    assert report["status"] == "INELIGIBLE"
    assert (
        sum(
            item["reason"] == "MISSING_TRIAL"
            for item in report["ineligibility_reasons"]
        )
        == 31
    )
    assert len(list(output.glob("*-reset.json"))) == 1
    status = json.loads((output / "session-status.json").read_text())
    assert status["status"] == "FAILED"
    assert status["local_cleanup"] == "COMPLETED"
