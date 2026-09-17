import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { api, ApiError } from "../lib/api";
import {
  ENGINE_EVALUATION_VERSION,
  evaluationApi,
  parseEvaluationDetail,
  type EvaluationReportDetail,
  type EvaluationReportIndex,
  type SGLangEvaluationStudyDetail
} from "../lib/evaluations";
import { Link, MemoryRouter } from "../lib/router";
import cacheFixture from "../test/evaluations/cache.json";
import sglangFixture from "../test/evaluations/sglang.json";
import studyFixture from "../test/evaluations/study.json";

// These authoritative fixtures are synthetic display tests, not runtime evidence.
const sglang = parseEvaluationDetail(sglangFixture, sglangFixture.summary.report_id) as SGLangEvaluationStudyDetail;
const study = parseEvaluationDetail(studyFixture, studyFixture.summary.report_id);
const cache = parseEvaluationDetail(cacheFixture, cacheFixture.summary.report_id);
const mixed: EvaluationReportIndex = { projection_version: ENGINE_EVALUATION_VERSION, reports: [study.summary, sglang.summary, cache.summary], rejected: [] };

beforeEach(() => {
  vi.spyOn(api, "listRuns").mockResolvedValue({
    projection_version: "inferdrome.dashboard.v1", generated_at: "2026-09-14T00:00:00Z", runs: [], rejected: []
  });
  vi.spyOn(evaluationApi, "list").mockResolvedValue(mixed);
  vi.spyOn(evaluationApi, "detail").mockImplementation(async (id) => {
    if (id === sglang.summary.report_id) return sglang;
    return id === study.summary.report_id ? study : cache;
  });
});
afterEach(() => vi.restoreAllMocks());

function open(path = "/evaluations") {
  return render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
}

describe("SGLang evaluations", () => {
  it("opens a bound report from a mixed index with engine identity and limitations", async () => {
    const user = userEvent.setup();
    open();
    const reportLink = await screen.findByRole("link", { name: sglang.summary.label });
    const engineRow = reportLink.closest("tr")!;
    expect(within(engineRow).getByText("SGLang 0.5.18")).toBeVisible();
    const legacyRow = screen.getByRole("link", { name: study.summary.label }).closest("tr")!;
    expect(within(legacyRow).queryByText(/SGLang|vLLM/)).not.toBeInTheDocument();

    await user.click(reportLink);
    expect(await screen.findByRole("heading", { name: sglang.summary.label })).toBeVisible();
    expect(screen.getByText("Synthetic only")).toBeVisible();
    expect(screen.getByText("Runtime unverified")).toBeVisible();
    expect(screen.getByText("Evidence ineligible")).toBeVisible();
    expect(screen.getByText(/Reported running and queued requests describe SGLang scheduler gauges/)).toHaveTextContent("scheduler-state age is unavailable");
    expect(screen.getByText(/Cold cache and warmup, drain and flush are declared/)).toHaveTextContent("does not verify the reset or cache state");
    expect(screen.getByText("Serving engine").nextElementSibling).toHaveTextContent("SGLang 0.5.18");
    expect(screen.getByText("Declared engine profile").nextElementSibling).toHaveTextContent("Single device · BF16");
    expect(screen.getByText("Engine binding digest").nextElementSibling?.querySelector("code")).toHaveAttribute("title", sglang.summary.engine_identity.engine_binding_sha256);
    expect(screen.getByText("Engine configuration digest").nextElementSibling?.querySelector("code")).toHaveAttribute("title", sglang.summary.engine_identity.engine_choice_sha256);
    expect(screen.getByText("Study config digest").nextElementSibling?.querySelector("code")).toHaveAttribute("title", sglang.summary.config_sha256);
    expect(screen.getByText("inferdrome.evaluation-study-report.v2")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Trial measurements" })).toBeVisible();

    await user.selectOptions(screen.getByLabelText("Trial"), String(sglang.trials.at(-1)!.index));
    await user.selectOptions(screen.getByLabelText("Population"), "background");
    expect(screen.getByRole("heading", { name: "Background population" })).toBeVisible();
    expect(screen.getByText("Engine binding digest")).toBeVisible();
  });

  it("removes the bound identity during refresh failure and restores it only with a new response", async () => {
    const user = userEvent.setup();
    open(`/evaluations/${sglang.summary.report_id}`);
    await screen.findByText("Engine binding digest");
    let finish!: (value: EvaluationReportDetail) => void;
    vi.mocked(evaluationApi.detail).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    await user.click(screen.getByRole("button", { name: "Refresh report" }));
    expect(screen.getByRole("button", { name: "Refresh report" })).toBeDisabled();
    expect(screen.queryByText("Engine binding digest")).not.toBeInTheDocument();
    await act(async () => finish(sglang));
    expect(screen.getByText("Engine binding digest")).toBeVisible();

    vi.mocked(evaluationApi.detail).mockRejectedValueOnce(new ApiError("This evaluation report is unavailable.", 404));
    await user.click(screen.getByRole("button", { name: "Refresh report" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This evaluation report is unavailable.");
    expect(screen.queryByText("Engine binding digest")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: sglang.summary.label })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Engine binding digest")).toBeVisible();
  });

  it("keeps legacy study and cache displays unchanged after leaving the bound report", async () => {
    const user = userEvent.setup();
    open(`/evaluations/${sglang.summary.report_id}`);
    await screen.findByText("Engine binding digest");
    for (const legacy of [study, cache]) {
      await user.click(screen.getByRole("link", { name: "Back to Evaluations" }));
      await user.click(await screen.findByRole("link", { name: legacy.summary.label }));
      expect(await screen.findByRole("heading", { name: legacy.summary.label })).toBeVisible();
      expect(screen.getByText("Config digest")).toBeVisible();
      expect(screen.queryByText("Engine binding digest")).not.toBeInTheDocument();
      expect(screen.queryByText("Serving engine")).not.toBeInTheDocument();
      expect(screen.queryByText(/SGLang scheduler gauges/)).not.toBeInTheDocument();
      expect(screen.queryByText(/vLLM/)).not.toBeInTheDocument();
    }
  });

  it("discards a delayed SGLang response after navigation to a legacy report", async () => {
    const user = userEvent.setup();
    let finish!: (value: EvaluationReportDetail) => void;
    vi.mocked(evaluationApi.detail).mockImplementation((id) => id === sglang.summary.report_id
      ? new Promise((resolve) => { finish = resolve; }) : Promise.resolve(study));
    render(<MemoryRouter initialEntries={[`/evaluations/${sglang.summary.report_id}`]}>
      <Link to={`/evaluations/${study.summary.report_id}`}>Legacy evaluation</Link><App />
    </MemoryRouter>);
    await screen.findByText("Reading pinned evaluation report…");
    await user.click(screen.getByRole("link", { name: "Legacy evaluation" }));
    await screen.findByRole("heading", { name: study.summary.label });
    await act(async () => finish(sglang));
    expect(screen.getByRole("heading", { name: study.summary.label })).toBeVisible();
    expect(screen.queryByRole("heading", { name: sglang.summary.label })).not.toBeInTheDocument();
    expect(screen.queryByText("Engine binding digest")).not.toBeInTheDocument();
  });
});
