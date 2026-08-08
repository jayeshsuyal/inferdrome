"""Typed, bounded response contracts for the local dashboard."""

from datetime import datetime
from typing import Literal

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.controlled_comparison import (
    ComparisonScheduleSlot,
    ConcurrencyIndependentVariable,
    ControlledComparisonResult,
    OutcomeSelector,
)


class MetricView(FrozenModel):
    key: str
    metric: str
    aggregation: str
    label: str
    value: str
    display_value: str
    unit: str
    sample_count: int
    population: str
    definition_id: str
    quantile_method: str | None
    rounding_policy: str


class RunSummary(FrozenModel):
    run_id: str
    experiment_id: str
    title: str
    model: str
    producer_name: str
    producer_version: str
    adapter_name: str
    adapter_version: str
    execution_mode: str
    started_at: datetime
    ended_at: datetime
    duration_ns: int
    integrity_status: Literal["VALID"]
    evidence_eligibility: str
    environment_completeness: str
    replayability: str
    bundle_digest: str
    measured_requests: int
    successful_requests: int
    failed_requests: int
    error_rate: str
    ttft_p50_ns: int | None
    ttft_p95_ns: int | None
    output_token_throughput_per_s: str
    headline_metrics: tuple[MetricView, ...]


class ExecutionView(FrozenModel):
    terminal_state: Literal["COMPLETE"]
    started_at: datetime
    ended_at: datetime
    duration_ns: int
    measurement_window_ns: int
    measurement_window_definition: str
    traffic_kind: str
    concurrency: int | None
    requests_per_second: str | None
    max_concurrency: int | None
    warmup_requests: int
    measured_requests: int
    producer_exit_status: int


class DistributionBin(FrozenModel):
    lower_bound: int
    upper_bound: int
    count: int


class DistributionView(FrozenModel):
    metric: str
    label: str
    unit: str
    sample_count: int
    minimum: int | None
    maximum: int | None
    bins: tuple[DistributionBin, ...]


class ContextFieldView(FrozenModel):
    key: str
    label: str
    value: str | None
    group: Literal[
        "experiment",
        "execution",
        "target",
        "traffic",
        "measurement",
        "producer",
    ]


class EnvironmentFieldView(FrozenModel):
    name: str
    label: str
    value: str | int | bool | None
    provenance: str
    evidence_path: str | None


class ArtifactView(FrozenModel):
    role: str
    path: str
    media_type: str
    sensitivity: str
    size_bytes: int
    content_exposed: Literal[False]


class UnavailableMetricView(FrozenModel):
    metric: str
    reason: str
    capability_matrix: str


class DigestView(FrozenModel):
    source_spec_digest: str
    execution_fingerprint: str
    request_plan_digest: str
    metric_definitions_digest: str
    exitspec_contract_digest: str | None


class SensitivityView(FrozenModel):
    prompt_content_in_request_plan: bool
    canonical_response_content_included: bool
    native_response_content_present: bool
    secrets_permitted: Literal[False]


class VerificationView(FrozenModel):
    bundle_digest: str
    artifact_count: int
    total_bytes: int
    integrity_status: Literal["VALID"]
    evidence_eligibility: str
    environment_completeness: str
    replayability: str
    verified_by_recalculation: Literal[True]


class ComparisonContractView(FrozenModel):
    execution_fingerprint: str
    metric_definitions_digest: str
    reducer_version: str
    execution_mode: str
    workload_sha256: str
    requested_output_tokens: int
    temperature: str
    seed: int
    traffic_signature: str
    measurement_signature: str


class RunDetail(FrozenModel):
    projection_version: Literal["inferdrome.dashboard.v1"] = "inferdrome.dashboard.v1"
    summary: RunSummary
    hypothesis: str | None
    verification: VerificationView
    execution: ExecutionView
    measurements: tuple[MetricView, ...]
    distributions: tuple[DistributionView, ...]
    context: tuple[ContextFieldView, ...]
    environment: tuple[EnvironmentFieldView, ...]
    artifacts: tuple[ArtifactView, ...]
    unavailable: tuple[UnavailableMetricView, ...]
    digests: DigestView
    sensitivity: SensitivityView
    comparison_contract: ComparisonContractView


class RejectedRun(FrozenModel):
    entry: str
    status: Literal["REJECTED"] = "REJECTED"
    code: Literal[
        "VERIFICATION_FAILED",
        "UNSAFE_ENTRY",
        "BUNDLE_UNAVAILABLE",
        "DUPLICATE_RUN_ID",
    ]
    message: str = "Bundle could not be verified."


