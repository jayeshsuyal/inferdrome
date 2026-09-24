"""Matched fixed-arrival replay across two engine pairs on one observed host.

The replay and independent population verifier are shared with routing studies.
Drivers own preparation and exact cleanup; no policy or timing parser is copied.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Protocol, Self, cast

from pydantic import Field, model_validator

from inferdrome.evaluation.contracts import (
    ClosedModel,
    EvaluationConfig,
    EvaluationError,
    load_config_bytes,
)
from inferdrome.evaluation.files import OutputFile
from inferdrome.evaluation.runner import EvaluationResult, run_evaluation
from inferdrome.evaluation.study_validation import load_fixed_population_bytes
from inferdrome.evaluation.transport import AiohttpTransport
from inferdrome.metrics.quantiles import nearest_rank
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

Engine = Literal["vllm", "sglang"]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Nanoseconds = Annotated[int, Field(ge=0, le=2**53 - 1)]
_ENGINES: tuple[Engine, Engine] = ("vllm", "sglang")
RESET: Final = "FRESH_PROCESS_PREFIX_CACHE_DISABLED_WARMUP_DRAIN"


def encoded(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


class MatchedPlan(ClosedModel):
    workload: EvaluationConfig
    model_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] = QWEN3_8B_REVISION
    snapshot_sha256: Digest
    chat_template_sha256: Digest
    dtype: Literal["bfloat16"] = "bfloat16"
    tensor_parallel_size: Literal[1] = 1
    seed: Annotated[int, Field(ge=0, le=2**31 - 1)] = 17
    context_length: Annotated[int, Field(ge=128, le=32768)] = 2048
    first_engine: Engine = "vllm"
    # Every repeat is AB then BA, preserving separate paired populations.
    counterbalanced_repeats: Annotated[int, Field(ge=1, le=4)] = 1
    prepare_seconds: Annotated[int, Field(ge=1, le=900)] = 660
    cleanup_seconds: Annotated[int, Field(ge=1, le=240)] = 120
    reset: Literal["FRESH_PROCESS_PREFIX_CACHE_DISABLED_WARMUP_DRAIN"] = RESET

    @model_validator(mode="after")
    def matched(self) -> Self:
        if (
            self.workload.model != QWEN3_8B_MODEL_ID
            or self.model_revision != QWEN3_8B_REVISION
            or self.snapshot_sha256 != qwen3_expected_snapshot_sha256()
            or any(
                not e.origin.startswith("http://127.0.0.1:")
                for e in self.workload.endpoints
            )
            or {o.endpoint_id for o in self.workload.offers}
            != {"endpoint-a", "endpoint-b"}
        ):
            raise ValueError(
                "comparison requires pinned Qwen3 and both loopback replicas"
            )
        return self

    def order(self) -> tuple[Engine, ...]:
        other: Engine = "sglang" if self.first_engine == "vllm" else "vllm"
        return (
            self.first_engine,
            other,
            other,
            self.first_engine,
        ) * self.counterbalanced_repeats

    def warmup(self) -> EvaluationConfig:
        value = self.workload.model_dump(mode="json")
        value["offers"] = [
            {
                "scheduled_ns": 0,
                "endpoint_id": endpoint.endpoint_id,
                "prompt": self.workload.offers[0].prompt,
            }
            for endpoint in self.workload.endpoints
        ]
        value["bounds"].update(max_requests=2, concurrency=2, max_queue=0)
        return load_config_bytes(encoded(value))

    @property
    def digest(self) -> str:
        return sha256_digest(encoded(self.model_dump(mode="json")))


class PhaseReceipt(ClosedModel):
    engine: Engine
    version: Annotated[
        str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.+-]{0,32})?$")
    ]
    runtime_sha256: Digest
    hardware_sha256: Digest
    plan_sha256: Digest
    launch_sha256: Digest
    reset: Literal["FRESH_PROCESS_PREFIX_CACHE_DISABLED_WARMUP_DRAIN"] = RESET
    gpu_indices: tuple[Literal[0], Literal[1]] = (0, 1)
    startup_to_ready_ns: Nanoseconds
    # Engines do not expose a common isolated model-load timer.
    model_load_ns: None = None
    runtime_verification: Literal["LOCAL_READBACK_NOT_ATTESTATION"] = (
        "LOCAL_READBACK_NOT_ATTESTATION"
    )


class GpuSample(ClosedModel):
    elapsed_ns: Nanoseconds
    memory_mib: tuple[
        Annotated[int, Field(ge=0, le=40960)], Annotated[int, Field(ge=0, le=40960)]
    ]
    utilization_percent: tuple[
        Annotated[int, Field(ge=0, le=100)], Annotated[int, Field(ge=0, le=100)]
    ]


class GpuObservations(ClosedModel):
    status: Literal["SAMPLED", "UNAVAILABLE"]
    interval_ns: Annotated[int, Field(ge=1, le=10_000_000_000)] = 1_000_000_000
    samples: Annotated[tuple[GpuSample, ...], Field(max_length=1000)] = ()

    @model_validator(mode="after")
    def coverage(self) -> Self:
        if (self.status == "SAMPLED") != bool(self.samples):
            raise ValueError("GPU sample coverage is inconsistent")
        if any(
            b.elapsed_ns <= a.elapsed_ns
            for a, b in zip(self.samples, self.samples[1:], strict=False)
        ):
            raise ValueError("GPU sample times must increase")
        return self


class PairDriver(Protocol):
    """Trusted local lifecycle, never a configuration-supplied Python hook."""

    async def prepare(
        self, plan: MatchedPlan, *, stop: asyncio.Event
    ) -> PhaseReceipt: ...
    async def begin(self) -> None: ...
    async def finish(self) -> GpuObservations: ...
    async def cleanup(self) -> None: ...


Replay = Callable[[EvaluationConfig, asyncio.Event], Awaitable[EvaluationResult]]


async def replay(config: EvaluationConfig, stop: asyncio.Event) -> EvaluationResult:
    return await run_evaluation(config, AiohttpTransport(config), stop=stop)


def _population(result: EvaluationResult, config: EvaluationConfig) -> EvaluationResult:
    return load_fixed_population_bytes(encoded(result.to_dict()), config)


def percentage_delta(
    reference: int | str | None, candidate: int | str | None
) -> str | None:
    """Signed (SGLang - vLLM) / vLLM * 100; zero baseline is unavailable."""
    if reference is None or candidate is None:
        return None
    with localcontext() as context:
        context.prec = 40
        left, right = Decimal(reference), Decimal(candidate)
        if not left.is_finite() or not right.is_finite() or left <= 0 or right < 0:
            return None
        return format(((right - left) * 100 / left).quantize(Decimal("0.000001")), "f")


def _summary(
    result: EvaluationResult, receipt: PhaseReceipt, gpu: GpuObservations
) -> dict[str, Any]:
    good = [r for r in result.records if r.outcome == "SUCCESS"]
    ttft = [
        r.first_content_ns - r.scheduled_ns
        for r in good
        if r.first_content_ns is not None
    ]
    e2e = [r.terminal_ns - r.scheduled_ns for r in good]
    counts = Counter(r.outcome for r in result.records)
    rejected = sum(v for k, v in counts.items() if k.startswith("REJECTED_"))
    token_coverage = sum(r.completion_tokens is not None for r in good)
    tokens = sum(r.completion_tokens or 0 for r in good)
    # Fixed offered duration plus actual drain through last terminal. Never
    # count tokens from failed/partial streams or hide missing usage as zero.
    window = max(
        cast(int, result.bounds["duration_ns"]),
        *(r.terminal_ns for r in result.records),
    )
    rate = None
    if good and token_coverage == len(good):
        with localcontext() as context:
            context.prec = 40
            rate = format(Decimal(tokens * 1_000_000_000) / Decimal(window), ".6f")
    metrics = {
        "ttft_p50_ns": nearest_rank(tuple(ttft), 50) if ttft else None,
        "ttft_p95_ns": nearest_rank(tuple(ttft), 95) if ttft else None,
        "e2e_p50_ns": nearest_rank(tuple(e2e), 50) if e2e else None,
        "e2e_p95_ns": nearest_rank(tuple(e2e), 95) if e2e else None,
        "output_tokens_per_second": rate,
        "startup_to_ready_ns": receipt.startup_to_ready_ns,
        "model_load_ns": None,
        "peak_gpu_memory_mib": max(
            (max(s.memory_mib) for s in gpu.samples), default=None
        ),
        "peak_gpu_utilization_percent": max(
            (max(s.utilization_percent) for s in gpu.samples), default=None
        ),
    }
    return {
        "metrics": metrics,
        "population": {
            "offered": len(result.records),
            "success": len(good),
            "error": len(result.records) - len(good) - rejected,
            "rejected": rejected,
            "outcomes": dict(sorted(counts.items())),
        },
        "ttft_success_coverage": len(ttft),
        "usage_success_coverage": token_coverage,
        "successful_output_tokens": str(tokens),
        "throughput_window_ns": window,
        "gpu_coverage": gpu.status,
    }


def comparison_report(
    plan: MatchedPlan,
    phases: list[dict[str, Any]],
    *,
    completed: bool,
    failure_cleanup: str | None = None,
) -> dict[str, Any]:
    """Offline recomputation: validates all populations and matched receipts."""
    plan = MatchedPlan.model_validate_json(encoded(plan.model_dump(mode="json")))
    if len(phases) > len(plan.order()) or (
        completed and len(phases) != len(plan.order())
    ):
        raise EvaluationError("comparison phase coverage is incomplete")
    hardware = None
    runtime: dict[str, str] = {}
    summaries = []
    for index, phase in enumerate(phases):
        if (
            set(phase) != {"receipt", "warmup", "population", "gpu", "cleanup"}
            or phase["cleanup"] != "CONFIRMED"
        ):
            raise EvaluationError("comparison phase cleanup is unconfirmed")
        receipt = PhaseReceipt.model_validate_json(encoded(phase["receipt"]))
        gpu = GpuObservations.model_validate_json(encoded(phase["gpu"]))
        if receipt.engine != plan.order()[index] or receipt.plan_sha256 != plan.digest:
            raise EvaluationError("comparison engine order or workload changed")
        if hardware is not None and hardware != receipt.hardware_sha256:
            raise EvaluationError("comparison hardware changed")
        hardware = receipt.hardware_sha256
        runtime_identity = sha256_digest(
            encoded(
                {
                    "version": receipt.version,
                    "runtime": receipt.runtime_sha256,
                    "launch": receipt.launch_sha256,
                }
            )
        )
        if runtime.setdefault(receipt.engine, runtime_identity) != runtime_identity:
            raise EvaluationError("comparison runtime changed between repeats")
        warmup = load_fixed_population_bytes(encoded(phase["warmup"]), plan.warmup())
        if any(r.outcome != "SUCCESS" for r in warmup.records):
            raise EvaluationError("comparison warmup did not drain successfully")
        result = load_fixed_population_bytes(
            encoded(phase["population"]), plan.workload
        )
        if result.cancelled or any(r.outcome == "CANCELLED" for r in result.records):
            raise EvaluationError("comparison cancelled population is incomplete")
        summaries.append(_summary(result, receipt, gpu))
    pairs = []
    if completed:
        for i in range(0, len(phases), 2):
            by_engine = {
                phases[j]["receipt"]["engine"]: summaries[j] for j in (i, i + 1)
            }
            left, right = by_engine["vllm"]["metrics"], by_engine["sglang"]["metrics"]
            pairs.append(
                {
                    "pair": i // 2,
                    "phase_indices": [i, i + 1],
                    "sglang_vs_vllm_percent": {
                        k: percentage_delta(left[k], right[k]) for k in left
                    },
                }
            )
    report = {
        "schema_version": "inferdrome.matched-engine-comparison.v1",
        "plan_sha256": plan.digest,
        "source_commit": plan.workload.source_commit,
        "status": "COMPLETED" if completed else "ABORTED",
        "failed_phase_cleanup": failure_cleanup,
        "engine_order": list(plan.order()),
        "completed_phases": len(phases),
        "phases": phases,
        "summaries": summaries,
        "pairs": pairs,
        "metric_definitions": {
            "latency": "SUCCESS_ONLY_NEAREST_RANK_FROM_SCHEDULED_ARRIVAL",
            "ttft": "FIRST_NONEMPTY_CONTENT_EVENT_NOT_WIRE_BYTE_OR_EXACT_TOKEN",
            "e2e": "TERMINAL_MINUS_SCHEDULED_INCLUDING_CLIENT_QUEUE",
            "throughput": "SUCCESS_SERVER_USAGE_TOKENS_OVER_OFFERED_WINDOW_AND_DRAIN",
            "gpu": "SAMPLED_MAX_PER_DEVICE_DURING_MEASUREMENT_NOT_CONTINUOUS_PEAK",
            "startup": "PAIR_LAUNCH_TO_HEALTH_READY_NOT_ISOLATED_MODEL_LOAD",
            "delta": "100*(SGLANG-VLLM)/VLLM;ZERO_OR_MISSING_BASELINE_UNAVAILABLE",
        },
        "limitations": [
            "DESCRIPTIVE_MATCHED_PAIRS_NO_POOLING_OR_WINNER",
            "ENGINE_SCHEDULERS_AND_KERNELS_DIFFER",
            "DECLARATIONS_AND_LOCAL_READBACKS_NOT_REMOTE_ATTESTATION",
            "GPU_SAMPLING_CAN_MISS_PEAKS",
            "MODEL_LOAD_TIMER_UNAVAILABLE",
            "NO_GPU_QUALIFICATION_FROM_CPU_TESTS",
        ],
        "evidence_eligible": False,
    }
    return {**report, "sha256": sha256_digest(encoded(report))}


def render_comparison(report: dict[str, Any]) -> str:
    rows = [
        "# Matched vLLM / SGLang comparison",
        "",
        f"Status: {report['status']}",
        "",
        "Signed deltas are SGLang relative to vLLM. No winner is inferred.",
        "Latency uses successful requests and includes client queue time. "
        "Populations stay separate.",
    ]
    for i, phase in enumerate(report["phases"]):
        summary = report["summaries"][i]
        rows += [
            "",
            f"Phase {i}: {phase['receipt']['engine']} {phase['receipt']['version']}",
            f"Population: {summary['population']}",
            f"Metrics: {summary['metrics']}",
        ]
    for pair in report["pairs"]:
        rows += [
            "",
            f"Pair {pair['pair']} deltas (%): {pair['sglang_vs_vllm_percent']}",
        ]
    rows += [
        "",
        "Unavailable values are null; zero baselines have no percentage delta.",
        "Startup covers pair launch to health readiness; "
        "isolated model-load time is unavailable.",
        "GPU peaks are sampled per-device maxima. Runtime remains unverified.",
        f"Report digest: {report['sha256']}",
        "",
    ]
    return "\n".join(rows)


async def run_comparison(
    plan: MatchedPlan,
    drivers: dict[Engine, PairDriver],
    output: Path,
    *,
    stop: asyncio.Event | None = None,
    execute: Replay = replay,
) -> dict[str, Any]:
    """Serial AB/BA phases; cleanup also runs after failed/partial preparation."""
    plan = MatchedPlan.model_validate_json(encoded(plan.model_dump(mode="json")))
    if set(drivers) != set(_ENGINES) or drivers["vllm"] is drivers["sglang"]:
        raise EvaluationError("comparison requires distinct engine owners")
    stop = stop or asyncio.Event()
    output.mkdir(mode=0o700)
    phases: list[dict[str, Any]] = []
    completed = False
    cleanup_state: str | None = None
    try:
        for engine in plan.order():
            owner = drivers[engine]
            try:
                if stop.is_set():
                    raise EvaluationError("comparison cancelled")
                async with asyncio.timeout(plan.prepare_seconds):
                    receipt = await owner.prepare(plan, stop=stop)
                    receipt = PhaseReceipt.model_validate_json(
                        encoded(receipt.model_dump(mode="json"))
                    )
                    if receipt.engine != engine or receipt.plan_sha256 != plan.digest:
                        raise EvaluationError("comparison preparation binding changed")
                    if (
                        phases
                        and phases[0]["receipt"]["hardware_sha256"]
                        != receipt.hardware_sha256
                    ):
                        raise EvaluationError(
                            "comparison hardware changed before dispatch"
                        )
                    warmup = _population(
                        await execute(plan.warmup(), stop), plan.warmup()
                    )
                    if any(r.outcome != "SUCCESS" for r in warmup.records):
                        raise EvaluationError("comparison warmup failed")
                if stop.is_set():
                    raise EvaluationError("comparison cancelled")
                await owner.begin()
                result = _population(await execute(plan.workload, stop), plan.workload)
                gpu = await owner.finish()
            finally:
                # A fresh cleanup reservation survives execution deadline/stop.
                cleanup_state = "UNCONFIRMED"
                cleanup = asyncio.create_task(owner.cleanup())
                deadline = asyncio.get_running_loop().time() + plan.cleanup_seconds
                while not cleanup.done():
                    try:
                        await asyncio.wait(
                            {cleanup},
                            timeout=max(
                                0, deadline - asyncio.get_running_loop().time()
                            ),
                        )
                    except asyncio.CancelledError:
                        stop.set()
                    if (
                        asyncio.get_running_loop().time() >= deadline
                        and not cleanup.done()
                    ):
                        cleanup.cancel()
                        raise EvaluationError("comparison cleanup deadline expired")
                cleanup.result()
                cleanup_state = "CONFIRMED"
            phase = {
                "receipt": receipt.model_dump(mode="json"),
                "warmup": warmup.to_dict(),
                "population": result.to_dict(),
                "gpu": gpu.model_dump(mode="json"),
                "cleanup": "CONFIRMED",
            }
            # Validate before advancing to the next pair; catches runtime drift.
            comparison_report(plan, [*phases, phase], completed=False)
            phases.append(phase)
            with OutputFile(output / f"phase-{len(phases) - 1:02d}.json") as file:
                file.write(encoded(phase))
        completed = True
    except (Exception, asyncio.CancelledError):
        raise EvaluationError(
            "comparison aborted; inspect report and cleanup state"
        ) from None
    finally:
        report = comparison_report(
            plan,
            phases,
            completed=completed,
            failure_cleanup=None if completed else cleanup_state,
        )
        with OutputFile(output / "report.json") as file:
            file.write(encoded(report))
        with OutputFile(output / "report.md") as file:
            file.write(render_comparison(report).encode())
    return report
