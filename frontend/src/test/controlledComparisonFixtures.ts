export const comparisonPlanId = "comparison-plan-11111111111111111111111111111111";
export const comparisonResultId = "comparison-result-11111111111111111111111111111111";
export const baselineTrialSetId = "trial-set-11111111111111111111111111111111";
export const candidateTrialSetId = "trial-set-22222222222222222222222222222222";
export const baselineRunIds = [
  "run-11111111111111111111111111111111",
  "run-22222222222222222222222222222222",
] as const;
export const candidateRunIds = [
  "run-33333333333333333333333333333333",
  "run-44444444444444444444444444444444",
] as const;

const digest = (character: string) => `sha256:${character.repeat(64)}`;

export const baselineTrialSetSummary = {
  trial_set_id: baselineTrialSetId,
  experiment_id: "exp-controlled-dashboard",
  title: "Baseline concurrency trials",
  created_at: "2026-08-07T12:10:00Z",
  member_count: 2,
  earliest_run_at: "2026-08-07T12:01:00Z",
  latest_run_at: "2026-08-07T12:04:00Z",
  model: "inferdrome/fake-model",
  execution_fingerprint: digest("b"),
  trial_set_digest: digest("c"),
  evidence_eligibilities: ["SYNTHETIC_ONLY"],
  environment_status: "CONSISTENT",
};

export const candidateTrialSetSummary = {
  ...baselineTrialSetSummary,
  trial_set_id: candidateTrialSetId,
  title: "Candidate concurrency trials",
  execution_fingerprint: digest("d"),
  trial_set_digest: digest("e"),
};

export const controlledPlanView = {
  schema_version: "inferdrome.controlled-comparison-plan.v1",
  comparison_plan_id: comparisonPlanId,
  experiment_id: "exp-controlled-dashboard",
  title: "Concurrency 1 versus 2",
  hypothesis: "Concurrency two may change the declared run-level TTFT outcome.",
  created_at: "2026-08-07T12:00:00Z",
  design_status: "PREDECLARED",
  arm_membership_policy: "exact_ordered_run_ids_v1",
  schedule_policy: "predeclared_permuted_pairs_v1",
  schedule_seed: "1".repeat(64),
  statistical_unit: "run",
  request_population_policy: "separate_per_run_v1",
  weighting: "equal_per_run",
  planned_repetitions_per_arm: 2,
  independent_variable: {
    value_type: "integer",
    path: "traffic.concurrency",
    baseline_value: 1,
    candidate_value: 2,
  },
  baseline_arm: {
    arm: "BASELINE",
    planned_trial_set_id: baselineTrialSetId,
    source_spec_digest: digest("f"),
    expected_execution_fingerprint: baselineTrialSetSummary.execution_fingerprint,
    run_ids: baselineRunIds,
  },
  candidate_arm: {
    arm: "CANDIDATE",
    planned_trial_set_id: candidateTrialSetId,
    source_spec_digest: digest("0"),
    expected_execution_fingerprint: candidateTrialSetSummary.execution_fingerprint,
    run_ids: candidateRunIds,
  },
  ordered_schedule: [
    { sequence_index: 0, block_index: 0, within_block_position: 0, arm: "BASELINE", repetition_index: 0, run_id: baselineRunIds[0] },
    { sequence_index: 1, block_index: 0, within_block_position: 1, arm: "CANDIDATE", repetition_index: 0, run_id: candidateRunIds[0] },
    { sequence_index: 2, block_index: 1, within_block_position: 0, arm: "CANDIDATE", repetition_index: 1, run_id: candidateRunIds[1] },
    { sequence_index: 3, block_index: 1, within_block_position: 1, arm: "BASELINE", repetition_index: 1, run_id: baselineRunIds[1] },
  ],
  primary_outcome: {
    metric: "ttft_ns",
    aggregation: "p50",
    definition_id: "vllm_first_choices_event_v0_26",
    unit: "ns",
    population: "successful_measured_requests_with_observed_ttft",
    quantile_method: "nearest_rank_v1",
    rounding_policy: "decimal_half_even_6_v1",
  },
  metric_definitions_digest: digest("1"),
  reducer_version: "1.0.0",
  estimator: "paired_run_mean_difference_v1",
  contrast_direction: "candidate_minus_baseline",
  uncertainty_method: "none_v1",
  missing_data_policy: "incomparable_if_any_outcome_missing_v1",
  exclusion_policy: "no_post_assignment_exclusions_v1",
  environment_policy: "complete_and_equal_observed_environment_v1",
  environment_control_scope: "OBSERVED_V1_ALLOWLIST_ONLY",
  predeclaration_anchor: "operator_retained_plan_digest_required_v1",
  predeclaration_assurance: "OPERATOR_ATTESTED",
};