class PageView(FrozenModel):
    limit: int
    returned: int
    total: int
    has_more: bool
    next_cursor: str | None


class RunIndexResponse(FrozenModel):
    projection_version: Literal["inferdrome.dashboard.v1"] = "inferdrome.dashboard.v1"
    generated_at: datetime
    runs: tuple[RunSummary, ...]
    rejected: tuple[RejectedRun, ...]
    page: PageView


class MetricDeltaView(FrozenModel):
    key: str
    metric: str
    aggregation: str
    label: str
    unit: str
    baseline_value: str
    candidate_value: str
    absolute_delta: str
    percent_delta: str | None
    baseline_display_value: str
    candidate_display_value: str
    delta_display_value: str


class ContextChangeView(FrozenModel):
    key: str
    label: str
    baseline_value: str | None
    candidate_value: str | None
    group: str


class ComparisonResponse(FrozenModel):
    projection_version: Literal["inferdrome.dashboard.v1"] = "inferdrome.dashboard.v1"
    baseline_run_id: str
    candidate_run_id: str
    status: Literal[
        "COMPARABLE",
        "COMPARABLE_WITH_CONTEXT_CHANGES",
        "INCOMPARABLE",
    ]
    reasons: tuple[str, ...]
    metric_deltas: tuple[MetricDeltaView, ...]
    context_changes: tuple[ContextChangeView, ...]
    directionality: Literal["NEUTRAL"] = "NEUTRAL"


class TrialRunPointView(FrozenModel):
    repetition_index: int
    run_id: str
    value: str | None
    display_value: str | None
    sample_count: int | None


class TrialMetricVariationView(FrozenModel):
    key: str
    metric: str
    aggregation: str
    label: str
    unit: str
    total_run_count: int
    available_run_count: int
    minimum: str | None
    maximum: str | None
    median: str | None
    mean: str | None
    span: str | None
    sample_standard_deviation: str | None
    minimum_display_value: str | None
    maximum_display_value: str | None
    median_display_value: str | None
    mean_display_value: str | None
    span_display_value: str | None
    sample_standard_deviation_display_value: str | None
    points: tuple[TrialRunPointView, ...]
    population: Literal["run_level_measurements"] = "run_level_measurements"
    weighting: Literal["equal_per_run"] = "equal_per_run"
    summary_method: Literal["per_run_scalar_sample_variation_v1"] = (
        "per_run_scalar_sample_variation_v1"
    )


class TrialSetSummary(FrozenModel):
    trial_set_id: str
    experiment_id: str
    title: str
    created_at: datetime
    member_count: int
    earliest_run_at: datetime
    latest_run_at: datetime
    model: str
    execution_fingerprint: str
    trial_set_digest: str
    evidence_eligibilities: tuple[str, ...]
    environment_status: Literal["CONSISTENT", "DRIFT_DETECTED"]


class TrialSetMemberView(FrozenModel):
    repetition_index: int
    run: RunSummary


class TrialSetDetail(FrozenModel):
    projection_version: Literal["inferdrome.dashboard.v1"] = "inferdrome.dashboard.v1"
    summary: TrialSetSummary
    hypothesis: str | None
    membership_policy: Literal["same_execution_fingerprint_v1"]
    metric_definitions_digest: str
    reducer_version: str
    members: tuple[TrialSetMemberView, ...]
    variations: tuple[TrialMetricVariationView, ...]
    environment_drift_fields: tuple[str, ...]
    design_status: Literal["RETROSPECTIVE"] = "RETROSPECTIVE"
    inference: Literal["DESCRIPTIVE_ONLY"] = "DESCRIPTIVE_ONLY"
    request_population_policy: Literal["separate_per_run_v1"] = "separate_per_run_v1"


class RejectedTrialSet(FrozenModel):
    entry: str
    status: Literal["REJECTED"] = "REJECTED"
    code: Literal[
        "VERIFICATION_FAILED",
        "UNSAFE_ENTRY",
        "MEMBER_UNAVAILABLE",
        "DECLARATION_UNAVAILABLE",
        "DUPLICATE_TRIAL_SET_ID",
    ]
    message: str = "Trial set could not be verified."


