import { act, renderHook } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DashboardAuthProvider, useDashboardAuth } from "../context/DashboardAuthContext";
import { EVALUATION_VERSION, evaluationApi, type EvaluationReportIndex } from "../lib/evaluations";
import cache from "../test/evaluations/cache.json";
import study from "../test/evaluations/study.json";
import { useEvaluationDetail, useEvaluations } from "./useEvaluations";

const emptyIndex: EvaluationReportIndex = { projection_version: EVALUATION_VERSION, reports: [], rejected: [] };
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "content-type": "application/json",
      ...(status === 503 ? { "X-Inferdrome-Evaluation-Busy": "1", "Retry-After": "1" } : {}),
    },
  });
}
function AuthWrapper({ children }: PropsWithChildren) {
  return <DashboardAuthProvider>{children}</DashboardAuthProvider>;
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("evaluation refresh state", () => {
  it("suppresses initial-loading, same-batch and pending duplicate refreshes", async () => {
    const initial = deferred<EvaluationReportIndex>();
    const refreshed = deferred<EvaluationReportIndex>();
    const list = vi.spyOn(evaluationApi, "list")
      .mockResolvedValue(emptyIndex)
      .mockReturnValueOnce(initial.promise)
      .mockReturnValueOnce(refreshed.promise);
    const { result } = renderHook(() => useEvaluations());
    expect(result.current.pendingRefresh).toBe(true);
    act(() => { result.current.retry(); result.current.retry(); });
    expect(list).toHaveBeenCalledTimes(1);
    await act(async () => initial.resolve(emptyIndex));
    expect(result.current.pendingRefresh).toBe(false);
    act(() => { result.current.retry(); result.current.retry(); });
    expect(result.current.data).toBeNull();
    expect(result.current.pendingRefresh).toBe(true);
    expect(list).toHaveBeenCalledTimes(2);
    act(() => result.current.retry());
    expect(list).toHaveBeenCalledTimes(2);
    await act(async () => refreshed.resolve(emptyIndex));
    expect(result.current.pendingRefresh).toBe(false);
    await act(async () => result.current.retry());
    expect(list).toHaveBeenCalledTimes(3);
    expect(result.current.data).toEqual(emptyIndex);
  });

  it("keeps busy retries loading and ends visibly in error when their budget expires", async () => {
    const fetch = vi.fn(() => Promise.resolve(response({}, 503)));
    vi.stubGlobal("fetch", fetch);
    const { result } = renderHook(() => useEvaluations());
    await act(async () => vi.advanceTimersByTimeAsync(3999));
    expect(result.current.status).toBe("loading");
    expect(result.current.pendingRefresh).toBe(true);
    expect(result.current.data).toBeNull();
    act(() => result.current.retry());
    expect(fetch).toHaveBeenCalledTimes(4);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(result.current.status).toBe("error");
    expect(result.current.pendingRefresh).toBe(false);
    expect(result.current.error?.message).toBe("Evaluation reports are busy. Try again.");
    expect(fetch).toHaveBeenCalledTimes(5);
  });

  it("aborts a waiting old route and never displays its data under the new identity", async () => {
    const fetch = vi.fn((path: string) => Promise.resolve(
      path.endsWith(study.summary.report_id) ? response({}, 503) : response(cache),
    ));
    vi.stubGlobal("fetch", fetch);
    const { result, rerender } = renderHook(({ id }) => useEvaluationDetail(id), {
      initialProps: { id: study.summary.report_id },
    });
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(result.current.pendingRefresh).toBe(true);
    expect(vi.getTimerCount()).toBe(1);
    rerender({ id: cache.summary.report_id });
    expect(result.current.data).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(result.current.data?.summary.report_id).toBe(cache.summary.report_id);
    expect(result.current.pendingRefresh).toBe(false);
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("relocks immediately when the next busy retry receives 401", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({}, 503))
      .mockResolvedValueOnce(response({}, 401));
    vi.stubGlobal("fetch", fetch);
    const { result } = renderHook(() => ({ request: useEvaluations(), auth: useDashboardAuth() }), { wrapper: AuthWrapper });
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(result.current.request.pendingRefresh).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(result.current.auth.authRequired).toBe(true);
    expect(result.current.auth.token).toBeNull();
    expect(result.current.auth.authError).toContain("token was not accepted");
    expect(result.current.request.data).toBeNull();
    expect(result.current.request.status).toBe("idle");
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("aborts the old authentication wait and reloads only under the replacement session", async () => {
    const fetch = vi.fn((_path: string, options: RequestInit) => Promise.resolve(
      (options.headers as Record<string, string>).Authorization === "Bearer replacement"
        ? response(emptyIndex) : response({}, 503),
    ));
    vi.stubGlobal("fetch", fetch);
    const { result } = renderHook(() => ({ request: useEvaluations(), auth: useDashboardAuth() }), { wrapper: AuthWrapper });
    await act(async () => vi.advanceTimersByTimeAsync(0));
    act(() => result.current.auth.unlock("old"));
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(result.current.request.pendingRefresh).toBe(true);
    expect(vi.getTimerCount()).toBe(1);
    act(() => result.current.auth.unlock("replacement"));
    expect(result.current.request.data).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(result.current.request.data).toEqual(emptyIndex);
    expect(result.current.auth.token).toBe("replacement");
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(vi.getTimerCount()).toBe(0);
  });
});
