import { useCallback, useEffect, useMemo, useRef, useState, type DependencyList } from "react";

import { useDashboardAuth } from "../context/DashboardAuthContext";
import { ApiError } from "../lib/api";

export type RequestStatus = "idle" | "loading" | "success" | "error";

export interface RequestState<T> {
  readonly data: T | null;
  readonly error: Error | null;
  readonly status: RequestStatus;
  readonly retry: () => void;
}

interface RequestOptions {
  readonly enabled?: boolean;
}

export function useRequest<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  dependencies: DependencyList,
  options: RequestOptions = {},
): RequestState<T> {
  const enabled = options.enabled ?? true;
  const { authRequired, authVersion, noteUnauthorized, token } = useDashboardAuth();
  const authenticationBlocked = authRequired && token === null;
  const loaderRef = useRef(loader);
  const [attempt, setAttempt] = useState(0);
  // A dependency change must hide the previous response during render, before
  // the effect can abort it and start the next request.
  const identity = useMemo(() => ({}), [
    enabled, authenticationBlocked, attempt, authVersion, noteUnauthorized, ...dependencies,
  ]);
  const [state, setState] = useState<Omit<RequestState<T>, "retry"> & { readonly identity: object | null }>({
    identity: null,
    data: null,
    error: null,
    status: enabled ? "loading" : "idle",
  });

  loaderRef.current = loader;

  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  useEffect(() => {
    if (!enabled || authenticationBlocked) {
      setState({ identity, data: null, error: null, status: "idle" });
      return;
    }

    const controller = new AbortController();
    setState({ identity, data: null, error: null, status: "loading" });

    void loaderRef.current(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setState({ identity, data, error: null, status: "success" });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) noteUnauthorized();
        setState({
          identity,
          data: null,
          error: error instanceof Error ? error : new Error("The request failed."),
          status: "error",
        });
      },
    );

    return () => controller.abort();
    // The caller owns the stable dependency list; the latest loader is read through a ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity]);

  if (state.identity !== identity) {
    return { data: null, error: null, status: enabled && !authenticationBlocked ? "loading" : "idle", retry };
  }
  return { data: state.data, error: state.error, status: state.status, retry };
}
