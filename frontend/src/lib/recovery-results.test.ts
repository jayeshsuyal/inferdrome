import { describe, expect, it } from "vitest";

import { recoveryMetrics, recoveryReport } from "./recovery-results";

describe("published recovery aggregate", () => {
  it("retains the complete 12-pair primary population, including misses", () => {
    const { pairs, primary, design } = recoveryReport;
    expect(pairs).toHaveLength(12);
    expect(pairs.length).toBe(design.pairs);
    expect(new Set(pairs.map((pair) => pair.pair)).size).toBe(design.pairs);
    expect(pairs.every((pair) => pair.valid)).toBe(true);
    expect(pairs.every((pair) => pair.offers_per_arm === 720)).toBe(true);
    expect(pairs.reduce((sum, pair) => sum + pair.offers_per_arm, 0)).toBe(8640);
    expect(pairs.reduce((sum, pair) => sum + pair.misses_500ms, 0)).toBe(primary["500ms"].misses);
    expect(pairs.reduce((sum, pair) => sum + pair.misses_400ms, 0)).toBe(primary["400ms"].misses);
    for (const arm of [primary["500ms"], primary["400ms"]]) {
      expect(arm.slo_good + arm.misses).toBe(arm.offers);
      expect(arm.offers).toBe(design.primary_recovery_offers_per_arm);
    }
    expect(design.rejects_timeouts_missing_and_errors_count_as_misses).toBe(true);
  });

  it("keeps history and secondary windows out of the primary denominator", () => {
    const { design } = recoveryReport;
    expect(design.primary_recovery_offers_per_arm * 2).toBe(design.primary_recovery_offers);
    expect(design.history_offers + design.primary_recovery_offers).toBe(design.total_offers);
    expect(design.total_offers).toBe(25920);
    expect(design.history_offers).toBe(8640);
    expect(design.primary_recovery_offers).toBe(17280);
    expect(design.trials).toBe(design.pairs * 2);
    expect(design.trials * design.history_seconds_per_trial * design.offered_requests_per_second).toBe(design.history_offers);
    expect(design.trials * design.recovery_seconds_per_trial * design.offered_requests_per_second).toBe(design.primary_recovery_offers);
    expect(recoveryReport.secondary_first30.offers_per_arm).toBe(2160);
  });

  it("retains balanced execution orders and every pair's measured difference", () => {
    const orders = recoveryReport.pairs.map((pair) => pair.order_ms.join("_then"));
    expect(orders.filter((order) => order === "500_then400")).toHaveLength(6);
    expect(orders.filter((order) => order === "400_then500")).toHaveLength(6);
    for (const pair of recoveryReport.pairs) {
      expect(pair.fewer_misses).toBe(pair.misses_500ms - pair.misses_400ms);
    }
    expect(recoveryMetrics.pairsFavoringGuard).toBe(12);
  });

  it("computes relative miss reduction separately from attainment and latency", () => {
    expect(recoveryMetrics.baseline.misses).toBe(3420);
    expect(recoveryMetrics.guard.misses).toBe(2998);
    expect(recoveryMetrics.fewerMisses).toBe(422);
    expect(recoveryMetrics.relativeMissReductionPercent).toBeCloseTo(12.339181286549708, 10);
    expect(recoveryMetrics.relativeMissReductionPercent).toBeCloseTo(recoveryReport.primary.relative_miss_reduction_percent, 10);
    expect(recoveryMetrics.baselineAttainmentPercent).toBeCloseTo(60.416666666666664, 10);
    expect(recoveryMetrics.guardAttainmentPercent).toBeCloseTo(65.30092592592592, 10);
    expect(recoveryMetrics.attainmentGainPoints).toBeCloseTo(4.8842592592592595, 10);
    expect(recoveryReport.design.first_token_deadline_ms).toBe(500);
    expect(recoveryReport.design.completion_deadline_ms).toBe(5000);
    expect(recoveryReport.runtime.physical_hosts).toBe(1);
  });
});
