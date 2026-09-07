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
  RejectedRoutingCampaign,
  RejectedRoutingQualification,
  RejectedRoutingExecution,
  RejectedRun,
  RejectedTrialSet,
  RoutingCandidateView,
  RoutingCampaignDetail,
  RoutingCampaignIndex,
  RoutingCampaignPageResponse,
  RoutingCampaignSummary,
  RoutingCampaignTrialView,
  RoutingEndpointId,
  RoutingResetView,
  RoutingRequestView,
  RoutingTelemetryView,
  RoutingTerminalOutcomeView,
  RoutingTerminalPopulationView,
  RoutingQualificationDetail,
  RoutingQualificationEndpointStateView,
  RoutingQualificationIndex,
  RoutingQualificationPageResponse,
  RoutingQualificationSummary,
  RoutingQualificationTrialView,
  RoutingExecutionCandidateView,
  RoutingExecutionDetail,
  RoutingExecutionEndpointIdentityView,
  RoutingExecutionIndex,
  RoutingExecutionPageResponse,
  RoutingExecutionRequestView,
  RoutingExecutionSummary,
  RoutingExecutionTelemetryView,
  RoutingExecutionTerminalView,
  RoutingExecutionTrialView,
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
let dashboardToken: string | null = null;

export function setDashboardToken(token: string | null): void {
  dashboardToken = token;
}
const RUN_PAGE_LIMIT = 200;
const MAX_RUN_PAGES = 5;
const TRIAL_SET_PAGE_LIMIT = 100;
const MAX_TRIAL_SET_PAGES = 2;
const CONTROLLED_COMPARISON_PAGE_LIMIT = 100;
const MAX_CONTROLLED_COMPARISON_PAGES = 4;
const ROUTING_CAMPAIGN_PAGE_LIMIT = 25;
const ROUTING_CAMPAIGN_PROJECTION = "inferdrome.routing-campaign-dashboard.v1";
const ROUTING_QUALIFICATION_PAGE_LIMIT = 25;
const ROUTING_QUALIFICATION_PROJECTION = "inferdrome.routing-qualification-dashboard.v1";
const ROUTING_QUALIFICATION_ID = "stale-telemetry-qualification-v1";
const ROUTING_QUALIFICATION_MAX_BYTES = 524_288;
const ROUTING_EXECUTION_PAGE_LIMIT = 25;
const ROUTING_EXECUTION_PROJECTION = "inferdrome.routing-execution-dashboard.v1";
const ROUTING_EXECUTION_ID = "routing-execution-v1";
const ROUTING_POLICY_IDS = [
  "fail_closed_required_load_v1",
  "explicit_fail_open_stale_load_v1",
  "typed_admissible_state_only_v1",
] as const;
const ROUTING_TERMINAL_STATUSES = [
  "SUCCEEDED",
  "TIMED_OUT",
  "FAILED",
  "CANCELLED",
  "NO_SAFE_ROUTE",
] as const;
const ROUTING_QUALIFICATION_TRIALS = [
  {
    policyId: "fail_closed_required_load_v1",
    trialId: "trial-fail-closed-v1",
    selectedEndpointId: null,
    fallbackReason: "REQUIRED_LOAD_STALE",
    terminalStatus: "NO_SAFE_ROUTE",
    terminalPopulation: [2, 0, 0, 0, 4],
  },
  {
    policyId: "explicit_fail_open_stale_load_v1",
    trialId: "trial-fail-open-v1",
    selectedEndpointId: "endpoint-b",
    fallbackReason: "STALE_LOAD_FAIL_OPEN",
    terminalStatus: "TIMED_OUT",
    terminalPopulation: [2, 4, 0, 0, 0],
  },
  {
    policyId: "typed_admissible_state_only_v1",
    trialId: "trial-typed-v1",
    selectedEndpointId: "endpoint-a",
    fallbackReason: "HEALTH_ONLY_TIE_BREAK",
    terminalStatus: "SUCCEEDED",
    terminalPopulation: [6, 0, 0, 0, 0],
  },
] as const;

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
    const headers: Record<string, string> = { Accept: "application/json" };
    if (dashboardToken !== null) headers.Authorization = `Bearer ${dashboardToken}`;
    response = await fetch(`${API_ROOT}${path}`, {
      method: "GET",
      headers,
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
  if (
    typeof value !== "number"
    || !Number.isSafeInteger(value)
    || value < minimum
  ) {
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
  if (
    value !== null
    && (
      typeof value !== "number"
      || !Number.isSafeInteger(value)
      || value < 0
    )
  ) {
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

function assertRoutingKeys(
  object: Record<string, unknown>,
  allowed: readonly string[],
  context: string,
): void {
  if (Object.keys(object).some((key) => !allowed.includes(key))) {
    throw protocolError(`${context} contains a field outside the routing projection allowlist`);
  }
}

function routingEndpointId(value: unknown, context: string): RoutingEndpointId {
  if (value !== "endpoint-a" && value !== "endpoint-b") {
    throw protocolError(`${context} must be endpoint-a or endpoint-b`);
  }
  return value;
}

function routingClaims(
  object: Record<string, unknown>,
  key: string,
  context: string,
): readonly string[] {
  const claims = stringArray(object, key, context);
  if (claims.length > 3 || new Set(claims).size !== claims.length) {
    throw protocolError(`${context}.${key} must contain at most three unique claims`);
  }
  return claims;
}

function parseRoutingCampaignSummary(
  value: unknown,
  context: string,
): RoutingCampaignSummary {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, [
    "campaign_id",
    "retained_digest",
    "execution_mode",
    "trial_count",
    "planned_request_count",
    "policy_ids",
    "verified_by_replay",
  ], context);
  if (value.campaign_id !== "routing-campaign-v1") {
    throw protocolError(`${context}.campaign_id is unsupported`);
  }
  const retainedDigest = requiredString(value, "retained_digest", context);
  if (!/^sha256:[0-9a-f]{64}$/.test(retainedDigest)) {
    throw protocolError(`${context}.retained_digest is invalid`);
  }
  if (value.execution_mode !== "SYNTHETIC_CPU_ONLY") {
    throw protocolError(`${context}.execution_mode is unsupported`);
  }
  if (requiredInteger(value, "trial_count", context, 1) !== ROUTING_POLICY_IDS.length) {
    throw protocolError(`${context}.trial_count is unsupported`);
  }
  if (requiredInteger(value, "planned_request_count", context, 1) !== 18) {
    throw protocolError(`${context}.planned_request_count is unsupported`);
  }
  const policies = stringArray(value, "policy_ids", context);
  if (
    policies.length !== ROUTING_POLICY_IDS.length
    || policies.some((policy, index) => policy !== ROUTING_POLICY_IDS[index])
  ) {
    throw protocolError(`${context}.policy_ids are not the frozen R1 policy order`);
  }
  if (value.verified_by_replay !== true) {
    throw protocolError(`${context}.verified_by_replay must be true`);
  }
  return value as unknown as RoutingCampaignSummary;
}

function parseRejectedRoutingCampaign(
  value: unknown,
  context: string,
): RejectedRoutingCampaign {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, ["entry", "status", "code", "message"], context);
  requiredString(value, "entry", context);
  const message = requiredString(value, "message", context);
  if (message.length === 0 || message.length > 160) {
    throw protocolError(`${context}.message is not bounded`);
  }
  if (value.status !== "REJECTED") {
    throw protocolError(`${context}.status must be REJECTED`);
  }
  if (value.code !== "VERIFICATION_FAILED" && value.code !== "UNSAFE_ENTRY") {
    throw protocolError(`${context}.code is unknown`);
  }
  return value as unknown as RejectedRoutingCampaign;
}

function parseRoutingCampaignPage(payload: unknown): RoutingCampaignPageResponse {
  if (!isRecord(payload)) throw protocolError("the routing-campaign index is not an object");
  assertRoutingKeys(payload, ["projection_version", "routing_campaigns", "rejected", "page"], "routing-campaign index");
  if (payload.projection_version !== ROUTING_CAMPAIGN_PROJECTION) {
    throw protocolError("the routing-campaign index projection version is unsupported");
  }
  if (!Array.isArray(payload.routing_campaigns) || !Array.isArray(payload.rejected)) {
    throw protocolError("routing-campaign index entries must be arrays");
  }
  payload.routing_campaigns.forEach((summary, index) => (
    parseRoutingCampaignSummary(summary, `routing_campaigns[${index}]`)
  ));
  payload.rejected.forEach((entry, index) => (
    parseRejectedRoutingCampaign(entry, `rejected[${index}]`)
  ));
  if (payload.routing_campaigns.length > 1 || payload.rejected.length > 1) {
    throw protocolError("routing-campaign index exceeds its one-root bound");
  }
  if (!isRecord(payload.page)) throw protocolError("routing-campaign page metadata is missing");
  assertRoutingKeys(payload.page, ["limit", "returned", "total", "has_more", "next_cursor"], "routing-campaign page");
  const { has_more: hasMore, limit, next_cursor: nextCursor, returned, total } = payload.page;
  if (
    typeof limit !== "number"
    || !Number.isInteger(limit)
    || limit < 1
    || limit > ROUTING_CAMPAIGN_PAGE_LIMIT
    || typeof returned !== "number"
    || !Number.isInteger(returned)
    || returned !== payload.routing_campaigns.length + payload.rejected.length
    || typeof total !== "number"
    || !Number.isInteger(total)
    || total !== returned
    || total > 1
    || hasMore !== false
    || nextCursor !== null
  ) {
    throw protocolError("routing-campaign page metadata is invalid");
  }
  return payload as unknown as RoutingCampaignPageResponse;
}

function parseRoutingTelemetry(
  value: unknown,
  context: string,
  expectedSignal: "HEALTH" | "LOAD" | "KV",
  expectedEndpoint: RoutingEndpointId,
  expectedDecisionTime: number,
): RoutingTelemetryView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, [
    "signal",
    "observer_id",
    "endpoint_id",
    "epoch",
    "observed_at_ms",
    "decision_time_ms",
    "age_ms",
    "freshness_bound_ms",
    "value",
    "admissibility",
  ], context);
  if (value.signal !== expectedSignal) {
    throw protocolError(`${context}.signal must be ${expectedSignal}`);
  }
  requiredString(value, "observer_id", context);
  if (routingEndpointId(value.endpoint_id, `${context}.endpoint_id`) !== expectedEndpoint) {
    throw protocolError(`${context}.endpoint_id disagrees with its candidate`);
  }
  requiredInteger(value, "epoch", context, 1);
  const observedAt = requiredInteger(value, "observed_at_ms", context);
  const decisionAt = requiredInteger(value, "decision_time_ms", context);
  const age = requiredInteger(value, "age_ms", context);
  const freshnessBound = requiredInteger(value, "freshness_bound_ms", context);
  if (decisionAt !== expectedDecisionTime || observedAt > decisionAt || age !== decisionAt - observedAt) {
    throw protocolError(`${context} clock values disagree`);
  }
  if (
    (typeof value.value !== "string" && (typeof value.value !== "number" || !Number.isFinite(value.value)))
    || (value.admissibility !== "ADMISSIBLE" && value.admissibility !== "INADMISSIBLE")
  ) {
    throw protocolError(`${context} value or admissibility is invalid`);
  }
  const expectedAdmissibility = age <= freshnessBound ? "ADMISSIBLE" : "INADMISSIBLE";
  if (value.admissibility !== expectedAdmissibility) {
    throw protocolError(`${context}.admissibility disagrees with freshness`);
  }
  return value as unknown as RoutingTelemetryView;
}

