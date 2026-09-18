"""Controlled CPU readiness timing; no claim about the cause of a CI failure."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic_ns

import pytest
from aiohttp import web

from inferdrome.evaluation import load_calibration_rehearsal as rehearsal_module
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.load_calibration import load_calibration_protocol_bytes
from inferdrome.evaluation.load_calibration_rehearsal import (
    CandidateStudyRecipe,
    LoopbackTwoEndpointReadinessLifecycle,
    compile_rehearsal,
    run_rehearsal,
)
from inferdrome.evaluation.study import TrialResult
from inferdrome.evaluation.study_config import CompiledTrial, compile_study
from tests.integration.test_load_calibration_rehearsal import (
    _protocol,
    _recipe_configs,
    _rehearsal_failure_diagnostics,
)

_GATED_READINESS_NS = 750_000_000
_REPAIRED_ALLOWANCE_NS = 2_000_000_000


@dataclass
class _ReadinessGate:
    arrivals: list[tuple[str, str]] = field(default_factory=list)
    all_arrived: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    completions: list[tuple[float, bool]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        self.arrivals.append((request.host, request.path))
        if len(self.arrivals) == 4:
            self.all_arrived.set()
        await self.release.wait()
        return web.Response(text="ready\n")

    def probe_finished(self, timeout_seconds: float, succeeded: bool) -> None:
        self.completions.append((timeout_seconds, succeeded))
        if len(self.completions) == 4:
            self.finished.set()


@asynccontextmanager
async def _gated_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[_ReadinessGate, tuple[str, str]]]:
    """Keep sockets alive until cancelled to_thread reads actually finish."""
    gate = _ReadinessGate()
    loop = asyncio.get_running_loop()
    original_probe = rehearsal_module._probe_loopback_readiness

    def tracked_probe(origin: str, path: str, timeout_seconds: float) -> None:
        succeeded = False
        try:
            original_probe(origin, path, timeout_seconds)
            succeeded = True
        finally:
            loop.call_soon_threadsafe(gate.probe_finished, timeout_seconds, succeeded)

    monkeypatch.setattr(rehearsal_module, "_probe_loopback_readiness", tracked_probe)
    runners: list[web.AppRunner] = []
    listeners: list[socket.socket] = []
    origins: list[str] = []
    try:
        for _ in range(2):
            app = web.Application()
            app.router.add_get("/health", gate.handle)
            app.router.add_get("/metrics", gate.handle)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
            runners.append(runner)
            await runner.setup()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.bind(("127.0.0.1", 0))
            origins.append(f"http://127.0.0.1:{listener.getsockname()[1]}")
            await web.SockSite(runner, listener).start()
        yield gate, (origins[0], origins[1])
    finally:
        gate.release.set()
        # Cancelling an asyncio.to_thread future cannot terminate urllib's
        # synchronous read. Join its explicit completion before server teardown.
        if gate.arrivals:
            await asyncio.wait_for(gate.finished.wait(), 2)
        for runner in runners:
            await asyncio.wait_for(runner.cleanup(), 2)
        for listener in listeners:
            listener.close()


def _trial(origins: tuple[str, str]) -> CompiledTrial:
    calibration, _ = _recipe_configs(
        origins, level_id="load-low", offered_count=7, seed=11
    )
    return compile_study(calibration).trials[0]


@pytest.mark.parametrize("allowance_ns", [500_000_000, _REPAIRED_ALLOWANCE_NS])
def test_actual_readiness_reads_need_the_fixture_preparation_allowance(
    monkeypatch: pytest.MonkeyPatch, allowance_ns: int
) -> None:
    async def exercise() -> None:
        async with _gated_readiness(monkeypatch) as (gate, origins):
            lifecycle = LoopbackTwoEndpointReadinessLifecycle(origins)
            trial = _trial(origins)

            async def release_after_controlled_delay() -> None:
                await gate.all_arrived.wait()
                held_at = monotonic_ns()
                await asyncio.sleep(_GATED_READINESS_NS / 1_000_000_000)
                assert monotonic_ns() - held_at >= _GATED_READINESS_NS
                gate.release.set()

            release_task = asyncio.create_task(release_after_controlled_delay())
            prepare = asyncio.create_task(
                lifecycle.prepare(trial, stop=asyncio.Event())
            )
            try:
                if allowance_ns == 500_000_000:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(prepare, allowance_ns / 1_000_000_000)
                    assert prepare.cancelled()
                    assert not gate.release.is_set()
                else:
                    await asyncio.wait_for(prepare, allowance_ns / 1_000_000_000)
                    assert prepare.done() and not prepare.cancelled()
                await asyncio.wait_for(release_task, 2)
                await asyncio.wait_for(gate.finished.wait(), 2)
                assert len(gate.arrivals) == len(set(gate.arrivals)) == 4
                assert sorted(path for _, path in gate.arrivals) == [
                    "/health", "/health", "/metrics", "/metrics"
                ]
                # Both cases use the real urllib helper with its existing one
                # second per-read timeout. Even after outer cancellation all
                # four network reads finish successfully, within that bound.
                assert gate.completions == [(1.0, True)] * 4
                assert trial.first_content_slo_ns == 100_000_000
                assert trial.completion_slo_ns == 150_000_000
            finally:
                gate.release.set()
                release_task.cancel()
                prepare.cancel()
                await asyncio.gather(release_task, prepare, return_exceptions=True)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(exercise())


def test_two_second_allowance_still_cancels_preparation_before_dispatch(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
        entered = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()
        delegated: list[bool] = []
        dispatched: list[str] = []

        class GatedLifecycle(LoopbackTwoEndpointReadinessLifecycle):
            async def prepare(
                self, trial: CompiledTrial, *, stop: asyncio.Event
            ) -> None:
                entered.set()
                try:
                    # Keep a cancellable gate ahead of to_thread work; an
                    # unbounded preparation never starts a lingering read.
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                delegated.append(True)
                await super().prepare(trial, stop=stop)

        async def unexpected_executor(
            trial: CompiledTrial, *, stop: asyncio.Event
        ) -> TrialResult:
            del stop
            dispatched.append(trial.trial_id)
            raise AssertionError("expired preparation reached native dispatch")

        recipes = tuple(
            (
                level,
                *_recipe_configs(
                    origins, level_id=level, offered_count=count, seed=seed
                ),
            )
            for level, count, seed in (("load-low", 7, 11), ("load-high", 14, 29))
        )
        rehearsal = compile_rehearsal(
            load_calibration_protocol_bytes(
                _protocol(recipes, lifecycle_allowance_ns=_REPAIRED_ALLOWANCE_NS)
            ),
            tuple(CandidateStudyRecipe(*recipe) for recipe in recipes),
        )
        output = tmp_path / "rehearsal"
        output.mkdir(mode=0o700)
        lifecycle = GatedLifecycle(origins)
        started = monotonic_ns()
        with pytest.raises(
            EvaluationError, match="incomplete calibration study cannot select a level"
        ):
            await run_rehearsal(
                rehearsal,
                output,
                lifecycle=lifecycle,
                executor=unexpected_executor,
            )
        assert entered.is_set() and cancelled.is_set()
        assert not release.is_set() and not delegated
        assert not dispatched
        assert monotonic_ns() - started >= _REPAIRED_ALLOWANCE_NS
        manifest = json.loads(
            (output / "calibration-load-low" / "manifest.json").read_bytes()
        )
        assert manifest["status"] == "ABORTED"
        assert manifest["reason"] == "CONTROLLER_OR_CLEANUP_FAILED"
        first, *remaining = manifest["trials"]
        assert first["state"] == "ABORTED"
        assert first["failure_reason"] == "CONTROLLER_OR_CLEANUP_FAILED"
        assert all(row["state"] == "NOT_RUN" for row in remaining)
        assert len(remaining) == 7
        receipts = json.loads(
            (output / "calibration-load-low-lifecycle.json").read_bytes()
        )["receipts"]
        assert len(receipts) == 1
        assert receipts[0]["reset_and_warmup"] == "FAILED"
        assert receipts[0]["cleanup"] == "CONFIRMED"
        assert receipts[0]["execute_elapsed_ns"] is None
        diagnostic = _rehearsal_failure_diagnostics(output)
        assert '"status":"ABORTED"' in diagnostic
        assert "CONTROLLER_OR_CLEANUP_FAILED" in diagnostic
        assert '"reset_and_warmup":"FAILED"' in diagnostic
        assert all(origin not in diagnostic for origin in origins)
        assert "127.0.0.1" not in diagnostic
        assert len(diagnostic) <= 32_768
        assert not (output / "calibration-load-high").exists()
        assert not (output / "calibration-selection.json").exists()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(exercise())
