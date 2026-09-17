import { afterEach, describe, expect, it, vi } from "vitest";

import cache from "../test/evaluations/cache.json";
import sglang from "../test/evaluations/sglang.json";
import study from "../test/evaluations/study.json";
import {
  ENGINE_EVALUATION_VERSION,
  EVALUATION_VERSION,
  evaluationApi,
  evaluationEngine,
  parseEvaluationDetail,
  parseEvaluationIndex
} from "./evaluations";

// Authoritative reducer/projection fixtures are SYNTHETIC_ONLY, not runtime or
// GPU evidence. Mutations below test the closed public display contract.
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
afterEach(() => vi.unstubAllGlobals());

describe("engine-bound evaluation projection", () => {
  it("retains the SGLang envelope identity and source claims", () => {
    const parsed = parseEvaluationDetail(sglang, sglang.summary.report_id);
    expect(parsed).toBe(sglang);
    expect(parsed.projection_version).toBe(ENGINE_EVALUATION_VERSION);
    expect(parsed.summary.source_schema).toBe("inferdrome.evaluation-study-report.v2");
    expect(evaluationEngine(parsed.summary)).toEqual(sglang.summary.engine_identity);
    expect(parsed.summary.evidence_class).toBe("SYNTHETIC_ONLY");
    expect(parsed.summary.runtime_verification).toBe("UNVERIFIED");
    expect(parsed.summary.evidence_eligible).toBe(false);
    expect(parsed.summary.source_replay).toBe("NOT_PERFORMED");
  });

  it("accepts a mixed index without assigning an engine to legacy reports", () => {
    const value = {
      projection_version: ENGINE_EVALUATION_VERSION,
      reports: [study.summary, sglang.summary, cache.summary],
      rejected: []
    };
    expect(parseEvaluationIndex(value)).toBe(value);
    expect(evaluationEngine(parseEvaluationDetail(study, study.summary.report_id).summary)).toBeNull();
    expect(evaluationEngine(parseEvaluationDetail(cache, cache.summary.report_id).summary)).toBeNull();
    expect(() => parseEvaluationIndex({ ...value, projection_version: EVALUATION_VERSION })).toThrow();
    expect(() => parseEvaluationIndex({ ...value, reports: [study.summary, cache.summary] })).toThrow();
  });

  it.each([
    ["engine", "vllm"],
    ["producer_version", "latest"],
    ["profile", "DISTRIBUTED"],
    ["engine_binding_sha256", "private-config-path"],
    ["engine_choice_sha256", null],
    ["telemetry_semantics", "VLLM_RUNNING_WAITING"],
    ["telemetry_freshness", "SOURCE_STATE_AGE"],
    ["telemetry_source_age", "VERIFIED"],
    ["cache_preparation", "VERIFIED_FLUSH"],
    ["cache_state", "VERIFIED_COLD"]
  ])("rejects an unsupported engine identity %s", (field, replacement) => {
    const value = copy(sglang);
    Object.assign(value.summary.engine_identity, { [field as string]: replacement });
    expect(() => parseEvaluationDetail(value, value.summary.report_id)).toThrow("invalid evaluation report projection");
  });

  it.each(Object.keys(sglang.summary.engine_identity))("requires engine identity field %s", (field) => {
    const value = copy(sglang);
    Reflect.deleteProperty(value.summary.engine_identity, field);
    expect(() => parseEvaluationDetail(value, value.summary.report_id)).toThrow();
  });

  it.each([
    ["missing identity", (value: typeof sglang) => Reflect.deleteProperty(value.summary, "engine_identity")],
    ["extra identity", (value: typeof sglang) => Object.assign(value.summary.engine_identity, { origin: "http://private-host" })],
    ["v1 projection", (value: typeof sglang) => { value.projection_version = EVALUATION_VERSION; }],
    ["v1 source", (value: typeof sglang) => { value.summary.source_schema = "inferdrome.evaluation-study-report.v1"; }],
    ["generic label", (value: typeof sglang) => { value.summary.label = "Study report 1"; }],
    ["missing study config", (value: typeof sglang) => Object.assign(value.summary, { config_sha256: null })],
    ["unavailable evidence", (value: typeof sglang) => Object.assign(value.summary, { evidence_class: null })],
    ["verified runtime", (value: typeof sglang) => { value.summary.runtime_verification = "VERIFIED"; }],
    ["eligible evidence", (value: typeof sglang) => { value.summary.evidence_eligible = true; }]
  ])("rejects %s rather than rendering a partial identity", (_label, mutate) => {
    const value = copy(sglang);
    mutate(value);
    expect(() => parseEvaluationDetail(value, value.summary.report_id)).toThrow("invalid evaluation report projection");
  });

  it("rejects engine identity smuggled into either legacy projection", () => {
    for (const fixture of [study, cache]) {
      const value = copy(fixture);
      Object.assign(value.summary, { engine_identity: sglang.summary.engine_identity });
      expect(() => parseEvaluationDetail(value, value.summary.report_id)).toThrow();
    }
  });
});

describe("versioned evaluation response byte bounds", () => {
  const legacyBytes = 4 * 1024 * 1024;
  const engineBytes = legacyBytes + 20 * 1024;
  function serve(value: unknown, bytes: number, declared = false) {
    const json = JSON.stringify(value);
    const size = new TextEncoder().encode(json).byteLength;
    const body = json + " ".repeat(bytes - size);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, {
      headers: {
        "content-type": "application/json",
        ...(declared ? { "content-length": String(bytes) } : {})
      }
    })));
  }

  it.each([false, true])("admits the exact SGLang byte ceiling (declared=%s)", async (declared) => {
    serve(sglang, engineBytes, declared);
    await expect(evaluationApi.detail(sglang.summary.report_id)).resolves.toEqual(sglang);
  });

  it.each([false, true])("retains the exact legacy byte ceiling (declared=%s)", async (declared) => {
    serve(study, legacyBytes, declared);
    await expect(evaluationApi.detail(study.summary.report_id)).resolves.toEqual(study);
    serve(study, legacyBytes + 1, declared);
    await expect(evaluationApi.detail(study.summary.report_id)).rejects.toThrow(declared ? "headers are unsupported" : "byte limit");
  });

  it.each([false, true])("rejects above the SGLang byte ceiling (declared=%s)", async (declared) => {
    serve(sglang, engineBytes + 1, declared);
    await expect(evaluationApi.detail(sglang.summary.report_id)).rejects.toThrow(declared ? "headers are unsupported" : "byte limit");
  });
});
