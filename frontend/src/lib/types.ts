/**
 * Exact TypeScript mirrors of src/inferdrome/dashboard/models.py.
 *
 * ApiObject permits additive response fields so a compatible backend extension
 * does not break the client, while every v1 field consumed below stays required
 * and precisely typed.
 */
export interface ApiObject {
  readonly [key: string]: unknown;
}

export type JsonPrimitive = string | number | boolean | null;
export type ComparisonStatus =
  | "COMPARABLE"
  | "COMPARABLE_WITH_CONTEXT_CHANGES"
  | "INCOMPARABLE";
export type EvidenceEligibility = string;
export type IntegrityStatus = "VALID";

export interface MetricView extends ApiObject {
  readonly key: string;
  readonly metric: string;
  readonly aggregation: string;
  readonly label: string;
  readonly value: string;
  readonly display_value: string;
  readonly unit: string;
  readonly sample_count: number;
  readonly population: string;
  readonly definition_id: string;
  readonly quantile_method: string | null;
  readonly rounding_policy: string;
}

export type Measurement = MetricView;
export type MeasurementValue = string;

export interface RunSummary extends ApiObject {
  readonly run_id: string;
  readonly experiment_id: string;
  readonly title: string;
  readonly model: string;
  readonly producer_name: string;
  readonly producer_version: string;
  readonly adapter_name: string;
  readonly adapter_version: string;
  readonly execution_mode: string;
  readonly started_at: string;
  readonly ended_at: string;
  readonly duration_ns: number;
  readonly integrity_status: "VALID";
  readonly evidence_eligibility: string;
  readonly environment_completeness: string;
  readonly replayability: string;
  readonly bundle_digest: string;
  readonly measured_requests: number;
  readonly successful_requests: number;
  readonly failed_requests: number;
  readonly error_rate: string;
  readonly ttft_p50_ns: number | null;
  readonly ttft_p95_ns: number | null;
  readonly output_token_throughput_per_s: string;
  readonly headline_metrics: readonly MetricView[];
}

export interface ExecutionView extends ApiObject {
  readonly terminal_state: "COMPLETE";
  readonly started_at: string;
  readonly ended_at: string;
  readonly duration_ns: number;
  readonly measurement_window_ns: number;
  readonly measurement_window_definition: string;
  readonly traffic_kind: string;
  readonly concurrency: number | null;
  readonly requests_per_second: string | null;
  readonly max_concurrency: number | null;
  readonly warmup_requests: number;
  readonly measured_requests: number;
  readonly producer_exit_status: number;
}

export interface DistributionBin extends ApiObject {
  readonly lower_bound: number;
  readonly upper_bound: number;
  readonly count: number;
}

export interface DistributionView extends ApiObject {
  readonly metric: string;
  readonly label: string;
  readonly unit: string;
  readonly sample_count: number;
  readonly minimum: number | null;
  readonly maximum: number | null;
  readonly bins: readonly DistributionBin[];
}

export interface ContextFieldView extends ApiObject {
  readonly key: string;
  readonly label: string;
  readonly value: string | null;
  readonly group: "experiment" | "execution" | "target" | "traffic" | "measurement" | "producer";
}

export interface EnvironmentFieldView extends ApiObject {
  readonly name: string;
  readonly label: string;
  readonly value: string | number | boolean | null;
  readonly provenance: string;
  readonly evidence_path: string | null;
}

export interface ArtifactView extends ApiObject {
  readonly role: string;
  readonly path: string;
  readonly media_type: string;
  readonly sensitivity: string;
  readonly size_bytes: number;
  readonly content_exposed: false;
}

export interface UnavailableMetricView extends ApiObject {
  readonly metric: string;
  readonly reason: string;
  readonly capability_matrix: string;
}

export interface DigestView extends ApiObject {
  readonly source_spec_digest: string;
  readonly execution_fingerprint: string;
  readonly request_plan_digest: string;
  readonly metric_definitions_digest: string;
  readonly exitspec_contract_digest: string | null;
}

export interface SensitivityView extends ApiObject {
  readonly prompt_content_in_request_plan: boolean;
  readonly canonical_response_content_included: boolean;
  readonly native_response_content_present: boolean;
  readonly secrets_permitted: false;
}

export interface VerificationView extends ApiObject {
  readonly bundle_digest: string;
  readonly artifact_count: number;
  readonly total_bytes: number;
  readonly integrity_status: "VALID";
  readonly evidence_eligibility: string;
  readonly environment_completeness: string;
  readonly replayability: string;
  readonly verified_by_recalculation: true;
}