function parseRoutingCandidate(
  value: unknown,
  context: string,
  expectedEndpoint: RoutingEndpointId,
  decisionTime: number,
): RoutingCandidateView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, ["endpoint_id", "eligible", "health", "load", "kv"], context);
  if (routingEndpointId(value.endpoint_id, `${context}.endpoint_id`) !== expectedEndpoint) {
    throw protocolError(`${context}.endpoint_id is out of order`);
  }
  if (typeof value.eligible !== "boolean") {
    throw protocolError(`${context}.eligible must be boolean`);
  }
  parseRoutingTelemetry(value.health, `${context}.health`, "HEALTH", expectedEndpoint, decisionTime);
  parseRoutingTelemetry(value.load, `${context}.load`, "LOAD", expectedEndpoint, decisionTime);
  parseRoutingTelemetry(value.kv, `${context}.kv`, "KV", expectedEndpoint, decisionTime);
  return value as unknown as RoutingCandidateView;
}

function parseRoutingReset(value: unknown, context: string): RoutingResetView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, [
    "virtual_time_ms",
    "endpoint_instances",
    "observer_epochs",
    "queue_cleared",
    "load_state_cleared",
    "kv_state_cleared",
  ], context);
  if (requiredInteger(value, "virtual_time_ms", context) !== 0) {
    throw protocolError(`${context}.virtual_time_ms must be zero`);
  }
  if (!Array.isArray(value.endpoint_instances) || value.endpoint_instances.length !== 2) {
    throw protocolError(`${context}.endpoint_instances must contain both endpoints`);
  }
  const instanceIds = new Set<string>();
  value.endpoint_instances.forEach((instance, index) => {
    if (!isRecord(instance)) throw protocolError(`${context}.endpoint_instances[${index}] must be an object`);
    assertRoutingKeys(instance, ["endpoint_id", "instance_id"], `${context}.endpoint_instances[${index}]`);
    if (routingEndpointId(instance.endpoint_id, `${context}.endpoint_instances[${index}].endpoint_id`) !== (index === 0 ? "endpoint-a" : "endpoint-b")) {
      throw protocolError(`${context}.endpoint_instances are not in endpoint order`);
    }
    const instanceId = requiredString(instance, "instance_id", `${context}.endpoint_instances[${index}]`);
    if (instanceId.length === 0 || instanceIds.has(instanceId)) {
      throw protocolError(`${context}.endpoint_instances repeat an identity`);
    }
    instanceIds.add(instanceId);
  });
  if (!Array.isArray(value.observer_epochs) || value.observer_epochs.length !== 3) {
    throw protocolError(`${context}.observer_epochs must contain three observers`);
  }
  const observerIds = new Set<string>();
  value.observer_epochs.forEach((observer, index) => {
    if (!isRecord(observer)) throw protocolError(`${context}.observer_epochs[${index}] must be an object`);
    assertRoutingKeys(observer, ["observer_id", "epoch"], `${context}.observer_epochs[${index}]`);
    const observerId = requiredString(observer, "observer_id", `${context}.observer_epochs[${index}]`);
    if (observerIds.has(observerId)) {
      throw protocolError(`${context}.observer_epochs repeat an observer`);
    }
    observerIds.add(observerId);
    requiredInteger(observer, "epoch", `${context}.observer_epochs[${index}]`, 1);
  });
  for (const key of ["queue_cleared", "load_state_cleared", "kv_state_cleared"] as const) {
    if (value[key] !== true) throw protocolError(`${context}.${key} must be true`);
  }
  return value as unknown as RoutingResetView;
}

function parseRoutingTerminal(value: unknown, context: string): RoutingTerminalOutcomeView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, [
    "terminal_outcome_id",
    "decision_id",
    "status",
    "reason",
    "started_at_ms",
    "ended_at_ms",
  ], context);
  requiredString(value, "terminal_outcome_id", context);
  requiredString(value, "decision_id", context);
  requiredString(value, "reason", context);
  if (!ROUTING_TERMINAL_STATUSES.includes(value.status as typeof ROUTING_TERMINAL_STATUSES[number])) {
    throw protocolError(`${context}.status is unknown`);
  }
  const startedAt = requiredInteger(value, "started_at_ms", context);
  const endedAt = requiredInteger(value, "ended_at_ms", context);
  if (endedAt < startedAt) throw protocolError(`${context} ends before it starts`);
  return value as unknown as RoutingTerminalOutcomeView;
}

function parseRoutingRequest(value: unknown, context: string, sequenceIndex: number): RoutingRequestView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, [
    "request_id",
    "sequence_index",
    "decision_id",
    "decision_time_ms",
    "candidates",
    "selected_endpoint_id",
    "claims_used",
    "claims_permitted_stale",
    "claims_discarded",
    "fallback_reason",
    "terminal",
  ], context);
  const expectedRequestId = `request-${String(sequenceIndex).padStart(3, "0")}`;
  if (requiredString(value, "request_id", context) !== expectedRequestId) {
    throw protocolError(`${context}.request_id is out of trace order`);
  }
  if (requiredInteger(value, "sequence_index", context) !== sequenceIndex) {
    throw protocolError(`${context}.sequence_index is not contiguous`);
  }
  const decisionId = requiredString(value, "decision_id", context);
  if (!/^decision-[a-z0-9-]+$/.test(decisionId)) {
    throw protocolError(`${context}.decision_id is invalid`);
  }
  const decisionTime = requiredInteger(value, "decision_time_ms", context);
  if (decisionTime !== sequenceIndex * 10) {
    throw protocolError(`${context}.decision_time_ms is not the frozen trace time`);
  }
  if (!Array.isArray(value.candidates) || value.candidates.length !== 2) {
    throw protocolError(`${context}.candidates must contain both endpoints`);
  }
  const candidates = [
    parseRoutingCandidate(value.candidates[0], `${context}.candidates[0]`, "endpoint-a", decisionTime),
    parseRoutingCandidate(value.candidates[1], `${context}.candidates[1]`, "endpoint-b", decisionTime),
  ];
  if (candidates[0].endpoint_id === candidates[1].endpoint_id) {
    throw protocolError(`${context}.candidates repeat an endpoint`);
  }
  const selected = value.selected_endpoint_id;
  if (selected !== null) routingEndpointId(selected, `${context}.selected_endpoint_id`);
  routingClaims(value, "claims_used", context);
  routingClaims(value, "claims_permitted_stale", context);
  routingClaims(value, "claims_discarded", context);
  if (![
    "NONE",
    "REQUIRED_LOAD_STALE",
    "STALE_LOAD_FAIL_OPEN",
    "HEALTH_ONLY_TIE_BREAK",
  ].includes(value.fallback_reason as string)) {
    throw protocolError(`${context}.fallback_reason is unknown`);
  }
  const terminal = parseRoutingTerminal(value.terminal, `${context}.terminal`);
  if (terminal.decision_id !== decisionId || terminal.started_at_ms !== decisionTime) {
    throw protocolError(`${context}.terminal does not close its decision`);
  }
  if (
    (value.fallback_reason === "REQUIRED_LOAD_STALE" && (selected !== null || terminal.status !== "NO_SAFE_ROUTE"))
    || (value.fallback_reason !== "REQUIRED_LOAD_STALE" && selected === null)
    || (terminal.status === "NO_SAFE_ROUTE" && selected !== null)
    || (terminal.status !== "NO_SAFE_ROUTE" && selected === null)
  ) {
    throw protocolError(`${context}.selection, fallback, and terminal disagree`);
  }
  return value as unknown as RoutingRequestView;
}

