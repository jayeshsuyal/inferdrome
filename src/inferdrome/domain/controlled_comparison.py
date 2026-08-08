"""Public contracts for operator-attested controlled run-level comparisons."""

import hashlib
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import DigestDomain, digest_canonical_json
from inferdrome.domain.experiment import ExperimentSpec
from inferdrome.domain.ids import (
    ComparisonPlanId,
    ComparisonResultId,
    ExperimentId,
    RunId,
    ScheduleSeed,
    SemanticVersion,
    Sha256Digest,
    SignedDecimalString,
    TrialSetId,
)
from inferdrome.domain.metrics import (
    Aggregation,
    DefinitionId,
    MetricId,
    Population,
    QuantileMethod,
    RoundingPolicy,
    Unit,
    frozen_metric_definitions_v1,
)
from inferdrome.resolution.canonicalization import (
    execution_fingerprint,
    execution_fingerprint_projection,
)


class ComparisonArm(StrEnum):
    BASELINE = "BASELINE"
    CANDIDATE = "CANDIDATE"


class ComparabilityStatus(StrEnum):
    COMPARABLE = "COMPARABLE"
    INCOMPARABLE = "INCOMPARABLE"


class ControlCheckId(StrEnum):
    LOCAL_PLAN_ORDER = "LOCAL_PLAN_ORDER"
    EXACT_ARM_MEMBERSHIP = "EXACT_ARM_MEMBERSHIP"
    OBSERVED_SCHEDULE = "OBSERVED_SCHEDULE"
    DECLARED_FINGERPRINT_DIFFERENCE = "DECLARED_FINGERPRINT_DIFFERENCE"
    COMPLETE_EQUAL_OBSERVED_ENVIRONMENT = "COMPLETE_EQUAL_OBSERVED_ENVIRONMENT"
    OUTCOME_COVERAGE_AND_SEMANTICS = "OUTCOME_COVERAGE_AND_SEMANTICS"


_CONTROL_CHECK_ORDER = tuple(ControlCheckId)
_CONTROLLED_DECIMAL_QUANTUM = Decimal("0.000001")
_CONTROLLED_DECIMAL_PRECISION = 80

ControlledDecimalString = Annotated[
    str,
    StringConstraints(
        max_length=64,
        pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$",
    ),
]

_DEFINITIONS = {
    definition.metric: definition
    for definition in frozen_metric_definitions_v1().definitions
}
_METRIC_DEFINITIONS_DIGEST = digest_canonical_json(
    DigestDomain.METRIC_DEFINITIONS,
    frozen_metric_definitions_v1().model_dump(
        mode="json",
        by_alias=True,
        exclude_none=False,
    ),
)


class ConcurrencyIndependentVariable(FrozenModel):
    value_type: Literal["integer"]
    path: Literal["traffic.concurrency"]
    baseline_value: Annotated[int, Field(strict=True, ge=1, le=100_000)]
    candidate_value: Annotated[int, Field(strict=True, ge=1, le=100_000)]

    @model_validator(mode="after")
    def values_must_differ(self) -> "ConcurrencyIndependentVariable":
        if self.baseline_value == self.candidate_value:
            raise ValueError("comparison treatment values must differ")
        return self


IndependentVariable = Annotated[
    ConcurrencyIndependentVariable,
    Field(discriminator="value_type"),
]


class ComparisonArmPlan(FrozenModel):
    arm: ComparisonArm
    planned_trial_set_id: TrialSetId
    source_spec_digest: Sha256Digest
    expected_execution_fingerprint: Sha256Digest
    resolved_experiment: ExperimentSpec
    run_ids: Annotated[tuple[RunId, ...], Field(min_length=2, max_length=100)]

    @model_validator(mode="after")
    def runs_must_be_unique(self) -> "ComparisonArmPlan":
        if len(set(self.run_ids)) != len(self.run_ids):
            raise ValueError("comparison arm run IDs must be unique")
        return self


class ComparisonScheduleSlot(FrozenModel):
    sequence_index: Annotated[int, Field(strict=True, ge=0, le=199)]
    block_index: Annotated[int, Field(strict=True, ge=0, le=99)]
    within_block_position: Literal[0, 1]
    arm: ComparisonArm
    repetition_index: Annotated[int, Field(strict=True, ge=0, le=99)]
    run_id: RunId


