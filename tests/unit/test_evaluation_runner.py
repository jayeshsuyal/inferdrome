"""Deterministic offered-traffic and lifecycle checks, without real network I/O."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from inferdrome.evaluation.contracts import (
    Bounds,
    Endpoint,
    EndpointId,
    EvaluationConfig,
    EvaluationError,
    Offer,
)
from inferdrome.evaluation.runner import (
    EvaluationResult,
    TransportError,
    run_evaluation,
)


class ManualClock:
    """Advance explicitly; sleeping never advances time or consumes real time."""

    def __init__(self, epoch_ns: int = 10_000) -> None:
        self.epoch_ns = epoch_ns
        self.elapsed_ns = 0
        self.waiters: dict[asyncio.Future[None], int] = {}

    def now_ns(self) -> int:
        return self.epoch_ns + self.elapsed_ns

    async def sleep_until(self, absolute_ns: int) -> None:
        if absolute_ns <= self.now_ns():
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters[waiter] = absolute_ns
        try:
            await waiter
        finally:
            del self.waiters[waiter]

    def advance_to(self, elapsed_ns: int) -> None:
        assert elapsed_ns >= self.elapsed_ns
        self.elapsed_ns = elapsed_ns
        for waiter, deadline in tuple(self.waiters.items()):
            if deadline <= self.now_ns() and not waiter.done():
                waiter.set_result(None)


async def settle() -> None:
    """Give ready tasks bounded scheduling turns; never move the manual clock."""
    for _ in range(40):
        await asyncio.sleep(0)


async def advance(clock: ManualClock, elapsed_ns: int) -> None:
    clock.advance_to(elapsed_ns)
    await settle()


def content(text: str = "generated", finish: str | None = None) -> bytes:
    payload = {
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}]
    }
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


DONE = b"data: [DONE]\n\n"
COMPLETE = content(finish="stop") + DONE
USAGE = (
    b'data: {"choices": [], "usage": {"prompt_tokens": 11, '
    b'"completion_tokens": 9, "total_tokens": 20}}\n\n'
)


@dataclass(frozen=True)
class Script:
    """All script times are relative to invocation of transport.stream."""

    chunks: tuple[tuple[int, bytes], ...] = ((0, COMPLETE),)
    headers_ns: int = 0
    status: int = 200
    eof_ns: int = 0
    error: Exception | None = None


class FakeTransport:
    def __init__(self, clock: ManualClock, scripts: dict[int, Script]) -> None:
        self.clock = clock
        self.scripts = scripts
        self.calls: list[tuple[int, int, str]] = []
        self.bodies: dict[int, bytes] = {}
        self.active = 0
        self.peak_active = 0
        self.cancelled: list[int] = []
        self.closed = False
        self.close_calls = 0

    async def stream(
        self,
        origin: str,
        body: bytes,
        on_headers: Callable[[int], None],
        on_bytes: Callable[[bytes], None],
    ) -> None:
        index = int(json.loads(body)["messages"][0]["content"].split("-")[-1])
        self.calls.append((index, self.clock.elapsed_ns, origin))
        self.bodies[index] = body
        script = self.scripts.get(index, Script())
        started = self.clock.now_ns()
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            await self.clock.sleep_until(started + script.headers_ns)
            on_headers(script.status)
            for timestamp, chunk in script.chunks:
                await self.clock.sleep_until(started + timestamp)
                on_bytes(chunk)
            await self.clock.sleep_until(started + script.eof_ns)
            if script.error is not None:
                raise script.error
        except asyncio.CancelledError:
            self.cancelled.append(index)
            raise
        finally:
            self.active -= 1

    async def close(self) -> None:
        self.close_calls += 1
        assert self.active == 0
        self.closed = True


def config(schedule: list[int], **overrides: int) -> EvaluationConfig:
    bounds = Bounds(
        **{
            "max_requests": len(schedule),
            "concurrency": 2,
            "max_queue": 2,
            "duration_ns": max(schedule) + 100,
            "request_timeout_ns": 1000,
            "drain_ns": 1000,
            "cleanup_timeout_ns": 100_000_000,
            "max_stream_bytes": 4096,
            "max_event_bytes": 1024,
            "max_content_events": 20,
            **overrides,
        }
    )
    return EvaluationConfig(
        schema_version="inferdrome.evaluation-config.v1",
        source_commit="a" * 40,
        model="test/private-model",
        endpoints=(
            Endpoint(endpoint_id="endpoint-a", origin="http://127.0.0.1:8001"),
            Endpoint(endpoint_id="endpoint-b", origin="http://127.0.0.1:8002"),
        ),
        bounds=bounds,
        offers=tuple(
            Offer(
                scheduled_ns=scheduled,
                endpoint_id="endpoint-a" if index % 2 == 0 else "endpoint-b",
                prompt=f"private-prompt-{index}",
            )
            for index, scheduled in enumerate(schedule)
        ),
    )


async def result_of(
    task: asyncio.Task[EvaluationResult],
    transport: FakeTransport,
    clock: ManualClock,
) -> EvaluationResult:
    await settle()
    assert task.done(), "replay did not complete at the declared manual time"
    result = await task
    assert transport.closed
    assert transport.close_calls == 1
    assert transport.active == 0
    assert clock.waiters == {}
    assert len(result.records) == len({row.request_index for row in result.records})
    assert all(row.attempts in (0, 1) for row in result.records)
    outcomes = result.to_dict()["outcomes"]
    assert isinstance(outcomes, dict)
    assert sum(outcomes.values()) == len(result.records)
    return result


def test_scheduled_arrivals_continue_while_busy_and_queue_stays_bounded() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        transport = FakeTransport(clock, {0: Script(chunks=((50, COMPLETE),))})
        task = asyncio.create_task(
            run_evaluation(
                config([0, 10, 20, 30], concurrency=1, max_queue=1),
                transport,
                clock=clock,
            )
        )
        await settle()
        assert [call[:2] for call in transport.calls] == [(0, 0)]
        for elapsed in (10, 20, 30):
            await advance(clock, elapsed)
            assert transport.active == 1
            assert len(transport.calls) == 1
            assert not task.done()
        await advance(clock, 50)
        result = await result_of(task, transport, clock)
        assert [r.outcome for r in result.records] == [
            "SUCCESS",
            "SUCCESS",
            "REJECTED_CAPACITY",
            "REJECTED_CAPACITY",
        ]
        assert [r.arrival_observed_ns for r in result.records] == [0, 10, 20, 30]
        assert [r.dispatch_ns for r in result.records] == [0, 50, None, None]
        assert [r.dispatch_lag_ns for r in result.records] == [0, 40, None, None]
        assert result.peak_active == transport.peak_active == 1
        assert result.peak_queue == 1
        assert result.to_dict()["offered_count"] == 4
        assert result.to_dict()["arrivals_observed_count"] == 4
        assert result.to_dict()["dispatched_count"] == 2

    asyncio.run(scenario())


def test_no_queue_rejects_excess_offers_without_dispatch_or_hidden_waiting() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        delayed = Script(chunks=((20, COMPLETE),))
        transport = FakeTransport(clock, {0: delayed, 1: delayed})
        task = asyncio.create_task(
            run_evaluation(
                config([0, 0, 0, 5], concurrency=2, max_queue=0),
                transport,
                clock=clock,
            )
        )
        await settle()
        assert transport.active == 2
        await advance(clock, 5)
        assert transport.active == 2
        assert [call[0] for call in transport.calls] == [0, 1]
        await advance(clock, 20)
        result = await result_of(task, transport, clock)
        assert result.peak_active == transport.peak_active == 2
        assert result.peak_queue == 0
        assert [r.outcome for r in result.records] == [
            "SUCCESS",
            "SUCCESS",
            "REJECTED_CAPACITY",
            "REJECTED_CAPACITY",
        ]
        assert [r.terminal_ns for r in result.records] == [20, 20, 0, 5]
        assert [r.attempts for r in result.records] == [1, 1, 0, 0]

    asyncio.run(scenario())


def test_deadline_includes_queue_time_and_partial_inflight_diagnostics() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        delayed = Script(chunks=((1, content()),), eof_ns=30)
        transport = FakeTransport(clock, {0: delayed, 1: delayed})
        task = asyncio.create_task(
            run_evaluation(
                config([0, 5], concurrency=1, max_queue=1, request_timeout_ns=10),
                transport,
                clock=clock,
            )
        )
        await settle()
        for elapsed in (1, 5, 10, 11, 15):
            await advance(clock, elapsed)
        result = await result_of(task, transport, clock)
        assert [r.outcome for r in result.records] == ["TIMEOUT", "TIMEOUT"]
        assert [r.dispatch_ns for r in result.records] == [0, 10]
        assert [r.dispatch_lag_ns for r in result.records] == [0, 5]
        assert [r.first_content_ns for r in result.records] == [1, 11]
        assert [r.terminal_ns for r in result.records] == [10, 15]
        assert transport.cancelled == [0, 1]

    asyncio.run(scenario())


def test_queued_request_expiring_at_boundary_never_dispatches() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        transport = FakeTransport(clock, {0: Script(chunks=(), eof_ns=50)})
        task = asyncio.create_task(
            run_evaluation(
                config([0, 0], concurrency=1, max_queue=1, request_timeout_ns=10),
                transport,
                clock=clock,
            )
        )
        await settle()
        await advance(clock, 10)
        result = await result_of(task, transport, clock)
        assert [r.outcome for r in result.records] == ["TIMEOUT", "TIMEOUT"]
        assert [r.attempts for r in result.records] == [1, 0]
        assert [r.terminal_ns for r in result.records] == [10, 10]
        assert result.records[1].arrival_observed_ns == 0
        assert result.records[1].dispatch_lag_ns is None

    asyncio.run(scenario())


@pytest.mark.parametrize("completion_ns", [9, 10, 11])
def test_completion_must_precede_request_deadline(completion_ns: int) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        transport = FakeTransport(
            clock, {0: Script(chunks=((completion_ns, COMPLETE),))}
        )
        task = asyncio.create_task(
            run_evaluation(config([0], request_timeout_ns=10), transport, clock=clock)
        )
        await settle()
        await advance(clock, min(completion_ns, 10))
        result = await result_of(task, transport, clock)
        assert result.records[0].outcome == (
            "SUCCESS" if completion_ns < 10 else "TIMEOUT"
        )
        assert result.records[0].terminal_ns == min(completion_ns, 10)

    asyncio.run(scenario())


@pytest.mark.parametrize("observed_ns, expected", [(25, "SUCCESS"), (110, "TIMEOUT")])
def test_late_coordinator_keeps_original_offer_time(
    observed_ns: int, expected: str
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        transport = FakeTransport(clock, {})
        task = asyncio.create_task(
            run_evaluation(config([10], request_timeout_ns=100), transport, clock=clock)
        )
        await settle()
        assert transport.calls == []
        await advance(clock, observed_ns)
        result = await result_of(task, transport, clock)
        row = result.records[0]
        assert row.scheduled_ns == 10
        assert row.arrival_observed_ns == observed_ns
        assert row.outcome == expected
        assert row.dispatch_lag_ns == (15 if expected == "SUCCESS" else None)
        assert row.attempts == int(expected == "SUCCESS")

    asyncio.run(scenario())


def test_stream_timing_distinguishes_headers_body_content_and_eof() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        generated = content("many tokens \u00e9 \U0001f642")
        split = generated.index("\u00e9".encode()) + 1
        script = Script(
            headers_ns=1,
            chunks=(
                (2, b": connected\n\n"),
                (3, content("")),
                (4, generated[:split]),
                (6, generated[split:]),
                (8, content("two") + content("three")),
                (9, content("", finish="length") + USAGE + DONE),
            ),
            eof_ns=11,
        )
        transport = FakeTransport(clock, {0: script})
        task = asyncio.create_task(run_evaluation(config([0]), transport, clock=clock))
        await settle()
        for elapsed in (1, 2, 3, 4, 6, 8, 9):
            await advance(clock, elapsed)
            assert not task.done()
        await advance(clock, 11)
        result = await result_of(task, transport, clock)
        row = result.records[0]
        assert row.outcome == "SUCCESS"
        assert row.response_headers_ns == 1
        assert row.first_body_byte_ns == 2
        assert row.first_content_ns == 6
        assert row.content_event_times_ns == (6, 8, 8)
        assert row.protocol_done_ns == 9
        assert row.terminal_ns == 11
        assert row.finish_reason == "length"
        assert row.prompt_tokens == 11
        assert row.completion_tokens == 9
        assert row.usage_provenance == "SERVER_REPORTED_STREAM_USAGE"
        assert result.exact_token_timing == "UNAVAILABLE"
        assert result.wire_first_response_byte == "UNAVAILABLE"
        assert result.evidence_eligible is False
        request = json.loads(transport.bodies[0])
        assert request["stream"] is True
        assert request["stream_options"] == {"include_usage": True}
        assert request["n"] == 1
        assert request["chat_template_kwargs"] == {"enable_thinking": False}
        output = json.dumps(result.to_dict())
        assert "private-prompt" not in output
        assert "test/private-model" not in output
        assert "many tokens" not in output

    asyncio.run(scenario())


def test_every_offer_has_exactly_one_outcome_even_when_requests_fail() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        scripts = {
            1: Script(status=503, chunks=()),
            2: Script(chunks=((0, b"data: malformed SECRET\n\n"),)),
            3: Script(chunks=((0, b":" + b"x" * 1100),)),
            4: Script(chunks=((0, content()),)),
            5: Script(chunks=(), error=TransportError("SECRET")),
            6: Script(chunks=(), error=RuntimeError("SECRET")),
            7: Script(chunks=((0, content()), (1, b"event: error\ndata: SECRET\n\n"))),
            8: Script(chunks=((1, content()),), eof_ns=50),
        }
        transport = FakeTransport(clock, scripts)

        def select(index: int, _now: int) -> EndpointId | None:
            if index == 9:
                return None
            if index == 10:
                return "invalid"  # type: ignore[return-value]
            if index == 11:
                raise RuntimeError("SECRET")
            return "endpoint-a"

        task = asyncio.create_task(
            run_evaluation(
                config([0] * 12, concurrency=12, max_queue=0, request_timeout_ns=20),
                transport,
                clock=clock,
                selector=select,
            )
        )
        await settle()
        await advance(clock, 1)
        await advance(clock, 20)
        result = await result_of(task, transport, clock)
        assert [r.outcome for r in result.records] == [
            "SUCCESS",
            "HTTP_ERROR",
            "STREAM_ERROR",
            "STREAM_LIMIT",
            "INCOMPLETE_STREAM",
            "TRANSPORT_ERROR",
            "INTERNAL_ERROR",
            "STREAM_ERROR",
            "TIMEOUT",
            "REJECTED_ROUTE",
            "INTERNAL_ERROR",
            "INTERNAL_ERROR",
        ]
        assert [r.attempts for r in result.records] == [1] * 9 + [0] * 3
        assert result.records[1].http_status == 503
        assert result.records[7].first_content_ns == 0
        assert result.records[7].content_event_times_ns == (0,)
        assert result.records[8].first_content_ns == 1
        assert result.records[8].content_event_times_ns == (1,)
        assert result.records[8].response_headers_ns == 0
        assert result.routing == "INJECTED"
        assert "SECRET" not in json.dumps(result.to_dict())

    asyncio.run(scenario())


@pytest.mark.parametrize("direct_cancel", [False, True])
def test_cancellation_before_first_arrival_preserves_entire_population(
    direct_cancel: bool,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        stop = asyncio.Event()
        transport = FakeTransport(clock, {})
        task = asyncio.create_task(
            run_evaluation(config([10, 20]), transport, clock=clock, stop=stop)
        )
        await settle()
        if direct_cancel:
            task.cancel()
        else:
            stop.set()
        result = await result_of(task, transport, clock)
        assert result.cancelled
        assert [r.outcome for r in result.records] == ["CANCELLED", "CANCELLED"]
        assert [r.arrival_observed_ns for r in result.records] == [None, None]
        assert [r.attempts for r in result.records] == [0, 0]
        assert result.to_dict()["arrivals_observed_count"] == 0
        assert transport.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("direct_cancel", [False, True])
def test_cancellation_cleans_inflight_queued_and_future_offers(
    direct_cancel: bool,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        stop = asyncio.Event()
        transport = FakeTransport(
            clock, {0: Script(chunks=((1, content()),), eof_ns=50)}
        )
        task = asyncio.create_task(
            run_evaluation(
                config([0, 0, 5], concurrency=1, max_queue=1),
                transport,
                clock=clock,
                stop=stop,
            )
        )
        await settle()
        await advance(clock, 1)
        await advance(clock, 2)
        if direct_cancel:
            task.cancel()
        else:
            stop.set()
        result = await result_of(task, transport, clock)
        assert result.cancelled
        assert [r.outcome for r in result.records] == ["CANCELLED"] * 3
        assert [r.attempts for r in result.records] == [1, 0, 0]
        assert [r.arrival_observed_ns for r in result.records] == [0, 0, None]
        assert [r.terminal_ns for r in result.records] == [2, 2, 2]
        assert result.records[0].content_event_times_ns == (1,)
        assert result.to_dict()["arrivals_observed_count"] == 2
        assert transport.cancelled == [0]

    asyncio.run(scenario())


@pytest.mark.parametrize("eof_ns", [10, 100])
def test_global_drain_boundary_cancels_active_and_queued_offers(eof_ns: int) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        transport = FakeTransport(clock, {0: Script(chunks=((eof_ns, COMPLETE),))})
        task = asyncio.create_task(
            run_evaluation(
                config(
                    [0, 3, 4],
                    concurrency=1,
                    max_queue=2,
                    duration_ns=5,
                    drain_ns=5,
                ),
                transport,
                clock=clock,
            )
        )
        await settle()
        for elapsed in (3, 4, 10):
            await advance(clock, elapsed)
        result = await result_of(task, transport, clock)
        assert not result.cancelled
        assert [r.outcome for r in result.records] == ["DRAIN_TIMEOUT"] * 3
        assert [r.attempts for r in result.records] == [1, 0, 0]
        assert [r.terminal_ns for r in result.records] == [10, 10, 10]
        assert result.elapsed_ns == 10

    asyncio.run(scenario())


def test_replay_is_reproducible_across_monotonic_clock_epochs() -> None:
    async def replay(epoch: int) -> EvaluationResult:
        clock = ManualClock(epoch)
        transport = FakeTransport(clock, {0: Script(chunks=((8, COMPLETE),))})
        task = asyncio.create_task(
            run_evaluation(config([0, 5, 10]), transport, clock=clock)
        )
        await settle()
        for elapsed in (5, 8, 10):
            await advance(clock, elapsed)
        return await result_of(task, transport, clock)

    assert asyncio.run(replay(1000)).to_dict() == asyncio.run(replay(90_000)).to_dict()


def test_injected_selector_uses_dispatch_time_and_selects_the_origin() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        transport = FakeTransport(clock, {0: Script(chunks=((8, COMPLETE),))})
        selections: list[tuple[int, int]] = []

        def select(index: int, now: int) -> EndpointId:
            selections.append((index, now))
            return "endpoint-b" if index == 0 else "endpoint-a"

        task = asyncio.create_task(
            run_evaluation(
                config([0, 2], concurrency=1),
                transport,
                clock=clock,
                selector=select,
            )
        )
        await settle()
        await advance(clock, 2)
        await advance(clock, 8)
        result = await result_of(task, transport, clock)
        assert selections == [(0, 0), (1, 8)]
        assert transport.calls == [
            (0, 0, "http://127.0.0.1:8002"),
            (1, 8, "http://127.0.0.1:8001"),
        ]
        assert [r.endpoint_id for r in result.records] == ["endpoint-b", "endpoint-a"]
        assert result.routing == "INJECTED"

    asyncio.run(scenario())


def test_cancellation_keeps_already_completed_request_outcomes() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        stop = asyncio.Event()
        transport = FakeTransport(
            clock,
            {
                0: Script(chunks=((1, COMPLETE),)),
                1: Script(chunks=(), eof_ns=50),
            },
        )
        task = asyncio.create_task(
            run_evaluation(
                config([0, 0, 5], concurrency=1, max_queue=1),
                transport,
                clock=clock,
                stop=stop,
            )
        )
        await settle()
        await advance(clock, 1)
        await advance(clock, 2)
        stop.set()
        result = await result_of(task, transport, clock)
        assert [r.outcome for r in result.records] == [
            "SUCCESS",
            "CANCELLED",
            "CANCELLED",
        ]
        assert [r.terminal_ns for r in result.records] == [1, 2, 2]
        assert [r.attempts for r in result.records] == [1, 1, 0]
        assert result.records[1].dispatch_lag_ns == 1
        assert transport.cancelled == [1]

    asyncio.run(scenario())


def test_client_cancellation_cleanup_bound_blocks_result_publication() -> None:
    class ResistantTransport(FakeTransport):
        def __init__(self, clock: ManualClock) -> None:
            super().__init__(clock, {0: Script(chunks=(), eof_ns=50)})
            self.release = asyncio.Event()
            self.resisted = False

        async def stream(
            self,
            origin: str,
            body: bytes,
            on_headers: Callable[[int], None],
            on_bytes: Callable[[bytes], None],
        ) -> None:
            try:
                await super().stream(origin, body, on_headers, on_bytes)
            except asyncio.CancelledError:
                self.resisted = True
                await self.release.wait()

        async def close(self) -> None:
            # End the deliberate misbehavior so the test leaves no live tasks.
            self.release.set()
            await settle()
            await super().close()

    async def scenario() -> None:
        clock = ManualClock()
        transport = ResistantTransport(clock)
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_evaluation(
                config([0], cleanup_timeout_ns=1_000_000),
                transport,
                clock=clock,
                stop=stop,
            )
        )
        await settle()
        stop.set()
        with pytest.raises(EvaluationError, match="client cleanup exceeded"):
            await task
        await settle()
        assert transport.resisted
        assert transport.closed
        assert transport.active == 0
        assert transport.close_calls == 1
        assert clock.waiters == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("hang", [False, True])
def test_transport_cleanup_failure_blocks_result_publication(hang: bool) -> None:
    class BrokenCloseTransport(FakeTransport):
        async def close(self) -> None:
            self.close_calls += 1
            if hang:
                await asyncio.Future()
            raise RuntimeError("SECRET cleanup error")

    async def scenario() -> None:
        clock = ManualClock()
        transport = BrokenCloseTransport(clock, {})
        # Cleanup deliberately uses a real event-loop guard, even with fake time.
        with pytest.raises(EvaluationError, match="transport cleanup") as error:
            await run_evaluation(
                config([0], cleanup_timeout_ns=1_000_000), transport, clock=clock
            )
        await settle()
        assert "SECRET" not in str(error.value)
        assert transport.close_calls == 1
        assert transport.active == 0
        assert clock.waiters == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "request_deadline, advance_ns, stop_inside_selector, expected",
    [
        (10, 15, False, "TIMEOUT"),
        (50, 35, False, "DRAIN_TIMEOUT"),
        (30, 35, False, "TIMEOUT"),
        (50, 0, True, "CANCELLED"),
        (10, 35, True, "CANCELLED"),
    ],
)
def test_selector_crossing_cutoff_cannot_start_a_transport_attempt(
    request_deadline: int,
    advance_ns: int,
    stop_inside_selector: bool,
    expected: str,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        transport = FakeTransport(clock, {})
        stop = asyncio.Event()
        selections: list[int] = []

        def select(index: int, _now: int) -> EndpointId:
            selections.append(index)
            clock.advance_to(advance_ns)
            if stop_inside_selector:
                stop.set()
            return "endpoint-b"

        task = asyncio.create_task(
            run_evaluation(
                config(
                    [0],
                    request_timeout_ns=request_deadline,
                    duration_ns=20,
                    drain_ns=10,
                ),
                transport,
                clock=clock,
                stop=stop,
                selector=select,
            )
        )
        result = await result_of(task, transport, clock)
        assert selections == [0]
        assert transport.calls == []
        row = result.records[0]
        assert row.attempts == 0
        assert row.dispatch_ns is None
        assert row.dispatch_lag_ns is None
        assert row.response_headers_ns is None
        assert row.outcome == expected
        assert row.terminal_ns == advance_ns
        assert result.cancelled is stop_inside_selector

    asyncio.run(scenario())


def test_success_never_has_a_terminal_timestamp_at_or_after_its_deadline() -> None:
    class TickingClock(ManualClock):
        """Time progresses between observations without any coroutine yielding."""

        def now_ns(self) -> int:
            self.elapsed_ns += 1
            return super().now_ns()

    async def scenario() -> None:
        outcomes: set[str] = set()
        # Sweep bounds, without depending on the implementation's clock-call count.
        # This places terminal observations on both sides of a deadline boundary.
        for timeout_ns in range(1, 65):
            clock = TickingClock()
            transport = FakeTransport(clock, {})
            result = await run_evaluation(
                config(
                    [0],
                    request_timeout_ns=timeout_ns,
                    duration_ns=200,
                    drain_ns=200,
                ),
                transport,
                clock=clock,
            )
            row = result.records[0]
            outcomes.add(row.outcome)
            if row.outcome == "SUCCESS":
                assert row.terminal_ns < row.scheduled_ns + timeout_ns
            assert transport.closed
            assert transport.active == 0
            assert clock.waiters == {}
        assert outcomes == {"SUCCESS", "TIMEOUT"}

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["transport", "stream", "incomplete", "http"])
@pytest.mark.parametrize(
    "request_deadline, observed_ns, expected",
    [
        (10, 15, "TIMEOUT"),
        (50, 35, "DRAIN_TIMEOUT"),
        (10, 55, "TIMEOUT"),
        (50, 55, "DRAIN_TIMEOUT"),
        (30, 35, "TIMEOUT"),
    ],
)
def test_late_failures_use_the_earliest_cutoff_for_terminal_outcome(
    failure: str, request_deadline: int, observed_ns: int, expected: str
) -> None:
    class LateFailureTransport(FakeTransport):
        async def stream(
            self,
            origin: str,
            body: bytes,
            on_headers: Callable[[int], None],
            on_bytes: Callable[[bytes], None],
        ) -> None:
            await super().stream(origin, body, on_headers, on_bytes)
            # Advance synchronously so the error beats the coordinator's timer
            # callback. Its recorded outcome must still respect the actual cutoff.
            self.clock.advance_to(observed_ns)
            if failure == "transport":
                raise TransportError("SECRET late transport failure")
            if failure == "stream":
                on_bytes(b"data: SECRET malformed JSON\n\n")

    async def scenario() -> None:
        clock = ManualClock()
        transport = LateFailureTransport(
            clock,
            {0: Script(status=503, chunks=())}
            if failure == "http"
            else {0: Script(chunks=((0, content()),))},
        )
        task = asyncio.create_task(
            run_evaluation(
                config(
                    [0],
                    request_timeout_ns=request_deadline,
                    duration_ns=20,
                    drain_ns=10,
                ),
                transport,
                clock=clock,
            )
        )
        result = await result_of(task, transport, clock)
        row = result.records[0]
        assert row.attempts == 1
        assert row.outcome == expected
        assert row.terminal_ns == observed_ns
        if failure != "http":
            assert row.first_content_ns == 0
            assert row.content_event_times_ns == (0,)
        assert "SECRET" not in json.dumps(result.to_dict())

    asyncio.run(scenario())


def test_cancellation_during_close_waits_for_cooperative_shutdown() -> None:
    class ControlledCloseTransport(FakeTransport):
        def __init__(self, clock: ManualClock) -> None:
            super().__init__(clock, {})
            self.close_started = asyncio.Event()
            self.allow_close = asyncio.Event()
            self.close_finished = False
            self.close_task: asyncio.Task[None] | None = None

        async def close(self) -> None:
            self.close_calls += 1
            self.close_task = asyncio.current_task()
            self.close_started.set()
            try:
                await self.allow_close.wait()
                self.closed = True
            finally:
                self.close_finished = True

    async def scenario() -> None:
        clock = ManualClock()
        transport = ControlledCloseTransport(clock)
        task = asyncio.create_task(run_evaluation(config([0]), transport, clock=clock))
        await transport.close_started.wait()
        task.cancel()
        await settle()
        assert not task.done()
        assert not transport.close_finished
        transport.allow_close.set()
        result = await result_of(task, transport, clock)
        assert result.cancelled
        assert result.records[0].outcome == "SUCCESS"
        assert transport.close_finished
        assert transport.close_task is not None
        assert transport.close_task.done()
        assert not transport.close_task.cancelled()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_during_close", [False, True])
def test_close_timeout_cancels_and_joins_cooperative_shutdown_task(
    cancel_during_close: bool,
) -> None:
    class WaitingCloseTransport(FakeTransport):
        def __init__(self, clock: ManualClock) -> None:
            super().__init__(clock, {})
            self.close_started = asyncio.Event()
            self.close_finished = False
            self.close_task: asyncio.Task[None] | None = None

        async def close(self) -> None:
            self.close_calls += 1
            self.close_task = asyncio.current_task()
            self.close_started.set()
            try:
                await asyncio.Future()
            finally:
                self.close_finished = True

    async def scenario() -> None:
        clock = ManualClock()
        transport = WaitingCloseTransport(clock)
        task = asyncio.create_task(
            run_evaluation(
                config([0], cleanup_timeout_ns=1_000_000), transport, clock=clock
            )
        )
        await transport.close_started.wait()
        if cancel_during_close:
            task.cancel()
        with pytest.raises(EvaluationError, match="transport cleanup"):
            await task
        # Assert immediately on API return: a later event-loop turn must not be
        # needed to finish the cancellation of a task owned by the runner.
        assert transport.close_finished
        assert transport.close_task is not None
        assert transport.close_task.done()
        assert transport.close_task.cancelled()
        assert transport.close_calls == 1
        assert transport.active == 0
        assert clock.waiters == {}

    asyncio.run(scenario())