export interface ComparisonContractView extends ApiObject {
  readonly execution_fingerprint: string;
  readonly metric_definitions_digest: string;
  readonly reducer_version: string;
  readonly execution_mode: string;
  readonly workload_sha256: string;
  readonly requested_output_tokens: number;
  readonly temperature: string;
  readonly seed: number;
  readonly traffic_signature: string;
  readonly measurement_signature: string;
}

export interface RunDetail extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly summary: RunSummary;
  readonly hypothesis: string | null;
  readonly verification: VerificationView;
  readonly execution: ExecutionView;
  readonly measurements: readonly MetricView[];
  readonly distributions: readonly DistributionView[];
  readonly context: readonly ContextFieldView[];
  readonly environment: readonly EnvironmentFieldView[];
  readonly artifacts: readonly ArtifactView[];
  readonly unavailable: readonly UnavailableMetricView[];
  readonly digests: DigestView;
  readonly sensitivity: SensitivityView;
  readonly comparison_contract: ComparisonContractView;
}

export interface RejectedRun extends ApiObject {
  readonly entry: string;
  readonly status: "REJECTED";
  readonly code:
    | "VERIFICATION_FAILED"
    | "UNSAFE_ENTRY"
    | "BUNDLE_UNAVAILABLE"
    | "DUPLICATE_RUN_ID";
  readonly message: string;
}

export interface PageView extends ApiObject {
  readonly limit: number;
  readonly returned: number;
  readonly total: number;
  readonly has_more: boolean;
  readonly next_cursor: string | null;
}

export interface RunsPageResponse extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly generated_at: string;
  readonly runs: readonly RunSummary[];
  readonly rejected: readonly RejectedRun[];
  readonly page: PageView;
}

export interface RunIndex extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly generated_at: string;
  readonly runs: readonly RunSummary[];
  readonly rejected: readonly RejectedRun[];
}

export interface MetricDeltaView extends ApiObject {
  readonly key: string;
  readonly metric: string;
  readonly aggregation: string;
  readonly label: string;
  readonly unit: string;
  readonly baseline_value: string;
  readonly candidate_value: string;
  readonly absolute_delta: string;
  readonly percent_delta: string | null;
  readonly baseline_display_value: string;
  readonly candidate_display_value: string;
  readonly delta_display_value: string;
}

export type ComparisonDelta = MetricDeltaView;

export interface ContextChangeView extends ApiObject {
  readonly key: string;
  readonly label: string;
  readonly baseline_value: string | null;
  readonly candidate_value: string | null;
  readonly group: string;
}

export interface Comparison extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly baseline_run_id: string;
  readonly candidate_run_id: string;
  readonly status: ComparisonStatus;
  readonly reasons: readonly string[];
  readonly metric_deltas: readonly MetricDeltaView[];
  readonly context_changes: readonly ContextChangeView[];
  readonly directionality: "NEUTRAL";
}

export interface TrialRunPointView extends ApiObject {
  readonly repetition_index: number;
  readonly run_id: string;
  readonly value: string | null;
  readonly display_value: string | null;
  readonly sample_count: number | null;
}

export interface TrialMetricVariationView extends ApiObject {
  readonly key: string;
  readonly metric: string;
  readonly aggregation: string;
  readonly label: string;
  readonly unit: string;
  readonly total_run_count: number;
  readonly available_run_count: number;
  readonly minimum: string | null;
  readonly maximum: string | null;
  readonly median: string | null;
  readonly mean: string | null;
  readonly span: string | null;
  readonly sample_standard_deviation: string | null;
  readonly minimum_display_value: string | null;
  readonly maximum_display_value: string | null;
  readonly median_display_value: string | null;
  readonly mean_display_value: string | null;
  readonly span_display_value: string | null;
  readonly sample_standard_deviation_display_value: string | null;
  readonly points: readonly TrialRunPointView[];
  readonly population: "run_level_measurements";
  readonly weighting: "equal_per_run";
  readonly summary_method: "per_run_scalar_sample_variation_v1";
}

export interface TrialSetSummary extends ApiObject {
  readonly trial_set_id: string;
  readonly experiment_id: string;
  readonly title: string;
  readonly created_at: string;
  readonly member_count: number;
  readonly earliest_run_at: string;
  readonly latest_run_at: string;
  readonly model: string;
  readonly execution_fingerprint: string;
  readonly trial_set_digest: string;
  readonly evidence_eligibilities: readonly string[];
  readonly environment_status: "CONSISTENT" | "DRIFT_DETECTED";
}

export interface TrialSetMemberView extends ApiObject {
  readonly repetition_index: number;
  readonly run: RunSummary;
}

