import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { MemoryRouter } from "./lib/router";

afterEach(() => vi.unstubAllGlobals());

const projectionVersion = "inferdrome.dashboard.v1";
const trialSetId = "trial-set-11111111111111111111111111111111";

function response(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function runSummary(runId: string, startedAt: string) {
  return {
    run_id: runId,
    experiment_id: "exp-dashboard",
    title: "Local evidence smoke",
    model: "inferdrome/fake-model",
    producer_name: "inferdrome_fake",
    producer_version: "1.0.0",
    adapter_name: "fake",
    adapter_version: "1.0.0",
    execution_mode: "synthetic_fixture",
    started_at: startedAt,
    ended_at: startedAt,
    duration_ns: 1_000_000_000,
    integrity_status: "VALID",
    evidence_eligibility: "SYNTHETIC_ONLY",
    environment_completeness: "COMPLETE",
    replayability: "FULL",
    bundle_digest: `sha256:${"a".repeat(64)}`,
    measured_requests: 2,
    successful_requests: 2,
    failed_requests: 0,
    error_rate: "0",
    ttft_p50_ns: 10_000_000,
    ttft_p95_ns: 12_000_000,
    output_token_throughput_per_s: "42",
    headline_metrics: [],
  };
}

const firstRun = runSummary(
  "run-11111111111111111111111111111111",
  "2026-08-07T12:00:00Z",
);
const secondRun = runSummary(
  "run-22222222222222222222222222222222",
  "2026-08-07T12:01:00Z",
);
const trialSummary = {
  trial_set_id: trialSetId,
  experiment_id: "exp-dashboard",
  title: "Repeated local evidence",
  created_at: "2026-08-07T12:02:00Z",
  member_count: 2,
  earliest_run_at: firstRun.started_at,
  latest_run_at: secondRun.ended_at,
  model: firstRun.model,
  execution_fingerprint: `sha256:${"b".repeat(64)}`,
  trial_set_digest: `sha256:${"c".repeat(64)}`,
  evidence_eligibilities: ["SYNTHETIC_ONLY"],
  environment_status: "CONSISTENT",
};
const emptyRunsIndex = {
  projection_version: projectionVersion,
  generated_at: "2026-08-07T12:03:00Z",
  runs: [],
  rejected: [],
  page: { limit: 200, returned: 0, total: 0, has_more: false, next_cursor: null },
};
const trialIndex = {
  projection_version: projectionVersion,
  generated_at: "2026-08-07T12:03:00Z",
  trial_sets: [trialSummary],
  rejected: [],
  page: { limit: 100, returned: 1, total: 1, has_more: false, next_cursor: null },
};
const trialDetail = {
  projection_version: projectionVersion,
  summary: trialSummary,
  hypothesis: "Run-level values should remain visible.",
  membership_policy: "same_execution_fingerprint_v1",
  metric_definitions_digest: `sha256:${"d".repeat(64)}`,
  reducer_version: "1.0.0",
  members: [
    { repetition_index: 0, run: firstRun },
    { repetition_index: 1, run: secondRun },
  ],
  variations: [{
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
      { repetition_index: 0, run_id: firstRun.run_id, value: "10000000", display_value: "10 ms", sample_count: 2 },
      { repetition_index: 1, run_id: secondRun.run_id, value: "12000000", display_value: "12 ms", sample_count: 2 },
    ],
    population: "run_level_measurements",
    weighting: "equal_per_run",
    summary_method: "per_run_scalar_sample_variation_v1",
  }],
  environment_drift_fields: [],
  design_status: "RETROSPECTIVE",
  inference: "DESCRIPTIVE_ONLY",
  request_population_policy: "separate_per_run_v1",
};

function dashboardFetch(input: RequestInfo | URL): Promise<Response> {
  const path = String(input);
  if (path === "/api/v1/runs?limit=200") return Promise.resolve(response(emptyRunsIndex));
  if (path === "/api/v1/trial-sets?limit=100") return Promise.resolve(response(trialIndex));
  if (path === `/api/v1/trial-sets/${trialSetId}`) return Promise.resolve(response(trialDetail));
  return Promise.resolve(response({ detail: "Not found" }, 404));
}

describe("Runs view", () => {
  it("shows rejected bundles without exposing measurements", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify({
      projection_version: "inferdrome.dashboard.v1",
      generated_at: "2026-08-07T12:00:00Z",
      runs: [],
      rejected: [{
        entry: "tampered-evidence",
        status: "REJECTED",
        code: "VERIFICATION_FAILED",
        message: "Bundle could not be verified.",
      }],
      page: {
        limit: 200,
        returned: 1,
        total: 1,
        has_more: false,
        next_cursor: null,
      },
    }), { status: 200, headers: { "content-type": "application/json" } }))));

    render(<MemoryRouter initialEntries={["/runs"]}><App /></MemoryRouter>);

    expect(await screen.findByText("No verified runs yet")).toBeInTheDocument();
    expect(screen.getByText("tampered-evidence")).toBeInTheDocument();
    expect(screen.getByText("Measurements withheld")).toBeInTheDocument();
  });
});

describe("Trial sets views", () => {
  it("shows verified retrospective groupings without causal or acceptance language", async () => {
    vi.stubGlobal("fetch", vi.fn(dashboardFetch));

    render(<MemoryRouter initialEntries={["/trial-sets"]}><App /></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: "Verified trial sets" })).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: /Repeated local evidence/ });
    expect(links.length).toBeGreaterThan(0);
    expect(links.every((link) => link.getAttribute("href") === `/trial-sets/${trialSetId}`)).toBe(true);
    expect(screen.getAllByText("RETROSPECTIVE").length).toBeGreaterThan(0);
    expect(screen.getAllByText("DESCRIPTIVE_ONLY").length).toBeGreaterThan(0);
    expect(screen.queryByText(/winner|significant|pass|fail/i)).not.toBeInTheDocument();
  });

  it("renders backend-authored run-level variation and keeps request populations separate", async () => {
    vi.stubGlobal("fetch", vi.fn(dashboardFetch));

    render(<MemoryRouter initialEntries={[`/trial-sets/${trialSetId}`]}><App /></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: "Repeated local evidence" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Run-to-run variation" })).toBeInTheDocument();
    expect(screen.getAllByText("10 ms").length).toBeGreaterThan(0);
    expect(screen.getAllByText("12 ms").length).toBeGreaterThan(0);
    expect(screen.getAllByText("11 ms").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/Each point is one verified run-level scalar/)).toBeInTheDocument();
    expect(screen.getByText(/Every member contributes at most one run-level scalar/)).toBeInTheDocument();
  });
});