function parseRoutingTrial(
  value: unknown,
  context: string,
  expectedPolicyId: string,
): RoutingCampaignTrialView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, [
    "trial_id",
    "policy_id",
    "reset",
    "requests",
    "terminal_population",
    "terminal_population_total",
  ], context);
  const trialId = requiredString(value, "trial_id", context);
  if (!/^trial-[a-z-]+-v1$/.test(trialId)) {
    throw protocolError(`${context}.trial_id is invalid`);
  }
  if (requiredString(value, "policy_id", context) !== expectedPolicyId) {
    throw protocolError(`${context}.policy_id is out of frozen order`);
  }
  parseRoutingReset(value.reset, `${context}.reset`);
  if (!Array.isArray(value.requests) || value.requests.length !== 6) {
    throw protocolError(`${context}.requests must contain the six-request trace`);
  }
  const requests = value.requests.map((request, index) => (
    parseRoutingRequest(request, `${context}.requests[${index}]`, index)
  ));
  if (!Array.isArray(value.terminal_population) || value.terminal_population.length !== ROUTING_TERMINAL_STATUSES.length) {
    throw protocolError(`${context}.terminal_population is incomplete`);
  }
  const population = value.terminal_population.map((entry, index) => {
    if (!isRecord(entry)) throw protocolError(`${context}.terminal_population[${index}] must be an object`);
    assertRoutingKeys(entry, ["status", "count"], `${context}.terminal_population[${index}]`);
    if (entry.status !== ROUTING_TERMINAL_STATUSES[index]) {
      throw protocolError(`${context}.terminal_population is not in terminal-status order`);
    }
    return requiredInteger(entry, "count", `${context}.terminal_population[${index}]`);
  });
  const declaredTotal = requiredInteger(value, "terminal_population_total", context);
  if (declaredTotal !== requests.length || population.reduce((total, count) => total + count, 0) !== declaredTotal) {
    throw protocolError(`${context}.terminal population does not close the trace`);
  }
  const actualPopulation = new Map<string, number>();
  requests.forEach((request) => {
    actualPopulation.set(request.terminal.status, (actualPopulation.get(request.terminal.status) ?? 0) + 1);
  });
  if (population.some((count, index) => count !== (actualPopulation.get(ROUTING_TERMINAL_STATUSES[index]) ?? 0))) {
    throw protocolError(`${context}.terminal population disagrees with terminal receipts`);
  }
  return value as unknown as RoutingCampaignTrialView;
}

function parseRoutingCampaignDetail(payload: unknown): RoutingCampaignDetail {
  if (!isRecord(payload)) throw protocolError("the routing-campaign detail is not an object");
  assertRoutingKeys(payload, [
    "projection_version",
    "summary",
    "fault_timeline",
    "trials",
    "interpretation_boundary",
  ], "routing-campaign detail");
  if (payload.projection_version !== ROUTING_CAMPAIGN_PROJECTION) {
    throw protocolError("the routing-campaign detail projection version is unsupported");
  }
  const summary = parseRoutingCampaignSummary(payload.summary, "routing-campaign summary");
  if (!isRecord(payload.fault_timeline)) {
    throw protocolError("routing-campaign fault_timeline must be an object");
  }
  const timeline = payload.fault_timeline;
  assertRoutingKeys(timeline, [
    "load_collection_paused_at_ms",
    "health_collection_continues",
    "load_freshness_bound_ms",
    "health_freshness_bound_ms",
  ], "routing-campaign fault_timeline");
  if (
    requiredInteger(timeline, "load_collection_paused_at_ms", "routing-campaign fault_timeline") !== 15
    || timeline.health_collection_continues !== true
    || requiredInteger(timeline, "load_freshness_bound_ms", "routing-campaign fault_timeline") !== 5
    || requiredInteger(timeline, "health_freshness_bound_ms", "routing-campaign fault_timeline") !== 5
  ) {
    throw protocolError("routing-campaign fault_timeline is not the frozen R1 boundary");
  }
  if (!Array.isArray(payload.trials) || payload.trials.length !== summary.trial_count) {
    throw protocolError("routing-campaign trials do not match the verified summary");
  }
  const trials = payload.trials.map((trial, index) => (
    parseRoutingTrial(trial, `routing-campaign trials[${index}]`, summary.policy_ids[index] ?? "")
  ));
  if (new Set(trials.map((trial) => trial.trial_id)).size !== trials.length) {
    throw protocolError("routing-campaign trials repeat an identity");
  }
  if (payload.interpretation_boundary !== "MEASUREMENT_EVIDENCE_ONLY") {
    throw protocolError("routing-campaign interpretation boundary is unsupported");
  }
  return payload as unknown as RoutingCampaignDetail;
}

function qualificationDigest(value: unknown, context: string): string {
  if (typeof value !== "string" || !/^sha256:[0-9a-f]{64}$/.test(value)) {
    throw protocolError(`${context} must be a retained sha256 digest`);
  }
  return value;
}

function parseRoutingQualificationSummary(
  value: unknown,
  context: string,
): RoutingQualificationSummary {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, [
    "qualification_id",
    "retained_digest",
    "source_campaign_id",
    "source_package_retained_digest",
    "source_execution_mode",
    "repetitions_per_mode",
    "population_accounting",
    "verified_by_source_replay",
    "verified_descriptor_binding",
  ], context);
  if (value.qualification_id !== ROUTING_QUALIFICATION_ID) {
    throw protocolError(`${context}.qualification_id is unsupported`);
  }
  qualificationDigest(value.retained_digest, `${context}.retained_digest`);
  if (
    value.source_campaign_id !== "routing-campaign-v1"
    || value.source_execution_mode !== "SYNTHETIC_CPU_ONLY"
    || value.repetitions_per_mode !== 1
    || value.population_accounting !== "SEPARATE_PER_TRIAL_NO_POOLING"
    || value.verified_by_source_replay !== true
    || value.verified_descriptor_binding !== true
  ) {
    throw protocolError(`${context} is outside the fixed causal qualification boundary`);
  }
  qualificationDigest(value.source_package_retained_digest, `${context}.source_package_retained_digest`);
  return value as unknown as RoutingQualificationSummary;
}

function parseRejectedRoutingQualification(
  value: unknown,
  context: string,
): RejectedRoutingQualification {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, ["entry", "status", "code", "message"], context);
  if (
    requiredString(value, "entry", context) !== "<configured-root>"
    || value.status !== "REJECTED"
    || !["CONFIGURATION_INVALID", "UNSAFE_ENTRY", "VERIFICATION_FAILED"].includes(value.code as string)
  ) {
    throw protocolError(`${context} is not a bounded qualification rejection`);
  }
  const message = requiredString(value, "message", context);
  if (message.length === 0 || message.length > 160) {
    throw protocolError(`${context}.message is not bounded`);
  }
  return value as unknown as RejectedRoutingQualification;
}

function parseRoutingQualificationPage(payload: unknown): RoutingQualificationPageResponse {
  if (!isRecord(payload)) throw protocolError("the routing-qualification index is not an object");
  assertRoutingKeys(payload, ["projection_version", "routing_qualifications", "rejected", "page"], "routing-qualification index");
  if (payload.projection_version !== ROUTING_QUALIFICATION_PROJECTION) {
    throw protocolError("the routing-qualification index projection version is unsupported");
  }
  if (!Array.isArray(payload.routing_qualifications) || !Array.isArray(payload.rejected)) {
    throw protocolError("routing-qualification index entries must be arrays");
  }
  payload.routing_qualifications.forEach((entry, index) => (
    parseRoutingQualificationSummary(entry, `routing_qualifications[${index}]`)
  ));
  payload.rejected.forEach((entry, index) => (
    parseRejectedRoutingQualification(entry, `rejected[${index}]`)
  ));
  if (payload.routing_qualifications.length + payload.rejected.length > 1) {
    throw protocolError("routing-qualification index exceeds its one-root bound");
  }
  if (!isRecord(payload.page)) throw protocolError("routing-qualification page metadata is missing");
  assertRoutingKeys(payload.page, ["limit", "returned", "total", "has_more", "next_cursor"], "routing-qualification page");
  if (
    requiredInteger(payload.page, "limit", "routing-qualification page", 1) > ROUTING_QUALIFICATION_PAGE_LIMIT
    || requiredInteger(payload.page, "returned", "routing-qualification page") !== payload.routing_qualifications.length + payload.rejected.length
    || requiredInteger(payload.page, "total", "routing-qualification page") !== payload.routing_qualifications.length + payload.rejected.length
    || payload.page.has_more !== false
    || payload.page.next_cursor !== null
  ) {
    throw protocolError("routing-qualification page metadata is invalid");
  }
  return payload as unknown as RoutingQualificationPageResponse;
}