export interface TrialSetDetail extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly summary: TrialSetSummary;
  readonly hypothesis: string | null;
  readonly membership_policy: "same_execution_fingerprint_v1";
  readonly metric_definitions_digest: string;
  readonly reducer_version: string;
  readonly members: readonly TrialSetMemberView[];
  readonly variations: readonly TrialMetricVariationView[];
  readonly environment_drift_fields: readonly string[];
  readonly design_status: "RETROSPECTIVE";
  readonly inference: "DESCRIPTIVE_ONLY";
  readonly request_population_policy: "separate_per_run_v1";
}

export interface RejectedTrialSet extends ApiObject {
  readonly entry: string;
  readonly status: "REJECTED";
  readonly code:
    | "VERIFICATION_FAILED"
    | "UNSAFE_ENTRY"
    | "MEMBER_UNAVAILABLE"
    | "DECLARATION_UNAVAILABLE"
    | "DUPLICATE_TRIAL_SET_ID";
  readonly message: string;
}

export interface TrialSetPageResponse extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly generated_at: string;
  readonly trial_sets: readonly TrialSetSummary[];
  readonly rejected: readonly RejectedTrialSet[];
  readonly page: PageView;
}

export interface TrialSetIndex extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly generated_at: string;
  readonly trial_sets: readonly TrialSetSummary[];
  readonly rejected: readonly RejectedTrialSet[];
}

export type ControlledComparisonResultStatus =
  | "COMPARABLE"
  | "INCOMPARABLE"
  | "NO_RESULT"
  | "WITHHELD";

export type ControlledComparisonCheckId =
  | "LOCAL_PLAN_ORDER"
  | "EXACT_ARM_MEMBERSHIP"
  | "OBSERVED_SCHEDULE"
  | "DECLARED_FINGERPRINT_DIFFERENCE"
  | "COMPLETE_EQUAL_OBSERVED_ENVIRONMENT"
  | "OUTCOME_COVERAGE_AND_SEMANTICS";

export interface ControlledComparisonSummary extends ApiObject {
  readonly comparison_plan_id: string;
  readonly comparison_plan_digest: string;
  readonly experiment_id: string;
  readonly title: string;
  readonly created_at: string;
  readonly treatment_path: "traffic.concurrency";
  readonly baseline_value: number;
  readonly candidate_value: number;
  readonly planned_repetitions_per_arm: number;
  readonly baseline_trial_set_id: string;
  readonly candidate_trial_set_id: string;
  readonly design_status: "PREDECLARED";
  readonly predeclaration_assurance: "OPERATOR_ATTESTED";
  readonly primary_outcome_key: string;
  readonly primary_outcome_label: string;
  readonly primary_outcome_unit: string;
  readonly result_status: ControlledComparisonResultStatus;
  readonly comparison_result_id: string | null;
  readonly comparison_result_digest: string | null;
  readonly estimate: string | null;
  readonly estimate_display_value: string | null;
}

export interface RejectedControlledComparison extends ApiObject {
  readonly entry: string;
  readonly status: "REJECTED";
  readonly code:
    | "UNSAFE_ENTRY"
    | "DECLARATION_UNAVAILABLE"
    | "PLAN_VERIFICATION_FAILED"
    | "RESULT_VERIFICATION_FAILED"
    | "DUPLICATE_RESULT_FOR_PLAN"
    | "ORPHAN_RESULT";
  readonly failure_fingerprint: string;
  readonly message: string;
}

export interface ControlledComparisonPageResponse extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly generated_at: string;
  readonly comparisons: readonly ControlledComparisonSummary[];
  readonly rejected: readonly RejectedControlledComparison[];
  readonly page: PageView;
}

export interface ControlledComparisonIndex extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly generated_at: string;
  readonly comparisons: readonly ControlledComparisonSummary[];
  readonly rejected: readonly RejectedControlledComparison[];
}

export interface ControlledComparisonIndependentVariable extends ApiObject {
  readonly value_type: "integer";
  readonly path: "traffic.concurrency";
  readonly baseline_value: number;
  readonly candidate_value: number;
}

export interface ControlledComparisonArmPlanView extends ApiObject {
  readonly arm: "BASELINE" | "CANDIDATE";
  readonly planned_trial_set_id: string;
  readonly source_spec_digest: string;
  readonly expected_execution_fingerprint: string;
  readonly run_ids: readonly string[];
}

export interface ControlledComparisonScheduleSlot extends ApiObject {
  readonly sequence_index: number;
  readonly block_index: number;
  readonly within_block_position: 0 | 1;
  readonly arm: "BASELINE" | "CANDIDATE";
  readonly repetition_index: number;
  readonly run_id: string;
}

export interface ControlledComparisonOutcomeSelector extends ApiObject {
  readonly metric: string;
  readonly aggregation: string;
  readonly definition_id: string;
  readonly unit: string;
  readonly population: string;
  readonly quantile_method: string | null;
  readonly rounding_policy: string;
}

