import {
  AlertTriangle,
  ArrowRight,
  ArrowUpRight,
  GitCompareArrows,
  Search,
  ShieldCheck,
  SlidersHorizontal,
} from "lucide-react";
import { useMemo, useState } from "react";

import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  Panel,
  SectionHeading,
  StatusBadge,
} from "../components/Primitives";
import { useControlledComparisons } from "../hooks/useControlledComparisons";
import { formatDateTime, shortDigest } from "../lib/format";
import { Link } from "../lib/router";
import type {
  ControlledComparisonResultStatus,
  ControlledComparisonSummary,
} from "../lib/types";

type ComparisonFilter = "ALL" | "COMPARABLE" | "INCOMPARABLE";

function resultLabel(status: ControlledComparisonResultStatus): string {
  if (status === "NO_RESULT") return "No result artifact";
  if (status === "WITHHELD") return "Withheld";
  return status;
}

function ResultStatus({ status }: { readonly status: ControlledComparisonResultStatus }) {
  if (status === "NO_RESULT") return <span className="comparison-no-result">No result artifact</span>;
  return (
    <StatusBadge
      status={status}
      label={resultLabel(status)}
      tone={status === "COMPARABLE" ? "info" : status === "WITHHELD" ? "warning" : "neutral"}
    />
  );
}

function ComparisonIdentity({ comparison }: { readonly comparison: ControlledComparisonSummary }) {
  return (
    <>
      <Link
        className="comparison-plan-link"
        to={`/comparisons/${encodeURIComponent(comparison.comparison_plan_id)}`}
      >
        <span>{comparison.title}</span>
        <ArrowUpRight aria-hidden="true" />
      </Link>
      <span className="table-subtext mono truncate-id" title={comparison.comparison_plan_id}>
        {comparison.comparison_plan_id}
      </span>
    </>
  );
}

function EstimateSummary({ comparison }: { readonly comparison: ControlledComparisonSummary }) {
  if (comparison.result_status === "COMPARABLE") {
    return (
      <span className="comparison-estimate">
        <strong>{comparison.estimate_display_value}</strong>
        <small>{comparison.primary_outcome_label}</small>
      </span>
    );
  }
  if (comparison.result_status === "NO_RESULT") {
    return <span className="comparison-muted-value">No result artifact</span>;
  }
  if (comparison.result_status === "WITHHELD") {
    return <span className="comparison-muted-value">Withheld</span>;
  }
  return <span className="comparison-muted-value">Suppressed</span>;
}

function ComparisonCard({ comparison }: { readonly comparison: ControlledComparisonSummary }) {
  return (
    <article className="controlled-comparison-card">
      <div className="controlled-comparison-card-head">
        <div><ComparisonIdentity comparison={comparison} /></div>
        <ResultStatus status={comparison.result_status} />
      </div>
      <dl>
        <div className="comparison-card-treatment">
          <dt>Declared treatment</dt>
          <dd>
            <code>{comparison.treatment_path}</code>
            <span>{comparison.baseline_value} → {comparison.candidate_value}</span>
          </dd>
        </div>
        <div><dt>Baseline arm</dt><dd title={comparison.baseline_trial_set_id}>{comparison.baseline_trial_set_id}</dd></div>
        <div><dt>Candidate arm</dt><dd title={comparison.candidate_trial_set_id}>{comparison.candidate_trial_set_id}</dd></div>
        <div><dt>Design</dt><dd><code>PREDECLARED</code> · {comparison.planned_repetitions_per_arm} + {comparison.planned_repetitions_per_arm} runs</dd></div>
        <div className="comparison-card-estimate"><dt>Declared outcome</dt><dd><EstimateSummary comparison={comparison} /></dd></div>
      </dl>
    </article>
  );
}

