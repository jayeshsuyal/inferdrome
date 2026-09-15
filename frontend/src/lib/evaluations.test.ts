import { describe, expect, it } from "vitest";

import cache from "../test/evaluations/cache.json";
import cacheEight from "../test/evaluations/cache-eight.json";
import empty from "../test/evaluations/empty.json";
import study from "../test/evaluations/study.json";
import { EVALUATION_VERSION, parseEvaluationDetail, parseEvaluationIndex } from "./evaluations";

// These projections were generated from the authoritative study/cache reducers
// with tests.evaluation_dashboard_support and evaluation_projection.project_report.
// They are synthetic test fixtures, not GPU or source-replay evidence.
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

describe("evaluation projection contract", () => {
  it("accepts the authoritative eight-block descriptive interval without promoting its evidence", () => {
    const value = parseEvaluationDetail(cacheEight, cacheEight.summary.report_id);
    if (value.kind !== "PREFIX_CACHE") throw new Error("Wrong fixture kind");
    expect(value.blocks).toHaveLength(8);
    expect(value.low_replication).toBe(false);
    expect(value.contrasts.map((item) => [item.interval_status, item.mean_rps, item.lower_rps, item.upper_rps])).toEqual([
      ["DESCRIPTIVE_ONLY", "33.333333", "33.333333", "33.333333"],
      ["DESCRIPTIVE_ONLY", "11.111111", "11.111111", "11.111111"],
      ["DESCRIPTIVE_ONLY", "22.222222", "22.222222", "22.222222"],
    ]);
    expect(value.cache_treatment_attribution).toBe("UNVERIFIED");
    expect(value.summary.evidence_eligible).toBe(false);
    expect(cache.contrasts.every((item) => item.lower_rps === null && item.upper_rps === null)).toBe(true);
  });
  it.each([study, cache, empty])("accepts the reducer-generated $kind projection", (fixture) => {
    expect(parseEvaluationDetail(fixture, fixture.summary.report_id)).toBe(fixture);
  });

  it("keeps all four new study policies and fixed cache conditions independent of frozen routes", () => {
    const parsedStudy = parseEvaluationDetail(study, study.summary.report_id);
    const parsedCache = parseEvaluationDetail(cache, cache.summary.report_id);
    if (parsedStudy.kind !== "STUDY" || parsedCache.kind !== "PREFIX_CACHE") throw new Error("Wrong fixture kind");
    expect(parsedStudy.trials.map((item) => item.policy_id)).toEqual([
      "evaluation_round_robin_v1",
      "evaluation_least_reported_load_v1",
      "evaluation_freshness_fallback_v1",
      "evaluation_fail_closed_v1",
    ]);
    expect(parsedCache.blocks[0].cells.map((item) => item.condition)).toEqual(["S0", "S1", "U0", "U1"]);
    expect(parsedCache.contrasts.map((item) => item.mean_rps)).toEqual(["33.333333", "11.111111", "22.222222"]);
    expect(parsedStudy.trials[3].foreground.metrics.find((item) => item.key === "slo_goodput_rps")?.value).toBe("27.777778");
  });

  it("accepts no returned measurement without promoting the local evidence class", () => {
    const value = parseEvaluationDetail(empty, empty.summary.report_id);
    expect(value.summary.returned_records).toBe(0);
    expect(value.summary.evidence_class).toBe("LOCAL_MEASUREMENT_ONLY");
    expect(value.summary.status).toBe("ABORTED");
  });

  const invalidCases: [string, (value: typeof study) => void][] = [
    ["extra root data", (v) => Object.assign(v, { raw_prompt: "secret" })],
    ["extra nested data", (v) => Object.assign(v.trials[0].foreground, { output: "secret" })],
    ["missing field", (v) => { delete (v as Partial<typeof study>).limitations; }],
    ["unsupported version", (v) => { v.projection_version = "future"; }],
    ["wrong kind", (v) => { v.kind = "PREFIX_CACHE"; }],
    ["unknown scenario", (v) => { v.trials[0].scenario = "GPU_VALIDATED"; }],
    ["wrong source schema", (v) => { v.summary.source_schema = "inferdrome.evaluation-cache-report.v1"; }],
    ["promoted evidence", (v) => { v.summary.evidence_eligible = true; }],
    ["promoted runtime", (v) => { v.summary.runtime_verification = "VERIFIED"; }],
    ["claimed source replay", (v) => { v.summary.source_replay = "PERFORMED"; }],
    ["claimed tokenizer recheck", (v) => { v.summary.tokenizer_reverified_here = true; }],
    ["unknown execution status", (v) => { v.summary.status = "SUCCESS"; }],
    ["contradictory comparison status", (v) => { v.summary.status = "CANCELLED"; }],
    ["private report label", (v) => { v.summary.label = "/private/report"; }],
    ["floating counter", (v) => { v.summary.returned_records = 1.5; }],
    ["unsafe integer", (v) => { v.summary.returned_records = Number.MAX_SAFE_INTEGER + 1; }],
    ["invalid decimal", (v) => { v.coverage[0].value = "NaN"; }],
    ["scientific notation", (v) => { v.coverage[0].value = "1e20"; }],
    ["excess decimal precision", (v) => { v.coverage[0].value = "0.1234567"; }],
    ["duplicate metric key", (v) => { v.coverage[1].key = v.coverage[0].key; }],
    ["duplicate trial selector", (v) => { v.trials[1].index = v.trials[0].index; }],
    ["duplicate policy", (v) => { v.strata[0].policies[1].policy_id = v.strata[0].policies[0].policy_id; }],
    ["frozen policy mixed in", (v) => { v.trials[0].policy_id = "fail_closed_required_load_v1"; }],
    ["oversized trials", (v) => { v.trials = Array.from({ length: 257 }, () => v.trials[0]); }],
    ["extra metric", (v) => { v.coverage = Array.from({ length: 25 }, () => v.coverage[0]); }],
    ["unknown outcome", (v) => { v.trials[0].foreground.outcomes[0].outcome = "GPU_SUCCESS"; }],
    ["partial interval", (v) => { v.strata[0].contrasts[0].lower_rps = "1" as never; }],
    ["unobserved zero recovery", (v) => {
      v.trials[0].recovery.intervals[0].status = "UNOBSERVED_OR_CENSORED";
      v.trials[0].recovery.intervals[0].duration_ns = "0" as never;
    }],
  ];
  it.each(invalidCases)("rejects %s without reflecting source text", (_name, mutate) => {
    const value = copy(study);
    mutate(value);
    expect(() => parseEvaluationDetail(value, study.summary.report_id)).toThrow("invalid evaluation report projection");
  });

  it("rejects route identity mismatches", () => {
    expect(() => parseEvaluationDetail(study, cache.summary.report_id)).toThrow();
    expect(() => parseEvaluationDetail(study, "private-path")).toThrow();
  });

  it("rejects missing or mislabelled cache cells and cache-treatment promotion", () => {
    const value = copy(cache);
    value.blocks[0].cells[0].mode = "DECLARED_ENABLED";
    expect(() => parseEvaluationDetail(value, cache.summary.report_id)).toThrow();
    value.blocks[0].cells[0].mode = "DECLARED_DISABLED";
    value.cache_treatment_attribution = "VERIFIED";
    expect(() => parseEvaluationDetail(value, cache.summary.report_id)).toThrow();
    value.cache_treatment_attribution = "UNVERIFIED";
    value.blocks[0].cells.pop();
    expect(() => parseEvaluationDetail(value, cache.summary.report_id)).toThrow();
  });

  it("bounds the index and withholds unsafe rejection text", () => {
    const index = {
      projection_version: EVALUATION_VERSION,
      reports: [study.summary, cache.summary],
      rejected: [{ entry: 3, code: "REPORT_UNAVAILABLE", message: "Configured report was withheld." }]
    };
    expect(parseEvaluationIndex(index)).toBe(index);
    expect(() => parseEvaluationIndex({ ...index, reports: Array.from({ length: 9 }, () => study.summary) })).toThrow();
    expect(() => parseEvaluationIndex({ ...index, reports: [study.summary, study.summary] })).toThrow();
    expect(() => parseEvaluationIndex({ ...index, rejected: [{ ...index.rejected[0], message: "private filename" }] })).toThrow();
  });
});
