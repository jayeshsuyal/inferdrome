import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  Link,
  MemoryRouter,
  Navigate,
  NavLink,
  useLocation,
  useParams,
} from "./router";

function RouterProbe() {
  const location = useLocation();
  const params = useParams();
  return (
    <>
      <output aria-label="Current path">{location.pathname}</output>
      <output aria-label="Current run">{params.runId ?? "none"}</output>
      <NavLink to="/runs" end>Runs</NavLink>
      <Link to="/runs/run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">Open run</Link>
    </>
  );
}

describe("local dashboard router", () => {
  it("navigates same-origin links and updates route params", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/runs"]}><RouterProbe /></MemoryRouter>);

    expect(screen.getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
    await user.click(screen.getByRole("link", { name: "Open run" }));

    expect(screen.getByLabelText("Current path")).toHaveTextContent(
      "/runs/run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    );
    expect(screen.getByLabelText("Current run")).toHaveTextContent(
      "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    );
    expect(screen.getByRole("link", { name: "Runs" })).not.toHaveAttribute("aria-current");
  });

  it("supports the root redirect used by the application", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Navigate to="/runs" replace />
        <RouterProbe />
      </MemoryRouter>,
    );

    expect(await screen.findByText("/runs", { selector: "output" })).toHaveAttribute(
      "aria-label",
      "Current path",
    );
  });
});
