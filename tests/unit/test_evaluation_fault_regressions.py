"""Regressions for review findings in fault recovery and lifecycle accounting."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest

from inferdrome.evaluation.contracts import (
    Bounds,
    Endpoint,
    EndpointId,
    EvaluationConfig,
    EvaluationError,
    Offer,
)
from inferdrome.evaluation.fault_config import (
    FaultTiming,
    RoutingFaultConfig,
    TelemetryBounds,
    load_routing_config_bytes,
)
from inferdrome.evaluation.faults import (
    Channel,
    FaultEvent,
    ObservationRecord,
    _acquire,
    _Selector,
    _Trial,
    run_routing_fault,
)
from inferdrome.evaluation.observations import ProbeResponse
from inferdrome.evaluation.policies import (
    EndpointSnapshot,
    HealthObservation,
    LoadObservation,
    RouterSnapshot,
    RoutingPolicy,
)
from inferdrome.evaluation.runner import run_evaluation
from tests.unit.test_evaluation_faults import MS, clients, payload
from tests.unit.test_evaluation_runner import ManualClock, advance, settle


class Clock:
    def __init__(self, now: int = 0) -> None:
        self.now = now

    def now_ns(self) -> int:
        return self.now

    async def sleep_until(self, at: int) -> None:
        if at > self.now:
            await asyncio.Future()


class Stream:
    def __init__(self) -> None:
        self.closed = 0
        self.calls = 0

    async def stream(
        self,
        origin: str,
        body: bytes,
        on_headers: Callable[[int], None],
        on_bytes: Callable[[bytes], None],
    ) -> None:
        self.calls += 1
        on_headers(200)
        on_bytes(
            b'data: {"choices": [{"index": 0, "delta": {"content": "hi"}, '
            b'"finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'
        )

    async def close(self) -> None:
        self.closed += 1


class Probe:
    def __init__(self) -> None:
        self.closed = 0

    async def get(self, origin: str, path: str) -> ProbeResponse:
        return ProbeResponse(200, b"")

    async def close(self) -> None:
        self.closed += 1


def config(foreground_at: int = 1_000_000) -> RoutingFaultConfig:
    endpoints = (
        Endpoint(endpoint_id="endpoint-a", origin="http://127.0.0.1:8001"),
        Endpoint(endpoint_id="endpoint-b", origin="http://127.0.0.1:8002"),
    )

    def population(at: int) -> EvaluationConfig:
        return EvaluationConfig(
            schema_version="inferdrome.evaluation-config.v1",
            source_commit="a" * 40,
            model="test-model",
            endpoints=endpoints,
            bounds=Bounds(
                max_requests=1,
                concurrency=1,
                max_queue=0,
                max_content_events=20,
                duration_ns=5_000_000,
                drain_ns=1_000_000,
            ),
            offers=(Offer(scheduled_ns=at, endpoint_id="endpoint-a", prompt="hello"),),
        )

    return RoutingFaultConfig(
        schema_version="inferdrome.evaluation-routing-config.v1",
        foreground=population(foreground_at),
        background=population(2_000_000),
        policy_id="evaluation_least_reported_load_v1",
        telemetry=TelemetryBounds(
            interval_ns=1_000_000,
            warmup_ns=1_000_000,
            health_freshness_ns=1_000_000,
            load_freshness_ns=10,
            max_observations=36,
        ),
        fault=FaultTiming(
            target_endpoint_id="endpoint-a",
            freeze_start_ns=2_000_000,
            restore_ns=3_000_000,
            background_stop_ns=4_000_000,
        ),
    )


@pytest.mark.parametrize("restored_sample_age", [10, 11])
def test_recovery_requires_fresh_restored_load_even_for_least_reported_policy(
    restored_sample_age: int,
) -> None:
    async def scenario() -> None:
        restored = 3_000_000
        decision_ns = restored + restored_sample_age
        recipe = config(decision_ns)
        clock = Clock(decision_ns)
        foreground_transport, background_transport = Stream(), Stream()
        trial = _Trial(
            recipe,
            foreground_transport,
            background_transport,
            Probe(),
            Probe(),
            clock,
            0,
        )
        trial.events.extend(
            (
                FaultEvent("FREEZE_STARTED", 2_000_000),
                FaultEvent("TELEMETRY_RESTORED", restored),
            )
        )
        trial.observations.append(
            ObservationRecord(
                "ROUTER_LOAD",
                "endpoint-a",
                1,
                restored,
                restored,
                restored,
                "VALID",
                True,
                None,
                1,
                0,
                0,
            )
        )
        health = HealthObservation(
            1, decision_ns, decision_ns, decision_ns, "VALID", True
        )
        target = LoadObservation(1, restored, restored, restored, "VALID", 1, 0)
        other = LoadObservation(1, decision_ns, decision_ns, decision_ns, "VALID", 2, 0)
        snapshot = RouterSnapshot(
            (
                EndpointSnapshot("endpoint-a", health, target, target),
                EndpointSnapshot("endpoint-b", health, other, other),
            )
        )
        decision = RoutingPolicy(
            recipe.policy_id,
            health_freshness_ns=1_000_000,
            load_freshness_ns=10,
        ).decide(snapshot, request_index=0, decision_ns=decision_ns)
        assert decision.mode == "LOAD"
        foreground = await run_evaluation(
            recipe.foreground,
            foreground_transport,
            clock=clock,
            start_ns=0,
        )
        stop = asyncio.Event()
        stop.set()
        background = await run_evaluation(
            recipe.background,
            background_transport,
            clock=clock,
            stop=stop,
            start_ns=0,
        )
        recovery = trial.recovery([decision], foreground, background)
        assert recovery["first_restored_publication_ns"] == restored
        expected = decision_ns if restored_sample_age == 10 else None
        assert recovery["first_decision_using_restored_load_ns"] == expected
        assert recovery["first_dispatch_using_restored_load_ns"] == expected

    asyncio.run(scenario())


def test_simultaneous_driver_completion_and_worker_error_blocks_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver_finished = False
    worker_failed = False

    async def failing_poll(
        self: _Trial, endpoint: EndpointId, channel: Channel
    ) -> None:
        nonlocal worker_failed
        if endpoint == "endpoint-a" and channel == "INDEPENDENT_LOAD":
            worker_failed = True
            raise RuntimeError("SECRET observer failure")
        await self.poll_stop.wait()

    async def finishing_driver(self: _Trial, selector: _Selector) -> None:
        nonlocal driver_finished
        driver_finished = True
        self.status = "COMPLETED"

    # Both tasks finish in the initial ready batch, before the coordinator resumes.
    monkeypatch.setattr(_Trial, "poll", failing_poll)
    monkeypatch.setattr(_Trial, "drive", finishing_driver)

    async def scenario() -> None:
        foreground, background, router, observer = Stream(), Stream(), Probe(), Probe()
        with pytest.raises(EvaluationError, match="trial or cleanup") as error:
            await run_routing_fault(
                config(),
                foreground,
                background,
                router,
                observer,
                clock=Clock(),
            )
        assert driver_finished and worker_failed
        assert "SECRET" not in str(error.value)
        assert foreground.calls == background.calls == 0
        assert (
            foreground.closed
            == background.closed
            == router.closed
            == observer.closed
            == 1
        )

    asyncio.run(scenario())


def test_acquisition_cancellation_during_timer_cleanup_is_not_swallowed() -> None:
    class CleanupClock(Clock):
        def __init__(self) -> None:
            super().__init__()
            self.timer_closing = asyncio.Event()
            self.release_timer = asyncio.Event()
            self.timer_finished = False

        async def sleep_until(self, at: int) -> None:
            try:
                await asyncio.Future()
            finally:
                self.timer_closing.set()
                await self.release_timer.wait()
                self.timer_finished = True

    async def scenario() -> None:
        clock = CleanupClock()
        acquisition = asyncio.create_task(
            _acquire(
                Probe(),
                "http://127.0.0.1:8001",
                "/health",
                clock,
                10,
                100_000_000,
            )
        )
        await clock.timer_closing.wait()
        acquisition.cancel()
        await asyncio.sleep(0)
        clock.release_timer.set()
        with pytest.raises(asyncio.CancelledError):
            await acquisition
        assert acquisition.cancelled()
        assert clock.timer_finished

    asyncio.run(scenario())


@pytest.mark.parametrize("expired_at_entry", [False, True])
def test_probe_cannot_start_after_acquisition_deadline(expired_at_entry: bool) -> None:
    class CountingProbe(Probe):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def get(self, origin: str, path: str) -> ProbeResponse:
            self.calls += 1
            return await super().get(origin, path)

    async def scenario() -> None:
        clock = Clock(10 if expired_at_entry else 0)
        probe = CountingProbe()
        acquisition = asyncio.create_task(
            _acquire(probe, "http://127.0.0.1:8001", "/health", clock, 10, 100_000_000)
        )

        async def consume_remaining_time() -> None:
            # This already-ready task runs after acquisition queues the request,
            # but before that queued request gets its own first scheduling turn.
            clock.now = 10

        advancing = asyncio.create_task(consume_remaining_time())
        with pytest.raises(TimeoutError):
            await acquisition
        await advancing
        assert probe.calls == 0

    asyncio.run(scenario())


def test_external_stop_at_freeze_prevents_new_background_dispatch() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        stop = asyncio.Event()
        data = payload()
        data["background"]["offers"][0]["scheduled_ns"] = 40 * MS
        recipe = load_routing_config_bytes(json.dumps(data).encode())
        task = asyncio.create_task(
            run_routing_fault(recipe, *owned, clock=clock, stop=stop)
        )
        await settle()
        for at in range(5, 40, 5):
            await advance(clock, at * MS)
        clock.advance_to(40 * MS)
        # Both the phase driver and stop watcher become ready in the same turn.
        # No background dispatch has occurred, and stop is set before either runs.
        stop.set()
        await settle()
        assert task.done()
        result = await task
        assert result.status == "CANCELLED"
        assert owned[1].calls == []
        assert all(row.attempts == 0 for row in result.background.records)
        assert all(row.outcome == "CANCELLED" for row in result.background.records)
        assert all(client.closed for client in owned)
        assert clock.waiters == {}

    asyncio.run(scenario())
