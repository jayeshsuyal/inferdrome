import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import {
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

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

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
    const detail = {
      projection_version: projectionVersion,
      summary: runSummary,
      hypothesis: null,
      verification: {},
      execution: {},
      measurements: [],
      distributions: [],
      context: [],
      environment: [],
      artifacts: [],
      unavailable: [],
      digests: {},
      sensitivity: {},
      comparison_contract: {},
    };
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
      .mockResolvedValueOnce(jsonResponse(detail))
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
    expect(detail.baseline_trial_set?.trial_set_id).toBe(
      comparableControlledDetail.result.baseline_trial_set.trial_set_id,
    );
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/controlled-comparisons/${comparableControlledDetail.summary.comparison_plan_id}`,
      expect.objectContaining({ method: "GET" }),
    );
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
});
