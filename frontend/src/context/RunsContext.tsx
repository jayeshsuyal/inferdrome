import { createContext, useContext, useMemo, type ReactNode } from "react";

import { useRequest, type RequestStatus } from "../hooks/useRequest";
import { api } from "../lib/api";
import type { RejectedRun, RunIndex, RunSummary } from "../lib/types";

interface RunsContextValue {
  readonly runs: readonly RunSummary[];
  readonly rejected: readonly RejectedRun[];
  readonly generatedAt: string | null;
  readonly status: RequestStatus;
  readonly error: Error | null;
  readonly retry: () => void;
}

const RunsContext = createContext<RunsContextValue | null>(null);

function runTime(run: RunSummary): number {
  const parsed = Date.parse(run.started_at);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function RunsProvider({ children }: { readonly children: ReactNode }) {
  const request = useRequest<RunIndex>((signal) => api.listRuns(signal), []);

  const value = useMemo<RunsContextValue>(() => {
    const runs = request.data ? [...request.data.runs].sort((a, b) => runTime(b) - runTime(a)) : [];
    return {
      runs,
      rejected: request.data?.rejected ?? [],
      generatedAt: request.data?.generated_at ?? null,
      status: request.status,
      error: request.error,
      retry: request.retry,
    };
  }, [request.data, request.error, request.retry, request.status]);

  return <RunsContext.Provider value={value}>{children}</RunsContext.Provider>;
}

export function useRuns(): RunsContextValue {
  const context = useContext(RunsContext);
  if (!context) throw new Error("useRuns must be used inside RunsProvider");
  return context;
}
