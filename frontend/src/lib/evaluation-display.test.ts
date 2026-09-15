import { describe, expect, it } from "vitest";

import cache from "../test/evaluations/cache.json";
import cacheEight from "../test/evaluations/cache-eight.json";
import study from "../test/evaluations/study.json";
import { evaluationLabel, exactEvaluationValue, formatEvaluationValue } from "./evaluation-display";
import type { EvaluationValue } from "./evaluation-display";
import { EVALUATION_POLICIES } from "./evaluations";

describe("evaluation values", () => {
  it("distinguishes missing values, observed zero and signed tiny nonzero values", () => {
    expect(formatEvaluationValue(null)).toBe("Unavailable");
    expect(exactEvaluationValue(null)).toBe("Unavailable");
    for (const unit of ["count", "ns", "ratio", "requests/s", "tokens", "blocks"] as const) {
      expect(formatEvaluationValue({ value: null, unit })).toBe("Unavailable");
      expect(exactEvaluationValue({ value: null, unit })).toBe("Unavailable");
      expect(formatEvaluationValue({ value: "0.000000", unit })).toBe(unit === "count" ? "0" : `0 ${unit}`);
      expect(formatEvaluationValue({ value: "0.000001", unit })).toBe(unit === "count" ? "0.000001" : `0.000001 ${unit}`);
      expect(formatEvaluationValue({ value: "-0.000001", unit })).toBe(unit === "count" ? "-0.000001" : `-0.000001 ${unit}`);
    }
  });

  it.each([
    ["1", "1 ns"],
    ["999", "999 ns"],
    ["1000", "1 μs"],
    ["1001", "1.001 μs"],
    ["999999", "999.999 μs"],
    ["1000000", "1 ms"],
    ["1000001", "≈ 1 ms"],
    ["1250000", "1.25 ms"],
    ["1234567", "≈ 1.235 ms"],
    ["1234499", "≈ 1.234 ms"],
    ["1234500", "≈ 1.235 ms"],
    ["-1234500", "≈ -1.235 ms"],
    ["999999499", "≈ 999.999 ms"],
    ["999999500", "≈ 1 s"],
    ["999999999", "≈ 1 s"],
    ["1000000000", "1 s"],
    ["1000000001", "≈ 1 s"],
    ["1001000000", "1.001 s"],
    ["1234567890", "≈ 1.235 s"],
    ["999.999999", "≈ 1 μs"],
    ["999999.999999", "≈ 1 ms"],
    ["999999999.999999", "≈ 1 s"],
    ["0.000001", "0.000001 ns"],
    ["0.000499", "0.000499 ns"],
    ["0.000500", "0.0005 ns"],
    ["-0.000999", "-0.000999 ns"],
    ["0.001000", "0.001 ns"],
    ["0.001001", "≈ 0.001 ns"],
    ["-0.001500", "≈ -0.002 ns"],
    ["000001000.000000", "1 μs"],
    ["-0.000000", "-0 ns"]
  ])("formats %s ns as %s with explicit approximation when needed", (value, expected) => {
    expect(formatEvaluationValue({ value, unit: "ns" })).toBe(expected);
    expect(exactEvaluationValue({ value, unit: "ns" })).toBe(`${value} ns`);
  });

  it("keeps rate and ratio statistics exact instead of rounding or calculating percentages", () => {
    expect(formatEvaluationValue({ value: "22.222222", unit: "requests/s" })).toBe("22.222222 requests/s");
    expect(formatEvaluationValue({ value: "0.625000", unit: "ratio" })).toBe("0.625 ratio");
    expect(formatEvaluationValue({ value: "-0.000001", unit: "requests/s" })).toBe("-0.000001 requests/s");
    const interval = ["-100.000001", "-0.000001", "0.000001"].map((value) => ({ value, unit: "requests/s" as const }));
    expect(interval.map(formatEvaluationValue)).toEqual(["-100.000001 requests/s", "-0.000001 requests/s", "0.000001 requests/s"]);
    expect(interval.map(exactEvaluationValue)).toEqual(["-100.000001 requests/s", "-0.000001 requests/s", "0.000001 requests/s"]);
  });

  it("preserves original source precision and units independently of display formatting", () => {
    for (const unit of ["count", "ns", "ratio", "requests/s", "tokens", "blocks"] as const) {
      const source = Object.freeze({ value: "-000001234.500000", unit });
      expect(exactEvaluationValue(source)).toBe(`-000001234.500000 ${unit}`);
      formatEvaluationValue(source);
      expect(source.value).toBe("-000001234.500000");
    }
    expect(formatEvaluationValue({ value: "16.000000", unit: "tokens" })).toBe("16 tokens");
    expect(formatEvaluationValue({ value: "8", unit: "blocks" })).toBe("8 blocks");
  });

  it("handles values beyond safe integer precision without a numeric conversion", () => {
    for (const value of ["9007199254740992", "9007199254740993", "9007199254740994", "9".repeat(128)]) {
      expect(formatEvaluationValue({ value, unit: "count" })).toBe(value);
      expect(exactEvaluationValue({ value, unit: "count" })).toBe(`${value} count`);
    }
    expect(formatEvaluationValue({ value: "9007199254740993", unit: "ns" })).toBe("≈ 9007199.255 s");
    expect(exactEvaluationValue({ value: "9007199254740993", unit: "ns" })).toBe("9007199254740993 ns");
  });

  it("bounds extreme decimal handling while preserving sign and exact access", () => {
    const digits = "9".repeat(128);
    const extreme = `-${digits}.999999`;
    expect(formatEvaluationValue({ value: extreme, unit: "ns" })).toBe(`≈ -1${"0".repeat(119)} s`);
    expect(exactEvaluationValue({ value: extreme, unit: "ns" })).toBe(`${extreme} ns`);
    expect(formatEvaluationValue({ value: extreme, unit: "requests/s" })).toBe(`${extreme} requests/s`);
    expect(formatEvaluationValue({ value: "0".repeat(128) + ".000001", unit: "ns" })).toBe("0.000001 ns");
    expect(formatEvaluationValue({ value: "1" + "0".repeat(127), unit: "ns" })).toBe(`1${"0".repeat(118)} s`);
  });

  it.each(["", "NaN", "Infinity", "-Infinity", "+1", "1e9", "0x10", "1.", ".1", "1.0000001", "1\n", " 1", "1 ", "1,000", "9".repeat(129), "1".repeat(10000)])("rejects unsupported decimal spelling case %#", (value) => {
    expect(formatEvaluationValue({ value, unit: "ns" })).toBe("Invalid value");
    expect(exactEvaluationValue({ value, unit: "ns" })).toBe("Invalid value");
  });

  it("does not silently coerce numeric or unsupported-unit callers", () => {
    for (const input of [{ value: 1, unit: "ns" }, { value: "1", unit: "seconds" }]) {
      expect(formatEvaluationValue(input as EvaluationValue)).toBe("Invalid value");
      expect(exactEvaluationValue(input as EvaluationValue)).toBe("Invalid value");
    }
  });
});

