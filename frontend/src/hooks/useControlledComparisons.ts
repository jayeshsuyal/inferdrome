import { api } from "../lib/api";
import type { ControlledComparisonIndex } from "../lib/types";
import { useRequest } from "./useRequest";

export function useControlledComparisons() {
  return useRequest<ControlledComparisonIndex>(
    (signal) => api.listControlledComparisons(signal),
    [],
  );
}
