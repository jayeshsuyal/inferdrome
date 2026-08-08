import type {
  Comparison,
  ControlledComparisonArmPlanView,
  ControlledComparisonCheckId,
  ControlledComparisonDetail,
  ControlledComparisonIndex,
  ControlledComparisonOutcomeEstimate,
  ControlledComparisonOutcomeSelector,
  ControlledComparisonPageResponse,
  ControlledComparisonPlanView,
  ControlledComparisonResult,
  ControlledComparisonScheduleSlot,
  ControlledComparisonSummary,
  MetricView,
  RejectedControlledComparison,
  RejectedRun,
  RejectedTrialSet,
  RunDetail,
  RunIndex,
  RunSummary,
  RunsPageResponse,
  TrialMetricVariationView,
  TrialRunPointView,
  TrialSetDetail,
  TrialSetIndex,
  TrialSetMemberView,
  TrialSetPageResponse,
  TrialSetSummary,
} from "./types";

const API_ROOT = "/api/v1";
const RUN_PAGE_LIMIT = 200;
const MAX_RUN_PAGES = 5;
const TRIAL_SET_PAGE_LIMIT = 100;
const MAX_TRIAL_SET_PAGES = 2;
const CONTROLLED_COMPARISON_PAGE_LIMIT = 100;
const MAX_CONTROLLED_COMPARISON_PAGES = 4;

const CONTROL_CHECKS: readonly ControlledComparisonCheckId[] = [
  "LOCAL_PLAN_ORDER",
  "EXACT_ARM_MEMBERSHIP",
  "OBSERVED_SCHEDULE",
  "DECLARED_FINGERPRINT_DIFFERENCE",
  "COMPLETE_EQUAL_OBSERVED_ENVIRONMENT",
  "OUTCOME_COVERAGE_AND_SEMANTICS",
];

const FROZEN_OUTCOME_SEMANTICS: Readonly<Record<string, {
  readonly aggregations: readonly string[];
  readonly definitionId: string;
  readonly population: string;
  readonly quantileMethod: "nearest_rank_v1" | null;
  readonly roundingPolicy: "none" | "decimal_half_even_6_v1";
  readonly unit: "count" | "ratio" | "ns" | "requests/s" | "tokens/s";
}>> = {
  measured_request_count: {
    aggregations: ["count"],
    definitionId: "measured_request_count_v1",
    population: "all_measured_requests",
    quantileMethod: null,
    roundingPolicy: "none",
    unit: "count",
  },
  successful_request_count: {
    aggregations: ["count"],
    definitionId: "successful_request_count_v1",
    population: "successful_measured_requests",
    quantileMethod: null,
    roundingPolicy: "none",
    unit: "count",
  },
  failed_request_count: {
    aggregations: ["count"],
    definitionId: "failed_request_count_v1",
    population: "failed_measured_requests",
    quantileMethod: null,
    roundingPolicy: "none",
    unit: "count",
  },
  error_rate: {
    aggregations: ["ratio"],
    definitionId: "measured_failure_ratio_v1",
    population: "all_measured_requests",
    quantileMethod: null,
    roundingPolicy: "decimal_half_even_6_v1",
    unit: "ratio",
  },
  ttft_ns: {
    aggregations: ["mean", "p50", "p95", "p99"],
    definitionId: "vllm_first_choices_event_v0_26",
    population: "successful_measured_requests_with_observed_ttft",
    quantileMethod: "nearest_rank_v1",
    roundingPolicy: "decimal_half_even_6_v1",
    unit: "ns",
  },
  last_choices_event_span_ns: {
    aggregations: ["mean", "p50", "p95", "p99"],
    definitionId: "last_choices_event_span_v1",
    population: "successful_measured_requests_with_observed_ttft",
    quantileMethod: "nearest_rank_v1",
    roundingPolicy: "decimal_half_even_6_v1",
    unit: "ns",
  },
  attempted_request_throughput_per_s: {
    aggregations: ["rate"],
    definitionId: "attempted_measured_requests_per_window_second_v1",
    population: "all_measured_requests",
    quantileMethod: null,
    roundingPolicy: "decimal_half_even_6_v1",
    unit: "requests/s",
  },
  successful_request_throughput_per_s: {
    aggregations: ["rate"],
    definitionId: "successful_measured_requests_per_window_second_v1",
    population: "successful_measured_requests",
    quantileMethod: null,
    roundingPolicy: "decimal_half_even_6_v1",
    unit: "requests/s",
  },
  output_token_throughput_per_s: {
    aggregations: ["rate"],
    definitionId: "successful_output_tokens_per_window_second_v1",
    population: "successful_measured_requests",
    quantileMethod: null,
    roundingPolicy: "decimal_half_even_6_v1",
    unit: "tokens/s",
  },
};

export class ApiError extends Error {
  readonly status: number;
  readonly requestId: string | null;

