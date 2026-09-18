"""Deterministic native rehearsal sessions on explicit synthetic transports.

The real session owners, telemetry parser, policies and request runners produce
every record. Only their existing clock/transport seams are supplied by tests;
trial configurations, timestamps and outcomes are never rewritten.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from inferdrome.evaluation import load_calibration_rehearsal as rehearsal_module
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.faults import RoutingFaultResult, run_routing_fault
from inferdrome.evaluation.healthy import HealthyRoutingResult, run_routing_healthy
from inferdrome.evaluation.observations import ProbeResponse
from inferdrome.evaluation.study import StudyManifest
from inferdrome.evaluation.study_config import CompiledTrial, StudyConfig
from tests.unit.test_evaluation_faults import Stream, finish
from tests.unit.test_evaluation_runner import COMPLETE, ManualClock, settle


class _ScriptedStream(Stream):
    def __init__(
        self, clock: ManualClock, *, delay_ns: int = 0, block: bool = False
    ) -> None:
        super().__init__(clock, block=block)
        self.delay_ns = delay_ns

    async def stream(
        self,
        origin: str,
        body: bytes,
        on_headers: Callable[[int], None],
        on_bytes: Callable[[bytes], None],
    ) -> None:
        del body
        self.calls.append((self.clock.elapsed_ns, origin))
        self.active += 1
        try:
            on_headers(200)
            if self.block:
                await asyncio.Event().wait()
            if self.delay_ns:
                await self.clock.sleep_until(self.clock.now_ns() + self.delay_ns)
            on_bytes(COMPLETE)
        finally:
            self.active -= 1


class _NativeProbe:
    """Return correctly labelled gauges through the unchanged native parser."""

    def __init__(
        self,
        *,
        model: str,
        first_origin: str,
        background: _ScriptedStream | None,
        missing: bool = False,
    ) -> None:
        self.model = model
        self.first_origin = first_origin
        self.background = background
        self.missing = missing
        self.active = 0
        self.closed = False

    async def get(self, origin: str, path: str) -> ProbeResponse:
        self.active += 1
        try:
            if path == "/health":
                return ProbeResponse(200, b"")
            if self.missing:
                return ProbeResponse(200, b"# selected load metrics unavailable\n")
            running = (
                self.background.active * 5
                if self.background is not None and origin == self.first_origin
                else 2
            )
            return ProbeResponse(
                200,
                (
                    f'vllm:num_requests_running{{model_name="{self.model}",'
                    f'engine="0"}} {running}\n'
                    f'vllm:num_requests_waiting{{model_name="{self.model}",'
                    'engine="0"} 0\n'
                ).encode(),
            )
        finally:
            self.active -= 1

    async def close(self) -> None:
        assert self.active == 0
        self.closed = True


class NativeRehearsalExecutor:
    def __init__(
        self, *, high_slo_miss: bool = False, warmup_failure: bool = False
    ) -> None:
        self.high_slo_miss = high_slo_miss
        self.warmup_failure = warmup_failure
        self.study_clock = ManualClock()
        self.calls: list[CompiledTrial] = []
        self.owned: list[tuple[_ScriptedStream | _NativeProbe, ...]] = []
        self.active = 0
        self._clocks: list[ManualClock] = []
        self._tasks: list[asyncio.Task[Any]] = []

    async def __call__(
        self, trial: CompiledTrial, *, stop: asyncio.Event
    ) -> HealthyRoutingResult | RoutingFaultResult:
        assert self.active == 0
        assert all(client.closed for group in self.owned for client in group)
        first_call = not self.calls
        self.calls.append(trial)
        self.active += 1
        clock = ManualClock()
        self._clocks.append(clock)
        foreground = _ScriptedStream(
            clock,
            delay_ns=200_000_000
            if self.high_slo_miss and trial.block_id.startswith("load-high-cal-")
            else 0,
        )
        background = (
            _ScriptedStream(clock, block=True)
            if isinstance(trial.config, RoutingFaultConfig)
            else None
        )
        probe_args = {
            "model": trial.config.foreground.model,
            "first_origin": trial.config.foreground.endpoints[0].origin,
            "background": background,
        }
        router = _NativeProbe(**probe_args)
        observer = _NativeProbe(
            **probe_args, missing=self.warmup_failure and first_call
        )
        self.owned.append(
            (foreground, background, router, observer)
            if background is not None
            else (foreground, router, observer)
        )
        if isinstance(trial.config, RoutingFaultConfig):
            assert background is not None
            session = run_routing_fault(
                trial.config,
                foreground,
                background,
                router,
                observer,
                clock=clock,
                stop=stop,
            )
        else:
            session = run_routing_healthy(
                trial.config, foreground, router, observer, clock=clock, stop=stop
            )
        task = asyncio.create_task(session)
        self._tasks.append(task)
        try:
            bounds = trial.config.foreground.bounds
            until_ms = (bounds.duration_ns + bounds.drain_ns + 4_999_999) // 5_000_000
            await finish(clock, until_ms * 5)
            assert task.done(), "native session exceeded its declared population window"
            result = await task
            assert not clock.waiters
            self.study_clock.advance_to(
                self.study_clock.elapsed_ns + result.elapsed_ns
            )
            populations = {
                "evidence_class": "SYNTHETIC_ONLY",
                "foreground": replace(
                    result.foreground, evidence_class="SYNTHETIC_ONLY"
                ),
            }
            if isinstance(result, RoutingFaultResult):
                populations["background"] = replace(
                    result.background, evidence_class="SYNTHETIC_ONLY"
                )
            return replace(result, **populations)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.active -= 1

    def assert_quiescent(self) -> None:
        assert self.active == 0
        assert all(
            client.closed and client.active == 0
            for group in self.owned
            for client in group
        )
        assert not self.study_clock.waiters
        assert all(not clock.waiters for clock in self._clocks)
        assert all(task.done() for task in self._tasks)

    def study_clock_context(self):
        return native_rehearsal_execution(self)


@contextmanager
def native_rehearsal_execution(executor: NativeRehearsalExecutor) -> Iterator[None]:
    """Inject only the study clock; preserve the existing executor/lifecycle owner."""
    original = rehearsal_module.run_study

    async def run_with_clock(
        config: StudyConfig, output_dir: Path, **kwargs: Any
    ) -> StudyManifest:
        assert "clock" not in kwargs
        phase_deadline = executor.study_clock.now_ns() + config.limits.max_duration_ns

        async def advance_cooldown() -> None:
            while True:
                await settle()
                if executor.active:
                    continue
                # The study deadline is not a cooldown. Never advance it while
                # real readiness I/O or an executor call is being awaited.
                cooldowns = [
                    deadline
                    for deadline in executor.study_clock.waiters.values()
                    if deadline < phase_deadline
                    and deadline
                    <= executor.study_clock.now_ns() + config.limits.cooldown_ns
                ]
                if cooldowns:
                    executor.study_clock.advance_to(
                        max(
                            executor.study_clock.elapsed_ns,
                            min(cooldowns) - executor.study_clock.epoch_ns,
                        )
                    )

        driver = asyncio.create_task(advance_cooldown())
        executor._tasks.append(driver)
        try:
            return await original(
                config, output_dir, clock=executor.study_clock, **kwargs
            )
        finally:
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rehearsal_module, "run_study", run_with_clock)
        yield
