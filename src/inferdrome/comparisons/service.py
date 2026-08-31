"""Fail-closed publication and evaluation of controlled comparisons."""

import errno
import hmac
import os
import secrets
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import TypeAdapter, ValidationError

from inferdrome.bundle import BundleAnalysis
from inferdrome.bundle.reader import BundleReader, strict_json_value
from inferdrome.domain.controlled_comparison import (
    ComparabilityStatus,
    ComparisonArm,
    ComparisonArmPlan,
    ComparisonControlCheck,
    ComparisonScheduleSlot,
    ControlCheckId,
    ControlledComparisonPlan,
    ControlledComparisonResult,
    ControlledOutcomeEstimate,
    ControlledRunValue,
    IndependentVariable,
    OutcomeSelector,
    PairedRunDifference,
    TrialSetReference,
    comparison_schedule_arm_order,
)
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes
from inferdrome.domain.environment import EnvironmentManifest
from inferdrome.domain.evidence import ArtifactRole
from inferdrome.domain.execution import ExecutionRecord
from inferdrome.domain.experiment import ExperimentSpec
from inferdrome.domain.ids import (
    ComparisonPlanId,
    ComparisonResultId,
    RunId,
    ScheduleSeed,
    TrialSetId,
    new_comparison_plan_id,
    new_comparison_result_id,
    new_run_id,
    new_trial_set_id,
)
from inferdrome.domain.metrics import Measurement, frozen_metric_definitions_v1
from inferdrome.domain.states import EnvironmentCompleteness
from inferdrome.errors import (
    ControlledComparisonError,
    VerificationError,
    WorkLimitError,
)
from inferdrome.immutable import publish_immutable_directory
from inferdrome.limits import WorkBudget, collect_bounded
from inferdrome.resolution.canonicalization import execution_fingerprint_projection
from inferdrome.trials import VerifiedTrialSet, verify_trial_set

_PLAN_FILENAME = "comparison-plan.json"
_RESULT_FILENAME = "comparison-result.json"
_MAX_DESCRIPTOR_BYTES = 524_288
_ROUNDING_QUANTUM = Decimal("0.000001")
_STATISTICS_PRECISION = 80


@dataclass(frozen=True)
class VerifiedComparisonPlan:
    path: Path
    descriptor: ControlledComparisonPlan
    comparison_plan_digest: str


@dataclass(frozen=True)
class VerifiedComparisonResult:
    path: Path
    descriptor: ControlledComparisonResult
    comparison_result_digest: str
    plan: VerifiedComparisonPlan
    baseline: VerifiedTrialSet
    candidate: VerifiedTrialSet


@dataclass(frozen=True)
class ComparisonResultDeclaration:
    """Structurally valid bytes only; this is not a verified comparison claim."""

    path: Path
    descriptor: ControlledComparisonResult
    comparison_result_digest: str


@dataclass(frozen=True)
class _RunContext:
    analysis: BundleAnalysis
    spec: ExperimentSpec
    environment: EnvironmentManifest
    execution: ExecutionRecord

    @property
    def run_id(self) -> str:
        return self.analysis.verification.run_id


def _canonical_model_bytes(
    model: ControlledComparisonPlan | ControlledComparisonResult,
) -> bytes:
    return canonical_json_bytes(
        model.model_dump(mode="json", by_alias=True, exclude_none=False)
    )


