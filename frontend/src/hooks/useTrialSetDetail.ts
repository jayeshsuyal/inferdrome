import { api } from "../lib/api";
import type { TrialSetDetail } from "../lib/types";
import { useRequest } from "./useRequest";

export function useTrialSetDetail(trialSetId: string | null | undefined) {
  return useRequest<TrialSetDetail>(
    (signal) => {
      if (!trialSetId) return Promise.reject(new Error("No trial set was selected."));
      return api.getTrialSet(trialSetId, signal);
    },
    [trialSetId],
    { enabled: Boolean(trialSetId) },
  );
}
