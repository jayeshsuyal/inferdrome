import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { EvaluationContrasts, EvaluationRecoveryData } from "../components/EvaluationData";
import { DashboardAuthProvider } from "../context/DashboardAuthContext";
import { api, ApiError } from "../lib/api";
import {
  EVALUATION_VERSION,
  evaluationApi,
  parseEvaluationDetail,
  type EvaluationCacheDetail,
  type EvaluationReportDetail,
  type EvaluationStudyDetail
} from "../lib/evaluations";
import { Link, MemoryRouter } from "../lib/router";
import cacheFixture from "../test/evaluations/cache.json";
import emptyFixture from "../test/evaluations/empty.json";
import studyFixture from "../test/evaluations/study.json";

const study = parseEvaluationDetail(studyFixture, studyFixture.summary.report_id) as EvaluationStudyDetail;
const cache = parseEvaluationDetail(cacheFixture, cacheFixture.summary.report_id) as EvaluationCacheDetail;
const empty = parseEvaluationDetail(emptyFixture, emptyFixture.summary.report_id) as EvaluationStudyDetail;
const emptyRuns = {
  projection_version: "inferdrome.dashboard.v1" as const,
  generated_at: "2026-09-14T00:00:00Z",
  runs: [],
  rejected: []
};
const token = "inferdrome-dashboard-v1.dk-0123456789abcdef." + "A".repeat(43);

beforeEach(() => {
  vi.spyOn(api, "listRuns").mockResolvedValue(emptyRuns);
  vi.spyOn(evaluationApi, "list").mockResolvedValue({ projection_version: EVALUATION_VERSION, reports: [study.summary, cache.summary], rejected: [] });
  vi.spyOn(evaluationApi, "detail").mockImplementation(async (id) => id === study.summary.report_id ? study : id === empty.summary.report_id ? empty : cache);
});
afterEach(() => vi.restoreAllMocks());
function open(path = "/evaluations") {
  return render(<MemoryRouter initialEntries={[path]}>
    <App />
  </MemoryRouter>);
}

