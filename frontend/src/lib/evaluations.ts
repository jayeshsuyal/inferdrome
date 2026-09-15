import { ApiError, fetchBoundedDashboardJson } from "./api";

export const EVALUATION_VERSION = "inferdrome.evaluation-dashboard.v1";
export const EVALUATION_POLICIES = [
  "evaluation_round_robin_v1",
  "evaluation_least_reported_load_v1",
  "evaluation_freshness_fallback_v1",
  "evaluation_fail_closed_v1"
] as const;
export type EvaluationPolicyId = typeof EVALUATION_POLICIES[number];
export type EvaluationKind = "STUDY" | "PREFIX_CACHE";
export interface EvaluationMetric {
  readonly key: string;
  readonly label: string;
  readonly value: string | null;
  readonly unit: "count" | "ns" | "ratio" | "requests/s" | "tokens" | "blocks";
}
export interface EvaluationLatency {
  readonly label: string;
  readonly population: string;
  readonly count: number;
  readonly p50_ns: string | null;
  readonly p90_ns: string | null;
  readonly p95_ns: string | null;
  readonly p99_ns: string | null;
  readonly p99_status: "BELOW_REPORTING_FLOOR" | "DESCRIPTIVE_ONLY" | "NOT_A_SUCCESS_LATENCY_POPULATION";
}
export interface EvaluationOutcome {
  readonly outcome: string;
  readonly count: number;
  readonly offered_fraction: string;
}
export interface EvaluationPopulation {
  readonly metrics: readonly EvaluationMetric[];
  readonly outcomes: readonly EvaluationOutcome[];
  readonly latency: readonly EvaluationLatency[];
  readonly usage: readonly EvaluationMetric[];
  readonly cancelled: boolean;
}
export interface EvaluationRecoveryInterval {
  readonly metric: "publication" | "decision" | "dispatch";
  readonly status: "NOT_APPLICABLE" | "RESTORE_NOT_OBSERVED" | "UNOBSERVED_OR_CENSORED" | "OBSERVED";
  readonly duration_ns: string | null;
  readonly observation_horizon_ns: string | null;
}
export interface EvaluationRecovery {
  readonly applicability: "NOT_APPLICABLE_SCENARIO" | "NOT_APPLICABLE_POLICY" | "APPLICABLE";
  readonly origin: "ACTUAL_TELEMETRY_RESTORED_EVENT";
  readonly planned_restore_ns: string | null;
  readonly actual_restore_ns: string | null;
  readonly intervals: readonly EvaluationRecoveryInterval[];
  readonly background_active_at_restore: boolean | null;
}
export interface EvaluationContrast {
  readonly label: string;
  readonly complete_blocks: number;
  readonly mean_rps: string | null;
  readonly lower_rps: string | null;
  readonly upper_rps: string | null;
  readonly interval_status: string;
}
export interface EvaluationPolicy {
  readonly policy_id: EvaluationPolicyId;
  readonly metrics: readonly EvaluationMetric[];
}
export interface EvaluationStratum {
  readonly index: number;
  readonly scenario: string;
  readonly profile_index: number;
  readonly target_endpoint: "endpoint-a" | "endpoint-b" | null;
  readonly metrics: readonly EvaluationMetric[];
  readonly policies: readonly EvaluationPolicy[];
  readonly contrasts: readonly EvaluationContrast[];
}
export interface EvaluationTrial {
  readonly index: number;
  readonly block_index: number;
  readonly profile_index: number;
  readonly policy_id: EvaluationPolicyId;
  readonly scenario: string;
  readonly status: string;
  readonly result_sha256: string;
  readonly metrics: readonly EvaluationMetric[];
  readonly foreground: EvaluationPopulation;
  readonly background: EvaluationPopulation | null;
  readonly recovery: EvaluationRecovery;
}
export interface EvaluationCell {
  readonly index: number;
  readonly condition: "S0" | "S1" | "U0" | "U1";
  readonly workload_family: "SHARED" | "UNIQUE";
  readonly mode: "DECLARED_ENABLED" | "DECLARED_DISABLED";
  readonly status: string;
  readonly reason: string;
  readonly cleanup: string;
  readonly config_sha256: string;
  readonly result_sha256: string | null;
  readonly declarations_consistent: boolean;
  readonly declaration_reasons: readonly string[];
  readonly metrics: readonly EvaluationMetric[];
  readonly population: EvaluationPopulation | null;
  readonly assignment: readonly EvaluationMetric[];
  readonly output_lengths: readonly EvaluationMetric[];
}
export interface EvaluationCacheBlock {
  readonly index: number;
  readonly cells: readonly EvaluationCell[];
  readonly prefix_potential: readonly EvaluationMetric[];
  readonly output_length_differences: readonly EvaluationMetric[];
}
export interface EvaluationSummary {
  readonly report_id: string;
  readonly kind: EvaluationKind;
  readonly label: string;
  readonly report_sha256: string;
  readonly plan_sha256: string;
  readonly config_sha256: string | null;
  readonly source_schema: "inferdrome.evaluation-study-report.v1" | "inferdrome.evaluation-cache-report.v1";
  readonly status: string;
  readonly comparison_status: string;
  readonly reason: string | null;
  readonly calibration: "UNCALIBRATED_REHEARSAL" | "UNAVAILABLE";
  readonly evidence_class: "SYNTHETIC_ONLY" | "LOCAL_MEASUREMENT_ONLY" | null;
  readonly returned_records: number;
  readonly report_integrity: "EXPECTED_DIGEST_MATCH";
  readonly report_contract: "VALIDATED";
  readonly source_replay: "NOT_PERFORMED";
  readonly runtime_verification: "UNVERIFIED";
  readonly evidence_eligible: false;
  readonly tokenizer_reverified_here: false;
}
export interface RejectedEvaluationReport {
  readonly entry: number;
  readonly code: "CONFIGURATION_INVALID" | "REPORT_UNAVAILABLE" | "DIGEST_MISMATCH" | "REPORT_INVALID" | "PROJECTION_LIMIT";
  readonly message: "Configured report was withheld.";
}
export interface EvaluationReportIndex {
  readonly projection_version: typeof EVALUATION_VERSION;
  readonly reports: readonly EvaluationSummary[];
  readonly rejected: readonly RejectedEvaluationReport[];
}
interface EvaluationDetail {
  readonly projection_version: typeof EVALUATION_VERSION;
  readonly summary: EvaluationSummary;
  readonly coverage: readonly EvaluationMetric[];
  readonly reporting: readonly EvaluationMetric[];
  readonly limitations: readonly string[];
}
export interface EvaluationStudyDetail extends EvaluationDetail {
  readonly kind: "STUDY";
  readonly strata: readonly EvaluationStratum[];
  readonly trials: readonly EvaluationTrial[];
}
export interface EvaluationCacheDetail extends EvaluationDetail {
  readonly kind: "PREFIX_CACHE";
  readonly cache_treatment_attribution: "UNVERIFIED";
  readonly workload_verification: "VERIFIED_PINNED_QWEN3" | "SYNTHETIC_TOKENIZER" | "UNAVAILABLE";
  readonly low_replication: boolean;
  readonly preparation_issues: readonly string[];
  readonly blocks: readonly EvaluationCacheBlock[];
  readonly contrasts: readonly EvaluationContrast[];
}
export type EvaluationReportDetail = EvaluationStudyDetail | EvaluationCacheDetail;