function parseQualificationEndpointState(
  value: unknown,
  context: string,
  expectedEndpoint: RoutingEndpointId,
): RoutingQualificationEndpointStateView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, [
    "endpoint_id",
    "health_epoch",
    "health_age_ms",
    "health_admissibility",
    "load_epoch",
    "load_age_ms",
    "load_admissibility",
  ], context);
  if (
    routingEndpointId(value.endpoint_id, `${context}.endpoint_id`) !== expectedEndpoint
    || requiredInteger(value, "health_epoch", context, 1) < 1
    || requiredInteger(value, "load_epoch", context, 1) < 1
    || value.health_age_ms !== 0
    || value.health_admissibility !== "ADMISSIBLE"
    || value.load_age_ms !== 10
    || value.load_admissibility !== "INADMISSIBLE"
  ) {
    throw protocolError(`${context} does not describe the fixed stale-load/fresh-health state`);
  }
  return value as unknown as RoutingQualificationEndpointStateView;
}

function parseRoutingQualificationTrial(
  value: unknown,
  context: string,
  expected: typeof ROUTING_QUALIFICATION_TRIALS[number],
): RoutingQualificationTrialView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, [
    "policy_id",
    "repetition_index",
    "trial_id",
    "request_denominator",
    "reset",
    "focal_request_id",
    "focal_decision_id",
    "focal_endpoint_states",
    "selected_endpoint_id",
    "fallback_reason",
    "terminal_status",
    "terminal_reason",
    "reset_receipt_sha256",
    "state_observations_sha256",
    "route_decisions_sha256",
    "terminal_outcomes_sha256",
    "terminal_population",
    "terminal_population_total",
  ], context);
  if (
    value.policy_id !== expected.policyId
    || value.repetition_index !== 0
    || requiredString(value, "trial_id", context) !== expected.trialId
    || value.request_denominator !== 6
    || value.focal_request_id !== "request-002"
    || !/^decision-[a-z0-9-]+$/.test(requiredString(value, "focal_decision_id", context))
  ) {
    throw protocolError(`${context} is outside the fixed qualification trial inventory`);
  }
  if (!isRecord(value.reset)) throw protocolError(`${context}.reset must be an object`);
  assertRoutingKeys(value.reset, [
    "virtual_time_ms",
    "endpoint_a_instance_id",
    "endpoint_b_instance_id",
    "observer_epochs",
    "queue_cleared",
    "load_state_cleared",
    "kv_state_cleared",
  ], `${context}.reset`);
  if (
    value.reset.virtual_time_ms !== 0
    || requiredString(value.reset, "endpoint_a_instance_id", `${context}.reset`) !== `${expected.trialId}-endpoint-a-instance-v1`
    || requiredString(value.reset, "endpoint_b_instance_id", `${context}.reset`) !== `${expected.trialId}-endpoint-b-instance-v1`
    || value.reset.endpoint_a_instance_id === value.reset.endpoint_b_instance_id
    || value.reset.queue_cleared !== true
    || value.reset.load_state_cleared !== true
    || value.reset.kv_state_cleared !== true
    || !Array.isArray(value.reset.observer_epochs)
    || value.reset.observer_epochs.length !== 3
  ) {
    throw protocolError(`${context}.reset is not a complete cold-reset receipt`);
  }
  value.reset.observer_epochs.forEach((epoch, index) => {
    if (typeof epoch !== "number" || !Number.isInteger(epoch) || epoch < 1) {
      throw protocolError(`${context}.reset.observer_epochs[${index}] is invalid`);
    }
  });
  if (!Array.isArray(value.focal_endpoint_states) || value.focal_endpoint_states.length !== 2) {
    throw protocolError(`${context}.focal_endpoint_states must contain both endpoints`);
  }
  parseQualificationEndpointState(value.focal_endpoint_states[0], `${context}.focal_endpoint_states[0]`, "endpoint-a");
  parseQualificationEndpointState(value.focal_endpoint_states[1], `${context}.focal_endpoint_states[1]`, "endpoint-b");
  if (
    value.selected_endpoint_id !== expected.selectedEndpointId
    || value.fallback_reason !== expected.fallbackReason
    || value.terminal_status !== expected.terminalStatus
    || !requiredString(value, "terminal_reason", context)
  ) {
    throw protocolError(`${context} selection, fallback, and terminal outcome disagree`);
  }
  for (const key of [
    "reset_receipt_sha256",
    "state_observations_sha256",
    "route_decisions_sha256",
    "terminal_outcomes_sha256",
  ] as const) qualificationDigest(value[key], `${context}.${key}`);
  if (!Array.isArray(value.terminal_population) || value.terminal_population.length !== ROUTING_TERMINAL_STATUSES.length) {
    throw protocolError(`${context}.terminal_population is incomplete`);
  }
  let total = 0;
  value.terminal_population.forEach((entry, index) => {
    if (!isRecord(entry) || entry.status !== ROUTING_TERMINAL_STATUSES[index]) {
      throw protocolError(`${context}.terminal_population[${index}] is out of status order`);
    }
    const count = requiredInteger(entry, "count", `${context}.terminal_population[${index}]`);
    if (count !== expected.terminalPopulation[index]) {
      throw protocolError(`${context}.terminal_population disagrees with the declared mode`);
    }
    total += count;
  });
  if (value.terminal_population_total !== 6 || total !== 6) {
    throw protocolError(`${context}.terminal_population does not close its separate trial`);
  }
  return value as unknown as RoutingQualificationTrialView;
}

function parseRoutingQualificationDetail(payload: unknown): RoutingQualificationDetail {
  if (!isRecord(payload)) throw protocolError("the routing-qualification detail is not an object");
  assertRoutingKeys(payload, [
    "projection_version",
    "summary",
    "fault_timeline",
    "trials",
    "source_receipts_path",
    "descriptor_download_path",
    "interpretation_boundary",
  ], "routing-qualification detail");
  if (payload.projection_version !== ROUTING_QUALIFICATION_PROJECTION) {
    throw protocolError("the routing-qualification detail projection version is unsupported");
  }
  parseRoutingQualificationSummary(payload.summary, "routing-qualification summary");
  if (!isRecord(payload.fault_timeline)) throw protocolError("routing-qualification fault_timeline must be an object");
  assertRoutingKeys(payload.fault_timeline, [
    "load_observer_pause_at_ms",
    "health_collection_continues",
    "focal_decision_time_ms",
    "health_age_ms",
    "load_age_ms",
    "freshness_bound_ms",
  ], "routing-qualification fault_timeline");
  if (
    payload.fault_timeline.load_observer_pause_at_ms !== 15
    || payload.fault_timeline.health_collection_continues !== true
    || payload.fault_timeline.focal_decision_time_ms !== 20
    || payload.fault_timeline.health_age_ms !== 0
    || payload.fault_timeline.load_age_ms !== 10
    || payload.fault_timeline.freshness_bound_ms !== 5
  ) {
    throw protocolError("routing-qualification fault_timeline is outside the fixed causal vector");
  }
  if (!Array.isArray(payload.trials) || payload.trials.length !== ROUTING_QUALIFICATION_TRIALS.length) {
    throw protocolError("routing-qualification trials must contain the three declared modes");
  }
  const trials = payload.trials.map((trial, index) => (
    parseRoutingQualificationTrial(
      trial,
      `routing-qualification trials[${index}]`,
      ROUTING_QUALIFICATION_TRIALS[index]!,
    )
  ));
  const trialIds = new Set(trials.map((trial) => trial.trial_id));
  const resetInstanceIds = new Set(
    trials.flatMap((trial) => [
      trial.reset.endpoint_a_instance_id,
      trial.reset.endpoint_b_instance_id,
    ]),
  );
  if (trialIds.size !== trials.length || resetInstanceIds.size !== trials.length * 2) {
    throw protocolError("routing-qualification trials do not retain separate cold-reset identities");
  }
  if (
    payload.source_receipts_path !== "/routing-campaigns/routing-campaign-v1"
    || payload.descriptor_download_path !== "/api/v1/routing-qualifications/stale-telemetry-qualification-v1/evidence"
    || payload.interpretation_boundary !== "MEASUREMENT_EVIDENCE_ONLY"
  ) {
    throw protocolError("routing-qualification bounded links or interpretation boundary disagree");
  }
  return payload as unknown as RoutingQualificationDetail;
}

