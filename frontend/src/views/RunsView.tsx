import { AlertTriangle, ArrowUpRight, Search, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";

import { MetricStrip, SUMMARY_HEADLINE_METRICS } from "../components/RunData";
import {
  EmptyState,
  ErrorState,
  EvidenceBadge,
  LoadingState,
  PageHeader,
  Panel,
  SectionHeading,
  StatusBadge,
} from "../components/Primitives";
import { useRuns } from "../context/RunsContext";
import {
  eligibilityLabel,
  findMeasurement,
  formatDateTime,
  formatMeasurement,
  runModelLabel,
  runTargetLabel,
  shortDigest,
} from "../lib/format";
import { Link } from "../lib/router";
import type { Measurement, RunSummary } from "../lib/types";

type EvidenceFilter = "ALL" | "CUSTOMER_ELIGIBLE" | "INELIGIBLE" | "SYNTHETIC_ONLY";

function metricText(
  measurements: readonly Measurement[] | undefined,
  metric: string,
  aggregation: string,
): string {
  const measurement = findMeasurement(measurements, metric, aggregation);
  if (!measurement) return "—";
  const formatted = formatMeasurement(measurement);
  return `${formatted.value}${formatted.unit ? ` ${formatted.unit}` : ""}`;
}

function RunIdentity({ run }: { readonly run: RunSummary }) {
  return (
    <>
      <Link className="run-link" to={`/runs/${encodeURIComponent(run.run_id)}`}>
        <span className="run-link-label">{run.run_id}</span>
        <ArrowUpRight aria-hidden="true" />
      </Link>
      <span className="table-subtext">{formatDateTime(run.started_at)}</span>
    </>
  );
}

function RunMobileCard({ run }: { readonly run: RunSummary }) {
  return (
    <article className="run-mobile-card">
      <div className="run-mobile-head">
        <div><RunIdentity run={run} /></div>
        <EvidenceBadge eligibility={run.evidence_eligibility} />
      </div>
      <strong>{runModelLabel(run)}</strong>
      <span className="run-mobile-target">{runTargetLabel(run)}</span>
      <dl className="run-mobile-metrics">
        <div>
          <dt>TTFT p50</dt>
          <dd>{metricText(run.headline_metrics, "ttft_ns", "p50")}</dd>
        </div>
        <div>
          <dt>Output</dt>
          <dd>{metricText(run.headline_metrics, "output_token_throughput_per_s", "rate")}</dd>
        </div>
        <div>
          <dt>Errors</dt>
          <dd>{metricText(run.headline_metrics, "error_rate", "ratio")}</dd>
        </div>
      </dl>
    </article>
  );
}

export function RunsView() {
  const { runs, rejected, status, error, retry } = useRuns();
  const [query, setQuery] = useState("");
  const [evidenceFilter, setEvidenceFilter] = useState<EvidenceFilter>("ALL");

  const filteredRuns = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return runs.filter((run) => {
      const matchesEvidence = evidenceFilter === "ALL" || run.evidence_eligibility === evidenceFilter;
      const haystack = [run.run_id, run.title, runModelLabel(run), runTargetLabel(run), run.experiment_id]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return matchesEvidence && (!needle || haystack.includes(needle));
    });
  }, [evidenceFilter, query, runs]);

  if (status === "loading") {
    return (
      <>
        <PageHeader
          title="Runs"
          subtitle="Benchmark history with evidence quality visible before performance."
        />
        <LoadingState label="Indexing sealed run bundles…" />
      </>
    );
  }

  if (status === "error" && error) {
    return (
      <>
        <PageHeader
          title="Runs"
          subtitle="Benchmark history with evidence quality visible before performance."
        />
        <ErrorState error={error} retry={retry} title="The run index is unavailable" />
      </>
    );
  }

  const eligibleCount = runs.filter((run) => run.evidence_eligibility === "CUSTOMER_ELIGIBLE").length;
  const ineligibleCount = runs.filter((run) => run.evidence_eligibility === "INELIGIBLE").length;
  const syntheticCount = runs.filter((run) => run.evidence_eligibility === "SYNTHETIC_ONLY").length;
  const validCount = runs.filter((run) => run.integrity_status === "VALID").length;
  const indexedCount = runs.length + rejected.length;
  const latestVerified = runs.find((run) => run.integrity_status === "VALID") ?? null;
  const evidenceSegments = [
    { label: "Eligible", count: eligibleCount, className: "segment-valid" },
    { label: "Ineligible", count: ineligibleCount, className: "segment-warning" },
    { label: "Synthetic", count: syntheticCount, className: "segment-info" },
    { label: "Rejected", count: rejected.length, className: "segment-danger" },
  ] as const;

  return (
    <>
      <PageHeader
        title="Runs"
        subtitle="Benchmark history with evidence quality visible before performance."
        action={<StatusBadge status="VALID" label={`${validCount} integrity-valid`} />}
      />

      {runs.length === 0 ? (
        <EmptyState
          title="No verified runs yet"
          message={
            rejected.length
              ? "Every discovered bundle was rejected. Review the bounded rejection reasons below."
              : "Complete an Inferdrome run or point the dashboard at a populated evidence root."
          }
          kind={rejected.length ? "warning" : "empty"}
        />
      ) : (
        <div className="overview-grid">
          <Panel as="article" className="latest-run-panel">
            {latestVerified ? (
              <>
                <div className="latest-run-head">
                  <div>
                    <span className="eyebrow">Latest verified run</span>
                    <Link className="latest-run-title" to={`/runs/${encodeURIComponent(latestVerified.run_id)}`}>
                      <span className="run-link-label">{latestVerified.run_id}</span>
                      <ArrowUpRight aria-hidden="true" />
                    </Link>
                    <span className="latest-run-context">
                      {runModelLabel(latestVerified)} · {runTargetLabel(latestVerified)} · {formatDateTime(latestVerified.started_at)}
                    </span>
                  </div>
                  <EvidenceBadge eligibility={latestVerified.evidence_eligibility} />
                </div>
                <MetricStrip measurements={latestVerified.headline_metrics} specs={SUMMARY_HEADLINE_METRICS} />
              </>
            ) : (
              <EmptyState
                title="No integrity-valid bundle"
                message="Performance summaries remain hidden until a bundle verifies."
                kind="warning"
              />
            )}
          </Panel>

          <Panel as="article" className="health-panel">
            <h2>Evidence mix</h2>
            <div className="health-total">
              {indexedCount.toLocaleString()}
              <span>indexed runs</span>
            </div>
            <div className="health-bar" aria-label={evidenceSegments.map((item) => `${item.count} ${item.label.toLowerCase()}`).join(", ")}>
              {evidenceSegments.map((item) =>
                item.count ? (
                  <span
                    className={item.className}
                    key={item.label}
                    style={{ flexGrow: item.count }}
                    title={`${item.label}: ${item.count}`}
                  />
                ) : null,
              )}
            </div>
            <dl className="health-legend">
              {evidenceSegments.map((item) => (
                <div key={item.label}>
                  <dt>
                    <span className={`legend-dot ${item.className}`} aria-hidden="true" />
                    {item.label}
                  </dt>
                  <dd>{item.count}</dd>
                </div>
              ))}
            </dl>
          </Panel>
        </div>
      )}

      {runs.length ? (
        <Panel className="runs-panel">
          <SectionHeading
            title="Recent runs"
            headingId="recent-runs-heading"
            meta={`${filteredRuns.length} of ${runs.length}`}
          />
          <div className="run-controls" role="search">
            <label className="search-field">
              <span className="visually-hidden">Search runs</span>
              <Search aria-hidden="true" />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search run, model, or target"
              />
            </label>
            <label className="filter-field">
              <span>Evidence</span>
              <select
                value={evidenceFilter}
                onChange={(event) => setEvidenceFilter(event.target.value as EvidenceFilter)}
              >
                <option value="ALL">All evidence</option>
                <option value="CUSTOMER_ELIGIBLE">Customer eligible</option>
                <option value="INELIGIBLE">Ineligible</option>
                <option value="SYNTHETIC_ONLY">Synthetic only</option>
              </select>
            </label>
          </div>

          {filteredRuns.length ? (
            <>
              <div className="table-scroll run-table-wrap">
                <table aria-labelledby="recent-runs-heading" className="runs-table">
                  <caption>Verified Inferdrome run bundles</caption>
                  <thead>
                    <tr>
                      <th scope="col">Run</th>
                      <th scope="col">Model / target</th>
                      <th scope="col">Evidence</th>
                      <th scope="col" className="number-cell">TTFT p50</th>
                      <th scope="col" className="number-cell">Output</th>
                      <th scope="col" className="number-cell">Errors</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredRuns.map((run) => (
                      <tr key={run.run_id}>
                        <td><RunIdentity run={run} /></td>
                        <td>
                          <strong>{runModelLabel(run)}</strong>
                          <span className="table-subtext">{runTargetLabel(run)}</span>
                        </td>
                        <td><EvidenceBadge eligibility={run.evidence_eligibility} /></td>
                        <td className="number-cell mono">{metricText(run.headline_metrics, "ttft_ns", "p50")}</td>
                        <td className="number-cell mono">{metricText(run.headline_metrics, "output_token_throughput_per_s", "rate")}</td>
                        <td className="number-cell mono">{metricText(run.headline_metrics, "error_rate", "ratio")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="run-card-list">
                {filteredRuns.map((run) => <RunMobileCard key={run.run_id} run={run} />)}
              </div>
            </>
          ) : (
            <EmptyState
              title="No runs match these filters"
              message="Change the evidence filter or clear the search text."
            />
          )}
        </Panel>
      ) : null}

      {rejected.length ? (
        <Panel className="rejected-panel">
          <SectionHeading
            title="Rejected bundles"
            headingId="rejected-runs-heading"
            meta="Measurements withheld"
          />
          <div className="rejection-intro">
            <AlertTriangle aria-hidden="true" />
            <p>
              These entries failed bounded verification. Inferdrome exposes the rejection reason, not untrusted measurements.
            </p>
          </div>
          <ul className="rejected-list" aria-labelledby="rejected-runs-heading">
            {rejected.map((item) => (
              <li key={item.entry}>
                <ShieldCheck aria-hidden="true" />
                <div>
                  <strong className="mono">{item.entry}</strong>
                  <span>{item.message}</span>
                </div>
                <div className="rejected-meta">
                  <StatusBadge status="INVALID" label="Rejected" />
                  <code>{item.code}</code>
                </div>
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}

      {latestVerified ? (
        <p className="index-footnote">
          Latest bundle digest <code>{shortDigest(latestVerified.bundle_digest)}</code>. Dashboard values are read-only projections of verified evidence.
        </p>
      ) : null}
    </>
  );
}
