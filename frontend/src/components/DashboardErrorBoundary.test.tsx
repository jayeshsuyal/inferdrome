import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DashboardAuthProvider, useDashboardAuth } from "../context/DashboardAuthContext";
import { MemoryRouter, useLocation } from "../lib/router";
import { DashboardErrorBoundary } from "./DashboardErrorBoundary";

afterEach(() => vi.restoreAllMocks());

describe("dashboard render recovery", () => {
  it("contains render errors without exposing details and retries with the same route and auth", async () => {
    // React reports caught render errors to the development console.
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    function EvidenceView() {
      const [failed, setFailed] = useState(false);
      const { token, unlock } = useDashboardAuth();
      const { pathname } = useLocation();
      if (failed) throw new Error("private exception details must not be rendered");
      return (
        <>
          <p>{pathname}</p>
          <p>{token ? "Session retained" : "Session not set"}</p>
          <button type="button" onClick={() => unlock("local-unit-test-token")}>Set test session</button>
          <button type="button" onClick={() => setFailed(true)}>Break view</button>
        </>
      );
    }

    const user = userEvent.setup();
    render(
      <DashboardAuthProvider>
        <MemoryRouter initialEntries={["/evidence/test-run"]}>
          <DashboardErrorBoundary><EvidenceView /></DashboardErrorBoundary>
        </MemoryRouter>
      </DashboardAuthProvider>,
    );
    await user.click(screen.getByRole("button", { name: "Set test session" }));
    await user.click(screen.getByRole("button", { name: "Break view" }));

    expect(screen.getByRole("alert")).toHaveTextContent("This view could not be displayed");
    expect(screen.queryByText(/private exception details/)).not.toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Try again" });
    expect(retry).toHaveFocus();
    await user.click(retry);

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("/evidence/test-run")).toBeInTheDocument();
    expect(screen.getByText("Session retained")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Break view" })).toBeInTheDocument();
  });
});
