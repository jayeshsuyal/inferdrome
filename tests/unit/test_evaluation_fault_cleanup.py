"""Final cancellation and failed-close ownership for the combined routing trial."""

from __future__ import annotations

import asyncio

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.faults import run_routing_fault
from tests.unit.test_evaluation_faults import clients, config, finish
from tests.unit.test_evaluation_runner import ManualClock, settle


async def _assert_no_owned_tasks(clock: ManualClock) -> None:
    await settle()
    assert clock.waiters == {}
    assert asyncio.all_tasks() == {asyncio.current_task()}


@pytest.mark.parametrize("client_index", range(4))
def test_external_stop_during_client_close_remains_cancelled(
    client_index: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        stop = asyncio.Event()
        original_close = owned[client_index].close
        close_calls = 0

        async def close_and_stop() -> None:
            nonlocal close_calls
            close_calls += 1
            stop.set()
            await original_close()

        monkeypatch.setattr(owned[client_index], "close", close_and_stop)
        running = asyncio.create_task(
            run_routing_fault(config(), *owned, clock=clock, stop=stop)
        )
        await finish(clock)
        result = await asyncio.wait_for(running, 2)
        assert stop.is_set()
        assert result.status == "CANCELLED"
        assert close_calls == 1
        assert all(client.closed and client.active == 0 for client in owned)
        await _assert_no_owned_tasks(clock)

    asyncio.run(scenario())


@pytest.mark.parametrize("client_index", [2, 3])
def test_task_cancellation_during_probe_close_waits_for_owned_close(
    client_index: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        original_close = owned[client_index].close
        closing = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()
        close_calls = 0

        async def gated_close() -> None:
            nonlocal close_calls
            close_calls += 1
            closing.set()
            await release.wait()
            await original_close()
            finished.set()

        monkeypatch.setattr(owned[client_index], "close", gated_close)
        running = asyncio.create_task(run_routing_fault(config(), *owned, clock=clock))
        await finish(clock)
        await asyncio.wait_for(closing.wait(), 2)
        running.cancel()
        release.set()
        result = await asyncio.wait_for(running, 2)
        assert result.status == "CANCELLED"
        assert finished.is_set()
        assert close_calls == 1
        assert all(client.closed and client.active == 0 for client in owned)
        await _assert_no_owned_tasks(clock)

    asyncio.run(scenario())


@pytest.mark.parametrize("client_index", range(4))
@pytest.mark.parametrize("failure_mode", ["raise", "timeout"])
def test_client_close_failure_blocks_report_and_closes_other_clients(
    client_index: int,
    failure_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        original_close = owned[client_index].close
        close_finished = asyncio.Event()
        close_calls = 0

        async def failing_close() -> None:
            nonlocal close_calls
            close_calls += 1
            try:
                if failure_mode == "timeout":
                    await asyncio.Future()
                raise RuntimeError("private close failure sentinel")
            finally:
                await original_close()
                close_finished.set()

        monkeypatch.setattr(owned[client_index], "close", failing_close)
        running = asyncio.create_task(run_routing_fault(config(), *owned, clock=clock))
        await finish(clock)
        with pytest.raises(EvaluationError, match="trial or cleanup") as error:
            await asyncio.wait_for(running, 2)
        assert "private" not in str(error.value)
        assert close_finished.is_set()
        assert close_calls == 1
        assert all(client.closed and client.active == 0 for client in owned)
        await _assert_no_owned_tasks(clock)

    asyncio.run(scenario())
