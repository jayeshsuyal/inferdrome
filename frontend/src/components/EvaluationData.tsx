import type { ReactNode } from "react";

import { evaluationLabel, exactEvaluationValue, formatEvaluationValue } from "../lib/evaluation-display";
import type { EvaluationContrast, EvaluationMetric, EvaluationPopulation, EvaluationRecovery, EvaluationSummary } from "../lib/evaluations";
import { StatusBadge } from "./Primitives";

export function EvaluationValue({ value, unit = "count", exact = false }: {
  readonly value: string | number | null;
  readonly unit?: EvaluationMetric["unit"];
  readonly exact?: boolean;
}) {
  const input = { value: value === null ? null : String(value), unit };
  return <span className={value === null ? "evaluation-empty" : "mono"}>
    {exact ? exactEvaluationValue(input) : formatEvaluationValue(input)}
  </span>;
}

export function EvaluationFacts({ metrics, exact = false }: {
  readonly metrics: readonly EvaluationMetric[];
  readonly exact?: boolean;
}) {
  return <dl className="evaluation-facts">
    {metrics.map((item) => <div key={item.key}>
      <dt>{item.label}</dt>
      <dd><EvaluationValue value={item.value} unit={item.unit} exact={exact} /></dd>
    </div>)}
  </dl>;
}

export function EvaluationDisclosure({ title, cue, children, className = "" }: {
  readonly title: string;
  readonly cue?: ReactNode;
  readonly children: ReactNode;
  readonly className?: string;
}) {
  return <details className={`evaluation-disclosure ${className}`.trim()}>
    <summary>
      <span className="evaluation-disclosure-title">{title}</span>
      {cue ? <span className="evaluation-disclosure-cue">{cue}</span> : null}
    </summary>
    <div className="evaluation-disclosure-body">{children}</div>
  </details>;
}

export function EvaluationTable({ caption, columns, children }: {
  readonly caption: string;
  readonly columns: readonly string[];
  readonly children: ReactNode;
}) {
  return <table className="evaluation-table">
    <caption>{caption}</caption>
    <thead><tr>{columns.map((column) => <th scope="col" key={column}>{column}</th>)}</tr></thead>
    <tbody>{children}</tbody>
  </table>;
}

export function EvaluationTrust({ summary }: { readonly summary: EvaluationSummary }) {
  return <div className="evaluation-boundary" role="note">
    <strong>Pinned report; source inputs not replayed</strong>
    <p>Report integrity and contract checked. Authorship and actual execution are not established here.</p>
    <div className="evaluation-badges">
      <StatusBadge
        status={summary.evidence_class ?? "UNAVAILABLE"}
        label={summary.evidence_class === "SYNTHETIC_ONLY" ? "Synthetic only" : summary.evidence_class === "LOCAL_MEASUREMENT_ONLY" ? "Local measurement only" : "Evidence class unavailable"}
      />
      <StatusBadge status="UNVERIFIED" label="Runtime unverified" tone="warning" />
      <StatusBadge status="INELIGIBLE" label="Evidence ineligible" tone="warning" />
      {summary.returned_records === 0 ? <StatusBadge status="UNAVAILABLE" label="No returned measurements" tone="warning" /> : null}
    </div>
    {summary.evidence_class === "LOCAL_MEASUREMENT_ONLY" ? <p>Local measurement classification does not establish cloud execution or successful requests.</p> : null}
  </div>;
}

export function EvaluationProvenance({ summary }: { readonly summary: EvaluationSummary }) {
  return <dl className="evaluation-facts evaluation-provenance">
    <div><dt>Report ID</dt><dd><code className="evaluation-digest">{summary.report_id}</code></dd></div>
    <div><dt>Report digest</dt><dd><code className="evaluation-digest">{summary.report_sha256}</code></dd></div>
    <div><dt>Plan digest</dt><dd><code className="evaluation-digest">{summary.plan_sha256}</code></dd></div>
    <div><dt>Config digest</dt><dd><code className="evaluation-digest">{summary.config_sha256 ?? "Unavailable"}</code></dd></div>
    <div><dt>Source schema</dt><dd><code>{summary.source_schema}</code></dd></div>
    <div><dt>Source inputs replayed</dt><dd>Not performed</dd></div>
    <div><dt>Tokenizer reverified here</dt><dd>No</dd></div>
    <div><dt>Report integrity status</dt><dd><code>{summary.report_integrity}</code></dd></div>
    <div><dt>Report contract status</dt><dd><code>{summary.report_contract}</code></dd></div>
    <div><dt>Source replay status</dt><dd><code>{summary.source_replay}</code></dd></div>
    <div><dt>Runtime verification status</dt><dd><code>{summary.runtime_verification}</code></dd></div>
    <div><dt>Evidence eligible</dt><dd><code>{String(summary.evidence_eligible)}</code></dd></div>
    <div><dt>Tokenizer reverified here flag</dt><dd><code>{String(summary.tokenizer_reverified_here)}</code></dd></div>
    <div><dt>Source evidence class</dt><dd><code>{summary.evidence_class ?? "UNAVAILABLE"}</code></dd></div>
    <div><dt>Source completion status</dt><dd><code>{summary.status}</code></dd></div>
    <div><dt>Source comparison status</dt><dd><code>{summary.comparison_status}</code></dd></div>
    <div><dt>Source reason</dt><dd><code>{summary.reason ?? "None reported"}</code></dd></div>
    <div><dt>Source calibration</dt><dd><code>{summary.calibration}</code></dd></div>
  </dl>;
}

