import type { ReactNode } from "react";

import { humanize, shortDigest } from "../lib/format";
import type { EvaluationContrast, EvaluationMetric, EvaluationPopulation, EvaluationRecovery, EvaluationSummary } from "../lib/evaluations";
import { evaluationEngine } from "../lib/evaluations";
import { StatusBadge } from "./Primitives";

export function EvaluationValue({ value, unit }: {
  readonly value: string | number | null;
  readonly unit?: string;
}) {
  return value === null ? <span className="evaluation-empty">Unavailable</span> : <span className="mono">
    {value}
    {unit ? ` ${unit}` : ""}
  </span>;
}
export function EvaluationFacts({ metrics }: { readonly metrics: readonly EvaluationMetric[]; }) {
  return <dl className="evaluation-facts">
    {metrics.map((item) => <div key={item.key}>
      <dt>
        {item.label}
      </dt>
      <dd>
        <EvaluationValue value={item.value} unit={item.unit} />
      </dd>
    </div>)}
  </dl>;
}
export function EvaluationTable({ caption, columns, children }: {
  readonly caption: string;
  readonly columns: readonly string[];
  readonly children: ReactNode;
}) {
  return <table className="evaluation-table">
    <caption>
      {caption}
    </caption>
    <thead>
      <tr>
        {columns.map((column) => <th scope="col" key={column}>
          {column}
        </th>)}
      </tr>
    </thead>
    <tbody>
      {children}
    </tbody>
  </table>;
}
export function EvaluationTrust({ summary }: { readonly summary: EvaluationSummary; }) {
  const engine = evaluationEngine(summary);
  return <div className="evaluation-boundary" role="note">

    <strong>Pinned report; source inputs not replayed</strong>

    <p>The retained digest and report contract were checked. Reducer authorship, source execution and runtime identity are not established here. Evidence eligibility remains false.</p>

    <div className="evaluation-badges">

      {engine ? <StatusBadge status="SGLANG" label={`SGLang ${engine.producer_version}`} /> : null}

      <StatusBadge
        status={summary.evidence_class ?? "UNAVAILABLE"}
        label={summary.evidence_class === "SYNTHETIC_ONLY" ? "Synthetic only" : summary.evidence_class === "LOCAL_MEASUREMENT_ONLY" ? "Local measurement only" : "Evidence class unavailable"}
      />

      <StatusBadge status="UNVERIFIED" label="Runtime unverified" tone="warning" />

      <StatusBadge status="INELIGIBLE" label="Evidence ineligible" tone="warning" />

      {summary.returned_records === 0 ? <StatusBadge status="UNAVAILABLE" label="No returned measurements" tone="warning" /> : null}
    </div>

    {summary.evidence_class === "LOCAL_MEASUREMENT_ONLY" ? <p>Local measurement classification does not establish cloud execution or successful requests.</p> : null}
    {engine ? <>
      <p>Reported running and queued requests describe SGLang scheduler gauges. Scrape freshness measures acquisition-start age; scheduler-state age is unavailable.</p>
      <p>Cold cache and warmup, drain and flush are declared. This viewer does not verify the reset or cache state.</p>
    </> : null}
  </div>;
}
export function EvaluationProvenance({ summary }: { readonly summary: EvaluationSummary; }) {
  const engine = evaluationEngine(summary);
  return <dl className="evaluation-facts">

    {engine ? <>
      <div>
        <dt>Serving engine</dt>
        <dd>SGLang {engine.producer_version}</dd>
      </div>
      <div>
        <dt>Declared engine profile</dt>
        <dd>Single device · BF16</dd>
      </div>
      <div>
        <dt>Engine binding digest</dt>
        <dd><code className="evaluation-digest" title={engine.engine_binding_sha256}>{shortDigest(engine.engine_binding_sha256)}</code></dd>
      </div>
      <div>
        <dt>Engine configuration digest</dt>
        <dd><code className="evaluation-digest" title={engine.engine_choice_sha256}>{shortDigest(engine.engine_choice_sha256)}</code></dd>
      </div>
    </> : null}

    <div>
      <dt>Report digest</dt>
      <dd>
        <code className="evaluation-digest" title={summary.report_sha256}>
          {shortDigest(summary.report_sha256)}
        </code>
      </dd>
    </div>

    <div>
      <dt>Plan digest</dt>
      <dd>
        <code className="evaluation-digest" title={summary.plan_sha256}>
          {shortDigest(summary.plan_sha256)}
        </code>
      </dd>
    </div>

    <div>
      <dt>{engine ? "Study config digest" : "Config digest"}</dt>
      <dd>
        {summary.config_sha256 ? <code className="evaluation-digest" title={summary.config_sha256}>
          {shortDigest(summary.config_sha256)}
        </code> : "Unavailable"}
      </dd>
    </div>

    <div>
      <dt>Source schema</dt>
      <dd>
        <code>
          {summary.source_schema}
        </code>
      </dd>
    </div>

    <div>
      <dt>Source inputs replayed</dt>
      <dd>Not performed</dd>
    </div>

    <div>
      <dt>Tokenizer reverified here</dt>
      <dd>No</dd>
    </div>
  </dl>;
}
export function EvaluationContrasts({ contrasts }: { readonly contrasts: readonly EvaluationContrast[]; }) {
  return <EvaluationTable
    caption="Authoritative descriptive contrasts in requests/s"
    columns={["Contrast", "Complete blocks", "Mean difference", "90% interval", "Interval status"]}
  >

    {contrasts.map((item) => <tr key={item.label}>

      <th scope="row" data-label="Contrast">
        <span>
          {item.label}
        </span>
      </th>

      <td data-label="Complete blocks">
        <EvaluationValue value={item.complete_blocks} />
      </td>

      <td data-label="Mean difference">
        <EvaluationValue value={item.mean_rps} unit="requests/s" />
      </td>

      <td data-label="90% interval">
        {item.lower_rps === null ? <EvaluationValue value={null} /> : <span className="mono">[{item.lower_rps}, {item.upper_rps}] requests/s</span>}
      </td>

      <td data-label="Interval status">
        <span>
          {humanize(item.interval_status)}
        </span>
      </td>
    </tr>)}
  </EvaluationTable>;
}
const latencyPopulation = (population: string) => population === "ALL_SUCCESS_INCLUDING_SLO_MISSES" ? "All successful requests, including SLO misses"
  : population === "ALL_DISPATCHED_OUTCOMES" ? "All dispatched outcomes" : humanize(population);
