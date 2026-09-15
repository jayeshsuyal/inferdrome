import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, EvaluationScanBusyError, fetchBoundedDashboardJson, setDashboardToken } from "./api";

afterEach(() => vi.unstubAllGlobals());

function response(body: string, status = 200, headers: Record<string, string> = {}) {
  return new Response(body, { status, headers: { "content-type": "application/json", ...headers } });
}

describe("bounded evaluation GET", () => {
  it("recognizes only the explicit one-second evaluation scan-contention response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response('{"detail":"private-source"}', 503, {
      "X-Inferdrome-Evaluation-Busy": "1", "Retry-After": "1",
    })));
    const error = await fetchBoundedDashboardJson("/evaluation-reports", 64).catch((value: unknown) => value);
    expect(error).toBeInstanceOf(EvaluationScanBusyError);
    expect((error as Error).message).toBe("Evaluation reports are busy. Try again.");
  });

  it.each([
    [503, {}],
    [503, { "Retry-After": "1" }],
    [503, { "X-Inferdrome-Evaluation-Busy": "1" }],
    [503, { "X-Inferdrome-Evaluation-Busy": "0", "Retry-After": "1" }],
    [503, { "X-Inferdrome-Evaluation-Busy": "1", "Retry-After": "2" }],
    [401, { "X-Inferdrome-Evaluation-Busy": "1", "Retry-After": "1" }],
    [404, { "X-Inferdrome-Evaluation-Busy": "1", "Retry-After": "1" }],
  ] as const)("does not classify status %s with unsupported headers %o as busy", async (status, headers) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response("{}", status, headers)));
    const error = await fetchBoundedDashboardJson("/evaluation-reports", 64).catch((value: unknown) => value);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBeInstanceOf(EvaluationScanBusyError);
    expect((error as ApiError).status).toBe(status);
  });

  it("uses the existing in-memory bearer and no-store on a read-only request", async () => {
    setDashboardToken("test-token");
    const fetch = vi.fn().mockResolvedValue(response('{"reports":[]}'));
    vi.stubGlobal("fetch", fetch);
    const signal = new AbortController().signal;
    await expect(fetchBoundedDashboardJson("/evaluation-reports", 64, signal)).resolves.toEqual({ reports: [] });
    expect(fetch).toHaveBeenCalledWith("/api/v1/evaluation-reports", {
      method: "GET",
      headers: { Accept: "application/json", Authorization: "Bearer test-token" },
      signal,
      cache: "no-store",
    });
  });

  it.each([401, 404, 503])("withholds server error text for status %s", async (status) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response('{"detail":"private-file-secret"}', status)));
    const error = await fetchBoundedDashboardJson("/evaluation-reports", 64).catch((value: unknown) => value);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(status);
    expect((error as Error).message).not.toContain("private-file-secret");
  });

  it("rejects a declared oversized body before reading it", async () => {
    const payload = response("{}", 200, { "content-length": "1000" });
    const reader = vi.spyOn(payload.body!, "getReader");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(payload));
    await expect(fetchBoundedDashboardJson("/evaluation-reports", 32)).rejects.toThrow("headers are unsupported");
    expect(reader).not.toHaveBeenCalled();
  });

  it("bounds streamed bytes even without a declared length", async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new TextEncoder().encode("x".repeat(65))); },
      cancel,
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { headers: { "content-type": "application/json" } })));
    await expect(fetchBoundedDashboardJson("/evaluation-reports", 64)).rejects.toThrow("byte limit");
    expect(cancel).toHaveBeenCalledOnce();
  });

  it.each(["NaN", "", "{", "\"secret\""])('parses only JSON, leaving contract checks separate (%s)', async (body) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(body)));
    if (body === '"secret"') await expect(fetchBoundedDashboardJson("/evaluation-reports", 64)).resolves.toBe("secret");
    else await expect(fetchBoundedDashboardJson("/evaluation-reports", 64)).rejects.toThrow("not valid JSON");
  });

  it("rejects invalid UTF-8 rather than substituting text", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(new Uint8Array([34, 255, 34]), { headers: { "content-type": "application/json" } })));
    await expect(fetchBoundedDashboardJson("/evaluation-reports", 64)).rejects.toThrow("not valid JSON");
  });

  it("does not reflect a stream reader failure into the UI", async () => {
    const body = new ReadableStream<Uint8Array>({ start(controller) { controller.error(new Error("private-source-origin")); } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { headers: { "content-type": "application/json" } })));
    await expect(fetchBoundedDashboardJson("/evaluation-reports", 64)).rejects.toThrow("response body could not be read");
  });

  it("preserves cancellation and hides other transport errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new DOMException("Aborted", "AbortError")).mockRejectedValueOnce(new Error("private origin")));
    await expect(fetchBoundedDashboardJson("/evaluation-reports", 64)).rejects.toMatchObject({ name: "AbortError" });
    await expect(fetchBoundedDashboardJson("/evaluation-reports", 64)).rejects.toThrow("local Inferdrome dashboard service is unavailable");
  });
});
