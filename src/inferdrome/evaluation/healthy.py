"""Healthy routing using the shared observation and client-ownership session.

Telemetry warm-up checks fresh health and both load channels; it neither warms
the model nor establishes cache state. This scenario has no background requests,
publication freeze, fault schedule, or load-recovery claim.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field

from inferdrome.evaluation.contracts import EndpointId
from inferdrome.evaluation.faults import (
    FaultEvent,
    ObservationRecord,
    ObservationSession,
    TrialStatus,
    _Selector,
    _StopEvent,
    run_observation_session,
)
from inferdrome.evaluation.healthy_config import HealthyRoutingConfig
from inferdrome.evaluation.observations import ProbeTransport, RouterObservations
from inferdrome.evaluation.policies import LoadObservation, PolicyDecision
from inferdrome.evaluation.runner import Clock, EvaluationResult, SystemClock, Transport
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest


@dataclass(frozen=True)
class HealthyRoutingResult:
    config_sha256: str
    policy_id: str
    status: TrialStatus
    foreground: EvaluationResult
    telemetry_bounds: dict[str, object]
    decisions: tuple[PolicyDecision, ...]
    observations: tuple[ObservationRecord, ...]
    events: tuple[FaultEvent, ...]
    elapsed_ns: int
    schema_version: str = "inferdrome.evaluation-healthy-result.v1"
    evidence_class: str = "LOCAL_MEASUREMENT_ONLY"
    evidence_eligible: bool = False
    endpoint_identity: str = "UNVERIFIED"
    observation_clock: str = "SHARED_TRIAL_MONOTONIC_ACQUISITION_START"
    observer_isolation: str = "SEPARATE_CLIENT_AND_DATAFLOW_NOT_SECURITY_SANDBOX"
    cleanup: str = "CLIENT_TASKS_AND_CONNECTIONS_CLOSED"

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["foreground"] = self.foreground.to_dict()
        return result


@dataclass
class _HealthySession(ObservationSession):
    config: HealthyRoutingConfig
    foreground_transport: Transport
    router_probe: ProbeTransport
    observer_probe: ProbeTransport
    clock: Clock
    epoch_ns: int
    gate: RouterObservations = field(default_factory=RouterObservations)
    foreground_stop: asyncio.Event = field(default_factory=asyncio.Event)
    poll_stop: asyncio.Event = field(default_factory=asyncio.Event)
    foreground_task: asyncio.Task[EvaluationResult] | None = None
    observations: list[ObservationRecord] = field(default_factory=list)
    events: list[FaultEvent] = field(default_factory=list)
    independent_latest_valid: dict[EndpointId, LoadObservation] = field(
        default_factory=dict
    )
    status: TrialStatus = "CANCELLED"

    async def drive(self, selector: _Selector) -> None:
        await self.clock.sleep_until(self.epoch_ns + self.config.telemetry.warmup_ns)
        if self.foreground_stop.is_set():
            self.interrupt()
            return
        warmup = self.now()
        if not self.warmed_up(warmup):
            self.status = "WARMUP_FAILED"
            self.event("WARMUP_FAILED", warmup)
            return
        self.event("WARMUP_PASSED", warmup)
        self.start_foreground(selector)
        assert self.foreground_task is not None
        await asyncio.wait([self.foreground_task])
        self.status = "COMPLETED"


async def run_routing_healthy(
    config: HealthyRoutingConfig,
    foreground_transport: Transport,
    router_probe: ProbeTransport,
    observer_probe: ProbeTransport,
    *,
    clock: Clock | None = None,
    stop: asyncio.Event | None = None,
) -> HealthyRoutingResult:
    """Own three distinct clients and run the selected routing baseline.

    A stopped or failed telemetry warm-up retains every foreground offer. Internal
    observation/cleanup failures block publication. Injected transports and clocks
    are trusted, cancellable code, never configuration-provided execution hooks.
    """
    clock = clock or SystemClock()
    stop = stop or asyncio.Event()
    session = _HealthySession(
        config,
        foreground_transport,
        router_probe,
        observer_probe,
        clock,
        clock.now_ns(),
        foreground_stop=_StopEvent(stop),
        poll_stop=_StopEvent(stop),
    )
    return await _run_healthy_session(session, stop=stop)


async def _run_healthy_session(
    session: _HealthySession, *, stop: asyncio.Event
) -> HealthyRoutingResult:
    """Internal numerical result; alternate engines must wrap it before export."""
    config = session.config
    decisions = await run_observation_session(session, stop=stop)
    assert session.foreground_task is not None
    return HealthyRoutingResult(
        config_sha256=sha256_digest(
            canonical_json_bytes(config.model_dump(mode="json"))
        ),
        policy_id=config.policy_id,
        status=session.status,
        foreground=session.foreground_task.result(),
        telemetry_bounds=config.telemetry.model_dump(),
        decisions=tuple(decisions),
        observations=tuple(session.observations),
        events=tuple(session.events),
        elapsed_ns=session.now(),
    )