class OutcomeSelector(FrozenModel):
    metric: MetricId
    aggregation: Aggregation
    definition_id: DefinitionId
    unit: Unit
    population: Population
    quantile_method: QuantileMethod | None
    rounding_policy: RoundingPolicy

    @model_validator(mode="after")
    def aggregation_must_match_metric(self) -> "OutcomeSelector":
        definition = _DEFINITIONS[self.metric]
        if self.aggregation not in definition.allowed_aggregations:
            raise ValueError("outcome aggregation is not valid for the metric")
        is_quantile = self.aggregation in {
            Aggregation.P50,
            Aggregation.P95,
            Aggregation.P99,
        }
        expected_quantile = definition.quantile_method if is_quantile else None
        if (
            self.definition_id is not definition.definition_id
            or self.unit is not definition.unit
            or self.population is not definition.population
            or self.quantile_method is not expected_quantile
            or self.rounding_policy is not definition.rounding_policy
        ):
            raise ValueError("outcome selector does not match frozen v1 semantics")
        return self


def frozen_outcome_selector(
    metric: MetricId,
    aggregation: Aggregation,
) -> OutcomeSelector:
    """Build a primary-outcome selector with the frozen metric semantics."""

    definition = _DEFINITIONS[metric]
    is_quantile = aggregation in {
        Aggregation.P50,
        Aggregation.P95,
        Aggregation.P99,
    }
    return OutcomeSelector(
        metric=metric,
        aggregation=aggregation,
        definition_id=definition.definition_id,
        unit=definition.unit,
        population=definition.population,
        quantile_method=definition.quantile_method if is_quantile else None,
        rounding_policy=definition.rounding_policy,
    )


def comparison_schedule_arm_order(
    seed: ScheduleSeed,
    block_index: int,
) -> tuple[ComparisonArm, ComparisonArm]:
    """Derive the auditable within-pair order from the frozen seed."""

    digest = hashlib.sha256(
        b"inferdrome:comparison-schedule-v1\0"
        + bytes.fromhex(seed)
        + block_index.to_bytes(2, "big")
    ).digest()
    if digest[0] & 1:
        return ComparisonArm.CANDIDATE, ComparisonArm.BASELINE
    return ComparisonArm.BASELINE, ComparisonArm.CANDIDATE


