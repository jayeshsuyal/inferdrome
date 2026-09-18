"""Sequential matched trials with an explicit incomplete-study ledger.

Injected executors are trusted cancellable Python code, never plan-supplied
hooks. A returned trial owns and closes its clients before another may start.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import Field, ValidationError

from inferdrome.evaluation.contracts import ClosedModel, EvaluationError
from inferdrome.evaluation.engine_binding import (
    MAX_ENGINE_BINDING_BYTES,
    EvaluationEngineBinding,
    engine_binding_bytes,
    load_engine_binding_bytes,
)
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.faults import (
    RoutingFaultResult,
    close_routing_clients,
    run_routing_fault,
)
from inferdrome.evaluation.healthy import HealthyRoutingResult, run_routing_healthy
from inferdrome.evaluation.observations import AiohttpProbeTransport, ProbeTransport
from inferdrome.evaluation.runner import Clock, SystemClock, Transport
from inferdrome.evaluation.sglang_results import (
    SGLangTrialResult,
    load_sglang_trial_bytes,
    sglang_trial_bytes,
)
from inferdrome.evaluation.study_config import (
    PLAN_MANIFEST_RESERVE_BYTES,
    CompiledStudy,
    CompiledTrial,
    StudyConfig,
    compile_study,
)
from inferdrome.evaluation.study_files import (
    MAX_METADATA_BYTES,
    StudyDirectory,
    trial_filename,
)
from inferdrome.evaluation.study_validation import (
    ValidatedTrialResult,
    load_trial_result_bytes,
)
from inferdrome.evaluation.transport import AiohttpTransport
from inferdrome.parsing import StructuredDataLimits, validate_json_structure
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

TrialResult = RoutingFaultResult | HealthyRoutingResult | SGLangTrialResult
StudyStatus = Literal["COMPLETED", "CANCELLED", "ABORTED"]
FailureReason = Literal[
    "CANCELLED",
    "STUDY_DEADLINE",
    "WARMUP_FAILED",
    "CONTROLLER_OR_CLEANUP_FAILED",
    "RESULT_VALIDATION_FAILED",
    "OUTPUT_FAILED",
]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_ENVELOPE_LIMITS = StructuredDataLimits(
    max_depth=33, max_tokens=4_000_100, max_integer_digits=16
)


class TrialLedger(ClosedModel):
    trial_id: Annotated[str, Field(pattern=r"^trial-[0-9]{4}$")]
    state: Literal["RETURNED", "ABORTED", "NOT_RUN"]
    result_filename: Annotated[str, Field(pattern=r"^trial-[0-9]{4}\.json$")] | None
    result_sha256: Digest | None
    result_status: Literal["COMPLETED", "WARMUP_FAILED", "CANCELLED"] | None
    failure_reason: FailureReason | None
    cleanup: Literal["CONFIRMED_BY_LOCAL_RESULT", "UNCONFIRMED", "NOT_STARTED"]


class StudyManifest(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-study-result.v1"]
    plan_sha256: Digest
    config_sha256: Digest
    status: StudyStatus
    reason: FailureReason | None
    elapsed_ns: Annotated[int, Field(ge=0, le=2**53 - 1)]
    trials: Annotated[tuple[TrialLedger, ...], Field(min_length=4, max_length=256)]
    evidence_eligible: Literal[False] = False


class StudyExecutor(Protocol):
    async def __call__(
        self, trial: CompiledTrial, *, stop: asyncio.Event
    ) -> TrialResult: ...


class StudyTrialArtifact(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-study-trial-result.v1"]
    plan_sha256: Digest
    trial_id: Annotated[str, Field(pattern=r"^trial-[0-9]{4}$")]
    block_id: str
    workload_sha256: Digest
    config_sha256: Digest
    result: dict[str, object]


def _trial_bytes(
    plan: CompiledStudy, trial: CompiledTrial, result: TrialResult
) -> bytes:
    if isinstance(result, SGLangTrialResult):
        raise EvaluationError("SGLang results require their bound v2 envelope")
    artifact = StudyTrialArtifact(
        schema_version="inferdrome.evaluation-study-trial-result.v1",
        plan_sha256=sha256_digest(plan_bytes(plan)),
        trial_id=trial.trial_id,
        block_id=trial.block_id,
        workload_sha256=trial.workload_sha256,
        config_sha256=trial.config_sha256,
        result=result.to_dict(),
    )
    return canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n"


def load_study_trial_bytes(
    content: bytes, plan: CompiledStudy, trial: CompiledTrial
) -> ValidatedTrialResult:
    try:
        if not 1 <= len(content) <= plan.config.limits.per_trial_result_bytes:
            raise ValueError
        validate_json_structure(content.decode("utf-8"), limits=_ENVELOPE_LIMITS)
        artifact = StudyTrialArtifact.model_validate_json(content)
        if (
            canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n" != content
            or artifact.plan_sha256 != sha256_digest(plan_bytes(plan))
            or artifact.trial_id != trial.trial_id
            or artifact.block_id != trial.block_id
            or artifact.workload_sha256 != trial.workload_sha256
            or artifact.config_sha256 != trial.config_sha256
        ):
            raise ValueError
        validated = load_trial_result_bytes(
            canonical_json_bytes(artifact.result) + b"\n", trial.config
        )
        return replace(validated, result_sha256=sha256_digest(content))
    except (ValueError, ValidationError, RecursionError):
        raise EvaluationError(
            "study trial artifact violates its expected binding"
        ) from None


def plan_bytes(plan: CompiledStudy) -> bytes:
    return canonical_json_bytes(plan.to_dict()) + b"\n"


def _not_run(trial: CompiledTrial) -> TrialLedger:
    return TrialLedger(
        trial_id=trial.trial_id,
        state="NOT_RUN",
        result_filename=None,
        result_sha256=None,
        result_status=None,
        failure_reason=None,
        cleanup="NOT_STARTED",
    )


def _manifest(
    plan: CompiledStudy,
    entries: list[TrialLedger],
    *,
    status: StudyStatus,
    reason: FailureReason | None,
    elapsed_ns: int,
) -> StudyManifest:
    return StudyManifest(
        schema_version="inferdrome.evaluation-study-result.v1",
        plan_sha256=sha256_digest(plan_bytes(plan)),
        config_sha256=plan.config_sha256,
        status=status,
        reason=reason,
        elapsed_ns=elapsed_ns,
        trials=tuple(entries),
    )


def _encoded(manifest: StudyManifest) -> bytes:
    return canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"


def preflight_metadata(
    plan: CompiledStudy, engine_binding: EvaluationEngineBinding | None = None
) -> None:
    # Reserve the largest ledger shape, plus slack for finite failure categories
    # and the elapsed clock. This happens before creating clients or a directory.
    entries = [
        TrialLedger(
            trial_id=trial.trial_id,
            state="RETURNED",
            result_filename=trial_filename(index),
            result_sha256="sha256:" + "f" * 64,
            result_status="COMPLETED",
            failure_reason=None,
            cleanup="CONFIRMED_BY_LOCAL_RESULT",
        )
        for index, trial in enumerate(plan.trials)
    ]
    largest = _manifest(
        plan, entries, status="COMPLETED", reason=None, elapsed_ns=2**53 - 1
    )
    if (
        len(plan_bytes(plan))
        + len(_encoded(largest))
        + (len(engine_binding_bytes(engine_binding)) if engine_binding else 0)
        + 4096
        > PLAN_MANIFEST_RESERVE_BYTES
    ):
        raise EvaluationError("study plan and ledger exceed their metadata reserve")


async def execute_trial(trial: CompiledTrial, *, stop: asyncio.Event) -> TrialResult:
    """Use only the PR1/PR2 transport and public scenario entrypoints."""
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
            config.foreground, max_response_bytes=config.telemetry.max_response_bytes
        )
        clients.append(router)
        observer = AiohttpProbeTransport(
            config.foreground, max_response_bytes=config.telemetry.max_response_bytes
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
    if isinstance(config, RoutingFaultConfig):
        assert background is not None
        return await run_routing_fault(
            config,
            foreground,
            background,
            router,
            observer,
            stop=stop,
        )
    return await run_routing_healthy(
        config,
        foreground,
        router,
        observer,
        stop=stop,
    )


async def _cooldown(clock: Clock, deadline_ns: int, stop: asyncio.Event) -> None:
    sleeper = asyncio.create_task(clock.sleep_until(deadline_ns))
    interrupted = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait([sleeper, interrupted], return_when=asyncio.FIRST_COMPLETED)
        if sleeper.done():
            sleeper.result()
        if not stop.is_set() and clock.now_ns() < deadline_ns:
            raise EvaluationError("study cooldown woke before its deadline")
    finally:
        sleeper.cancel()
        interrupted.cancel()
        await asyncio.gather(sleeper, interrupted, return_exceptions=True)


async def run_study(
    config: StudyConfig,
    output_dir: Path,
    *,
    executor: StudyExecutor = execute_trial,
    stop: asyncio.Event | None = None,
    clock: Clock | None = None,
    engine_binding: EvaluationEngineBinding | None = None,
) -> StudyManifest:
    """Publish each completed local result once; abort later trials on failure.

    The wall-time stop includes cooldown. Existing per-trial cleanup bounds own
    draining after a stop; filesystem fsync and scheduling are not hard real-time.
    """
    plan = compile_study(config)
    if engine_binding is not None:
        engine_binding = load_engine_binding_bytes(
            engine_binding_bytes(engine_binding), plan
        )
        if executor is execute_trial:
            raise EvaluationError("SGLang binding requires an explicit SGLang executor")
    if engine_binding is None:
        preflight_metadata(plan)
    else:
        preflight_metadata(plan, engine_binding)
    clock = clock or SystemClock()
    stop = stop or asyncio.Event()
    started = clock.now_ns()
    expired = False
    status: StudyStatus = "COMPLETED"
    reason: FailureReason | None = None
    entries = [_not_run(trial) for trial in plan.trials]

    def expire_if_due() -> None:
        nonlocal expired
        if clock.now_ns() >= started + config.limits.max_duration_ns:
            expired = True
            stop.set()

    async def deadline() -> None:
        nonlocal expired
        await clock.sleep_until(started + config.limits.max_duration_ns)
        expired = True
        stop.set()

    with StudyDirectory.create(
        output_dir, budget=config.limits.total_output_bytes
    ) as directory:
        directory.write("plan.json", plan_bytes(plan), limit=MAX_METADATA_BYTES)
        if engine_binding is not None:
            directory.write(
                "engine-binding.json",
                engine_binding_bytes(engine_binding),
                limit=MAX_ENGINE_BINDING_BYTES,
            )
        timer = asyncio.create_task(deadline())
        try:
            for index, trial in enumerate(plan.trials):
                expire_if_due()
                if stop.is_set():
                    status = "CANCELLED"
                    reason = "STUDY_DEADLINE" if expired else "CANCELLED"
                    break
                failure: FailureReason | None = None
                try:
                    result = await executor(trial, stop=stop)
                except asyncio.CancelledError:
                    stop.set()
                    failure = "STUDY_DEADLINE" if expired else "CANCELLED"
                except Exception:
                    failure = "CONTROLLER_OR_CLEANUP_FAILED"
                if failure is None:
                    try:
                        if engine_binding is None:
                            content = _trial_bytes(plan, trial, result)
                            load_study_trial_bytes(content, plan, trial)
                        else:
                            if not isinstance(result, SGLangTrialResult):
                                raise EvaluationError(
                                    "bound study requires typed results"
                                )
                            content = sglang_trial_bytes(
                                plan, trial, result, engine_binding
                            )
                            result = load_sglang_trial_bytes(
                                content, plan, trial, engine_binding
                            )
                    except Exception:
                        failure = "RESULT_VALIDATION_FAILED"
                if failure is None:
                    try:
                        directory.write(
                            trial_filename(index),
                            content,
                            limit=config.limits.per_trial_result_bytes,
                        )
                    except Exception:
                        failure = "OUTPUT_FAILED"
                if failure is not None:
                    expire_if_due()
                    entries[index] = TrialLedger(
                        trial_id=trial.trial_id,
                        state="ABORTED",
                        result_filename=None,
                        result_sha256=None,
                        result_status=None,
                        failure_reason=failure,
                        cleanup="UNCONFIRMED",
                    )
                    status = "CANCELLED" if stop.is_set() else "ABORTED"
                    reason = "STUDY_DEADLINE" if expired else failure
                    break
                entries[index] = TrialLedger(
                    trial_id=trial.trial_id,
                    state="RETURNED",
                    result_filename=trial_filename(index),
                    result_sha256=sha256_digest(content),
                    result_status=result.status,
                    failure_reason=None,
                    cleanup="CONFIRMED_BY_LOCAL_RESULT",
                )
                expire_if_due()
                if result.status != "COMPLETED" or stop.is_set():
                    status = (
                        "CANCELLED"
                        if result.status == "CANCELLED" or stop.is_set()
                        else "ABORTED"
                    )
                    reason = (
                        "STUDY_DEADLINE"
                        if expired
                        else (
                            "WARMUP_FAILED"
                            if result.status == "WARMUP_FAILED"
                            else "CANCELLED"
                        )
                    )
                    break
                if index + 1 < len(plan.trials) and trial.cooldown_ns:
                    await _cooldown(clock, clock.now_ns() + trial.cooldown_ns, stop)
        except asyncio.CancelledError:
            stop.set()
            status, reason = "CANCELLED", "CANCELLED"
        except Exception:
            status, reason = "ABORTED", "CONTROLLER_OR_CLEANUP_FAILED"
        finally:
            timer.cancel()
            with suppress(asyncio.CancelledError):
                await timer
        expire_if_due()
        if stop.is_set() and status == "COMPLETED":
            status = "CANCELLED"
            reason = "STUDY_DEADLINE" if expired else "CANCELLED"
        manifest = _manifest(
            plan,
            entries,
            status=status,
            reason=reason,
            elapsed_ns=max(0, clock.now_ns() - started),
        )
        validate_manifest(plan, manifest)
        content = _encoded(manifest)
        binding_size = (
            len(engine_binding_bytes(engine_binding)) if engine_binding else 0
        )
        if (
            len(content) + len(plan_bytes(plan)) + binding_size
            > PLAN_MANIFEST_RESERVE_BYTES
        ):
            raise EvaluationError("study manifest exceeds its metadata reserve")
        directory.write("manifest.json", content, limit=MAX_METADATA_BYTES)
        return manifest


def validate_manifest(plan: CompiledStudy, manifest: StudyManifest) -> None:
    if (
        manifest.plan_sha256 != sha256_digest(plan_bytes(plan))
        or manifest.config_sha256 != plan.config_sha256
        or len(manifest.trials) != len(plan.trials)
    ):
        raise EvaluationError("study manifest does not match the expected plan")
    ended = False
    for index, (trial, entry) in enumerate(
        zip(plan.trials, manifest.trials, strict=True)
    ):
        if entry.trial_id != trial.trial_id:
            raise EvaluationError("study trial ledger is unordered or mismatched")
        if entry.state == "RETURNED":
            if (
                ended
                or entry.result_filename != trial_filename(index)
                or entry.result_sha256 is None
                or entry.result_status is None
                or entry.failure_reason is not None
                or entry.cleanup != "CONFIRMED_BY_LOCAL_RESULT"
            ):
                raise EvaluationError("study returned-trial ledger is inconsistent")
            ended = entry.result_status != "COMPLETED"
        else:
            if any(
                value is not None
                for value in (
                    entry.result_filename,
                    entry.result_sha256,
                    entry.result_status,
                )
            ):
                raise EvaluationError("study missing-trial ledger fabricates a result")
            if entry.state == "ABORTED":
                if (
                    ended
                    or entry.failure_reason is None
                    or entry.cleanup != "UNCONFIRMED"
                ):
                    raise EvaluationError("study abort ledger is inconsistent")
            elif entry.failure_reason is not None or entry.cleanup != "NOT_STARTED":
                raise EvaluationError("study unrun ledger is inconsistent")
            ended = True
    complete = all(
        entry.state == "RETURNED" and entry.result_status == "COMPLETED"
        for entry in manifest.trials
    )
    if (
        manifest.status == "COMPLETED" and (not complete or manifest.reason is not None)
    ) or (manifest.status != "COMPLETED" and manifest.reason is None):
        raise EvaluationError("study completion claim contradicts the trial ledger")
    if manifest.status == "ABORTED" and manifest.reason in {
        "CANCELLED",
        "STUDY_DEADLINE",
    }:
        raise EvaluationError("study abort reason contradicts its status")
    if manifest.reason == "WARMUP_FAILED" and not any(
        entry.state == "RETURNED" and entry.result_status == "WARMUP_FAILED"
        for entry in manifest.trials
    ):
        raise EvaluationError("study warmup failure lacks a matching result")


def read_manifest(directory: StudyDirectory, plan: CompiledStudy) -> StudyManifest:
    if directory.read("plan.json", limit=MAX_METADATA_BYTES) != plan_bytes(plan):
        raise EvaluationError("stored study plan differs from the supplied config")
    content = directory.read("manifest.json", limit=MAX_METADATA_BYTES)
    try:
        validate_json_structure(content.decode("utf-8"))
        manifest = StudyManifest.model_validate_json(content)
        if _encoded(manifest) != content:
            raise ValueError
        validate_manifest(plan, manifest)
        return manifest
    except (ValueError, ValidationError):
        raise EvaluationError(
            "study manifest violates its canonical contract"
        ) from None


def report_study(
    config: StudyConfig,
    study_dir: Path,
    output_dir: Path,
    *,
    engine_binding: EvaluationEngineBinding | None = None,
) -> dict[str, object]:
    """Validate one expected artifact at a time; publish bounded offline reports."""
    from inferdrome.evaluation.study_report import (
        render_markdown,
        summarize_study,
        summarize_trial,
    )

    plan = compile_study(config)
    if engine_binding is not None:
        engine_binding = load_engine_binding_bytes(
            engine_binding_bytes(engine_binding), plan
        )
    if engine_binding is None:
        preflight_metadata(plan)
    else:
        preflight_metadata(plan, engine_binding)
    summaries: list[dict[str, object]] = []
    trial_elapsed_ns = 0
    with StudyDirectory.open(
        study_dir, budget=config.limits.total_output_bytes
    ) as source:
        try:
            stored_binding = source.read(
                "engine-binding.json", limit=MAX_ENGINE_BINDING_BYTES
            )
        except FileNotFoundError:
            stored_binding = None
        if engine_binding is None:
            if stored_binding is not None:
                raise EvaluationError(
                    "engine-bound study requires its explicit binding"
                )
        elif stored_binding != engine_binding_bytes(engine_binding):
            raise EvaluationError(
                "study engine binding differs from the supplied binding"
            )
        manifest = read_manifest(source, plan)
        for index, (trial, entry) in enumerate(
            zip(plan.trials, manifest.trials, strict=True)
        ):
            if entry.state != "RETURNED":
                continue
            content = source.read(
                trial_filename(index), limit=config.limits.per_trial_result_bytes
            )
            if sha256_digest(content) != entry.result_sha256:
                raise EvaluationError("study trial bytes differ from the ledger digest")
            if engine_binding is None:
                validated = load_study_trial_bytes(content, plan, trial)
            else:
                # Only used inside the bound report below; never exported as v1.
                validated = load_sglang_trial_bytes(
                    content, plan, trial, engine_binding
                )._statistical_input
            trial_elapsed_ns += validated.elapsed_ns
            summaries.append(summarize_trial(trial, validated))
    started_trials = sum(entry.state != "NOT_RUN" for entry in manifest.trials)
    required_cooldown_ns = max(0, started_trials - 1) * config.limits.cooldown_ns
    if trial_elapsed_ns + required_cooldown_ns > manifest.elapsed_ns:
        raise EvaluationError(
            "study elapsed time contradicts sequential trial durations"
        )
    report = summarize_study(plan, summaries, manifest.model_dump(mode="json"))
    report_limit = MAX_METADATA_BYTES
    if engine_binding is None:
        markdown_content = render_markdown(report).encode("utf-8")
    else:
        from inferdrome.evaluation.sglang_report import (
            MAX_SGLANG_REPORT_BYTES,
            bind_sglang_report,
            render_sglang_markdown,
        )

        report_limit = MAX_SGLANG_REPORT_BYTES
        report = bind_sglang_report(report, engine_binding, plan)
        markdown_content = render_sglang_markdown(report, plan, engine_binding).encode(
            "utf-8"
        )
    json_content = canonical_json_bytes(report) + b"\n"
    if max(len(json_content), len(markdown_content)) > report_limit:
        raise EvaluationError("study report exceeds its output bound")
    with StudyDirectory.create(output_dir, budget=2 * report_limit) as output:
        output.write("report.json", json_content, limit=report_limit)
        output.write("report.md", markdown_content, limit=report_limit)
    return report
