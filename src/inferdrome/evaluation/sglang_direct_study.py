"""One host, two independent replicas: two smoke trials then four policies.

This explicit local entrypoint never provisions, installs, uploads or publishes.
It requires a pre-reviewed runtime manifest and a preloaded Qwen3 snapshot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from time import monotonic_ns
from typing import Protocol

from inferdrome.evaluation.contracts import EndpointId, EvaluationError
from inferdrome.evaluation.direct_process_lifecycle import resolve_direct_runtime
from inferdrome.evaluation.engine_binding import (
    build_sglang_direct_binding,
    engine_binding_bytes,
)
from inferdrome.evaluation.sglang_direct_lifecycle import Sglang015DirectLifecycle
from inferdrome.evaluation.sglang_direct_runtime import (
    SglangDirectRuntimeManifest,
    runtime_manifest_bytes,
    runtime_manifest_sha256,
)
from inferdrome.evaluation.sglang_execution import SGLangStudyExecutor
from inferdrome.evaluation.sglang_profile import SglangServingConfig
from inferdrome.evaluation.sglang_results import SGLangTrialResult, sglang_trial_bytes
from inferdrome.evaluation.study import plan_bytes, report_study, run_study
from inferdrome.evaluation.study_config import CompiledTrial, StudyConfig, compile_study
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes

PREPARE_NS = 660_000_000_000
CLEANUP_NS = 240_000_000_000


class DirectExecutor(Protocol):
    async def __call__(
        self, trial: CompiledTrial, *, stop: asyncio.Event
    ) -> SGLangTrialResult: ...


def _write(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)


async def run_sglang_direct_smoke_study(
    config: StudyConfig,
    profiles: Mapping[EndpointId, SglangServingConfig],
    runtime: SglangDirectRuntimeManifest,
    output: Path,
    *,
    stop: asyncio.Event | None = None,
    lifecycle: Sglang015DirectLifecycle | None = None,
    executor: DirectExecutor | None = None,
) -> None:
    """Run the bounded local plan. Injected owner/executor are trusted test code."""
    plan = compile_study(config)
    binding = build_sglang_direct_binding(
        plan, profiles, runtime_manifest_sha256=runtime_manifest_sha256(runtime)
    )
    if (
        len(plan.trials) != 4
        or len(config.blocks) != 1
        or config.blocks[0].scenario != "HEALTHY"
        or any(
            p.served_model_name != QWEN3_8B_MODEL_ID
            or p.model_revision != QWEN3_8B_REVISION
            or p.model_snapshot_sha256 != qwen3_expected_snapshot_sha256()
            or p.tokenizer_snapshot_sha256 != qwen3_expected_snapshot_sha256()
            for p in profiles.values()
        )
    ):
        raise EvaluationError(
            "direct study requires the smallest four-policy Qwen3 plan"
        )
    owner = lifecycle or Sglang015DirectLifecycle(
        profiles,
        contexts=((plan, binding),),
        runtime_manifest=runtime,
        ownership_id="sglang015-study",
        executable=resolve_direct_runtime(runtime.roles["python"]),
        startup_timeout_ns=300_000_000_000,
        readiness_timeout_ns=10_000_000_000,
    )
    # A caller cannot accidentally reuse an owner bound to another engine choice.
    from inferdrome.evaluation.engine_binding import engine_choice_sha256

    if owner.engine_choice_sha256 != engine_choice_sha256(binding):
        raise EvaluationError("direct study owner has a different engine choice")
    native = executor or SGLangStudyExecutor(plan, binding, profiles)
    stop = stop or asyncio.Event()
    output.mkdir(mode=0o700)
    smoke = output / "smoke"
    smoke.mkdir(mode=0o700)
    _write(output / "runtime-manifest.json", runtime_manifest_bytes(runtime))
    _write(smoke / "plan.json", plan_bytes(plan))
    _write(smoke / "engine-binding.json", engine_binding_bytes(binding))
    count = 0

    async def one(trial: CompiledTrial, *, stop: asyncio.Event) -> SGLangTrialResult:
        nonlocal count
        try:
            if stop.is_set():
                raise EvaluationError("direct study stopped before preparation")
            owner.set_operation_deadline(monotonic_ns() + PREPARE_NS)
            async with asyncio.timeout(PREPARE_NS / 1_000_000_000):
                await owner.prepare(trial, stop=stop)
            if stop.is_set():
                raise EvaluationError("direct study stopped before dispatch")
            result = await native(trial, stop=stop)
            await _check_after_trial(owner, stop)
            return result
        finally:
            owner.set_operation_deadline(monotonic_ns() + CLEANUP_NS)
            try:
                async with asyncio.timeout(CLEANUP_NS / 1_000_000_000):
                    await owner.cleanup(trial, stop=stop)
            finally:
                receipt = {
                    "processes": [asdict(r) for r in owner.receipts],
                    "resets": [[asdict(r) for r in pair] for pair in owner.resets],
                    "loaded_runtime": owner.loaded_runtime_receipts,
                    "runtime_verification": "UNVERIFIED",
                    "evidence_eligible": False,
                }
                _write(
                    output / f"lifecycle-{count:02d}.json",
                    canonical_json_bytes(receipt) + b"\n",
                )
                count += 1

    status = "FAILED"
    try:
        for trial in plan.trials[:2]:
            result = await one(trial, stop=stop)
            content = sglang_trial_bytes(plan, trial, result, binding)
            _write(smoke / f"{trial.trial_id}.json", content)
            records = result.to_dict()["foreground"]["records"]
            if (
                result.status != "COMPLETED"
                or not records
                or any(r["outcome"] != "SUCCESS" for r in records)
            ):
                raise EvaluationError("direct smoke failed; study withheld")
        _write(
            smoke / "outcome.json",
            canonical_json_bytes(
                {
                    "status": "TWO_TRIAL_SMOKE_ONLY",
                    "completed_study": False,
                    "evidence_eligible": False,
                }
            )
            + b"\n",
        )
        manifest = await run_study(
            config, output / "study", executor=one, stop=stop, engine_binding=binding
        )
        report_study(
            config, output / "study", output / "report", engine_binding=binding
        )
        if manifest.status != "COMPLETED":
            raise EvaluationError("direct study did not complete")
        status = "COMPLETED_LOCAL_ONLY"
    finally:
        _write(
            output / "outcome.json",
            canonical_json_bytes(
                {
                    "status": status,
                    "runtime_verification": "UNVERIFIED",
                    "evidence_eligible": False,
                }
            )
            + b"\n",
        )


async def _check_after_trial(
    owner: Sglang015DirectLifecycle, stop: asyncio.Event
) -> None:
    check = asyncio.create_task(owner.check_loaded_runtime())
    stopped = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {check, stopped}, timeout=120, return_when=asyncio.FIRST_COMPLETED
        )
        if check not in done or stop.is_set():
            raise EvaluationError("direct post-trial provenance stopped or expired")
        await check
    finally:
        for task in (check, stopped):
            if not task.done():
                task.cancel()
        await asyncio.gather(check, stopped, return_exceptions=True)