function ContrastTable({ contrasts, exact = false }: {
  readonly contrasts: readonly EvaluationContrast[];
  readonly exact?: boolean;
}) {
  return <EvaluationTable
    caption={exact ? "Exact authoritative contrast values" : "Authoritative descriptive contrasts in requests/s"}
    columns={["Contrast", "Complete blocks", "Mean difference", "90% interval", "Interval status"]}
  >
    {contrasts.map((item) => <tr key={item.label}>
      <th scope="row" data-label="Contrast"><span>{item.label}</span></th>
      <td data-label="Complete blocks"><EvaluationValue value={item.complete_blocks} exact={exact} /></td>
      <td data-label="Mean difference"><EvaluationValue value={item.mean_rps} unit="requests/s" exact={exact} /></td>
      <td data-label="90% interval">
        {item.lower_rps === null ? <EvaluationValue value={null} /> : <span className="mono">[{item.lower_rps}, {item.upper_rps}] requests/s</span>}
      </td>
      <td data-label="Interval status"><span>{exact ? item.interval_status : evaluationLabel(item.interval_status)}</span></td>
    </tr>)}
  </EvaluationTable>;
}

export function EvaluationContrasts({ contrasts }: { readonly contrasts: readonly EvaluationContrast[] }) {
  return <>
    <ContrastTable contrasts={contrasts} />
    <EvaluationDisclosure title="Exact contrast values" cue="Source precision and interval statuses">
      <ContrastTable contrasts={contrasts} exact />
    </EvaluationDisclosure>
  </>;
}

export function evaluationPopulationCue(population: EvaluationPopulation | null): string {
  if (population === null) return "Measurements unavailable";
  const cues: string[] = [];
  if (population.cancelled) cues.push("Population cancelled");
  const belowFloor = population.latency.find((row) => row.p99_status === "BELOW_REPORTING_FLOOR");
  if (belowFloor) cues.push("p99 unavailable: below reporting floor");
  else if (population.latency.length === 0) cues.push("Latency unavailable");
  return cues.join(" · ") || "All offered outcomes retained";
}

export function evaluationRecoveryCue(recovery: EvaluationRecovery): string {
  return recovery.intervals
    .filter((row) => row.status === "UNOBSERVED_OR_CENSORED" || row.status === "RESTORE_NOT_OBSERVED")
    .map((row) => `${evaluationLabel(row.metric)}: ${evaluationLabel(row.status)}`)
    .join(" · ");
}

function OutcomeTable({ population, exact = false }: {
  readonly population: EvaluationPopulation;
  readonly exact?: boolean;
}) {
  return <EvaluationTable
    caption={exact ? "Exact offered outcome values" : "All offered outcomes, including failures and cancellations"}
    columns={["Outcome", "Count", "Offered fraction"]}
  >
    {population.outcomes.map((item) => <tr key={item.outcome}>
      <th scope="row" data-label="Outcome"><span>{exact ? item.outcome : evaluationLabel(item.outcome)}</span></th>
      <td data-label="Count"><EvaluationValue value={item.count} exact={exact} /></td>
      <td data-label="Offered fraction"><EvaluationValue value={item.offered_fraction} unit="ratio" exact={exact} /></td>
    </tr>)}
  </EvaluationTable>;
}

function LatencyTable({ population, exact = false }: {
  readonly population: EvaluationPopulation;
  readonly exact?: boolean;
}) {
  return <EvaluationTable
    caption={exact ? "Exact latency quantiles in nanoseconds" : "Authoritative latency quantiles"}
    columns={["Series and population", "Count", "p50", "p90", "p95", "p99", "p99 availability"]}
  >
    {population.latency.map((item, index) => <tr key={`${item.label}-${index}`}>
      <th scope="row" data-label="Series and population">
        <span>{exact ? item.label : evaluationLabel(item.label)}</span>
        <span className="table-subtext">{exact ? item.population : evaluationLabel(item.population)}</span>
      </th>
      <td data-label="Count"><EvaluationValue value={item.count} exact={exact} /></td>
      <td data-label="p50"><EvaluationValue value={item.p50_ns} unit="ns" exact={exact} /></td>
      <td data-label="p90"><EvaluationValue value={item.p90_ns} unit="ns" exact={exact} /></td>
      <td data-label="p95"><EvaluationValue value={item.p95_ns} unit="ns" exact={exact} /></td>
      <td data-label="p99"><EvaluationValue value={item.p99_ns} unit="ns" exact={exact} /></td>
      <td data-label="p99 availability"><span>{exact ? item.p99_status : evaluationLabel(item.p99_status)}</span></td>
    </tr>)}
  </EvaluationTable>;
}

