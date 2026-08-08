import { useCallback, useEffect, useRef, useState, type DependencyList } from "react";

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
  const loaderRef = useRef(loader);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<Omit<RequestState<T>, "retry">>({
    data: null,
    error: null,
    status: enabled ? "loading" : "idle",
  });

  loaderRef.current = loader;

  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  useEffect(() => {
    if (!enabled) {
      setState({ data: null, error: null, status: "idle" });
      return;
    }

    const controller = new AbortController();
    setState((current) => ({ ...current, error: null, status: "loading" }));

    void loaderRef.current(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setState({ data, error: null, status: "success" });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          data: null,
          error: error instanceof Error ? error : new Error("The request failed."),
          status: "error",
        });
      },
    );

    return () => controller.abort();
    // The caller owns the stable dependency list; the latest loader is read through a ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, attempt, ...dependencies]);

  return { ...state, retry };
}
