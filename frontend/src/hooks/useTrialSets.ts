import { api } from "../lib/api";
import type { TrialSetIndex } from "../lib/types";
import { useRequest } from "./useRequest";

export function useTrialSets() {
  return useRequest<TrialSetIndex>((signal) => api.listTrialSets(signal), []);
}
