import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import cache from "../test/evaluations/cache.json";
import { ApiError, EvaluationScanBusyError } from "./api";
import { EVALUATION_VERSION, evaluationApi } from "./evaluations";

const emptyIndex = { projection_version: EVALUATION_VERSION, reports: [], rejected: [] };
const busyHeaders = { "X-Inferdrome-Evaluation-Busy": "1", "Retry-After": "1" };
function response(value: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json", ...headers } });
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("evaluation-only scan-contention retry", () => {
  it("waits one second before a marked retry and returns the fresh index", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({}, 503, busyHeaders))
      .mockResolvedValueOnce(response(emptyIndex));
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    const result = evaluationApi.list(controller.signal);
    await vi.advanceTimersByTimeAsync(999);
    expect(fetch).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await expect(result).resolves.toEqual(emptyIndex);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls.every(([path, options]) => path === "/api/v1/evaluation-reports"
      && options.method === "GET" && options.cache === "no-store" && options.signal === controller.signal)).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("uses the same bounded retry for detail and validates its route identity", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({}, 503, busyHeaders))
      .mockResolvedValueOnce(response(cache));
    vi.stubGlobal("fetch", fetch);
    const result = evaluationApi.detail(cache.summary.report_id);
    await vi.advanceTimersByTimeAsync(1000);
    await expect(result).resolves.toEqual(cache);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls.every(([path]) => path === `/api/v1/evaluation-reports/${cache.summary.report_id}`)).toBe(true);
  });

  it("ends with an explicit busy error after five GETs and four seconds of waits", async () => {
    const fetch = vi.fn(() => Promise.resolve(response({}, 503, busyHeaders)));
    vi.stubGlobal("fetch", fetch);
    const result = evaluationApi.list().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(3999);
    expect(fetch).toHaveBeenCalledTimes(4);
    await vi.advanceTimersByTimeAsync(1);
    expect(await result).toBeInstanceOf(EvaluationScanBusyError);
    expect((await result as Error).message).toBe("Evaluation reports are busy. Try again.");
    expect(fetch).toHaveBeenCalledTimes(5);
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(fetch).toHaveBeenCalledTimes(5);
  });

  it.each([401, 404, 503])("does not retry an unmarked %s failure", async (status) => {
    const fetch = vi.fn(() => Promise.resolve(response({ detail: "private-source" }, status)));
    vi.stubGlobal("fetch", fetch);
    const result = evaluationApi.list().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(5000);
    expect(await result).toBeInstanceOf(ApiError);
    expect((await result as ApiError).status).toBe(status);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each([401, 404, 503])("stops immediately if a busy retry becomes an unmarked %s failure", async (status) => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({}, 503, busyHeaders))
      .mockResolvedValueOnce(response({}, status));
    vi.stubGlobal("fetch", fetch);
    const result = evaluationApi.list().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(5000);
    expect((await result as ApiError).status).toBe(status);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not retry an invalid projection after a busy response", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({}, 503, busyHeaders))
      .mockResolvedValueOnce(response({ ...emptyIndex, raw_prompt: "private-source" }));
    vi.stubGlobal("fetch", fetch);
    const result = evaluationApi.list().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(5000);
    expect((await result as ApiError).status).toBe(502);
    expect((await result as Error).message).not.toContain("private-source");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("cancels the wait immediately on abort without another GET", async () => {
    const fetch = vi.fn(() => Promise.resolve(response({}, 503, busyHeaders)));
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    const result = evaluationApi.list(controller.signal).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(1);
    controller.abort();
    expect(await result).toMatchObject({ name: "AbortError" });
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("never starts a GET for an already-aborted evaluation request", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    controller.abort();
    await expect(evaluationApi.list(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    expect(fetch).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});
