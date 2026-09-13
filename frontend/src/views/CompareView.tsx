import { ArrowLeftRight, CircleSlash2, Info, Scale } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  Panel,
  SectionHeading,
  StatusBadge,
} from "../components/Primitives";
import { useRuns } from "../context/RunsContext";
import { useRequest } from "../hooks/useRequest";
import { api } from "../lib/api";
import {
  asFiniteNumber,
  comparisonReason,
  formatDateTime,
  formatDelta,
  formatPrimitive,
  humanize,
  metricLabel,
  runModelLabel,
} from "../lib/format";
import type { Comparison, ComparisonDelta, RunSummary } from "../lib/types";

function RunOption({ run }: { readonly run: RunSummary }) {
  return <>{run.run_id} · {runModelLabel(run)} · {formatDateTime(run.started_at)}</>;
}

function ComparisonBars({ delta }: { readonly delta: ComparisonDelta }) {
  const baseline = asFiniteNumber(delta.baseline_value);
  const candidate = asFiniteNumber(delta.candidate_value);
  const max = Math.max(Math.abs(baseline ?? 0), Math.abs(candidate ?? 0), 1);

  return (
    <div className="comparison-bar-group">
      <h3>{metricLabel(delta.metric, delta.label)} · {humanize(delta.aggregation)}</h3>
      <div className="comparison-bar-row">
        <span>Baseline</span>
        <span className="comparison-track" aria-hidden="true">
          <span className="comparison-fill comparison-baseline" style={{ width: `${Math.max(0, Math.abs(baseline ?? 0) / max) * 100}%` }} />
        </span>
        <span className="mono">{delta.baseline_display_value}</span>
      </div>
      <div className="comparison-bar-row">
        <span>Candidate</span>
        <span className="comparison-track" aria-hidden="true">
          <span className="comparison-fill comparison-candidate" style={{ width: `${Math.max(0, Math.abs(candidate ?? 0) / max) * 100}%` }} />
        </span>
        <span className="mono">{delta.candidate_display_value}</span>
      </div>
    </div>
  );
}

