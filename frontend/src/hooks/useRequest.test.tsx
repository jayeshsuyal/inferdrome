import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useRequest } from "./useRequest";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}

describe("request state identity", () => {
  it("never renders an old success under changed dependencies or while disabled", async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const loader = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const renders: Array<{ selected: string; enabled: boolean; data: string | null; status: string }> = [];
    const { rerender } = renderHook(({ selected, enabled }) => {
      const request = useRequest<string>(loader, [selected], { enabled });
      renders.push({ selected, enabled, data: request.data, status: request.status });
      return request;
    }, { initialProps: { selected: "first", enabled: true } });

    await act(async () => first.resolve("first evidence"));
    renders.length = 0;
    rerender({ selected: "second", enabled: true });
    expect(renders.every((render) => render.data === null && render.status === "loading")).toBe(true);
    await act(async () => second.resolve("second evidence"));

    renders.length = 0;
    rerender({ selected: "second", enabled: false });
    expect(renders.every((render) => render.data === null && render.status === "idle")).toBe(true);
  });

  it("clears success before retry effects and ignores late responses that disregard abort", async () => {
    const first = deferred<string>();
    const obsolete = deferred<string>();
    const current = deferred<string>();
    const loader = vi.fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(obsolete.promise)
      .mockReturnValueOnce(current.promise);
    const renders: Array<string | null> = [];
    const { result } = renderHook(() => {
      const request = useRequest<string>(loader, []);
      renders.push(request.data);
      return request;
    });
    await act(async () => first.resolve("old evidence"));

    renders.length = 0;
    act(() => result.current.retry());
    expect(renders.every((value) => value === null)).toBe(true);
    act(() => result.current.retry());
    await act(async () => current.resolve("current evidence"));
    await act(async () => obsolete.resolve("obsolete evidence"));
    expect(result.current.data).toBe("current evidence");
    expect(renders).not.toContain("obsolete evidence");
  });
});
