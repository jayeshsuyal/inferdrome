import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { DashboardAuthProvider } from "../context/DashboardAuthContext";
import { api, ApiError } from "../lib/api";
import { MemoryRouter } from "../lib/router";
import type { RoutingQualificationDetail, RoutingQualificationTrialView } from "../lib/types";

const qualificationId = "stale-telemetry-qualification-v1";
const retainedDigest = `sha256:${"f".repeat(64)}`;
const auditToken = "inferdrome-dashboard-v1.dk-0123456789abcdef." + "A".repeat(43);

// Typed synthetic view data: the transport/parser contract is tested separately.
function trial(policy: RoutingQualificationTrialView["policy_id"]): RoutingQualificationTrialView {
  const closed = policy === "fail_closed_required_load_v1";
  const open = policy === "explicit_fail_open_stale_load_v1";
  return {
    policy_id: policy,
    repetition_index: 0,
    trial_id: `trial-${policy}`,
    request_denominator: 6,
    reset: {
      virtual_time_ms: 0,
      endpoint_a_instance_id: `${policy}-endpoint-a`,
      endpoint_b_instance_id: `${policy}-endpoint-b`,
      observer_epochs: [1, 1, 1],
      queue_cleared: true,
      load_state_cleared: true,
      kv_state_cleared: true,
    },
    focal_request_id: "request-002",
    focal_decision_id: `decision-${policy}-002`,
    focal_endpoint_states: [
      { endpoint_id: "endpoint-a", health_epoch: 2, health_age_ms: 0, health_admissibility: "ADMISSIBLE", load_epoch: 1, load_age_ms: 10, load_admissibility: "INADMISSIBLE" },
      { endpoint_id: "endpoint-b", health_epoch: 2, health_age_ms: 0, health_admissibility: "ADMISSIBLE", load_epoch: 1, load_age_ms: 10, load_admissibility: "INADMISSIBLE" },
    ],
    selected_endpoint_id: closed ? null : open ? "endpoint-b" : "endpoint-a",
    fallback_reason: closed ? "REQUIRED_LOAD_STALE" : open ? "STALE_LOAD_FAIL_OPEN" : "HEALTH_ONLY_TIE_BREAK",
    terminal_status: closed ? "NO_SAFE_ROUTE" : open ? "TIMED_OUT" : "SUCCEEDED",
    terminal_reason: closed ? "REQUIRED_LOAD_STALE" : open ? "SIMULATED_ENDPOINT_B_SATURATED" : "SIMULATED_ENDPOINT_A_COMPLETED",
    reset_receipt_sha256: `sha256:${"1".repeat(64)}`,
    state_observations_sha256: `sha256:${"2".repeat(64)}`,
    route_decisions_sha256: `sha256:${"3".repeat(64)}`,
    terminal_outcomes_sha256: `sha256:${"4".repeat(64)}`,
    terminal_population: [
      { status: "SUCCEEDED", count: closed || open ? 2 : 6 },
      { status: "TIMED_OUT", count: open ? 4 : 0 },
      { status: "FAILED", count: 0 },
      { status: "CANCELLED", count: 0 },
      { status: "NO_SAFE_ROUTE", count: closed ? 4 : 0 },
    ],
    terminal_population_total: 6,
  };
}

const detail: RoutingQualificationDetail = {
  projection_version: "inferdrome.routing-qualification-dashboard.v1",
  summary: {
    qualification_id: qualificationId,
    retained_digest: retainedDigest,
    source_campaign_id: "routing-campaign-v1",
    source_package_retained_digest: `sha256:${"a".repeat(64)}`,
    source_execution_mode: "SYNTHETIC_CPU_ONLY",
    repetitions_per_mode: 1,
    population_accounting: "SEPARATE_PER_TRIAL_NO_POOLING",
    verified_by_source_replay: true,
    verified_descriptor_binding: true,
  },
  fault_timeline: {
    load_observer_pause_at_ms: 15,
    health_collection_continues: true,
    focal_decision_time_ms: 20,
    health_age_ms: 0,
    load_age_ms: 10,
    freshness_bound_ms: 5,
  },
  trials: [trial("fail_closed_required_load_v1"), trial("explicit_fail_open_stale_load_v1"), trial("typed_admissible_state_only_v1")],
  source_receipts_path: "/routing-campaigns/routing-campaign-v1",
  descriptor_download_path: "/api/v1/routing-qualifications/stale-telemetry-qualification-v1/evidence",
  interpretation_boundary: "MEASUREMENT_EVIDENCE_ONLY",
};

