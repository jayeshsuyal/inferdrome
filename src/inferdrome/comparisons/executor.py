"""Fail-closed execution of one frozen controlled-comparison schedule."""

import errno
import fcntl
import hmac
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError

from inferdrome.bundle import (
    BundleAnalysis,
    recalculate_bundle,
    verify_bundle_matches_workspace,
)
from inferdrome.bundle.reader import BundleReader, strict_json_value
from inferdrome.comparisons.service import (
    VerifiedComparisonPlan,
    VerifiedComparisonResult,
    create_comparison_result,
    inspect_comparison_result_declaration,
    verify_comparison_plan,
    verify_comparison_result,
)
from inferdrome.domain.controlled_comparison import (
    ComparisonArm,
    ComparisonArmPlan,
    ControlledComparisonPlan,
)
from inferdrome.domain.evidence import ArtifactRole
from inferdrome.domain.execution import ExecutionRecord
from inferdrome.domain.experiment import ExperimentSpec
from inferdrome.domain.states import RunState
from inferdrome.errors import (
    CancellationRequested,
    ControlledComparisonExecutionError,
    InferdromeError,
    TrialSetError,
    WorkLimitError,
)
from inferdrome.execution.cancellation import CancellationToken
from inferdrome.execution.orchestrator import run_resolved_experiment
from inferdrome.gpu_proof import ManagedVllmConfig
from inferdrome.immutable import is_internal_staging_entry
from inferdrome.limits import WorkBudget, collect_bounded
from inferdrome.resolution import ResolutionResult, resolve_experiment
from inferdrome.trials import VerifiedTrialSet, create_trial_set, verify_trial_set
from inferdrome.workspace import RunWorkspace

_RESULT_ID = re.compile(r"^comparison-result-[0-9a-f]{32}$")
_MAX_RESULT_ENTRIES = 200
_LOCK_DIRECTORY = ".inferdrome-comparison-executor-locks"

PlannedRunState = Literal[
    "PENDING",
    "CREATED",
    "PREFLIGHT",
    "WARMUP",
    "MEASURING",
    "FINALIZING",
    "COMPLETE",
    "FAILED",
    "INTERRUPTED",
    "INVALID",
]
ExecutionProgressStatus = Literal[
    "NOT_STARTED",
    "PARTIAL",
    "BLOCKED",
    "EVIDENCE_COMPLETE",
]


@dataclass(frozen=True)
class ExecutedComparison:
    """Verified terminal output from one controlled-comparison execution."""

    plan: VerifiedComparisonPlan
    baseline_trial_set: VerifiedTrialSet
    candidate_trial_set: VerifiedTrialSet
    result: VerifiedComparisonResult
    executed_run_ids: tuple[str, ...]
    reused_run_ids: tuple[str, ...]


@dataclass(frozen=True)
class _CompletedRun:
    analysis: BundleAnalysis
    execution: ExecutionRecord


@dataclass(frozen=True)
class PlannedRunProgress:
    """Read-only state for one preallocated schedule slot."""

    sequence_index: int
    run_id: str
    state: PlannedRunState
    verified_bundle: bool


@dataclass(frozen=True)
class ComparisonExecutionProgress:
    """Derived local progress; it is not a portable evidence contract."""

    status: ExecutionProgressStatus
    completed_run_count: int
    planned_run_count: int
    next_sequence_index: int | None
    exact_schedule_prefix: bool
    slots: tuple[PlannedRunProgress, ...]


