"""No GPU/process dispatch: exercise the real local adapter at its I/O seams."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from inferdrome.evaluation import engine_comparison_local as local
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.direct_process_lifecycle import DirectProcessLease
from inferdrome.evaluation.engine_comparison import encoded
from inferdrome.evaluation.load_calibration_rehearsal import SubprocessResult
from inferdrome.evaluation.observations import ProbeResponse
from inferdrome.routing_execution.canonical import sha256_digest
from tests.unit.test_engine_comparison import DIGEST, plan


def inputs():
    runtimes = {}
    for engine, version in (("vllm", "0.26.0"), ("sglang", "0.5.15")):
        runtimes[engine] = local.EngineRuntime(
            engine=engine,
            version=version,
            python=f"/opt/{engine}/bin/python3.12",
            python_sha256=DIGEST,
            packages_sha256=sha256_digest(encoded([[engine, version]])),
            cuda_home="/usr/local/cuda-13.1",
        )
    return local.LocalInputs(
        plan=plan(),
        model_path="/opt/model",
        chat_template_path="/opt/template.jinja",
        **runtimes,
    )


def test_finite_argv_match_and_inert_constructor(monkeypatch):
    def forbid(*args, **kw):
        raise AssertionError("unexpected dispatch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbid)
    value = inputs()
    assert set(local.drivers(value)) == {"vllm", "sglang"}
    for engine in ("vllm", "sglang"):
        runtime = getattr(value, engine)
        left = local.launch_argv(runtime, value, 0)
        right = local.launch_argv(runtime, value, 1)
        assert left.count("bfloat16") >= 1
        assert left[
            left.index("--seed" if engine == "vllm" else "--random-seed") + 1
        ] == str(value.plan.seed)
        assert "/opt/model" in left and "/opt/template.jinja" in left
        assert (
            "--no-enable-prefix-caching" in left
            if engine == "vllm"
            else "--disable-radix-cache" in left
        )
        assert [(a, b) for a, b in zip(left, right, strict=True) if a != b] == [
            ("8001", "8002")
        ]


@pytest.mark.parametrize("engine", ["vllm", "sglang"])
def test_partial_second_start_cleans_first_exact_lease(tmp_path, monkeypatch, engine):
    async def exercise():
        value = inputs()
        process = AsyncMock()
        first = DirectProcessLease(101, 101, DIGEST)
        process.start.side_effect = [first, OSError("not retained")]
        command = AsyncMock()

        async def invoke(argv, *, timeout_ns):
            if "importlib.metadata" in argv[-1]:
                body = encoded([[engine, getattr(value, engine).version]])
            elif "--query-compute-apps=pid" in argv:
                body = b""
            else:
                body = (
                    b"0, GPU-a, NVIDIA A100-SXM4-40GB, "
                    b"580.159.03, 40960, 0, 0, Disabled\n"
                    b"1, GPU-b, NVIDIA A100-SXM4-40GB, "
                    b"580.159.03, 40960, 0, 0, Disabled\n"
                )
            return SubprocessResult(argv, 0, body, b"")

        command.run.side_effect = invoke
        owner = local.LocalEnginePair(
            value, engine, processes=process, commands=command
        )
        monkeypatch.setattr(owner, "_artifact_check", lambda: None)
        monkeypatch.setattr(local, "resolve_direct_runtime", lambda _path: object())
        monkeypatch.setattr(local, "_assert_loopback_port_closed", lambda *_args: None)
        with pytest.raises(OSError):
            await owner.prepare(value.plan, stop=asyncio.Event())
        await owner.cleanup()
        process.terminate.assert_awaited_once_with(first, timeout_ns=10_000_000_000)
        assert not owner.leases
        environments = [c.kwargs["environment"] for c in process.start.call_args_list]
        assert [e["CUDA_VISIBLE_DEVICES"] for e in environments] == ["0", "1"]
        assert all("HF_TOKEN" not in e and "PYTHONPATH" not in e for e in environments)
        assert environments[0]["HOME"] != environments[1]["HOME"]

    asyncio.run(exercise())


def test_readiness_waits_and_gpu_identity_sampled(monkeypatch):
    async def exercise():
        value = inputs()
        process, command = AsyncMock(), AsyncMock()
        process.start.side_effect = [
            DirectProcessLease(i, i, DIGEST) for i in (101, 102)
        ]
        owner = local.LocalEnginePair(
            value, "vllm", processes=process, commands=command
        )
        monkeypatch.setattr(owner, "_artifact_check", lambda: None)
        monkeypatch.setattr(local, "resolve_direct_runtime", lambda _: object())
        owner._idle = AsyncMock()
        owner._gpu = AsyncMock(
            return_value=([["gpu-a"], ["gpu-b"]], (20000, 21000), (50, 60))
        )
        owner._command = AsyncMock(return_value=encoded([["vllm", "0.26.0"]]))
        probe = AsyncMock()
        probe.get.side_effect = [
            ProbeResponse(503, b""),
            ProbeResponse(200, b""),
            ProbeResponse(200, b""),
            ProbeResponse(200, b""),
        ]
        monkeypatch.setattr(local, "AiohttpProbeTransport", lambda *_a, **_kw: probe)
        receipt = await owner.prepare(value.plan, stop=asyncio.Event())
        assert receipt.startup_to_ready_ns >= 250_000_000
        assert probe.get.await_count == 4
        await owner.cleanup()
        assert process.terminate.await_count == 2

    asyncio.run(exercise())


def test_wrong_inventory_fails_before_gpu_or_process(monkeypatch):
    async def exercise():
        owner = local.LocalEnginePair(
            inputs(), "vllm", processes=AsyncMock(), commands=AsyncMock()
        )
        monkeypatch.setattr(owner, "_artifact_check", lambda: None)
        monkeypatch.setattr(local, "resolve_direct_runtime", lambda _: object())
        owner._command = AsyncMock(return_value=encoded([["vllm", "0.27.0"]]))
        owner._idle = AsyncMock()
        with pytest.raises(EvaluationError, match="inventory"):
            await owner.prepare(owner.inputs.plan, stop=asyncio.Event())
        await owner.cleanup()
        owner.processes.start.assert_not_awaited()
        owner._idle.assert_not_awaited()

    asyncio.run(exercise())