  constructor(message: string, status: number, requestId: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.requestId = requestId;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function protocolError(message: string): ApiError {
  return new ApiError(`Inferdrome returned an invalid dashboard response: ${message}`, 502);
}

async function readError(response: Response): Promise<string> {
  const fallback = `Request failed with status ${response.status}`;
  try {
    const payload: unknown = await response.json();
    if (!isRecord(payload)) return fallback;
    const detail = payload.detail;
    const message = payload.message;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (typeof message === "string" && message.trim()) return message;
  } catch {
    // A non-JSON failure still gets a bounded, useful message.
  }
  return fallback;
}

async function fetchJson(path: string, signal?: AbortSignal): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${API_ROOT}${path}`, {
      method: "GET",
      headers: { Accept: "application/json" },
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(
      "The local Inferdrome dashboard service is unavailable. Start it and try again.",
      0,
    );
  }

  if (!response.ok) {
    throw new ApiError(
      await readError(response),
      response.status,
      response.headers.get("x-request-id"),
    );
  }

  try {
    return await response.json();
  } catch {
    throw protocolError("the body is not valid JSON");
  }
}

function parseRuns(payload: unknown): RunsPageResponse {
  if (!isRecord(payload)) throw protocolError("the run index is not an object");
  if (payload.projection_version !== "inferdrome.dashboard.v1") {
    throw protocolError("the run index projection version is unsupported");
  }
  if (!Array.isArray(payload.runs)) throw protocolError("runs must be an array");
  if (!Array.isArray(payload.rejected)) throw protocolError("rejected must be an array");
  if (typeof payload.generated_at !== "string") {
    throw protocolError("generated_at must be a string");
  }
  if (!isRecord(payload.page)) throw protocolError("page metadata is missing");
  const { has_more: hasMore, limit, next_cursor: nextCursor, returned, total } = payload.page;
  if (
    typeof limit !== "number" ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > RUN_PAGE_LIMIT ||
    typeof returned !== "number" ||
    !Number.isInteger(returned) ||
    returned !== payload.runs.length + payload.rejected.length ||
    typeof total !== "number" ||
    !Number.isInteger(total) ||
    total < returned ||
    typeof hasMore !== "boolean" ||
    (nextCursor !== null && (
      typeof nextCursor !== "string" ||
      nextCursor.length === 0 ||
      nextCursor.length > 128
    )) ||
    hasMore !== (nextCursor !== null)
  ) {
    throw protocolError("page metadata is invalid");
  }
  return payload as unknown as RunsPageResponse;
}

function parseRunDetail(payload: unknown): RunDetail {
  if (!isRecord(payload) || payload.projection_version !== "inferdrome.dashboard.v1") {
    throw protocolError("the run detail projection version is unsupported");
  }
  if (!isRecord(payload.summary) || typeof payload.summary.run_id !== "string") {
    throw protocolError("run detail is missing summary.run_id");
  }
  if (!isRecord(payload.verification) || !isRecord(payload.execution)) {
    throw protocolError("run detail is missing verification or execution");
  }
  for (const key of ["measurements", "distributions", "context", "environment", "artifacts", "unavailable"] as const) {
    if (!Array.isArray(payload[key])) throw protocolError(`${key} must be an array`);
  }
  return payload as unknown as RunDetail;
}

function parseComparison(payload: unknown): Comparison {
  if (!isRecord(payload)) throw protocolError("comparison is not an object");
  if (payload.projection_version !== "inferdrome.dashboard.v1") {
    throw protocolError("the comparison projection version is unsupported");
  }
  if (
    payload.status !== "COMPARABLE" &&
    payload.status !== "COMPARABLE_WITH_CONTEXT_CHANGES" &&
    payload.status !== "INCOMPARABLE"
  ) {
    throw protocolError("comparison status is unknown");
  }
  if (typeof payload.baseline_run_id !== "string" || typeof payload.candidate_run_id !== "string") {
    throw protocolError("comparison is missing run identities");
  }
  for (const key of ["reasons", "metric_deltas", "context_changes"] as const) {
    if (!Array.isArray(payload[key])) throw protocolError(`${key} must be an array`);
  }
  if (payload.directionality !== "NEUTRAL") throw protocolError("comparison directionality must be neutral");
  return payload as unknown as Comparison;
}

function requiredString(
  object: Record<string, unknown>,
  key: string,
  context: string,
): string {
  const value = object[key];
  if (typeof value !== "string") throw protocolError(`${context}.${key} must be a string`);
  return value;
}

function nullableString(
  object: Record<string, unknown>,
  key: string,
  context: string,
): string | null {
  const value = object[key];
  if (value !== null && typeof value !== "string") {
    throw protocolError(`${context}.${key} must be a string or null`);
  }
  return value;
}

function requiredInteger(
  object: Record<string, unknown>,
  key: string,
  context: string,
  minimum = 0,
): number {
  const value = object[key];
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum) {
    throw protocolError(`${context}.${key} must be an integer of at least ${minimum}`);
  }
  return value;
}

function nullableInteger(
  object: Record<string, unknown>,
  key: string,
  context: string,
): number | null {
  const value = object[key];
  if (value !== null && (typeof value !== "number" || !Number.isInteger(value) || value < 0)) {
    throw protocolError(`${context}.${key} must be a non-negative integer or null`);
  }
  return value;
}

function stringArray(
  object: Record<string, unknown>,
  key: string,
  context: string,
): readonly string[] {
  const value = object[key];
  if (!Array.isArray(value) || value.some((entry) => typeof entry !== "string")) {
    throw protocolError(`${context}.${key} must be an array of strings`);
  }
  return value;
}

function parseMetric(value: unknown, context: string): MetricView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  for (const key of [
    "key",
    "metric",
    "aggregation",
    "label",
    "value",
    "display_value",
    "unit",
    "population",
    "definition_id",
    "rounding_policy",
  ] as const) {
    requiredString(value, key, context);
  }
  requiredInteger(value, "sample_count", context);
  nullableString(value, "quantile_method", context);
  return value as unknown as MetricView;
}

function parseRunSummary(value: unknown, context: string): RunSummary {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  for (const key of [
    "run_id",
    "experiment_id",
    "title",
    "model",
    "producer_name",
    "producer_version",
    "adapter_name",
    "adapter_version",
    "execution_mode",
    "started_at",
    "ended_at",
    "evidence_eligibility",
    "environment_completeness",
    "replayability",
    "bundle_digest",
    "error_rate",
    "output_token_throughput_per_s",
  ] as const) {
    requiredString(value, key, context);
  }
  for (const key of [
    "duration_ns",
    "measured_requests",
    "successful_requests",
    "failed_requests",
  ] as const) {
    requiredInteger(value, key, context);
  }
  nullableInteger(value, "ttft_p50_ns", context);
  nullableInteger(value, "ttft_p95_ns", context);
  if (value.integrity_status !== "VALID") {
    throw protocolError(`${context}.integrity_status must be VALID`);
  }
  if (!Array.isArray(value.headline_metrics)) {
    throw protocolError(`${context}.headline_metrics must be an array`);
  }
  value.headline_metrics.forEach((metric, index) => parseMetric(metric, `${context}.headline_metrics[${index}]`));
  return value as unknown as RunSummary;
}

function parseTrialSetSummary(value: unknown, context: string): TrialSetSummary {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  for (const key of [
    "trial_set_id",
    "experiment_id",
    "title",
    "created_at",
    "earliest_run_at",
    "latest_run_at",
    "model",
    "execution_fingerprint",
    "trial_set_digest",
  ] as const) {
    requiredString(value, key, context);
  }
  if (!/^trial-set-[0-9a-f]{32}$/.test(value.trial_set_id as string)) {
    throw protocolError(`${context}.trial_set_id is invalid`);
  }
  const memberCount = requiredInteger(value, "member_count", context, 2);
  if (memberCount > 100) throw protocolError(`${context}.member_count exceeds 100`);
  const evidenceEligibilities = stringArray(value, "evidence_eligibilities", context);
  if (evidenceEligibilities.length === 0 || new Set(evidenceEligibilities).size !== evidenceEligibilities.length) {
    throw protocolError(`${context}.evidence_eligibilities must be non-empty and unique`);
  }
  if (value.environment_status !== "CONSISTENT" && value.environment_status !== "DRIFT_DETECTED") {
    throw protocolError(`${context}.environment_status is unknown`);
  }
  return value as unknown as TrialSetSummary;
}

function parseRejectedTrialSet(value: unknown, context: string): RejectedTrialSet {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  requiredString(value, "entry", context);
  requiredString(value, "message", context);
  if (value.status !== "REJECTED") throw protocolError(`${context}.status must be REJECTED`);
  if (![
    "VERIFICATION_FAILED",
    "UNSAFE_ENTRY",
    "MEMBER_UNAVAILABLE",
    "DECLARATION_UNAVAILABLE",
    "DUPLICATE_TRIAL_SET_ID",
  ].includes(value.code as string)) {
    throw protocolError(`${context}.code is unknown`);
  }
  return value as unknown as RejectedTrialSet;
}

function parseTrialSetPage(payload: unknown): TrialSetPageResponse {
  if (!isRecord(payload)) throw protocolError("the trial-set index is not an object");
  if (payload.projection_version !== "inferdrome.dashboard.v1") {
    throw protocolError("the trial-set index projection version is unsupported");
  }
  if (typeof payload.generated_at !== "string") {
    throw protocolError("trial-set generated_at must be a string");
  }
  if (!Array.isArray(payload.trial_sets)) throw protocolError("trial_sets must be an array");
  if (!Array.isArray(payload.rejected)) throw protocolError("trial-set rejected must be an array");
  payload.trial_sets.forEach((summary, index) => parseTrialSetSummary(summary, `trial_sets[${index}]`));
  payload.rejected.forEach((entry, index) => parseRejectedTrialSet(entry, `rejected[${index}]`));
  if (!isRecord(payload.page)) throw protocolError("trial-set page metadata is missing");
  const { has_more: hasMore, limit, next_cursor: nextCursor, returned, total } = payload.page;
  if (
    typeof limit !== "number" ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > 200 ||
    typeof returned !== "number" ||
    !Number.isInteger(returned) ||
    returned !== payload.trial_sets.length + payload.rejected.length ||
    typeof total !== "number" ||
    !Number.isInteger(total) ||
    total < returned ||
    total > TRIAL_SET_PAGE_LIMIT * MAX_TRIAL_SET_PAGES ||
    typeof hasMore !== "boolean" ||
    (nextCursor !== null && (
      typeof nextCursor !== "string" ||
      nextCursor.length === 0 ||
      nextCursor.length > 128
    )) ||
    hasMore !== (nextCursor !== null)
  ) {
    throw protocolError("trial-set page metadata is invalid");
  }
  return payload as unknown as TrialSetPageResponse;
}

function parseTrialPoint(value: unknown, context: string): TrialRunPointView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  requiredInteger(value, "repetition_index", context);
  requiredString(value, "run_id", context);
  const rawValue = nullableString(value, "value", context);
  const displayValue = nullableString(value, "display_value", context);
  const sampleCount = nullableInteger(value, "sample_count", context);
  if ((rawValue === null) !== (displayValue === null)) {
    throw protocolError(`${context} value availability is inconsistent`);
  }
  if ((rawValue === null) !== (sampleCount === null)) {
    throw protocolError(`${context} sample-count availability is inconsistent`);
  }
  return value as unknown as TrialRunPointView;
}

function parseTrialVariation(value: unknown, context: string): TrialMetricVariationView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  for (const key of ["key", "metric", "aggregation", "label", "unit"] as const) {
    requiredString(value, key, context);
  }
  const total = requiredInteger(value, "total_run_count", context, 2);
  if (total > 100) throw protocolError(`${context}.total_run_count exceeds 100`);
  const available = requiredInteger(value, "available_run_count", context);
  if (available > total) throw protocolError(`${context}.available_run_count exceeds total_run_count`);
  for (const [rawKey, displayKey] of [
    ["minimum", "minimum_display_value"],
    ["maximum", "maximum_display_value"],
    ["median", "median_display_value"],
    ["mean", "mean_display_value"],
    ["span", "span_display_value"],
    ["sample_standard_deviation", "sample_standard_deviation_display_value"],
  ] as const) {
    const raw = nullableString(value, rawKey, context);
    const display = nullableString(value, displayKey, context);
    if ((raw === null) !== (display === null)) {
      throw protocolError(`${context}.${rawKey} display availability is inconsistent`);
    }
  }
  if (!Array.isArray(value.points)) throw protocolError(`${context}.points must be an array`);
  value.points.forEach((point, index) => parseTrialPoint(point, `${context}.points[${index}]`));
  if (value.points.length !== total) throw protocolError(`${context}.points must represent every run`);
  if (value.points.filter((point) => isRecord(point) && point.value !== null).length !== available) {
    throw protocolError(`${context}.available_run_count disagrees with points`);
  }
  const expectedSummaryAvailability = available > 0;
  for (const key of ["minimum", "maximum", "median", "mean", "span"] as const) {
    if ((value[key] !== null) !== expectedSummaryAvailability) {
      throw protocolError(`${context}.${key} availability disagrees with available runs`);
    }
  }
  if ((value.sample_standard_deviation !== null) !== (available >= 2)) {
    throw protocolError(`${context}.sample_standard_deviation availability is invalid`);
  }
  if (value.population !== "run_level_measurements") {
    throw protocolError(`${context}.population is unsupported`);
  }
  if (value.weighting !== "equal_per_run") {
    throw protocolError(`${context}.weighting is unsupported`);
  }
  if (value.summary_method !== "per_run_scalar_sample_variation_v1") {
    throw protocolError(`${context}.summary_method is unsupported`);
  }
  return value as unknown as TrialMetricVariationView;
}

function parseTrialMember(value: unknown, context: string): TrialSetMemberView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  requiredInteger(value, "repetition_index", context);
  parseRunSummary(value.run, `${context}.run`);
  return value as unknown as TrialSetMemberView;
}

function parseTrialSetDetail(payload: unknown): TrialSetDetail {
  if (!isRecord(payload) || payload.projection_version !== "inferdrome.dashboard.v1") {
    throw protocolError("the trial-set detail projection version is unsupported");
  }
  const summary = parseTrialSetSummary(payload.summary, "trial-set summary");
  nullableString(payload, "hypothesis", "trial-set detail");
  requiredString(payload, "metric_definitions_digest", "trial-set detail");
  requiredString(payload, "reducer_version", "trial-set detail");
  if (payload.membership_policy !== "same_execution_fingerprint_v1") {
    throw protocolError("trial-set membership_policy is unsupported");
  }
  if (payload.design_status !== "RETROSPECTIVE") {
    throw protocolError("trial-set design_status must be RETROSPECTIVE");
  }
  if (payload.inference !== "DESCRIPTIVE_ONLY") {
    throw protocolError("trial-set inference must be DESCRIPTIVE_ONLY");
  }
  if (payload.request_population_policy !== "separate_per_run_v1") {
    throw protocolError("trial-set request_population_policy is unsupported");
  }
  if (!Array.isArray(payload.members)) throw protocolError("trial-set members must be an array");
  if (!Array.isArray(payload.variations)) throw protocolError("trial-set variations must be an array");
  const members = payload.members.map((member, index) => parseTrialMember(member, `members[${index}]`));
  const variations = payload.variations.map((variation, index) => parseTrialVariation(variation, `variations[${index}]`));
  const driftFields = stringArray(payload, "environment_drift_fields", "trial-set detail");
  if (summary.member_count !== members.length) {
    throw protocolError("trial-set summary member_count disagrees with members");
  }
  const memberRunIds = new Set<string>();
  for (const [index, member] of members.entries()) {
    if (member.repetition_index !== index) {
      throw protocolError("trial-set member repetition indices must be ordered and contiguous");
    }
    if (memberRunIds.has(member.run.run_id)) {
      throw protocolError("trial-set members repeat a run identity");
    }
    memberRunIds.add(member.run.run_id);
    if (member.run.experiment_id !== summary.experiment_id || member.run.model !== summary.model) {
      throw protocolError("trial-set member context disagrees with its summary");
    }
  }
  const variationKeys = new Set<string>();
  for (const variation of variations) {
    if (variationKeys.has(variation.key)) throw protocolError("trial-set variations repeat a metric key");
    variationKeys.add(variation.key);
    if (variation.total_run_count !== members.length) {
      throw protocolError("trial-set variation run count disagrees with members");
    }
    variation.points.forEach((point, index) => {
      const member = members[index];
      if (
        !member ||
        point.repetition_index !== member.repetition_index ||
        point.run_id !== member.run.run_id
      ) {
        throw protocolError("trial-set variation points do not align with members");
      }
    });
  }
  if (new Set(driftFields).size !== driftFields.length) {
    throw protocolError("trial-set environment drift fields contain a duplicate");
  }
  if ((summary.environment_status === "DRIFT_DETECTED") !== (driftFields.length > 0)) {
    throw protocolError("trial-set environment status disagrees with drift fields");
  }
  const projectedEligibilities = [...new Set(members.map((member) => member.run.evidence_eligibility))].sort();
  if (
    projectedEligibilities.length !== summary.evidence_eligibilities.length ||
    projectedEligibilities.some((value, index) => value !== [...summary.evidence_eligibilities].sort()[index])
  ) {
    throw protocolError("trial-set evidence eligibilities disagree with members");
  }
  return payload as unknown as TrialSetDetail;
}

function parseControlledSummary(
  value: unknown,
  context: string,
): ControlledComparisonSummary {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  for (const key of [
    "comparison_plan_id",
    "comparison_plan_digest",
    "experiment_id",
    "title",
    "created_at",
    "treatment_path",
    "baseline_trial_set_id",
    "candidate_trial_set_id",
    "design_status",
    "predeclaration_assurance",
    "primary_outcome_key",
    "primary_outcome_label",
    "primary_outcome_unit",
    "result_status",
  ] as const) {
    requiredString(value, key, context);
  }
  if (!/^comparison-plan-[0-9a-f]{32}$/.test(value.comparison_plan_id as string)) {
    throw protocolError(`${context}.comparison_plan_id is invalid`);
  }
  if (value.treatment_path !== "traffic.concurrency") {
    throw protocolError(`${context}.treatment_path is unsupported`);
  }
  const baselineValue = requiredInteger(value, "baseline_value", context, 1);
  const candidateValue = requiredInteger(value, "candidate_value", context, 1);
  if (baselineValue === candidateValue || baselineValue > 100_000 || candidateValue > 100_000) {
    throw protocolError(`${context} treatment values are invalid`);
  }
  const repetitions = requiredInteger(value, "planned_repetitions_per_arm", context, 2);
  if (repetitions > 100) throw protocolError(`${context} repetitions exceed 100`);
  if (value.design_status !== "PREDECLARED") {
    throw protocolError(`${context}.design_status must be PREDECLARED`);
  }
  if (value.predeclaration_assurance !== "OPERATOR_ATTESTED") {
    throw protocolError(`${context}.predeclaration_assurance must be OPERATOR_ATTESTED`);
  }
  if (!["COMPARABLE", "INCOMPARABLE", "NO_RESULT", "WITHHELD"].includes(value.result_status as string)) {
    throw protocolError(`${context}.result_status is unknown`);
  }
  const resultId = nullableString(value, "comparison_result_id", context);
  const resultDigest = nullableString(value, "comparison_result_digest", context);
  const estimate = nullableString(value, "estimate", context);
  const estimateDisplay = nullableString(value, "estimate_display_value", context);
  const hasResult = value.result_status === "COMPARABLE" || value.result_status === "INCOMPARABLE";
  if (hasResult !== (resultId !== null && resultDigest !== null)) {
    throw protocolError(`${context} result identity availability is inconsistent`);
  }
  if ((value.result_status === "COMPARABLE") !== (estimate !== null && estimateDisplay !== null)) {
    throw protocolError(`${context} estimate availability is inconsistent`);
  }
  return value as unknown as ControlledComparisonSummary;
}

function parseRejectedControlledComparison(
  value: unknown,
  context: string,
): RejectedControlledComparison {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  for (const key of ["entry", "failure_fingerprint", "message"] as const) {
    requiredString(value, key, context);
  }
  if (value.status !== "REJECTED") throw protocolError(`${context}.status must be REJECTED`);
  if (![
    "UNSAFE_ENTRY",
    "DECLARATION_UNAVAILABLE",
    "PLAN_VERIFICATION_FAILED",
    "RESULT_VERIFICATION_FAILED",
    "DUPLICATE_RESULT_FOR_PLAN",
    "ORPHAN_RESULT",
  ].includes(value.code as string)) {
    throw protocolError(`${context}.code is unknown`);
  }
  return value as unknown as RejectedControlledComparison;
}

function parseControlledComparisonPage(payload: unknown): ControlledComparisonPageResponse {
  if (!isRecord(payload)) throw protocolError("the controlled-comparison index is not an object");
  if (payload.projection_version !== "inferdrome.dashboard.v1") {
    throw protocolError("the controlled-comparison projection version is unsupported");
  }
  requiredString(payload, "generated_at", "controlled-comparison index");
  if (!Array.isArray(payload.comparisons)) {
    throw protocolError("controlled-comparison comparisons must be an array");
  }
  if (!Array.isArray(payload.rejected)) {
    throw protocolError("controlled-comparison rejected must be an array");
  }
  payload.comparisons.forEach((summary, index) => (
    parseControlledSummary(summary, `comparisons[${index}]`)
  ));
  payload.rejected.forEach((entry, index) => (
    parseRejectedControlledComparison(entry, `rejected[${index}]`)
  ));
  if (!isRecord(payload.page)) {
    throw protocolError("controlled-comparison page metadata is missing");
  }
  const { has_more: hasMore, limit, next_cursor: nextCursor, returned, total } = payload.page;
  if (
    typeof limit !== "number" ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > 200 ||
    typeof returned !== "number" ||
    !Number.isInteger(returned) ||
    returned !== payload.comparisons.length + payload.rejected.length ||
    typeof total !== "number" ||
    !Number.isInteger(total) ||
    total < returned ||
    total > CONTROLLED_COMPARISON_PAGE_LIMIT * MAX_CONTROLLED_COMPARISON_PAGES ||
    typeof hasMore !== "boolean" ||
    (nextCursor !== null && (
      typeof nextCursor !== "string" || nextCursor.length === 0 || nextCursor.length > 128
    )) ||
    hasMore !== (nextCursor !== null)
  ) {
    throw protocolError("controlled-comparison page metadata is invalid");
  }
  return payload as unknown as ControlledComparisonPageResponse;
}

function parseControlledOutcomeSelector(
  value: unknown,
  context: string,
): ControlledComparisonOutcomeSelector {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  for (const key of [
    "metric",
    "aggregation",
    "definition_id",
    "unit",
    "population",
    "rounding_policy",
  ] as const) {
    requiredString(value, key, context);
  }
  const quantileMethod = nullableString(value, "quantile_method", context);
  const metric = value.metric as string;
  const aggregation = value.aggregation as string;
  const semantics = FROZEN_OUTCOME_SEMANTICS[metric];
  const expectedQuantile = ["p50", "p95", "p99"].includes(aggregation)
    ? semantics?.quantileMethod
    : null;
  if (
    !semantics ||
    !semantics.aggregations.includes(aggregation) ||
    value.definition_id !== semantics.definitionId ||
    value.unit !== semantics.unit ||
    value.population !== semantics.population ||
    quantileMethod !== expectedQuantile ||
    value.rounding_policy !== semantics.roundingPolicy
  ) {
    throw protocolError(`${context} does not match frozen v1 outcome semantics`);
  }
  return value as unknown as ControlledComparisonOutcomeSelector;
}

function parseControlledArm(
  value: unknown,
  context: string,
  expectedArm: "BASELINE" | "CANDIDATE",
  repetitions: number,
): ControlledComparisonArmPlanView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  if (value.arm !== expectedArm) throw protocolError(`${context}.arm is invalid`);
  const allowedKeys = new Set([
    "arm",
    "planned_trial_set_id",
    "source_spec_digest",
    "expected_execution_fingerprint",
    "run_ids",
  ]);
  for (const key of [
    "planned_trial_set_id",
    "source_spec_digest",
    "expected_execution_fingerprint",
  ] as const) {
    requiredString(value, key, context);
  }
  if ("resolved_experiment" in value) {
    throw protocolError(`${context}.resolved_experiment is forbidden in dashboard projections`);
  }
  if (Object.keys(value).some((key) => !allowedKeys.has(key))) {
    throw protocolError(`${context} contains a field outside the dashboard arm allowlist`);
  }
  const runIds = stringArray(value, "run_ids", context);
  if (runIds.length !== repetitions || new Set(runIds).size !== runIds.length) {
    throw protocolError(`${context}.run_ids do not match the frozen repetitions`);
  }
  return value as unknown as ControlledComparisonArmPlanView;
}

function parseControlledScheduleSlot(
  value: unknown,
  context: string,
): ControlledComparisonScheduleSlot {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  requiredInteger(value, "sequence_index", context);
  requiredInteger(value, "block_index", context);
  requiredInteger(value, "repetition_index", context);
  requiredString(value, "run_id", context);
  if (value.within_block_position !== 0 && value.within_block_position !== 1) {
    throw protocolError(`${context}.within_block_position is invalid`);
  }
  if (value.arm !== "BASELINE" && value.arm !== "CANDIDATE") {
    throw protocolError(`${context}.arm is invalid`);
  }
  return value as unknown as ControlledComparisonScheduleSlot;
}

function parseControlledPlan(value: unknown, context: string): ControlledComparisonPlanView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  if ("external_predeclaration_proof" in value) {
    throw protocolError(`${context}.external_predeclaration_proof is a retired field`);
  }
  if (value.schema_version !== "inferdrome.controlled-comparison-plan.v1") {
    throw protocolError(`${context}.schema_version is unsupported`);
  }
  for (const key of [
    "comparison_plan_id",
    "experiment_id",
    "title",
    "hypothesis",
    "created_at",
    "schedule_seed",
    "metric_definitions_digest",
    "reducer_version",
  ] as const) {
    requiredString(value, key, context);
  }
  const repetitions = requiredInteger(value, "planned_repetitions_per_arm", context, 2);
  if (repetitions > 100) throw protocolError(`${context} repetitions exceed 100`);
  const fixedLiterals: readonly [string, string][] = [
    ["design_status", "PREDECLARED"],
    ["arm_membership_policy", "exact_ordered_run_ids_v1"],
    ["schedule_policy", "predeclared_permuted_pairs_v1"],
    ["statistical_unit", "run"],
    ["request_population_policy", "separate_per_run_v1"],
    ["weighting", "equal_per_run"],
    ["estimator", "paired_run_mean_difference_v1"],
    ["contrast_direction", "candidate_minus_baseline"],
    ["uncertainty_method", "none_v1"],
    ["missing_data_policy", "incomparable_if_any_outcome_missing_v1"],
    ["exclusion_policy", "no_post_assignment_exclusions_v1"],
    ["environment_policy", "complete_and_equal_observed_environment_v1"],
    ["environment_control_scope", "OBSERVED_V1_ALLOWLIST_ONLY"],
    ["predeclaration_anchor", "operator_retained_plan_digest_required_v1"],
    ["predeclaration_assurance", "OPERATOR_ATTESTED"],
  ];
  fixedLiterals.forEach(([key, expected]) => {
    if (value[key] !== expected) throw protocolError(`${context}.${key} is unsupported`);
  });
  if (!isRecord(value.independent_variable)) {
    throw protocolError(`${context}.independent_variable must be an object`);
  }
  const treatment = value.independent_variable;
  if (
    treatment.value_type !== "integer" ||
    treatment.path !== "traffic.concurrency"
  ) {
    throw protocolError(`${context}.independent_variable is unsupported`);
  }
  const baselineTreatment = requiredInteger(treatment, "baseline_value", `${context}.independent_variable`, 1);
  const candidateTreatment = requiredInteger(treatment, "candidate_value", `${context}.independent_variable`, 1);
  if (baselineTreatment === candidateTreatment) {
    throw protocolError(`${context}.independent_variable values must differ`);
  }
  const baseline = parseControlledArm(value.baseline_arm, `${context}.baseline_arm`, "BASELINE", repetitions);
  const candidate = parseControlledArm(value.candidate_arm, `${context}.candidate_arm`, "CANDIDATE", repetitions);
  const allRunIds = [...baseline.run_ids, ...candidate.run_ids];
  if (new Set(allRunIds).size !== allRunIds.length) {
    throw protocolError(`${context} arms must be disjoint`);
  }
  if (!Array.isArray(value.ordered_schedule) || value.ordered_schedule.length !== repetitions * 2) {
    throw protocolError(`${context}.ordered_schedule must cover every planned run`);
  }
  const schedule = value.ordered_schedule.map((slot, index) => (
    parseControlledScheduleSlot(slot, `${context}.ordered_schedule[${index}]`)
  ));
  const scheduledRunIds = new Set<string>();
  schedule.forEach((slot, index) => {
    const expectedRuns = slot.arm === "BASELINE" ? baseline.run_ids : candidate.run_ids;
    if (
      slot.sequence_index !== index ||
      slot.block_index !== slot.repetition_index ||
      slot.sequence_index !== slot.block_index * 2 + slot.within_block_position ||
      expectedRuns[slot.repetition_index] !== slot.run_id ||
      scheduledRunIds.has(slot.run_id)
    ) {
      throw protocolError(`${context}.ordered_schedule is inconsistent`);
    }
    scheduledRunIds.add(slot.run_id);
  });
  if (scheduledRunIds.size !== allRunIds.length) {
    throw protocolError(`${context}.ordered_schedule is incomplete`);
  }
  parseControlledOutcomeSelector(value.primary_outcome, `${context}.primary_outcome`);
  return value as unknown as ControlledComparisonPlanView;
}

function controlledDecimal(
  value: Record<string, unknown>,
  key: string,
  context: string,
  signed = false,
): string {
  const text = requiredString(value, key, context);
  const expression = signed
    ? /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/
    : /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
  if (text.length > 64 || !expression.test(text) || /^-0(?:\.0+)?$/.test(text)) {
    throw protocolError(`${context}.${key} is not a bounded decimal string`);
  }
  return text;
}

function parseControlledOutcome(
  value: unknown,
  context: string,
): ControlledComparisonOutcomeEstimate {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  const selector = parseControlledOutcomeSelector(value.selector, `${context}.selector`);
  const unit = requiredString(value, "unit", context);
  if (unit !== selector.unit) throw protocolError(`${context}.unit disagrees with selector`);
  if (!Array.isArray(value.baseline_values) || !Array.isArray(value.candidate_values)) {
    throw protocolError(`${context} arm values must be arrays`);
  }
  const parsePoints = (points: unknown[], arm: string) => points.map((point, index) => {
    if (!isRecord(point)) throw protocolError(`${context}.${arm}[${index}] must be an object`);
    if (requiredInteger(point, "repetition_index", `${context}.${arm}[${index}]`) !== index) {
      throw protocolError(`${context}.${arm} repetition indices must be contiguous`);
    }
    requiredString(point, "run_id", `${context}.${arm}[${index}]`);
    controlledDecimal(point, "value", `${context}.${arm}[${index}]`);
    requiredInteger(point, "sample_count", `${context}.${arm}[${index}]`);
    return point;
  });
  const baseline = parsePoints(value.baseline_values, "baseline_values");
  const candidate = parsePoints(value.candidate_values, "candidate_values");
  if (baseline.length < 2 || baseline.length > 100 || baseline.length !== candidate.length) {
    throw protocolError(`${context} arm value counts are invalid`);
  }
  const baselineIds = new Set(baseline.map((point) => point.run_id as string));
  const candidateIds = new Set(candidate.map((point) => point.run_id as string));
  if (
    baselineIds.size !== baseline.length ||
    candidateIds.size !== candidate.length ||
    [...baselineIds].some((runId) => candidateIds.has(runId))
  ) {
    throw protocolError(`${context} arm run identities are invalid`);
  }
  if (!Array.isArray(value.paired_differences) || value.paired_differences.length !== baseline.length) {
    throw protocolError(`${context}.paired_differences must cover every block`);
  }
  value.paired_differences.forEach((difference, index) => {
    if (!isRecord(difference)) {
      throw protocolError(`${context}.paired_differences[${index}] must be an object`);
    }
    if (
      requiredInteger(difference, "block_index", `${context}.paired_differences[${index}]`) !== index ||
      requiredString(difference, "baseline_run_id", `${context}.paired_differences[${index}]`) !== baseline[index].run_id ||
      requiredString(difference, "candidate_run_id", `${context}.paired_differences[${index}]`) !== candidate[index].run_id
    ) {
      throw protocolError(`${context}.paired_differences do not align with arm points`);
    }
    controlledDecimal(difference, "candidate_minus_baseline", `${context}.paired_differences[${index}]`, true);
  });
  controlledDecimal(value, "baseline_mean", context);
  controlledDecimal(value, "candidate_mean", context);
  controlledDecimal(value, "estimate", context, true);
  return value as unknown as ControlledComparisonOutcomeEstimate;
}

function parseControlledResult(value: unknown, context: string): ControlledComparisonResult {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  if (value.schema_version !== "inferdrome.controlled-comparison-result.v1") {
    throw protocolError(`${context}.schema_version is unsupported`);
  }
  for (const key of [
    "comparison_result_id",
    "comparison_plan_id",
    "comparison_plan_digest",
    "created_at",
  ] as const) {
    requiredString(value, key, context);
  }
  const fixedLiterals: readonly [string, string][] = [
    ["inference_scope", "POINT_ESTIMATE_ONLY"],
    ["predeclaration_assurance", "OPERATOR_ATTESTED"],
    ["environment_control_scope", "OBSERVED_V1_ALLOWLIST_ONLY"],
    ["statistical_unit", "run"],
    ["weighting", "equal_per_run"],
    ["estimator", "paired_run_mean_difference_v1"],
    ["contrast_direction", "candidate_minus_baseline"],
    ["uncertainty_method", "none_v1"],
  ];
  fixedLiterals.forEach(([key, expected]) => {
    if (value[key] !== expected) throw protocolError(`${context}.${key} is unsupported`);
  });
  if (value.status !== "COMPARABLE" && value.status !== "INCOMPARABLE") {
    throw protocolError(`${context}.status is unknown`);
  }
  for (const [key, armContext] of [
    ["baseline_trial_set", "baseline"],
    ["candidate_trial_set", "candidate"],
  ] as const) {
    const reference = value[key];
    if (!isRecord(reference)) throw protocolError(`${context}.${key} must be an object`);
    requiredString(reference, "trial_set_id", `${context}.${armContext}_trial_set`);
    requiredString(reference, "trial_set_digest", `${context}.${armContext}_trial_set`);
  }
  if (!Array.isArray(value.control_checks) || value.control_checks.length !== CONTROL_CHECKS.length) {
    throw protocolError(`${context}.control_checks must contain the v1 checks`);
  }
  const unsatisfied: ControlledComparisonCheckId[] = [];
  value.control_checks.forEach((check, index) => {
    if (!isRecord(check) || check.check !== CONTROL_CHECKS[index]) {
      throw protocolError(`${context}.control_checks are not in the v1 order`);
    }
    if (check.status !== "SATISFIED" && check.status !== "UNSATISFIED") {
      throw protocolError(`${context}.control_checks[${index}].status is unknown`);
    }
    if (check.status === "UNSATISFIED") unsatisfied.push(CONTROL_CHECKS[index]);
  });
  const declaredUnsatisfied = stringArray(value, "unsatisfied_controls", context);
  if (
    declaredUnsatisfied.length !== unsatisfied.length ||
    declaredUnsatisfied.some((check, index) => check !== unsatisfied[index])
  ) {
    throw protocolError(`${context}.unsatisfied_controls disagree with control checks`);
  }
  if (!Array.isArray(value.outcomes)) throw protocolError(`${context}.outcomes must be an array`);
  value.outcomes.forEach((outcome, index) => parseControlledOutcome(outcome, `${context}.outcomes[${index}]`));
  if (
    (value.status === "COMPARABLE" && (unsatisfied.length !== 0 || value.outcomes.length !== 1)) ||
    (value.status === "INCOMPARABLE" && (unsatisfied.length === 0 || value.outcomes.length !== 0))
  ) {
    throw protocolError(`${context} status, controls, and outcomes disagree`);
  }
  return value as unknown as ControlledComparisonResult;
}

function parseControlledComparisonDetail(payload: unknown): ControlledComparisonDetail {
  if (!isRecord(payload) || payload.projection_version !== "inferdrome.dashboard.v1") {
    throw protocolError("the controlled-comparison detail projection version is unsupported");
  }
  const summary = parseControlledSummary(payload.summary, "controlled-comparison summary");
  const plan = parseControlledPlan(payload.plan, "controlled-comparison plan");
  if (
    summary.comparison_plan_id !== plan.comparison_plan_id ||
    summary.experiment_id !== plan.experiment_id ||
    summary.title !== plan.title ||
    summary.planned_repetitions_per_arm !== plan.planned_repetitions_per_arm ||
    summary.baseline_trial_set_id !== plan.baseline_arm.planned_trial_set_id ||
    summary.candidate_trial_set_id !== plan.candidate_arm.planned_trial_set_id
  ) {
    throw protocolError("controlled-comparison summary disagrees with its plan");
  }
  const issue = nullableString(payload, "result_issue", "controlled-comparison detail");
  if (issue !== null && issue !== "RESULT_VERIFICATION_FAILED" && issue !== "DUPLICATE_RESULT_FOR_PLAN") {
    throw protocolError("controlled-comparison result_issue is unknown");
  }
  const result = payload.result === null
    ? null
    : parseControlledResult(payload.result, "controlled-comparison result");
  const baselineTrialSet = payload.baseline_trial_set === null
    ? null
    : parseTrialSetSummary(payload.baseline_trial_set, "controlled-comparison baseline_trial_set");
  const candidateTrialSet = payload.candidate_trial_set === null
    ? null
    : parseTrialSetSummary(payload.candidate_trial_set, "controlled-comparison candidate_trial_set");
  if (result === null) {
    const expectedStatus = issue === null ? "NO_RESULT" : "WITHHELD";
    if (summary.result_status !== expectedStatus || baselineTrialSet !== null || candidateTrialSet !== null) {
      throw protocolError("controlled-comparison missing-result projection is inconsistent");
    }
    return payload as unknown as ControlledComparisonDetail;
  }
  if (
    issue !== null ||
    summary.result_status !== result.status ||
    summary.comparison_result_id !== result.comparison_result_id ||
    summary.comparison_result_digest === null ||
    result.comparison_plan_id !== plan.comparison_plan_id ||
    result.comparison_plan_digest !== summary.comparison_plan_digest
  ) {
    throw protocolError("controlled-comparison result projection is inconsistent");
  }
  if (result.status === "COMPARABLE") {
    const outcome = result.outcomes[0];
    if (
      baselineTrialSet === null ||
      candidateTrialSet === null ||
      baselineTrialSet.trial_set_id !== result.baseline_trial_set.trial_set_id ||
      baselineTrialSet.trial_set_digest !== result.baseline_trial_set.trial_set_digest ||
      candidateTrialSet.trial_set_id !== result.candidate_trial_set.trial_set_id ||
      candidateTrialSet.trial_set_digest !== result.candidate_trial_set.trial_set_digest ||
      !outcome ||
      outcome.selector.metric !== plan.primary_outcome.metric ||
      outcome.selector.aggregation !== plan.primary_outcome.aggregation ||
      outcome.estimate !== summary.estimate ||
      outcome.baseline_values.some((point, index) => point.run_id !== plan.baseline_arm.run_ids[index]) ||
      outcome.candidate_values.some((point, index) => point.run_id !== plan.candidate_arm.run_ids[index])
    ) {
      throw protocolError("controlled-comparison outcome disagrees with its frozen plan");
    }
  } else if (baselineTrialSet !== null || candidateTrialSet !== null) {
    throw protocolError("incomparable comparisons must withhold Trial Set projections");
  }
  return payload as unknown as ControlledComparisonDetail;
}

export const api = {
  async listRuns(signal?: AbortSignal): Promise<RunIndex> {
    const runs: RunSummary[] = [];
    const rejected: RejectedRun[] = [];
    let cursor: string | null = null;
    let generatedAt: string | null = null;
    let expectedTotal: number | null = null;
    const seenRunIds = new Set<string>();
    const seenRejectedEntries = new Set<string>();
    const seenCursors = new Set<string>();

    for (let pageNumber = 0; pageNumber < MAX_RUN_PAGES; pageNumber += 1) {
      const query = new URLSearchParams({ limit: String(RUN_PAGE_LIMIT) });
      if (cursor) query.set("cursor", cursor);
      const page = parseRuns(await fetchJson(`/runs?${query.toString()}`, signal));
      generatedAt ??= page.generated_at;
      expectedTotal ??= page.page.total;
      if (page.page.total !== expectedTotal) {
        throw protocolError("the run index changed while it was being paged");
      }
      for (const run of page.runs) {
        if (seenRunIds.has(run.run_id)) {
          throw protocolError("the paged run index repeated a run");
        }
        seenRunIds.add(run.run_id);
        runs.push(run);
      }
      for (const entry of page.rejected) {
        const identity = `${entry.entry}\u0000${entry.code}`;
        if (seenRejectedEntries.has(identity)) {
          throw protocolError("the paged run index repeated a rejection");
        }
        seenRejectedEntries.add(identity);
        rejected.push(entry);
      }
      cursor = page.page.next_cursor;
      if (!cursor) {
        if (runs.length + rejected.length !== expectedTotal) {
          throw protocolError("the paged run index is incomplete");
        }
        return {
          projection_version: "inferdrome.dashboard.v1",
          generated_at: generatedAt,
          runs,
          rejected,
        };
      }
      if (seenCursors.has(cursor)) {
        throw protocolError("the run index repeated a pagination cursor");
      }
      seenCursors.add(cursor);
    }
    throw protocolError("the run index exceeded its bounded page count");
  },

  async getRun(runId: string, signal?: AbortSignal): Promise<RunDetail> {
    return parseRunDetail(await fetchJson(`/runs/${encodeURIComponent(runId)}`, signal));
  },

  async compareRuns(
    baselineRunId: string,
    candidateRunId: string,
    signal?: AbortSignal,
  ): Promise<Comparison> {
    const query = new URLSearchParams({
      baseline_run_id: baselineRunId,
      candidate_run_id: candidateRunId,
    });
    return parseComparison(await fetchJson(`/compare?${query.toString()}`, signal));
  },

  async listTrialSets(signal?: AbortSignal): Promise<TrialSetIndex> {
    const trialSets: TrialSetSummary[] = [];
    const rejected: RejectedTrialSet[] = [];
    let cursor: string | null = null;
    let generatedAt: string | null = null;
    let expectedTotal: number | null = null;
    const seenTrialSetIds = new Set<string>();
    const seenRejectedEntries = new Set<string>();
    const seenCursors = new Set<string>();

    for (let pageNumber = 0; pageNumber < MAX_TRIAL_SET_PAGES; pageNumber += 1) {
      const query = new URLSearchParams({ limit: String(TRIAL_SET_PAGE_LIMIT) });
      if (cursor) query.set("cursor", cursor);
      const page = parseTrialSetPage(await fetchJson(`/trial-sets?${query.toString()}`, signal));
      generatedAt ??= page.generated_at;
      expectedTotal ??= page.page.total;
      if (page.page.total !== expectedTotal) {
        throw protocolError("the trial-set index changed while it was being paged");
      }
      for (const trialSet of page.trial_sets) {
        if (seenTrialSetIds.has(trialSet.trial_set_id)) {
          throw protocolError("the paged trial-set index repeated a trial set");
        }
        seenTrialSetIds.add(trialSet.trial_set_id);
        trialSets.push(trialSet);
      }
      for (const entry of page.rejected) {
        const identity = `${entry.entry}\u0000${entry.code}`;
        if (seenRejectedEntries.has(identity)) {
          throw protocolError("the paged trial-set index repeated a rejection");
        }
        seenRejectedEntries.add(identity);
        rejected.push(entry);
      }
      cursor = page.page.next_cursor;
      if (!cursor) {
        if (trialSets.length + rejected.length !== expectedTotal) {
          throw protocolError("the paged trial-set index is incomplete");
        }
        return {
          projection_version: "inferdrome.dashboard.v1",
          generated_at: generatedAt,
          trial_sets: trialSets,
          rejected,
        };
      }
      if (seenCursors.has(cursor)) {
        throw protocolError("the trial-set index repeated a pagination cursor");
      }
      seenCursors.add(cursor);
    }
    throw protocolError("the trial-set index exceeded its bounded page count");
  },

  async getTrialSet(trialSetId: string, signal?: AbortSignal): Promise<TrialSetDetail> {
    return parseTrialSetDetail(
      await fetchJson(`/trial-sets/${encodeURIComponent(trialSetId)}`, signal),
    );
  },

  async listControlledComparisons(signal?: AbortSignal): Promise<ControlledComparisonIndex> {
    const comparisons: ControlledComparisonSummary[] = [];
    const rejected: RejectedControlledComparison[] = [];
    let cursor: string | null = null;
    let generatedAt: string | null = null;
    let expectedTotal: number | null = null;
    const seenPlanIds = new Set<string>();
    const seenRejectedEntries = new Set<string>();
    const seenCursors = new Set<string>();

    for (let pageNumber = 0; pageNumber < MAX_CONTROLLED_COMPARISON_PAGES; pageNumber += 1) {
      const query = new URLSearchParams({ limit: String(CONTROLLED_COMPARISON_PAGE_LIMIT) });
      if (cursor) query.set("cursor", cursor);
      const page = parseControlledComparisonPage(
        await fetchJson(`/controlled-comparisons?${query.toString()}`, signal),
      );
      generatedAt ??= page.generated_at;
      expectedTotal ??= page.page.total;
      if (page.page.total !== expectedTotal) {
        throw protocolError("the controlled-comparison index changed while it was being paged");
      }
      for (const comparison of page.comparisons) {
        if (seenPlanIds.has(comparison.comparison_plan_id)) {
          throw protocolError("the controlled-comparison index repeated a plan");
        }
        seenPlanIds.add(comparison.comparison_plan_id);
        comparisons.push(comparison);
      }
      for (const entry of page.rejected) {
        const identity = `${entry.entry}\u0000${entry.code}\u0000${entry.failure_fingerprint}`;
        if (seenRejectedEntries.has(identity)) {
          throw protocolError("the controlled-comparison index repeated a rejection");
        }
        seenRejectedEntries.add(identity);
        rejected.push(entry);
      }
      cursor = page.page.next_cursor;
      if (!cursor) {
        if (comparisons.length + rejected.length !== expectedTotal) {
          throw protocolError("the controlled-comparison index is incomplete");
        }
        return {
          projection_version: "inferdrome.dashboard.v1",
          generated_at: generatedAt,
          comparisons,
          rejected,
        };
      }
      if (seenCursors.has(cursor)) {
        throw protocolError("the controlled-comparison index repeated a pagination cursor");
      }
      seenCursors.add(cursor);
    }
    throw protocolError("the controlled-comparison index exceeded its bounded page count");
  },

  async getControlledComparison(
    comparisonPlanId: string,
    signal?: AbortSignal,
  ): Promise<ControlledComparisonDetail> {
    return parseControlledComparisonDetail(
      await fetchJson(
        `/controlled-comparisons/${encodeURIComponent(comparisonPlanId)}`,
        signal,
      ),
    );
  },
};
