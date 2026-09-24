"""Explicit local process adapter; construction and preview are inert.

Only execute starts preinstalled engines. No downloads, containers, provider
clients or ambient credentials. Reuses the exact-owned process supervisor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from collections.abc import Callable
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from time import monotonic_ns
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field

from inferdrome.evaluation.contracts import ClosedModel, EvaluationError
from inferdrome.evaluation.direct_process_lifecycle import (
    AsyncioDirectProcessRunner,
    DirectProcessLease,
    DirectProcessRunner,
    resolve_direct_runtime,
)
from inferdrome.evaluation.engine_comparison import (
    Digest,
    Engine,
    GpuObservations,
    GpuSample,
    MatchedPlan,
    PhaseReceipt,
    encoded,
)
from inferdrome.evaluation.load_calibration_rehearsal import (
    AsyncioLocalSubprocessRunner,
    LocalSubprocessRunner,
    _assert_loopback_port_closed,
    _verify_pinned_model_snapshot,
)
from inferdrome.evaluation.observations import (
    AiohttpProbeTransport,
    ProbeError,
    parse_load,
)
from inferdrome.evaluation.sglang_metrics import parse_sglang_metrics
from inferdrome.evaluation.sglang_profile import (
    LocalPath,
    SglangServingConfig,
    build_sglang_serving_profile,
)
from inferdrome.qwen3_campaign import QWEN3_8B_MODEL_ID, QWEN3_8B_REVISION
from inferdrome.routing_execution.canonical import sha256_digest


class EngineRuntime(ClosedModel):
    engine: Engine
    version: Literal["0.26.0", "0.5.15"]
    python: LocalPath
    python_sha256: Digest
    # Pre-reviewed exact importlib.metadata name/version inventory digest.
    packages_sha256: Digest
    cuda_home: LocalPath
    library_directories: Annotated[tuple[LocalPath, ...], Field(max_length=8)] = ()


class LocalInputs(ClosedModel):
    plan: MatchedPlan
    model_path: LocalPath
    chat_template_path: LocalPath
    vllm: EngineRuntime
    sglang: EngineRuntime


def launch_argv(
    runtime: EngineRuntime, inputs: LocalInputs, index: int
) -> tuple[str, ...]:
    """Finite matched single-device BF16 arguments, no free-form shell/argv."""
    plan = inputs.plan
    if index not in (0, 1) or (runtime.engine, runtime.version) not in (
        ("vllm", "0.26.0"),
        ("sglang", "0.5.15"),
    ):
        raise EvaluationError("comparison runtime version or GPU index unsupported")
    port = urlsplit(plan.workload.endpoints[index].origin).port
    if runtime.engine == "sglang":
        profile = SglangServingConfig(
            origin=plan.workload.endpoints[index].origin,
            served_model_name=QWEN3_8B_MODEL_ID,
            model_path=inputs.model_path,
            tokenizer_path=inputs.model_path,
            chat_template_path=inputs.chat_template_path,
            model_revision=QWEN3_8B_REVISION,
            tokenizer_revision=QWEN3_8B_REVISION,
            model_snapshot_sha256=plan.snapshot_sha256,
            tokenizer_snapshot_sha256=plan.snapshot_sha256,
            chat_template_sha256=plan.chat_template_sha256,
            context_length=plan.context_length,
            max_running_requests=plan.workload.bounds.concurrency,
            max_queued_requests=plan.workload.bounds.max_queue,
            mem_fraction_static=Decimal("0.90"),
            random_seed=plan.seed,
            prefix_cache="RADIX_DISABLED",
        )
        return (runtime.python, *build_sglang_serving_profile(profile).argv[1:])
    return (
        runtime.python,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        inputs.model_path,
        "--tokenizer",
        inputs.model_path,
        "--revision",
        QWEN3_8B_REVISION,
        "--tokenizer-revision",
        QWEN3_8B_REVISION,
        "--served-model-name",
        QWEN3_8B_MODEL_ID,
        "--chat-template",
        inputs.chat_template_path,
        "--dtype",
        "bfloat16",
        "--tensor-parallel-size",
        "1",
        "--gpu-memory-utilization",
        "0.90",
        "--max-model-len",
        str(plan.context_length),
        "--max-num-seqs",
        str(plan.workload.bounds.concurrency),
        "--seed",
        str(plan.seed),
        "--no-enable-prefix-caching",
        "--no-enable-chunked-prefill",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--disable-log-requests",
    )


def _hash(path: Path) -> str:
    if not path.is_file() or path.resolve() != path:
        raise EvaluationError("comparison artifact must be a regular canonical file")
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


class LocalEnginePair:
    def __init__(
        self,
        inputs: LocalInputs,
        engine: Engine,
        *,
        processes: DirectProcessRunner | None = None,
        commands: LocalSubprocessRunner | None = None,
        snapshot_verifier: Callable[[Path], None] = _verify_pinned_model_snapshot,
    ) -> None:
        self.inputs = LocalInputs.model_validate_json(
            encoded(inputs.model_dump(mode="json"))
        )
        self.runtime = self.inputs.vllm if engine == "vllm" else self.inputs.sglang
        if self.runtime.engine != engine:
            raise EvaluationError("comparison engine runtime mismatch")
        self.processes = processes or AsyncioDirectProcessRunner()
        self.commands = commands or AsyncioLocalSubprocessRunner(
            environment={"PATH": "/usr/bin:/bin"}
        )
        self.verify_snapshot = snapshot_verifier
        self.leases: list[DirectProcessLease] = []
        self.cache: tempfile.TemporaryDirectory[str] | None = None
        self.worker: asyncio.Task[None] | None = None
        self.poller: asyncio.Task[None] | None = None
        self.samples: list[GpuSample] = []
        self.sample_stop = asyncio.Event()
        self.hardware: list[list[str]] = []
        self.runtime_touched = False

    async def _command(self, argv: tuple[str, ...]) -> bytes:
        result = await self.commands.run(argv, timeout_ns=5_000_000_000)
        if result.returncode != 0 or len(result.stdout) > 65536:
            raise EvaluationError("comparison local readback unavailable")
        return result.stdout

    async def _gpu(self) -> tuple[list[list[str]], tuple[int, int], tuple[int, int]]:
        content = await self._command(
            (
                "/usr/bin/nvidia-smi",
                "--query-gpu=index,uuid,name,driver_version,memory.total,memory.used,utilization.gpu,mig.mode.current",
                "--format=csv,noheader,nounits",
            )
        )
        rows = [
            [s.strip() for s in line.split(",")]
            for line in content.decode("ascii").splitlines()
        ]
        if len(rows) != 2 or any(len(row) != 8 for row in rows):
            raise EvaluationError("comparison requires exactly two GPU observations")
        for i, row in enumerate(rows):
            if (
                row[0] != str(i)
                or row[2] != "NVIDIA A100-SXM4-40GB"
                or not 39000 <= int(row[4]) <= 41000
                or row[7] != "Disabled"
            ):
                raise EvaluationError("comparison GPU topology changed")
        if rows[0][1] == rows[1][1] or rows[0][3] != rows[1][3]:
            raise EvaluationError("comparison GPU identity ambiguous")
        identity = [[*r[:5], r[7]] for r in rows]
        return (
            identity,
            (int(rows[0][5]), int(rows[1][5])),
            (int(rows[0][6]), int(rows[1][6])),
        )

    async def _idle(self) -> None:
        content = await self._command(
            (
                "/usr/bin/nvidia-smi",
                "--query-compute-apps=pid",
                "--format=csv,noheader,nounits",
            )
        )
        if content.strip():
            raise EvaluationError("comparison GPU cleanup/idle unconfirmed")
        for endpoint in self.inputs.plan.workload.endpoints:
            port = urlsplit(endpoint.origin).port
            assert port is not None
            _assert_loopback_port_closed(port, 0.1)

    def _artifact_check(self) -> None:
        self.verify_snapshot(Path(self.inputs.model_path))
        if (
            _hash(Path(self.inputs.chat_template_path))
            != self.inputs.plan.chat_template_sha256
            or _hash(Path(self.runtime.python)) != self.runtime.python_sha256
        ):
            raise EvaluationError("comparison local artifact digest mismatch")

    async def prepare(self, plan: MatchedPlan, *, stop: asyncio.Event) -> PhaseReceipt:
        if plan != self.inputs.plan or self.leases or self.worker or stop.is_set():
            raise EvaluationError("comparison driver plan or ownership changed")
        self.worker = asyncio.create_task(asyncio.to_thread(self._artifact_check))
        await asyncio.shield(self.worker)
        self.worker = None
        executable = resolve_direct_runtime(self.runtime.python)
        # Only installed distribution metadata is imported, never the engine.
        script = (
            "import importlib.metadata as m,json; "
            "print(json.dumps(sorted((d.metadata['Name'].lower().replace('_','-'),"
            "d.version) for d in m.distributions()),separators=(',',':')))"
        )
        inventory = json.loads(
            await self._command((self.runtime.python, "-I", "-B", "-c", script))
        )
        if (
            sha256_digest(encoded(inventory)) != self.runtime.packages_sha256
            or [self.runtime.engine, self.runtime.version] not in inventory
        ):
            raise EvaluationError("comparison installed runtime inventory mismatch")
        self.runtime_touched = True
        await self._idle()
        self.hardware, _, _ = await self._gpu()
        self.cache = tempfile.TemporaryDirectory(prefix="inferdrome-comparison-")
        started = monotonic_ns()
        commands = []
        for index in (0, 1):
            if stop.is_set():
                raise EvaluationError("comparison stopped before engine launch")
            argv = launch_argv(self.runtime, self.inputs, index)
            commands.append(argv)
            private = Path(self.cache.name) / str(index)
            private.mkdir(mode=0o700)
            environment = {
                "PATH": f"{self.runtime.cuda_home}/bin:/usr/bin:/bin",
                "CUDA_HOME": self.runtime.cuda_home,
                "LD_LIBRARY_PATH": ":".join(self.runtime.library_directories),
                "CUDA_VISIBLE_DEVICES": str(index),
                "HOME": str(private),
                "TMPDIR": str(private),
                "XDG_CACHE_HOME": str(private),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            self.leases.append(
                await self.processes.start(executable, argv, environment=environment)
            )
        probe = AiohttpProbeTransport(plan.workload, max_response_bytes=4096)
        try:
            while True:
                if stop.is_set():
                    raise EvaluationError("comparison startup cancelled")
                try:
                    async with asyncio.timeout(2):
                        responses = [
                            await probe.get(e.origin, "/health")
                            for e in plan.workload.endpoints
                        ]
                    if all(r.status == 200 for r in responses):
                        break
                except (ProbeError, TimeoutError):
                    pass
                await asyncio.sleep(0.25)
        finally:
            await probe.close()
        return PhaseReceipt(
            engine=self.runtime.engine,
            version=self.runtime.version,
            runtime_sha256=sha256_digest(encoded(self.runtime.model_dump(mode="json"))),
            hardware_sha256=sha256_digest(encoded(self.hardware)),
            plan_sha256=plan.digest,
            launch_sha256=sha256_digest(encoded(commands)),
            startup_to_ready_ns=monotonic_ns() - started,
        )

    async def begin(self) -> None:
        # Warmup has fully drained. Require fresh idle server observations before
        # measuring; a request-local KV cache cannot survive a finished request.
        probe = AiohttpProbeTransport(
            self.inputs.plan.workload, max_response_bytes=1_048_576
        )
        try:
            async with asyncio.timeout(10):
                for endpoint in self.inputs.plan.workload.endpoints:
                    response = await probe.get(endpoint.origin, "/metrics")
                    if response.status != 200:
                        raise EvaluationError("comparison idle metrics unavailable")
                    if self.runtime.engine == "vllm":
                        running, queued = parse_load(
                            response.body, model=QWEN3_8B_MODEL_ID
                        )
                    else:
                        counts = parse_sglang_metrics(
                            response.body,
                            model=QWEN3_8B_MODEL_ID,
                            started_ns=0,
                            completed_ns=0,
                        ).counts
                        running, queued = (
                            counts.reported_running_requests,
                            counts.reported_queued_requests,
                        )
                    if running or queued:
                        raise EvaluationError("comparison warmup did not drain")
        finally:
            await probe.close()
        self.samples = []
        self.sample_stop.clear()
        started = monotonic_ns()

        async def sample() -> None:
            while not self.sample_stop.is_set():
                hardware, memory, utilization = await self._gpu()
                if hardware != self.hardware:
                    raise EvaluationError(
                        "comparison hardware changed during measurement"
                    )
                self.samples.append(
                    GpuSample(
                        elapsed_ns=monotonic_ns() - started,
                        memory_mib=memory,
                        utilization_percent=utilization,
                    )
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.sample_stop.wait(), 1)

        self.poller = asyncio.create_task(sample())

    async def finish(self) -> GpuObservations:
        self.sample_stop.set()
        if self.poller:
            await self.poller
            self.poller = None
        hardware, _, _ = await self._gpu()
        if hardware != self.hardware:
            raise EvaluationError("comparison hardware changed after measurement")
        return GpuObservations(
            status="SAMPLED" if self.samples else "UNAVAILABLE",
            samples=tuple(self.samples),
        )

    async def cleanup(self) -> None:
        self.sample_stop.set()
        errors = []
        if self.poller:
            try:
                await self.poller
            except Exception as error:
                errors.append(error)
            self.poller = None
        if self.worker:
            await asyncio.shield(self.worker)
            self.worker = None
        for lease in tuple(reversed(self.leases)):
            try:
                await self.processes.terminate(lease, timeout_ns=10_000_000_000)
                self.leases.remove(lease)
            except Exception as error:
                errors.append(error)
        if self.runtime_touched:
            await self._idle()
        if errors:
            raise EvaluationError("comparison exact process cleanup unconfirmed")
        if self.cache:
            self.cache.cleanup()
            self.cache = None


def drivers(inputs: LocalInputs) -> dict[Engine, LocalEnginePair]:
    return {
        "vllm": LocalEnginePair(inputs, "vllm"),
        "sglang": LocalEnginePair(inputs, "sglang"),
    }
