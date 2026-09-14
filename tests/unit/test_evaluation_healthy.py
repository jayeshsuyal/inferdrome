"""Healthy observation sessions without a hidden background/fault controller."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.fault_config import load_routing_config_bytes
from inferdrome.evaluation.faults import ObservationSession, _Selector, _Trial
from inferdrome.evaluation.healthy import _HealthySession, run_routing_healthy
from inferdrome.evaluation.healthy_config import (
    HealthyRoutingConfig,
    load_healthy_config_bytes,
)
from inferdrome.evaluation.observations import (
    ProbeError,
    ProbeResponse,
    RouterObservations,
)
from inferdrome.evaluation.policies import (
    POLICY_IDS,
    HealthObservation,
    LoadObservation,
    PolicyId,
    RoutingPolicy,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_evaluation_faults import MS, Stream, finish
from tests.unit.test_evaluation_faults import payload as fault_payload
from tests.unit.test_evaluation_runner import ManualClock, advance, settle


def payload(policy: PolicyId = "evaluation_freshness_fallback_v1") -> dict:
    source = fault_payload(policy)
    return {
        "schema_version": "inferdrome.evaluation-healthy-config.v1",
        "foreground": source["foreground"],
        "policy_id": policy,
        "telemetry": source["telemetry"],
    }


def config(
    policy: PolicyId = "evaluation_freshness_fallback_v1",
) -> HealthyRoutingConfig:
    return load_healthy_config_bytes(json.dumps(payload(policy)).encode())


class Probe:
    """Independent client whose data does not refer to foreground execution."""

    def __init__(self, clock: ManualClock, scores: tuple[int, int]) -> None:
        self.clock = clock
        self.scores = scores
        self.mode = "VALID"
        self.calls: list[tuple[int, str, str]] = []
        self.active = 0
        self.closed = False
        self.close_calls = 0
        self.close_started = asyncio.Event()
        self.close_release: asyncio.Event | None = None
        self.close_finished = False
        self.close_error = False

    async def get(self, origin: str, path: str) -> ProbeResponse:
        self.calls.append((self.clock.elapsed_ns, origin, path))
        self.active += 1
        try:
            if self.mode == "TIMEOUT":
                await asyncio.Event().wait()
            if self.mode == "TRANSPORT_ERROR":
                raise ProbeError("sanitized")
            if self.mode == "INTERNAL_ERROR":
                raise RuntimeError("private acquisition failure")
            if self.mode == "HTTP_ERROR":
                return ProbeResponse(503, b"private error body")
            if path == "/health":
                return ProbeResponse(200, b"")
            if self.mode == "MISSING":
                return ProbeResponse(200, b"# selected metric absent\n")
            if self.mode == "MALFORMED":
                return ProbeResponse(200, b"vllm:num_requests_running 1\n")
            score = self.scores[0 if origin.endswith("8001") else 1]
            return ProbeResponse(
                200,
                (
                    'vllm:num_requests_running{model_name="private-model",engine="0"} '
                    f"{score}\n"
                    'vllm:num_requests_waiting{model_name="private-model",engine="0"}'
                    " 0\n"
                ).encode(),
            )
        finally:
            self.active -= 1

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            assert self.active == 0
            if self.close_release is not None:
                await self.close_release.wait()
            if self.close_error:
                raise RuntimeError("private cleanup failure")
            self.closed = True
        finally:
            self.close_finished = True


def clients(clock: ManualClock) -> tuple[Stream, Probe, Probe]:
    return Stream(clock), Probe(clock, (1, 3)), Probe(clock, (90, 0))


def assert_quiescent(clock: ManualClock, owned: tuple[Stream, Probe, Probe]) -> None:
    assert all(client.closed and client.active == 0 for client in owned)
    assert all(probe.close_calls == 1 for probe in owned[1:])
    assert not clock.waiters
    assert asyncio.all_tasks() == {asyncio.current_task()}


@pytest.mark.parametrize("policy", POLICY_IDS)
def test_healthy_routes_with_live_six_channels_and_never_injects_fault(
    policy: PolicyId, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("healthy session invoked a publication fault")

    monkeypatch.setattr(RouterObservations, "freeze", unexpected)
    monkeypatch.setattr(RouterObservations, "restore", unexpected)

    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        parsed = config(policy)
        task = asyncio.create_task(run_routing_healthy(parsed, *owned, clock=clock))
        await finish(clock)
        result = await task
        assert result.status == "COMPLETED"
        assert len(result.foreground.records) == len(parsed.foreground.offers) == 6
        assert all(row.outcome == "SUCCESS" for row in result.foreground.records)
        assert len(owned[0].calls) == 6
        assert len(result.decisions) == 6
        assert [row.decision_ns for row in result.decisions] == [
            offer.scheduled_ns for offer in parsed.foreground.offers
        ]
        assert {row.channel for row in result.observations} == {
            "HEALTH",
            "ROUTER_LOAD",
            "INDEPENDENT_LOAD",
        }
        assert {row.endpoint_id for row in result.observations} == {
            "endpoint-a",
            "endpoint-b",
        }
        assert all(
            row.published_to_router is (row.channel != "INDEPENDENT_LOAD")
            if row.channel != "INDEPENDENT_LOAD"
            else row.published_to_router is None
            for row in result.observations
        )
        assert {path for _, _, path in owned[1].calls} == {"/health", "/metrics"}
        assert {path for _, _, path in owned[2].calls} == {"/metrics"}
        assert [event.kind for event in result.events] == ["WARMUP_PASSED"]
        assert result.events[0].observed_ns == 20 * MS
        assert all(
            endpoint.load is not None
            and endpoint.load.running == (1 if index == 0 else 3)
            for decision in result.decisions
            for index, endpoint in enumerate(decision.snapshot.endpoints)
        )
        assert result.decisions[-1].snapshot.endpoints[0].load.sequence > 1
        if policy == "evaluation_round_robin_v1":
            assert [row.selected_endpoint_id for row in result.decisions] == [
                "endpoint-a",
                "endpoint-b",
                "endpoint-a",
                "endpoint-b",
                "endpoint-a",
                "endpoint-b",
            ]
        else:
            assert {row.selected_endpoint_id for row in result.decisions} == {
                "endpoint-a"
            }
        exported = result.to_dict()
        assert set(exported) == {
            "config_sha256",
            "policy_id",
            "status",
            "foreground",
            "telemetry_bounds",
            "decisions",
            "observations",
            "events",
            "elapsed_ns",
            "schema_version",
            "evidence_class",
            "evidence_eligible",
            "endpoint_identity",
            "observation_clock",
            "observer_isolation",
            "cleanup",
        }
        assert result.config_sha256 == sha256_digest(
            canonical_json_bytes(parsed.model_dump(mode="json"))
        )
        assert result.cleanup == "CLIENT_TASKS_AND_CONNECTIONS_CLOSED"
        assert not result.evidence_eligible
        serialized = json.dumps(exported)
        for private in (
            "private-model",
            "private foreground",
            "127.0.0.1",
            "generated",
        ):
            assert private not in serialized
        assert_quiescent(clock, owned)

    asyncio.run(scenario())


@pytest.mark.parametrize("client_index", [1, 2])
@pytest.mark.parametrize(
    "mode", ["MISSING", "MALFORMED", "HTTP_ERROR", "TIMEOUT", "TRANSPORT_ERROR"]
)
def test_probe_failure_prevents_warmup_and_retains_all_offers(
    client_index: int, mode: str
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        owned[client_index].mode = mode
        task = asyncio.create_task(run_routing_healthy(config(), *owned, clock=clock))
        await finish(clock, 25)
        result = await task
        assert result.status == "WARMUP_FAILED"
        assert len(result.foreground.records) == 6
        assert all(row.outcome == "CANCELLED" for row in result.foreground.records)
        assert all(row.dispatch_ns is None for row in result.foreground.records)
        assert owned[0].calls == []
        assert result.decisions == ()
        assert [event.kind for event in result.events] == ["WARMUP_FAILED"]
        assert any(row.status == mode for row in result.observations)
        assert_quiescent(clock, owned)

    asyncio.run(scenario())


@pytest.mark.parametrize("mechanism", ["initial", "warmup", "event", "task"])
def test_stop_preserves_population_and_closes_exactly_three_clients(
    mechanism: str,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        owned[0].block = True
        stop = asyncio.Event()
        if mechanism == "initial":
            stop.set()
        task = asyncio.create_task(
            run_routing_healthy(config(), *owned, clock=clock, stop=stop)
        )
        if mechanism != "initial":
            await finish(clock, 5 if mechanism == "warmup" else 65)
            if mechanism == "task":
                task.cancel()
            else:
                stop.set()
        await settle()
        result = await task
        assert result.status == "CANCELLED"
        assert len(result.foreground.records) == 6
        assert all(row.outcome == "CANCELLED" for row in result.foreground.records)
        if mechanism == "initial":
            assert all(not client.calls for client in owned)
            assert not result.observations
        if mechanism in {"initial", "warmup"}:
            assert not result.decisions
            assert not result.events
        if mechanism == "task":
            assert not stop.is_set()  # The caller's event remains caller-owned.
        assert_quiescent(clock, owned)

    asyncio.run(scenario())


@pytest.mark.parametrize("client_index", [1, 2])
def test_internal_probe_error_blocks_result_and_closes_clients(
    client_index: int,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        owned[client_index].mode = "INTERNAL_ERROR"
        task = asyncio.create_task(run_routing_healthy(config(), *owned, clock=clock))
        await settle()
        with pytest.raises(EvaluationError, match=r"^routing trial or cleanup failed$"):
            await task
        assert_quiescent(clock, owned)

    asyncio.run(scenario())


def test_repeated_cancel_during_probe_close_waits_for_completion() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        release = asyncio.Event()
        for probe in owned[1:]:
            probe.close_release = release
        task = asyncio.create_task(run_routing_healthy(config(), *owned, clock=clock))
        await finish(clock, 170)
        assert all(probe.close_started.is_set() for probe in owned[1:])
        for _ in range(3):
            task.cancel()
            await settle()
            assert not task.done()
            assert all(not probe.close_finished for probe in owned[1:])
        release.set()
        result = await asyncio.wait_for(task, 1)
        assert result.status == "CANCELLED"
        assert all(probe.close_finished for probe in owned[1:])
        assert_quiescent(clock, owned)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "timeout"])
def test_probe_close_failure_blocks_cleanup_claim_without_orphan_tasks(
    failure: str,
) -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        if failure == "exception":
            owned[2].close_error = True
        else:
            owned[2].close_release = asyncio.Event()
        task = asyncio.create_task(run_routing_healthy(config(), *owned, clock=clock))
        await finish(clock, 170)
        with pytest.raises(EvaluationError, match=r"^routing trial or cleanup failed$"):
            await asyncio.wait_for(task, 1)
        assert owned[0].closed and owned[1].closed
        assert owned[2].close_finished and not owned[2].closed
        assert all(client.active == 0 for client in owned)
        assert not clock.waiters
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_expected_probe_failure_after_warmup_is_reported_without_hidden_fault() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        owned = clients(clock)
        task = asyncio.create_task(run_routing_healthy(config(), *owned, clock=clock))
        await finish(clock, 35)
        owned[1].mode = "HTTP_ERROR"
        for milliseconds in range(40, 175, 5):
            await advance(clock, milliseconds * MS)
        result = await task
        assert result.status == "COMPLETED"
        assert result.foreground.records[0].outcome == "SUCCESS"
        assert all(
            row.outcome == "REJECTED_ROUTE" for row in result.foreground.records[1:]
        )
        assert all(row.reason == "NO_HEALTHY_ENDPOINT" for row in result.decisions[1:])
        assert any(row.status == "HTTP_ERROR" for row in result.observations)
        assert [event.kind for event in result.events] == ["WARMUP_PASSED"]
        assert_quiescent(clock, owned)

    asyncio.run(scenario())


def test_same_healthy_plan_restarts_policy_and_observation_state() -> None:
    async def scenario() -> list:
        results = []
        parsed = config("evaluation_round_robin_v1")
        for _ in range(2):
            clock = ManualClock()
            owned = clients(clock)
            task = asyncio.create_task(run_routing_healthy(parsed, *owned, clock=clock))
            await finish(clock)
            results.append((await task).to_dict())
            assert_quiescent(clock, owned)
        return results

    results = asyncio.run(scenario())
    assert results[0] == results[1]


def test_healthy_configuration_round_trip_and_exact_preflight_boundary() -> None:
    parsed = config()
    assert load_healthy_config_bytes(parsed.model_dump_json().encode()) == parsed
    assert parsed.telemetry.max_observations == 132
    source = payload()
    source["foreground"]["bounds"].update(max_requests=4000, max_content_events=125)
    assert load_healthy_config_bytes(json.dumps(source).encode())


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p.update(background={}),
        lambda p: p.update(fault={}),
        lambda p: p.update(observer_for_policy=True),
        lambda p: p.update(policy_id="round_robin_v1"),
        lambda p: p["telemetry"].update(interval_ns=True),
        lambda p: p["telemetry"].update(load_freshness_ns=1.0),
        lambda p: p["telemetry"].update(max_observations=131),
        lambda p: p["telemetry"].update(warmup_ns=200 * MS),
        lambda p: p["foreground"]["offers"][0].update(scheduled_ns=19 * MS),
        lambda p: p["foreground"]["bounds"].update(max_requests=4001),
        lambda p: p["foreground"]["bounds"].update(
            max_requests=4000, max_content_events=126
        ),
    ],
)
def test_healthy_configuration_rejects_faults_and_resource_overflow(
    edit: Callable[[dict], None],
) -> None:
    source = payload()
    edit(source)
    with pytest.raises(
        EvaluationError, match=r"^healthy routing configuration violates its contract$"
    ):
        load_healthy_config_bytes(json.dumps(source).encode())


@pytest.mark.parametrize(
    "content", [b"", b"\xff", b"[]", b'{"private":NaN}', b'{"private":1,"private":2}']
)
def test_healthy_loader_sanitizes_malformed_json(content: bytes) -> None:
    with pytest.raises(
        EvaluationError, match=r"^healthy routing configuration violates its contract$"
    ):
        load_healthy_config_bytes(content)


@pytest.mark.parametrize("scenario", ["HEALTHY", "STALE_LOAD"])
@pytest.mark.parametrize("offset_ns", [0, 1])
def test_warmup_event_records_the_exact_evaluated_freshness_instant(
    scenario: str, offset_ns: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ForegroundReached(Exception):
        pass

    class TickingClock(ManualClock):
        def now_ns(self) -> int:
            current = self.elapsed_ns
            self.elapsed_ns += 1
            return current

        async def sleep_until(self, absolute_ns: int) -> None:
            assert absolute_ns == 20 * MS

    def foreground_reached(self: ObservationSession, selector: _Selector) -> None:
        raise ForegroundReached

    monkeypatch.setattr(ObservationSession, "start_foreground", foreground_reached)

    async def run() -> None:
        clock = TickingClock(epoch_ns=0)
        clock.elapsed_ns = 20 * MS + offset_ns
        owned = clients(clock)
        source = payload() if scenario == "HEALTHY" else fault_payload()
        source["telemetry"].update(
            health_freshness_ns=20 * MS, load_freshness_ns=20 * MS
        )
        session: ObservationSession
        if scenario == "HEALTHY":
            session = _HealthySession(
                load_healthy_config_bytes(json.dumps(source).encode()),
                *owned,
                clock,
                0,
            )
        else:
            session = _Trial(
                load_routing_config_bytes(json.dumps(source).encode()),
                owned[0],
                Stream(clock),
                owned[1],
                owned[2],
                clock,
                0,
            )
        for endpoint in ("endpoint-a", "endpoint-b"):
            session.gate.publish_health(
                endpoint, HealthObservation(0, 0, 0, 0, "VALID", True)
            )
            sample = LoadObservation(0, 0, 0, 0, "VALID", 0, 0)
            session.gate.publish_load(endpoint, sample)
            session.independent_latest_valid[endpoint] = sample
        selector = _Selector(
            RoutingPolicy(
                session.config.policy_id,
                health_freshness_ns=20 * MS,
                load_freshness_ns=20 * MS,
            ),
            session.gate,
        )
        if offset_ns == 0:
            with pytest.raises(ForegroundReached):
                await session.drive(selector)
        else:
            await session.drive(selector)
        assert len(session.events) == 1
        event = session.events[0]
        assert event.observed_ns == 20 * MS + offset_ns
        assert event.kind == ("WARMUP_PASSED" if offset_ns == 0 else "WARMUP_FAILED")
        assert session.warmed_up(event.observed_ns) is (offset_ns == 0)
        assert clock.elapsed_ns == event.observed_ns + 1
        assert not session.warmed_up()  # The next clock tick is already stale.

    asyncio.run(run())