export function ControlledComparisonsView() {
  const request = useControlledComparisons();
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<ComparisonFilter>("ALL");

  const filteredComparisons = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (request.data?.comparisons ?? []).filter((comparison) => {
      if (filter !== "ALL" && comparison.result_status !== filter) return false;
      if (!needle) return true;
      return [
        comparison.comparison_plan_id,
        comparison.title,
        comparison.experiment_id,
        comparison.treatment_path,
        comparison.baseline_value,
        comparison.candidate_value,
        comparison.baseline_trial_set_id,
        comparison.candidate_trial_set_id,
        comparison.result_status,
        comparison.primary_outcome_label,
      ].join(" ").toLowerCase().includes(needle);
    });
  }, [filter, query, request.data]);

  const header = (
    <PageHeader
      title="Controlled comparisons"
      subtitle="Operator-attested designs over verified baseline and candidate Trial Sets."
      action={
        <Link className="button button-secondary" to="/compare">
          <GitCompareArrows aria-hidden="true" />
          Compare two runs
        </Link>
      }
    />
  );

  if (request.status === "loading") {
    return <>{header}<LoadingState label="Verifying controlled-comparison declarations…" /></>;
  }
  if (request.status === "error" && request.error) {
    return (
      <>
        {header}
        <ErrorState
          error={request.error}
          retry={request.retry}
          title="The controlled-comparison index is unavailable"
        />
      </>
    );
  }

  const comparisons = request.data?.comparisons ?? [];
  const rejected = request.data?.rejected ?? [];

  return (
    <>
      {header}
      <Panel className="controlled-comparisons-panel">
        <SectionHeading
          title="Comparison plans"
          headingId="controlled-comparisons-heading"
          meta={`${filteredComparisons.length} of ${comparisons.length}`}
        />

        <div className="comparison-scope-note">
          <SlidersHorizontal aria-hidden="true" />
          <div>
            <strong><code>PREDECLARED</code> · <code>OPERATOR_ATTESTED</code></strong>
            <p>
              <code>PREDECLARED</code> means the design choices were frozen in a local artifact before the named run workspaces under the operator-attested workflow. Plan chronology is not independently proven. It does not establish causality, recommendation, winner, or acceptance.
            </p>
          </div>
        </div>

        {comparisons.length ? (
          <div className="comparison-index-controls">
            <label className="search-field">
              <span className="visually-hidden">Search controlled comparisons</span>
              <Search aria-hidden="true" />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search plan, experiment, treatment, or outcome"
              />
            </label>
            <div className="comparison-filter-group" aria-label="Filter comparison results">
              {(["ALL", "COMPARABLE", "INCOMPARABLE"] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={filter === value}
                  onClick={() => setFilter(value)}
                >
                  {value === "ALL" ? "All" : value === "COMPARABLE" ? "Comparable" : "Incomparable"}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {comparisons.length === 0 ? (
          <div className="comparison-index-state">
            <EmptyState
              title="No verified comparison plans"
              message={
                rejected.length
                  ? "Every discovered declaration was withheld. Review the bounded reasons below."
                  : "Create an immutable controlled-comparison plan to make its frozen design visible here."
              }
              kind={rejected.length ? "warning" : "empty"}
            />
          </div>
        ) : filteredComparisons.length ? (
          <>
            <div className="table-scroll controlled-comparison-table-wrap">
              <table className="controlled-comparison-table" aria-labelledby="controlled-comparisons-heading">
                <caption>Verified Inferdrome controlled-comparison plans</caption>
                <thead>
                  <tr>
                    <th scope="col">Comparison</th>
                    <th scope="col">Declared treatment</th>
                    <th scope="col">Arms</th>
                    <th scope="col">Design</th>
                    <th scope="col">Result</th>
                    <th scope="col">Point estimate</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredComparisons.map((comparison) => (
                    <tr key={comparison.comparison_plan_id}>
                      <td>
                        <ComparisonIdentity comparison={comparison} />
                        <span className="table-subtext">{formatDateTime(comparison.created_at)}</span>
                      </td>
                      <td>
                        <code>{comparison.treatment_path}</code>
                        <span className="table-subtext mono">{comparison.baseline_value} → {comparison.candidate_value}</span>
                      </td>
                      <td>
                        <span className="comparison-arm-count">{comparison.planned_repetitions_per_arm} + {comparison.planned_repetitions_per_arm} runs</span>
                        <span className="table-subtext mono" title={comparison.baseline_trial_set_id}>B · {shortDigest(comparison.baseline_trial_set_id)}</span>
                        <span className="table-subtext mono" title={comparison.candidate_trial_set_id}>C · {shortDigest(comparison.candidate_trial_set_id)}</span>
                      </td>
                      <td>
                        <StatusBadge status="PREDECLARED" label="PREDECLARED" tone="neutral" />
                        <span className="table-subtext"><code>OPERATOR_ATTESTED</code></span>
                      </td>
                      <td><ResultStatus status={comparison.result_status} /></td>
                      <td><EstimateSummary comparison={comparison} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="controlled-comparison-card-list">
              {filteredComparisons.map((comparison) => (
                <ComparisonCard key={comparison.comparison_plan_id} comparison={comparison} />
              ))}
            </div>
          </>
        ) : (
          <div className="comparison-index-state">
            <EmptyState
              title="No comparisons match this view"
              message="Clear the search text or select another comparability filter."
            />
          </div>
        )}

        {rejected.length ? (
          <div className="comparison-rejections">
            <SectionHeading
              title="Withheld declarations"
              headingId="rejected-controlled-comparisons-heading"
              meta="Designs and estimates not projected"
            />
            <div className="rejection-intro">
              <AlertTriangle aria-hidden="true" />
              <p>These entries did not satisfy bounded controlled-comparison verification.</p>
            </div>
            <ul className="rejected-list" aria-labelledby="rejected-controlled-comparisons-heading">
              {rejected.map((item) => (
                <li key={`${item.entry}:${item.code}:${item.failure_fingerprint}`}>
                  <ShieldCheck aria-hidden="true" />
                  <div>
                    <strong className="mono">{item.entry}</strong>
                    <span>{item.message}</span>
                  </div>
                  <div className="rejected-meta">
                    <StatusBadge status="WITHHELD" label="Withheld" tone="warning" />
                    <code>{item.code}</code>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </Panel>

      <div className="comparison-index-footnote">
        <ArrowRight aria-hidden="true" />
        <span>Need a descriptive check without a frozen design? Use <Link to="/compare">Compare two runs</Link>.</span>
      </div>
    </>
  );
}