def _real_directory(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _node_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _assert_single_descriptor_entry(
    directory_descriptor: int,
    *,
    filename: str,
    label: str,
) -> None:
    try:
        with os.scandir(directory_descriptor) as iterator:
            entries = collect_bounded(
                iterator,
                limit=1,
                error=lambda: ControlledComparisonError(
                    f"{label} directory contains undeclared entries"
                ),
            )
    except OSError:
        raise ControlledComparisonError(
            f"{label} directory cannot be inspected safely"
        ) from None
    if len(entries) != 1 or entries[0].name != filename:
        raise ControlledComparisonError(
            f"{label} directory contains undeclared entries"
        )


def _publish_descriptor(
    *,
    root: Path,
    artifact_id: str,
    filename: str,
    content: bytes,
    root_label: str,
    artifact_label: str,
) -> Path:
    selected_root = root.absolute()
    try:
        selected_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        raise ControlledComparisonError(f"{root_label} could not be created") from None
    if not _real_directory(selected_root):
        raise ControlledComparisonError(f"{root_label} must be a real directory")
    try:
        destination = publish_immutable_directory(
            root=selected_root,
            artifact_id=artifact_id,
            filename=filename,
            content=content,
        )
    except FileExistsError:
        raise ControlledComparisonError(
            f"{artifact_label} ID is already reserved"
        ) from None
    except (OSError, ValueError) as error:
        raise ControlledComparisonError(
            f"{artifact_label} publication failed closed"
        ) from error
    return destination


def _load_descriptor(
    path: Path,
    *,
    filename: str,
    model: type[ControlledComparisonPlan] | type[ControlledComparisonResult],
    id_attribute: str,
    label: str,
    work_budget: WorkBudget | None = None,
) -> tuple[ControlledComparisonPlan | ControlledComparisonResult, bytes]:
    selected_path = path.absolute()
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_descriptor = os.open(selected_path, directory_flags)
    except OSError:
        raise ControlledComparisonError(
            f"{label} path must be a real directory"
        ) from None
    try:
        initial_directory = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(initial_directory.st_mode)
            or stat.S_IMODE(initial_directory.st_mode) & 0o222
        ):
            raise ControlledComparisonError(
                f"{label} directory must be a read-only real directory"
            )
        _assert_single_descriptor_entry(
            directory_descriptor,
            filename=filename,
            label=label,
        )

        file_flags = (
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            file_descriptor = os.open(
                filename,
                file_flags,
                dir_fd=directory_descriptor,
            )
        except OSError:
            raise ControlledComparisonError(
                f"{label} descriptor is unavailable or unsafe"
            ) from None
        try:
            initial_file = os.fstat(file_descriptor)
            if (
                not stat.S_ISREG(initial_file.st_mode)
                or initial_file.st_nlink != 1
                or stat.S_IMODE(initial_file.st_mode) & 0o222
                or initial_file.st_size > _MAX_DESCRIPTOR_BYTES
            ):
                raise ControlledComparisonError(
                    f"{label} descriptor is not one bounded read-only file"
                )
            if work_budget is not None:
                work_budget.reserve(bytes_=initial_file.st_size)
            content = bytearray()
            remaining = initial_file.st_size
            while remaining:
                chunk = os.read(file_descriptor, min(65_536, remaining))
                if not chunk:
                    raise ControlledComparisonError(f"{label} descriptor was truncated")
                content.extend(chunk)
                remaining -= len(chunk)
            if os.read(file_descriptor, 1):
                raise ControlledComparisonError(f"{label} descriptor grew")
            final_file = os.fstat(file_descriptor)
            path_file = os.stat(
                filename,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if _node_identity(initial_file) != _node_identity(
                final_file
            ) or _node_identity(initial_file) != _node_identity(path_file):
                raise ControlledComparisonError(
                    f"{label} descriptor changed during its read"
                )
        finally:
            os.close(file_descriptor)
        _assert_single_descriptor_entry(
            directory_descriptor,
            filename=filename,
            label=label,
        )
        if _node_identity(initial_directory) != _node_identity(
            os.fstat(directory_descriptor)
        ):
            raise ControlledComparisonError(
                f"{label} directory changed during its read"
            )
    except OSError:
        raise ControlledComparisonError(
            f"{label} descriptor changed during its read"
        ) from None
    finally:
        os.close(directory_descriptor)
    raw_content = bytes(content)
    try:
        strict_json_value(raw_content, label=f"{label} descriptor")
        descriptor = model.model_validate_json(raw_content)
    except (RecursionError, ValueError, ValidationError, VerificationError):
        raise ControlledComparisonError(
            f"{label} descriptor failed contract validation"
        ) from None
    if selected_path.name != getattr(descriptor, id_attribute):
        raise ControlledComparisonError(f"{label} directory and identifier disagree")
    if raw_content != _canonical_model_bytes(descriptor):
        raise ControlledComparisonError(f"{label} descriptor is not canonical JSON")
    return descriptor, raw_content


def _validate_id(value: str, kind: type[Any], *, label: str) -> str:
    try:
        return cast(str, TypeAdapter(kind).validate_python(value, strict=True))
    except ValidationError:
        raise ControlledComparisonError(f"{label} is invalid") from None


def _artifact_path(root: Path, artifact_id: str, *, root_label: str) -> Path:
    selected_root = root.absolute()
    if not _real_directory(selected_root):
        raise ControlledComparisonError(f"{root_label} must be a real directory")
    selected = selected_root / artifact_id
    if not _real_directory(selected):
        raise ControlledComparisonError(
            f"{root_label} artifact is unavailable or unsafe"
        )
    return selected


def _ensure_planned_runs_are_unreserved(
    runs_root: Path, run_ids: Sequence[str]
) -> None:
    root = runs_root.absolute()
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        raise ControlledComparisonError("runs root could not be created") from None
    if not _real_directory(root):
        raise ControlledComparisonError("runs root must be a real directory")
    for run_id in run_ids:
        try:
            os.lstat(root / run_id)
        except OSError as error:
            if error.errno == errno.ENOENT:
                continue
            raise ControlledComparisonError(
                "planned run reservation could not be inspected"
            ) from None
        raise ControlledComparisonError(
            "comparison plans may only name currently unreserved run IDs"
        )


def create_comparison_plan(
    *,
    runs_root: Path,
    comparison_plans_root: Path,
    experiment_id: str,
    title: str,
    hypothesis: str,
    planned_repetitions_per_arm: int,
    independent_variable: IndependentVariable,
    primary_outcome: OutcomeSelector,
    baseline_resolved_experiment: ExperimentSpec,
    baseline_source_spec_digest: str,
    baseline_execution_fingerprint: str,
    candidate_resolved_experiment: ExperimentSpec,
    candidate_source_spec_digest: str,
    candidate_execution_fingerprint: str,
    comparison_plan_id: str | None = None,
    baseline_run_ids: Sequence[str] | None = None,
    candidate_run_ids: Sequence[str] | None = None,
    baseline_trial_set_id: str | None = None,
    candidate_trial_set_id: str | None = None,
    ordered_schedule: Sequence[ComparisonScheduleSlot] | None = None,
    schedule_seed: str | None = None,
    created_at: datetime | None = None,
) -> VerifiedComparisonPlan:
    """Publish a complete design before any named run workspace exists."""

    if isinstance(planned_repetitions_per_arm, bool) or not (
        2 <= planned_repetitions_per_arm <= 100
    ):
        raise ControlledComparisonError(
            "comparison repetitions per arm must be between 2 and 100"
        )
    if baseline_run_ids is None:
        selected_baseline = tuple(
            new_run_id() for _ in range(planned_repetitions_per_arm)
        )
    else:
        selected_baseline = tuple(
            _validate_id(run_id, RunId, label="baseline run ID")
            for run_id in baseline_run_ids
        )
    if candidate_run_ids is None:
        selected_candidate = tuple(
            new_run_id() for _ in range(planned_repetitions_per_arm)
        )
    else:
        selected_candidate = tuple(
            _validate_id(run_id, RunId, label="candidate run ID")
            for run_id in candidate_run_ids
        )

    selected_seed = _validate_id(
        schedule_seed or secrets.token_hex(32),
        ScheduleSeed,
        label="comparison schedule seed",
    )
    if ordered_schedule is None:
        slots: list[ComparisonScheduleSlot] = []
        for block_index in range(planned_repetitions_per_arm):
            for within_block_position, arm in enumerate(
                comparison_schedule_arm_order(selected_seed, block_index)
            ):
                run_id = (
                    selected_baseline[block_index]
                    if arm is ComparisonArm.BASELINE
                    else selected_candidate[block_index]
                )
                slots.append(
                    ComparisonScheduleSlot(
                        sequence_index=block_index * 2 + within_block_position,
                        block_index=block_index,
                        within_block_position=cast(
                            Literal[0, 1], within_block_position
                        ),
                        arm=arm,
                        repetition_index=block_index,
                        run_id=run_id,
                    )
                )
        schedule = tuple(slots)
    else:
        schedule = tuple(ordered_schedule)

    all_run_ids = (*selected_baseline, *selected_candidate)
    _ensure_planned_runs_are_unreserved(runs_root, all_run_ids)
    selected_baseline_trial_set_id = _validate_id(
        baseline_trial_set_id or new_trial_set_id(),
        TrialSetId,
        label="baseline Trial Set ID",
    )
    selected_candidate_trial_set_id = _validate_id(
        candidate_trial_set_id or new_trial_set_id(),
        TrialSetId,
        label="candidate Trial Set ID",
    )
    metric_definitions = frozen_metric_definitions_v1()
    metric_definitions_digest = digest_bytes(
        DigestDomain.METRIC_DEFINITIONS,
        canonical_json_bytes(
            metric_definitions.model_dump(
                mode="json", by_alias=True, exclude_none=False
            )
        ),
    )
    try:
        descriptor = ControlledComparisonPlan(
            schema_version="inferdrome.controlled-comparison-plan.v1",
            comparison_plan_id=(comparison_plan_id or new_comparison_plan_id()),
            experiment_id=experiment_id,
            title=title,
            hypothesis=hypothesis,
            created_at=created_at or datetime.now(UTC),
            design_status="PREDECLARED",
            arm_membership_policy="exact_ordered_run_ids_v1",
            schedule_policy="predeclared_permuted_pairs_v1",
            schedule_seed=selected_seed,
            statistical_unit="run",
            request_population_policy="separate_per_run_v1",
            weighting="equal_per_run",
            planned_repetitions_per_arm=planned_repetitions_per_arm,
            independent_variable=independent_variable,
            baseline_arm=ComparisonArmPlan(
                arm=ComparisonArm.BASELINE,
                planned_trial_set_id=selected_baseline_trial_set_id,
                source_spec_digest=baseline_source_spec_digest,
                expected_execution_fingerprint=baseline_execution_fingerprint,
                resolved_experiment=baseline_resolved_experiment,
                run_ids=selected_baseline,
            ),
            candidate_arm=ComparisonArmPlan(
                arm=ComparisonArm.CANDIDATE,
                planned_trial_set_id=selected_candidate_trial_set_id,
                source_spec_digest=candidate_source_spec_digest,
                expected_execution_fingerprint=candidate_execution_fingerprint,
                resolved_experiment=candidate_resolved_experiment,
                run_ids=selected_candidate,
            ),
            ordered_schedule=schedule,
            primary_outcome=primary_outcome,
            metric_definitions_digest=metric_definitions_digest,
            reducer_version=(baseline_resolved_experiment.measurement.reducer_version),
            estimator="paired_run_mean_difference_v1",
            contrast_direction="candidate_minus_baseline",
            uncertainty_method="none_v1",
            missing_data_policy="incomparable_if_any_outcome_missing_v1",
            exclusion_policy="no_post_assignment_exclusions_v1",
            environment_policy="complete_and_equal_observed_environment_v1",
            environment_control_scope="OBSERVED_V1_ALLOWLIST_ONLY",
            predeclaration_anchor="operator_retained_plan_digest_required_v1",
            predeclaration_assurance="OPERATOR_ATTESTED",
        )
    except ValidationError:
        raise ControlledComparisonError(
            "comparison-plan metadata failed contract validation"
        ) from None
    destination = _publish_descriptor(
        root=comparison_plans_root,
        artifact_id=descriptor.comparison_plan_id,
        filename=_PLAN_FILENAME,
        content=_canonical_model_bytes(descriptor),
        root_label="comparison-plans root",
        artifact_label="comparison plan",
    )
    return verify_comparison_plan(destination)


def verify_comparison_plan(
    path: Path,
    *,
    expected_comparison_plan_digest: str | None = None,
    work_budget: WorkBudget | None = None,
) -> VerifiedComparisonPlan:
    """Verify the immutable plan bytes and optional externally retained digest."""

    loaded, initial_bytes = _load_descriptor(
        path,
        filename=_PLAN_FILENAME,
        model=ControlledComparisonPlan,
        id_attribute="comparison_plan_id",
        label="comparison plan",
        work_budget=work_budget,
    )
    if not isinstance(loaded, ControlledComparisonPlan):
        raise AssertionError("comparison-plan loader returned the wrong model")
    digest = digest_bytes(DigestDomain.COMPARISON_PLAN, initial_bytes)
    if expected_comparison_plan_digest is not None and not hmac.compare_digest(
        digest,
        expected_comparison_plan_digest,
    ):
        raise ControlledComparisonError(
            "comparison-plan digest does not match the expected value"
        )
    final, final_bytes = _load_descriptor(
        path,
        filename=_PLAN_FILENAME,
        model=ControlledComparisonPlan,
        id_attribute="comparison_plan_id",
        label="comparison plan",
        work_budget=work_budget,
    )
    if final != loaded or final_bytes != initial_bytes:
        raise ControlledComparisonError(
            "comparison-plan descriptor changed during verification"
        )
    return VerifiedComparisonPlan(
        path=path.absolute(),
        descriptor=loaded,
        comparison_plan_digest=digest,
    )


def _run_context(
    analysis: BundleAnalysis,
    *,
    work_budget: WorkBudget | None = None,
) -> _RunContext:
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
    environment_bytes = reader.read_bytes(role_paths[ArtifactRole.ENVIRONMENT])
    execution_bytes = reader.read_bytes(role_paths[ArtifactRole.EXECUTION])
    strict_json_value(spec_bytes, label="resolved experiment")
    strict_json_value(environment_bytes, label="environment manifest")
    strict_json_value(execution_bytes, label="execution record")
    try:
        spec = ExperimentSpec.model_validate_json(spec_bytes)
        environment = EnvironmentManifest.model_validate_json(environment_bytes)
        execution = ExecutionRecord.model_validate_json(execution_bytes)
    except ValidationError:
        raise ControlledComparisonError(
            "comparison member context failed contract validation"
        ) from None
    if spec.experiment.id != verification.descriptor.experiment_id:
        raise ControlledComparisonError(
            "comparison member experiment identity disagrees"
        )
    if (
        environment.run_id != verification.run_id
        or execution.run_id != verification.run_id
    ):
        raise ControlledComparisonError("comparison member run identity disagrees")
    reader.assert_unchanged()
    return _RunContext(
        analysis=analysis,
        spec=spec,
        environment=environment,
        execution=execution,
    )


def _flatten(value: Any, *, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        flattened: dict[str, Any] = {}
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten(nested, prefix=path))
        return flattened
    return {prefix: value}


def _projection(context: _RunContext) -> dict[str, Any]:
    return _flatten(execution_fingerprint_projection(context.spec))


def _environment_signature(context: _RunContext) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            field.name.value,
            field.value,
            field.provenance.value,
            field.evidence_path,
        )
        for field in context.environment.fields
    )