export function EvaluationPopulationData({ population }: { readonly population: EvaluationPopulation; }) {
  return <div>

    {population.cancelled ? <p className="evaluation-note"><StatusBadge status="CANCELLED" label="Population cancelled" tone="warning" /> Cancelled offers remain in the reported population.</p> : null}

    <EvaluationFacts metrics={population.metrics} />

    <div className="evaluation-section">
      <h3>Outcomes and offered fractions</h3>

      <EvaluationTable caption="All offered outcomes, including failures and cancellations" columns={["Outcome", "Count", "Offered fraction"]}>

        {population.outcomes.map((item) => <tr key={item.outcome}>
          <th scope="row" data-label="Outcome">
            <span>
              {humanize(item.outcome)}
            </span>
          </th>
          <td data-label="Count">
            <EvaluationValue value={item.count} />
          </td>
          <td data-label="Offered fraction">
            <EvaluationValue value={item.offered_fraction} unit="ratio" />
          </td>
        </tr>)}
      </EvaluationTable>
    </div>

    {population.latency.length ? <div className="evaluation-section">
      <h3>Latency and dispatch timing</h3>

      <p className="evaluation-note">Successful latency includes all successful requests, including SLO misses. Scheduled and dispatch origins remain separate. Partial failed-request timings are separate populations.</p>

      <EvaluationTable
        caption="Authoritative latency quantiles in nanoseconds"
        columns={["Series and population", "Count", "p50", "p90", "p95", "p99", "p99 availability"]}
      >

        {population.latency.map((item, index) => <tr key={`${item.label}-${index}`}>

          <th scope="row" data-label="Series and population">
            <span>
              {item.label}
            </span>
            <span className="table-subtext">
              {latencyPopulation(item.population)}
            </span>
          </th>

          <td data-label="Count">
            <EvaluationValue value={item.count} />
          </td>

          <td data-label="p50">
            <EvaluationValue value={item.p50_ns} unit="ns" />
          </td>
          <td data-label="p90">
            <EvaluationValue value={item.p90_ns} unit="ns" />
          </td>
          <td data-label="p95">
            <EvaluationValue value={item.p95_ns} unit="ns" />
          </td>
          <td data-label="p99">
            <EvaluationValue value={item.p99_ns} unit="ns" />
          </td>
          <td data-label="p99 availability">
            <span>
              {humanize(item.p99_status)}
            </span>
          </td>
        </tr>)}
      </EvaluationTable>
    </div> : <p className="evaluation-note">Latency quantiles are unavailable for this population.</p>}

    {population.usage.length ? <div className="evaluation-section">
      <h3>Reported usage coverage</h3>
      <EvaluationFacts metrics={population.usage} />
    </div> : null}
  </div>;
}
export function EvaluationRecoveryData({ recovery }: { readonly recovery: EvaluationRecovery; }) {
  return <div className="evaluation-section">
    <h3>Recovery</h3>

    <p className="evaluation-note">{humanize(recovery.applicability)}. Intervals start at the actual telemetry-restored event. Scheduled restoration does not establish actual restoration or overload.</p>

    <dl className="evaluation-facts">
      <div>
        <dt>Planned restore</dt>
        <dd>
          <EvaluationValue value={recovery.planned_restore_ns} unit="ns" />
        </dd>
      </div>
      <div>
        <dt>Actual restore</dt>
        <dd>
          <EvaluationValue value={recovery.actual_restore_ns} unit="ns" />
        </dd>
      </div>
      <div>
        <dt>Background active at restore</dt>
        <dd>
          {recovery.background_active_at_restore === null ? "Unavailable" : recovery.background_active_at_restore ? "Yes" : "No"}
        </dd>
      </div>
    </dl>

    <EvaluationTable
      caption="Publication, decision and dispatch recovery from actual restoration"
      columns={["Recovery stage", "Status", "Duration", "Observation horizon"]}
    >

      {recovery.intervals.map((item) => <tr key={item.metric}>
        <th scope="row" data-label="Recovery stage">
          <span>
            {humanize(item.metric)}
          </span>
        </th>
        <td data-label="Status">
          <span>
            {humanize(item.status)}
          </span>
        </td>
        <td data-label="Duration">
          <EvaluationValue value={item.duration_ns} unit="ns" />
        </td>
        <td data-label="Observation horizon">
          <EvaluationValue value={item.observation_horizon_ns} unit="ns" />
        </td>
      </tr>)}
    </EvaluationTable>

    <p className="evaluation-note">Observed zero is shown as 0 ns. Unobserved or censored recovery is unavailable; publication is distinct from decision and dispatch recovery.</p>
  </div>;
}