function executionDigest(value: unknown, context: string): string {
  if (typeof value !== "string" || !/^sha256:[0-9a-f]{64}$/.test(value)) {
    throw protocolError(`${context} must be a retained sha256 digest`);
  }
  return value;
}

function parseExecutionEndpoint(
  value: unknown,
  context: string,
  expectedEndpoint: RoutingEndpointId,
): RoutingExecutionEndpointIdentityView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, ["endpoint_id"], context);
  if (routingEndpointId(value.endpoint_id, `${context}.endpoint_id`) !== expectedEndpoint) {
    throw protocolError(`${context}.endpoint_id is out of bounded order`);
  }
  return value as unknown as RoutingExecutionEndpointIdentityView;
}

function parseExecutionSummary(
  value: unknown,
  context: string,
): RoutingExecutionSummary {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, [
    "execution_id",
    "retained_digest",
    "mode",
    "source_commit",
    "model",
    "runtime",
    "topology",
    "policy_ids",
    "trial_count",
    "request_denominator_per_trial",
    "terminal_denominator",
    "verified_by_offline_replay",
  ], context);
  if (
    value.execution_id !== ROUTING_EXECUTION_ID
    || !["LOCAL_LOOPBACK", "GCP_PRIVATE", "LAMBDA_MANUAL_HOST"].includes(value.mode as string)
    || !/^[0-9a-f]{40}$/.test(requiredString(value, "source_commit", context))
    || value.trial_count !== 3
    || value.request_denominator_per_trial !== 6
    || value.terminal_denominator !== 18
    || value.verified_by_offline_replay !== true
  ) {
    throw protocolError(`${context} is outside the bounded routing-execution contract`);
  }
  executionDigest(value.retained_digest, `${context}.retained_digest`);
  if (!isRecord(value.model)) throw protocolError(`${context}.model must be an object`);
  assertRoutingKeys(value.model, ["model_id", "model_revision", "tokenizer_revision"], `${context}.model`);
  if (
    value.model.model_id !== "Qwen/Qwen3-8B"
    || !/^[0-9a-f]{40}$/.test(requiredString(value.model, "model_revision", `${context}.model`))
    || value.model.tokenizer_revision !== value.model.model_revision
  ) {
    throw protocolError(`${context}.model is not the pinned Qwen3 identity`);
  }
  if (!isRecord(value.runtime)) throw protocolError(`${context}.runtime must be an object`);
  assertRoutingKeys(value.runtime, ["runtime_name", "runtime_version", "adapter_id", "adapter_version"], `${context}.runtime`);
  if (
    value.runtime.runtime_name !== "vllm"
    || requiredString(value.runtime, "runtime_version", `${context}.runtime`) !== "0.26.0"
    || !requiredString(value.runtime, "adapter_id", `${context}.runtime`)
    || !requiredString(value.runtime, "adapter_version", `${context}.runtime`)
  ) {
    throw protocolError(`${context}.runtime is unsupported`);
  }
  if (!isRecord(value.topology)) throw protocolError(`${context}.topology must be an object`);
  assertRoutingKeys(value.topology, [
    "accelerator_model", "accelerator_count", "runner_separate_from_serving",
    "serving_engine_count", "one_engine_per_endpoint", "declared_provider",
    "declared_provisioning", "identity_assertion", "lifecycle_protection",
  ], `${context}.topology`);
  const acceleratorModel = requiredString(value.topology, "accelerator_model", `${context}.topology`);
  if (
    requiredInteger(value.topology, "accelerator_count", `${context}.topology`) < 0
    || value.topology.runner_separate_from_serving !== true
    || value.topology.serving_engine_count !== 2
    || value.topology.one_engine_per_endpoint !== true
    || !acceleratorModel
  ) {
    throw protocolError(`${context}.topology disagrees with the two-engine boundary`);
  }
  const manual = value.mode === "LAMBDA_MANUAL_HOST";
  if (
    (manual && (
      value.topology.accelerator_model !== "NVIDIA A100-PCIE-40GB"
      || value.topology.accelerator_count !== 2
      || value.topology.declared_provider !== "LAMBDA"
      || value.topology.declared_provisioning !== "OPERATOR_SUPPLIED_VM"
      || value.topology.identity_assertion !== "OPERATOR_DECLARED_NOT_OBSERVED"
      || value.topology.lifecycle_protection !== "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY"
    ))
    || (!manual && (
      value.topology.declared_provider !== null
      || value.topology.declared_provisioning !== null
      || value.topology.identity_assertion !== "NOT_RETAINED_BY_V1"
      || value.topology.lifecycle_protection !== "NOT_RETAINED_BY_V1"
    ))
  ) {
    throw protocolError(`${context}.topology provider claim is unsupported`);
  }
  const policies = stringArray(value, "policy_ids", context);
  if (
    policies.length !== ROUTING_POLICY_IDS.length
    || policies.some((policy, index) => policy !== ROUTING_POLICY_IDS[index])
  ) {
    throw protocolError(`${context}.policy_ids are not the fixed R1 order`);
  }
  return value as unknown as RoutingExecutionSummary;
}

function parseRejectedExecution(value: unknown, context: string): RejectedRoutingExecution {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, ["entry", "status", "code", "message"], context);
  const message = requiredString(value, "message", context);
  if (
    value.entry !== "<configured-root>"
    || value.status !== "REJECTED"
    || !["CONFIGURATION_INVALID", "UNSAFE_ENTRY", "VERIFICATION_FAILED"].includes(value.code as string)
    || message.length < 1
    || message.length > 160
  ) {
    throw protocolError(`${context} is not a bounded routing-execution rejection`);
  }
  return value as unknown as RejectedRoutingExecution;
}

function parseRoutingExecutionPage(payload: unknown): RoutingExecutionPageResponse {
  if (!isRecord(payload)) throw protocolError("the routing-execution index is not an object");
  assertRoutingKeys(payload, ["projection_version", "routing_executions", "rejected", "page"], "routing-execution index");
  if (
    payload.projection_version !== ROUTING_EXECUTION_PROJECTION
    || !Array.isArray(payload.routing_executions)
    || !Array.isArray(payload.rejected)
    || payload.routing_executions.length + payload.rejected.length > 1
  ) {
    throw protocolError("routing-execution index is outside the one-root boundary");
  }
  payload.routing_executions.forEach((entry, index) => parseExecutionSummary(entry, `routing_executions[${index}]`));
  payload.rejected.forEach((entry, index) => parseRejectedExecution(entry, `rejected[${index}]`));
  if (!isRecord(payload.page)) throw protocolError("routing-execution page metadata is missing");
  assertRoutingKeys(payload.page, ["limit", "returned", "total", "has_more", "next_cursor"], "routing-execution page");
  if (
    requiredInteger(payload.page, "limit", "routing-execution page", 1) > ROUTING_EXECUTION_PAGE_LIMIT
    || requiredInteger(payload.page, "returned", "routing-execution page") !== payload.routing_executions.length + payload.rejected.length
    || requiredInteger(payload.page, "total", "routing-execution page") !== payload.routing_executions.length + payload.rejected.length
    || payload.page.has_more !== false
    || payload.page.next_cursor !== null
  ) {
    throw protocolError("routing-execution page metadata is invalid");
  }
  return payload as unknown as RoutingExecutionPageResponse;
}

function parseExecutionTelemetry(
  value: unknown,
  context: string,
  expectedSignal: "HEALTH" | "LOAD" | "GPU_DCGM" | "KV_CACHE",
  expectedEndpoint: RoutingEndpointId,
  expectedDecisionAt: number,
): RoutingExecutionTelemetryView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, [
    "signal", "observer_id", "endpoint_id", "epoch", "sampled_at_monotonic_ns",
    "decision_at_monotonic_ns", "age_ns", "freshness_bound_ns", "state",
    "admissibility", "value", "source",
  ], context);
  const sampledAt = requiredInteger(value, "sampled_at_monotonic_ns", context);
  const decisionAt = requiredInteger(value, "decision_at_monotonic_ns", context);
  const age = requiredInteger(value, "age_ns", context);
  const bound = requiredInteger(value, "freshness_bound_ns", context);
  if (
    value.signal !== expectedSignal
    || routingEndpointId(value.endpoint_id, `${context}.endpoint_id`) !== expectedEndpoint
    || !requiredString(value, "observer_id", context)
    || requiredInteger(value, "epoch", context) < 0
    || decisionAt !== expectedDecisionAt
    || decisionAt < sampledAt
    || age !== decisionAt - sampledAt
    || !["AVAILABLE", "STALE", "UNAVAILABLE"].includes(value.state as string)
    || !["ADMISSIBLE", "INADMISSIBLE"].includes(value.admissibility as string)
    || !["HTTP_HEALTH", "VLLM_METRICS", "UNAVAILABLE_CAPABILITY"].includes(value.source as string)
  ) {
    throw protocolError(`${context} is not a complete bound telemetry receipt`);
  }
  if (value.state === "AVAILABLE" && (value.admissibility !== "ADMISSIBLE" || age > bound)) {
    throw protocolError(`${context} available telemetry freshness disagrees`);
  }
  if (value.state === "STALE" && (value.admissibility !== "INADMISSIBLE" || age <= bound)) {
    throw protocolError(`${context} stale telemetry freshness disagrees`);
  }
  if (
    value.state === "UNAVAILABLE"
    && (value.admissibility !== "INADMISSIBLE" || value.value !== "UNAVAILABLE")
  ) {
    throw protocolError(`${context} unavailable telemetry was fabricated`);
  }
  const expectedSource = expectedSignal === "HEALTH"
    ? "HTTP_HEALTH"
    : expectedSignal === "LOAD"
      ? "VLLM_METRICS"
      : "UNAVAILABLE_CAPABILITY";
  if (value.source !== expectedSource) {
    throw protocolError(`${context} telemetry source is not bound to its signal`);
  }
  if (
    typeof value.value !== "number"
    && value.value !== "HEALTHY"
    && value.value !== "UNAVAILABLE"
  ) {
    throw protocolError(`${context}.value is unsupported`);
  }
  if (
    typeof value.value === "number"
    && (!Number.isSafeInteger(value.value) || value.value < 0)
  ) {
    throw protocolError(`${context}.value is not a precise non-negative integer`);
  }
  return value as unknown as RoutingExecutionTelemetryView;
}

