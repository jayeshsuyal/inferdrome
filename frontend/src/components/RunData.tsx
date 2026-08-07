import { CircleOff } from "lucide-react";

import {
  findMeasurement,
  formatMeasurement,
  formatRawMetricValue,
  humanize,
  metricLabel,
} from "../lib/format";
import type { DistributionView, Measurement } from "../lib/types";

interface MetricSpec {
  readonly metric: string;
  readonly aggregation: string;
  readonly label: string;
}

export const HEADLINE_METRICS: readonly MetricSpec[] = [
  { metric: "ttft_ns", aggregation: "p50", label: "TTFT p50" },
  {
    metric: "successful_request_throughput_per_s",
    aggregation: "rate",
    label: "Request throughput",
  },
  {
    metric: "output_token_throughput_per_s",
    aggregation: "rate",
    label: "Output throughput",
  },
  { metric: "error_rate", aggregation: "ratio", label: "Error rate" },
] as const;

export const SUMMARY_HEADLINE_METRICS: readonly MetricSpec[] = [
  {
    metric: "output_token_throughput_per_s",
    aggregation: "rate",
    label: "Output throughput",
  },
  { metric: "ttft_ns", aggregation: "p50", label: "TTFT p50" },
  { metric: "ttft_ns", aggregation: "p95", label: "TTFT p95" },
  { metric: "error_rate", aggregation: "ratio", label: "Error rate" },
] as const;

export function MetricStrip({
  measurements,
  specs = HEADLINE_METRICS,
}: {
  readonly measurements: readonly Measurement[];
  readonly specs?: readonly MetricSpec[];
}) {
  return (
    <div className="metric-grid">
      {specs.map((spec) => {
        const measurement = findMeasurement(measurements, spec.metric, spec.aggregation);
        const formatted = measurement ? formatMeasurement(measurement) : null;
        return (
          <div className="metric-cell" key={`${spec.metric}-${spec.aggregation}`}>
            <span className="metric-label">{spec.label}</span>
            <span className="metric-value">
              {formatted?.value ?? "—"}
              {formatted?.unit ? <small>{formatted.unit}</small> : null}
            </span>
            <span className="metric-meta">
              {measurement?.sample_count !== null && measurement?.sample_count !== undefined
                ? `${measurement.sample_count.toLocaleString()} samples`
                : "Not reported"}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function MeasurementTable({ measurements }: { readonly measurements: readonly Measurement[] }) {
  if (measurements.length === 0) {
    return (
      <div className="inline-empty">
        <CircleOff aria-hidden="true" /> No recalculated measurements were returned.
      </div>
    );
  }
  return (
    <div className="table-scroll">
      <table>
        <caption>Recalculated measurements for this run</caption>
        <thead>
          <tr>
            <th scope="col">Metric</th>
            <th scope="col">Aggregation</th>
            <th scope="col" className="number-cell">Value</th>
            <th scope="col" className="number-cell">Samples</th>
          </tr>
        </thead>
        <tbody>
          {measurements.map((measurement) => {
            const formatted = formatMeasurement(measurement);
            return (
              <tr key={`${measurement.metric}-${measurement.aggregation}`}>
                <td>
                  <strong>{metricLabel(measurement.metric, measurement.label)}</strong>
                  {measurement.definition_id ? (
                    <span className="table-subtext mono">{measurement.definition_id}</span>
                  ) : null}
                </td>
                <td>{humanize(measurement.aggregation)}</td>
                <td className="number-cell mono">
                  {formatted.value} {formatted.unit}
                </td>
                <td className="number-cell mono">
                  {measurement.sample_count?.toLocaleString() ?? "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function DistributionBars({ distribution }: { readonly distribution: DistributionView }) {
  const maxCount = Math.max(...distribution.bins.map((bin) => bin.count), 1);

  const rangeLabel = (lower: number, upper: number): string => {
    const formattedLower = formatRawMetricValue(String(lower), distribution.unit);
    const formattedUpper = formatRawMetricValue(String(upper), distribution.unit);
    const left = `${formattedLower.value}${formattedLower.unit ? ` ${formattedLower.unit}` : ""}`;
    const right = `${formattedUpper.value}${formattedUpper.unit ? ` ${formattedUpper.unit}` : ""}`;
    return lower === upper ? left : `${left}–${right}`;
  };

  return (
    <div className="distribution-chart">
      <div className="chart-heading">
        <div>
          <h3>{distribution.label || metricLabel(distribution.metric)}</h3>
          <span>{distribution.sample_count.toLocaleString()} successful measured requests</span>
        </div>
        <span className="chart-unit">{distribution.unit === "ns" ? "milliseconds" : distribution.unit}</span>
      </div>
      <ul className="bar-list" aria-label={`${distribution.label} histogram`}>
        {distribution.bins.map((bin) => {
          const label = rangeLabel(bin.lower_bound, bin.upper_bound);
          const width = bin.count <= 0 ? 0 : Math.max(3, (bin.count / maxCount) * 100);
          return (
            <li className="bar-row" key={`${bin.lower_bound}-${bin.upper_bound}`}>
              <span className="bar-label" title={label}>{label}</span>
              <span className="bar-track" aria-hidden="true">
                <span className="bar-fill" style={{ width: `${width}%` }} />
              </span>
              <span className="bar-value">{bin.count.toLocaleString()}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
