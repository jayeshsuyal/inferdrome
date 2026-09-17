"""SYNTHETIC_ONLY thread/deadline ownership; no Docker, GPU or model access."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from tests.unit.test_sglang_lifecycle import SGLangRunner, harness


class BlockedVerifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0
        self.fail = fail

    def __call__(self, _config: object) -> None:
        self.calls += 1
        self.started.set()
        try:
            if not self.release.wait(3):
                raise AssertionError("test did not release its synthetic worker")
            if self.fail:
                raise EvaluationError("synthetic artifact failure")
        finally:
            self.finished.set()


async def started(verifier: BlockedVerifier) -> None:
    async def spin() -> None:
        while not verifier.started.is_set():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(spin(), 1)


def test_expired_operation_never_starts_artifact_or_process_work() -> None:
    async def scenario() -> None:
        verifier = BlockedVerifier()
        owner, plans, _, runner, _, _, _ = harness(artifact_verifier=verifier)
        owner.set_operation_deadline(time.monotonic_ns() - 1)
        with pytest.raises(EvaluationError, match="deadline expired"):
            await owner.prepare(plans[0].trials[0], stop=asyncio.Event())
        await owner.cleanup(plans[0].trials[0], stop=asyncio.Event())
        assert verifier.calls == 0
        assert not runner.commands and owner._artifact_task is None

    asyncio.run(scenario())


def test_timed_out_read_only_worker_remains_owned_until_cleanup_reconciles() -> None:
    async def scenario() -> None:
        verifier = BlockedVerifier()
        owner, plans, _, runner, _, _, _ = harness(artifact_verifier=verifier)
        trial = plans[0].trials[0]
        deadline = time.monotonic_ns() + 80_000_000
        owner.set_operation_deadline(deadline)
        prepare = asyncio.create_task(owner.prepare(trial, stop=asyncio.Event()))
        try:
            await started(verifier)
            # This event-loop turn would not run during synchronous hashing.
            await asyncio.sleep(0)
            assert not verifier.finished.is_set()
            with pytest.raises(EvaluationError, match=r"deadline|unconfirmed"):
                await asyncio.wait_for(prepare, 1)
            worker = owner._artifact_task
            assert worker is not None and not worker.done() and not worker.cancelled()
            assert owner._operation_deadline_ns == deadline
            with pytest.raises(EvaluationError, match="worker cleanup is unconfirmed"):
                await owner.cleanup(trial, stop=asyncio.Event())
            with pytest.raises(EvaluationError, match="unresolved owned work"):
                await owner.prepare(trial, stop=asyncio.Event())
            assert not runner.commands and verifier.calls == 1
        finally:
            verifier.release.set()
        owner.set_operation_deadline(time.monotonic_ns() + 1_000_000_000)
        await owner.cleanup(trial, stop=asyncio.Event())
        assert verifier.finished.is_set() and owner._artifact_task is None
        assert not runner.commands

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", ["task", "stop"])
@pytest.mark.parametrize("worker_fails", [False, True])
def test_cancelled_preflight_keeps_worker_live_and_cleanup_waits_for_it(
    cancel: str, worker_fails: bool
) -> None:
    async def scenario() -> None:
        verifier = BlockedVerifier(fail=worker_fails)
        owner, plans, _, runner, _, _, _ = harness(artifact_verifier=verifier)
        trial = plans[0].trials[0]
        deadline = time.monotonic_ns() + 2_000_000_000
        owner.set_operation_deadline(deadline)
        stop = asyncio.Event()
        prepare = asyncio.create_task(owner.prepare(trial, stop=stop))
        try:
            await started(verifier)
            if cancel == "task":
                prepare.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await prepare
            else:
                stop.set()
                with pytest.raises(EvaluationError, match="cancelled"):
                    await asyncio.wait_for(prepare, 1)
            worker = owner._artifact_task
            assert worker is not None and not worker.done() and not worker.cancelled()
            cleanup = asyncio.create_task(owner.cleanup(trial, stop=stop))
            await asyncio.sleep(0)
            assert not cleanup.done() and not runner.commands
            verifier.release.set()
            await asyncio.wait_for(cleanup, 1)
            assert owner._artifact_task is None
            assert verifier.calls == 1 and verifier.finished.is_set()
            assert owner._operation_deadline_ns == deadline
            assert not runner.commands
        finally:
            verifier.release.set()

    asyncio.run(scenario())


def test_deadline_is_checked_between_endpoint_verifications() -> None:
    async def scenario() -> None:
        now = [1]
        calls = []

        def verifier(_config: object) -> None:
            calls.append("artifact")
            now[0] = 2

        owner, plans, _, runner, _, _, _ = harness(
            artifact_verifier=verifier, clock=lambda: now[0]
        )
        owner.set_operation_deadline(2)
        with pytest.raises(EvaluationError, match="deadline expired"):
            await owner.prepare(plans[0].trials[0], stop=asyncio.Event())
        await owner.cleanup(plans[0].trials[0], stop=asyncio.Event())
        assert calls == ["artifact"] and not runner.commands

    asyncio.run(scenario())


def test_failed_runtime_readback_remains_required_after_targets_are_removed() -> None:
    async def scenario() -> None:
        owner, plans, _, runner, _, _, _ = harness()
        trial = plans[0].trials[0]
        await owner.prepare(trial, stop=asyncio.Event())
        calls = []

        def fail_once(port: int, _timeout: float) -> None:
            calls.append(port)
            if len(calls) == 1:
                raise EvaluationError("synthetic unavailable port readback")

        owner._port_closed_probe = fail_once
        with pytest.raises(EvaluationError, match="cleanup is unconfirmed"):
            await owner.cleanup(trial, stop=asyncio.Event())
        assert not owner._active and owner._runtime_readbacks_required
        before = len(runner.commands)
        await owner.cleanup(trial, stop=asyncio.Event())
        assert len(calls) == 4
        assert len(runner.commands) == before + 2
        assert not owner._runtime_readbacks_required

    asyncio.run(scenario())


def test_artifact_failure_full_prepare_cleanup_has_no_process_edges() -> None:
    async def scenario() -> None:
        def invalid(_config: object) -> None:
            raise EvaluationError("synthetic unreadable artifact")

        owner, plans, _, runner, _, _, _ = harness(artifact_verifier=invalid)
        trial = plans[0].trials[0]
        try:
            with pytest.raises(EvaluationError, match="unreadable artifact"):
                await owner.prepare(trial, stop=asyncio.Event())
        finally:
            await owner.cleanup(trial, stop=asyncio.Event())
        assert not runner.commands and owner._artifact_task is None

    asyncio.run(scenario())


@pytest.mark.parametrize("cause", ["stop", "deadline"])
def test_queued_worker_rechecks_original_stop_and_deadline_before_file_access(
    monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    async def scenario() -> None:
        queued, release = asyncio.Event(), asyncio.Event()
        original_to_thread = asyncio.to_thread
        calls = []
        now = [1_000_000]

        async def delayed_to_thread(function, *args, **kwargs):
            queued.set()
            await release.wait()
            return await original_to_thread(function, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", delayed_to_thread)
        owner, plans, _, runner, _, _, _ = harness(
            artifact_verifier=lambda _config: calls.append("artifact"),
            clock=lambda: now[0],
        )
        trial = plans[0].trials[0]
        owner.set_operation_deadline(1_000_000_000)
        stop = asyncio.Event()
        prepare = asyncio.create_task(owner.prepare(trial, stop=stop))
        await asyncio.wait_for(queued.wait(), 1)
        prepare.cancel()
        with pytest.raises(asyncio.CancelledError):
            await prepare
        if cause == "stop":
            stop.set()
        else:
            now[0] = 1_000_000_000
        owner.set_operation_deadline(now[0] + 1_000_000_000)
        release.set()
        await asyncio.wait_for(owner.cleanup(trial, stop=stop), 1)
        assert not calls and not runner.commands and owner._artifact_task is None

    asyncio.run(scenario())


def test_runtime_preflight_error_keeps_original_port_and_gpu_cleanup_readbacks() -> (
    None
):
    async def scenario() -> None:
        runner = SGLangRunner(gpu_error_indices={0})
        owner, plans, _, _, events, _, _ = harness(runner=runner)
        trial = plans[0].trials[0]
        with pytest.raises(EvaluationError, match="GPU-idle readback is unconfirmed"):
            await owner.prepare(trial, stop=asyncio.Event())
        assert not owner._active and owner._runtime_readbacks_required
        assert len(runner.commands) == 2
        with pytest.raises(EvaluationError, match="cleanup is unconfirmed"):
            await owner.cleanup(trial, stop=asyncio.Event())
        assert owner._runtime_readbacks_required
        assert events.count("port-closed") == 2 and len(runner.commands) == 4
        runner.gpu_error_indices.clear()
        await owner.cleanup(trial, stop=asyncio.Event())
        assert not owner._runtime_readbacks_required
        assert events.count("port-closed") == 4 and len(runner.commands) == 6
        assert all(command[0] == "nvidia-smi" for command in runner.commands)

    asyncio.run(scenario())
