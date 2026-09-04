import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { MemoryRouter } from "./lib/router";
import {
  comparableControlledDetail,
  controlledComparisonIndex,
  incomparableControlledDetail,
} from "./test/controlledComparisonFixtures";

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

const routingCampaignSummary = {
  campaign_id: "routing-campaign-v1" as const,
  retained_digest: `sha256:${"e".repeat(64)}`,
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

function routingTrial(
  trialId: string,
  policyId: string,
) {
  const token = trialId.replace("trial-", "");
  const requests = Array.from({ length: 6 }, (_, sequenceIndex) => {
    const decisionTime = sequenceIndex * 10;
    const stale = decisionTime >= 20;
    const selected = !stale || policyId === "explicit_fail_open_stale_load_v1"
      ? "endpoint-b"
      : policyId === "typed_admissible_state_only_v1"
        ? "endpoint-a"
        : null;
    const status = selected === null
      ? "NO_SAFE_ROUTE"
      : selected === "endpoint-b" && stale
        ? "TIMED_OUT"
        : "SUCCEEDED";
    const fallback = !stale
      ? "NONE"
      : policyId === "fail_closed_required_load_v1"
        ? "REQUIRED_LOAD_STALE"
        : policyId === "explicit_fail_open_stale_load_v1"
          ? "STALE_LOAD_FAIL_OPEN"
          : "HEALTH_ONLY_TIE_BREAK";
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
  const terminalPopulation = ["SUCCEEDED", "TIMED_OUT", "FAILED", "CANCELLED", "NO_SAFE_ROUTE"].map((status) => ({
    status,
    count: requests.filter((request) => request.terminal.status === status).length,
  }));
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
    terminal_population: terminalPopulation,
    terminal_population_total: 6,
  };
}

const routingCampaignIndex = {
  projection_version: "inferdrome.routing-campaign-dashboard.v1",
  routing_campaigns: [routingCampaignSummary],
  rejected: [],
  page: { limit: 25, returned: 1, total: 1, has_more: false, next_cursor: null },
};

const routingCampaignDetail = {
  projection_version: "inferdrome.routing-campaign-dashboard.v1",
  summary: routingCampaignSummary,
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
  retained_digest: `sha256:${"f".repeat(64)}`,
  source_campaign_id: "routing-campaign-v1" as const,
  source_package_retained_digest: routingCampaignSummary.retained_digest,
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
  const outcome = policyId === "fail_closed_required_load_v1"
    ? [null, "REQUIRED_LOAD_STALE", "NO_SAFE_ROUTE", "REQUIRED_LOAD_STALE"] as const
    : policyId === "explicit_fail_open_stale_load_v1"
      ? ["endpoint-b", "STALE_LOAD_FAIL_OPEN", "TIMED_OUT", "SIMULATED_ENDPOINT_B_SATURATED"] as const
      : ["endpoint-a", "HEALTH_ONLY_TIE_BREAK", "SUCCEEDED", "SIMULATED_ENDPOINT_A_COMPLETED"] as const;
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
    focal_endpoint_states: ["endpoint-a", "endpoint-b"].map((endpointId) => ({
      endpoint_id: endpointId,
      health_epoch: 2,
      health_age_ms: 0,
      health_admissibility: "ADMISSIBLE" as const,
      load_epoch: 1,
      load_age_ms: 10,
      load_admissibility: "INADMISSIBLE" as const,
    })),
    selected_endpoint_id: outcome[0],
    fallback_reason: outcome[1],
    terminal_status: outcome[2],
    terminal_reason: outcome[3],
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

const routingQualificationIndex = {
  projection_version: "inferdrome.routing-qualification-dashboard.v1",
  routing_qualifications: [routingQualificationSummary],
  rejected: [],
  page: { limit: 25, returned: 1, total: 1, has_more: false, next_cursor: null },
};

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

function dashboardFetch(input: RequestInfo | URL): Promise<Response> {
  const path = String(input);
  if (path === "/api/v1/runs?limit=200") return Promise.resolve(response(emptyRunsIndex));
  if (path === "/api/v1/trial-sets?limit=100") return Promise.resolve(response(trialIndex));
  if (path === `/api/v1/trial-sets/${trialSetId}`) return Promise.resolve(response(trialDetail));
  if (path === "/api/v1/routing-campaigns?limit=25") return Promise.resolve(response(routingCampaignIndex));
  if (path === "/api/v1/routing-campaigns/routing-campaign-v1") return Promise.resolve(response(routingCampaignDetail));
  if (path === "/api/v1/routing-qualifications?limit=25") return Promise.resolve(response(routingQualificationIndex));
  if (path === "/api/v1/routing-qualifications/stale-telemetry-qualification-v1") return Promise.resolve(response(routingQualificationDetail));
  if (path === "/api/v1/controlled-comparisons?limit=100") return Promise.resolve(response(controlledComparisonIndex));
  if (path === `/api/v1/controlled-comparisons/${comparableControlledDetail.summary.comparison_plan_id}`) {
    return Promise.resolve(response(comparableControlledDetail));
  }
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

describe("Routing campaign views", () => {
  it("lists only replay-verified campaigns and identifies withheld rather than unverified content", async () => {
    vi.stubGlobal("fetch", vi.fn(dashboardFetch));

    render(<MemoryRouter initialEntries={["/routing-campaigns"]}><App /></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: "Verified routing campaigns" })).toBeInTheDocument();
    const campaigns = screen.getAllByRole("link", { name: /routing-campaign-v1/ });
    expect(campaigns).toHaveLength(2);
    for (const campaign of campaigns) {
      expect(campaign).toHaveAttribute("href", "/routing-campaigns/routing-campaign-v1");
    }
    expect(screen.getAllByText("Verified by replay").length).toBeGreaterThan(0);
    expect(screen.getByText("MEASUREMENT_EVIDENCE_ONLY")).toBeInTheDocument();
    expect(screen.queryByText(/winner|promotion|pass/i)).not.toBeInTheDocument();
  });

  it("renders reset, fault, telemetry, decision, and complete terminal receipts", async () => {
    vi.stubGlobal("fetch", vi.fn(dashboardFetch));

    render(
      <MemoryRouter initialEntries={["/routing-campaigns/routing-campaign-v1"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Fault timeline" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("routing-campaign-v1");
    expect(screen.getAllByRole("heading", { name: "Cold reset receipt" })).toHaveLength(3);
    expect(screen.getAllByRole("heading", { name: "Per-request routing receipts" })).toHaveLength(3);
    expect(screen.getAllByRole("heading", { name: "Complete terminal population" })).toHaveLength(3);
    expect(screen.getAllByText("Admissible").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Inadmissible").length).toBeGreaterThan(0);
    expect(screen.getAllByText("REQUIRED_LOAD_STALE").length).toBeGreaterThan(0);
    expect(screen.getAllByText("NO SAFE ROUTE").length).toBeGreaterThan(0);
    expect(screen.queryByText(/winner|promotion|pass/i)).not.toBeInTheDocument();
  });
});

describe("Causal qualification views", () => {
  it("lists only replay-and-binding-verified descriptors without a verdict", async () => {
    vi.stubGlobal("fetch", vi.fn(dashboardFetch));

    render(<MemoryRouter initialEntries={["/routing-qualifications"]}><App /></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: "Verified qualification descriptors" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /stale-telemetry-qualification-v1/ })[0]).toHaveAttribute(
      "href",
      "/routing-qualifications/stale-telemetry-qualification-v1",
    );
    expect(screen.getByText("MEASUREMENT_EVIDENCE_ONLY")).toBeInTheDocument();
    expect(screen.getByText(/not a winner, recommendation, or acceptance verdict/i)).toBeInTheDocument();
  });

  it("renders the fixed causal chain, separate populations, and bounded evidence actions", async () => {
    vi.stubGlobal("fetch", vi.fn(dashboardFetch));

    render(
      <MemoryRouter initialEntries={["/routing-qualifications/stale-telemetry-qualification-v1"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "What the router knew at the focal request" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Separate terminal populations" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Cold-reset receipts" })).toBeInTheDocument();
    expect(screen.getAllByText("REQUIRED_LOAD_STALE").length).toBeGreaterThan(0);
    expect(screen.getAllByText("STALE_LOAD_FAIL_OPEN").length).toBeGreaterThan(0);
    expect(screen.getAllByText("HEALTH_ONLY_TIE_BREAK").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Download verified descriptor" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open source receipts" })).toHaveAttribute(
      "href",
      "/routing-campaigns/routing-campaign-v1",
    );
  });
});

describe("Controlled comparisons views", () => {
  it("shows operator-attested plans, result filters, and the ad hoc comparison utility", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn(dashboardFetch));

    render(<MemoryRouter initialEntries={["/comparisons"]}><App /></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: "Controlled comparisons" })).toBeInTheDocument();
    expect(screen.getAllByText("OPERATOR_ATTESTED").length).toBeGreaterThan(0);
    expect(screen.getByText(/Plan chronology is not independently proven/)).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /Compare two runs/ }).every(
      (link) => link.getAttribute("href") === "/compare",
    )).toBe(true);
    expect(screen.getAllByText("Suppressed").length).toBeGreaterThan(0);
    expect(screen.getAllByText("No result artifact").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Withheld").length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "Incomparable" }));
    expect(screen.getAllByText("Concurrency control mismatch").length).toBeGreaterThan(0);
    expect(screen.queryAllByText("Concurrency 1 versus 2")).toHaveLength(0);
    expect(screen.queryAllByText("Concurrency plan awaiting evidence")).toHaveLength(0);
    expect(screen.queryAllByText("Concurrency result withheld")).toHaveLength(0);
  });

  it("renders the allowlisted design, controls, shared-axis points, and neutral estimate copy", async () => {
    vi.stubGlobal("fetch", vi.fn(dashboardFetch));

    render(
      <MemoryRouter initialEntries={[`/comparisons/${comparableControlledDetail.summary.comparison_plan_id}`]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Concurrency 1 versus 2" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Predeclared design" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Comparability result" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Outcome estimate" })).toBeInTheDocument();
    expect(screen.getByText("OBSERVED_V1_ALLOWLIST_ONLY")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Baseline repeat 1/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Candidate repeat 2/ })).toBeInTheDocument();
    expect(screen.getByText("Exact paired run differences")).toBeInTheDocument();
    expect(screen.getAllByText("+1000000 ns")).toHaveLength(2);
    expect(screen.getByText(/colors represent arm roles only/)).toBeInTheDocument();
    expect(screen.getByText(
      /4 of 4 workspaces verified.*EVIDENCE COMPLETE.*result published/,
    )).toBeInTheDocument();
    expect(screen.getByText(/No confidence interval, significance test, causal attribution/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Baseline concurrency trials/ })).toHaveAttribute(
      "href",
      `/trial-sets/${comparableControlledDetail.baseline_trial_set.trial_set_id}`,
    );
  });

  it("keeps controls but withholds Trial Set projections and every estimate when incomparable", async () => {
    const incomparableFetch = (input: RequestInfo | URL): Promise<Response> => {
      const path = String(input);
      if (path === `/api/v1/controlled-comparisons/${incomparableControlledDetail.summary.comparison_plan_id}`) {
        return Promise.resolve(response(incomparableControlledDetail));
      }
      return dashboardFetch(input);
    };
    vi.stubGlobal("fetch", vi.fn(incomparableFetch));

    render(
      <MemoryRouter initialEntries={[`/comparisons/${incomparableControlledDetail.summary.comparison_plan_id}`]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Comparability result" })).toBeInTheDocument();
    expect(screen.getAllByText("INCOMPARABLE").length).toBeGreaterThan(0);
    expect(screen.getByText("Unsatisfied")).toBeInTheDocument();
    expect(screen.getByText(/Trial Set summaries and run-level measurements are withheld/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Outcome estimate" })).not.toBeInTheDocument();
    expect(screen.queryByText("Exact paired run differences")).not.toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /Baseline repeat/ })).not.toBeInTheDocument();
    expect(screen.queryByText("1 ms")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Baseline concurrency trials/ })).not.toBeInTheDocument();
  });
});