function parseExecutionCandidate(
  value: unknown,
  context: string,
  expectedEndpoint: RoutingEndpointId,
  decisionAt: number,
): RoutingExecutionCandidateView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, ["endpoint_id", "eligible", "health", "load", "gpu_dcgm", "kv_cache"], context);
  if (routingEndpointId(value.endpoint_id, `${context}.endpoint_id`) !== expectedEndpoint || typeof value.eligible !== "boolean") {
    throw protocolError(`${context} candidate identity is invalid`);
  }
  parseExecutionTelemetry(value.health, `${context}.health`, "HEALTH", expectedEndpoint, decisionAt);
  parseExecutionTelemetry(value.load, `${context}.load`, "LOAD", expectedEndpoint, decisionAt);
  parseExecutionTelemetry(value.gpu_dcgm, `${context}.gpu_dcgm`, "GPU_DCGM", expectedEndpoint, decisionAt);
  parseExecutionTelemetry(value.kv_cache, `${context}.kv_cache`, "KV_CACHE", expectedEndpoint, decisionAt);
  return value as unknown as RoutingExecutionCandidateView;
}

function parseExecutionTerminal(
  value: unknown,
  context: string,
  decisionId: string,
  selectedEndpoint: RoutingEndpointId | null,
): RoutingExecutionTerminalView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, [
    "terminal_outcome_id", "decision_id", "selected_endpoint_id", "status", "reason",
    "started_at_monotonic_ns", "ended_at_monotonic_ns", "http_status", "attempt_count",
  ], context);
  const status = value.status;
  const started = requiredInteger(value, "started_at_monotonic_ns", context);
  const ended = requiredInteger(value, "ended_at_monotonic_ns", context);
  const httpStatus = nullableInteger(value, "http_status", context);
  if (
    !requiredString(value, "terminal_outcome_id", context)
    || value.decision_id !== decisionId
    || value.selected_endpoint_id !== selectedEndpoint
    || !ROUTING_TERMINAL_STATUSES.includes(status as typeof ROUTING_TERMINAL_STATUSES[number])
    || !requiredString(value, "reason", context)
    || ended < started
    || (value.attempt_count !== 0 && value.attempt_count !== 1)
    || (status === "NO_SAFE_ROUTE" && (selectedEndpoint !== null || value.attempt_count !== 0 || httpStatus !== null))
    || (status !== "NO_SAFE_ROUTE" && selectedEndpoint === null)
    || (value.attempt_count === 0 && status !== "CANCELLED" && status !== "NO_SAFE_ROUTE")
    || (value.attempt_count === 0 && httpStatus !== null)
  ) {
    throw protocolError(`${context} terminal closure is invalid`);
  }
  return value as unknown as RoutingExecutionTerminalView;
}

function parseExecutionTrial(
  value: unknown,
  context: string,
  expectedPolicy: string,
): RoutingExecutionTrialView {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  assertRoutingKeys(value, ["trial_id", "policy_id", "reset", "fault", "requests", "terminal_population", "terminal_population_total"], context);
  if (value.policy_id !== expectedPolicy || !requiredString(value, "trial_id", context)) {
    throw protocolError(`${context} policy trial identity is invalid`);
  }
  if (!isRecord(value.reset)) throw protocolError(`${context}.reset must be an object`);
  assertRoutingKeys(value.reset, [
    "reset_at_monotonic_ns", "observer_epochs", "runner_connection_state_cleared",
    "runner_telemetry_state_cleared", "endpoint_engine_reset_assertion", "endpoint_runtime_identities",
  ], `${context}.reset`);
  if (
    requiredInteger(value.reset, "reset_at_monotonic_ns", `${context}.reset`) < 0
    || value.reset.runner_connection_state_cleared !== true
    || value.reset.runner_telemetry_state_cleared !== true
    || value.reset.endpoint_engine_reset_assertion !== "NOT_ASSERTED_SEPARATE_SERVING_ENGINE"
    || !Array.isArray(value.reset.observer_epochs)
    || value.reset.observer_epochs.length !== 4
    || !Array.isArray(value.reset.endpoint_runtime_identities)
    || value.reset.endpoint_runtime_identities.length !== 2
  ) {
    throw protocolError(`${context}.reset is not the bounded runner-only reset receipt`);
  }
  const signals = ["GPU_DCGM", "HEALTH", "KV_CACHE", "LOAD"] as const;
  value.reset.observer_epochs.forEach((entry, index) => {
    if (!isRecord(entry) || entry.signal !== signals[index] || requiredInteger(entry, "epoch", `${context}.reset.observer_epochs[${index}]`) < 0) {
      throw protocolError(`${context}.reset observer epoch inventory is invalid`);
    }
  });
  parseExecutionEndpoint(value.reset.endpoint_runtime_identities[0], `${context}.reset.endpoint_runtime_identities[0]`, "endpoint-a");
  parseExecutionEndpoint(value.reset.endpoint_runtime_identities[1], `${context}.reset.endpoint_runtime_identities[1]`, "endpoint-b");
  if (!isRecord(value.fault)) throw protocolError(`${context}.fault must be an object`);
  assertRoutingKeys(value.fault, ["fault_id", "activated_at_sequence_index", "activated_at_monotonic_ns", "load_collection_paused", "health_collection_continues"], `${context}.fault`);
  if (
    value.fault.fault_id !== "stale-load-fresh-health-v1"
    || value.fault.activated_at_sequence_index !== 2
    || requiredInteger(value.fault, "activated_at_monotonic_ns", `${context}.fault`) < 0
    || value.fault.load_collection_paused !== true
    || value.fault.health_collection_continues !== true
  ) {
    throw protocolError(`${context}.fault is outside the stale-load/fresh-health boundary`);
  }
  if (!Array.isArray(value.requests) || value.requests.length !== 6) {
    throw protocolError(`${context}.requests do not close the six-request trace`);
  }
  const requests = value.requests.map((request, index) => {
    const requestContext = `${context}.requests[${index}]`;
    if (!isRecord(request)) throw protocolError(`${requestContext} must be an object`);
    assertRoutingKeys(request, [
      "request_id", "sequence_index", "decision_id", "decision_at_monotonic_ns", "candidates",
      "selected_endpoint_id", "claims_used", "claims_permitted_stale", "claims_discarded",
      "fallback_reason", "terminal",
    ], requestContext);
    const decisionId = requiredString(request, "decision_id", requestContext);
    const selected = request.selected_endpoint_id === null
      ? null
      : routingEndpointId(request.selected_endpoint_id, `${requestContext}.selected_endpoint_id`);
    const decisionAt = requiredInteger(request, "decision_at_monotonic_ns", requestContext);
    if (
      request.request_id !== `request-${String(index).padStart(3, "0")}`
      || request.sequence_index !== index
      || !decisionId
      || !Array.isArray(request.candidates)
      || request.candidates.length !== 2
      || !["NONE", "REQUIRED_LOAD_STALE", "REQUIRED_LOAD_UNAVAILABLE", "STALE_LOAD_FAIL_OPEN", "HEALTH_ONLY_TIE_BREAK", "HEALTH_NOT_ADMISSIBLE"].includes(request.fallback_reason as string)
    ) {
      throw protocolError(`${requestContext} is outside the fixed request trace`);
    }
    parseExecutionCandidate(request.candidates[0], `${requestContext}.candidates[0]`, "endpoint-a", decisionAt);
    parseExecutionCandidate(request.candidates[1], `${requestContext}.candidates[1]`, "endpoint-b", decisionAt);
    for (const key of ["claims_used", "claims_permitted_stale", "claims_discarded"] as const) {
      const claims = stringArray(request, key, requestContext);
      if (claims.length > 3 || new Set(claims).size !== claims.length) {
        throw protocolError(`${requestContext}.${key} exceeds its bounded claim inventory`);
      }
    }
    parseExecutionTerminal(request.terminal, `${requestContext}.terminal`, decisionId, selected);
    return request as unknown as RoutingExecutionRequestView;
  });
  if (!Array.isArray(value.terminal_population) || value.terminal_population.length !== ROUTING_TERMINAL_STATUSES.length) {
    throw protocolError(`${context}.terminal_population is incomplete`);
  }
  let total = 0;
  value.terminal_population.forEach((entry, index) => {
    if (!isRecord(entry) || entry.status !== ROUTING_TERMINAL_STATUSES[index]) {
      throw protocolError(`${context}.terminal_population status order is invalid`);
    }
    total += requiredInteger(entry, "count", `${context}.terminal_population[${index}]`);
  });
  if (value.terminal_population_total !== 6 || total !== 6 || new Set(requests.map((request) => request.decision_id)).size !== 6) {
    throw protocolError(`${context}.terminal population or decision closure is invalid`);
  }
  return value as unknown as RoutingExecutionTrialView;
}

