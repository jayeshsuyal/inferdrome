import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { DashboardAuthProvider } from "./DashboardAuthContext";
import { MemoryRouter } from "../lib/router";

const token = "inferdrome-dashboard-v1.dk-0123456789abcdef." + "A".repeat(43);
const emptyIndex = {
  projection_version: "inferdrome.dashboard.v1",
  generated_at: "2026-08-25T00:00:00Z",
  runs: [],
  rejected: [],
  page: {
    limit: 200,
    returned: 0,
    total: 0,
    has_more: false,
    next_cursor: null,
  },
};

function response(value: unknown, status: number): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("dashboard authentication UX", () => {
  it("unlocks after a protected 401 and sends the token only as a header", async () => {
    const requests: Array<{ input: RequestInfo | URL; init: RequestInit | undefined }> = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      const headers = new Headers(init?.headers);
      if (headers.get("Authorization") === `Bearer ${token}`) {
        return Promise.resolve(response(emptyIndex, 200));
      }
      return Promise.resolve(response({ detail: "dashboard authentication failed" }, 401));
    });
    vi.stubGlobal("fetch", fetchMock);

    const user = userEvent.setup();
    render(
      <DashboardAuthProvider>
        <MemoryRouter initialEntries={["/runs"]}>
          <App />
        </MemoryRouter>
      </DashboardAuthProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Unlock evidence views" })).toBeInTheDocument();
    const input = screen.getByLabelText("Bearer token");
    await user.type(input, token);
    await user.click(screen.getByRole("button", { name: "Unlock dashboard" }));

    expect(await screen.findByText("No verified runs yet")).toBeInTheDocument();
    expect(requests.some(({ init }) => new Headers(init?.headers).get("Authorization") === `Bearer ${token}`)).toBe(true);
    expect(window.localStorage.getItem("inferdrome-dashboard-token")).toBeNull();
    expect(window.sessionStorage.length).toBe(0);
    expect(document.cookie).not.toContain(token);
    expect(document.body.textContent).not.toContain(token);
    expect(requests.every(({ input }) => !String(input).includes(token))).toBe(true);
  });

  it("can clear the in-memory token and returns to the unlock screen", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      return Promise.resolve(
        headers.get("Authorization") === `Bearer ${token}`
          ? response(emptyIndex, 200)
          : response({ detail: "dashboard authentication failed" }, 401),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(
      <DashboardAuthProvider>
        <MemoryRouter initialEntries={["/runs"]}>
          <App />
        </MemoryRouter>
      </DashboardAuthProvider>,
    );
    await user.type(await screen.findByLabelText("Bearer token"), token);
    await user.click(screen.getByRole("button", { name: "Unlock dashboard" }));
    expect(await screen.findByText("No verified runs yet")).toBeInTheDocument();

    const requestsBeforeLock = fetchMock.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Lock dashboard" }));
    expect(await screen.findByRole("heading", { name: "Unlock evidence views" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.length).toBe(requestsBeforeLock);
  });
});