describe("evaluation labels", () => {
  it("names exactly the four supported study policies", () => {
    expect(EVALUATION_POLICIES.map(evaluationLabel)).toEqual(["Round robin", "Least reported load", "Freshness fallback", "Fail closed"]);
  });

  it.each([
    ["STUDY", "Study"], ["PREFIX_CACHE", "Prefix cache"],
    ["HEALTHY", "Healthy"], ["STALE_LOAD", "Stale load"],
    ["S0", "S0 · Shared, declared disabled"], ["S1", "S1 · Shared, declared enabled"],
    ["U0", "U0 · Unique, declared disabled"], ["U1", "U1 · Unique, declared enabled"],
    ["SHARED", "Shared"], ["UNIQUE", "Unique"],
    ["DECLARED_ENABLED", "Declared enabled"], ["DECLARED_DISABLED", "Declared disabled"],
    ["COMPLETED", "Completed"], ["CANCELLED", "Cancelled"], ["ABORTED", "Aborted"],
    ["INCOMPLETE", "Incomplete"], ["WARMUP_FAILED", "Warmup failed"],
    ["SUCCESS", "Success"], ["REJECTED_CAPACITY", "Capacity rejected"], ["REJECTED_ROUTE", "Route rejected"],
    ["TIMEOUT", "Timeout"], ["HTTP_ERROR", "HTTP error"], ["STREAM_ERROR", "Stream error"],
    ["STREAM_LIMIT", "Stream limit"], ["INCOMPLETE_STREAM", "Incomplete stream"],
    ["TRANSPORT_ERROR", "Transport error"], ["DRAIN_TIMEOUT", "Drain timeout"], ["INTERNAL_ERROR", "Internal error"],
    ["AVAILABLE", "Available"], ["UNAVAILABLE", "Unavailable"], ["UNKNOWN", "Unknown"],
    ["UNSUPPORTED", "Unsupported"], ["WITHHELD", "Withheld"], ["SUPPRESSED", "Suppressed"],
    ["DESCRIPTIVE_ONLY", "Descriptive only"], ["INCOMPLETE_STUDY", "Incomplete study"],
    ["INSUFFICIENT_TRIAL_REPLICATION", "Insufficient trial replication"],
    ["INSUFFICIENT_COMPLETE_BLOCKS", "Insufficient complete blocks"],
    ["SUPPRESSED_COMPARISON", "Comparison suppressed"],
    ["SUPPRESSED_INCOMPLETE", "Suppressed: incomplete report"],
    ["SUPPRESSED_INCOMPLETE_STUDY", "Suppressed: incomplete study"],
    ["SUPPRESSED_DECLARATION_OR_EVIDENCE_MISMATCH", "Suppressed: declaration or evidence mismatch"],
    ["DESCRIPTIVE_UNCALIBRATED_REHEARSAL", "Descriptive, uncalibrated rehearsal"],
    ["UNCALIBRATED_REHEARSAL", "Uncalibrated rehearsal"],
    ["SYNTHETIC_ONLY", "Synthetic only"], ["LOCAL_MEASUREMENT_ONLY", "Local measurement only"],
    ["EXPECTED_DIGEST_MATCH", "Expected digest matched"], ["VALIDATED", "Validated"],
    ["NOT_PERFORMED", "Not performed"], ["UNVERIFIED", "Unverified"], ["INELIGIBLE", "Ineligible"],
    ["SYNTHETIC_TOKENIZER", "Synthetic tokenizer"],
    ["VERIFIED_PINNED_QWEN3", "Pinned Qwen3 tokenizer verified in source report"],
    ["CONFIGURATION_INVALID", "Invalid configuration"], ["REPORT_UNAVAILABLE", "Report unavailable"],
    ["DIGEST_MISMATCH", "Digest mismatch"], ["REPORT_INVALID", "Invalid report"], ["PROJECTION_LIMIT", "Projection limit"],
    ["NOT_APPLICABLE", "Not applicable"], ["NOT_APPLICABLE_SCENARIO", "Not applicable to this scenario"],
    ["NOT_APPLICABLE_POLICY", "Not applicable to this policy"], ["APPLICABLE", "Applicable"],
    ["RESTORE_NOT_OBSERVED", "Restore not observed"], ["UNOBSERVED_OR_CENSORED", "Unobserved or censored"],
    ["OBSERVED", "Observed"], ["ACTUAL_TELEMETRY_RESTORED_EVENT", "Actual telemetry-restored event"],
    ["publication", "Publication"], ["decision", "Decision"], ["dispatch", "Dispatch"],
    ["BELOW_REPORTING_FLOOR", "Below reporting floor"],
    ["NOT_A_SUCCESS_LATENCY_POPULATION", "Not a successful latency population"],
    ["ALL_SUCCESS_INCLUDING_SLO_MISSES", "All successful requests, including SLO misses"],
    ["ALL_DISPATCHED_OUTCOMES", "All dispatched outcomes"],
    ["STUDY_DEADLINE", "Study deadline"], ["CONTROLLER_OR_CLEANUP_FAILED", "Controller or cleanup failed"],
    ["RESULT_VALIDATION_FAILED", "Result validation failed"], ["OUTPUT_FAILED", "Output failed"],
    ["MISSING_CELL_INPUT", "Missing cell input"], ["INVALID_CELL_INPUT", "Invalid cell input"],
    ["EXECUTION_FAILED", "Execution failed"], ["RESULT_LIMIT", "Result limit"], ["DURATION_LIMIT", "Duration limit"],
    ["CONFIRMED_BY_LOCAL_RESULT", "Confirmed by local result"], ["UNCONFIRMED", "Unconfirmed"], ["NOT_STARTED", "Not started"],
    ["UNKNOWN_CACHE_MODE", "Unknown cache mode"], ["UNKNOWN_PROCESS_GENERATION", "Unknown process generation"],
    ["UNKNOWN_RUNTIME_RECIPE", "Unknown runtime recipe"], ["UNKNOWN_INITIAL_PREFIX_STATE", "Unknown initial prefix state"],
    ["UNKNOWN_PREPARATION_METHOD", "Unknown preparation method"], ["UNKNOWN_WARMUP", "Unknown warmup"],
    ["UNKNOWN_CHRONOLOGY", "Unknown chronology"], ["UNCONTROLLED_TRAFFIC", "Uncontrolled traffic"],
    ["TOKENIZATION_UNAVAILABLE", "Tokenization unavailable"], ["REUSED_PREPARATION_ID", "Reused preparation ID"],
    ["RUNTIME_RECIPE_UNRESOLVED_OR_MISMATCHED", "Runtime recipe unresolved or mismatched"],
    ["PREPARATION_METHOD_MISMATCH", "Preparation method mismatch"],
    ["PROCESS_GENERATION_UNRESOLVED", "Process generation unresolved"],
    ["REUSED_FRESH_PROCESS_GENERATION", "Reused fresh process generation"], ["EVIDENCE_CLASS_MISMATCH", "Evidence class mismatch"]
  ])("labels %s without changing its meaning", (code, expected) => {
    expect(evaluationLabel(code)).toBe(expected);
  });

  it("supports the report fixtures' vocabulary including every displayed limitation", () => {
    const codes = new Set<string>();
    function visit(value: unknown): void {
      if (typeof value === "string" && /^[A-Z][A-Z0-9_]+$/.test(value)) codes.add(value);
      if (Array.isArray(value)) value.forEach(visit);
      else if (typeof value === "object" && value !== null) Object.values(value).forEach(visit);
    }
    [study, cache, cacheEight].forEach(visit);
    expect(codes.size).toBeGreaterThan(30);
    for (const code of codes) expect(evaluationLabel(code), code).not.toBe(code);
    expect(evaluationLabel("Partial first content: HTTP_ERROR")).toBe("Partial first content: HTTP error");
    expect(evaluationLabel("Partial first content: DRAIN_TIMEOUT")).toBe("Partial first content: Drain timeout");
  });

  it.each(["evaluation_future_policy_v2", "FUTURE_STATUS", "Not_Rewritten_HTTP_SLO", "p99 and TTFT", "constructor", "toString", "__proto__", "<script>unknown</script>"])("keeps unrecognized labels unchanged: %s", (value) => {
    expect(evaluationLabel(value)).toBe(value);
  });
});