beforeEach(() => {
  vi.spyOn(api, "listRuns")
    .mockRejectedValueOnce(new ApiError("dashboard authentication failed", 401))
    .mockResolvedValue({ projection_version: "inferdrome.dashboard.v1", generated_at: "2026-09-13T00:00:00Z", runs: [], rejected: [] });
  vi.spyOn(api, "getRoutingQualification").mockResolvedValue(detail);
});

afterEach(() => vi.restoreAllMocks());

async function unlock(user: ReturnType<typeof userEvent.setup>, token = auditToken) {
  await user.type(await screen.findByLabelText("Bearer token"), token);
  await user.click(screen.getByRole("button", { name: "Unlock dashboard" }));
  return screen.findByRole("button", { name: "Download verified descriptor" });
}

function renderQualification() {
  render(
    <DashboardAuthProvider>
      <MemoryRouter initialEntries={[`/routing-qualifications/${qualificationId}`]}>
        <App />
      </MemoryRouter>
    </DashboardAuthProvider>,
  );
}

describe("qualification descriptor download recovery", () => {
  it("immediately relocks on download 401 and supports replacement unlock and retry", async () => {
    const download = vi.spyOn(api, "downloadRoutingQualification")
      .mockRejectedValueOnce(new ApiError("dashboard authentication failed", 401))
      .mockResolvedValue(retainedDigest);
    const user = userEvent.setup();
    renderQualification();
    await user.click(await unlock(user));

    expect(await screen.findByRole("heading", { name: "Unlock evidence views" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download verified descriptor" })).not.toBeInTheDocument();
    await user.click(await unlock(user, auditToken.replace(/A/g, "B")));

    expect(await screen.findByRole("status")).toHaveTextContent("Verified descriptor download started.");
    expect(download.mock.calls).toEqual([
      [qualificationId, retainedDigest, expect.any(AbortSignal)],
      [qualificationId, retainedDigest, expect.any(AbortSignal)],
    ]);
  });

  it("keeps ordinary download failures in the view and retries the same verified descriptor", async () => {
    const download = vi.spyOn(api, "downloadRoutingQualification")
      .mockRejectedValueOnce(new ApiError("Descriptor is temporarily unavailable.", 500))
      .mockResolvedValue(retainedDigest);
    const user = userEvent.setup();
    renderQualification();
    await user.click(await unlock(user));

    expect(await screen.findByRole("alert")).toHaveTextContent("Descriptor is temporarily unavailable.");
    expect(screen.queryByRole("heading", { name: "Unlock evidence views" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Download verified descriptor" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Verified descriptor download started.");
    expect(download.mock.calls).toEqual([
      [qualificationId, retainedDigest, expect.any(AbortSignal)],
      [qualificationId, retainedDigest, expect.any(AbortSignal)],
    ]);
  });

  it("ignores a deferred old-session download 401 after replacement unlock", async () => {
    let rejectOldDownload!: (reason: unknown) => void;
    const oldDownload = new Promise<string>((_resolve, reject) => {
      rejectOldDownload = reject;
    });
    const download = vi.spyOn(api, "downloadRoutingQualification")
      .mockReturnValueOnce(oldDownload)
      .mockResolvedValue(retainedDigest);
    const user = userEvent.setup();
    renderQualification();
    await user.click(await unlock(user));
    expect(download).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Lock dashboard" }));
    expect(download.mock.calls[0]?.[2]?.aborted).toBe(true);
    await unlock(user, auditToken.replace(/A/g, "B"));
    await act(async () => {
      rejectOldDownload(new ApiError("old dashboard authentication failed", 401));
    });

    expect(screen.queryByRole("heading", { name: "Unlock evidence views" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Download verified descriptor" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Verified descriptor download started.");
    expect(download.mock.calls).toEqual([
      [qualificationId, retainedDigest, expect.any(AbortSignal)],
      [qualificationId, retainedDigest, expect.any(AbortSignal)],
    ]);
    expect(download.mock.calls[1]?.[2]?.aborted).toBe(false);
  });
});
