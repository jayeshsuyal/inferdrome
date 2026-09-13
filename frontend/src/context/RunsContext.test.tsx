import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../components/AppShell";
import { useRunDetail } from "../hooks/useRunDetail";
import { api } from "../lib/api";
import { MemoryRouter } from "../lib/router";
import type { Comparison, RunDetail, RunIndex, RunSummary } from "../lib/types";
import { CompareView } from "../views/CompareView";
import { RunsProvider, useRuns } from "./RunsContext";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((complete, fail) => { resolve = complete; reject = fail; });
  return { promise, resolve, reject };
}

function summary(character: string, digest = character): RunSummary {
  return {
    run_id: `run-${character.repeat(32)}`, experiment_id: "snapshot-fixture", title: "Snapshot fixture",
    model: "inferdrome/fake-model", producer_name: "inferdrome_fake", producer_version: "1.0.0",
    adapter_name: "fake", adapter_version: "1.0.0", execution_mode: "synthetic_fixture",
    started_at: `2026-08-07T12:0${character === "a" ? "0" : "1"}:00Z`, ended_at: "2026-08-07T12:02:00Z",
    duration_ns: 1_000_000_000, integrity_status: "VALID", evidence_eligibility: "SYNTHETIC_ONLY",
    environment_completeness: "PARTIAL", replayability: "FULL", bundle_digest: `sha256:${digest.repeat(64)}`,
    measured_requests: 2, successful_requests: 2, failed_requests: 0, error_rate: "0",
    ttft_p50_ns: null, ttft_p95_ns: null, output_token_throughput_per_s: "4", headline_metrics: [],
  };
}

function index(runs: readonly RunSummary[]): RunIndex {
  return { projection_version: "inferdrome.dashboard.v1", generated_at: "2026-08-07T12:03:00Z", runs, rejected: [] };
}

// API calls are mocked: these tests exercise request ordering and snapshot identity,
// not the evidence parser or actual bundle verification.
function detail(run: RunSummary): RunDetail {
  return {
    projection_version: "inferdrome.dashboard.v1", summary: run,
    verification: { bundle_digest: run.bundle_digest },
  } as RunDetail;
}

function comparison(baseline: string, candidate: string, reason = "Synthetic evidence withheld"): Comparison {
  return {
    projection_version: "inferdrome.dashboard.v1", baseline_run_id: baseline, candidate_run_id: candidate,
    status: "INCOMPARABLE", reasons: [reason], metric_deltas: [], context_changes: [], directionality: "NEUTRAL",
  };
}

function Refresh() {
  const runs = useRuns();
  return <button type="button" onClick={runs.refresh}>Refresh test snapshot</button>;
}

function DetailProbe({ runId }: { readonly runId: string }) {
  const request = useRunDetail(runId);
  return <>
    <Refresh />
    <output aria-label="detail state">{request.status}</output>
    <output aria-label="detail digest">{request.data?.summary.bundle_digest ?? "none"}</output>
    {request.error ? <p role="alert">{request.error.message}</p> : null}
    <button type="button" onClick={request.retry}>Retry selected run</button>
  </>;
}

function mountDetail(runId: string) {
  return render(<RunsProvider><DetailProbe runId={runId} /></RunsProvider>);
}

afterEach(() => vi.restoreAllMocks());