def _real_directory(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _path_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return False
        raise ControlledComparisonExecutionError(
            "comparison execution path could not be inspected"
        ) from None
    return True


def _ensure_real_root(path: Path, *, label: str) -> Path:
    selected = path.absolute()
    try:
        selected.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        raise ControlledComparisonExecutionError(
            f"{label} could not be created"
        ) from None
    if not _real_directory(selected):
        raise ControlledComparisonExecutionError(
            f"{label} must be a real directory"
        )
    return selected


@contextmanager
def _execution_lock(
    runs_root: Path,
    comparison_plan_id: str,
    comparison_plan_digest: str,
) -> Iterator[None]:
    root = _ensure_real_root(
        runs_root,
        label="runs root",
    )
    lock_root = root / _LOCK_DIRECTORY
    try:
        lock_root.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        raise ControlledComparisonExecutionError(
            "comparison execution lock root could not be created"
        ) from None
    if not _real_directory(lock_root):
        raise ControlledComparisonExecutionError(
            "comparison execution lock root must be a real directory"
        )

    digest_suffix = comparison_plan_digest.removeprefix("sha256:")
    lock_path = lock_root / f"{comparison_plan_id}-{digest_suffix}.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError:
        raise ControlledComparisonExecutionError(
            "comparison execution lock is unavailable or unsafe"
        ) from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ControlledComparisonExecutionError(
                "comparison execution lock must be one regular file"
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ControlledComparisonExecutionError(
                "another executor already holds this comparison plan"
            ) from None
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _write_new(path: Path, content: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o400)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short prepared-source write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepared_source(
    root: Path,
    name: str,
    resolution: ResolutionResult,
) -> Path:
    arm_root = root / name
    arm_root.mkdir(mode=0o700)
    source_path = arm_root / "experiment.yaml"
    try:
        workload_relative = resolution.workload_path.relative_to(
            resolution.source_path.parent
        )
    except ValueError:
        raise ControlledComparisonExecutionError(
            "comparison workload is outside its source directory"
        ) from None
    if (
        workload_relative.is_absolute()
        or not workload_relative.parts
        or any(part in {"", ".", ".."} for part in workload_relative.parts)
    ):
        raise ControlledComparisonExecutionError(
            "comparison workload path cannot be prepared safely"
        )
    workload_path = arm_root / workload_relative
    if workload_path == source_path:
        raise ControlledComparisonExecutionError(
            "comparison source and workload paths collide"
        )
    try:
        workload_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_new(source_path, resolution.source_bytes)
        _write_new(workload_path, resolution.workload_bytes)
    except OSError:
        raise ControlledComparisonExecutionError(
            "comparison inputs could not be prepared from verified bytes"
        ) from None
    return source_path


@contextmanager
def _prepared_sources(
    baseline: ResolutionResult,
    candidate: ResolutionResult,
) -> Iterator[dict[ComparisonArm, Path]]:
    with tempfile.TemporaryDirectory(prefix="inferdrome-comparison-") as temporary:
        root = Path(temporary)
        yield {
            ComparisonArm.BASELINE: _prepared_source(root, "baseline", baseline),
            ComparisonArm.CANDIDATE: _prepared_source(
                root,
                "candidate",
                candidate,
            ),
        }


def _arm_plan(
    descriptor: ControlledComparisonPlan,
    arm: ComparisonArm,
) -> ComparisonArmPlan:
    if arm is ComparisonArm.BASELINE:
        return descriptor.baseline_arm
    return descriptor.candidate_arm


def _validate_resolution(
    resolution: ResolutionResult,
    arm: ComparisonArmPlan,
) -> None:
    matches = (
        hmac.compare_digest(
            resolution.source_spec_digest,
            arm.source_spec_digest,
        )
        and hmac.compare_digest(
            resolution.execution_fingerprint,
            arm.expected_execution_fingerprint,
        )
        and resolution.resolved_spec == arm.resolved_experiment
    )
    if not matches:
        raise ControlledComparisonExecutionError(
            f"{arm.arm.value.lower()} source does not match the frozen plan"
        )


def _resolve_arm_source(
    source: Path,
    arm: ComparisonArmPlan,
    run_id: str,
) -> ResolutionResult:
    try:
        resolution = resolve_experiment(source, run_id=run_id, strict=True)
    except InferdromeError as error:
        raise ControlledComparisonExecutionError(
            f"{arm.arm.value.lower()} source could not be resolved"
        ) from error
    _validate_resolution(resolution, arm)
    return resolution


def _run_context(
    analysis: BundleAnalysis,
    *,
    work_budget: WorkBudget | None = None,
) -> tuple[ExperimentSpec, ExecutionRecord]:
    verification = analysis.verification
    reader = BundleReader(
        verification.bundle_path,
        require_immutable=True,
        work_budget=work_budget,
    )
    role_paths = {
        artifact.role: artifact.path for artifact in verification.descriptor.artifacts
    }
    spec_bytes = reader.read_bytes(role_paths[ArtifactRole.RESOLVED_SPEC])
    execution_bytes = reader.read_bytes(role_paths[ArtifactRole.EXECUTION])
    strict_json_value(spec_bytes, label="resolved experiment")
    strict_json_value(execution_bytes, label="execution record")
    try:
        spec = ExperimentSpec.model_validate_json(spec_bytes)
        execution = ExecutionRecord.model_validate_json(execution_bytes)
    except ValidationError:
        raise ControlledComparisonExecutionError(
            "planned run context failed contract validation"
        ) from None
    if execution.run_id != verification.run_id:
        raise ControlledComparisonExecutionError(
            "planned run execution identity disagrees"
        )
    reader.assert_unchanged()
    return spec, execution


def _verify_completed_run(
    run_path: Path,
    arm: ComparisonArmPlan,
    *,
    work_budget: WorkBudget | None = None,
) -> _CompletedRun:
    try:
        workspace = RunWorkspace.open(run_path, work_budget=work_budget)
        state = workspace.current_state(work_budget=work_budget)
    except WorkLimitError:
        raise
    except InferdromeError as error:
        raise ControlledComparisonExecutionError(
            "planned run workspace failed verification"
        ) from error
    if state.state is not RunState.COMPLETE:
        raise ControlledComparisonExecutionError(
            f"planned run {workspace.run_id} is {state.state.value}; "
            "v1 forbids retries and replacement runs"
        )
    try:
        analysis = recalculate_bundle(
            workspace.path / "bundle",
            work_budget=work_budget,
        )
        verify_bundle_matches_workspace(
            workspace,
            analysis,
            work_budget=work_budget,
        )
    except WorkLimitError:
        raise
    except InferdromeError as error:
        raise ControlledComparisonExecutionError(
            "completed planned run failed independent recalculation"
        ) from error
    verification = analysis.verification
    descriptor = verification.descriptor
    if (
        verification.run_id != workspace.run_id
        or not hmac.compare_digest(
            workspace.metadata.source_spec_digest,
            arm.source_spec_digest,
        )
        or not hmac.compare_digest(
            workspace.metadata.execution_fingerprint,
            arm.expected_execution_fingerprint,
        )
        or not hmac.compare_digest(
            descriptor.digests.source_spec_digest,
            arm.source_spec_digest,
        )
        or not hmac.compare_digest(
            descriptor.digests.execution_fingerprint,
            arm.expected_execution_fingerprint,
        )
    ):
        raise ControlledComparisonExecutionError(
            "completed planned run does not match its frozen arm"
        )
    spec, execution = _run_context(analysis, work_budget=work_budget)
    if spec != arm.resolved_experiment:
        raise ControlledComparisonExecutionError(
            "completed planned run resolved specification disagrees"
        )
    return _CompletedRun(analysis=analysis, execution=execution)


def _existing_completed_prefix(
    plan: VerifiedComparisonPlan,
    runs_root: Path,
) -> tuple[_CompletedRun, ...]:
    root = runs_root.absolute()
    if not root.exists():
        return ()
    if not _real_directory(root):
        raise ControlledComparisonExecutionError(
            "runs root must be a real directory"
        )

    completed: list[_CompletedRun] = []
    missing_seen = False
    previous_start = None
    for slot in plan.descriptor.ordered_schedule:
        run_path = root / slot.run_id
        if not _path_exists(run_path):
            missing_seen = True
            continue
        if missing_seen:
            raise ControlledComparisonExecutionError(
                "existing planned runs do not form the exact schedule prefix"
            )
        if not _real_directory(run_path):
            raise ControlledComparisonExecutionError(
                "planned run path must be a real directory"
            )
        verified = _verify_completed_run(
            run_path,
            _arm_plan(plan.descriptor, slot.arm),
        )
        started_at = verified.execution.started_at
        if plan.descriptor.created_at >= started_at:
            raise ControlledComparisonExecutionError(
                "completed planned run does not follow local plan creation"
            )
        if previous_start is not None and previous_start >= started_at:
            raise ControlledComparisonExecutionError(
                "completed planned runs do not follow the frozen schedule order"
            )
        previous_start = started_at
        completed.append(verified)
    return tuple(completed)


def inspect_comparison_execution(
    plan: VerifiedComparisonPlan,
    runs_root: Path,
    *,
    work_budget: WorkBudget | None = None,
) -> ComparisonExecutionProgress:
    """Derive bounded progress without trusting mutable executor state."""

    root = runs_root.absolute()
    slots: list[PlannedRunProgress] = []
    completed_contexts: dict[int, _CompletedRun] = {}
    if root.exists() and not _real_directory(root):
        raise ControlledComparisonExecutionError(
            "runs root must be a real directory"
        )

    for slot in plan.descriptor.ordered_schedule:
        if work_budget is not None:
            work_budget.reserve(units=1)
        run_path = root / slot.run_id
        if not _path_exists(run_path):
            slots.append(
                PlannedRunProgress(
                    sequence_index=slot.sequence_index,
                    run_id=slot.run_id,
                    state="PENDING",
                    verified_bundle=False,
                )
            )
            continue
        if not _real_directory(run_path):
            slots.append(
                PlannedRunProgress(
                    sequence_index=slot.sequence_index,
                    run_id=slot.run_id,
                    state="INVALID",
                    verified_bundle=False,
                )
            )
            continue
        try:
            workspace = RunWorkspace.open(run_path, work_budget=work_budget)
            state = workspace.current_state(work_budget=work_budget).state
        except WorkLimitError:
            raise
        except InferdromeError:
            slots.append(
                PlannedRunProgress(
                    sequence_index=slot.sequence_index,
                    run_id=slot.run_id,
                    state="INVALID",
                    verified_bundle=False,
                )
            )
            continue
        if state is not RunState.COMPLETE:
            slots.append(
                PlannedRunProgress(
                    sequence_index=slot.sequence_index,
                    run_id=slot.run_id,
                    state=cast(PlannedRunState, state.value),
                    verified_bundle=False,
                )
            )
            continue
        try:
            completed = _verify_completed_run(
                run_path,
                _arm_plan(plan.descriptor, slot.arm),
                work_budget=work_budget,
            )
            if work_budget is not None:
                work_budget.checkpoint()
        except WorkLimitError:
            raise
        except InferdromeError:
            slots.append(
                PlannedRunProgress(
                    sequence_index=slot.sequence_index,
                    run_id=slot.run_id,
                    state="INVALID",
                    verified_bundle=False,
                )
            )
            continue
        completed_contexts[slot.sequence_index] = completed
        slots.append(
            PlannedRunProgress(
                sequence_index=slot.sequence_index,
                run_id=slot.run_id,
                state="COMPLETE",
                verified_bundle=True,
            )
        )

    exact_prefix = True
    noncomplete_seen = False
    missing_seen = False
    previous_start = None
    for progress_slot in slots:
        if progress_slot.state == "PENDING":
            missing_seen = True
        elif missing_seen:
            exact_prefix = False
        if progress_slot.state != "COMPLETE":
            noncomplete_seen = True
            continue
        completed = completed_contexts[progress_slot.sequence_index]
        started_at = completed.execution.started_at
        if (
            noncomplete_seen
            or plan.descriptor.created_at >= started_at
            or (previous_start is not None and previous_start >= started_at)
        ):
            exact_prefix = False
        previous_start = started_at

    blocked_states = {"FAILED", "INTERRUPTED", "INVALID"}
    blocked = not exact_prefix or any(
        progress_slot.state in blocked_states for progress_slot in slots
    )
    completed_count = sum(
        progress_slot.state == "COMPLETE" for progress_slot in slots
    )
    status: ExecutionProgressStatus
    if blocked:
        status = "BLOCKED"
        next_sequence_index = None
    elif completed_count == len(slots):
        status = "EVIDENCE_COMPLETE"
        next_sequence_index = None
    elif all(progress_slot.state == "PENDING" for progress_slot in slots):
        status = "NOT_STARTED"
        next_sequence_index = 0
    else:
        status = "PARTIAL"
        next_sequence_index = next(
            progress_slot.sequence_index
            for progress_slot in slots
            if progress_slot.state != "COMPLETE"
        )
    return ComparisonExecutionProgress(
        status=status,
        completed_run_count=completed_count,
        planned_run_count=len(slots),
        next_sequence_index=next_sequence_index,
        exact_schedule_prefix=exact_prefix,
        slots=tuple(slots),
    )


def _validate_trial_set(
    verified: VerifiedTrialSet,
    arm: ComparisonArmPlan,
) -> None:
    descriptor = verified.descriptor
    run_ids = tuple(member.run_id for member in descriptor.members)
    if (
        descriptor.trial_set_id != arm.planned_trial_set_id
        or run_ids != arm.run_ids
        or not hmac.compare_digest(
            descriptor.execution_fingerprint,
            arm.expected_execution_fingerprint,
        )
        or any(
            not hmac.compare_digest(
                member.verification.descriptor.digests.source_spec_digest,
                arm.source_spec_digest,
            )
            for member in verified.members
        )
    ):
        raise ControlledComparisonExecutionError(
            f"existing {arm.arm.value.lower()} Trial Set disagrees with the plan"
        )


def _get_or_create_trial_set(
    *,
    plan: VerifiedComparisonPlan,
    arm: ComparisonArmPlan,
    runs_root: Path,
    trial_sets_root: Path,
) -> VerifiedTrialSet:
    path = trial_sets_root.absolute() / arm.planned_trial_set_id
    verified: VerifiedTrialSet
    if _path_exists(path):
        try:
            verified = verify_trial_set(path, runs_root=runs_root)
        except TrialSetError as error:
            raise ControlledComparisonExecutionError(
                f"existing {arm.arm.value.lower()} Trial Set failed verification"
            ) from error
    else:
        try:
            verified = create_trial_set(
                runs_root=runs_root,
                trial_sets_root=trial_sets_root,
                run_ids=arm.run_ids,
                title=f"{arm.arm.value.title()} arm",
                hypothesis=plan.descriptor.hypothesis,
                trial_set_id=arm.planned_trial_set_id,
            )
        except TrialSetError as error:
            if _path_exists(path):
                try:
                    verified = verify_trial_set(path, runs_root=runs_root)
                except TrialSetError:
                    raise ControlledComparisonExecutionError(
                        f"{arm.arm.value.lower()} Trial Set publication "
                        "failed closed"
                    ) from error
            else:
                raise ControlledComparisonExecutionError(
                    f"{arm.arm.value.lower()} Trial Set publication failed closed"
                ) from error
    _validate_trial_set(verified, arm)
    return verified


def _result_paths_for_plan(
    comparison_results_root: Path,
    comparison_plan_id: str,
) -> tuple[Path, ...]:
    root = _ensure_real_root(
        comparison_results_root,
        label="comparison-results root",
    )
    try:
        with os.scandir(root) as iterator:
            entries = sorted(
                (
                    entry
                    for entry in collect_bounded(
                        iterator,
                        limit=_MAX_RESULT_ENTRIES,
                        error=lambda: ControlledComparisonExecutionError(
                            "comparison-results root exceeds the executor entry limit"
                        ),
                    )
                    if not is_internal_staging_entry(entry.name)
                ),
                key=lambda item: item.name,
            )
    except OSError:
        raise ControlledComparisonExecutionError(
            "comparison-results root could not be scanned"
        ) from None
    matching: list[Path] = []
    for entry in entries:
        if not _RESULT_ID.fullmatch(entry.name):
            continue
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            continue
        path = Path(entry.path)
        try:
            declaration = inspect_comparison_result_declaration(path)
        except InferdromeError:
            continue
        if declaration.descriptor.comparison_plan_id == comparison_plan_id:
            matching.append(path)
    return tuple(matching)


def _validate_existing_result(
    verified: VerifiedComparisonResult,
    *,
    plan: VerifiedComparisonPlan,
    baseline: VerifiedTrialSet,
    candidate: VerifiedTrialSet,
) -> None:
    descriptor = verified.descriptor
    if (
        descriptor.comparison_plan_id != plan.descriptor.comparison_plan_id
        or not hmac.compare_digest(
            descriptor.comparison_plan_digest,
            plan.comparison_plan_digest,
        )
        or descriptor.baseline_trial_set.trial_set_id
        != baseline.descriptor.trial_set_id
        or not hmac.compare_digest(
            descriptor.baseline_trial_set.trial_set_digest,
            baseline.trial_set_digest,
        )
        or descriptor.candidate_trial_set.trial_set_id
        != candidate.descriptor.trial_set_id
        or not hmac.compare_digest(
            descriptor.candidate_trial_set.trial_set_digest,
            candidate.trial_set_digest,
        )
    ):
        raise ControlledComparisonExecutionError(
            "existing comparison result disagrees with finalized evidence"
        )


def _get_or_create_result(
    *,
    plan: VerifiedComparisonPlan,
    baseline: VerifiedTrialSet,
    candidate: VerifiedTrialSet,
    runs_root: Path,
    trial_sets_root: Path,
    comparison_results_root: Path,
) -> VerifiedComparisonResult:
    matching = _result_paths_for_plan(
        comparison_results_root,
        plan.descriptor.comparison_plan_id,
    )
    if len(matching) > 1:
        raise ControlledComparisonExecutionError(
            "multiple comparison results already reference this plan"
        )
    if matching:
        try:
            verified = verify_comparison_result(
                matching[0],
                runs_root=runs_root,
                trial_sets_root=trial_sets_root,
                comparison_plans_root=plan.path.parent,
            )
        except InferdromeError as error:
            raise ControlledComparisonExecutionError(
                "existing comparison result failed verification"
            ) from error
    else:
        suffix = plan.descriptor.comparison_plan_id.removeprefix(
            "comparison-plan-"
        )
        try:
            verified = create_comparison_result(
                runs_root=runs_root,
                trial_sets_root=trial_sets_root,
                comparison_plans_root=plan.path.parent,
                comparison_results_root=comparison_results_root,
                comparison_plan_id=plan.descriptor.comparison_plan_id,
                expected_comparison_plan_digest=plan.comparison_plan_digest,
                baseline_trial_set_id=baseline.descriptor.trial_set_id,
                expected_baseline_trial_set_digest=baseline.trial_set_digest,
                candidate_trial_set_id=candidate.descriptor.trial_set_id,
                expected_candidate_trial_set_digest=candidate.trial_set_digest,
                comparison_result_id=f"comparison-result-{suffix}",
            )
        except InferdromeError as error:
            raise ControlledComparisonExecutionError(
                "comparison result publication failed closed"
            ) from error
    _validate_existing_result(
        verified,
        plan=plan,
        baseline=baseline,
        candidate=candidate,
    )
    return verified


def execute_comparison_plan(
    comparison_plan: Path,
    *,
    expected_comparison_plan_digest: str,
    baseline_source: Path,
    candidate_source: Path,
    runs_root: Path,
    trial_sets_root: Path,
    comparison_results_root: Path,
    tokenizer_path: Path | None = None,
    managed_vllm: ManagedVllmConfig | None = None,
    cancellation: CancellationToken | None = None,
) -> ExecutedComparison:
    """Execute, group, evaluate, and reverify one immutable plan."""

    selected_cancellation = cancellation or CancellationToken()
    selected_cancellation.raise_if_requested()
    plan = verify_comparison_plan(
        comparison_plan,
        expected_comparison_plan_digest=expected_comparison_plan_digest,
    )
    descriptor = plan.descriptor

    with _execution_lock(
        runs_root,
        descriptor.comparison_plan_id,
        expected_comparison_plan_digest,
    ):
        plan = verify_comparison_plan(
            plan.path,
            expected_comparison_plan_digest=expected_comparison_plan_digest,
        )
        baseline_initial = _resolve_arm_source(
            baseline_source,
            descriptor.baseline_arm,
            descriptor.baseline_arm.run_ids[0],
        )
        candidate_initial = _resolve_arm_source(
            candidate_source,
            descriptor.candidate_arm,
            descriptor.candidate_arm.run_ids[0],
        )

        with _prepared_sources(baseline_initial, candidate_initial) as sources:
            _resolve_arm_source(
                sources[ComparisonArm.BASELINE],
                descriptor.baseline_arm,
                descriptor.baseline_arm.run_ids[0],
            )
            _resolve_arm_source(
                sources[ComparisonArm.CANDIDATE],
                descriptor.candidate_arm,
                descriptor.candidate_arm.run_ids[0],
            )
            completed = list(_existing_completed_prefix(plan, runs_root))
            reused_run_ids = tuple(
                slot.run_id
                for slot in descriptor.ordered_schedule[: len(completed)]
            )
            executed_run_ids: list[str] = []
            previous_start = (
                completed[-1].execution.started_at if completed else None
            )

            for slot in descriptor.ordered_schedule[len(completed) :]:
                selected_cancellation.raise_if_requested()
                verify_comparison_plan(
                    plan.path,
                    expected_comparison_plan_digest=(
                        expected_comparison_plan_digest
                    ),
                )
                arm = _arm_plan(descriptor, slot.arm)
                resolution = _resolve_arm_source(
                    sources[slot.arm],
                    arm,
                    slot.run_id,
                )
                try:
                    run_resolved_experiment(
                        resolution,
                        runs_root=runs_root,
                        tokenizer_path=tokenizer_path,
                        managed_vllm=managed_vllm,
                        cancellation=selected_cancellation,
                    )
                except (CancellationRequested, KeyboardInterrupt):
                    raise
                except Exception as error:
                    raise ControlledComparisonExecutionError(
                        f"schedule slot {slot.sequence_index + 1} failed closed; "
                        "v1 will not retry or replace its run ID"
                    ) from error

                verified_run = _verify_completed_run(
                    runs_root.absolute() / slot.run_id,
                    arm,
                )
                started_at = verified_run.execution.started_at
                if descriptor.created_at >= started_at:
                    raise ControlledComparisonExecutionError(
                        "executed run does not follow local plan creation"
                    )
                if previous_start is not None and previous_start >= started_at:
                    raise ControlledComparisonExecutionError(
                        "executed runs do not follow the frozen schedule order"
                    )
                previous_start = started_at
                completed.append(verified_run)
                executed_run_ids.append(slot.run_id)

        if len(completed) != len(descriptor.ordered_schedule):
            raise ControlledComparisonExecutionError(
                "comparison execution ended without every planned run"
            )
        selected_cancellation.raise_if_requested()
        plan = verify_comparison_plan(
            plan.path,
            expected_comparison_plan_digest=expected_comparison_plan_digest,
        )
        selected_cancellation.raise_if_requested()
        baseline = _get_or_create_trial_set(
            plan=plan,
            arm=descriptor.baseline_arm,
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
        )
        selected_cancellation.raise_if_requested()
        candidate = _get_or_create_trial_set(
            plan=plan,
            arm=descriptor.candidate_arm,
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
        )
        selected_cancellation.raise_if_requested()
        result = _get_or_create_result(
            plan=plan,
            baseline=baseline,
            candidate=candidate,
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            comparison_results_root=comparison_results_root,
        )
        selected_cancellation.raise_if_requested()
        result = verify_comparison_result(
            result.path,
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            comparison_plans_root=plan.path.parent,
            expected_comparison_result_digest=result.comparison_result_digest,
        )
        return ExecutedComparison(
            plan=plan,
            baseline_trial_set=baseline,
            candidate_trial_set=candidate,
            result=result,
            executed_run_ids=tuple(executed_run_ids),
            reused_run_ids=reused_run_ids,
        )
