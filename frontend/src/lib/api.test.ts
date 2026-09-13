import { afterEach, describe, expect, it, vi } from "vitest";

import { api, setDashboardToken } from "./api";
import {
  baselineRunIds,
  baselineTrialSetSummary,
  comparableControlledDetail,
  controlledComparisonIndex,
  incomparableControlledDetail,
} from "../test/controlledComparisonFixtures";

const projectionVersion = "inferdrome.dashboard.v1" as const;

const runSummary = {
  run_id: "run-11111111111111111111111111111111",
  experiment_id: "exp-dashboard",
  title: "Local evidence smoke",
  model: "inferdrome/fake-model",
  producer_name: "inferdrome_fake",
  producer_version: "1.0.0",
  adapter_name: "fake",
  adapter_version: "1.0.0",
  execution_mode: "synthetic_fixture",
  started_at: "2026-08-07T12:00:00Z",
  ended_at: "2026-08-07T12:00:01Z",
  duration_ns: 1_000_000_000,
  integrity_status: "VALID" as const,
  evidence_eligibility: "SYNTHETIC_ONLY",
  environment_completeness: "COMPLETE",
  replayability: "FULL",
  bundle_digest: `sha256:${"a".repeat(64)}`,
  measured_requests: 2,
  successful_requests: 1,
  failed_requests: 1,
  error_rate: "0.500000",
  ttft_p50_ns: 10_000_000,
  ttft_p95_ns: 10_000_000,
  output_token_throughput_per_s: "42.000000",
  headline_metrics: [],
};

const secondRunSummary = {
  ...runSummary,
  run_id: "run-22222222222222222222222222222222",
  started_at: "2026-08-07T12:01:00Z",
  ended_at: "2026-08-07T12:01:01Z",
};

const runDetail = {
  projection_version: projectionVersion,
  summary: runSummary,
  hypothesis: null,
  verification: {
    bundle_digest: runSummary.bundle_digest, artifact_count: 4, total_bytes: 2048,
    integrity_status: "VALID", evidence_eligibility: "SYNTHETIC_ONLY",
    environment_completeness: "COMPLETE", replayability: "FULL",
    verified_by_recalculation: true,
  },
  execution: {
    terminal_state: "COMPLETE", started_at: runSummary.started_at, ended_at: runSummary.ended_at,
    duration_ns: 1_000_000_000, measurement_window_ns: 1_000_000_000,
    measurement_window_definition: "first_start_to_last_terminal_v1", traffic_kind: "closed_loop",
    concurrency: 1, requests_per_second: null, max_concurrency: null,
    warmup_requests: 0, measured_requests: 2, producer_exit_status: 0,
  },
  measurements: [],
  distributions: [{
    metric: "ttft_ns", label: "Time to first token", unit: "ns", sample_count: 1,
    minimum: 10_000_000, maximum: 10_000_000,
    bins: [{ lower_bound: 10_000_000, upper_bound: 10_000_000, count: 1 }],
  }],
  context: [{ key: "model", label: "Model", value: runSummary.model, group: "target" }],
  environment: [{ name: "gpu_count", label: "GPU count", value: 0, provenance: "OBSERVED", evidence_path: null }],
  artifacts: [{ role: "REQUEST_PLAN", path: "request-plan.json", media_type: "application/json", sensitivity: "PUBLIC", size_bytes: 256, content_exposed: false }],
  unavailable: [{ metric: "memory_bytes", reason: "Not observed by this adapter", capability_matrix: "fake_v1" }],
  digests: {
    source_spec_digest: runSummary.bundle_digest, execution_fingerprint: runSummary.bundle_digest,
    request_plan_digest: runSummary.bundle_digest, metric_definitions_digest: runSummary.bundle_digest,
    exitspec_contract_digest: null,
  },
  sensitivity: {
    prompt_content_in_request_plan: true, canonical_response_content_included: false,
    native_response_content_present: false, secrets_permitted: false,
  },
  comparison_contract: {
    execution_fingerprint: runSummary.bundle_digest, metric_definitions_digest: runSummary.bundle_digest,
    reducer_version: "1.0.0", execution_mode: "synthetic_fixture", workload_sha256: runSummary.bundle_digest,
    requested_output_tokens: 42, temperature: "0", seed: 0,
    traffic_signature: "closed_loop:1", measurement_signature: "fixed_count:2",
  },
};

const trialSetId = "trial-set-11111111111111111111111111111111";
const trialSetSummary = {
  trial_set_id: trialSetId,
  experiment_id: runSummary.experiment_id,
  title: "Repeated local evidence",
  created_at: "2026-08-07T12:02:00Z",
  member_count: 2,
  earliest_run_at: runSummary.started_at,
  latest_run_at: secondRunSummary.ended_at,
  model: runSummary.model,
  execution_fingerprint: `sha256:${"b".repeat(64)}`,
  trial_set_digest: `sha256:${"c".repeat(64)}`,
  evidence_eligibilities: ["SYNTHETIC_ONLY"],
  environment_status: "CONSISTENT" as const,
};