export function CompareView() {
  const runIndex = useRuns();
  const [baselineRunId, setBaselineRunId] = useState("");
  const [candidateRunId, setCandidateRunId] = useState("");

  useEffect(() => {
    if (runIndex.status !== "success" || !runIndex.runs.length) return;
    if (!candidateRunId) {
      setCandidateRunId(runIndex.runs[0].run_id);
    }
    if (!baselineRunId) {
      setBaselineRunId(runIndex.runs[1]?.run_id ?? runIndex.runs[0].run_id);
    }
  }, [baselineRunId, candidateRunId, runIndex.runs, runIndex.status]);

  const selectedBaseline = useMemo(
    () => runIndex.runs.find((run) => run.run_id === baselineRunId),
    [baselineRunId, runIndex.runs],
  );
  const selectedCandidate = useMemo(
    () => runIndex.runs.find((run) => run.run_id === candidateRunId),
    [candidateRunId, runIndex.runs],
  );

  const missingSelection = Boolean(
    (baselineRunId && !selectedBaseline) || (candidateRunId && !selectedCandidate),
  );
  const canCompare = Boolean(
    runIndex.status === "success" && selectedBaseline && selectedCandidate && baselineRunId !== candidateRunId,
  );
  const comparison = useRequest<Comparison>(
    async (signal) => {
      const result = await api.compareRuns(baselineRunId, candidateRunId, signal);
      if (result.baseline_run_id !== baselineRunId || result.candidate_run_id !== candidateRunId) {
        throw new Error("Returned comparison does not match the selected runs. Refresh runs and try again.");
      }
      return result;
    },
    [baselineRunId, candidateRunId, selectedBaseline?.bundle_digest, selectedCandidate?.bundle_digest, runIndex.refreshRevision],
    { enabled: canCompare },
  );

  const swapRuns = () => {
    setBaselineRunId(candidateRunId);
    setCandidateRunId(baselineRunId);
  };

  if (runIndex.status === "loading") {
    return (
      <>
        <PageHeader
          title="Compare runs"
          subtitle="Performance deltas appear only after evidence compatibility is established."
        />
        <LoadingState label="Loading comparable run candidates…" />
      </>
    );
  }
  if (runIndex.status === "error" && runIndex.error) {
    return (
      <>
        <PageHeader
          title="Compare runs"
          subtitle="Performance deltas appear only after evidence compatibility is established."
        />
        <ErrorState error={runIndex.error} retry={runIndex.retry} title="Runs cannot be compared yet" />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Compare runs"
        subtitle="Performance deltas appear only after Inferdrome confirms compatible metric and evidence contracts."
        action={<StatusBadge status="DESCRIPTIVE_ONLY" label="Descriptive · not causal" tone="neutral" />}
      />

      {runIndex.runs.length < 2 && !missingSelection ? (
        <EmptyState
          title="Two verified runs are required"
          message="Add another run bundle before requesting a pairwise comparison. Rejected bundles cannot be compared."
        />
      ) : (
        <>
          <Panel className="compare-control-panel">
            <div className="compare-controls">
              <label>
                <span>Baseline</span>
                <select value={baselineRunId} onChange={(event) => setBaselineRunId(event.target.value)}>
                  {baselineRunId && !selectedBaseline ? <option value={baselineRunId} disabled>{baselineRunId} · unavailable in refreshed snapshot</option> : null}
                  {runIndex.runs.map((run) => (
                    <option value={run.run_id} key={run.run_id}><RunOption run={run} /></option>
                  ))}
                </select>
              </label>
              <button
                className="swap-button"
                type="button"
                onClick={swapRuns}
                aria-label="Swap baseline and candidate"
                title="Swap baseline and candidate"
              >
                <ArrowLeftRight aria-hidden="true" />
              </button>
              <label>
                <span>Candidate</span>
                <select value={candidateRunId} onChange={(event) => setCandidateRunId(event.target.value)}>
                  {candidateRunId && !selectedCandidate ? <option value={candidateRunId} disabled>{candidateRunId} · unavailable in refreshed snapshot</option> : null}
                  {runIndex.runs.map((run) => (
                    <option value={run.run_id} key={run.run_id}><RunOption run={run} /></option>
                  ))}
                </select>
              </label>
            </div>
            <p className="comparison-disclaimer">
              <Info aria-hidden="true" /> Signed deltas describe candidate minus baseline. Color identifies each run, not performance direction.
            </p>
          </Panel>

          {missingSelection ? (
            <EmptyState title="A selected run is unavailable" message="The selected evidence is not in the latest verified run snapshot. Choose available runs, or refresh after restoring the evidence." />
          ) : !canCompare ? (
            <EmptyState title="Choose two different runs" message="A run cannot serve as both baseline and candidate." />
          ) : comparison.status === "loading" ? (
            <LoadingState label="Checking pairwise comparability…" />
          ) : comparison.status === "error" && comparison.error ? (
            <ErrorState error={comparison.error} retry={runIndex.refresh} title="The comparison could not be calculated" />
          ) : comparison.data ? (
            <ComparisonResult
              comparison={comparison.data}
              baselineLabel={selectedBaseline?.run_id ?? comparison.data.baseline_run_id}
              candidateLabel={selectedCandidate?.run_id ?? comparison.data.candidate_run_id}
            />
          ) : null}
        </>
      )}
    </>
  );
}

function ComparisonResult({
  comparison,
  baselineLabel,
  candidateLabel,
}: {
  readonly comparison: Comparison;
  readonly baselineLabel: string;
  readonly candidateLabel: string;
}) {
  const isIncomparable = comparison.status === "INCOMPARABLE";
  const statusTone = isIncomparable
    ? "neutral"
    : comparison.status === "COMPARABLE_WITH_CONTEXT_CHANGES"
      ? "warning"
      : "info";

  return (
    <div className="comparison-result" aria-live="polite">
      <section className={`comparability-banner comparability-${statusTone}`}>
        {isIncomparable ? <CircleSlash2 aria-hidden="true" /> : <Scale aria-hidden="true" />}
        <div>
          <h2>{humanize(comparison.status)}</h2>
          <p>
            {isIncomparable
              ? "Metric deltas are suppressed because the evidence contracts do not align."
              : `${candidateLabel} is described against ${baselineLabel}; no acceptance or causal claim is implied.`}
          </p>
          {comparison.reasons.length ? (
            <ul>
              {comparison.reasons.map((reason, index) => (
                <li key={`${comparisonReason(reason)}-${index}`}>{comparisonReason(reason)}</li>
              ))}
            </ul>
          ) : null}
        </div>
        <StatusBadge status={comparison.status} tone={statusTone} />
      </section>

      {!isIncomparable ? (
        comparison.metric_deltas.length ? (
          <>
            <div className="delta-grid">
              {comparison.metric_deltas.map((delta) => {
                return (
                  <article className="delta-card" key={`${delta.metric}-${delta.aggregation}`}>
                    <span>{metricLabel(delta.metric, delta.label)}</span>
                    <strong>{formatDelta(delta)}</strong>
                    <small>
                      {delta.baseline_display_value} → {delta.candidate_display_value}
                    </small>
                  </article>
                );
              })}
            </div>

            <div className="compare-detail-grid">
              <Panel>
                <SectionHeading
                  title="Baseline versus candidate"
                  meta={<span className="comparison-legend"><i className="legend-baseline" /> Baseline <i className="legend-candidate" /> Candidate</span>}
                />
                <div className="comparison-chart-list">
                  {comparison.metric_deltas.map((delta) => (
                    <ComparisonBars delta={delta} key={`${delta.metric}-${delta.aggregation}`} />
                  ))}
                </div>
              </Panel>
              <ConfigurationDiff comparison={comparison} />
            </div>
          </>
        ) : (
          <EmptyState
            title="No shared deltas returned"
            message="The runs are comparable, but the API did not return a metric delta projection."
          />
        )
      ) : (
        <ConfigurationDiff comparison={comparison} />
      )}
    </div>
  );
}

function ConfigurationDiff({ comparison }: { readonly comparison: Comparison }) {
  const changedCount = comparison.context_changes.length;
  return (
    <Panel className="config-panel">
      <SectionHeading title="Configuration diff" meta={`${changedCount} changed`} />
      {comparison.context_changes.length ? (
        <div className="table-scroll">
          <table className="config-table">
            <caption>Configuration values for baseline and candidate runs</caption>
            <thead>
              <tr><th scope="col">Field</th><th scope="col">Baseline</th><th scope="col">Candidate</th></tr>
            </thead>
            <tbody>
              {comparison.context_changes.map((change) => (
                <tr className="config-changed" key={change.key}>
                  <td>{change.label || humanize(change.key)}<span className="table-subtext">{humanize(change.group)}</span></td>
                  <td className="mono">{formatPrimitive(change.baseline_value)}</td>
                  <td className="mono">{formatPrimitive(change.candidate_value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="inline-empty"><Scale aria-hidden="true" /> No configuration fields were returned.</div>
      )}
    </Panel>
  );
}