export const comparableControlledSummary = {
  comparison_plan_id: comparisonPlanId,
  comparison_plan_digest: digest("2"),
  experiment_id: controlledPlanView.experiment_id,
  title: controlledPlanView.title,
  created_at: controlledPlanView.created_at,
  treatment_path: "traffic.concurrency",
  baseline_value: 1,
  candidate_value: 2,
  planned_repetitions_per_arm: 2,
  baseline_trial_set_id: baselineTrialSetId,
  candidate_trial_set_id: candidateTrialSetId,
  design_status: "PREDECLARED",
  predeclaration_assurance: "OPERATOR_ATTESTED",
  primary_outcome_key: "ttft_ns:p50",
  primary_outcome_label: "Time to first token p50",
  primary_outcome_unit: "ns",
  result_status: "COMPARABLE",
  comparison_result_id: comparisonResultId,
  comparison_result_digest: digest("3"),
  estimate: "1000000",
  estimate_display_value: "1 ms",
};

const satisfiedChecks = [
  "LOCAL_PLAN_ORDER",
  "EXACT_ARM_MEMBERSHIP",
  "OBSERVED_SCHEDULE",
  "DECLARED_FINGERPRINT_DIFFERENCE",
  "COMPLETE_EQUAL_OBSERVED_ENVIRONMENT",
  "OUTCOME_COVERAGE_AND_SEMANTICS",
].map((check) => ({ check, status: "SATISFIED" }));

const controlledOutcome = {
  selector: controlledPlanView.primary_outcome,
  unit: "ns",
  baseline_values: [
    { repetition_index: 0, run_id: baselineRunIds[0], value: "10000000", sample_count: 20 },
    { repetition_index: 1, run_id: baselineRunIds[1], value: "12000000", sample_count: 20 },
  ],
  candidate_values: [
    { repetition_index: 0, run_id: candidateRunIds[0], value: "11000000", sample_count: 20 },
    { repetition_index: 1, run_id: candidateRunIds[1], value: "13000000", sample_count: 20 },
  ],
  paired_differences: [
    { block_index: 0, baseline_run_id: baselineRunIds[0], candidate_run_id: candidateRunIds[0], candidate_minus_baseline: "1000000" },
    { block_index: 1, baseline_run_id: baselineRunIds[1], candidate_run_id: candidateRunIds[1], candidate_minus_baseline: "1000000" },
  ],
  baseline_mean: "11000000",
  candidate_mean: "12000000",
  estimate: "1000000",
};