// The additive protocol is closed at every object level. No untrusted object is
// rendered before this parser; errors contain only fixed local text.
type Rule = (value: unknown) => boolean;
const count: Rule = (v) => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const integer = (min: number, max: number): Rule => (v) => count(v) && (v as number) >= min && (v as number) <= max;
const text = (pattern: RegExp): Rule => (v) => typeof v === "string" && pattern.test(v);
const label = text(/^[^\u0000-\u001f\u007f]{1,120}$/u);
const code = text(/^[A-Z][A-Z0-9_]{0,95}$/);
const digest = text(/^sha256:[0-9a-f]{64}$/);
const reportId = text(/^ev-[0-9a-f]{64}$/);
const decimal = text(/^-?[0-9]{1,128}(?:\.[0-9]{1,6})?$/);
const bool: Rule = (v) => typeof v === "boolean";
const oneOf = (...choices: readonly unknown[]): Rule => (v) => choices.includes(v);
const optional = (rule: Rule): Rule => (v) => v === null || rule(v);
const array = (rule: Rule, max: number, min = 0): Rule => (v) => Array.isArray(v) && v.length >= min && v.length <= max && v.every(rule);
const object = (shape: Readonly<Record<string, Rule>>): Rule => (v) => {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return false;
  const record = v as Record<string, unknown>;
  const keys = Object.keys(record);
  return keys.length === Object.keys(shape).length && keys.every((key) => Object.hasOwn(shape, key) && shape[key](record[key]));
};
const unique = <T,>(values: readonly T[], key: (value: T) => unknown) => new Set(values.map(key)).size === values.length;
const metric = object({
  key: text(/^[a-z][a-z0-9_]{0,95}$/),
  label,
  value: optional(decimal),
  unit: oneOf("count", "ns", "ratio", "requests/s", "tokens", "blocks")
});
const metrics = (max: number): Rule => (v) => array(metric, max)(v) && unique(v as EvaluationMetric[], (m) => m.key);
const latency = object({
  label,
  population: code,
  count,
  p50_ns: optional(decimal),
  p90_ns: optional(decimal),
  p95_ns: optional(decimal),
  p99_ns: optional(decimal),
  p99_status: oneOf("BELOW_REPORTING_FLOOR", "DESCRIPTIVE_ONLY", "NOT_A_SUCCESS_LATENCY_POPULATION")
});
const outcome = object({
  outcome: oneOf(
    "SUCCESS",
    "REJECTED_CAPACITY",
    "REJECTED_ROUTE",
    "TIMEOUT",
    "HTTP_ERROR",
    "STREAM_ERROR",
    "STREAM_LIMIT",
    "INCOMPLETE_STREAM",
    "TRANSPORT_ERROR",
    "CANCELLED",
    "DRAIN_TIMEOUT",
    "INTERNAL_ERROR"
  ),
  count,
  offered_fraction: decimal
});
const population = object({
  metrics: metrics(32),
  outcomes: array(outcome, 16),
  latency: array(latency, 24),
  usage: metrics(16),
  cancelled: bool
});
const recoveryInterval: Rule = (v) => {
  if (!object({
    metric: oneOf("publication", "decision", "dispatch"),
    status: oneOf("NOT_APPLICABLE", "RESTORE_NOT_OBSERVED", "UNOBSERVED_OR_CENSORED", "OBSERVED"),
    duration_ns: optional(decimal),
    observation_horizon_ns: optional(decimal)
  })(v)) return false;
  const row = v as EvaluationRecoveryInterval;
  return (row.status === "OBSERVED") === (row.duration_ns !== null);
};
const recovery: Rule = (v) => object({
  applicability: oneOf("NOT_APPLICABLE_SCENARIO", "NOT_APPLICABLE_POLICY", "APPLICABLE"),
  origin: oneOf("ACTUAL_TELEMETRY_RESTORED_EVENT"),
  planned_restore_ns: optional(decimal),
  actual_restore_ns: optional(decimal),
  intervals: array(recoveryInterval, 3, 3),
  background_active_at_restore: optional(bool)
})(v)
  && unique((v as EvaluationRecovery).intervals, (row) => row.metric);
