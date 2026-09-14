"""One bounded local stale-load trial with a logically independent observer.

The selector receives only RouterObservations. Independent samples and discarded
publication attempts belong to the report owner and never enter route decisions.
Injected transports/clocks are trusted, cancellable code, not config plugins.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import Literal

from inferdrome.evaluation.contracts import EndpointId, EvaluationError
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.observations import (
    MalformedLoad,
    MissingLoad,
    ProbeError,
    ProbeLimitError,
    ProbeResponse,
    ProbeTransport,
    RouterObservations,
    parse_load,
)
from inferdrome.evaluation.policies import (
    HealthObservation,
    LoadObservation,
    PolicyDecision,
    RoutingPolicy,
    SampleStatus,
)
from inferdrome.evaluation.runner import (
    Clock,
    EvaluationResult,
    SystemClock,
    Transport,
    run_evaluation,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

Channel = Literal["HEALTH", "ROUTER_LOAD", "INDEPENDENT_LOAD"]
TrialStatus = Literal["COMPLETED", "WARMUP_FAILED", "CANCELLED"]


@dataclass(frozen=True)
class ObservationRecord:
    channel: Channel
    endpoint_id: EndpointId
    sequence: int
    started_ns: int
    completed_ns: int
    published_ns: int
    status: SampleStatus
    published_to_router: bool | None
    healthy: bool | None
    running: int | None
    waiting: int | None
    response_body_bytes: int | None


@dataclass(frozen=True)
class FaultEvent:
    kind: str
    observed_ns: int


@dataclass(frozen=True)
class RoutingFaultResult:
    config_sha256: str
    policy_id: str
    status: TrialStatus
    foreground: EvaluationResult
    background: EvaluationResult
    telemetry_bounds: dict[str, object]
    fault_schedule: dict[str, object]
    decisions: tuple[PolicyDecision, ...]
    observations: tuple[ObservationRecord, ...]
    events: tuple[FaultEvent, ...]
    recovery: dict[str, object]
    elapsed_ns: int
    schema_version: str = "inferdrome.evaluation-routing-result.v1"
    evidence_class: str = "LOCAL_MEASUREMENT_ONLY"
    evidence_eligible: bool = False
    endpoint_identity: str = "UNVERIFIED"
    observation_clock: str = "SHARED_TRIAL_MONOTONIC_ACQUISITION_START"
    observer_isolation: str = "SEPARATE_CLIENT_AND_DATAFLOW_NOT_SECURITY_SANDBOX"
    actual_overload: str = "NOT_ESTABLISHED_BY_FAULT_SCHEDULE"
    cleanup: str = "PUBLICATION_RESTORED_CLIENT_TASKS_AND_CONNECTIONS_CLOSED"

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["foreground"] = self.foreground.to_dict()
        result["background"] = self.background.to_dict()
        return result


class _Selector:
    """Small route adapter with no trial, transport or independent-sink handle."""

    def __init__(self, policy: RoutingPolicy, observations: RouterObservations) -> None:
        self.policy = policy
        self.observations = observations
        self.decisions: list[PolicyDecision] = []

    def __call__(self, request_index: int, decision_ns: int) -> EndpointId | None:
        decision = self.policy.decide(
            self.observations.snapshot(),
            request_index=request_index,
            decision_ns=decision_ns,
        )
        self.decisions.append(decision)
        return decision.selected_endpoint_id


class _StopEvent(asyncio.Event):
    """Local cancellation plus an immediately visible caller cancellation flag."""

    def __init__(self, external: asyncio.Event) -> None:
        super().__init__()
        self.external = external

    def is_set(self) -> bool:
        return super().is_set() or self.external.is_set()

    async def wait(self) -> Literal[True]:
        if self.is_set():
            return True
        local = asyncio.create_task(super().wait())
        external = asyncio.create_task(self.external.wait())
        try:
            await asyncio.wait([local, external], return_when=asyncio.FIRST_COMPLETED)
        finally:
            local.cancel()
            external.cancel()
            await asyncio.gather(local, external, return_exceptions=True)
        return True


async def _settle(
    tasks: Sequence[asyncio.Task[object]],
    timeout_ns: int,
    *,
    cancel: bool = False,
    on_interrupt: Callable[[], None] | None = None,
) -> bool:
    """Join owned tasks under one real-time bound, including repeated interrupts."""
    if not tasks:
        return False
    interrupted = False
    if cancel:
        for task in tasks:
            if not task.done():
                task.cancel()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ns / 1_000_000_000
    while True:
        try:
            _, unfinished = await asyncio.wait(
                tasks, timeout=max(0, deadline - loop.time())
            )
            break
        except asyncio.CancelledError:
            interrupted = True
            if on_interrupt is not None:
                on_interrupt()
    for task in tasks:
        if task.done() and not task.cancelled():
            task.exception()
    if unfinished:
        raise EvaluationError("routing task cleanup exceeded its bound")
    return interrupted


async def _acquire(
    transport: ProbeTransport,
    origin: str,
    path: str,
    clock: Clock,
    deadline_ns: int,
    cleanup_ns: int,
    stop: asyncio.Event | None = None,
) -> ProbeResponse:
    async def guarded_request() -> ProbeResponse:
        if stop is not None and stop.is_set():
            raise asyncio.CancelledError
        if clock.now_ns() >= deadline_ns:
            raise TimeoutError
        return await transport.get(origin, path)

    if clock.now_ns() >= deadline_ns:
        raise TimeoutError
    request = asyncio.create_task(guarded_request())
    timer = asyncio.create_task(clock.sleep_until(deadline_ns))
    try:
        await asyncio.wait([request, timer], return_when=asyncio.FIRST_COMPLETED)
        # Deadline wins a completion tie. No slow acquisition gains fresh age.
        if clock.now_ns() >= deadline_ns:
            raise TimeoutError
        if not request.done():
            raise EvaluationError("probe timer woke before its deadline")
        return request.result()
    finally:
        if await _settle([request, timer], cleanup_ns, cancel=True):
            raise asyncio.CancelledError


@dataclass
class _Trial:
    config: RoutingFaultConfig
    foreground_transport: Transport
    background_transport: Transport
    router_probe: ProbeTransport
    observer_probe: ProbeTransport
    clock: Clock
    epoch_ns: int
    gate: RouterObservations = field(default_factory=RouterObservations)
    foreground_stop: asyncio.Event = field(default_factory=asyncio.Event)
    background_stop: asyncio.Event = field(default_factory=asyncio.Event)
    poll_stop: asyncio.Event = field(default_factory=asyncio.Event)
    foreground_task: asyncio.Task[EvaluationResult] | None = None
    background_task: asyncio.Task[EvaluationResult] | None = None
    observations: list[ObservationRecord] = field(default_factory=list)
    events: list[FaultEvent] = field(default_factory=list)
    independent_latest_valid: dict[EndpointId, LoadObservation] = field(
        default_factory=dict
    )
    status: TrialStatus = "CANCELLED"

    def now(self) -> int:
        return self.clock.now_ns() - self.epoch_ns

    def event(self, kind: str, observed_ns: int | None = None) -> None:
        self.events.append(
            FaultEvent(kind, self.now() if observed_ns is None else observed_ns)
        )

    def interrupt(self) -> None:
        self.status = "CANCELLED"
        self.foreground_stop.set()
        self.background_stop.set()
        self.poll_stop.set()

    async def poll(self, endpoint: EndpointId, channel: Channel) -> None:
        bounds = self.config.telemetry
        end = (
            self.config.foreground.bounds.duration_ns
            + self.config.foreground.bounds.drain_ns
        )
        transport = (
            self.observer_probe if channel == "INDEPENDENT_LOAD" else self.router_probe
        )
        index = 0 if endpoint == "endpoint-a" else 1
        origin = self.config.foreground.endpoints[index].origin
        sequence = 0
        while not self.poll_stop.is_set() and self.now() < end:
            started = self.now()
            status: SampleStatus = "VALID"
            healthy = None
            running = waiting = None
            body_bytes = None
            interrupted = False
            try:
                response = await _acquire(
                    transport,
                    origin,
                    "/health" if channel == "HEALTH" else "/metrics",
                    self.clock,
                    self.epoch_ns + min(end, started + bounds.poll_timeout_ns),
                    bounds.cleanup_timeout_ns,
                    self.poll_stop,
                )
                body_bytes = len(response.body)
                if body_bytes > bounds.max_response_bytes:
                    raise ProbeLimitError("probe response exceeds its byte bound")
                if response.status != 200:
                    status = "HTTP_ERROR"
                elif channel == "HEALTH":
                    healthy = True
                else:
                    running, waiting = parse_load(
                        response.body, model=self.config.foreground.model, engine="0"
                    )
            except TimeoutError:
                status = "TIMEOUT"
            except MissingLoad:
                status = "MISSING"
            except (MalformedLoad, ProbeLimitError):
                status = "MALFORMED"
            except ProbeError:
                status = "TRANSPORT_ERROR"
            except asyncio.CancelledError:
                status = "CANCELLED"
                interrupted = True
            completed = self.now()
            published = self.now()
            publication: bool | None = None
            if channel == "HEALTH":
                self.gate.publish_health(
                    endpoint,
                    HealthObservation(
                        sequence, started, completed, published, status, healthy
                    ),
                )
                publication = True
            else:
                sample = LoadObservation(
                    sequence, started, completed, published, status, running, waiting
                )
                if channel == "ROUTER_LOAD":
                    publication = self.gate.publish_load(endpoint, sample)
                elif status == "VALID":
                    self.independent_latest_valid[endpoint] = sample
            if len(self.observations) >= bounds.max_observations:
                raise EvaluationError("routing observation storage exceeded its bound")
            self.observations.append(
                ObservationRecord(
                    channel,
                    endpoint,
                    sequence,
                    started,
                    completed,
                    published,
                    status,
                    publication,
                    healthy,
                    running if publication is not False else None,
                    waiting if publication is not False else None,
                    body_bytes,
                )
            )
            sequence += 1
            if interrupted:
                raise asyncio.CancelledError
            # Skip missed intervals; no backlog or burst of catch-up scrapes.
            next_ns = (self.now() // bounds.interval_ns + 1) * bounds.interval_ns
            await self.clock.sleep_until(self.epoch_ns + min(next_ns, end))
        # Keep a normally finished channel alive until the owner ends the trial.
        await self.poll_stop.wait()

    def warmed_up(self) -> bool:
        now = self.now()
        bounds = self.config.telemetry
        for endpoint in self.gate.snapshot().endpoints:
            health, load = endpoint.health, endpoint.load
            independent = self.independent_latest_valid.get(endpoint.endpoint_id)
            if (
                health is None
                or health.status != "VALID"
                or health.healthy is not True
                or not 0 <= now - health.started_ns <= bounds.health_freshness_ns
                or load is None
                or not 0 <= now - load.started_ns <= bounds.load_freshness_ns
                or independent is None
                or not 0 <= now - independent.started_ns <= bounds.load_freshness_ns
            ):
                return False
        return True

    def start_foreground(self, selector: _Selector) -> None:
        self.foreground_task = asyncio.create_task(
            run_evaluation(
                self.config.foreground,
                self.foreground_transport,
                clock=self.clock,
                stop=self.foreground_stop,
                selector=selector,
                start_ns=self.epoch_ns,
            )
        )

    def start_background(self) -> None:
        self.background_task = asyncio.create_task(
            run_evaluation(
                self.config.background,
                self.background_transport,
                clock=self.clock,
                stop=self.background_stop,
                start_ns=self.epoch_ns,
                stop_at_ns=self.config.fault.background_stop_ns,
            )
        )

    async def drive(self, selector: _Selector) -> None:
        await self.clock.sleep_until(self.epoch_ns + self.config.telemetry.warmup_ns)
        if self.foreground_stop.is_set():
            self.interrupt()
            return
        if not self.warmed_up():
            self.status = "WARMUP_FAILED"
            self.event("WARMUP_FAILED")
            return
        self.event("WARMUP_PASSED")
        self.start_foreground(selector)
        timing = self.config.fault
        await self.clock.sleep_until(self.epoch_ns + timing.freeze_start_ns)
        if self.foreground_stop.is_set():
            self.interrupt()
            return
        freeze = self.now()
        self.gate.freeze(timing.target_endpoint_id, freeze)
        self.event("FREEZE_STARTED", freeze)
        self.start_background()
        self.event("BACKGROUND_STARTED")
        await self.clock.sleep_until(self.epoch_ns + timing.restore_ns)
        if self.foreground_stop.is_set():
            self.interrupt()
            return
        restore = self.now()
        self.gate.restore(restore)
        self.event("TELEMETRY_RESTORED", restore)
        await self.clock.sleep_until(self.epoch_ns + timing.background_stop_ns)
        self.background_stop.set()
        self.event("BACKGROUND_STOP_REQUESTED")
        assert self.foreground_task is not None and self.background_task is not None
        await asyncio.wait([self.foreground_task, self.background_task])
        self.status = "COMPLETED"

    def recovery(
        self,
        decisions: list[PolicyDecision],
        foreground: EvaluationResult,
        background: EvaluationResult,
    ) -> dict[str, object]:
        timing = {event.kind: event.observed_ns for event in self.events}
        restored = timing.get("TELEMETRY_RESTORED")
        target = self.config.fault.target_endpoint_id
        target_index = 0 if target == "endpoint-a" else 1
        publications = [
            row.published_ns
            for row in self.observations
            if restored is not None
            and row.channel == "ROUTER_LOAD"
            and row.endpoint_id == target
            and row.published_to_router is True
            and row.status == "VALID"
            and row.started_ns >= restored
        ]
        usable = [
            decision
            for decision in decisions
            if restored is not None
            and decision.mode == "LOAD"
            and target in decision.eligible_endpoint_ids
            and decision.load_states[target_index] == "FRESH"
            and (sample := decision.snapshot.endpoints[target_index].load) is not None
            and sample.started_ns >= restored
        ]
        first_decision = usable[0] if usable else None
        # A dispatch recovery exists only when this very decision was dispatched.
        dispatches = [
            dispatch
            for decision in usable
            if (dispatch := foreground.records[decision.request_index].dispatch_ns)
            is not None
        ]
        background_dispatches = [
            row.dispatch_ns for row in background.records if row.dispatch_ns is not None
        ]
        return {
            "freeze_started_ns": timing.get("FREEZE_STARTED"),
            "telemetry_restored_ns": restored,
            "first_restored_publication_ns": min(publications, default=None),
            "first_decision_using_restored_load_ns": (
                first_decision.decision_ns if first_decision else None
            ),
            "first_dispatch_using_restored_load_ns": min(dispatches, default=None),
            "first_background_dispatch_ns": min(background_dispatches, default=None),
            "background_active_at_restore": (
                None
                if restored is None
                else any(
                    row.dispatch_ns is not None
                    and row.dispatch_ns <= restored < row.terminal_ns
                    for row in background.records
                )
            ),
            "load_recovery_applicable": (
                self.config.policy_id != "evaluation_round_robin_v1"
            ),
            "missing_timestamps": "UNOBSERVED_OR_CENSORED_NOT_ZERO",
        }


async def run_routing_fault(
    config: RoutingFaultConfig,
    foreground_transport: Transport,
    background_transport: Transport,
    router_probe: ProbeTransport,
    observer_probe: ProbeTransport,
    *,
    clock: Clock | None = None,
    stop: asyncio.Event | None = None,
) -> RoutingFaultResult:
    """Own four distinct clients, restore publication and close before returning.

    Warmup failure and external cancellation retain every planned request in its
    separate population. Internal/cleanup failures produce no completed report.
    Population cleanup has its PR1 guard; probe operations, worker joins and
    client closes have real-time guards even when the trial clock is simulated.
    """
    clock = clock or SystemClock()
    stop = stop or asyncio.Event()
    trial = _Trial(
        config,
        foreground_transport,
        background_transport,
        router_probe,
        observer_probe,
        clock,
        clock.now_ns(),
        foreground_stop=_StopEvent(stop),
        background_stop=_StopEvent(stop),
        poll_stop=_StopEvent(stop),
    )
    selector = _Selector(
        RoutingPolicy(
            config.policy_id,
            health_freshness_ns=config.telemetry.health_freshness_ns,
            load_freshness_ns=config.telemetry.load_freshness_ns,
        ),
        trial.gate,
    )
    workers: list[asyncio.Task[object]] = []
    failure = False
    try:
        if (
            len(
                {
                    id(client)
                    for client in (
                        foreground_transport,
                        background_transport,
                        router_probe,
                        observer_probe,
                    )
                }
            )
            != 4
        ):
            raise EvaluationError("routing requires four distinct owned clients")
        if stop.is_set():
            trial.interrupt()
        else:
            endpoints: tuple[EndpointId, ...] = ("endpoint-a", "endpoint-b")
            channels: tuple[Channel, ...] = (
                "HEALTH",
                "ROUTER_LOAD",
                "INDEPENDENT_LOAD",
            )
            for endpoint in endpoints:
                for channel in channels:
                    workers.append(asyncio.create_task(trial.poll(endpoint, channel)))
            driver = asyncio.create_task(trial.drive(selector))
            interrupt = asyncio.create_task(stop.wait())
            workers.extend([driver, interrupt])
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                trial.interrupt()
            elif driver in done:
                driver.result()
            else:
                raise EvaluationError("routing observation worker failed")
    except asyncio.CancelledError:
        trial.interrupt()
    except Exception:
        failure = True
    finally:
        try:
            restored = trial.now()
            if trial.gate.restore(restored):
                trial.event("RESTORED_DURING_CLEANUP", restored)
        except EvaluationError:
            failure = True
        trial.foreground_stop.set()
        trial.background_stop.set()
        trial.poll_stop.set()
        # Missing runners still terminalize all offers and close their clients.
        if trial.foreground_task is None:
            trial.start_foreground(selector)
        if trial.background_task is None:
            trial.start_background()
        assert trial.foreground_task is not None and trial.background_task is not None
        cleanup_ns = config.telemetry.cleanup_timeout_ns
        try:
            await _settle(
                workers, 2 * cleanup_ns, cancel=True, on_interrupt=trial.interrupt
            )
        except EvaluationError:
            failure = True
        populations = [trial.foreground_task, trial.background_task]
        replay_cleanup_ns = max(
            config.foreground.bounds.cleanup_timeout_ns,
            config.background.bounds.cleanup_timeout_ns,
        )
        try:
            await _settle(
                populations, 3 * replay_cleanup_ns, on_interrupt=trial.interrupt
            )
        except EvaluationError:
            failure = True
            with suppress(EvaluationError):
                await _settle(
                    populations,
                    replay_cleanup_ns,
                    cancel=True,
                    on_interrupt=trial.interrupt,
                )
        closes = [
            asyncio.create_task(probe.close())
            for probe in (router_probe, observer_probe)
        ]
        try:
            await _settle(closes, cleanup_ns, on_interrupt=trial.interrupt)
        except EvaluationError:
            failure = True
            with suppress(EvaluationError):
                await _settle(
                    closes, cleanup_ns, cancel=True, on_interrupt=trial.interrupt
                )
        if any(
            not task.done() or (not task.cancelled() and task.exception() is not None)
            for task in workers
        ):
            failure = True
        if any(
            not task.done() or task.cancelled() or task.exception() is not None
            for task in [*populations, *closes]
        ):
            failure = True
    if failure:
        raise EvaluationError("routing trial or cleanup failed") from None
    if stop.is_set():
        trial.interrupt()
    foreground = trial.foreground_task.result()
    background = trial.background_task.result()
    return RoutingFaultResult(
        config_sha256=sha256_digest(
            canonical_json_bytes(config.model_dump(mode="json"))
        ),
        policy_id=config.policy_id,
        status=trial.status,
        foreground=foreground,
        background=background,
        telemetry_bounds=config.telemetry.model_dump(),
        fault_schedule=config.fault.model_dump(),
        decisions=tuple(selector.decisions),
        observations=tuple(trial.observations),
        events=tuple(trial.events),
        recovery=trial.recovery(selector.decisions, foreground, background),
        elapsed_ns=trial.now(),
    )