const trialVariation = {
  key: "ttft_ns:p50",
  metric: "ttft_ns",
  aggregation: "p50",
  label: "Time to first token p50",
  unit: "ns",
  total_run_count: 2,
  available_run_count: 2,
  minimum: "10000000",
  maximum: "12000000",
  median: "11000000",
  mean: "11000000",
  span: "2000000",
  sample_standard_deviation: "1414213.562373",
  minimum_display_value: "10 ms",
  maximum_display_value: "12 ms",
  median_display_value: "11 ms",
  mean_display_value: "11 ms",
  span_display_value: "2 ms",
  sample_standard_deviation_display_value: "1.41 ms",
  points: [
    {
      repetition_index: 0,
      run_id: runSummary.run_id,
      value: "10000000",
      display_value: "10 ms",
      sample_count: 2,
    },
    {
      repetition_index: 1,
      run_id: secondRunSummary.run_id,
      value: "12000000",
      display_value: "12 ms",
      sample_count: 2,
    },
  ],
  population: "run_level_measurements" as const,
  weighting: "equal_per_run" as const,
  summary_method: "per_run_scalar_sample_variation_v1" as const,
};

const trialSetDetail = {
  projection_version: projectionVersion,
  summary: trialSetSummary,
  hypothesis: "Run-level values should remain visible.",
  membership_policy: "same_execution_fingerprint_v1" as const,
  metric_definitions_digest: `sha256:${"d".repeat(64)}`,
  reducer_version: "1.0.0",
  members: [
    { repetition_index: 0, run: runSummary },
    { repetition_index: 1, run: secondRunSummary },
  ],
  variations: [trialVariation],
  environment_drift_fields: [],
  design_status: "RETROSPECTIVE" as const,
  inference: "DESCRIPTIVE_ONLY" as const,
  request_population_policy: "separate_per_run_v1" as const,
};

const routingSummary = {
  campaign_id: "routing-campaign-v1" as const,
  retained_digest: `sha256:${"f".repeat(64)}`,
  execution_mode: "SYNTHETIC_CPU_ONLY" as const,
  trial_count: 3,
  planned_request_count: 18,
  policy_ids: [
    "fail_closed_required_load_v1",
    "explicit_fail_open_stale_load_v1",
    "typed_admissible_state_only_v1",
  ],
  verified_by_replay: true as const,
};

function routingTelemetry(
  signal: "HEALTH" | "LOAD" | "KV",
  endpointId: "endpoint-a" | "endpoint-b",
  decisionTime: number,
) {
  const observedAt = signal === "LOAD" && decisionTime >= 20 ? 10 : decisionTime;
  const age = decisionTime - observedAt;
  return {
    signal,
    observer_id: `${signal.toLowerCase()}-observer-v1`,
    endpoint_id: endpointId,
    epoch: signal === "LOAD" ? (decisionTime === 0 ? 1 : 2) : decisionTime / 5 + 1,
    observed_at_ms: observedAt,
    decision_time_ms: decisionTime,
    age_ms: age,
    freshness_bound_ms: 5,
    value: signal === "HEALTH" ? "HEALTHY" : signal === "LOAD" ? (endpointId === "endpoint-a" ? 4 : 1) : "NO_USABLE_SESSION_CLAIM",
    admissibility: age <= 5 ? "ADMISSIBLE" as const : "INADMISSIBLE" as const,
  };
}

function routingTrial(trialId: string, policyId: string) {
  const token = trialId.replace("trial-", "");
  const requests = Array.from({ length: 6 }, (_, sequenceIndex) => {
    const decisionTime = sequenceIndex * 10;
    const stale = decisionTime >= 20;
    const selected = !stale || policyId === "explicit_fail_open_stale_load_v1"
      ? "endpoint-b"
      : policyId === "typed_admissible_state_only_v1" ? "endpoint-a" : null;
    const status = selected === null ? "NO_SAFE_ROUTE" : selected === "endpoint-b" && stale ? "TIMED_OUT" : "SUCCEEDED";
    const fallback = !stale
      ? "NONE"
      : policyId === "fail_closed_required_load_v1"
        ? "REQUIRED_LOAD_STALE"
        : policyId === "explicit_fail_open_stale_load_v1" ? "STALE_LOAD_FAIL_OPEN" : "HEALTH_ONLY_TIE_BREAK";
    const decisionId = `decision-${token}-${String(sequenceIndex).padStart(3, "0")}`;
    return {
      request_id: `request-${String(sequenceIndex).padStart(3, "0")}`,
      sequence_index: sequenceIndex,
      decision_id: decisionId,
      decision_time_ms: decisionTime,
      candidates: (["endpoint-a", "endpoint-b"] as const).map((endpointId) => ({
        endpoint_id: endpointId,
        eligible: policyId !== "fail_closed_required_load_v1" || !stale,
        health: routingTelemetry("HEALTH", endpointId, decisionTime),
        load: routingTelemetry("LOAD", endpointId, decisionTime),
        kv: routingTelemetry("KV", endpointId, decisionTime),
      })),
      selected_endpoint_id: selected,
      claims_used: stale && policyId !== "explicit_fail_open_stale_load_v1" ? ["health"] : ["health", "load"],
      claims_permitted_stale: stale && policyId === "explicit_fail_open_stale_load_v1" ? ["load"] : [],
      claims_discarded: stale && policyId !== "explicit_fail_open_stale_load_v1" ? ["load:stale", "kv:no_session_claim"] : ["kv:no_session_claim"],
      fallback_reason: fallback,
      terminal: {
        terminal_outcome_id: `terminal-${token}-${String(sequenceIndex).padStart(3, "0")}`,
        decision_id: decisionId,
        status,
        reason: status === "TIMED_OUT" ? "SIMULATED_ENDPOINT_B_SATURATED" : fallback,
        started_at_ms: decisionTime,
        ended_at_ms: decisionTime + (status === "TIMED_OUT" ? 5 : status === "SUCCEEDED" ? 1 : 0),
      },
    };
  });
  return {
    trial_id: trialId,
    policy_id: policyId,
    reset: {
      virtual_time_ms: 0,
      endpoint_instances: [
        { endpoint_id: "endpoint-a", instance_id: `${trialId}-endpoint-a-instance-v1` },
        { endpoint_id: "endpoint-b", instance_id: `${trialId}-endpoint-b-instance-v1` },
      ],
      observer_epochs: [
        { observer_id: "health-observer-v1", epoch: 1 },
        { observer_id: "kv-observer-v1", epoch: 1 },
        { observer_id: "load-observer-v1", epoch: 1 },
      ],
      queue_cleared: true,
      load_state_cleared: true,
      kv_state_cleared: true,
    },
    requests,
    terminal_population: ["SUCCEEDED", "TIMED_OUT", "FAILED", "CANCELLED", "NO_SAFE_ROUTE"].map((status) => ({
      status,
      count: requests.filter((request) => request.terminal.status === status).length,
    })),
    terminal_population_total: 6,
  };
}

