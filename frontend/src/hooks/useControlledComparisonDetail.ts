import { api } from "../lib/api";
import type { ControlledComparisonDetail } from "../lib/types";
import { useRequest } from "./useRequest";

export function useControlledComparisonDetail(
  comparisonPlanId: string | null | undefined,
) {
  return useRequest<ControlledComparisonDetail>(
    (signal) => {
      if (!comparisonPlanId) {
        return Promise.reject(new Error("No controlled comparison was selected."));
      }
      return api.getControlledComparison(comparisonPlanId, signal);
    },
    [comparisonPlanId],
    { enabled: Boolean(comparisonPlanId) },
  );
}
