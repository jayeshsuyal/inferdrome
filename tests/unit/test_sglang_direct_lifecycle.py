"""Injected CPU lifecycle failures, owned cleanup and six-pair smoke sequencing."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

import inferdrome.evaluation.sglang_direct_lifecycle as direct
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.direct_process_lifecycle import resolve_direct_runtime
from inferdrome.evaluation.engine_binding import (
    build_sglang_direct_binding,
    engine_choice_sha256,
)
from inferdrome.evaluation.load_calibration_rehearsal import SubprocessResult
from inferdrome.evaluation.sglang_direct_runtime import runtime_manifest_sha256
from inferdrome.evaluation.sglang_direct_study import run_sglang_direct_smoke_study
from inferdrome.evaluation.sglang_results import wrap_sglang_result
from inferdrome.evaluation.study_config import compile_study
from tests.sglang_direct_support import (
    direct_profiles,
    direct_study,
    synthetic_native,
    synthetic_runtime,
)
from tests.unit.test_vast_container_direct_process import _Processes


class Commands:
    def __init__(self, driver="595.84"):
        self.driver = driver

    async def run(self, argv, *, timeout_ns):
        output = b""
        if any(a.startswith("--query-gpu=") for a in argv):
            output = "".join(
                f"{i}, GPU-00000000-0000-0000-0000-00000000000{i + 1}, "
                f"NVIDIA A100-SXM4-40GB, 40536, {self.driver}, Disabled\n"
                for i in (0, 1)
            ).encode()
        return SubprocessResult(argv, 0, output, b"")


def owner(monkeypatch, processes, driver="595.84", manifest_driver="595.84"):
    manifest = synthetic_runtime().model_copy(
        update={"driver_version": manifest_driver}
    )
    executable = resolve_direct_runtime("/bin/sh")
    old = manifest.roles["python"]
    roles = dict(manifest.roles, python=str(executable.path))
    files = tuple(
        f.model_copy(update={"path": str(executable.path)}) if f.path == old else f
        for f in manifest.files
    )
    manifest = manifest.model_copy(update={"roles": roles, "files": files})
    plan = compile_study(direct_study())
    binding = build_sglang_direct_binding(
        plan,
        direct_profiles(),
        runtime_manifest_sha256=runtime_manifest_sha256(manifest),
    )
    monkeypatch.setattr(direct, "verify_runtime_host", lambda manifest: None)
    lifecycle = direct.Sglang015DirectLifecycle(
        direct_profiles(),
        contexts=((plan, binding),),
        runtime_manifest=manifest,
        ownership_id="test-direct015",
        executable=executable,
        process_runner=processes,
        command_runner=Commands(driver),
        artifact_verifier=lambda p: None,
        port_closed_probe=lambda p, t: None,
    )

    async def ready(trial, stop):
        assert len(lifecycle._active) == 2

    lifecycle._await_ready_and_warm = ready
    return lifecycle, plan


@pytest.mark.parametrize("failure", [None, 0, 1, "driver"])
def test_pair_isolation_and_exact_cleanup(monkeypatch, failure):
    processes = _Processes(fail_start=failure if type(failure) is int else None)
    lifecycle, plan = owner(
        monkeypatch, processes, "580.159.03" if failure == "driver" else "595.84"
    )

    async def run():
        try:
            if failure is not None:
                with pytest.raises(EvaluationError):
                    await lifecycle.prepare(plan.trials[0], stop=asyncio.Event())
            else:
                await lifecycle.prepare(plan.trials[0], stop=asyncio.Event())
        finally:
            # Driver mismatch also makes final hardware readback unavailable.
            if failure == "driver":
                lifecycle._command_runner = Commands()
            await lifecycle.cleanup(plan.trials[0], stop=asyncio.Event())
        assert not lifecycle._active and not lifecycle._cache_roots

    asyncio.run(run())
    assert len(processes.terminated) == len(processes.started)
    for i, (argv, env, lease) in enumerate(processes.started):
        assert argv[1:5] == ("-I", "-B", "-m", "sglang.launch_server")
        assert env["CUDA_VISIBLE_DEVICES"] == str(i)
        assert "PYTHONPATH" not in env and "CUDA_PATH" not in env
        assert lease in processes.terminated
    if len(processes.started) == 2:
        assert processes.started[0][1]["HOME"] != processes.started[1][1]["HOME"]


@pytest.mark.parametrize("driver", ["580.159.03", "580.178.04"])
def test_current_cuda_13_driver_inventory_is_admitted_and_exact(monkeypatch, driver):
    processes = _Processes()
    lifecycle, plan = owner(
        monkeypatch, processes, driver=driver, manifest_driver=driver
    )

    async def run():
        await lifecycle.prepare(plan.trials[0], stop=asyncio.Event())
        await lifecycle.cleanup(plan.trials[0], stop=asyncio.Event())

    asyncio.run(run())
    assert len(processes.started) == 2


def test_worker_is_retained_across_cancellation_until_cleanup(monkeypatch):
    processes = _Processes()
    lifecycle, plan = owner(monkeypatch, processes)
    started, release = threading.Event(), threading.Event()

    def work(manifest):
        started.set()
        release.wait(5)

    monkeypatch.setattr(direct, "verify_runtime_host", work)

    async def run():
        task = asyncio.create_task(
            lifecycle.prepare(plan.trials[0], stop=asyncio.Event())
        )
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert lifecycle._worker is not None and not lifecycle._worker.done()
        release.set()
        await lifecycle.cleanup(plan.trials[0], stop=asyncio.Event())
        assert lifecycle._worker is None and not processes.started

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "smoke", "cleanup", "provenance"])
def test_two_smokes_precede_smallest_complete_four_policy_study(tmp_path, failure):
    config = direct_study()
    profiles = direct_profiles()
    manifest = synthetic_runtime()
    plan = compile_study(config)
    binding = build_sglang_direct_binding(
        plan, profiles, runtime_manifest_sha256=runtime_manifest_sha256(manifest)
    )
    good = {
        trial.trial_id: wrap_sglang_result(
            synthetic_native(trial), binding, plan, trial
        )
        for trial in plan.trials
    }
    failed = None
    # A real terminal failed fixture, not a forged result status.
    if failure == "smoke":
        from tests.unit.test_sglang_results import _native

        failed = wrap_sglang_result(
            _native(plan.trials[0], "CANCELLED_BEFORE_START"),
            binding,
            plan,
            plan.trials[0],
        )

    class FakeOwner:
        engine_choice_sha256 = engine_choice_sha256(binding)
        receipts = ()

        def __init__(self):
            self.events = []
            self.resets = []
            self.loaded_runtime_receipts = []

        def set_operation_deadline(self, deadline):
            assert deadline > 0

        async def prepare(self, trial, *, stop):
            self.events.append(("prepare", trial.trial_id))

        async def check_loaded_runtime(self):
            if failure == "provenance":
                raise EvaluationError("injected provenance failure")

        async def cleanup(self, trial, *, stop):
            self.events.append(("cleanup", trial.trial_id))
            if failure == "cleanup":
                raise EvaluationError("injected cleanup failure")

    lifecycle = FakeOwner()

    async def execute(trial, *, stop):
        lifecycle.events.append(("execute", trial.trial_id))
        result = failed if failed is not None else good[trial.trial_id]
        await asyncio.sleep(result._statistical_input.elapsed_ns / 1_000_000_000)
        return result

    output = tmp_path / "run"
    if failure:
        with pytest.raises(EvaluationError):
            asyncio.run(
                run_sglang_direct_smoke_study(
                    config,
                    profiles,
                    manifest,
                    output,
                    lifecycle=lifecycle,
                    executor=execute,
                )
            )
        assert not (output / "study").exists()
        assert json.loads((output / "outcome.json").read_bytes())["status"] == "FAILED"
        assert lifecycle.events[-1][0] == "cleanup"
    else:
        asyncio.run(
            run_sglang_direct_smoke_study(
                config,
                profiles,
                manifest,
                output,
                lifecycle=lifecycle,
                executor=execute,
            )
        )
        expected = [t.trial_id for t in (*plan.trials[:2], *plan.trials)]
        assert [t for action, t in lifecycle.events if action == "prepare"] == expected
        assert [t for action, t in lifecycle.events if action == "cleanup"] == expected
        assert not (output / "smoke/manifest.json").exists()
        assert (
            json.loads((output / "outcome.json").read_bytes())["status"]
            == "COMPLETED_LOCAL_ONLY"
        )
        assert len(list(output.glob("lifecycle-*.json"))) == 6


def test_post_trial_stop_cancels_probe_wrapper_without_skipping_cleanup():
    from inferdrome.evaluation.sglang_direct_study import _check_after_trial

    async def run():
        stop = asyncio.Event()
        started = asyncio.Event()
        ended = asyncio.Event()

        class FakeOwner:
            async def check_loaded_runtime(self):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    ended.set()

        task = asyncio.create_task(_check_after_trial(FakeOwner(), stop))
        await started.wait()
        stop.set()
        with pytest.raises(EvaluationError):
            await task
        assert ended.is_set()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation", ["80gb", "pcie", "extra", "mig", "duplicate", "memory"]
)
def test_gpu_inventory_requires_exact_bound_pair(monkeypatch, mutation):
    lifecycle, _plan = owner(monkeypatch, _Processes())

    class WrongInventory(Commands):
        async def run(self, argv, *, timeout_ns):
            result = await super().run(argv, timeout_ns=timeout_ns)
            if result.stdout:
                data = result.stdout
                if mutation == "80gb":
                    data = data.replace(b"40GB", b"80GB")
                elif mutation == "pcie":
                    data = data.replace(b"SXM4", b"PCIE")
                elif mutation == "extra":
                    data += data.splitlines()[0] + b"\n"
                elif mutation == "mig":
                    data = data.replace(b"Disabled", b"Enabled")
                elif mutation == "duplicate":
                    data = data.replace(b"000000000002", b"000000000001")
                else:
                    data = data.replace(b"40536", b"81000")
                return SubprocessResult(argv, 0, data, b"")
            return result

    lifecycle._command_runner = WrongInventory()
    with pytest.raises(EvaluationError):
        asyncio.run(lifecycle._verify_gpu_idle(deadline_ns=lifecycle._deadline()))