def _measurement_map(context: _RunContext) -> dict[tuple[str, str], Measurement]:
    return {
        (measurement.metric.value, measurement.aggregation.value): measurement
        for measurement in context.analysis.reduction.measurements.measurements
    }


def _decimal_text(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = _STATISTICS_PRECISION
        context.rounding = ROUND_HALF_EVEN
        rounded = value.quantize(_ROUNDING_QUANTUM)
        normalized = rounded.normalize()
    if rounded == 0:
        return "0"
    return format(normalized, "f")


def _control_check(
    check: ControlCheckId,
    satisfied: bool,
) -> ComparisonControlCheck:
    return ComparisonControlCheck(
        check=check,
        status="SATISFIED" if satisfied else "UNSATISFIED",
    )


def _build_outcomes(
    plan: ControlledComparisonPlan,
    baseline_contexts: tuple[_RunContext, ...],
    candidate_contexts: tuple[_RunContext, ...],
) -> tuple[bool, tuple[ControlledOutcomeEstimate, ...]]:
    all_contexts = (*baseline_contexts, *candidate_contexts)
    baseline_descriptor = baseline_contexts[0].analysis.verification.descriptor
    candidate_descriptor = candidate_contexts[0].analysis.verification.descriptor
    baseline_reducer = baseline_contexts[
        0
    ].analysis.reduction.measurements.reducer_version
    candidate_reducer = candidate_contexts[
        0
    ].analysis.reduction.measurements.reducer_version
    if (
        baseline_descriptor.digests.metric_definitions_digest
        != candidate_descriptor.digests.metric_definitions_digest
        or baseline_reducer != candidate_reducer
    ):
        return False, ()

    maps = tuple(_measurement_map(context) for context in all_contexts)
    outcomes: list[ControlledOutcomeEstimate] = []
    for selector in (plan.primary_outcome,):
        key = (selector.metric.value, selector.aggregation.value)
        selected = tuple(measurements.get(key) for measurements in maps)
        if any(measurement is None for measurement in selected):
            return False, ()
        measurements = tuple(
            measurement for measurement in selected if measurement is not None
        )
        units = {measurement.unit for measurement in measurements}
        if len(units) != 1:
            return False, ()

        split = len(baseline_contexts)
        baseline_measurements = measurements[:split]
        candidate_measurements = measurements[split:]
        baseline_decimals = tuple(
            Decimal(str(measurement.value)) for measurement in baseline_measurements
        )
        candidate_decimals = tuple(
            Decimal(str(measurement.value)) for measurement in candidate_measurements
        )
        with localcontext() as context:
            context.prec = _STATISTICS_PRECISION
            context.rounding = ROUND_HALF_EVEN
            baseline_mean = sum(baseline_decimals, Decimal(0)) / len(baseline_decimals)
            candidate_mean = sum(candidate_decimals, Decimal(0)) / len(
                candidate_decimals
            )
            estimate = candidate_mean - baseline_mean
        if any(
            measurement.definition_id is not selector.definition_id
            or measurement.unit is not selector.unit
            or measurement.population is not selector.population
            or measurement.quantile_method is not selector.quantile_method
            or measurement.rounding_policy is not selector.rounding_policy
            for measurement in measurements
        ):
            return False, ()
        outcomes.append(
            ControlledOutcomeEstimate(
                selector=selector,
                unit=measurements[0].unit,
                baseline_values=tuple(
                    ControlledRunValue(
                        repetition_index=index,
                        run_id=context.run_id,
                        value=str(measurement.value),
                        sample_count=measurement.sample_count,
                    )
                    for index, (context, measurement) in enumerate(
                        zip(
                            baseline_contexts,
                            baseline_measurements,
                            strict=True,
                        )
                    )
                ),
                candidate_values=tuple(
                    ControlledRunValue(
                        repetition_index=index,
                        run_id=context.run_id,
                        value=str(measurement.value),
                        sample_count=measurement.sample_count,
                    )
                    for index, (context, measurement) in enumerate(
                        zip(
                            candidate_contexts,
                            candidate_measurements,
                            strict=True,
                        )
                    )
                ),
                paired_differences=tuple(
                    PairedRunDifference(
                        block_index=index,
                        baseline_run_id=baseline_context.run_id,
                        candidate_run_id=candidate_context.run_id,
                        candidate_minus_baseline=_decimal_text(
                            candidate_value - baseline_value
                        ),
                    )
                    for index, (
                        baseline_context,
                        candidate_context,
                        baseline_value,
                        candidate_value,
                    ) in enumerate(
                        zip(
                            baseline_contexts,
                            candidate_contexts,
                            baseline_decimals,
                            candidate_decimals,
                            strict=True,
                        )
                    )
                ),
                baseline_mean=_decimal_text(baseline_mean),
                candidate_mean=_decimal_text(candidate_mean),
                estimate=_decimal_text(estimate),
            )
        )
    return True, tuple(outcomes)


def _evaluate_result(
    *,
    plan: VerifiedComparisonPlan,
    baseline: VerifiedTrialSet,
    candidate: VerifiedTrialSet,
    comparison_result_id: str,
    created_at: datetime,
    work_budget: WorkBudget | None = None,
) -> ControlledComparisonResult:
    descriptor = plan.descriptor
    baseline_contexts = tuple(
        _run_context(member, work_budget=work_budget) for member in baseline.members
    )
    candidate_contexts = tuple(
        _run_context(member, work_budget=work_budget) for member in candidate.members
    )
    all_contexts = (*baseline_contexts, *candidate_contexts)

    plan_precedes = all(
        descriptor.created_at < context.execution.started_at for context in all_contexts
    )
    plan_check = _control_check(
        ControlCheckId.LOCAL_PLAN_ORDER,
        plan_precedes,
    )

    baseline_ids = tuple(member.run_id for member in baseline.descriptor.members)
    candidate_ids = tuple(member.run_id for member in candidate.descriptor.members)
    exact_membership = (
        baseline.descriptor.experiment_id == descriptor.experiment_id
        and candidate.descriptor.experiment_id == descriptor.experiment_id
        and baseline.descriptor.trial_set_id
        == descriptor.baseline_arm.planned_trial_set_id
        and candidate.descriptor.trial_set_id
        == descriptor.candidate_arm.planned_trial_set_id
        and baseline_ids == descriptor.baseline_arm.run_ids
        and candidate_ids == descriptor.candidate_arm.run_ids
        and all(
            context.analysis.verification.descriptor.digests.source_spec_digest
            == descriptor.baseline_arm.source_spec_digest
            and context.analysis.verification.descriptor.digests.execution_fingerprint
            == descriptor.baseline_arm.expected_execution_fingerprint
            for context in baseline_contexts
        )
        and all(
            context.analysis.verification.descriptor.digests.source_spec_digest
            == descriptor.candidate_arm.source_spec_digest
            and context.analysis.verification.descriptor.digests.execution_fingerprint
            == descriptor.candidate_arm.expected_execution_fingerprint
            for context in candidate_contexts
        )
        and not (
            {member.bundle_digest for member in baseline.descriptor.members}
            & {member.bundle_digest for member in candidate.descriptor.members}
        )
    )
    membership_check = _control_check(
        ControlCheckId.EXACT_ARM_MEMBERSHIP,
        exact_membership,
    )

    starts = tuple(context.execution.started_at for context in all_contexts)
    unique_starts = len(set(starts)) == len(starts)
    observed_order = tuple(
        context.run_id
        for context in sorted(
            all_contexts,
            key=lambda context: (context.execution.started_at, context.run_id),
        )
    )
    planned_order = tuple(slot.run_id for slot in descriptor.ordered_schedule)
    schedule_matches = unique_starts and observed_order == planned_order
    schedule_check = _control_check(
        ControlCheckId.OBSERVED_SCHEDULE,
        schedule_matches,
    )

    baseline_projections = tuple(_projection(context) for context in baseline_contexts)
    candidate_projections = tuple(
        _projection(context) for context in candidate_contexts
    )
    baseline_projection = baseline_projections[0]
    candidate_projection = candidate_projections[0]
    changed_paths = {
        path
        for path in set(baseline_projection) | set(candidate_projection)
        if baseline_projection.get(path) != candidate_projection.get(path)
    }
    treatment = descriptor.independent_variable
    treatment_matches = (
        baseline_projection.get(treatment.path) == treatment.baseline_value
        and candidate_projection.get(treatment.path) == treatment.candidate_value
    )
    fingerprint_difference_matches = (
        all(projection == baseline_projection for projection in baseline_projections)
        and all(
            projection == candidate_projection for projection in candidate_projections
        )
        and changed_paths == {treatment.path}
        and treatment_matches
        and all(
            context.spec == descriptor.baseline_arm.resolved_experiment
            for context in baseline_contexts
        )
        and all(
            context.spec == descriptor.candidate_arm.resolved_experiment
            for context in candidate_contexts
        )
    )
    fingerprint_check = _control_check(
        ControlCheckId.DECLARED_FINGERPRINT_DIFFERENCE,
        fingerprint_difference_matches,
    )

    environment_complete = all(
        context.environment.completeness is EnvironmentCompleteness.COMPLETE
        and context.analysis.verification.descriptor.environment_completeness
        is EnvironmentCompleteness.COMPLETE
        for context in all_contexts
    )
    environment_signatures = tuple(
        _environment_signature(context) for context in all_contexts
    )
    environment_equal = len(set(environment_signatures)) == 1
    environment_ok = environment_complete and environment_equal
    environment_check = _control_check(
        ControlCheckId.COMPLETE_EQUAL_OBSERVED_ENVIRONMENT,
        environment_ok,
    )

    outcome_ok, outcomes = _build_outcomes(
        descriptor,
        baseline_contexts,
        candidate_contexts,
    )
    outcome_check = _control_check(
        ControlCheckId.OUTCOME_COVERAGE_AND_SEMANTICS,
        outcome_ok,
    )
    checks = (
        plan_check,
        membership_check,
        schedule_check,
        fingerprint_check,
        environment_check,
        outcome_check,
    )
    if outcome_ok:
        arm_metric_digests = {
            context.analysis.verification.descriptor.digests.metric_definitions_digest
            for context in all_contexts
        }
        reducer_versions = {
            context.analysis.reduction.measurements.reducer_version
            for context in all_contexts
        }
        outcome_ok = arm_metric_digests == {
            descriptor.metric_definitions_digest
        } and reducer_versions == {descriptor.reducer_version}
        if not outcome_ok:
            outcome_check = _control_check(
                ControlCheckId.OUTCOME_COVERAGE_AND_SEMANTICS,
                False,
            )
            checks = (
                plan_check,
                membership_check,
                schedule_check,
                fingerprint_check,
                environment_check,
                outcome_check,
            )
    comparable = all(check.status == "SATISFIED" for check in checks)
    if comparable:
        status = ComparabilityStatus.COMPARABLE
        published_outcomes = outcomes
    else:
        status = ComparabilityStatus.INCOMPARABLE
        published_outcomes = ()
    unsatisfied_controls = tuple(
        check.check for check in checks if check.status == "UNSATISFIED"
    )

    return ControlledComparisonResult(
        schema_version="inferdrome.controlled-comparison-result.v1",
        comparison_result_id=comparison_result_id,
        comparison_plan_id=descriptor.comparison_plan_id,
        comparison_plan_digest=plan.comparison_plan_digest,
        created_at=created_at,
        baseline_trial_set=TrialSetReference(
            trial_set_id=baseline.descriptor.trial_set_id,
            trial_set_digest=baseline.trial_set_digest,
        ),
        candidate_trial_set=TrialSetReference(
            trial_set_id=candidate.descriptor.trial_set_id,
            trial_set_digest=candidate.trial_set_digest,
        ),
        status=status,
        inference_scope="POINT_ESTIMATE_ONLY",
        predeclaration_assurance="OPERATOR_ATTESTED",
        environment_control_scope="OBSERVED_V1_ALLOWLIST_ONLY",
        statistical_unit="run",
        weighting="equal_per_run",
        estimator="paired_run_mean_difference_v1",
        contrast_direction="candidate_minus_baseline",
        uncertainty_method="none_v1",
        control_checks=checks,
        unsatisfied_controls=unsatisfied_controls,
        outcomes=published_outcomes,
    )


def _verified_plan_by_id(
    comparison_plans_root: Path,
    comparison_plan_id: str,
    expected_digest: str,
    *,
    work_budget: WorkBudget | None = None,
) -> VerifiedComparisonPlan:
    selected_id = _validate_id(
        comparison_plan_id,
        ComparisonPlanId,
        label="comparison-plan ID",
    )
    return verify_comparison_plan(
        _artifact_path(
            comparison_plans_root,
            selected_id,
            root_label="comparison-plans root",
        ),
        expected_comparison_plan_digest=expected_digest,
        work_budget=work_budget,
    )


def _verified_trial_by_id(
    trial_sets_root: Path,
    runs_root: Path,
    trial_set_id: str,
    expected_digest: str,
    *,
    work_budget: WorkBudget | None = None,
) -> VerifiedTrialSet:
    selected_id = _validate_id(trial_set_id, TrialSetId, label="trial-set ID")
    try:
        return verify_trial_set(
            _artifact_path(
                trial_sets_root,
                selected_id,
                root_label="trial-sets root",
            ),
            runs_root=runs_root,
            expected_trial_set_digest=expected_digest,
            work_budget=work_budget,
        )
    except WorkLimitError:
        raise
    except ControlledComparisonError:
        raise
    except Exception as error:
        raise ControlledComparisonError(
            "controlled-comparison trial set failed verification"
        ) from error


def create_comparison_result(
    *,
    runs_root: Path,
    trial_sets_root: Path,
    comparison_plans_root: Path,
    comparison_results_root: Path,
    comparison_plan_id: str,
    expected_comparison_plan_digest: str,
    baseline_trial_set_id: str,
    expected_baseline_trial_set_digest: str,
    candidate_trial_set_id: str,
    expected_candidate_trial_set_digest: str,
    comparison_result_id: str | None = None,
    created_at: datetime | None = None,
) -> VerifiedComparisonResult:
    """Evaluate all controls and publish either point estimates or INCOMPARABLE."""

    plan = _verified_plan_by_id(
        comparison_plans_root,
        comparison_plan_id,
        expected_comparison_plan_digest,
    )
    baseline = _verified_trial_by_id(
        trial_sets_root,
        runs_root,
        baseline_trial_set_id,
        expected_baseline_trial_set_digest,
    )
    candidate = _verified_trial_by_id(
        trial_sets_root,
        runs_root,
        candidate_trial_set_id,
        expected_candidate_trial_set_digest,
    )
    result_id = comparison_result_id or new_comparison_result_id()
    _validate_id(result_id, ComparisonResultId, label="comparison-result ID")
    try:
        descriptor = _evaluate_result(
            plan=plan,
            baseline=baseline,
            candidate=candidate,
            comparison_result_id=result_id,
            created_at=created_at or datetime.now(UTC),
        )
    except (ArithmeticError, ValidationError, ValueError):
        raise ControlledComparisonError(
            "comparison-result evaluation failed closed"
        ) from None
    destination = _publish_descriptor(
        root=comparison_results_root,
        artifact_id=descriptor.comparison_result_id,
        filename=_RESULT_FILENAME,
        content=_canonical_model_bytes(descriptor),
        root_label="comparison-results root",
        artifact_label="comparison result",
    )
    return verify_comparison_result(
        destination,
        runs_root=runs_root,
        trial_sets_root=trial_sets_root,
        comparison_plans_root=comparison_plans_root,
    )


def verify_comparison_result(
    path: Path,
    *,
    runs_root: Path,
    trial_sets_root: Path,
    comparison_plans_root: Path,
    expected_comparison_result_digest: str | None = None,
    work_budget: WorkBudget | None = None,
) -> VerifiedComparisonResult:
    """Recalculate a result from its retained plan and trial-set digests."""

    loaded, initial_bytes = _load_descriptor(
        path,
        filename=_RESULT_FILENAME,
        model=ControlledComparisonResult,
        id_attribute="comparison_result_id",
        label="comparison result",
        work_budget=work_budget,
    )
    if not isinstance(loaded, ControlledComparisonResult):
        raise AssertionError("comparison-result loader returned the wrong model")
    digest = digest_bytes(DigestDomain.COMPARISON_RESULT, initial_bytes)
    if expected_comparison_result_digest is not None and not hmac.compare_digest(
        digest,
        expected_comparison_result_digest,
    ):
        raise ControlledComparisonError(
            "comparison-result digest does not match the expected value"
        )
    plan = _verified_plan_by_id(
        comparison_plans_root,
        loaded.comparison_plan_id,
        loaded.comparison_plan_digest,
        work_budget=work_budget,
    )
    baseline = _verified_trial_by_id(
        trial_sets_root,
        runs_root,
        loaded.baseline_trial_set.trial_set_id,
        loaded.baseline_trial_set.trial_set_digest,
        work_budget=work_budget,
    )
    candidate = _verified_trial_by_id(
        trial_sets_root,
        runs_root,
        loaded.candidate_trial_set.trial_set_id,
        loaded.candidate_trial_set.trial_set_digest,
        work_budget=work_budget,
    )
    try:
        recalculated = _evaluate_result(
            plan=plan,
            baseline=baseline,
            candidate=candidate,
            comparison_result_id=loaded.comparison_result_id,
            created_at=loaded.created_at,
            work_budget=work_budget,
        )
    except (ArithmeticError, ValidationError, ValueError):
        raise ControlledComparisonError(
            "comparison-result recalculation failed closed"
        ) from None
    if recalculated != loaded:
        raise ControlledComparisonError(
            "comparison-result descriptor disagrees with recalculation"
        )
    final, final_bytes = _load_descriptor(
        path,
        filename=_RESULT_FILENAME,
        model=ControlledComparisonResult,
        id_attribute="comparison_result_id",
        label="comparison result",
        work_budget=work_budget,
    )
    if final != loaded or final_bytes != initial_bytes:
        raise ControlledComparisonError(
            "comparison-result descriptor changed during verification"
        )
    return VerifiedComparisonResult(
        path=path.absolute(),
        descriptor=loaded,
        comparison_result_digest=digest,
        plan=plan,
        baseline=baseline,
        candidate=candidate,
    )


def inspect_comparison_result_declaration(
    path: Path,
    *,
    work_budget: WorkBudget | None = None,
) -> ComparisonResultDeclaration:
    """Read immutable result metadata without validating referenced evidence."""

    loaded, content = _load_descriptor(
        path,
        filename=_RESULT_FILENAME,
        model=ControlledComparisonResult,
        id_attribute="comparison_result_id",
        label="comparison result",
        work_budget=work_budget,
    )
    if not isinstance(loaded, ControlledComparisonResult):
        raise AssertionError("comparison-result loader returned the wrong model")
    return ComparisonResultDeclaration(
        path=path.absolute(),
        descriptor=loaded,
        comparison_result_digest=digest_bytes(
            DigestDomain.COMPARISON_RESULT,
            content,
        ),
    )