describe("Evaluations views", () => {
  it("filters the bounded index and keeps the evidence boundary visible", async () => {
    const user = userEvent.setup();
    open();
    expect(await screen.findByRole("heading", { name: "Evaluations" })).toBeVisible();
    await screen.findByRole("link", { name: "Study report 1" });
    expect(screen.getByText("Pinned report; source inputs not replayed")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Report kind"), "PREFIX_CACHE");
    expect(screen.queryByRole("link", { name: "Study report 1" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Prefix-cache report 1" }));
    expect(await screen.findByRole("heading", { name: "Prefix-cache report 1" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Evaluations" })).toHaveAttribute("aria-current", "page");
    await user.click(screen.getByRole("link", { name: "Back to Evaluations" }));
    expect(await screen.findByLabelText("Report kind")).toHaveValue("ALL");
  });

  it("selects all four study policies and keeps background and recovery separate", async () => {
    const user = userEvent.setup();
    open(`/evaluations/${study.summary.report_id}`);
    const trial = await screen.findByLabelText("Trial");
    await user.selectOptions(trial, "4");
    expect(screen.getByRole("heading", { name: "Foreground population" })).toBeVisible();
    expect(screen.getAllByText("27.777778 requests/s").length).toBeGreaterThan(0);
    expect(screen.getAllByText("0.833333 ratio").length).toBeGreaterThan(0);
    expect(screen.getAllByText("All successful requests, including SLO misses").length).toBe(4);
    expect(screen.getAllByText("BELOW REPORTING FLOOR").length).toBe(4);
    expect(screen.getByRole("table", { name: "Publication, decision and dispatch recovery from actual restoration" })).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Population"), "background");
    expect(screen.getByRole("heading", { name: "Background population" })).toBeVisible();
    expect(screen.getByText(/Background requests do not inflate foreground success/)).toBeVisible();
    expect(screen.queryByRole("table", { name: "Authoritative latency quantiles in nanoseconds" })).not.toBeInTheDocument();
    await user.selectOptions(trial, "1");
    expect(screen.getByLabelText("Population")).toHaveValue("foreground");
  });

  it("shows cache values without treatment promotion and switches the selected cell", async () => {
    const user = userEvent.setup();
    open(`/evaluations/${cache.summary.report_id}`);
    await screen.findByRole("heading", { name: "Four-cell comparison" });
    expect(screen.getByText("33.333333 requests/s")).toBeVisible();
    expect(screen.getAllByText("22.222222 requests/s").length).toBeGreaterThan(0);
    expect(screen.getByText("Low-replication rehearsal / pilot")).toBeVisible();
    expect(screen.getByText("Synthetic only")).toBeVisible();
    expect(screen.getByText("Runtime unverified")).toBeVisible();
    expect(screen.getByText("Evidence ineligible")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Cell"), "S1");
    expect(screen.getByRole("heading", { name: "S1 · Cell 2 details" })).toBeVisible();
    expect(screen.getAllByText("44.444444 requests/s").length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "Potential matching prefix blocks" })).toBeVisible();
    expect(screen.getByText(/Potential matching token-prefix blocks do not measure cache hits/)).toBeVisible();
    expect(screen.queryByText("Cache hits", { exact: true })).not.toBeInTheDocument();
  });

  it("labels zero coverage even when the source defaults to local measurement only", async () => {
    open(`/evaluations/${empty.summary.report_id}`);
    await screen.findByText("No returned measurements");
    expect(screen.getByText("Local measurement only")).toBeVisible();
    expect(screen.getByText("No returned trial measurements")).toBeVisible();
    expect(screen.getByText("SUPPRESSED INCOMPLETE STUDY")).toBeVisible();
    expect(screen.queryByLabelText("Trial")).not.toBeInTheDocument();
  });

  it("keeps measured zero distinct from unavailable and shows cancelled populations", async () => {
    const user = userEvent.setup();
    // Typed presentation variants test zero/null rendering; protocol fixtures above
    // remain unchanged and all execution claims remain explicitly synthetic.
    const variant: EvaluationCacheDetail = {
      ...cache, blocks: [{
        ...cache.blocks[0], cells: cache.blocks[0].cells.map((cell, index) => index === 0 ? {
          ...cell, population: {
            ...cell.population!,
            cancelled: true,
            metrics: cell.population!.metrics.map((metric) => metric.key === "slo_goodput_rps" ? { ...metric, value: "0.000000" } : metric)
          }
        } : index === 1 ? { ...cell, status: "UNAVAILABLE", reason: "MISSING_CELL_INPUT", population: null } : cell)
      }]
    };
    vi.mocked(evaluationApi.detail).mockResolvedValue(variant);
    open(`/evaluations/${cache.summary.report_id}`);
    await screen.findByText("Population cancelled");
    expect(screen.getAllByText("0.000000 requests/s").length).toBeGreaterThan(0);
    await user.selectOptions(screen.getByLabelText("Cell"), "S1");
    expect(screen.getByText("Cell measurements unavailable")).toBeVisible();
    expect(screen.queryByText("Population cancelled")).not.toBeInTheDocument();
  });

  it("copies supplied interval bounds and preserves observed zero versus censored recovery", () => {
    render(<>
      <EvaluationContrasts contrasts={[{
        label: "Shared: on minus off",
        complete_blocks: 8,
        mean_rps: "33.333333",
        lower_rps: "33.333333",
        upper_rps: "33.333333",
        interval_status: "AVAILABLE"
      }]} />
      <EvaluationRecoveryData recovery={{
        ...study.trials[0].recovery, intervals: [{ metric: "publication", status: "OBSERVED", duration_ns: "0", observation_horizon_ns: "100" }, {
          metric: "decision",
          status: "UNOBSERVED_OR_CENSORED",
          duration_ns: null,
          observation_horizon_ns: "100"
        }, {
          metric: "dispatch",
          status: "RESTORE_NOT_OBSERVED",
          duration_ns: null,
          observation_horizon_ns: null
        }]
      }} />
    </>);
    expect(screen.getByText("[33.333333, 33.333333] requests/s")).toBeVisible();
    const recovery = screen.getByRole("table", { name: "Publication, decision and dispatch recovery from actual restoration" });
    expect(within(recovery).getByText("0 ns")).toBeVisible();
    expect(within(recovery).getByText("UNOBSERVED OR CENSORED")).toBeVisible();
    expect(within(recovery).getAllByText("Unavailable")).toHaveLength(3);
  });

  it("removes old detail immediately on refresh and leaves a generic failure after source removal", async () => {
    const user = userEvent.setup();
    open(`/evaluations/${cache.summary.report_id}`);
    await screen.findByRole("heading", { name: "Four-cell comparison" });
    vi.mocked(evaluationApi.detail).mockRejectedValueOnce(new ApiError("This evaluation report is unavailable.", 404));
    await user.click(screen.getByRole("button", { name: "Refresh report" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This evaluation report is unavailable.");
    expect(screen.queryByRole("heading", { name: "Four-cell comparison" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Four-cell comparison" })).toBeVisible();
  });

  it("discards a delayed old-route response after navigating to another report", async () => {
    const user = userEvent.setup();
    let finish: (detail: EvaluationReportDetail) => void = () => undefined;
    vi.mocked(evaluationApi.detail).mockImplementation((id) => id === study.summary.report_id ? new Promise((resolve) => { finish = resolve; }) : Promise.resolve(cache));
    render(<MemoryRouter initialEntries={[`/evaluations/${study.summary.report_id}`]}>
      <Link to={`/evaluations/${cache.summary.report_id}`}>Other evaluation</Link>
      <App />
    </MemoryRouter>);
    await screen.findByText("Reading pinned evaluation report…");
    await user.click(screen.getByRole("link", { name: "Other evaluation" }));
    await screen.findByRole("heading", { name: "Four-cell comparison" });
    await act(async () => finish(study));
    expect(screen.queryByRole("heading", { name: "Trial measurements" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Four-cell comparison" })).toBeVisible();
  });

  it("hides evaluation data after revocation and reloads it only after unlock", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listRuns).mockRejectedValueOnce(new ApiError("Dashboard authentication failed.", 401));
    render(<DashboardAuthProvider>
      <MemoryRouter initialEntries={[`/evaluations/${cache.summary.report_id}`]}>
        <App />
      </MemoryRouter>
    </DashboardAuthProvider>);
    await user.type(await screen.findByLabelText("Bearer token"), token);
    await user.click(screen.getByRole("button", { name: "Unlock dashboard" }));
    await screen.findByRole("heading", { name: "Four-cell comparison" });
    vi.mocked(evaluationApi.detail).mockRejectedValueOnce(new ApiError("Dashboard authentication failed.", 401));
    await user.click(screen.getByRole("button", { name: "Refresh report" }));
    await screen.findByLabelText("Bearer token");
    expect(screen.queryByRole("heading", { name: "Four-cell comparison" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Bearer token"), token);
    await user.click(screen.getByRole("button", { name: "Unlock dashboard" }));
    expect(await screen.findByRole("heading", { name: "Four-cell comparison" })).toBeVisible();
  });
});
