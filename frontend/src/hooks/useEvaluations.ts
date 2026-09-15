import { useCallback, useEffect, useRef } from "react";

import { evaluationApi } from "../lib/evaluations";
import { useRequest, type RequestState } from "./useRequest";

function useEvaluationRefresh<T>(request: RequestState<T>) {
  const refreshQueued = useRef(false);
  const pendingRefresh = request.status === "loading";

  useEffect(() => {
    if (!pendingRefresh) refreshQueued.current = false;
  }, [pendingRefresh]);

  const retry = useCallback(() => {
    // The ref also closes the gap between two clicks in the same React batch.
    if (pendingRefresh || refreshQueued.current) return;
    refreshQueued.current = true;
    request.retry();
  }, [pendingRefresh, request.retry]);

  return { ...request, retry, pendingRefresh };
}

export function useEvaluations() {
  const request = useRequest((signal) => evaluationApi.list(signal), []);
  return useEvaluationRefresh(request);
}

export function useEvaluationDetail(reportId: string | undefined) {
  const request = useRequest((signal) => evaluationApi.detail(reportId!, signal), [reportId], { enabled: reportId !== undefined });
  return useEvaluationRefresh(request);
}
