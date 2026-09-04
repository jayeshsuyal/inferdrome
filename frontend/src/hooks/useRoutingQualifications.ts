import { api } from "../lib/api";
import type { RoutingQualificationIndex } from "../lib/types";
import { useRequest } from "./useRequest";

/** Read the one bounded causal qualification index after backend verification. */
export function useRoutingQualifications() {
  return useRequest<RoutingQualificationIndex>(
    (signal) => api.listRoutingQualifications(signal),
    [],
  );
}