const routingDetail = {
  projection_version: "inferdrome.routing-campaign-dashboard.v1",
  summary: routingSummary,
  fault_timeline: {
    load_collection_paused_at_ms: 15,
    health_collection_continues: true,
    load_freshness_bound_ms: 5,
    health_freshness_bound_ms: 5,
  },
  trials: [
    routingTrial("trial-fail-closed-v1", "fail_closed_required_load_v1"),
    routingTrial("trial-fail-open-v1", "explicit_fail_open_stale_load_v1"),
    routingTrial("trial-typed-v1", "typed_admissible_state_only_v1"),
  ],
  interpretation_boundary: "MEASUREMENT_EVIDENCE_ONLY" as const,
};

const routingQualificationSummary = {
  qualification_id: "stale-telemetry-qualification-v1" as const,
  retained_digest: `sha256:${"8".repeat(64)}`,
  source_campaign_id: "routing-campaign-v1" as const,
  source_package_retained_digest: routingSummary.retained_digest,
  source_execution_mode: "SYNTHETIC_CPU_ONLY" as const,
  repetitions_per_mode: 1 as const,
  population_accounting: "SEPARATE_PER_TRIAL_NO_POOLING" as const,
  verified_by_source_replay: true as const,
  verified_descriptor_binding: true as const,
};

function qualificationTrial(
  policyId: "fail_closed_required_load_v1" | "explicit_fail_open_stale_load_v1" | "typed_admissible_state_only_v1",
  trialId: string,
) {
  const selected = policyId === "fail_closed_required_load_v1" ? null : policyId === "explicit_fail_open_stale_load_v1" ? "endpoint-b" : "endpoint-a";
  const fallback = policyId === "fail_closed_required_load_v1" ? "REQUIRED_LOAD_STALE" : policyId === "explicit_fail_open_stale_load_v1" ? "STALE_LOAD_FAIL_OPEN" : "HEALTH_ONLY_TIE_BREAK";
  const terminal = policyId === "fail_closed_required_load_v1" ? "NO_SAFE_ROUTE" : policyId === "explicit_fail_open_stale_load_v1" ? "TIMED_OUT" : "SUCCEEDED";
  const population = policyId === "fail_closed_required_load_v1"
    ? [2, 0, 0, 0, 4]
    : policyId === "explicit_fail_open_stale_load_v1"
      ? [2, 4, 0, 0, 0]
      : [6, 0, 0, 0, 0];
  return {
    policy_id: policyId,
    repetition_index: 0,
    trial_id: trialId,
    request_denominator: 6,
    reset: {
      virtual_time_ms: 0,
      endpoint_a_instance_id: `${trialId}-endpoint-a-instance-v1`,
      endpoint_b_instance_id: `${trialId}-endpoint-b-instance-v1`,
      observer_epochs: [1, 1, 1],
      queue_cleared: true,
      load_state_cleared: true,
      kv_state_cleared: true,
    },
    focal_request_id: "request-002" as const,
    focal_decision_id: `decision-${trialId}-002`,
    focal_endpoint_states: ["endpoint-a", "endpoint-b"].map((endpoint_id) => ({
      endpoint_id,
      health_epoch: 1,
      health_age_ms: 0,
      health_admissibility: "ADMISSIBLE" as const,
      load_epoch: 1,
      load_age_ms: 10,
      load_admissibility: "INADMISSIBLE" as const,
    })),
    selected_endpoint_id: selected,
    fallback_reason: fallback,
    terminal_status: terminal,
    terminal_reason: fallback,
    reset_receipt_sha256: `sha256:${"1".repeat(64)}`,
    state_observations_sha256: `sha256:${"2".repeat(64)}`,
    route_decisions_sha256: `sha256:${"3".repeat(64)}`,
    terminal_outcomes_sha256: `sha256:${"4".repeat(64)}`,
    terminal_population: ["SUCCEEDED", "TIMED_OUT", "FAILED", "CANCELLED", "NO_SAFE_ROUTE"].map((status) => ({
      status,
      count: population[["SUCCEEDED", "TIMED_OUT", "FAILED", "CANCELLED", "NO_SAFE_ROUTE"].indexOf(status)],
    })),
    terminal_population_total: 6,
  };
}

