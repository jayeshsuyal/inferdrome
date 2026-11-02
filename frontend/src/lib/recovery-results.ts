import report from "../../../docs/results/recovery-headroom-2026-10-19.json";

/** Published aggregate only; this is not a live run or a raw-capture replay. */
export const recoveryReport = report;

const baseline = report.primary["500ms"];
const guard = report.primary["400ms"];
const fewerMisses = baseline.misses - guard.misses;
const baselineAttainmentPercent = baseline.slo_good / baseline.offers * 100;
const guardAttainmentPercent = guard.slo_good / guard.offers * 100;

/** Derive display metrics from their primary populations, without rounding. */
export const recoveryMetrics = {
  baseline,
  guard,
  fewerMisses,
  relativeMissReductionPercent: fewerMisses / baseline.misses * 100,
  baselineAttainmentPercent,
  guardAttainmentPercent,
  attainmentGainPoints: guardAttainmentPercent - baselineAttainmentPercent,
  pairsFavoringGuard: report.pairs.filter((pair) => pair.misses_400ms < pair.misses_500ms).length,
};