class ControlledComparisonPlan(FrozenModel):
    """Immutable design created before every run named by the plan."""

    schema_version: Literal["inferdrome.controlled-comparison-plan.v1"]
    comparison_plan_id: ComparisonPlanId
    experiment_id: ExperimentId
    title: Annotated[str, Field(min_length=1, max_length=256)]
    hypothesis: Annotated[str, Field(min_length=1, max_length=4096)]
    created_at: AwareDatetime
    design_status: Literal["PREDECLARED"]
    arm_membership_policy: Literal["exact_ordered_run_ids_v1"]
    schedule_policy: Literal["predeclared_permuted_pairs_v1"]
    schedule_seed: ScheduleSeed
    statistical_unit: Literal["run"]
    request_population_policy: Literal["separate_per_run_v1"]
    weighting: Literal["equal_per_run"]
    planned_repetitions_per_arm: Annotated[int, Field(strict=True, ge=2, le=100)]
    independent_variable: IndependentVariable
    baseline_arm: ComparisonArmPlan
    candidate_arm: ComparisonArmPlan
    ordered_schedule: Annotated[
        tuple[ComparisonScheduleSlot, ...], Field(min_length=4, max_length=200)
    ]
    primary_outcome: OutcomeSelector
    metric_definitions_digest: Sha256Digest
    reducer_version: SemanticVersion
    estimator: Literal["paired_run_mean_difference_v1"]
    contrast_direction: Literal["candidate_minus_baseline"]
    uncertainty_method: Literal["none_v1"]
    missing_data_policy: Literal["incomparable_if_any_outcome_missing_v1"]
    exclusion_policy: Literal["no_post_assignment_exclusions_v1"]
    environment_policy: Literal["complete_and_equal_observed_environment_v1"]
    environment_control_scope: Literal["OBSERVED_V1_ALLOWLIST_ONLY"]
    predeclaration_anchor: Literal["operator_retained_plan_digest_required_v1"]
    predeclaration_assurance: Literal["OPERATOR_ATTESTED"]

    @model_validator(mode="after")
    def design_must_be_closed_and_ordered(self) -> "ControlledComparisonPlan":
        repetition_count = self.planned_repetitions_per_arm
        if self.baseline_arm.arm is not ComparisonArm.BASELINE:
            raise ValueError("baseline arm must have the BASELINE role")
        if self.candidate_arm.arm is not ComparisonArm.CANDIDATE:
            raise ValueError("candidate arm must have the CANDIDATE role")
        if (
            self.baseline_arm.planned_trial_set_id
            == self.candidate_arm.planned_trial_set_id
        ):
            raise ValueError("comparison arms require distinct Trial Set IDs")
        if len(self.baseline_arm.run_ids) != repetition_count:
            raise ValueError("baseline arm size must match the planned repetitions")
        if len(self.candidate_arm.run_ids) != repetition_count:
            raise ValueError("candidate arm size must match the planned repetitions")

        all_run_ids = (*self.baseline_arm.run_ids, *self.candidate_arm.run_ids)
        if len(set(all_run_ids)) != len(all_run_ids):
            raise ValueError("comparison arms must be disjoint")
        for arm in (self.baseline_arm, self.candidate_arm):
            if arm.resolved_experiment.experiment.id != self.experiment_id:
                raise ValueError("comparison arm experiment identity disagrees")
            if (
                execution_fingerprint(arm.resolved_experiment)
                != arm.expected_execution_fingerprint
            ):
                raise ValueError("comparison arm fingerprint pin is incorrect")
            if (
                arm.resolved_experiment.measurement.reducer_version
                != self.reducer_version
            ):
                raise ValueError("comparison arm reducer version disagrees")
        if self.metric_definitions_digest != _METRIC_DEFINITIONS_DIGEST:
            raise ValueError("comparison metric-definition digest is not frozen v1")

        baseline_full = _flatten_mapping(
            self.baseline_arm.resolved_experiment.model_dump(
                mode="json", by_alias=True, exclude_none=False
            )
        )
        candidate_full = _flatten_mapping(
            self.candidate_arm.resolved_experiment.model_dump(
                mode="json", by_alias=True, exclude_none=False
            )
        )
        full_changed_paths = {
            path
            for path in set(baseline_full) | set(candidate_full)
            if baseline_full.get(path) != candidate_full.get(path)
        }
        baseline_fingerprint = _flatten_mapping(
            execution_fingerprint_projection(self.baseline_arm.resolved_experiment)
        )
        candidate_fingerprint = _flatten_mapping(
            execution_fingerprint_projection(self.candidate_arm.resolved_experiment)
        )
        fingerprint_changed_paths = {
            path
            for path in set(baseline_fingerprint) | set(candidate_fingerprint)
            if baseline_fingerprint.get(path) != candidate_fingerprint.get(path)
        }
        treatment = self.independent_variable
        if full_changed_paths != {treatment.path} or fingerprint_changed_paths != {
            treatment.path
        }:
            raise ValueError("planned arms must differ only at the treatment path")
        if (
            baseline_full.get(treatment.path) != treatment.baseline_value
            or candidate_full.get(treatment.path) != treatment.candidate_value
        ):
            raise ValueError("planned arm values disagree with the treatment")
        if len(self.ordered_schedule) != repetition_count * 2:
            raise ValueError("comparison schedule must cover both complete arms")
        if tuple(slot.sequence_index for slot in self.ordered_schedule) != tuple(
            range(repetition_count * 2)
        ):
            raise ValueError("comparison schedule indices must be contiguous")
        if len({slot.run_id for slot in self.ordered_schedule}) != len(all_run_ids):
            raise ValueError("comparison schedule run IDs must be unique")

        runs_by_arm = {
            ComparisonArm.BASELINE: self.baseline_arm.run_ids,
            ComparisonArm.CANDIDATE: self.candidate_arm.run_ids,
        }
        for slot in self.ordered_schedule:
            arm_runs = runs_by_arm[slot.arm]
            if slot.repetition_index >= len(arm_runs):
                raise ValueError("comparison schedule repetition is out of range")
            if arm_runs[slot.repetition_index] != slot.run_id:
                raise ValueError("comparison schedule disagrees with arm membership")
            if slot.block_index != slot.repetition_index:
                raise ValueError("comparison schedule blocks must pair repetitions")
            if slot.sequence_index != slot.block_index * 2 + slot.within_block_position:
                raise ValueError("comparison schedule position is inconsistent")
        if {slot.run_id for slot in self.ordered_schedule} != set(all_run_ids):
            raise ValueError("comparison schedule must cover every planned run")
        for block_index in range(repetition_count):
            block = tuple(
                slot
                for slot in self.ordered_schedule
                if slot.block_index == block_index
            )
            expected_order = comparison_schedule_arm_order(
                self.schedule_seed,
                block_index,
            )
            if (
                len(block) != 2
                or tuple(slot.within_block_position for slot in block) != (0, 1)
                or tuple(slot.arm for slot in block) != expected_order
            ):
                raise ValueError("comparison schedule disagrees with its seed")
        return self


