import { evaluationApi } from "../lib/evaluations";
import { useRequest } from "./useRequest";

export function useEvaluations() {
  return useRequest((signal) => evaluationApi.list(signal), []);
}

export function useEvaluationDetail(reportId: string | undefined) {
  return useRequest((signal) => evaluationApi.detail(reportId!, signal), [reportId], { enabled: reportId !== undefined });
}
