import { describe, expect, it } from "vitest";

import { formatDelta, formatMeasurement } from "./format";
import type { ComparisonDelta, Measurement } from "./types";

const measurement: Measurement = {
  key: "ttft_ns:p50",
  metric: "ttft_ns",
  aggregation: "p50",
  label: "TTFT P50",
  value: "84000000",
  display_value: "84.00 ms",
  unit: "ns",
  sample_count: 500,
  population: "successful_measured_requests_with_observed_ttft",
  definition_id: "vllm_first_choices_event_v0_26",
  quantile_method: "nearest_rank_v1",
  rounding_policy: "decimal_half_even_6_v1",
};

describe("authoritative display formatting", () => {
  it("uses the server-projected metric display value", () => {
    expect(formatMeasurement(measurement)).toEqual({ value: "84.00", unit: "ms" });
  });

  it("uses the neutral server-projected signed delta", () => {
    const delta: ComparisonDelta = {
      key: "ttft_ns:p50",
      metric: "ttft_ns",
      aggregation: "p50",
      label: "TTFT P50",
      unit: "ns",
      baseline_value: "96000000",
      candidate_value: "84000000",
      absolute_delta: "-12000000",
      percent_delta: "-12.5",
      baseline_display_value: "96.00 ms",
      candidate_display_value: "84.00 ms",
      delta_display_value: "-12.00 ms",
    };
    expect(formatDelta(delta)).toBe("-12.00 ms");
  });
});