class TrialSetReference(FrozenModel):
    trial_set_id: TrialSetId
    trial_set_digest: Sha256Digest


class ComparisonControlCheck(FrozenModel):
    check: ControlCheckId
    status: Literal["SATISFIED", "UNSATISFIED"]


class ControlledRunValue(FrozenModel):
    repetition_index: Annotated[int, Field(strict=True, ge=0, le=99)]
    run_id: RunId
    value: ControlledDecimalString
    sample_count: Annotated[int, Field(strict=True, ge=0)]


class PairedRunDifference(FrozenModel):
    block_index: Annotated[int, Field(strict=True, ge=0, le=99)]
    baseline_run_id: RunId
    candidate_run_id: RunId
    candidate_minus_baseline: SignedDecimalString


class ControlledOutcomeEstimate(FrozenModel):
    selector: OutcomeSelector
    unit: Unit
    baseline_values: Annotated[
        tuple[ControlledRunValue, ...], Field(min_length=2, max_length=100)
    ]
    candidate_values: Annotated[
        tuple[ControlledRunValue, ...], Field(min_length=2, max_length=100)
    ]
    paired_differences: Annotated[
        tuple[PairedRunDifference, ...], Field(min_length=2, max_length=100)
    ]
    baseline_mean: ControlledDecimalString
    candidate_mean: ControlledDecimalString
    estimate: SignedDecimalString

    @model_validator(mode="after")
    def points_must_be_ordered_and_disjoint(self) -> "ControlledOutcomeEstimate":
        for points in (self.baseline_values, self.candidate_values):
            expected = tuple(range(len(points)))
            if tuple(point.repetition_index for point in points) != expected:
                raise ValueError("controlled outcome points must be repetition ordered")
            if len({point.run_id for point in points}) != len(points):
                raise ValueError("controlled outcome run IDs must be unique per arm")
        baseline_ids = {point.run_id for point in self.baseline_values}
        candidate_ids = {point.run_id for point in self.candidate_values}
        if baseline_ids & candidate_ids:
            raise ValueError("controlled outcome arms must be disjoint")
        if len(self.baseline_values) != len(self.candidate_values):
            raise ValueError("controlled outcome arms must have equal run counts")
        if len(self.paired_differences) != len(self.baseline_values):
            raise ValueError(
                "every controlled repetition requires one paired difference"
            )
        if tuple(point.block_index for point in self.paired_differences) != tuple(
            range(len(self.paired_differences))
        ):
            raise ValueError("paired differences must be block ordered")
        for baseline, candidate, difference in zip(
            self.baseline_values,
            self.candidate_values,
            self.paired_differences,
            strict=True,
        ):
            if (
                difference.baseline_run_id != baseline.run_id
                or difference.candidate_run_id != candidate.run_id
            ):
                raise ValueError("paired difference run identities disagree")
            if difference.candidate_minus_baseline != _controlled_decimal_text(
                Decimal(candidate.value) - Decimal(baseline.value)
            ):
                raise ValueError("paired difference arithmetic disagrees")
        if self.selector.unit is not self.unit:
            raise ValueError("controlled outcome selector and unit disagree")
        with localcontext() as context:
            context.prec = _CONTROLLED_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            baseline_mean = sum(
                (Decimal(point.value) for point in self.baseline_values),
                start=Decimal(0),
            ) / len(self.baseline_values)
            candidate_mean = sum(
                (Decimal(point.value) for point in self.candidate_values),
                start=Decimal(0),
            ) / len(self.candidate_values)
        if self.baseline_mean != _controlled_decimal_text(baseline_mean):
            raise ValueError("controlled baseline mean arithmetic disagrees")
        if self.candidate_mean != _controlled_decimal_text(candidate_mean):
            raise ValueError("controlled candidate mean arithmetic disagrees")
        if self.estimate != _controlled_decimal_text(candidate_mean - baseline_mean):
            raise ValueError("controlled estimate arithmetic disagrees")
        return self