const contrast: Rule = (v) => {
  if (!object({
    label,
    complete_blocks: count,
    mean_rps: optional(decimal),
    lower_rps: optional(decimal),
    upper_rps: optional(decimal),
    interval_status: oneOf("AVAILABLE", "DESCRIPTIVE_ONLY", "INCOMPLETE_STUDY", "INSUFFICIENT_TRIAL_REPLICATION", "INSUFFICIENT_COMPLETE_BLOCKS", "SUPPRESSED_COMPARISON")
  })(v)) return false;
  const row = v as EvaluationContrast;
  return (row.lower_rps === null) === (row.upper_rps === null)
    && ["AVAILABLE", "DESCRIPTIVE_ONLY"].includes(row.interval_status) === (row.lower_rps !== null)
    && (row.lower_rps === null || row.mean_rps !== null);
};
const policy = object({ policy_id: oneOf(...EVALUATION_POLICIES), metrics: metrics(8) });
const stratum: Rule = (v) => object({
  index: integer(1, 64),
  scenario: oneOf("HEALTHY", "STALE_LOAD"),
  profile_index: integer(1, 64),
  target_endpoint: optional(oneOf("endpoint-a", "endpoint-b")),
  metrics: metrics(8),
  policies: array(policy, 4, 4),
  contrasts: array(contrast, 3, 3)
})(v)
  && unique((v as EvaluationStratum).policies, (row) => row.policy_id);
