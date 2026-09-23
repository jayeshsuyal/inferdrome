"""SGLang telemetry on the native routing session and request populations.

Only the parser and exported result semantics differ. The existing session owns
polls, policy state, deadlines, populations, publication faults and client cleanup.
No serving-process lifecycle is implemented here. A caller prepares two distinct
servers before this entrypoint; runtime identity remains unverified.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from inferdrome.evaluation.contracts import EndpointId, EvaluationError
from inferdrome.evaluation.engine_binding import (
    SglangEngineBinding,
    engine_binding_bytes,
    load_engine_binding_bytes,
    validate_engine_binding_trial,
)
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.faults import (
    RoutingFaultResult,
    _run_fault_session,
    _StopEvent,
    _Trial,
    close_routing_clients,
)
from inferdrome.evaluation.healthy import (
    HealthyRoutingResult,
    _HealthySession,
    _run_healthy_session,
)
from inferdrome.evaluation.healthy_config import HealthyRoutingConfig
from inferdrome.evaluation.observations import (
    AiohttpProbeTransport,
    MalformedLoad,
    MissingLoad,
    ProbeTransport,
)
from inferdrome.evaluation.runner import Clock, SystemClock, Transport
from inferdrome.evaluation.sglang_metrics import (
    MalformedSGLangMetrics,
    MissingSGLangMetrics,
    parse_sglang_metrics,
)
from inferdrome.evaluation.sglang_profile import SglangServingConfig
from inferdrome.evaluation.sglang_results import SGLangTrialResult, wrap_sglang_result
from inferdrome.evaluation.study_config import CompiledStudy, CompiledTrial
from inferdrome.evaluation.transport import AiohttpTransport


def _policy_coordinates(
    body: bytes, *, model: str, started_ns: int, completed_ns: int
) -> tuple[int, int]:
    """Explicit mathematical adaptation, never a vLLM telemetry equivalence.

    Native policies use only the sum of two bounded counts and acquisition age.
    These coordinates mean reported scheduler running and queued requests; they
    exclude SGLang's separate grammar queue and do not establish source-state age.
    The private numerical trace is exported only through the typed v2 wrapper.
    """
    try:
        counts = parse_sglang_metrics(
            body, model=model, started_ns=started_ns, completed_ns=completed_ns
        ).counts
        return counts.reported_running_requests, counts.reported_queued_requests
    except MissingSGLangMetrics:
        raise MissingLoad("selected SGLang gauges are missing") from None
    except MalformedSGLangMetrics:
        raise MalformedLoad("selected SGLang gauges are malformed") from None


class _SGLangHealthySession(_HealthySession):
    def policy_counts(
        self, body: bytes, *, started_ns: int, completed_ns: int
    ) -> tuple[int, int]:
        return _policy_coordinates(
            body,
            model=self.config.foreground.model,
            started_ns=started_ns,
            completed_ns=completed_ns,
        )


class _SGLangFaultSession(_Trial):
    def policy_counts(
        self, body: bytes, *, started_ns: int, completed_ns: int
    ) -> tuple[int, int]:
        return _policy_coordinates(
            body,
            model=self.config.foreground.model,
            started_ns=started_ns,
            completed_ns=completed_ns,
        )


def _bound_trial(
    plan: CompiledStudy, trial: CompiledTrial, binding: SglangEngineBinding
) -> SglangEngineBinding:
    validate_engine_binding_trial(binding, plan, trial)
    return load_engine_binding_bytes(engine_binding_bytes(binding), plan)


async def run_sglang_trial(
    plan: CompiledStudy,
    trial: CompiledTrial,
    binding: SglangEngineBinding,
    foreground_transport: Transport,
    router_probe: ProbeTransport,
    observer_probe: ProbeTransport,
    *,
    background_transport: Transport | None = None,
    clock: Clock | None = None,
    stop: asyncio.Event | None = None,
) -> SGLangTrialResult:
    """Take native client ownership after validating the bound trial context.

    Injected clients/clocks are trusted cancellable code. The caller retains them
    if preflight rejects the context or background-client shape. Otherwise the
    shared native session closes every client before any result can be returned.
    """
    binding = _bound_trial(plan, trial, binding)
    config = trial.config
    if isinstance(config, RoutingFaultConfig) != (background_transport is not None):
        raise EvaluationError("SGLang background client does not match its scenario")
    clock = clock or SystemClock()
    stop = stop or asyncio.Event()
    numerical: RoutingFaultResult | HealthyRoutingResult
    if isinstance(config, RoutingFaultConfig):
        assert background_transport is not None
        fault = _SGLangFaultSession(
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
        numerical = await _run_fault_session(fault, stop=stop)
    else:
        assert isinstance(config, HealthyRoutingConfig)
        healthy = _SGLangHealthySession(
            config,
            foreground_transport,
            router_probe,
            observer_probe,
            clock,
            clock.now_ns(),
            foreground_stop=_StopEvent(stop),
            poll_stop=_StopEvent(stop),
        )
        numerical = await _run_healthy_session(healthy, stop=stop)
    return wrap_sglang_result(numerical, binding, plan, trial)


class SGLangStudyExecutor:
    """One predeclared SGLang choice throughout a native sequential study."""

    def __init__(
        self,
        plan: CompiledStudy,
        binding: SglangEngineBinding,
        profiles: Mapping[EndpointId, SglangServingConfig],
    ) -> None:
        self.plan = plan
        self.profiles = dict(profiles)
        self.binding = load_engine_binding_bytes(
            engine_binding_bytes(binding), plan, profiles=self.profiles
        )

    async def __call__(
        self, trial: CompiledTrial, *, stop: asyncio.Event
    ) -> SGLangTrialResult:
        # Recheck even if trusted Python code mutated this executor between trials.
        binding = load_engine_binding_bytes(
            engine_binding_bytes(self.binding), self.plan, profiles=self.profiles
        )
        _bound_trial(self.plan, trial, binding)
        config = trial.config
        clients: list[Transport | ProbeTransport] = []
        background: AiohttpTransport | None = None
        try:
            foreground = AiohttpTransport(config.foreground)
            clients.append(foreground)
            if isinstance(config, RoutingFaultConfig):
                background = AiohttpTransport(config.background)
                clients.append(background)
            router = AiohttpProbeTransport(
                config.foreground,
                max_response_bytes=config.telemetry.max_response_bytes,
            )
            clients.append(router)
            observer = AiohttpProbeTransport(
                config.foreground,
                max_response_bytes=config.telemetry.max_response_bytes,
            )
            clients.append(observer)
        except BaseException:
            await close_routing_clients(
                clients,
                max(
                    config.foreground.bounds.cleanup_timeout_ns,
                    config.telemetry.cleanup_timeout_ns,
                    config.background.bounds.cleanup_timeout_ns
                    if isinstance(config, RoutingFaultConfig)
                    else 0,
                ),
            )
            raise
        return await run_sglang_trial(
            self.plan,
            trial,
            binding,
            foreground,
            router,
            observer,
            background_transport=background,
            stop=stop,
        )