export interface ControlledComparisonPlanView extends ApiObject {
  readonly schema_version: "inferdrome.controlled-comparison-plan.v1";
  readonly comparison_plan_id: string;
  readonly experiment_id: string;
  readonly title: string;
  readonly hypothesis: string;
  readonly created_at: string;
  readonly design_status: "PREDECLARED";
  readonly arm_membership_policy: "exact_ordered_run_ids_v1";
  readonly schedule_policy: "predeclared_permuted_pairs_v1";
  readonly schedule_seed: string;
  readonly statistical_unit: "run";
  readonly request_population_policy: "separate_per_run_v1";
  readonly weighting: "equal_per_run";
  readonly planned_repetitions_per_arm: number;
  readonly independent_variable: ControlledComparisonIndependentVariable;
  readonly baseline_arm: ControlledComparisonArmPlanView;
  readonly candidate_arm: ControlledComparisonArmPlanView;
  readonly ordered_schedule: readonly ControlledComparisonScheduleSlot[];
  readonly primary_outcome: ControlledComparisonOutcomeSelector;
  readonly metric_definitions_digest: string;
  readonly reducer_version: string;
  readonly estimator: "paired_run_mean_difference_v1";
  readonly contrast_direction: "candidate_minus_baseline";
  readonly uncertainty_method: "none_v1";
  readonly missing_data_policy: "incomparable_if_any_outcome_missing_v1";
  readonly exclusion_policy: "no_post_assignment_exclusions_v1";
  readonly environment_policy: "complete_and_equal_observed_environment_v1";
  readonly environment_control_scope: "OBSERVED_V1_ALLOWLIST_ONLY";
  readonly predeclaration_anchor: "operator_retained_plan_digest_required_v1";
  readonly predeclaration_assurance: "OPERATOR_ATTESTED";
}

export interface ControlledComparisonControlCheck extends ApiObject {
  readonly check: ControlledComparisonCheckId;
  readonly status: "SATISFIED" | "UNSATISFIED";
}

export interface ControlledComparisonRunValue extends ApiObject {
  readonly repetition_index: number;
  readonly run_id: string;
  readonly value: string;
  readonly sample_count: number;
}

export interface ControlledComparisonPairedDifference extends ApiObject {
  readonly block_index: number;
  readonly baseline_run_id: string;
  readonly candidate_run_id: string;
  readonly candidate_minus_baseline: string;
}

export interface ControlledComparisonOutcomeEstimate extends ApiObject {
  readonly selector: ControlledComparisonOutcomeSelector;
  readonly unit: string;
  readonly baseline_values: readonly ControlledComparisonRunValue[];
  readonly candidate_values: readonly ControlledComparisonRunValue[];
  readonly paired_differences: readonly ControlledComparisonPairedDifference[];
  readonly baseline_mean: string;
  readonly candidate_mean: string;
  readonly estimate: string;
}

export interface ControlledComparisonResult extends ApiObject {
  readonly schema_version: "inferdrome.controlled-comparison-result.v1";
  readonly comparison_result_id: string;
  readonly comparison_plan_id: string;
  readonly comparison_plan_digest: string;
  readonly created_at: string;
  readonly baseline_trial_set: {
    readonly trial_set_id: string;
    readonly trial_set_digest: string;
  };
  readonly candidate_trial_set: {
    readonly trial_set_id: string;
    readonly trial_set_digest: string;
  };
  readonly status: "COMPARABLE" | "INCOMPARABLE";
  readonly inference_scope: "POINT_ESTIMATE_ONLY";
  readonly predeclaration_assurance: "OPERATOR_ATTESTED";
  readonly environment_control_scope: "OBSERVED_V1_ALLOWLIST_ONLY";
  readonly statistical_unit: "run";
  readonly weighting: "equal_per_run";
  readonly estimator: "paired_run_mean_difference_v1";
  readonly contrast_direction: "candidate_minus_baseline";
  readonly uncertainty_method: "none_v1";
  readonly control_checks: readonly ControlledComparisonControlCheck[];
  readonly unsatisfied_controls: readonly ControlledComparisonCheckId[];
  readonly outcomes: readonly ControlledComparisonOutcomeEstimate[];
}

export interface ControlledComparisonDetail extends ApiObject {
  readonly projection_version: "inferdrome.dashboard.v1";
  readonly summary: ControlledComparisonSummary;
  readonly plan: ControlledComparisonPlanView;
  readonly result: ControlledComparisonResult | null;
  readonly baseline_trial_set: TrialSetSummary | null;
  readonly candidate_trial_set: TrialSetSummary | null;
  readonly result_issue:
    | "RESULT_VERIFICATION_FAILED"
    | "DUPLICATE_RESULT_FOR_PLAN"
    | null;
}
