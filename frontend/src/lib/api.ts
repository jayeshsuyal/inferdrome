import type {
  Comparison,
  MetricView,
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
};
