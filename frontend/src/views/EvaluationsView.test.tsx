import { act, fireEvent, render, screen, within } from "@testing-library/react";
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

async function disclose(user: ReturnType<typeof userEvent.setup>, title: string) {
  const summary = screen.getByText(title, { selector: "summary .evaluation-disclosure-title" });
  await user.click(summary);
  expect(summary.closest("details")).toHaveAttribute("open");
}

describe("Evaluations views", () => {
  it("disables index refresh while loading and restores it after completion", async () => {
    let complete!: (value: Awaited<ReturnType<typeof evaluationApi.list>>) => void;
    vi.mocked(evaluationApi.list).mockReturnValueOnce(new Promise((resolve) => { complete = resolve; }));
    open();
    const button = screen.getByRole("button", { name: "Refresh reports" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    fireEvent.click(button);
    fireEvent.click(button);
    expect(evaluationApi.list).toHaveBeenCalledTimes(1);
    await act(async () => complete({ projection_version: EVALUATION_VERSION, reports: [study.summary], rejected: [] }));
    expect(button).toBeEnabled();
    expect(button).toHaveAttribute("aria-busy", "false");
  });

  it("keeps a disabled detail refresh control visible during its pending load", async () => {
    let complete!: (value: EvaluationReportDetail) => void;
    vi.mocked(evaluationApi.detail).mockReturnValueOnce(new Promise((resolve) => { complete = resolve; }));
    open(`/evaluations/${cache.summary.report_id}`);
    const button = screen.getByRole("button", { name: "Refresh report" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    fireEvent.click(button);
    expect(evaluationApi.detail).toHaveBeenCalledTimes(1);
    await act(async () => complete(cache));
    expect(screen.getByRole("button", { name: "Refresh report" })).toBeEnabled();
  });

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

  it("reveals the full index digest through a native disclosure", async () => {
    const user = userEvent.setup();
    open();
    const link = await screen.findByRole("link", { name: study.summary.label });
    const row = link.closest("tr")!;
    const digest = within(row).getByText(study.summary.report_sha256, { selector: "code" });
    const control = within(row).getByText(/^Report digest ·/, { selector: "summary" });
    expect(digest).not.toBeVisible();
    expect(control.closest("details")).not.toHaveAttribute("open");
    await user.click(control);
    expect(digest).toBeVisible();
    expect(digest).not.toHaveAttribute("title");
    expect(screen.getByText(/checks do not establish authorship or actual execution/)).toBeVisible();
    expect(within(row).getByText("24 returned records")).toBeVisible();
  });

  it.each([
    ["SUPPRESSED_INCOMPLETE", "Suppressed: incomplete report"],
    ["SUPPRESSED_DECLARATION_OR_EVIDENCE_MISMATCH", "Suppressed: declaration or evidence mismatch"],
  ])("separates completed reports from %s comparisons", async (comparisonStatus, comparisonLabel) => {
    const user = userEvent.setup();
    vi.mocked(evaluationApi.detail).mockResolvedValue({
      ...cache,
      summary: { ...cache.summary, comparison_status: comparisonStatus },
    });
    open(`/evaluations/${cache.summary.report_id}`);
    const heading = await screen.findByRole("heading", { name: "Report overview" });
    const overview = heading.closest<HTMLElement>(".evaluation-panel")!;
    const sourceFact = (label: string) => within(overview).getByText(label, { selector: "dt" }).parentElement!;
    expect(sourceFact("Reported completion")).toHaveTextContent("Completed");
    expect(sourceFact("Comparison availability")).toHaveTextContent(comparisonLabel);
    expect(screen.getByText("Evidence ineligible")).toBeVisible();
    for (const key of ["planned_blocks", "complete_blocks", "planned_cells", "completed_cells", "planned_offers", "returned_records"]) {
      const source = cache.coverage.find((item) => item.key === key)!;
      const visibleFact = within(overview).getAllByText(source.label, { selector: "dt" })[0].parentElement!;
      expect(visibleFact).toBeVisible();
      expect(within(visibleFact).getByText(source.value!)).toBeVisible();
    }
    expect(within(overview).getByText(/Returned records are not a success count/)).toBeVisible();
    await disclose(user, "Coverage and reporting details");
    const fullCoverage = screen.getByText("Coverage and reporting details").closest("details")!;
    for (const source of cache.coverage) {
      const fact = within(fullCoverage).getByText(source.label, { selector: "dt" }).parentElement!;
      expect(fact).toHaveTextContent(`${source.value} count`);
    }
  });

  it("keeps each study stratum identity visible before opening exact policy values", async () => {
    const first = study.strata[0];
    vi.mocked(evaluationApi.detail).mockResolvedValue({
      ...study,
      strata: [
        { ...first, index: 1, target_endpoint: "endpoint-a" },
        { ...first, index: 2, target_endpoint: "endpoint-b" },
      ],
    });
    open(`/evaluations/${study.summary.report_id}`);
    expect(await screen.findByText(/^Stratum 1 ·.*Target endpoint-a$/)).toBeVisible();
    expect(screen.getByText(/^Stratum 2 ·.*Target endpoint-b$/)).toBeVisible();
    expect(screen.getAllByRole("table", { name: "Four evaluation policies and reported goodput summaries" })).toHaveLength(2);
    expect(screen.queryByLabelText("Study stratum")).not.toBeInTheDocument();
  });

  it("keeps cancellation, unavailable p99 and censored recovery visible when measurements close", async () => {
    const user = userEvent.setup();
    const sourceTrial = study.trials[0];
    const variant: EvaluationStudyDetail = {
      ...study,
      trials: [{
        ...sourceTrial,
        status: "CANCELLED",
        foreground: { ...sourceTrial.foreground, cancelled: true },
        recovery: {
          ...sourceTrial.recovery,
          intervals: [{ metric: "decision", status: "UNOBSERVED_OR_CENSORED", duration_ns: null, observation_horizon_ns: "1234567" }],
        },
      }],
    };
    vi.mocked(evaluationApi.detail).mockResolvedValue(variant);
    open(`/evaluations/${study.summary.report_id}`);
    const title = await screen.findByText("Trial measurements", { selector: "summary .evaluation-disclosure-title" });
    const summary = title.closest("summary")!;
    expect(summary).toBeVisible();
    expect(summary).toHaveTextContent("Cancelled");
    expect(summary).toHaveTextContent("Population cancelled · p99 unavailable: below reporting floor");
    expect(summary).toHaveTextContent("Decision: Unobserved or censored");
    expect(summary.closest("details")).not.toHaveAttribute("open");
    await user.click(summary);
    expect(screen.getByLabelText("Trial")).toBeVisible();
    await user.click(summary);
    expect(screen.getByLabelText("Trial")).not.toBeVisible();
    expect(summary).toHaveTextContent("Decision: Unobserved or censored");
  });

  it("exposes full identities and exact trust flags without requiring hover", async () => {
    const user = userEvent.setup();
    open(`/evaluations/${cache.summary.report_id}`);
    const title = await screen.findByText("Provenance and report details", { selector: "summary .evaluation-disclosure-title" });
    const disclosure = title.closest("details")!;
    const fullHash = within(disclosure).getByText(cache.summary.report_sha256, { selector: "code" });
    expect(fullHash).not.toBeVisible();
    await user.click(title);
    expect(fullHash).toBeVisible();
    expect(fullHash).not.toHaveAttribute("title");
    for (const [label, value] of [
      ["Report ID", cache.summary.report_id],
      ["Plan digest", cache.summary.plan_sha256],
      ["Source replay status", cache.summary.source_replay],
      ["Runtime verification status", cache.summary.runtime_verification],
      ["Evidence eligible", "false"],
      ["Tokenizer reverified here flag", "false"],
    ]) {
      const fact = within(disclosure).getByText(label, { selector: "dt" }).parentElement!;
      expect(within(fact).getByText(value)).toBeVisible();
    }
    expect(within(disclosure).getByText(/Source cache treatment attribution/)).toHaveTextContent("UNVERIFIED");
    await disclose(user, "Workload and output diagnostics");
    expect(screen.getByText(cache.workload_verification, { selector: "code" })).toBeVisible();
  });

  it("selects all four study policies and keeps background and recovery separate", async () => {
    const user = userEvent.setup();
    open(`/evaluations/${study.summary.report_id}`);
    await screen.findByText("Trial measurements", { selector: "summary .evaluation-disclosure-title" });
    await disclose(user, "Trial measurements");
    const trial = screen.getByLabelText("Trial");
    await user.selectOptions(trial, "4");
    expect(screen.getByRole("heading", { name: "Foreground population" })).toBeVisible();
    expect(screen.getAllByText("27.777778 requests/s").length).toBeGreaterThan(0);
    expect(screen.getAllByText("0.833333 ratio")[0]).toBeVisible();
    await disclose(user, "Latency and dispatch timing");
    const latency = screen.getByRole("table", { name: "Authoritative latency quantiles" });
    expect(within(latency).getAllByText("All successful requests, including SLO misses")).toHaveLength(4);
    expect(within(latency).getAllByText("Below reporting floor")).toHaveLength(4);
    expect(screen.getByRole("table", { name: "Publication, decision and dispatch recovery from actual restoration" })).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Population"), "background");
    expect(screen.getByRole("heading", { name: "Background population" })).toBeVisible();
    expect(screen.getByText(/Background requests do not inflate foreground success/)).toBeVisible();
    expect(screen.queryByRole("table", { name: "Authoritative latency quantiles" })).not.toBeInTheDocument();
    await user.selectOptions(trial, "1");
    expect(screen.getByLabelText("Population")).toHaveValue("foreground");
  });

  it("shows cache values without treatment promotion and switches the selected cell", async () => {
    const user = userEvent.setup();
    open(`/evaluations/${cache.summary.report_id}`);
    await screen.findByRole("heading", { name: "Four-cell comparison" });
    expect(within(screen.getByRole("table", { name: "Authoritative descriptive contrasts in requests/s" })).getByText("33.333333 requests/s")).toBeVisible();
    expect(screen.getAllByText("22.222222 requests/s").length).toBeGreaterThan(0);
    expect(screen.getByText("Low-replication rehearsal / pilot")).toBeVisible();
    expect(screen.getByText("Synthetic only")).toBeVisible();
    expect(screen.getByText("Runtime unverified")).toBeVisible();
    expect(screen.getByText("Evidence ineligible")).toBeVisible();
    await disclose(user, "Cell measurements");
    await user.selectOptions(screen.getByLabelText("Cell"), "S1");
    expect(screen.getByRole("heading", { name: "S1 · Cell 2 details" })).toBeVisible();
    expect(screen.getAllByText("44.444444 requests/s").length).toBeGreaterThan(0);
    await disclose(user, "Workload and output diagnostics");
    expect(screen.getByRole("heading", { name: "Potential matching prefix blocks" })).toBeVisible();
    expect(screen.getByText(/Potential matching token-prefix blocks do not measure cache hits/)).toBeVisible();
    expect(screen.queryByText("Cache hits", { exact: true })).not.toBeInTheDocument();
  });

  it("labels zero coverage even when the source defaults to local measurement only", async () => {
    open(`/evaluations/${empty.summary.report_id}`);
    await screen.findByText("No returned measurements");
    expect(screen.getByText("Local measurement only")).toBeVisible();
    expect(screen.getByText("No returned trial measurements", { selector: ".evaluation-disclosure-cue" })).toBeVisible();
    expect(screen.getByText("Suppressed: incomplete study")).toBeVisible();
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
    await screen.findByText("Cell measurements", { selector: "summary .evaluation-disclosure-title" });
    await disclose(user, "Cell measurements");
    expect(screen.getByText("Population cancelled", { selector: ".status-badge" })).toBeVisible();
    await disclose(user, "Exact population values");
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
    expect(within(screen.getByRole("table", { name: "Authoritative descriptive contrasts in requests/s" })).getByText("[33.333333, 33.333333] requests/s")).toBeVisible();
    const recovery = screen.getByRole("table", { name: "Publication, decision and dispatch recovery from actual restoration" });
    expect(within(recovery).getByText("0 ns")).toBeVisible();
    expect(within(recovery).getByText("Unobserved or censored")).toBeVisible();
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
