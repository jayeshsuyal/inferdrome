import { api } from "../lib/api";
import type { RoutingExecutionIndex } from "../lib/types";
import { useRequest } from "./useRequest";

/** Read the one digest-bound routing-execution index after offline verification. */
export function useRoutingExecutions() {
  return useRequest<RoutingExecutionIndex>(
    (signal) => api.listRoutingExecutions(signal),
    [],
  );
}
