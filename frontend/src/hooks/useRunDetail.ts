import { useRuns } from "../context/RunsContext";
import { useRequest, type RequestState } from "./useRequest";
import { api } from "../lib/api";
import type { RunDetail } from "../lib/types";

export function useRunDetail(runId: string | null | undefined): RequestState<RunDetail> {
  const runIndex = useRuns();
  const selected = runIndex.runs.find((run) => run.run_id === runId);
  const request = useRequest<RunDetail>(
    async (signal) => {
      if (!runId || !selected) throw new Error("No verified run was selected.");
      const detail = await api.getRun(runId, signal);
      if (
        detail.summary.run_id !== runId
        || detail.summary.bundle_digest !== selected.bundle_digest
        || detail.verification.bundle_digest !== selected.bundle_digest
      ) {
        throw new Error("Returned evidence does not match the latest verified run snapshot. Refresh runs and try again.");
      }
      return detail;
    },
    [runId, selected?.bundle_digest, runIndex.refreshRevision],
    { enabled: runIndex.status === "success" && Boolean(selected) },
  );

  const retry = runIndex.refresh;
  if (!runId) return { data: null, error: null, status: "idle", retry };
  if (runIndex.status !== "success") {
    return { data: null, error: runIndex.error, status: runIndex.status === "error" ? "error" : "loading", retry };
  }
  if (!selected) {
    return { data: null, error: new Error("This run is not in the latest verified run snapshot. Refresh runs after restoring or adding its evidence."), status: "error", retry };
  }
  return { ...request, retry };
}