export const comparableControlledResult = {
  schema_version: "inferdrome.controlled-comparison-result.v1",
  comparison_result_id: comparisonResultId,
  comparison_plan_id: comparisonPlanId,
  comparison_plan_digest: comparableControlledSummary.comparison_plan_digest,
  created_at: "2026-08-07T12:12:00Z",
  baseline_trial_set: {
    trial_set_id: baselineTrialSetId,
    trial_set_digest: baselineTrialSetSummary.trial_set_digest,
  },
  candidate_trial_set: {
    trial_set_id: candidateTrialSetId,
    trial_set_digest: candidateTrialSetSummary.trial_set_digest,
  },
  status: "COMPARABLE",
  inference_scope: "POINT_ESTIMATE_ONLY",
  predeclaration_assurance: "OPERATOR_ATTESTED",
  environment_control_scope: "OBSERVED_V1_ALLOWLIST_ONLY",
  statistical_unit: "run",
  weighting: "equal_per_run",
  estimator: "paired_run_mean_difference_v1",
  contrast_direction: "candidate_minus_baseline",
  uncertainty_method: "none_v1",
  control_checks: satisfiedChecks,
  unsatisfied_controls: [],
  outcomes: [controlledOutcome],
};

export const publishedExecutionProgress = {
  status: "EVIDENCE_COMPLETE",
  result_published: true,
  completed_run_count: controlledPlanView.ordered_schedule.length,
  planned_run_count: controlledPlanView.ordered_schedule.length,
  next_sequence_index: null,
  exact_schedule_prefix: true,
  issue: null,
  slots: controlledPlanView.ordered_schedule.map((slot) => ({
    sequence_index: slot.sequence_index,
    run_id: slot.run_id,
    state: "COMPLETE",
    verified_bundle: true,
  })),
};

export const comparableControlledDetail = {
  projection_version: "inferdrome.dashboard.v1",
  summary: comparableControlledSummary,
  plan: controlledPlanView,
  execution: publishedExecutionProgress,
  result: comparableControlledResult,
  baseline_trial_set: baselineTrialSetSummary,
  candidate_trial_set: candidateTrialSetSummary,
  result_issue: null,
};

export const incomparableControlledSummary = {
  ...comparableControlledSummary,
  result_status: "INCOMPARABLE",
  estimate: null,
  estimate_display_value: null,
};

export const incomparableIndexSummary = {
  ...incomparableControlledSummary,
  comparison_plan_id: "comparison-plan-22222222222222222222222222222222",
  comparison_result_id: "comparison-result-22222222222222222222222222222222",
  title: "Concurrency control mismatch",
};

export const noResultIndexSummary = {
  ...comparableControlledSummary,
  comparison_plan_id: "comparison-plan-33333333333333333333333333333333",
  title: "Concurrency plan awaiting evidence",
  result_status: "NO_RESULT",
  comparison_result_id: null,
  comparison_result_digest: null,
  estimate: null,
  estimate_display_value: null,
};

export const withheldIndexSummary = {
  ...noResultIndexSummary,
  comparison_plan_id: "comparison-plan-44444444444444444444444444444444",
  title: "Concurrency result withheld",
  result_status: "WITHHELD",
};

export const incomparableControlledResult = {
  ...comparableControlledResult,
  status: "INCOMPARABLE",
  control_checks: satisfiedChecks.map((check) => (
    check.check === "COMPLETE_EQUAL_OBSERVED_ENVIRONMENT"
      ? { ...check, status: "UNSATISFIED" }
      : check
  )),
  unsatisfied_controls: ["COMPLETE_EQUAL_OBSERVED_ENVIRONMENT"],
  outcomes: [],
};

export const incomparableControlledDetail = {
  projection_version: "inferdrome.dashboard.v1",
  summary: incomparableControlledSummary,
  plan: controlledPlanView,
  execution: publishedExecutionProgress,
  result: incomparableControlledResult,
  baseline_trial_set: null,
  candidate_trial_set: null,
  result_issue: null,
};

export const controlledComparisonIndex = {
  projection_version: "inferdrome.dashboard.v1",
  generated_at: "2026-08-07T12:13:00Z",
  comparisons: [
    comparableControlledSummary,
    incomparableIndexSummary,
    noResultIndexSummary,
    withheldIndexSummary,
  ],
  rejected: [],
  page: {
    limit: 100,
    returned: 4,
    total: 4,
    has_more: false,
    next_cursor: null,
  },
};