const trial = object({
  index: integer(1, 256),
  block_index: integer(1, 64),
  profile_index: integer(1, 64),
  policy_id: oneOf(...EVALUATION_POLICIES),
  scenario: oneOf("HEALTHY", "STALE_LOAD"),
  status: oneOf("COMPLETED", "WARMUP_FAILED", "CANCELLED"),
  result_sha256: digest,
  metrics: metrics(8),
  foreground: population,
  background: optional(population),
  recovery
});
const cell: Rule = (v) => {
  if (!object({
    index: integer(1, 32),
    condition: oneOf("S0", "S1", "U0", "U1"),
    workload_family: oneOf("SHARED", "UNIQUE"),
    mode: oneOf("DECLARED_ENABLED", "DECLARED_DISABLED"),
    status: oneOf("COMPLETED", "CANCELLED", "ABORTED", "UNAVAILABLE"),
    reason: code,
    cleanup: code,
    config_sha256: digest,
    result_sha256: optional(digest),
    declarations_consistent: bool,
    declaration_reasons: array(code, 32),
    metrics: metrics(8),
    population: optional(population),
    assignment: metrics(4),
    output_lengths: metrics(16)
  })(v)) return false;
  const row = v as EvaluationCell;
  return row.workload_family === (row.condition.startsWith("S") ? "SHARED" : "UNIQUE")
    && row.mode === (row.condition.endsWith("1") ? "DECLARED_ENABLED" : "DECLARED_DISABLED");
};
const block: Rule = (v) => object({
  index: integer(1, 8),
  cells: array(cell, 4, 4),
  prefix_potential: metrics(12),
  output_length_differences: metrics(2)
})(v)
  && unique((v as EvaluationCacheBlock).cells, (row) => row.condition)
  && unique((v as EvaluationCacheBlock).cells, (row) => row.index);
