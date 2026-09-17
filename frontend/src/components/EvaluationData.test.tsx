import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { EvaluationPopulation } from "../lib/evaluations";
import {
  EvaluationContrasts,
  EvaluationPopulationData,
  EvaluationRecoveryData,
} from "./EvaluationData";

const population: EvaluationPopulation = {
  metrics: [
    { key: "slo_goodput_rps", label: "SLO goodput", value: "0.000000", unit: "requests/s" },
    { key: "elapsed_ns", label: "Population elapsed", value: "1234567", unit: "ns" },
  ],
  outcomes: [{ outcome: "HTTP_ERROR", count: 1, offered_fraction: "1.000000" }],
  latency: [{
    label: "Partial first content: HTTP_ERROR",
    population: "HTTP_ERROR",
    count: 1,
    p50_ns: "1250000",
    p90_ns: "1234567",
    p95_ns: "0",
    p99_ns: null,
    p99_status: "NOT_A_SUCCESS_LATENCY_POPULATION",
  }],
  usage: [{ key: "usage_missing", label: "Usage missing", value: null, unit: "tokens" }],
  cancelled: false,
};

async function expand(user: ReturnType<typeof userEvent.setup>, title: string) {
  const control = screen.getByText(title, { selector: "summary .evaluation-disclosure-title" });
  await user.click(control);
  const disclosure = control.closest("details")!;
  expect(disclosure).toHaveAttribute("open");
  return disclosure;
}

describe("Evaluation data disclosures", () => {
  it("keeps readable timings, exact source precision, measured zero and unavailable values distinct", async () => {
    const user = userEvent.setup();
    render(<EvaluationPopulationData population={population} />);
    expect(screen.getAllByText("0 requests/s")[0]).toBeVisible();
    expect(screen.getByText("0.000000 requests/s")).not.toBeVisible();
    await expand(user, "Latency and dispatch timing");
    const displayed = screen.getByRole("table", { name: "Authoritative latency quantiles" });
    expect(within(displayed).getByText("1.25 ms")).toBeVisible();
    expect(within(displayed).getByText("≈ 1.235 ms")).toBeVisible();
    expect(within(displayed).getByText("0 ns")).toBeVisible();
    expect(within(displayed).getByText("Unavailable")).toBeVisible();
    expect(within(displayed).getByText("Partial first content: HTTP error")).toBeVisible();
    const exact = await expand(user, "Exact population values");
    expect(within(exact).getByText("0.000000 requests/s")).toBeVisible();
    expect(within(exact).getByText("1250000 ns")).toBeVisible();
    expect(within(exact).getAllByText("1234567 ns")).toHaveLength(2);
    expect(within(exact).getByText("Partial first content: HTTP_ERROR")).toBeVisible();
    expect(within(exact).getByText("NOT_A_SUCCESS_LATENCY_POPULATION")).toBeVisible();
    expect(within(exact).getByText("Usage missing").parentElement).toHaveTextContent("Unavailable");
  });

  it("retains cancelled and absent latency cues before any data disclosure opens", () => {
    render(<EvaluationPopulationData population={{ ...population, cancelled: true, latency: [] }} />);
    expect(screen.getByText("Population cancelled", { selector: ".status-badge" })).toBeVisible();
    const title = screen.getByText("Latency and dispatch timing", { selector: "summary .evaluation-disclosure-title" });
    const summary = title.closest("summary")!;
    expect(summary).toHaveTextContent("Population cancelled · Latency unavailable");
    expect(summary.closest("details")).not.toHaveAttribute("open");
    expect(summary).toBeVisible();
  });

  it("discloses exact signed contrast values and source interval status", async () => {
    const user = userEvent.setup();
    render(<EvaluationContrasts contrasts={[{
      label: "Shared contrast minus unique contrast",
      complete_blocks: 8,
      mean_rps: "-0.000001",
      lower_rps: "-1.230000",
      upper_rps: "0.000000",
      interval_status: "DESCRIPTIVE_ONLY",
    }]} />);
    const normal = screen.getByRole("table", { name: "Authoritative descriptive contrasts in requests/s" });
    expect(within(normal).getByText("-0.000001 requests/s")).toBeVisible();
    const exact = await expand(user, "Exact contrast values");
    expect(within(exact).getByText("-0.000001 requests/s")).toBeVisible();
    expect(within(exact).getByText("[-1.230000, 0.000000] requests/s")).toBeVisible();
    expect(within(exact).getByText("DESCRIPTIVE_ONLY")).toBeVisible();
  });

  it("makes exact recovery origins, observed zero and censored horizons reachable", async () => {
    const user = userEvent.setup();
    render(<EvaluationRecoveryData recovery={{
      applicability: "APPLICABLE",
      origin: "ACTUAL_TELEMETRY_RESTORED_EVENT",
      planned_restore_ns: "1000000000",
      actual_restore_ns: "1000000001",
      background_active_at_restore: false,
      intervals: [
        { metric: "publication", status: "OBSERVED", duration_ns: "0", observation_horizon_ns: "1250000" },
        { metric: "decision", status: "UNOBSERVED_OR_CENSORED", duration_ns: null, observation_horizon_ns: "1234567" },
      ],
    }} />);
    const displayed = screen.getByRole("table", { name: "Publication, decision and dispatch recovery from actual restoration" });
    expect(within(displayed).getByText("0 ns")).toBeVisible();
    expect(within(displayed).getByText("Unavailable")).toBeVisible();
    const exact = await expand(user, "Exact recovery values");
    expect(within(exact).getByText("ACTUAL_TELEMETRY_RESTORED_EVENT")).toBeVisible();
    expect(within(exact).getByText("1000000001 ns")).toBeVisible();
    expect(within(exact).getByText("UNOBSERVED_OR_CENSORED")).toBeVisible();
    expect(within(exact).getByText("1234567 ns")).toBeVisible();
  });
});
