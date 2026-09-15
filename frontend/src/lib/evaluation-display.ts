import type { EvaluationMetric } from "./evaluations";

export type EvaluationValue = Pick<EvaluationMetric, "value" | "unit">;

// Match the projection's bounded decimal vocabulary before doing string work.
// Neither values nor their integer parts pass through Number or BigInt.
const DECIMAL = /^(-?)([0-9]{1,128})(?:\.([0-9]{1,6}))?$/;
const UNITS: readonly EvaluationMetric["unit"][] = ["count", "ns", "ratio", "requests/s", "tokens", "blocks"];
const TIME_UNITS = ["ns", "μs", "ms", "s"] as const;
interface DecimalParts {
  readonly sign: string;
  readonly integer: string;
  readonly fraction: string;
}

function parseValue(input: EvaluationValue): DecimalParts | null {
  if (typeof input.value !== "string" || input.value.length > 136 || !UNITS.includes(input.unit)) return null;
  const match = DECIMAL.exec(input.value);
  if (!match || match[0] !== input.value) return null;
  return { sign: match[1], integer: match[2].replace(/^0+(?=[0-9])/, ""), fraction: (match[3] ?? "").replace(/0+$/, "") };
}

function decimalText(value: DecimalParts): string {
  return value.sign + value.integer + (value.fraction ? `.${value.fraction}` : "");
}

function shiftTime(value: DecimalParts, places: number): DecimalParts {
  if (places === 0) return value;
  const padded = value.integer.padStart(places + 1, "0");
  return {
    sign: value.sign,
    integer: padded.slice(0, -places),
    fraction: (padded.slice(-places) + value.fraction).replace(/0+$/, "")
  };
}

function incrementDigits(digits: string): string {
  const nextDigit: Readonly<Record<string, string>> = { "0": "1", "1": "2", "2": "3", "3": "4", "4": "5", "5": "6", "6": "7", "7": "8", "8": "9" };
  let index = digits.length - 1;
  while (index >= 0 && digits[index] === "9") index -= 1;
  return index < 0 ? `1${"0".repeat(digits.length)}` : digits.slice(0, index) + nextDigit[digits[index]] + "0".repeat(digits.length - index - 1);
}

function roundTime(value: DecimalParts): { value: DecimalParts; approximate: boolean; } {
  if (value.fraction.length <= 3) return { value, approximate: false };
  // Keep sub-picosecond values exact instead of displaying an observed nonzero as zero.
  if (value.integer === "0" && value.fraction.startsWith("000")) return { value, approximate: false };
  const retained = value.integer + value.fraction.slice(0, 3);
  const rounded = value.fraction[3] >= "5" ? incrementDigits(retained) : retained;
  return {
    value: { sign: value.sign, integer: rounded.slice(0, -3), fraction: rounded.slice(-3).replace(/0+$/, "") },
    approximate: true
  };
}

/** Presentation only: unit shifts are exact; rounded timing values carry ≈. */
export function formatEvaluationValue(input: EvaluationValue | null): string {
  if (input === null || input.value === null) return "Unavailable";
  const value = parseValue(input);
  if (value === null) return "Invalid value";
  if (input.unit !== "ns") {
    return decimalText(value) + (input.unit === "count" ? "" : ` ${input.unit}`);
  }
  let unitIndex = value.integer.length > 9 ? 3 : value.integer.length > 6 ? 2 : value.integer.length > 3 ? 1 : 0;
  let rounded = roundTime(shiftTime(value, unitIndex * 3));
  if (rounded.value.integer === "1000" && unitIndex < 3) {
    unitIndex += 1;
    rounded = roundTime(shiftTime(value, unitIndex * 3));
  }
  return `${rounded.approximate ? "≈ " : ""}${decimalText(rounded.value)} ${TIME_UNITS[unitIndex]}`;
}

/** Keep the source spelling, precision, sign and unit for accessible exact values. */
export function exactEvaluationValue(input: EvaluationValue | null): string {
  if (input === null || input.value === null) return "Unavailable";
  return parseValue(input) === null ? "Invalid value" : `${input.value} ${input.unit}`;
}

const OUTCOME_LABELS = {
  SUCCESS: "Success",
  REJECTED_CAPACITY: "Capacity rejected",
  REJECTED_ROUTE: "Route rejected",
  TIMEOUT: "Timeout",
  HTTP_ERROR: "HTTP error",
  STREAM_ERROR: "Stream error",
  STREAM_LIMIT: "Stream limit",
  INCOMPLETE_STREAM: "Incomplete stream",
  TRANSPORT_ERROR: "Transport error",
  CANCELLED: "Cancelled",
  DRAIN_TIMEOUT: "Drain timeout",
  INTERNAL_ERROR: "Internal error"
} as const;