const routingQualificationDetail = {
  projection_version: "inferdrome.routing-qualification-dashboard.v1",
  summary: routingQualificationSummary,
  fault_timeline: {
    load_observer_pause_at_ms: 15,
    health_collection_continues: true,
    focal_decision_time_ms: 20,
    health_age_ms: 0,
    load_age_ms: 10,
    freshness_bound_ms: 5,
  },
  trials: [
    qualificationTrial("fail_closed_required_load_v1", "trial-fail-closed-v1"),
    qualificationTrial("explicit_fail_open_stale_load_v1", "trial-fail-open-v1"),
    qualificationTrial("typed_admissible_state_only_v1", "trial-typed-v1"),
  ],
  source_receipts_path: "/routing-campaigns/routing-campaign-v1" as const,
  descriptor_download_path: "/api/v1/routing-qualifications/stale-telemetry-qualification-v1/evidence" as const,
  interpretation_boundary: "MEASUREMENT_EVIDENCE_ONLY" as const,
};

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  setDashboardToken(null);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("dashboard API client", () => {
  it("accepts the exact run index projection", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({
      projection_version: projectionVersion,
      generated_at: "2026-08-07T12:00:02Z",
      runs: [runSummary],
      rejected: [],
      page: {
        limit: 200,
        returned: 1,
        total: 1,
        has_more: false,
        next_cursor: null,
      },
    })));
    vi.stubGlobal("fetch", fetchMock);

    const response = await api.listRuns();

    expect(response.runs[0].run_id).toBe(runSummary.run_id);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/runs?limit=200",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("follows bounded run-index cursors", async () => {
    const secondRun = {
      ...runSummary,
      run_id: "run-22222222222222222222222222222222",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        projection_version: projectionVersion,
        generated_at: "2026-08-07T12:00:02Z",
        runs: [runSummary],
        rejected: [],
        page: {
          limit: 200,
          returned: 1,
          total: 2,
          has_more: true,
          next_cursor: "djE6MQ",
        },
      }))
      .mockResolvedValueOnce(jsonResponse({
        projection_version: projectionVersion,
        generated_at: "2026-08-07T12:00:03Z",
        runs: [secondRun],
        rejected: [],
        page: {
          limit: 200,
          returned: 1,
          total: 2,
          has_more: false,
          next_cursor: null,
        },
      }));
    vi.stubGlobal("fetch", fetchMock);

    const response = await api.listRuns();

    expect(response.runs.map((run) => run.run_id)).toEqual([
      runSummary.run_id,
      secondRun.run_id,
    ]);
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/v1/runs?limit=200&cursor=djE6MQ",
    );
  });

  it("reads summary-nested detail and uses the exact compare query", async () => {
    const comparison = {
      projection_version: projectionVersion,
      baseline_run_id: runSummary.run_id,
      candidate_run_id: "run-22222222222222222222222222222222",
      status: "COMPARABLE",
      reasons: ["Verified measurement and context contracts align."],
      metric_deltas: [],
      context_changes: [],
      directionality: "NEUTRAL",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(runDetail))
      .mockResolvedValueOnce(jsonResponse(comparison));
    vi.stubGlobal("fetch", fetchMock);

    const run = await api.getRun(runSummary.run_id);
    const result = await api.compareRuns(runSummary.run_id, comparison.candidate_run_id);

    expect(run.summary.run_id).toBe(runSummary.run_id);
    expect(result.directionality).toBe("NEUTRAL");
    expect(fetchMock.mock.calls[1][0]).toBe(
      `/api/v1/compare?baseline_run_id=${runSummary.run_id}&candidate_run_id=${comparison.candidate_run_id}`,
    );
  });

  it("rejects a null headline metric before the run index can render", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({
      projection_version: projectionVersion, generated_at: runSummary.ended_at,
      runs: [{ ...runSummary, headline_metrics: [null] }], rejected: [],
      page: { limit: 200, returned: 1, total: 1, has_more: false, next_cursor: null },
    })));
    await expect(api.listRuns()).rejects.toMatchObject({ status: 502 });
  });

  it.each([
    ["headline metrics", { summary: { ...runSummary, headline_metrics: [null] } }],
    ["measurements", { measurements: [null] }],
    ["histogram bins", { distributions: [{ ...runDetail.distributions[0], bins: [null] }] }],
    ["context values", { context: [{ ...runDetail.context[0], value: {} }] }],
    ["environment values", { environment: [{ ...runDetail.environment[0], value: [] }] }],
    ["artifact metadata", { artifacts: [{ ...runDetail.artifacts[0], content_exposed: true }] }],
    ["unavailable reasons", { unavailable: [{ ...runDetail.unavailable[0], reason: {} }] }],
    ["verification counts", { verification: { ...runDetail.verification, artifact_count: null } }],
    ["execution counts", { execution: { ...runDetail.execution, measured_requests: "2" } }],
    ["digests", { digests: null }],
    ["sensitivity", { sensitivity: { ...runDetail.sensitivity, secrets_permitted: true } }],
  ])("rejects malformed nested %s with a recoverable protocol error", async (_label, changed) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ ...runDetail, ...changed })));
    await expect(api.getRun(runSummary.run_id)).rejects.toMatchObject({ status: 502 });
  });

  it("binds a valid run response to the requested identity", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ ...runDetail, summary: secondRunSummary })));
    await expect(api.getRun(runSummary.run_id)).rejects.toMatchObject({ status: 502 });
  });

  it("preserves compatible additive fields and unavailable metrics", async () => {
    const detail = {
      ...runDetail, future_metadata: { version: 2 },
      summary: { ...runSummary, ttft_p50_ns: null, ttft_p95_ns: null, future_summary_field: true },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(detail)));
    await expect(api.getRun(runSummary.run_id)).resolves.toEqual(detail);
  });

  it("retains backend integer types in newly validated detail fields", async () => {
    const detail = {
      ...runDetail,
      execution: { ...runDetail.execution, measurement_window_ns: 2 ** 53, max_concurrency: 2 ** 53 },
      distributions: [{ ...runDetail.distributions[0], minimum: 2 ** 53, maximum: 2 ** 53 }],
      environment: [{ ...runDetail.environment[0], value: 2 ** 53 }],
      comparison_contract: { ...runDetail.comparison_contract, seed: -42 },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(detail)));
    await expect(api.getRun(runSummary.run_id)).resolves.toEqual(detail);
  });

  it("follows bounded trial-set cursors and reads the descriptive detail contract", async () => {
    const secondTrialSet = {
      ...trialSetSummary,
      trial_set_id: "trial-set-22222222222222222222222222222222",
      title: "Second repeated condition",
      trial_set_digest: `sha256:${"e".repeat(64)}`,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        projection_version: projectionVersion,
        generated_at: "2026-08-07T12:03:00Z",
        trial_sets: [trialSetSummary],
        rejected: [],
        page: {
          limit: 100,
          returned: 1,
          total: 2,
          has_more: true,
          next_cursor: "djE6MTphYmM",
        },
      }))
      .mockResolvedValueOnce(jsonResponse({
        projection_version: projectionVersion,
        generated_at: "2026-08-07T12:03:01Z",
        trial_sets: [secondTrialSet],
        rejected: [],
        page: {
          limit: 100,
          returned: 1,
          total: 2,
          has_more: false,
          next_cursor: null,
        },
      }))
      .mockResolvedValueOnce(jsonResponse(trialSetDetail));
    vi.stubGlobal("fetch", fetchMock);

    const index = await api.listTrialSets();
    const detail = await api.getTrialSet(trialSetId);

    expect(index.trial_sets.map((item) => item.trial_set_id)).toEqual([
      trialSetId,
      secondTrialSet.trial_set_id,
    ]);
    expect(detail.inference).toBe("DESCRIPTIVE_ONLY");
    expect(detail.variations[0].points).toHaveLength(2);
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/v1/trial-sets?limit=100&cursor=djE6MTphYmM",
    );
    expect(fetchMock.mock.calls[2][0]).toBe(`/api/v1/trial-sets/${trialSetId}`);
  });

  it("rejects trial-set detail whose member order violates the public contract", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      ...trialSetDetail,
      members: [...trialSetDetail.members].reverse(),
    }))));

    await expect(api.getTrialSet(trialSetId)).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("ordered and contiguous"),
    });
  });

  it("reads the one-root routing-campaign projection only after replay verification", async () => {
    const index = {
      projection_version: "inferdrome.routing-campaign-dashboard.v1",
      routing_campaigns: [routingSummary],
      rejected: [],
      page: { limit: 25, returned: 1, total: 1, has_more: false, next_cursor: null },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(index))
      .mockResolvedValueOnce(jsonResponse(routingDetail));
    vi.stubGlobal("fetch", fetchMock);

    const campaigns = await api.listRoutingCampaigns();
    const detail = await api.getRoutingCampaign("routing-campaign-v1");

    expect(campaigns.routing_campaigns).toHaveLength(1);
    expect(campaigns.routing_campaigns[0]?.verified_by_replay).toBe(true);
    expect(detail.trials).toHaveLength(3);
    expect(detail.trials[0]?.requests).toHaveLength(6);
    expect(detail.trials[0]?.terminal_population_total).toBe(6);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/routing-campaigns?limit=25");
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/v1/routing-campaigns/routing-campaign-v1",
    );
    expect(fetchMock.mock.calls.every(([, options]) => options.method === "GET")).toBe(true);
  });

  it("fails closed when routing telemetry is inconsistent with its freshness receipt", async () => {
    const malformed = structuredClone(routingDetail);
    malformed.trials[0].requests[2].candidates[0].load.admissibility = "ADMISSIBLE";
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse(malformed))));

    await expect(api.getRoutingCampaign("routing-campaign-v1")).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("admissibility disagrees with freshness"),
    });
  });

  it("fails closed when a routing response includes an unallowlisted field", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      ...routingDetail,
      raw_package_content: "must never render",
    }))));

    await expect(api.getRoutingCampaign("routing-campaign-v1")).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("allowlist"),
    });
  });

  it("reads the source-replayed, descriptor-bound causal qualification", async () => {
    const index = {
      projection_version: "inferdrome.routing-qualification-dashboard.v1",
      routing_qualifications: [routingQualificationSummary],
      rejected: [],
      page: { limit: 25, returned: 1, total: 1, has_more: false, next_cursor: null },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(index))
      .mockResolvedValueOnce(jsonResponse(routingQualificationDetail));
    vi.stubGlobal("fetch", fetchMock);

    const qualifications = await api.listRoutingQualifications();
    const detail = await api.getRoutingQualification("stale-telemetry-qualification-v1");

    expect(qualifications.routing_qualifications).toHaveLength(1);
    expect(detail.summary.verified_by_source_replay).toBe(true);
    expect(detail.summary.verified_descriptor_binding).toBe(true);
    expect(detail.trials.map((trial) => trial.terminal_population_total)).toEqual([6, 6, 6]);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/routing-qualifications?limit=25");
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/v1/routing-qualifications/stale-telemetry-qualification-v1",
    );
  });

  it("fails closed when a causal qualification loses its fixed freshness binding", async () => {
    const malformed = structuredClone(routingQualificationDetail);
    malformed.trials[0].focal_endpoint_states[0].load_age_ms = 0;
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse(malformed))));

    await expect(api.getRoutingQualification("stale-telemetry-qualification-v1")).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("fixed stale-load/fresh-health state"),
    });
  });

  it("fails closed when a complete causal population disagrees with its declared mode", async () => {
    const malformed = structuredClone(routingQualificationDetail);
    malformed.trials[0].terminal_population[0].count = 3;
    malformed.trials[0].terminal_population[4].count = 3;
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse(malformed))));

    await expect(api.getRoutingQualification("stale-telemetry-qualification-v1")).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("terminal_population disagrees with the declared mode"),
    });
  });

  it("fails closed when a causal trial reuses another cold-reset identity", async () => {
    const malformed = structuredClone(routingQualificationDetail);
    malformed.trials[1].reset.endpoint_a_instance_id = malformed.trials[0].reset.endpoint_a_instance_id;
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse(malformed))));

    await expect(api.getRoutingQualification("stale-telemetry-qualification-v1")).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("complete cold-reset receipt"),
    });
  });

  it("rejects an evidence download when its digest does not bind the rendered descriptor", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("{}", {
      status: 200,
      headers: {
        "content-type": "application/json",
        "content-disposition": 'attachment; filename="stale-telemetry-qualification-v1.json"',
        "x-inferdrome-evidence-digest": `sha256:${"0".repeat(64)}`,
      },
    }))));

    await expect(api.downloadRoutingQualification(
      "stale-telemetry-qualification-v1",
      routingQualificationSummary.retained_digest,
    )).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("does not match the rendered descriptor"),
    });
  });

  it("downloads descriptor evidence through the authenticated GET-only path", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(new Response("{}", {
      status: 200,
      headers: {
        "content-type": "application/json",
        "content-disposition": 'attachment; filename="stale-telemetry-qualification-v1.json"',
        "x-inferdrome-evidence-digest": routingQualificationSummary.retained_digest,
      },
    })));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const createObjectURL = vi.fn(() => "blob:qualification");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    setDashboardToken("test-dashboard-token");

    await expect(api.downloadRoutingQualification(
      "stale-telemetry-qualification-v1",
      routingQualificationSummary.retained_digest,
    )).resolves.toBe(routingQualificationSummary.retained_digest);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/routing-qualifications/stale-telemetry-qualification-v1/evidence",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({ Authorization: "Bearer test-dashboard-token" }),
      }),
    );
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(click).toHaveBeenCalledOnce();
    await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:qualification");
  });

  it("fails closed when causal qualification content exceeds its allowlist", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      ...routingQualificationDetail,
      raw_descriptor_path: "/must-not-render",
    }))));

    await expect(api.getRoutingQualification("stale-telemetry-qualification-v1")).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("allowlist"),
    });
  });

  it("reads the controlled-comparison index from its bounded endpoint", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(controlledComparisonIndex)));
    vi.stubGlobal("fetch", fetchMock);

    const index = await api.listControlledComparisons();

    expect(index.comparisons).toHaveLength(4);
    expect(index.comparisons.map((item) => item.result_status)).toEqual([
      "COMPARABLE",
      "INCOMPARABLE",
      "NO_RESULT",
      "WITHHELD",
    ]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/controlled-comparisons?limit=100",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("reads an allowlisted comparable detail without raw resolved configuration", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(comparableControlledDetail)));
    vi.stubGlobal("fetch", fetchMock);

    const detail = await api.getControlledComparison(
      comparableControlledDetail.summary.comparison_plan_id,
    );

    expect(detail.result?.status).toBe("COMPARABLE");
    expect(detail.result?.outcomes[0].estimate).toBe("1000000");
    expect(detail.execution).toMatchObject({
      status: "EVIDENCE_COMPLETE",
      result_published: true,
      completed_run_count: 4,
      planned_run_count: 4,
      exact_schedule_prefix: true,
    });
    expect(detail.baseline_trial_set?.trial_set_id).toBe(
      comparableControlledDetail.result.baseline_trial_set.trial_set_id,
    );
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/controlled-comparisons/${comparableControlledDetail.summary.comparison_plan_id}`,
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("keeps a published result when current workspace inspection is blocked", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      ...comparableControlledDetail,
      execution: {
        ...comparableControlledDetail.execution,
        status: "BLOCKED",
        completed_run_count: 3,
        next_sequence_index: null,
        exact_schedule_prefix: false,
        slots: comparableControlledDetail.execution.slots.map((slot, index) => ({
          ...slot,
          state: index === 0 ? "INVALID" : "COMPLETE",
          verified_bundle: index !== 0,
        })),
      },
    }))));

    const detail = await api.getControlledComparison(
      comparableControlledDetail.summary.comparison_plan_id,
    );

    expect(detail.result?.status).toBe("COMPARABLE");
    expect(detail.execution).toMatchObject({
      status: "BLOCKED",
      result_published: true,
      exact_schedule_prefix: false,
    });
  });

  it("requires incomparable details to withhold both Trial Set projections", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse(incomparableControlledDetail))));

    const detail = await api.getControlledComparison(
      incomparableControlledDetail.summary.comparison_plan_id,
    );

    expect(detail.result?.status).toBe("INCOMPARABLE");
    expect(detail.result?.outcomes).toEqual([]);
    expect(detail.baseline_trial_set).toBeNull();
    expect(detail.candidate_trial_set).toBeNull();

    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      ...incomparableControlledDetail,
      baseline_trial_set: baselineTrialSetSummary,
    }))));
    await expect(api.getControlledComparison(
      incomparableControlledDetail.summary.comparison_plan_id,
    )).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("must withhold Trial Set projections"),
    });
  });

  it("accepts exact-prefix operational progress without upgrading it to evidence", async () => {
    const partialExecution = {
      ...comparableControlledDetail.execution,
      status: "PARTIAL",
      result_published: false,
      completed_run_count: 1,
      next_sequence_index: 1,
      slots: comparableControlledDetail.execution.slots.map((slot, index) => ({
        ...slot,
        state: index === 0 ? "COMPLETE" : "PENDING",
        verified_bundle: index === 0,
      })),
    };
    const detailWithoutResult = {
      ...comparableControlledDetail,
      summary: {
        ...comparableControlledDetail.summary,
        result_status: "NO_RESULT",
        comparison_result_id: null,
        comparison_result_digest: null,
        estimate: null,
        estimate_display_value: null,
      },
      execution: partialExecution,
      result: null,
      baseline_trial_set: null,
      candidate_trial_set: null,
    };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse(detailWithoutResult))));

    const detail = await api.getControlledComparison(
      comparableControlledDetail.summary.comparison_plan_id,
    );

    expect(detail.execution.status).toBe("PARTIAL");
    expect(detail.result).toBeNull();
  });

  it("rejects controlled progress with a mismatched run identity", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      ...comparableControlledDetail,
      execution: {
        ...comparableControlledDetail.execution,
        slots: comparableControlledDetail.execution.slots.map((slot, index) => (
          index === 1 ? { ...slot, run_id: baselineRunIds[0] } : slot
        )),
      },
    }))));

    await expect(api.getControlledComparison(
      comparableControlledDetail.summary.comparison_plan_id,
    )).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("disagrees with verified progress"),
    });
  });

  it("rejects partial progress whose next slot is not the frozen prefix boundary", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      ...comparableControlledDetail,
      summary: {
        ...comparableControlledDetail.summary,
        result_status: "NO_RESULT",
        comparison_result_id: null,
        comparison_result_digest: null,
        estimate: null,
        estimate_display_value: null,
      },
      execution: {
        ...comparableControlledDetail.execution,
        status: "PARTIAL",
        result_published: false,
        completed_run_count: 1,
        next_sequence_index: 2,
        slots: comparableControlledDetail.execution.slots.map((slot, index) => ({
          ...slot,
          state: index === 0 ? "COMPLETE" : "PENDING",
          verified_bundle: index === 0,
        })),
      },
      result: null,
      baseline_trial_set: null,
      candidate_trial_set: null,
    }))));

    await expect(api.getControlledComparison(
      comparableControlledDetail.summary.comparison_plan_id,
    )).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("status disagrees with its slot states"),
    });
  });

  it("rejects a pending hole followed by a nonterminal workspace", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      ...comparableControlledDetail,
      summary: {
        ...comparableControlledDetail.summary,
        result_status: "NO_RESULT",
        comparison_result_id: null,
        comparison_result_digest: null,
        estimate: null,
        estimate_display_value: null,
      },
      execution: {
        ...comparableControlledDetail.execution,
        status: "PARTIAL",
        result_published: false,
        completed_run_count: 0,
        next_sequence_index: 0,
        exact_schedule_prefix: true,
        slots: comparableControlledDetail.execution.slots.map((slot, index) => ({
          ...slot,
          state: index === 1 ? "CREATED" : "PENDING",
          verified_bundle: false,
        })),
      },
      result: null,
      baseline_trial_set: null,
      candidate_trial_set: null,
    }))));

    await expect(api.getControlledComparison(
      comparableControlledDetail.summary.comparison_plan_id,
    )).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("completion arithmetic is inconsistent"),
    });
  });

  it("rejects a detail arm that exposes raw resolved experiment configuration", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      ...comparableControlledDetail,
      plan: {
        ...comparableControlledDetail.plan,
        baseline_arm: {
          ...comparableControlledDetail.plan.baseline_arm,
          resolved_experiment: {
            target: { endpoint: "https://secret.example/v1" },
          },
        },
      },
    }))));

    await expect(api.getControlledComparison(
      comparableControlledDetail.summary.comparison_plan_id,
    )).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("resolved_experiment is forbidden"),
    });
  });

  it("rejects controlled outcomes outside the frozen v1 selector vocabulary", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      ...comparableControlledDetail,
      plan: {
        ...comparableControlledDetail.plan,
        primary_outcome: {
          ...comparableControlledDetail.plan.primary_outcome,
          aggregation: "median",
        },
      },
    }))));

    await expect(api.getControlledComparison(
      comparableControlledDetail.summary.comparison_plan_id,
    )).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("frozen v1 outcome semantics"),
    });
  });

  it("fails closed instead of rounding an unsafe routing-execution integer", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      projection_version: "inferdrome.routing-execution-dashboard.v1",
      routing_executions: [],
      rejected: [],
      page: {
        limit: Number.MAX_SAFE_INTEGER + 1,
        returned: 0,
        total: 0,
        has_more: false,
        next_cursor: null,
      },
    }))));

    await expect(api.listRoutingExecutions()).rejects.toMatchObject({
      status: 502,
      message: expect.stringContaining("must be an integer"),
    });
  });

  describe("closed manual-host execution profiles", () => {
    function executionIndex(topology: Record<string, unknown>) {
      return {
        projection_version: "inferdrome.routing-execution-dashboard.v1",
        routing_executions: [{
          execution_id: "routing-execution-v1",
          retained_digest: `sha256:${"a".repeat(64)}`,
          mode: "LAMBDA_MANUAL_HOST",
          source_commit: "b".repeat(40),
          model: {
            model_id: "Qwen/Qwen3-8B",
            model_revision: "c".repeat(40),
            tokenizer_revision: "c".repeat(40),
          },
          runtime: {
            runtime_name: "vllm",
            runtime_version: "0.26.0",
            adapter_id: "openai-compatible-routing-execution-v1",
            adapter_version: "1.0.0",
          },
          topology: {
            accelerator_model: "NVIDIA H100-SXM5-80GB",
            accelerator_count: 2,
            runner_separate_from_serving: true,
            serving_engine_count: 2,
            one_engine_per_endpoint: true,
            declared_provider: "LAMBDA",
            declared_provisioning: "OPERATOR_SUPPLIED_VM",
            identity_assertion: "OPERATOR_DECLARED_NOT_OBSERVED",
            lifecycle_protection: "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY",
            ...topology,
          },
          policy_ids: [
            "fail_closed_required_load_v1",
            "explicit_fail_open_stale_load_v1",
            "typed_admissible_state_only_v1",
          ],
          trial_count: 3,
          request_denominator_per_trial: 6,
          terminal_denominator: 18,
          verified_by_offline_replay: true,
        }],
        rejected: [],
        page: { limit: 25, returned: 1, total: 1, has_more: false, next_cursor: null },
      };
    }

    it.each(["NVIDIA A100-PCIE-40GB", "NVIDIA H100-SXM5-80GB"])(
      "accepts the reviewed two-GPU %s projection",
      async (acceleratorModel) => {
        vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse(
          executionIndex({ accelerator_model: acceleratorModel }),
        ))));

        const response = await api.listRoutingExecutions();

        expect(response.routing_executions[0].topology.accelerator_model).toBe(acceleratorModel);
        expect(response.routing_executions[0].topology.identity_assertion).toBe("OPERATOR_DECLARED_NOT_OBSERVED");
      },
    );

    it.each([
      ["unsupported model", { accelerator_model: "NVIDIA H200" }],
      ["mixed models", { accelerator_model: "NVIDIA A100-PCIE-40GB,NVIDIA H100-SXM5-80GB" }],
      ["wrong count", { accelerator_count: 1 }],
      ["different provider", { declared_provider: "GCP" }],
      ["different provisioning", { declared_provisioning: "AUTOMATIC_VM" }],
      ["observed identity claim", { identity_assertion: "OBSERVED" }],
      ["resolved lifecycle claim", { lifecycle_protection: "VERIFIED" }],
      ["shared runner", { runner_separate_from_serving: false }],
      ["wrong engine count", { serving_engine_count: 1 }],
      ["shared endpoint engine", { one_engine_per_endpoint: false }],
    ] satisfies [string, Record<string, unknown>][])(
      "rejects %s while retaining the manual-host boundary",
      async (_case, topology) => {
        vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse(executionIndex(topology)))));

        await expect(api.listRoutingExecutions()).rejects.toMatchObject({ status: 502 });
      },
    );
  });
});