function parseRoutingExecutionDetail(payload: unknown): RoutingExecutionDetail {
  if (!isRecord(payload)) throw protocolError("the routing-execution detail is not an object");
  assertRoutingKeys(payload, ["projection_version", "summary", "evidence", "campaign", "trials", "interpretation_boundary"], "routing-execution detail");
  if (payload.projection_version !== ROUTING_EXECUTION_PROJECTION || payload.interpretation_boundary !== "MEASUREMENT_EVIDENCE_ONLY") {
    throw protocolError("routing-execution detail projection or boundary is unsupported");
  }
  const summary = parseExecutionSummary(payload.summary, "routing-execution summary");
  if (!isRecord(payload.evidence)) throw protocolError("routing-execution evidence must be an object");
  assertRoutingKeys(payload.evidence, ["runner_image", "serving_image", "endpoints", "input_transfer_receipt_sha256", "input_transfer"], "routing-execution evidence");
  for (const key of ["runner_image", "serving_image"] as const) {
    const reference = requiredString(payload.evidence, key, "routing-execution evidence");
    if (!/^[-a-z0-9./]+@sha256:[0-9a-f]{64}$/.test(reference)) {
      throw protocolError(`routing-execution evidence.${key} must be an immutable OCI reference`);
    }
  }
  if (!Array.isArray(payload.evidence.endpoints) || payload.evidence.endpoints.length !== 2) {
    throw protocolError("routing-execution evidence endpoint inventory is invalid");
  }
  parseExecutionEndpoint(payload.evidence.endpoints[0], "routing-execution evidence.endpoints[0]", "endpoint-a");
  parseExecutionEndpoint(payload.evidence.endpoints[1], "routing-execution evidence.endpoints[1]", "endpoint-b");
  executionDigest(payload.evidence.input_transfer_receipt_sha256, "routing-execution evidence.input_transfer_receipt_sha256");
  if (!isRecord(payload.evidence.input_transfer)) throw protocolError("routing-execution input transfer must be an object");
  assertRoutingKeys(payload.evidence.input_transfer, ["config_sha256", "selected_workload_sha256", "workload_size_bytes", "declared_input_transfer_sha256", "verified_before_transport"], "routing-execution input transfer");
  for (const key of ["config_sha256", "selected_workload_sha256", "declared_input_transfer_sha256"] as const) executionDigest(payload.evidence.input_transfer[key], `routing-execution input transfer.${key}`);
  if (requiredInteger(payload.evidence.input_transfer, "workload_size_bytes", "routing-execution input transfer", 1) < 1 || payload.evidence.input_transfer.verified_before_transport !== true) {
    throw protocolError("routing-execution input transfer is not verified before transport");
  }
  if (!isRecord(payload.campaign)) throw protocolError("routing-execution campaign must be an object");
  assertRoutingKeys(payload.campaign, ["routing_inputs", "workload", "telemetry", "fault"], "routing-execution campaign");
  if (!isRecord(payload.campaign.routing_inputs) || !isRecord(payload.campaign.workload) || !isRecord(payload.campaign.telemetry) || !isRecord(payload.campaign.fault)) {
    throw protocolError("routing-execution campaign records are invalid");
  }
  assertRoutingKeys(payload.campaign.routing_inputs, ["campaign_id", "plan_sha256", "trace_sha256", "fault_schedule_sha256", "trial_plan_sha256", "policies"], "routing-execution routing inputs");
  if (payload.campaign.routing_inputs.campaign_id !== "routing-campaign-v1") throw protocolError("routing-execution campaign identity is invalid");
  for (const key of ["plan_sha256", "trace_sha256", "fault_schedule_sha256", "trial_plan_sha256"] as const) executionDigest(payload.campaign.routing_inputs[key], `routing-execution routing inputs.${key}`);
  const policies = stringArray(payload.campaign.routing_inputs, "policies", "routing-execution routing inputs");
  if (policies.length !== 3 || policies.some((policy, index) => policy !== summary.policy_ids[index])) throw protocolError("routing-execution policy binding is invalid");
  assertRoutingKeys(payload.campaign.workload, ["workload_id", "workload_sha256", "selected_workload_sha256", "selected_request_ids", "request_denominator"], "routing-execution workload");
  for (const key of ["workload_id"] as const) requiredString(payload.campaign.workload, key, "routing-execution workload");
  for (const key of ["workload_sha256", "selected_workload_sha256"] as const) executionDigest(payload.campaign.workload[key], `routing-execution workload.${key}`);
  const selectedRequestIds = stringArray(payload.campaign.workload, "selected_request_ids", "routing-execution workload");
  if (selectedRequestIds.length !== 6 || selectedRequestIds.some((value, index) => value !== `request-${String(index).padStart(3, "0")}`) || payload.campaign.workload.request_denominator !== 6) throw protocolError("routing-execution workload trace is invalid");
  assertRoutingKeys(payload.campaign.telemetry, ["clock_domain", "health_freshness_ms", "load_freshness_ms", "gpu_freshness_ms", "load_metric_name"], "routing-execution telemetry");
  if (payload.campaign.telemetry.clock_domain !== "RUNNER_MONOTONIC_NS" || payload.campaign.telemetry.health_freshness_ms !== 5 || payload.campaign.telemetry.load_freshness_ms !== 5 || payload.campaign.telemetry.gpu_freshness_ms !== 5 || payload.campaign.telemetry.load_metric_name !== "vllm:num_requests_running") throw protocolError("routing-execution telemetry contract is invalid");
  assertRoutingKeys(payload.campaign.fault, ["fault_id", "load_collection_pause_after_sequence_index", "health_collection_continues", "inter_request_interval_ms"], "routing-execution fault");
  if (payload.campaign.fault.fault_id !== "stale-load-fresh-health-v1" || payload.campaign.fault.load_collection_pause_after_sequence_index !== 1 || payload.campaign.fault.health_collection_continues !== true || requiredInteger(payload.campaign.fault, "inter_request_interval_ms", "routing-execution fault", 1) < 1) throw protocolError("routing-execution fault contract is invalid");
  if (!Array.isArray(payload.trials) || payload.trials.length !== 3) throw protocolError("routing-execution trial inventory is invalid");
  const trials = payload.trials.map((trial, index) => parseExecutionTrial(trial, `routing-execution trials[${index}]`, summary.policy_ids[index]!));
  if (new Set(trials.map((trial) => trial.trial_id)).size !== 3) throw protocolError("routing-execution trials repeat an identity");
  return payload as unknown as RoutingExecutionDetail;
}

