import { api } from "../lib/api";
import type { RoutingExecutionDetail } from "../lib/types";
import { useRequest } from "./useRequest";

/** Abort a stale execution-detail read when its bounded route identifier changes. */
export function useRoutingExecutionDetail(
  executionId: string | null | undefined,
) {
  return useRequest<RoutingExecutionDetail>(
    (signal) => {
      if (!executionId) {
        return Promise.reject(new Error("No routing execution was selected."));
      }
      return api.getRoutingExecution(executionId, signal);
    },
    [executionId],
    { enabled: Boolean(executionId) },
  );
}