export function EvaluationPopulationData({ population }: { readonly population: EvaluationPopulation }) {
  const primaryKeys = ["offered_count", "successful_count", "dispatched_count", "slo_good_count", "slo_success_fraction", "slo_goodput_rps"];
  return <div>
    <EvaluationFacts metrics={population.metrics.filter((item) => primaryKeys.includes(item.key))} />
    <div className="evaluation-badges" aria-label="Reported nonzero outcomes">
      {population.outcomes.filter((item) => item.count > 0).map((item) => <StatusBadge
        key={item.outcome}
        status={item.outcome}
        tone={item.outcome === "SUCCESS" ? "neutral" : "warning"}
        label={`${evaluationLabel(item.outcome)}: ${item.count}`}
      />)}
      {population.cancelled ? <StatusBadge status="CANCELLED" label="Population cancelled" tone="warning" /> : null}
    </div>
    <p className="evaluation-note">{evaluationPopulationCue(population)}. All offered requests remain in the denominator.</p>

    <EvaluationDisclosure title="Outcomes and offered fractions" cue="Includes failures, rejections and cancellations">
      <OutcomeTable population={population} />
    </EvaluationDisclosure>
    <EvaluationDisclosure title="Latency and dispatch timing" cue={evaluationPopulationCue(population)}>
      <p className="evaluation-note">All successful requests, including SLO misses. Scheduled and dispatch origins remain separate; partial failed-request timings are separate populations.</p>
      {population.latency.length ? <LatencyTable population={population} /> : <p className="evaluation-note">Latency quantiles are unavailable for this population.</p>}
    </EvaluationDisclosure>
    <EvaluationDisclosure title="Population accounting and reported usage" cue="Fixed window, drain and conditional usage coverage">
      <EvaluationFacts metrics={population.metrics} />
      {population.usage.length ? <div className="evaluation-section"><h3>Reported usage coverage</h3><EvaluationFacts metrics={population.usage} /></div> : null}
    </EvaluationDisclosure>
    <EvaluationDisclosure title="Exact population values" cue="All source values; timings in nanoseconds">
      <EvaluationFacts metrics={population.metrics} exact />
      <OutcomeTable population={population} exact />
      {population.latency.length ? <LatencyTable population={population} exact /> : null}
      <EvaluationFacts metrics={population.usage} exact />
    </EvaluationDisclosure>
  </div>;
}

function RecoveryValues({ recovery, exact = false }: {
  readonly recovery: EvaluationRecovery;
  readonly exact?: boolean;
}) {
  return <>
    <dl className="evaluation-facts">
      <div><dt>Planned restore</dt><dd><EvaluationValue value={recovery.planned_restore_ns} unit="ns" exact={exact} /></dd></div>
      <div><dt>Actual restore</dt><dd><EvaluationValue value={recovery.actual_restore_ns} unit="ns" exact={exact} /></dd></div>
      <div><dt>Background active at restore</dt><dd>{recovery.background_active_at_restore === null ? "Unavailable" : recovery.background_active_at_restore ? "Yes" : "No"}</dd></div>
    </dl>
    <EvaluationTable
      caption={exact ? "Exact recovery values from actual restoration" : "Publication, decision and dispatch recovery from actual restoration"}
      columns={["Recovery stage", "Status", "Duration", "Observation horizon"]}
    >
      {recovery.intervals.map((item) => <tr key={item.metric}>
        <th scope="row" data-label="Recovery stage"><span>{exact ? item.metric : evaluationLabel(item.metric)}</span></th>
        <td data-label="Status"><span>{exact ? item.status : evaluationLabel(item.status)}</span></td>
        <td data-label="Duration"><EvaluationValue value={item.duration_ns} unit="ns" exact={exact} /></td>
        <td data-label="Observation horizon"><EvaluationValue value={item.observation_horizon_ns} unit="ns" exact={exact} /></td>
      </tr>)}
    </EvaluationTable>
  </>;
}

export function EvaluationRecoveryData({ recovery }: { readonly recovery: EvaluationRecovery }) {
  return <div className="evaluation-section">
    <h3>Recovery</h3>
    <p className="evaluation-note">{evaluationLabel(recovery.applicability)}. Intervals start at actual telemetry restoration. Scheduled restoration does not establish actual restoration or overload.</p>
    <RecoveryValues recovery={recovery} />
    <p className="evaluation-note">Observed zero remains zero. Unobserved or censored recovery stays unavailable. Publication, decision and dispatch recovery remain distinct.</p>
    <EvaluationDisclosure title="Exact recovery values" cue="Source statuses, origin and nanoseconds">
      <p className="evaluation-note">Origin: <code>{recovery.origin}</code> · Applicability: <code>{recovery.applicability}</code></p>
      <RecoveryValues recovery={recovery} exact />
    </EvaluationDisclosure>
  </div>;
}