async function fetchRoutingQualificationEvidence(
  qualificationId: string,
  expectedDigest: string,
  signal?: AbortSignal,
): Promise<{ readonly content: Blob; readonly digest: string }> {
  let response: Response;
  try {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (dashboardToken !== null) headers.Authorization = `Bearer ${dashboardToken}`;
    response = await fetch(
      `${API_ROOT}/routing-qualifications/${encodeURIComponent(qualificationId)}/evidence`,
      { method: "GET", headers, signal },
    );
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("The local Inferdrome dashboard service is unavailable. Start it and try again.", 0);
  }
  if (!response.ok) {
    throw new ApiError(await readError(response), response.status, response.headers.get("x-request-id"));
  }
  if (
    response.headers.get("content-type") !== "application/json"
    || response.headers.get("content-disposition") !== 'attachment; filename="stale-telemetry-qualification-v1.json"'
  ) {
    throw protocolError("the qualification evidence download headers are unsupported");
  }
  const digest = qualificationDigest(
    response.headers.get("x-inferdrome-evidence-digest"),
    "qualification evidence digest",
  );
  if (digest !== expectedDigest) {
    throw protocolError("the qualification evidence digest does not match the rendered descriptor");
  }
  const content = await response.blob();
  if (content.size < 1 || content.size > ROUTING_QUALIFICATION_MAX_BYTES) {
    throw protocolError("the qualification evidence download size is invalid");
  }
  return { content, digest };
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

function parseControlledExecution(
  value: unknown,
  plan: ControlledComparisonPlanView,
  context: string,
): ControlledComparisonDetail["execution"] {
  if (!isRecord(value)) throw protocolError(`${context} must be an object`);
  const allowedKeys = new Set([
    "status",
    "result_published",
    "completed_run_count",
    "planned_run_count",
    "next_sequence_index",
    "exact_schedule_prefix",
    "issue",
    "slots",
  ]);
  if (Object.keys(value).some((key) => !allowedKeys.has(key))) {
    throw protocolError(`${context} contains a field outside the progress allowlist`);
  }
  const statuses = [
    "NOT_STARTED",
    "PARTIAL",
    "BLOCKED",
    "EVIDENCE_COMPLETE",
  ];
  if (!statuses.includes(value.status as string)) {
    throw protocolError(`${context}.status is unknown`);
  }
  if (typeof value.result_published !== "boolean") {
    throw protocolError(`${context}.result_published must be boolean`);
  }
  const planned = requiredInteger(value, "planned_run_count", context, 4);
  const completed = requiredInteger(value, "completed_run_count", context);
  if (planned !== plan.ordered_schedule.length || completed > planned) {
    throw protocolError(`${context} run counts disagree with the frozen schedule`);
  }
  const next = value.next_sequence_index;
  if (
    next !== null && (
      typeof next !== "number" ||
      !Number.isInteger(next) ||
      next < 0 ||
      next >= planned
    )
  ) {
    throw protocolError(`${context}.next_sequence_index is invalid`);
  }
  if (typeof value.exact_schedule_prefix !== "boolean") {
    throw protocolError(`${context}.exact_schedule_prefix must be boolean`);
  }
  const issue = nullableString(value, "issue", context);
  if (issue !== null && issue !== "PROGRESS_INSPECTION_FAILED") {
    throw protocolError(`${context}.issue is unknown`);
  }
  if (!Array.isArray(value.slots) || value.slots.length !== planned) {
    throw protocolError(`${context}.slots must cover the frozen schedule`);
  }
  const states = new Set([
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
  ]);
  let observedCompleted = 0;
  let noncompleteSeen = false;
  let pendingSeen = false;
  let locallyExactPrefix = true;
  let firstNoncompleteIndex: number | null = null;
  let hasBlockingSlot = false;
  let hasOperationalSlot = false;
  value.slots.forEach((slot, index) => {
    if (!isRecord(slot)) throw protocolError(`${context}.slots[${index}] must be an object`);
    const allowedSlotKeys = new Set(["sequence_index", "run_id", "state", "verified_bundle"]);
    if (Object.keys(slot).some((key) => !allowedSlotKeys.has(key))) {
      throw protocolError(`${context}.slots[${index}] contains an unknown field`);
    }
    if (
      requiredInteger(slot, "sequence_index", `${context}.slots[${index}]`) !== index ||
      requiredString(slot, "run_id", `${context}.slots[${index}]`) !== plan.ordered_schedule[index].run_id ||
      !states.has(slot.state as string) ||
      typeof slot.verified_bundle !== "boolean" ||
      slot.verified_bundle !== (slot.state === "COMPLETE")
    ) {
      throw protocolError(`${context}.slots[${index}] disagrees with verified progress`);
    }
    if (slot.state === "PENDING") {
      pendingSeen = true;
    } else if (pendingSeen) {
      locallyExactPrefix = false;
    }
    if (slot.state === "COMPLETE") {
      observedCompleted += 1;
      if (noncompleteSeen) locallyExactPrefix = false;
    } else {
      noncompleteSeen = true;
      firstNoncompleteIndex ??= index;
      if (["FAILED", "INTERRUPTED", "INVALID"].includes(slot.state as string)) {
        hasBlockingSlot = true;
      }
      if (slot.state !== "PENDING") hasOperationalSlot = true;
    }
  });
  if (
    observedCompleted !== completed ||
    (value.exact_schedule_prefix && !locallyExactPrefix)
  ) {
    throw protocolError(`${context} completion arithmetic is inconsistent`);
  }
  if (
    (value.status === "NOT_STARTED" && (
      completed !== 0 ||
      next !== 0 ||
      issue !== null ||
      !value.exact_schedule_prefix ||
      hasOperationalSlot
    )) ||
    (value.status === "EVIDENCE_COMPLETE" && (
      completed !== planned || next !== null || !value.exact_schedule_prefix || issue !== null
    )) ||
    (value.status === "BLOCKED" && (
      next !== null ||
      (!hasBlockingSlot && value.exact_schedule_prefix) ||
      (!hasBlockingSlot && completed === 0 && !hasOperationalSlot && issue === null) ||
      (issue !== null && (
        issue !== "PROGRESS_INSPECTION_FAILED" ||
        completed !== 0 ||
        value.exact_schedule_prefix ||
        value.slots.some((slot) => !isRecord(slot) || slot.state !== "INVALID")
      ))
    )) ||
    (value.status === "PARTIAL" && (
      completed >= planned ||
      next === null ||
      next !== firstNoncompleteIndex ||
      issue !== null ||
      !value.exact_schedule_prefix ||
      hasBlockingSlot ||
      (!hasOperationalSlot && completed === 0)
    ))
  ) {
    throw protocolError(`${context}.status disagrees with its slot states`);
  }
  return value as unknown as ControlledComparisonDetail["execution"];
}

function parseControlledComparisonDetail(payload: unknown): ControlledComparisonDetail {
  if (!isRecord(payload) || payload.projection_version !== "inferdrome.dashboard.v1") {
    throw protocolError("the controlled-comparison detail projection version is unsupported");
  }
  const summary = parseControlledSummary(payload.summary, "controlled-comparison summary");
  const plan = parseControlledPlan(payload.plan, "controlled-comparison plan");
  const execution = parseControlledExecution(
    payload.execution,
    plan,
    "controlled-comparison execution",
  );
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
    if (
      summary.result_status !== expectedStatus ||
      baselineTrialSet !== null ||
      candidateTrialSet !== null ||
      execution.result_published
    ) {
      throw protocolError("controlled-comparison missing-result projection is inconsistent");
    }
    return payload as unknown as ControlledComparisonDetail;
  }
  if (
    issue !== null ||
    !execution.result_published ||
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

  async listRoutingCampaigns(signal?: AbortSignal): Promise<RoutingCampaignIndex> {
    const page = parseRoutingCampaignPage(
      await fetchJson(`/routing-campaigns?limit=${ROUTING_CAMPAIGN_PAGE_LIMIT}`, signal),
    );
    return {
      projection_version: ROUTING_CAMPAIGN_PROJECTION,
      routing_campaigns: page.routing_campaigns,
      rejected: page.rejected,
    };
  },

  async getRoutingCampaign(
    campaignId: string,
    signal?: AbortSignal,
  ): Promise<RoutingCampaignDetail> {
    const detail = parseRoutingCampaignDetail(
      await fetchJson(`/routing-campaigns/${encodeURIComponent(campaignId)}`, signal),
    );
    if (detail.summary.campaign_id !== campaignId) {
      throw protocolError("routing-campaign detail identity disagrees with its requested URL");
    }
    return detail;
  },

  async listRoutingExecutions(signal?: AbortSignal): Promise<RoutingExecutionIndex> {
    const page = parseRoutingExecutionPage(
      await fetchJson(`/routing-executions?limit=${ROUTING_EXECUTION_PAGE_LIMIT}`, signal),
    );
    return {
      projection_version: ROUTING_EXECUTION_PROJECTION,
      routing_executions: page.routing_executions,
      rejected: page.rejected,
    };
  },

  async getRoutingExecution(
    executionId: string,
    signal?: AbortSignal,
  ): Promise<RoutingExecutionDetail> {
    const detail = parseRoutingExecutionDetail(
      await fetchJson(`/routing-executions/${encodeURIComponent(executionId)}`, signal),
    );
    if (detail.summary.execution_id !== executionId) {
      throw protocolError("routing-execution detail identity disagrees with its requested URL");
    }
    return detail;
  },

  async listRoutingQualifications(signal?: AbortSignal): Promise<RoutingQualificationIndex> {
    const page = parseRoutingQualificationPage(
      await fetchJson(`/routing-qualifications?limit=${ROUTING_QUALIFICATION_PAGE_LIMIT}`, signal),
    );
    return {
      projection_version: ROUTING_QUALIFICATION_PROJECTION,
      routing_qualifications: page.routing_qualifications,
      rejected: page.rejected,
    };
  },

  async getRoutingQualification(
    qualificationId: string,
    signal?: AbortSignal,
  ): Promise<RoutingQualificationDetail> {
    const detail = parseRoutingQualificationDetail(
      await fetchJson(`/routing-qualifications/${encodeURIComponent(qualificationId)}`, signal),
    );
    if (detail.summary.qualification_id !== qualificationId) {
      throw protocolError("routing-qualification detail identity disagrees with its requested URL");
    }
    return detail;
  },

  async downloadRoutingQualification(
    qualificationId: string,
    expectedDigest: string,
    signal?: AbortSignal,
  ): Promise<string> {
    const evidence = await fetchRoutingQualificationEvidence(
      qualificationId,
      expectedDigest,
      signal,
    );
    const url = URL.createObjectURL(evidence.content);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "stale-telemetry-qualification-v1.json";
    anchor.style.display = "none";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
    return evidence.digest;
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