class TrialSetIndexResponse(FrozenModel):
    projection_version: Literal["inferdrome.dashboard.v1"] = "inferdrome.dashboard.v1"
    generated_at: datetime
    trial_sets: tuple[TrialSetSummary, ...]
    rejected: tuple[RejectedTrialSet, ...]
    page: PageView


class ControlledComparisonSummary(FrozenModel):
    comparison_plan_id: str
    comparison_plan_digest: str
    experiment_id: str
    title: str
    created_at: datetime
    treatment_path: Literal["traffic.concurrency"]
    baseline_value: int
    candidate_value: int
    planned_repetitions_per_arm: int
    baseline_trial_set_id: str
    candidate_trial_set_id: str
    design_status: Literal["PREDECLARED"]
    predeclaration_assurance: Literal["OPERATOR_ATTESTED"]
    primary_outcome_key: str
    primary_outcome_label: str
    primary_outcome_unit: str
    result_status: Literal[
        "COMPARABLE",
        "INCOMPARABLE",
        "NO_RESULT",
        "WITHHELD",
    ]
    comparison_result_id: str | None
    comparison_result_digest: str | None
    estimate: str | None
    estimate_display_value: str | None


class RejectedControlledComparison(FrozenModel):
    entry: str
    status: Literal["REJECTED"] = "REJECTED"
    code: Literal[
        "UNSAFE_ENTRY",
        "DECLARATION_UNAVAILABLE",
        "PLAN_VERIFICATION_FAILED",
        "RESULT_VERIFICATION_FAILED",
        "DUPLICATE_RESULT_FOR_PLAN",
        "ORPHAN_RESULT",
    ]
    failure_fingerprint: str
    message: str = "Controlled comparison declaration could not be verified."


class ControlledComparisonIndexResponse(FrozenModel):
    projection_version: Literal["inferdrome.dashboard.v1"] = "inferdrome.dashboard.v1"
    generated_at: datetime
    comparisons: tuple[ControlledComparisonSummary, ...]
    rejected: tuple[RejectedControlledComparison, ...]
    page: PageView


class ControlledComparisonArmPlanView(FrozenModel):
    arm: Literal["BASELINE", "CANDIDATE"]
    planned_trial_set_id: str
    source_spec_digest: str
    expected_execution_fingerprint: str
    run_ids: tuple[str, ...]


class ControlledComparisonPlanView(FrozenModel):
    schema_version: Literal["inferdrome.controlled-comparison-plan.v1"]
    comparison_plan_id: str
    experiment_id: str
    title: str
    hypothesis: str
    created_at: datetime
    design_status: Literal["PREDECLARED"]
    arm_membership_policy: Literal["exact_ordered_run_ids_v1"]
    schedule_policy: Literal["predeclared_permuted_pairs_v1"]
    schedule_seed: str
    statistical_unit: Literal["run"]
    request_population_policy: Literal["separate_per_run_v1"]
    weighting: Literal["equal_per_run"]
    planned_repetitions_per_arm: int
    independent_variable: ConcurrencyIndependentVariable
    baseline_arm: ControlledComparisonArmPlanView
    candidate_arm: ControlledComparisonArmPlanView
    ordered_schedule: tuple[ComparisonScheduleSlot, ...]
    primary_outcome: OutcomeSelector
    metric_definitions_digest: str
    reducer_version: str
    estimator: Literal["paired_run_mean_difference_v1"]
    contrast_direction: Literal["candidate_minus_baseline"]
    uncertainty_method: Literal["none_v1"]
    missing_data_policy: Literal["incomparable_if_any_outcome_missing_v1"]
    exclusion_policy: Literal["no_post_assignment_exclusions_v1"]
    environment_policy: Literal["complete_and_equal_observed_environment_v1"]
    environment_control_scope: Literal["OBSERVED_V1_ALLOWLIST_ONLY"]
    predeclaration_anchor: Literal["operator_retained_plan_digest_required_v1"]
    predeclaration_assurance: Literal["OPERATOR_ATTESTED"]


class ControlledComparisonDetail(FrozenModel):
    projection_version: Literal["inferdrome.dashboard.v1"] = "inferdrome.dashboard.v1"
    summary: ControlledComparisonSummary
    plan: ControlledComparisonPlanView
    result: ControlledComparisonResult | None
    baseline_trial_set: TrialSetSummary | None
    candidate_trial_set: TrialSetSummary | None
    result_issue: (
        Literal[
            "RESULT_VERIFICATION_FAILED",
            "DUPLICATE_RESULT_FOR_PLAN",
        ]
        | None
    )
