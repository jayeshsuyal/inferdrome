import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { MemoryRouter } from "./lib/router";

afterEach(() => vi.unstubAllGlobals());

describe("Runs view", () => {
  it("shows rejected bundles without exposing measurements", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify({
      projection_version: "inferdrome.dashboard.v1",
      generated_at: "2026-08-07T12:00:00Z",
      runs: [],
      rejected: [{
        entry: "tampered-evidence",
        status: "REJECTED",
        code: "VERIFICATION_FAILED",
        message: "Bundle could not be verified.",
      }],
      page: {
        limit: 200,
        returned: 1,
        total: 1,
        has_more: false,
        next_cursor: null,
      },
    }), { status: 200, headers: { "content-type": "application/json" } }))));

    render(<MemoryRouter initialEntries={["/runs"]}><App /></MemoryRouter>);

    expect(await screen.findByText("No verified runs yet")).toBeInTheDocument();
    expect(screen.getByText("tampered-evidence")).toBeInTheDocument();
    expect(screen.getByText("Measurements withheld")).toBeInTheDocument();
  });
});