describe("verified run snapshot coordination", () => {
  it("waits for the whole index before requesting an explicit detail, including a warm server cache", async () => {
    const run = summary("a");
    const pending = deferred<RunIndex>();
    vi.spyOn(api, "listRuns").mockReturnValue(pending.promise);
    const getRun = vi.spyOn(api, "getRun").mockResolvedValue(detail(run));
    mountDetail(run.run_id);
    expect(screen.getByLabelText("detail state")).toHaveTextContent("loading");
    expect(getRun).not.toHaveBeenCalled();
    await act(async () => pending.resolve(index([run])));
    expect(await screen.findByLabelText("detail digest")).toHaveTextContent(run.bundle_digest);
    expect(getRun).toHaveBeenCalledTimes(1);
  });

  it("invalidates removed selections and recovers restored evidence with one refresh", async () => {
    const run = summary("a");
    const refresh = deferred<RunIndex>();
    vi.spyOn(api, "listRuns").mockResolvedValueOnce(index([run])).mockReturnValueOnce(refresh.promise).mockResolvedValueOnce(index([run]));
    const getRun = vi.spyOn(api, "getRun").mockResolvedValue(detail(run));
    const user = userEvent.setup();
    mountDetail(run.run_id);
    await waitFor(() => expect(screen.getByLabelText("detail digest")).toHaveTextContent(run.bundle_digest));

    await user.click(screen.getByRole("button", { name: "Refresh test snapshot" }));
    expect(screen.getByLabelText("detail digest")).toHaveTextContent("none");
    expect(screen.getByLabelText("detail state")).toHaveTextContent("loading");
    await act(async () => refresh.resolve(index([])));
    expect(await screen.findByRole("alert")).toHaveTextContent("not in the latest verified run snapshot");
    expect(getRun).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Retry selected run" }));
    await waitFor(() => expect(screen.getByLabelText("detail digest")).toHaveTextContent(run.bundle_digest));
    expect(getRun).toHaveBeenCalledTimes(2);
  });

  it("does not render a late old digest after a same-ID replacement", async () => {
    const original = summary("a"); const replaced = summary("a", "c");
    const obsolete = deferred<RunDetail>();
    vi.spyOn(api, "listRuns").mockResolvedValueOnce(index([original])).mockResolvedValueOnce(index([replaced]));
    const getRun = vi.spyOn(api, "getRun").mockReturnValueOnce(obsolete.promise).mockResolvedValueOnce(detail(replaced));
    const user = userEvent.setup();
    mountDetail(original.run_id);
    await waitFor(() => expect(getRun).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Refresh test snapshot" }));
    await waitFor(() => expect(screen.getByLabelText("detail digest")).toHaveTextContent(replaced.bundle_digest));
    await act(async () => obsolete.resolve(detail(original)));
    expect(screen.getByLabelText("detail digest")).toHaveTextContent(replaced.bundle_digest);
    expect(getRun).toHaveBeenCalledTimes(2);
  });

  it("rejects a detail digest outside the refreshed index and recovers after refresh failure", async () => {
    const run = summary("a");
    vi.spyOn(api, "listRuns").mockResolvedValueOnce(index([run])).mockRejectedValueOnce(new Error("Index unavailable")).mockResolvedValueOnce(index([run]));
    vi.spyOn(api, "getRun").mockResolvedValueOnce(detail(summary("a", "c"))).mockResolvedValueOnce(detail(run));
    const user = userEvent.setup();
    mountDetail(run.run_id);
    expect(await screen.findByRole("alert")).toHaveTextContent("does not match the latest verified run snapshot");
    expect(screen.getByLabelText("detail digest")).toHaveTextContent("none");
    await user.click(screen.getByRole("button", { name: "Retry selected run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Index unavailable");
    await user.click(screen.getByRole("button", { name: "Retry selected run" }));
    await waitFor(() => expect(screen.getByLabelText("detail digest")).toHaveTextContent(run.bundle_digest));
  });

  it.each(["runs", "evidence"])("uses router trailing-slash identity for the %s sidebar context", async (route) => {
    const selected = summary("a"); const latest = summary("b");
    vi.spyOn(api, "listRuns").mockResolvedValue(index([selected, latest]));
    render(<MemoryRouter initialEntries={[`/${route}/${selected.run_id}/`]}><RunsProvider><AppShell><p>Page</p></AppShell></RunsProvider></MemoryRouter>);
    await screen.findByText("2 verified · 0 rejected");
    expect(screen.getByRole("link", { name: "Run detail" })).toHaveAttribute("href", `/runs/${selected.run_id}`);
    expect(screen.getByRole("link", { name: "Evidence" })).toHaveAttribute("href", `/evidence/${selected.run_id}`);
    expect(screen.getByRole("button", { name: "Refresh runs" })).toBeEnabled();
    expect(screen.queryByText("Index current")).not.toBeInTheDocument();
  });

  it("clears an old comparison on replacement and preserves a removed choice until explicit reselection", async () => {
    const first = summary("a"); const second = summary("b"); const replaced = summary("a", "c");
    const pending = deferred<RunIndex>(); const obsolete = deferred<Comparison>();
    vi.spyOn(api, "listRuns").mockResolvedValueOnce(index([first, second])).mockReturnValueOnce(pending.promise).mockResolvedValueOnce(index([replaced, second])).mockResolvedValueOnce(index([second]));
    const compare = vi.spyOn(api, "compareRuns")
      .mockResolvedValueOnce(comparison(first.run_id, second.run_id, "Original result"))
      .mockReturnValueOnce(obsolete.promise)
      .mockResolvedValueOnce(comparison(first.run_id, second.run_id, "Replacement result"));
    const user = userEvent.setup();
    render(<RunsProvider><Refresh /><CompareView /></RunsProvider>);
    expect(await screen.findByText("Original result")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh test snapshot" }));
    expect(screen.queryByText("Original result")).not.toBeInTheDocument();
    expect(compare).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve(index([first, second])));
    await waitFor(() => expect(compare).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole("button", { name: "Refresh test snapshot" }));
    expect(await screen.findByText("Replacement result")).toBeInTheDocument();
    await act(async () => obsolete.resolve(comparison(first.run_id, second.run_id, "Obsolete result")));
    expect(screen.queryByText("Obsolete result")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh test snapshot" }));
    expect(await screen.findByText("A selected run is unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Replacement result")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Baseline")).toHaveValue(first.run_id);
    expect(compare).toHaveBeenCalledTimes(3);
  });

  it("rejects a comparison for a different selected pair and retries through the index", async () => {
    const baseline = summary("a"); const candidate = summary("b");
    const list = vi.spyOn(api, "listRuns").mockResolvedValue(index([baseline, candidate]));
    const compare = vi.spyOn(api, "compareRuns")
      .mockResolvedValueOnce(comparison(candidate.run_id, baseline.run_id, "Wrong pair result"))
      .mockResolvedValueOnce(comparison(baseline.run_id, candidate.run_id, "Selected pair result"));
    const user = userEvent.setup();
    render(<RunsProvider><CompareView /></RunsProvider>);
    expect(await screen.findByText("The comparison could not be calculated")).toBeInTheDocument();
    expect(screen.queryByText("Wrong pair result")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Baseline")).toHaveValue(baseline.run_id);
    expect(screen.getByLabelText("Candidate")).toHaveValue(candidate.run_id);
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Selected pair result")).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(2);
    expect(compare).toHaveBeenCalledTimes(2);
  });
});