class ControlledComparisonResult(FrozenModel):
    """Immutable point estimates or an explicit incomparable determination."""

    schema_version: Literal["inferdrome.controlled-comparison-result.v1"]
    comparison_result_id: ComparisonResultId
    comparison_plan_id: ComparisonPlanId
    comparison_plan_digest: Sha256Digest
    created_at: AwareDatetime
    baseline_trial_set: TrialSetReference
    candidate_trial_set: TrialSetReference
    status: ComparabilityStatus
    inference_scope: Literal["POINT_ESTIMATE_ONLY"]
    predeclaration_assurance: Literal["OPERATOR_ATTESTED"]
    environment_control_scope: Literal["OBSERVED_V1_ALLOWLIST_ONLY"]
    statistical_unit: Literal["run"]
    weighting: Literal["equal_per_run"]
    estimator: Literal["paired_run_mean_difference_v1"]
    contrast_direction: Literal["candidate_minus_baseline"]
    uncertainty_method: Literal["none_v1"]
    control_checks: Annotated[
        tuple[ComparisonControlCheck, ...], Field(min_length=6, max_length=6)
    ]
    unsatisfied_controls: Annotated[tuple[ControlCheckId, ...], Field(max_length=6)]
    outcomes: Annotated[tuple[ControlledOutcomeEstimate, ...], Field(max_length=1)]

    @model_validator(mode="after")
    def result_must_match_comparability(self) -> "ControlledComparisonResult":
        if (
            self.baseline_trial_set.trial_set_id
            == self.candidate_trial_set.trial_set_id
        ):
            raise ValueError("controlled comparison requires two trial sets")
        if tuple(check.check for check in self.control_checks) != _CONTROL_CHECK_ORDER:
            raise ValueError("controlled comparison checks must use the v1 order")
        satisfied = all(check.status == "SATISFIED" for check in self.control_checks)
        expected_unsatisfied = tuple(
            check.check
            for check in self.control_checks
            if check.status == "UNSATISFIED"
        )
        if self.unsatisfied_controls != expected_unsatisfied:
            raise ValueError("unsatisfied controls must match the ordered checks")
        if self.status is ComparabilityStatus.COMPARABLE:
            if not satisfied or not self.outcomes:
                raise ValueError("comparable results require all checks and outcomes")
        elif satisfied or self.outcomes:
            raise ValueError("incomparable results must omit point estimates")
        selector_keys = tuple(
            (outcome.selector.metric, outcome.selector.aggregation)
            for outcome in self.outcomes
        )
        if len(set(selector_keys)) != len(selector_keys):
            raise ValueError("controlled result outcomes must be unique")
        return self


def _flatten_mapping(value: object, *, prefix: str = "") -> dict[str, object]:
    if isinstance(value, dict):
        flattened: dict[str, object] = {}
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten_mapping(nested, prefix=path))
        return flattened
    return {prefix: value}


def _controlled_decimal_text(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = _CONTROLLED_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        rounded = value.quantize(_CONTROLLED_DECIMAL_QUANTUM)
        normalized = rounded.normalize()
    if rounded == 0:
        return "0"
    return format(normalized, "f")
