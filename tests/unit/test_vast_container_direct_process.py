"""CPU/fake regression checks for the ordinary-container direct-process path."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.direct_process_lifecycle import (
    AsyncioDirectProcessRunner,
    DirectProcessLease,
    TwoEngineSglangDirectProcessLifecycle,
    TwoEngineVllmDirectProcessLifecycle,
    resolve_direct_runtime,
)
from inferdrome.evaluation.load_calibration_rehearsal import SubprocessResult
from inferdrome.evaluation.sglang_rehearsal import bind_sglang_rehearsal
from inferdrome.evaluation.study_config import compile_study
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.integration.test_load_calibration_rehearsal import _recipe_configs
from tests.integration.test_sglang_rehearsal import _profiles as sglang_profiles
from tests.integration.test_sglang_rehearsal import _rehearsal as sglang_rehearsal


def _trial():
    origins = ("http://127.0.0.1:18101", "http://127.0.0.1:18102")
    calibration, _ = _recipe_configs(
        origins, level_id="load-low", offered_count=7, seed=11
    )
    return origins, compile_study(calibration).trials[0]


class _Commands:
    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []

    async def run(self, argv: tuple[str, ...], *, timeout_ns: int) -> SubprocessResult:
        assert timeout_ns > 0
        self.argvs.append(argv)
        return SubprocessResult(argv=argv, returncode=0, stdout=b"", stderr=b"")


class _Processes:
    def __init__(self, *, fail_start: int | None = None) -> None:
        self.fail_start = fail_start
        self.started: list[
            tuple[tuple[str, ...], dict[str, str], DirectProcessLease]
        ] = []
        self.terminated: list[DirectProcessLease] = []

    async def start(
        self,
        executable: object,
        argv: tuple[str, ...],
        *,
        environment: dict[str, str],
    ) -> DirectProcessLease:
        del executable
        if self.fail_start == len(self.started):
            raise EvaluationError("injected direct start failure")
        lease = DirectProcessLease(
            pid=100 + len(self.started),
            process_group_id=100 + len(self.started),
            argv_sha256=sha256_digest(canonical_json_bytes(list(argv))),
        )
        self.started.append((argv, dict(environment), lease))
        return lease

    async def terminate(self, lease: DirectProcessLease, *, timeout_ns: int) -> int:
        assert timeout_ns > 0
        self.terminated.append(lease)
        return -15


def _lifecycle(
    tmp_path: Path,
    processes: _Processes,
    commands: _Commands,
) -> TwoEngineVllmDirectProcessLifecycle:
    origins, _ = _trial()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    lifecycle = TwoEngineVllmDirectProcessLifecycle(
        origins,
        ownership_id="direct-run-001",
        model_snapshot_path=snapshot,
        executable=resolve_direct_runtime("/bin/sh"),
        command_runner=commands,
        process_runner=processes,
        snapshot_verifier=lambda path: (
            path.is_dir() or (_ for _ in ()).throw(AssertionError)
        ),
        port_closed_probe=lambda port, timeout: (port, timeout),
    )
    return lifecycle


def test_vllm_direct_pair_is_gpu_isolated_and_ready_only_after_both_start(
    tmp_path: Path,
) -> None:
    origins, trial = _trial()
    commands, processes = _Commands(), _Processes()
    lifecycle = _lifecycle(tmp_path, processes, commands)
    readiness = False

    async def ready_after_pair(actual_trial: object, stop: asyncio.Event) -> None:
        nonlocal readiness
        assert actual_trial == trial
        assert not stop.is_set()
        assert len(processes.started) == 2
        readiness = True

    lifecycle._await_ready_and_warm = ready_after_pair  # type: ignore[method-assign]
    asyncio.run(lifecycle.prepare(trial, stop=asyncio.Event()))
    assert readiness is True
    assert len(processes.started) == 2
    for index, (argv, environment, _) in enumerate(processes.started):
        assert argv[0] == "/bin/sh"
        assert argv[1:3] == ("serve", str(tmp_path / "snapshot"))
        assert "--dtype" in argv and argv[argv.index("--dtype") + 1] == "bfloat16"
        assert "--tensor-parallel-size" in argv
        assert environment["CUDA_VISIBLE_DEVICES"] == str(index)
        assert f"127.0.0.1:{18101 + index}" not in " ".join(argv)
        assert argv[argv.index("--host") + 1] == "127.0.0.1"
    asyncio.run(lifecycle.cleanup(trial, stop=asyncio.Event()))
    assert processes.terminated == [processes.started[1][2], processes.started[0][2]]
    assert not lifecycle._active
    assert [receipt.state for receipt in lifecycle.receipts] == [
        "STARTED",
        "STARTED",
        "TERMINATED",
        "TERMINATED",
    ]
    assert all(argv[0] == "nvidia-smi" for argv in commands.argvs)
    assert origins == ("http://127.0.0.1:18101", "http://127.0.0.1:18102")


@pytest.mark.parametrize("fail_start, expected_terminations", [(0, 0), (1, 1)])
def test_direct_start_failures_clean_only_known_owned_groups(
    tmp_path: Path, fail_start: int, expected_terminations: int
) -> None:
    _, trial = _trial()
    commands, processes = _Commands(), _Processes(fail_start=fail_start)
    lifecycle = _lifecycle(tmp_path, processes, commands)
    with pytest.raises(EvaluationError, match="startup or warmup failed"):
        asyncio.run(lifecycle.prepare(trial, stop=asyncio.Event()))
    assert len(processes.started) == expected_terminations
    assert len(processes.terminated) == expected_terminations
    assert not lifecycle._active
    assert all(
        lease in [item[2] for item in processes.started]
        for lease in processes.terminated
    )


def test_direct_cancellation_precedes_all_engine_starts(tmp_path: Path) -> None:
    _, trial = _trial()
    commands, processes = _Commands(), _Processes()
    lifecycle = _lifecycle(tmp_path, processes, commands)
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(EvaluationError, match="was cancelled"):
        asyncio.run(lifecycle.prepare(trial, stop=stop))
    assert not processes.started
    assert not processes.terminated
    assert not commands.argvs


def test_expired_direct_deadline_blocks_gpu_readback_and_engine_start(
    tmp_path: Path,
) -> None:
    _, trial = _trial()
    commands, processes = _Commands(), _Processes()
    lifecycle = _lifecycle(tmp_path, processes, commands)
    lifecycle.set_operation_deadline(1)
    lifecycle._clock = lambda: 1
    with pytest.raises(EvaluationError, match="deadline expired"):
        asyncio.run(lifecycle.prepare(trial, stop=asyncio.Event()))
    assert not commands.argvs
    assert not processes.started
    assert lifecycle.receipts == ()


def test_sglang_direct_projection_reuses_native_choice_and_gpu_isolation() -> None:
    origins = ("http://127.0.0.1:18121", "http://127.0.0.1:18122")
    rehearsal = sglang_rehearsal(origins)
    profiles = sglang_profiles(origins)
    bindings = bind_sglang_rehearsal(rehearsal, profiles, containerized=False)
    lifecycle = TwoEngineSglangDirectProcessLifecycle(
        profiles,
        contexts=bindings.contexts,
        ownership_id="direct-run-001",
        executable=resolve_direct_runtime("/bin/sh"),
        artifact_verifier=lambda _profile: None,
    )
    assert lifecycle.engine_choice_sha256 == bindings.engine_choice_sha256
    for index, trial in enumerate(rehearsal.candidates[0].calibration_plan.trials[:2]):
        argv = lifecycle._engine_argv(trial, index=index)
        environment = lifecycle._engine_environment(index=index)
        assert argv[:3] == ("/bin/sh", "-m", "sglang.launch_server")
        assert "--dtype" in argv and argv[argv.index("--dtype") + 1] == "bfloat16"
        assert "--tp-size" in argv and argv[argv.index("--tp-size") + 1] == "1"
        assert environment["CUDA_VISIBLE_DEVICES"] == str(index)
        assert environment["HF_HUB_OFFLINE"] == "1"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_asyncio_runner_terminates_its_exact_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "child-started"
    identity = resolve_direct_runtime(sys.executable)
    code = (
        "import pathlib, subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(marker)!r}).write_text('started'); time.sleep(60)"
    )

    async def exercise() -> None:
        runner = AsyncioDirectProcessRunner()
        lease = await runner.start(
            identity,
            (str(identity.path), "-c", code),
            environment={
                "PATH": os.defpath,
                "HOME": str(tmp_path),
                "TMPDIR": str(tmp_path),
            },
        )
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.exists()
        result = await runner.terminate(lease, timeout_ns=2_000_000_000)
        assert result is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(lease.process_group_id, 0)
        with pytest.raises(EvaluationError, match="not owned"):
            await runner.terminate(lease, timeout_ns=1)

    asyncio.run(exercise())


def test_direct_lifecycle_has_no_docker_runner_dependency() -> None:
    source = (
        Path(__file__).parents[2]
        / "src/inferdrome/evaluation/direct_process_lifecycle.py"
    )
    content = source.read_text(encoding="utf-8")
    assert "PinnedLocalDockerRunner" not in content
    assert '"docker"' not in content


def test_direct_runtime_path_must_be_absolute() -> None:
    with pytest.raises(EvaluationError, match="must be absolute"):
        resolve_direct_runtime("vllm")
