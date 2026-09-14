"""Shared recipe epochs preserve arrival/deadline timing and client ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from inferdrome.evaluation.contracts import (
    Bounds,
    Endpoint,
    EvaluationConfig,
    EvaluationError,
    Offer,
)
from inferdrome.evaluation.runner import run_evaluation


class Clock:
    def __init__(self, now: int = 1000) -> None:
        self.now = now
        self.waiters: dict[asyncio.Future[None], int] = {}

    def now_ns(self) -> int:
        return self.now

    async def sleep_until(self, absolute_ns: int) -> None:
        if absolute_ns <= self.now:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters[waiter] = absolute_ns
        try:
            await waiter
        finally:
            del self.waiters[waiter]

    def advance_to(self, now: int) -> None:
        assert now >= self.now
        self.now = now
        for waiter, at in tuple(self.waiters.items()):
            if at <= now and not waiter.done():
                waiter.set_result(None)


class Transport:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.dispatches: list[int] = []
        self.close_count = 0

    async def stream(
        self,
        origin: str,
        body: bytes,
        on_headers: Callable[[int], None],
        on_bytes: Callable[[bytes], None],
    ) -> None:
        self.dispatches.append(self.clock.now_ns())
        on_headers(200)
        on_bytes(
            b'data: {"choices": [{"index": 0, "delta": {"content": "hi"}, '
            b'"finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'
        )

    async def close(self) -> None:
        self.close_count += 1


def config(schedule: tuple[int, ...]) -> EvaluationConfig:
    return EvaluationConfig(
        schema_version="inferdrome.evaluation-config.v1",
        source_commit="a" * 40,
        model="test-model",
        endpoints=(
            Endpoint(endpoint_id="endpoint-a", origin="http://127.0.0.1:8001"),
            Endpoint(endpoint_id="endpoint-b", origin="http://127.0.0.1:8002"),
        ),
        bounds=Bounds(duration_ns=200, request_timeout_ns=20, drain_ns=50),
        offers=tuple(
            Offer(scheduled_ns=at, endpoint_id="endpoint-a", prompt="hello")
            for at in schedule
        ),
    )


async def settle() -> None:
    for _ in range(40):
        await asyncio.sleep(0)


def test_shared_epoch_includes_warmup_and_preserves_future_arrivals() -> None:
    async def scenario() -> None:
        clock = Clock()
        transport = Transport(clock)
        task = asyncio.create_task(
            run_evaluation(config((100, 150)), transport, clock=clock, start_ns=900)
        )
        await settle()
        assert transport.dispatches == [1000]
        assert not task.done()
        assert 1050 in clock.waiters.values()
        clock.advance_to(1050)
        await settle()
        assert task.done()
        result = await task
        assert [row.scheduled_ns for row in result.records] == [100, 150]
        assert [row.arrival_observed_ns for row in result.records] == [100, 150]
        assert [row.dispatch_ns for row in result.records] == [100, 150]
        assert [row.dispatch_lag_ns for row in result.records] == [0, 0]
        assert [row.first_content_ns for row in result.records] == [100, 150]
        assert result.elapsed_ns == 150
        assert transport.close_count == 1
        assert clock.waiters == {}

    asyncio.run(scenario())


def test_arrival_deadline_includes_time_before_shared_replay_invocation() -> None:
    async def scenario() -> None:
        clock = Clock(now=1010)
        transport = Transport(clock)
        task = asyncio.create_task(
            run_evaluation(config((90, 120)), transport, clock=clock, start_ns=900)
        )
        await settle()
        assert transport.dispatches == []
        clock.advance_to(1020)
        await settle()
        assert task.done()
        result = await task
        assert [row.outcome for row in result.records] == ["TIMEOUT", "SUCCESS"]
        assert [row.attempts for row in result.records] == [0, 1]
        assert [row.terminal_ns for row in result.records] == [110, 120]
        assert transport.dispatches == [1020]
        assert transport.close_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("origin", [None, 1000])
def test_default_and_explicit_current_epoch_keep_existing_timing(
    origin: int | None,
) -> None:
    async def scenario() -> None:
        clock = Clock()
        transport = Transport(clock)
        result = await run_evaluation(
            config((0,)), transport, clock=clock, start_ns=origin
        )
        row = result.records[0]
        assert row.outcome == "SUCCESS"
        assert row.dispatch_ns == 0
        assert row.terminal_ns == 0
        assert result.elapsed_ns == 0
        assert transport.close_count == 1

    asyncio.run(scenario())


def test_zero_epoch_is_valid() -> None:
    async def scenario() -> None:
        clock = Clock(now=0)
        transport = Transport(clock)
        result = await run_evaluation(config((0,)), transport, clock=clock, start_ns=0)
        assert result.records[0].outcome == "SUCCESS"
        assert result.records[0].terminal_ns == 0
        assert transport.close_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", [-1, True, 1.5, "SECRET", [], 1001])
def test_invalid_or_future_epoch_still_closes_owned_transport(invalid: object) -> None:
    async def scenario() -> None:
        clock = Clock()
        transport = Transport(clock)
        with pytest.raises(EvaluationError, match="start epoch") as error:
            await run_evaluation(
                config((0,)),
                transport,
                clock=clock,
                start_ns=invalid,  # type: ignore[arg-type]
            )
        assert "SECRET" not in str(error.value)
        assert transport.dispatches == []
        assert transport.close_count == 1
        assert clock.waiters == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", [-1, True, 1.5, "SECRET", [], 251])
def test_invalid_stop_boundary_still_closes_transport(invalid: object) -> None:
    async def scenario() -> None:
        clock = Clock()
        transport = Transport(clock)
        with pytest.raises(EvaluationError, match="stop boundary"):
            await run_evaluation(
                config((0,)),
                transport,
                clock=clock,
                stop_at_ns=invalid,  # type: ignore[arg-type]
            )
        assert transport.dispatches == []
        assert transport.close_count == 1

    asyncio.run(scenario())


def test_stop_boundary_cancels_all_later_offers_without_waiting_for_controller() -> (
    None
):
    async def scenario() -> None:
        clock = Clock()
        transport = Transport(clock)
        task = asyncio.create_task(
            run_evaluation(
                config((0, 100, 150)), transport, clock=clock, stop_at_ns=100
            )
        )
        await settle()
        clock.advance_to(1100)
        await settle()
        result = await task
        assert [row.outcome for row in result.records] == [
            "SUCCESS",
            "CANCELLED",
            "CANCELLED",
        ]
        assert transport.dispatches == [1000]
        assert result.cancelled
        assert transport.close_count == 1
        assert not clock.waiters

    asyncio.run(scenario())


def test_stop_boundary_rechecked_after_selector_before_dispatch() -> None:
    async def scenario() -> None:
        clock = Clock()
        transport = Transport(clock)

        def selector(index: int, now: int) -> str:
            clock.advance_to(1010)
            return "endpoint-a"

        result = await run_evaluation(
            config((0,)),
            transport,
            clock=clock,
            stop_at_ns=10,
            selector=selector,  # type: ignore[arg-type]
        )
        assert result.records[0].outcome == "CANCELLED"
        assert result.records[0].attempts == 0
        assert result.cancelled
        assert transport.dispatches == []
        assert transport.close_count == 1

    asyncio.run(scenario())


def test_stop_boundary_overrides_late_success_callback() -> None:
    class LateTransport(Transport):
        async def stream(self, origin, body, on_headers, on_bytes) -> None:
            self.clock.advance_to(1010)
            await super().stream(origin, body, on_headers, on_bytes)

    async def scenario() -> None:
        clock = Clock()
        transport = LateTransport(clock)
        result = await run_evaluation(
            config((0,)), transport, clock=clock, stop_at_ns=10
        )
        assert result.records[0].outcome == "CANCELLED"
        assert result.records[0].terminal_ns == 10
        assert result.cancelled
        assert transport.close_count == 1

    asyncio.run(scenario())
