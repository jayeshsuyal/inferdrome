import { api } from "../lib/api";
import type { RoutingQualificationDetail } from "../lib/types";
import { useRequest } from "./useRequest";

/** Abort stale causal-qualification reads as the selected route changes. */
export function useRoutingQualificationDetail(
  qualificationId: string | null | undefined,
) {
  return useRequest<RoutingQualificationDetail>(
    (signal) => {
      if (!qualificationId) {
        return Promise.reject(new Error("No routing qualification was selected."));
      }
      return api.getRoutingQualification(qualificationId, signal);
    },
    [qualificationId],
    { enabled: Boolean(qualificationId) },
  );
}
