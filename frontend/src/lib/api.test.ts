import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

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
});