// Deliberately limited to evaluation vocabulary. Unknown strings retain their
// spelling; in particular, human-authored labels and acronyms are not title-cased.
const EVALUATION_LABELS: Readonly<Record<string, string>> = {
  ...OUTCOME_LABELS,
  ...Object.fromEntries(Object.entries(OUTCOME_LABELS).map(([code, label]) => [`Partial first content: ${code}`, `Partial first content: ${label}`])),
  evaluation_round_robin_v1: "Round robin",
  evaluation_least_reported_load_v1: "Least reported load",
  evaluation_freshness_fallback_v1: "Freshness fallback",
  evaluation_fail_closed_v1: "Fail closed",
  STUDY: "Study",
  PREFIX_CACHE: "Prefix cache",
  HEALTHY: "Healthy",
  STALE_LOAD: "Stale load",
  S0: "S0 · Shared, declared disabled",
  S1: "S1 · Shared, declared enabled",
  U0: "U0 · Unique, declared disabled",
  U1: "U1 · Unique, declared enabled",
  SHARED: "Shared",
  UNIQUE: "Unique",
  DECLARED_ENABLED: "Declared enabled",
  DECLARED_DISABLED: "Declared disabled",
  COMPLETED: "Completed",
  INCOMPLETE: "Incomplete",
  ABORTED: "Aborted",
  WARMUP_FAILED: "Warmup failed",
  AVAILABLE: "Available",
  UNAVAILABLE: "Unavailable",
  UNKNOWN: "Unknown",
  UNSUPPORTED: "Unsupported",
  WITHHELD: "Withheld",
  SUPPRESSED: "Suppressed",
  DESCRIPTIVE_ONLY: "Descriptive only",
  INCOMPLETE_STUDY: "Incomplete study",
  INSUFFICIENT_TRIAL_REPLICATION: "Insufficient trial replication",
  INSUFFICIENT_COMPLETE_BLOCKS: "Insufficient complete blocks",
  SUPPRESSED_COMPARISON: "Comparison suppressed",
  SUPPRESSED_INCOMPLETE: "Suppressed: incomplete report",
  SUPPRESSED_INCOMPLETE_STUDY: "Suppressed: incomplete study",
  SUPPRESSED_DECLARATION_OR_EVIDENCE_MISMATCH: "Suppressed: declaration or evidence mismatch",
  DESCRIPTIVE_UNCALIBRATED_REHEARSAL: "Descriptive, uncalibrated rehearsal",
  UNCALIBRATED_REHEARSAL: "Uncalibrated rehearsal",
  SYNTHETIC_ONLY: "Synthetic only",
  LOCAL_MEASUREMENT_ONLY: "Local measurement only",
  EXPECTED_DIGEST_MATCH: "Expected digest matched",
  VALIDATED: "Validated",
  NOT_PERFORMED: "Not performed",
  UNVERIFIED: "Unverified",
  INELIGIBLE: "Ineligible",
  SYNTHETIC_TOKENIZER: "Synthetic tokenizer",
  VERIFIED_PINNED_QWEN3: "Pinned Qwen3 tokenizer verified in source report",
  CONFIGURATION_INVALID: "Invalid configuration",
  REPORT_UNAVAILABLE: "Report unavailable",
  DIGEST_MISMATCH: "Digest mismatch",
  REPORT_INVALID: "Invalid report",
  PROJECTION_LIMIT: "Projection limit",
  NOT_APPLICABLE: "Not applicable",
  NOT_APPLICABLE_SCENARIO: "Not applicable to this scenario",
  NOT_APPLICABLE_POLICY: "Not applicable to this policy",
  APPLICABLE: "Applicable",
  RESTORE_NOT_OBSERVED: "Restore not observed",
  UNOBSERVED_OR_CENSORED: "Unobserved or censored",
  OBSERVED: "Observed",
  ACTUAL_TELEMETRY_RESTORED_EVENT: "Actual telemetry-restored event",
  publication: "Publication",
  decision: "Decision",
  dispatch: "Dispatch",
  BELOW_REPORTING_FLOOR: "Below reporting floor",
  NOT_A_SUCCESS_LATENCY_POPULATION: "Not a successful latency population",
  ALL_SUCCESS_INCLUDING_SLO_MISSES: "All successful requests, including SLO misses",
  ALL_DISPATCHED_OUTCOMES: "All dispatched outcomes",
  STUDY_DEADLINE: "Study deadline",
  CONTROLLER_OR_CLEANUP_FAILED: "Controller or cleanup failed",
  RESULT_VALIDATION_FAILED: "Result validation failed",
  OUTPUT_FAILED: "Output failed",
  MISSING_CELL_INPUT: "Missing cell input",
  INVALID_CELL_INPUT: "Invalid cell input",
  EXECUTION_FAILED: "Execution failed",
  RESULT_LIMIT: "Result limit",
  DURATION_LIMIT: "Duration limit",
  CONFIRMED_BY_LOCAL_RESULT: "Confirmed by local result",
  UNCONFIRMED: "Unconfirmed",
  NOT_STARTED: "Not started",
  UNKNOWN_CACHE_MODE: "Unknown cache mode",
  UNKNOWN_PROCESS_GENERATION: "Unknown process generation",
  UNKNOWN_RUNTIME_RECIPE: "Unknown runtime recipe",
  UNKNOWN_INITIAL_PREFIX_STATE: "Unknown initial prefix state",
  UNKNOWN_PREPARATION_METHOD: "Unknown preparation method",
  UNKNOWN_WARMUP: "Unknown warmup",
  UNKNOWN_CHRONOLOGY: "Unknown chronology",
  UNCONTROLLED_TRAFFIC: "Uncontrolled traffic",
  TOKENIZATION_UNAVAILABLE: "Tokenization unavailable",
  REUSED_PREPARATION_ID: "Reused preparation ID",
  RUNTIME_RECIPE_UNRESOLVED_OR_MISMATCHED: "Runtime recipe unresolved or mismatched",
  PREPARATION_METHOD_MISMATCH: "Preparation method mismatch",
  PROCESS_GENERATION_UNRESOLVED: "Process generation unresolved",
  REUSED_FRESH_PROCESS_GENERATION: "Reused fresh process generation",
  EVIDENCE_CLASS_MISMATCH: "Evidence class mismatch",
  DIGESTS_CHECK_INTEGRITY_NOT_RUNTIME_ATTESTATION_OR_INDEPENDENT_EXECUTION: "Digests check integrity, not runtime attestation or independent execution.",
  SCHEDULED_ARRIVAL_ORIGIN_AND_FIXED_OFFERED_WINDOW: "Timing starts at scheduled arrival and uses a fixed offered window.",
  REJECTIONS_FAILURES_CANCELLATIONS_REMAIN_IN_OFFERED_DENOMINATOR: "Rejections, failures and cancellations remain in the offered denominator.",
  TRIAL_BLOCK_BOOTSTRAP_ASSUMES_INDEPENDENT_BLOCKS_NOT_REQUESTS: "The trial-block bootstrap assumes independent blocks, not independent requests.",
  P99_FLOOR_IS_NOT_A_PRECISION_OR_INDEPENDENCE_GUARANTEE: "The p99 reporting floor does not guarantee precision or independence.",
  DECLARED_CACHE_PREPARATION_AND_RUNTIME_IDENTITIES_UNVERIFIED: "Declared cache preparation and runtime identities remain unverified.",
  MALFORMED_AIOHTTP_FRAMING_CAN_BE_CLASSIFIED_TIMEOUT: "Malformed aiohttp framing can be classified as a timeout.",
  NO_LIVE_CAPACITY_COST_OR_ACTUAL_OVERLOAD_ESTABLISHED: "Live capacity, cost and actual overload are not established.",
  DECLARATIONS_AND_DIGESTS_DO_NOT_ATTEST_RUNTIME_OR_INDEPENDENT_EXECUTION: "Declarations and digests do not attest runtime or independent execution.",
  ALL_OFFERED_SCHEDULED_ORIGIN_SLOS_WITH_FIXED_WINDOW: "SLOs use scheduled arrival, all offered requests and a fixed window.",
  MISSING_OR_INVALID_CELLS_ARE_UNAVAILABLE_NOT_ZERO: "Missing or invalid cells are unavailable, not zero.",
  WHOLE_BLOCK_BOOTSTRAP_ASSUMES_INDEPENDENT_BLOCKS_NOT_REQUESTS: "The whole-block bootstrap assumes independent blocks, not independent requests.",
  INTERACTION_IS_NOT_A_PREFILL_ONLY_OR_CACHE_HIT_MEASUREMENT: "The interaction is not a prefill-only or cache-hit measurement.",
  GENERATED_LENGTHS_USE_ONLY_VALID_SERVER_USAGE_WITH_EXPLICIT_COVERAGE: "Generated lengths use only valid server usage with explicit coverage.",
  EXTERNAL_PREPARATION_AND_BETWEEN_CELL_TIME_ARE_UNMEASURED: "External preparation and time between cells are unmeasured."
};

export function evaluationLabel(value: string): string {
  return Object.hasOwn(EVALUATION_LABELS, value) ? EVALUATION_LABELS[value] : value;
}
