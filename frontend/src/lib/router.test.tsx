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
      <output aria-label="Current trial set">{params.trialSetId ?? "none"}</output>
      <output aria-label="Current comparison">{params.comparisonPlanId ?? "none"}</output>
      <NavLink to="/runs" end>Runs</NavLink>
      <NavLink to="/comparisons" activeOn={["/compare"]}>Comparisons</NavLink>
      <Link to="/runs/run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">Open run</Link>
      <Link to="/trial-sets/trial-set-11111111111111111111111111111111">Open trial set</Link>
      <Link to="/comparisons/comparison-plan-11111111111111111111111111111111">Open comparison</Link>
      <Link to="/compare">Ad hoc compare</Link>
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

  it("decodes the trial-set detail route independently from run routes", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/trial-sets"]}><RouterProbe /></MemoryRouter>);

    await user.click(screen.getByRole("link", { name: "Open trial set" }));

    expect(screen.getByLabelText("Current trial set")).toHaveTextContent(
      "trial-set-11111111111111111111111111111111",
    );
    expect(screen.getByLabelText("Current run")).toHaveTextContent("none");
  });

  it("decodes controlled-comparison plans and keeps the shared nav active for ad hoc compare", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/comparisons"]}><RouterProbe /></MemoryRouter>);

    expect(screen.getByRole("link", { name: "Comparisons" })).toHaveAttribute("aria-current", "page");
    await user.click(screen.getByRole("link", { name: "Open comparison" }));
    expect(screen.getByLabelText("Current comparison")).toHaveTextContent(
      "comparison-plan-11111111111111111111111111111111",
    );

    await user.click(screen.getByRole("link", { name: "Ad hoc compare" }));
    expect(screen.getByLabelText("Current path")).toHaveTextContent("/compare");
    expect(screen.getByRole("link", { name: "Comparisons" })).toHaveAttribute("aria-current", "page");
  });
});
