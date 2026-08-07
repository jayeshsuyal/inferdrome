import type {
  ComparisonDelta,
  JsonPrimitive,
  Measurement,
  MeasurementValue,
  RunSummary,
} from "./types";

const NUMBER = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const COMPACT_NUMBER = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 2,
});
const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

const METRIC_LABELS: Readonly<Record<string, string>> = {
  measured_request_count: "Measured requests",
  successful_request_count: "Successful requests",
  failed_request_count: "Failed requests",
  error_rate: "Error rate",
  ttft_ns: "Time to first token",
  last_choices_event_span_ns: "Last-choice event span",
  attempted_request_throughput_per_s: "Attempted request throughput",
  successful_request_throughput_per_s: "Request throughput",
  output_token_throughput_per_s: "Output throughput",
  first_nonempty_content_ttft_ns: "First non-empty content TTFT",
  terminal_e2e_latency_ns: "Terminal end-to-end latency",
  upstream_tpot_ns: "Upstream time per output token",
  exact_achieved_concurrency: "Exact achieved concurrency",
  scheduled_offset_ns: "Scheduled offset",
  http_status: "HTTP status",
  finish_reason: "Finish reason",
};

export interface FormattedMeasurement {
  readonly value: string;
  readonly unit: string;
}

export function asFiniteNumber(value: MeasurementValue): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function humanize(value: string): string {
  return value
    .replace(/[._-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

export function metricLabel(metric: string, explicit?: string | null): string {
  return explicit || METRIC_LABELS[metric] || humanize(metric);
}

export function formatMeasurement(measurement: Measurement): FormattedMeasurement {
  const display = measurement.display_value.trim();
  if (display.endsWith("%")) return { value: display.slice(0, -1), unit: "%" };
  const unitMatch = display.match(/^(.+?)\s+(ms|μs|ns|req\/s|tok\/s|requests\/s|tokens\/s)$/);
  if (unitMatch) return { value: unitMatch[1], unit: unitMatch[2] };
  return { value: display, unit: "" };
}

export function formatRawMetricValue(value: MeasurementValue, unit: string): FormattedMeasurement {
  const numeric = asFiniteNumber(value);
  if (numeric === null) return { value: String(value), unit };

  if (unit === "ns") {
    const milliseconds = numeric / 1_000_000;
    return {
      value: NUMBER.format(milliseconds),
      unit: "ms",
    };
  }
  if (unit === "ratio") {
    return { value: NUMBER.format(numeric * 100), unit: "%" };
  }
  if (unit === "tokens/s") {
    return { value: COMPACT_NUMBER.format(numeric), unit: "tok/s" };
  }
  if (unit === "requests/s") {
    return { value: NUMBER.format(numeric), unit: "req/s" };
  }
  if (unit === "count") {
    return { value: NUMBER.format(numeric), unit: "" };
  }
  return { value: NUMBER.format(numeric), unit };
}

export function findMeasurement(
  measurements: readonly Measurement[] | undefined,
  metric: string,
  aggregation: string,
): Measurement | undefined {
  return measurements?.find(
    (measurement) => measurement.metric === metric && measurement.aggregation === aggregation,
  );
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "Not reported";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : DATE_TIME.format(date);
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "Not reported";
  const units = ["B", "KB", "MB", "GB"];
  let size = Math.max(0, value);
  let unit = 0;
  while (size >= 1000 && unit < units.length - 1) {
    size /= 1000;
    unit += 1;
  }
  return `${NUMBER.format(size)} ${units[unit]}`;
}

export function formatDurationNs(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "Not reported";
  if (value < 1_000_000) return `${NUMBER.format(value / 1_000)} μs`;
  if (value < 1_000_000_000) return `${NUMBER.format(value / 1_000_000)} ms`;
  return `${NUMBER.format(value / 1_000_000_000)} s`;
}

export function shortDigest(value: string | null | undefined): string {
  if (!value) return "Not reported";
  const body = value.startsWith("sha256:") ? value.slice(7) : value;
  return body.length > 13 ? `${body.slice(0, 8)}…${body.slice(-5)}` : body;
}

export function formatPrimitive(value: JsonPrimitive | undefined): string {
  if (value === null || value === undefined) return "Not reported";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

export function runModelLabel(run: RunSummary): string {
  return run.model || run.title || "Model not reported";
}

export function runTargetLabel(run: RunSummary): string {
  const producer = [run.producer_name, run.producer_version].filter(Boolean).join(" ");
  const adapter = [run.adapter_name, run.adapter_version].filter(Boolean).join(" ");
  return [producer, adapter].filter(Boolean).join(" · ") || "Target not reported";
}

export function eligibilityLabel(value: string): string {
  if (value === "CUSTOMER_ELIGIBLE") return "Customer eligible";
  if (value === "SYNTHETIC_ONLY") return "Synthetic only";
  if (value === "INELIGIBLE") return "Ineligible";
  return humanize(value);
}

export function comparisonReason(reason: string): string {
  return reason;
}

export function formatDelta(delta: ComparisonDelta): string {
  return delta.delta_display_value;
}
