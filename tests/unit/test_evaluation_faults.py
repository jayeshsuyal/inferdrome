"""Deterministic fault lifecycle, observer separation and combined input bounds."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.fault_config import (
    RoutingFaultConfig,
    load_routing_config_bytes,
)
from inferdrome.evaluation.faults import run_routing_fault
from inferdrome.evaluation.observations import ProbeError, ProbeResponse
from inferdrome.evaluation.policies import POLICY_IDS, PolicyId
from tests.unit.test_evaluation_runner import COMPLETE, ManualClock, advance, settle

MS = 1_000_000


def payload(policy: PolicyId = "evaluation_freshness_fallback_v1") -> dict:
    def population(schedule: list[int], background: bool = False) -> dict:
        return {
            "schema_version": "inferdrome.evaluation-config.v1",
            "source_commit": "a" * 40,
            "model": "private-model",
            "endpoints": [
                {"endpoint_id": "endpoint-a", "origin": "http://127.0.0.1:8001"},
                {"endpoint_id": "endpoint-b", "origin": "http://127.0.0.1:8002"},
            ],
            "bounds": {
                "max_requests": 8,
                "concurrency": 2,
                "max_queue": 2,
                "duration_ns": 200 * MS,
                "drain_ns": 20 * MS,
                "request_timeout_ns": 200 * MS,
                "cleanup_timeout_ns": 20 * MS,
                "max_content_events": 10,
            },
            "offers": [
                {
                    "scheduled_ns": at * MS,
                    "endpoint_id": "endpoint-a",
                    "prompt": "private background"
                    if background
                    else "private foreground",
                }
                for at in schedule
            ],
        }

    return {
        "schema_version": "inferdrome.evaluation-routing-config.v1",
        "foreground": population([30, 60, 80, 110, 130, 170]),
        "background": population([50, 70], True),
        "policy_id": policy,
        "telemetry": {
            "interval_ns": 10 * MS,
            "poll_timeout_ns": 5 * MS,
            "health_freshness_ns": 30 * MS,
            "load_freshness_ns": 30 * MS,
            "warmup_ns": 20 * MS,
            "max_observations": 132,
            "max_response_bytes": 4096,
            "cleanup_timeout_ns": 20 * MS,
        },
        "fault": {
            "target_endpoint_id": "endpoint-a",
            "freeze_start_ns": 40 * MS,
            "restore_ns": 100 * MS,
            "background_stop_ns": 160 * MS,
        },
    }


def config(policy: PolicyId = "evaluation_freshness_fallback_v1") -> RoutingFaultConfig:
    return load_routing_config_bytes(json.dumps(payload(policy)).encode())


class Stream:
    def __init__(self, clock: ManualClock, *, block: bool = False) -> None:
        self.clock = clock
        self.block = block
        self.calls: list[tuple[int, str]] = []
        self.active = 0
        self.closed = False

    async def stream(
        self,
        origin: str,
        body: bytes,
        on_headers: Callable[[int], None],
        on_bytes: Callable[[bytes], None],
    ) -> None:
        self.calls.append((self.clock.elapsed_ns, origin))
        self.active += 1
        try:
            on_headers(200)
            if self.block:
                await asyncio.Event().wait()
            on_bytes(COMPLETE)
        finally:
            self.active -= 1

    async def close(self) -> None:
        assert self.active == 0
        self.closed = True


class Probe:
    def __init__(self, clock: ManualClock, background: Stream) -> None:
        self.clock = clock
        self.background = background
        self.mode = "VALID"
        self.calls: list[tuple[int, str, str]] = []
        self.closed = False
        self.active = 0

    async def get(self, origin: str, path: str) -> ProbeResponse:
        self.calls.append((self.clock.elapsed_ns, origin, path))
        self.active += 1
        try:
            if self.mode == "TIMEOUT":
                await asyncio.Event().wait()
            if self.mode == "TRANSPORT_ERROR":
                raise ProbeError("sanitized")
            if self.mode == "INTERNAL_ERROR":
                raise RuntimeError("private raw failure")
            if self.mode == "HTTP_ERROR":
                return ProbeResponse(503, b"")
            if path == "/health":
                return ProbeResponse(200, b"")
            if self.mode == "MISSING":
                return ProbeResponse(200, b"# no selected metric\n")
            if self.mode == "MALFORMED":
                return ProbeResponse(200, b"vllm:num_requests_running 1\n")
            count = self.background.active * 5 if origin.endswith("8001") else 2
            return ProbeResponse(
                200,
                (
                    'vllm:num_requests_running{model_name="private-model",engine="0"} '
                    f"{count}\n"
                    'vllm:num_requests_waiting{model_name="private-model",engine="0"}'
                    " 0\n"
                ).encode(),
            )
        finally:
            self.active -= 1

    async def close(self) -> None:
        assert self.active == 0
        self.closed = True


def clients(clock: ManualClock) -> tuple[Stream, Stream, Probe, Probe]:
    foreground, background = Stream(clock), Stream(clock, block=True)
    return foreground, background, Probe(clock, background), Probe(clock, background)


async def finish(clock: ManualClock, until: int = 220) -> None:
    await settle()
    for milliseconds in range(5, until + 1, 5):
        await advance(clock, milliseconds * MS)


@pytest.mark.parametrize("policy", POLICY_IDS)
def test_complete_recipe_uses_shared_clock_and_separate_populations(
    policy: PolicyId,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        task = asyncio.create_task(
            run_routing_fault(config(policy), *owned, clock=clock)
        )
        await finish(clock)
        assert task.done()
        result = await task
        assert result.status == "COMPLETED"
        assert len(result.foreground.records) == 6
        assert len(result.background.records) == 2
        assert all(row.outcome == "CANCELLED" for row in result.background.records)
        assert all(row.terminal_ns == 160 * MS for row in result.background.records)
        assert all(
            row.dispatch_ns == row.scheduled_ns for row in result.background.records
        )
        assert len(result.decisions) == 6
        assert result.recovery["freeze_started_ns"] == 40 * MS
        assert result.recovery["telemetry_restored_ns"] == 100 * MS
        assert result.recovery["background_active_at_restore"] is True
        assert result.recovery["first_restored_publication_ns"] == 100 * MS
        assert all(client.closed for client in owned)
        assert not clock.waiters
        assert not result.evidence_eligible
        exported = json.dumps(result.to_dict())
        assert "private-model" not in exported
        assert "private foreground" not in exported
        assert "private background" not in exported
        assert "127.0.0.1" not in exported
        suppressed = [
            row for row in result.observations if row.published_to_router is False
        ]
        assert suppressed
        assert all(row.running is None and row.waiting is None for row in suppressed)
        independent = [
            row
            for row in result.observations
            if row.channel == "INDEPENDENT_LOAD"
            and row.endpoint_id == "endpoint-a"
            and 50 * MS < row.started_ns < 100 * MS
        ]
        assert independent and all(
            row.running and row.running > 0 for row in independent
        )
        if policy == "evaluation_round_robin_v1":
            assert result.recovery["first_decision_using_restored_load_ns"] is None
        else:
            assert result.recovery["first_decision_using_restored_load_ns"] == 110 * MS
        if policy == "evaluation_freshness_fallback_v1":
            assert result.decisions[2].mode == "FALLBACK"
        if policy == "evaluation_fail_closed_v1":
            assert result.foreground.records[2].outcome == "REJECTED_ROUTE"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode", ["HTTP_ERROR", "MISSING", "MALFORMED", "TIMEOUT", "TRANSPORT_ERROR"]
)
def test_failed_warmup_preserves_all_offers_and_dispatches_nothing(mode: str) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        owned[2].mode = mode
        task = asyncio.create_task(run_routing_fault(config(), *owned, clock=clock))
        await finish(clock, 25)
        assert task.done()
        result = await task
        assert result.status == "WARMUP_FAILED"
        assert all(
            row.outcome == "CANCELLED"
            for row in (*result.foreground.records, *result.background.records)
        )
        assert owned[0].calls == owned[1].calls == []
        assert any(row.status == mode for row in result.observations)
        assert result.decisions == ()
        assert all(client.closed for client in owned)
        assert not clock.waiters

    asyncio.run(scenario())


def test_independent_failure_aborts_warmup_before_policy_decisions() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        owned[3].mode = "MISSING"
        task = asyncio.create_task(run_routing_fault(config(), *owned, clock=clock))
        await finish(clock, 25)
        result = await task
        assert result.status == "WARMUP_FAILED"
        assert all(
            row.status == "VALID"
            for row in result.observations
            if row.channel != "INDEPENDENT_LOAD" and row.started_ns < 20 * MS
        )
        assert result.decisions == ()
        assert all(client.closed for client in owned)

    asyncio.run(scenario())


@pytest.mark.parametrize("mechanism", ["event", "task", "initial"])
def test_cancel_restores_publication_and_owns_all_clients(mechanism: str) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        stop = asyncio.Event()
        if mechanism == "initial":
            stop.set()
        task = asyncio.create_task(
            run_routing_fault(config(), *owned, clock=clock, stop=stop)
        )
        if mechanism != "initial":
            await finish(clock, 65)
            if mechanism == "event":
                stop.set()
            else:
                task.cancel()
        await settle()
        assert task.done()
        result = await task
        assert result.status == "CANCELLED"
        assert len(result.foreground.records) + len(result.background.records) == 8
        if mechanism != "initial":
            assert any(
                event.kind == "RESTORED_DURING_CLEANUP" for event in result.events
            )
        else:
            assert owned[2].calls == owned[3].calls == []
        assert all(client.closed for client in owned)
        assert not clock.waiters

    asyncio.run(scenario())


def test_internal_observer_failure_closes_every_client_and_blocks_result() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        owned[3].mode = "INTERNAL_ERROR"
        task = asyncio.create_task(run_routing_fault(config(), *owned, clock=clock))
        await settle()
        assert task.done()
        with pytest.raises(EvaluationError, match="routing trial or cleanup failed"):
            await task
        assert all(client.closed for client in owned)
        assert not clock.waiters

    asyncio.run(scenario())


def test_config_round_trip_and_strict_duplicate_json() -> None:
    parsed = config()
    assert load_routing_config_bytes(parsed.model_dump_json().encode()) == parsed
    with pytest.raises(EvaluationError):
        load_routing_config_bytes(b'{"foreground":{},"foreground":{}}')


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("telemetry", "interval_ns", True),
        ("telemetry", "interval_ns", 999999),
        ("telemetry", "max_observations", 131),
        ("telemetry", "max_observations", 10001),
        ("telemetry", "max_response_bytes", 1048577),
        ("telemetry", "warmup_ns", 40 * MS),
        ("telemetry", "health_freshness_ns", 0),
        ("telemetry", "load_freshness_ns", 1.5),
        ("fault", "freeze_start_ns", 100 * MS),
        ("fault", "restore_ns", 160 * MS),
        ("fault", "background_stop_ns", 201 * MS),
        ("fault", "target_endpoint_id", "third"),
    ],
)
def test_invalid_combined_config(section: str, field: str, value: object) -> None:
    source = payload()
    source[section][field] = value
    with pytest.raises(EvaluationError):
        load_routing_config_bytes(json.dumps(source).encode())


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p["foreground"].update(model="another-model"),
        lambda p: p["background"].update(source_commit="b" * 40),
        lambda p: p["background"]["bounds"].update(duration_ns=201 * MS),
        lambda p: p["background"]["bounds"].update(drain_ns=21 * MS),
        lambda p: p["foreground"]["bounds"].update(concurrency=64),
        lambda p: p["foreground"]["bounds"].update(max_queue=1024),
        lambda p: p["foreground"]["bounds"].update(max_requests=4000),
        lambda p: p["foreground"]["bounds"].update(
            max_requests=1000, max_content_events=501
        ),
        lambda p: p["foreground"]["offers"][0].update(scheduled_ns=0),
        lambda p: p["background"]["offers"][0].update(scheduled_ns=39 * MS),
        lambda p: p["background"]["offers"][-1].update(scheduled_ns=160 * MS),
        lambda p: p["background"]["offers"][0].update(endpoint_id="endpoint-b"),
        lambda p: p.update(policy_id="round_robin_v1"),
        lambda p: p.update(observer_for_policy=True),
    ],
)
def test_cross_population_contracts(edit: Callable[[dict], None]) -> None:
    source = payload()
    edit(source)
    with pytest.raises(EvaluationError):
        load_routing_config_bytes(json.dumps(source).encode())


def test_same_input_plan_can_be_reused_without_policy_state_leakage() -> None:
    async def scenario() -> list[str | None]:
        clock = ManualClock()
        task = asyncio.create_task(
            run_routing_fault(config(), *clients(clock), clock=clock)
        )
        await finish(clock)
        return [row.selected_endpoint_id for row in (await task).decisions]

    assert asyncio.run(scenario()) == asyncio.run(scenario())