const summary: Rule = (v) => {
  if (!object({
    report_id: reportId,
    kind: oneOf("STUDY", "PREFIX_CACHE"),
    label,
    report_sha256: digest,
    plan_sha256: digest,
    config_sha256: optional(digest),
    source_schema: oneOf("inferdrome.evaluation-study-report.v1", "inferdrome.evaluation-cache-report.v1"),
    status: code,
    comparison_status: code,
    reason: optional(code),
    calibration: oneOf("UNCALIBRATED_REHEARSAL", "UNAVAILABLE"),
    evidence_class: optional(oneOf("SYNTHETIC_ONLY", "LOCAL_MEASUREMENT_ONLY")),
    returned_records: count,
    report_integrity: oneOf("EXPECTED_DIGEST_MATCH"),
    report_contract: oneOf("VALIDATED"),
    source_replay: oneOf("NOT_PERFORMED"),
    runtime_verification: oneOf("UNVERIFIED"),
    evidence_eligible: oneOf(false),
    tokenizer_reverified_here: oneOf(false)
  })(v)) return false;
  const row = v as EvaluationSummary;
  return (row.kind === "STUDY"
    ? ["COMPLETED", "ABORTED", "CANCELLED"].includes(row.status) && row.comparison_status === (row.status === "COMPLETED" ? "DESCRIPTIVE_UNCALIBRATED_REHEARSAL" : "SUPPRESSED_INCOMPLETE_STUDY")
    : ["COMPLETED", "INCOMPLETE"].includes(row.status) && ["AVAILABLE", "SUPPRESSED_INCOMPLETE", "SUPPRESSED_DECLARATION_OR_EVIDENCE_MISMATCH"].includes(row.comparison_status))
    && row.source_schema === (row.kind === "STUDY" ? "inferdrome.evaluation-study-report.v1" : "inferdrome.evaluation-cache-report.v1")
    && (row.kind === "STUDY" ? /^Study report [1-8]$/ : /^Prefix-cache report [1-8]$/).test(row.label);
};
const rejected = object({
  entry: integer(1, 8),
  code: oneOf("CONFIGURATION_INVALID", "REPORT_UNAVAILABLE", "DIGEST_MISMATCH", "REPORT_INVALID", "PROJECTION_LIMIT"),
  message: oneOf("Configured report was withheld.")
});
const detailShape = {
  projection_version: oneOf(EVALUATION_VERSION),
  summary,
  coverage: metrics(24),
  reporting: metrics(16),
  limitations: array(code, 16)
};
const study = object({ ...detailShape, kind: oneOf("STUDY"), strata: array(stratum, 64), trials: array(trial, 256) });
const cache = object({
  ...detailShape,
  kind: oneOf("PREFIX_CACHE"),
  cache_treatment_attribution: oneOf("UNVERIFIED"),
  workload_verification: oneOf("VERIFIED_PINNED_QWEN3", "SYNTHETIC_TOKENIZER", "UNAVAILABLE"),
  low_replication: bool,
  preparation_issues: array(code, 128),
  blocks: array(block, 8),
  contrasts: array(contrast, 3, 3)
});
function invalid(): never { throw new ApiError("Inferdrome returned an invalid evaluation report projection.", 502); }

export function parseEvaluationIndex(value: unknown): EvaluationReportIndex {
  if (!object({
    projection_version: oneOf(EVALUATION_VERSION),
    reports: array(summary, 8),
    rejected: array(rejected, 8)
  })(value)) invalid();
  const index = value as EvaluationReportIndex;
  if (!unique(index.reports, (row) => row.report_id) || !unique(index.rejected, (row) => row.entry) || index.reports.length + index.rejected.length > 8) invalid();
  return index;
}
export function parseEvaluationDetail(value: unknown, expectedId: string): EvaluationReportDetail {
  if (!reportId(expectedId) || (!study(value) && !cache(value))) invalid();
  const detail = value as EvaluationReportDetail;
  if (detail.summary.report_id !== expectedId || detail.summary.kind !== detail.kind) invalid();
  if (detail.kind === "STUDY") {
    if (!unique(detail.trials, (row) => row.index) || !unique(detail.strata, (row) => row.index)) invalid();
  } else if (!unique(detail.blocks, (row) => row.index) || !unique(detail.blocks.flatMap((row) => row.cells), (row) => row.index)) invalid();
  return detail;
}

export const evaluationApi = {
  list: async (signal?: AbortSignal): Promise<EvaluationReportIndex> => parseEvaluationIndex(await fetchBoundedDashboardJson("/evaluation-reports", 65_536, signal)),
  detail: async (id: string, signal?: AbortSignal): Promise<EvaluationReportDetail> => {
    if (!reportId(id)) throw new ApiError("This evaluation report is unavailable.", 404);
    return parseEvaluationDetail(await fetchBoundedDashboardJson(`/evaluation-reports/${encodeURIComponent(id)}`, 4 * 1024 * 1024, signal), id);
  },
};
