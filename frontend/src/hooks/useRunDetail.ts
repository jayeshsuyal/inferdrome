import { useRequest } from "./useRequest";
import { api } from "../lib/api";
import type { RunDetail } from "../lib/types";

export function useRunDetail(runId: string | null | undefined) {
  return useRequest<RunDetail>(
    (signal) => {
      if (!runId) return Promise.reject(new Error("No run was selected."));
      return api.getRun(runId, signal);
    },
    [runId],
    { enabled: Boolean(runId) },
  );
}
