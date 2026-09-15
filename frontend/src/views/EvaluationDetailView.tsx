import { ArrowLeft, RefreshCw } from "lucide-react";
import { useState } from "react";

import {
  EvaluationContrasts,
  EvaluationFacts,
  EvaluationPopulationData,
  EvaluationProvenance,
  EvaluationRecoveryData,
  EvaluationTable,
  EvaluationTrust,
  EvaluationValue
} from "../components/EvaluationData";
import { EmptyState, ErrorState, LoadingState, PageHeader, Panel, SectionHeading, StatusBadge } from "../components/Primitives";
import { useEvaluationDetail } from "../hooks/useEvaluations";
import type { EvaluationCacheDetail, EvaluationCell, EvaluationMetric, EvaluationReportDetail, EvaluationStudyDetail } from "../lib/evaluations";
import { humanize, shortDigest } from "../lib/format";
import { Link, useParams } from "../lib/router";

const valueAt = (metrics: readonly EvaluationMetric[], key: string) => metrics.find((item) => item.key === key)?.value ?? null;

function StudyResults({ detail }: { readonly detail: EvaluationStudyDetail; }) {
  const [trialIndex, setTrialIndex] = useState(detail.trials[0]?.index ?? 0);
  const [population, setPopulation] = useState("foreground");
  const trial = detail.trials.find((item) => item.index === trialIndex);
  const selectedPopulation = trial ? population === "foreground" ? trial.foreground : trial.background : null;
  return <>

    {detail.strata.map((stratum) => <Panel className="evaluation-panel" key={stratum.index}>

      <SectionHeading
        title={`Stratum ${stratum.index} · ${humanize(stratum.scenario)}`}
        meta={`Profile ${stratum.profile_index} · ${stratum.target_endpoint ?? "No target endpoint"}`}
      />

      <EvaluationFacts metrics={stratum.metrics} />

      <div className="evaluation-section">
        <h3>Four study policies</h3>
        <p className="evaluation-note">Returned and completed trial counts remain distinct. Policy summaries are authoritative per-trial summaries; incomplete reports do not supply a comparative headline.</p>

        <EvaluationTable caption="Four evaluation policies and reported goodput summaries" columns={["Policy", "Reported summaries"]}>

          {stratum.policies.map((policy) => <tr key={policy.policy_id}>
            <th scope="row" data-label="Policy">
              <code>
                {policy.policy_id}
              </code>
            </th>
            <td data-label="Reported summaries">
              <EvaluationFacts metrics={policy.metrics} />
            </td>
          </tr>)}
        </EvaluationTable>
      </div>

      <div className="evaluation-section">
        <h3>Paired policy contrasts</h3>
        <EvaluationContrasts contrasts={stratum.contrasts} />
      </div>
    </Panel>)}

    <Panel className="evaluation-panel">
      <SectionHeading title="Trial measurements" meta="Foreground and background populations remain separate" />

      {detail.trials.length ? <>

        <div className="evaluation-controls">

          <label>Trial<select
            aria-label="Trial"
            value={trialIndex}
            onChange={(event) => { setTrialIndex(Number(event.target.value)); setPopulation("foreground"); }}
          >
            {detail.trials.map((item) => <option key={item.index} value={item.index}>Trial {item.index} · Block {item.block_index} · {item.policy_id}</option>)}
          </select></label>

          <label>Population<select aria-label="Population" value={population} onChange={(event) => setPopulation(event.target.value)}>
            <option value="foreground">Foreground</option>
            <option value="background">Background</option>
          </select></label>
        </div>

        {trial ? <>

          <div className="evaluation-badges">
            <StatusBadge status={trial.status} />
            <span>Block {trial.block_index} · Profile {trial.profile_index} · {humanize(trial.scenario)}</span>
          </div>

          <p className="evaluation-note"><code>
            {trial.policy_id}
          </code> · Result <code title={trial.result_sha256}>
              {shortDigest(trial.result_sha256)}
            </code></p>

          <EvaluationFacts metrics={trial.metrics} />

          <div className="evaluation-section">
            <h3>
              {population === "foreground" ? "Foreground population" : "Background population"}
            </h3>

            {population === "background" ? <p className="evaluation-note">Background requests do not inflate foreground success. Only the background statistics supplied by the report are shown.</p> : <p className="evaluation-note">All offered foreground requests remain in the SLO denominator. Goodput uses the fixed offered window; successful completions during drain remain identified separately.</p>}

            {selectedPopulation ? <EvaluationPopulationData population={selectedPopulation} /> : <EmptyState
              title="Background measurements unavailable"
              message="This trial report has no background population; unavailable values are not zero measurements."
            />}
          </div>

          <EvaluationRecoveryData recovery={trial.recovery} />
        </> : null}
      </> : <EmptyState
        title="No returned trial measurements"
        message="Planned, aborted and not-run trial coverage remains above. Missing measurements do not become successful requests or measured zeros."
      />}
    </Panel>
  </>;
}

function CacheCellDetail({ cell }: { readonly cell: EvaluationCell; }) {
  return <div className="evaluation-section">

    <h3>{cell.condition} · Cell {cell.index} details</h3>

    <div className="evaluation-badges">
      <StatusBadge status={cell.status} />
      <StatusBadge
        status={cell.declarations_consistent ? "CONSISTENT" : "UNVERIFIED"}
        label={cell.declarations_consistent ? "Declarations consistent" : "Declarations incomplete or inconsistent"}
        tone={cell.declarations_consistent ? "neutral" : "warning"}
      />
    </div>

    <p className="evaluation-note">Reason: {humanize(cell.reason)}. Cleanup: {humanize(cell.cleanup)}. Declared preparation does not verify cache treatment.</p>

    {cell.declaration_reasons.length ? <ul className="evaluation-note">
      {cell.declaration_reasons.map((reason) => <li key={reason}>
        {humanize(reason)}
      </li>)}
    </ul> : null}

    <EvaluationFacts metrics={cell.metrics} />

    <p className="evaluation-note">Config <code title={cell.config_sha256}>
      {shortDigest(cell.config_sha256)}
    </code> · Result {cell.result_sha256 ? <code title={cell.result_sha256}>
      {shortDigest(cell.result_sha256)}
    </code> : "Unavailable"}</p>

    {cell.population ? <EvaluationPopulationData population={cell.population} /> : <EmptyState
      title="Cell measurements unavailable"
      message="Missing, invalid, cancelled or aborted input does not supply a successful population or a measured zero."
    />}

    {cell.assignment.length ? <div className="evaluation-section">
      <h3>Fixed endpoint assignment</h3>
      <EvaluationFacts metrics={cell.assignment} />
    </div> : null}

    {cell.output_lengths.length ? <div className="evaluation-section">
      <h3>Reported output lengths</h3>
      <p className="evaluation-note">Usage coverage and output lengths are conditional diagnostics. They do not establish matched generated outputs.</p>
      <EvaluationFacts metrics={cell.output_lengths} />
    </div> : null}
  </div>;
}

function CacheResults({ detail }: { readonly detail: EvaluationCacheDetail; }) {
  const [blockIndex, setBlockIndex] = useState(detail.blocks[0]?.index ?? 0);
  const [condition, setCondition] = useState("S0");
  const block = detail.blocks.find((item) => item.index === blockIndex);
  const cell = block?.cells.find((item) => item.condition === condition);
  return <>

    <Panel className="evaluation-panel">
      <SectionHeading title="Four-cell comparison" meta="S0 / S1 / U0 / U1" />

      <p className="evaluation-note">Shared and unique document families are compared with declared cache off/on. The report supplies each on-minus-off contrast and their difference. Cache treatment remains unverified; token-prefix potential is not cache hits.</p>

      {detail.low_replication ? <p className="evaluation-note"><StatusBadge status="LOW_REPLICATION" label="Low-replication rehearsal / pilot" tone="warning" /> Fewer than eight complete eligible blocks do not supply an interval.</p> : null}

      <EvaluationContrasts contrasts={detail.contrasts} />

      {detail.preparation_issues.length ? <div className="evaluation-section">
        <h3>Preparation consistency issues</h3>
        <ul className="evaluation-note">
          {detail.preparation_issues.map((reason) => <li key={reason}>
            {humanize(reason)}
          </li>)}
        </ul>
      </div> : null}
    </Panel>

    <Panel className="evaluation-panel">
      <SectionHeading title="Cache blocks and cells" meta="Separate fixed-window populations" />

      {detail.blocks.length ? <>

        <div className="evaluation-controls">
          <label>Block<select aria-label="Block" value={blockIndex} onChange={(event) => { setBlockIndex(Number(event.target.value)); setCondition("S0"); }}>
            {detail.blocks.map((item) => <option key={item.index} value={item.index}>Block {item.index}</option>)}
          </select></label>
        </div>

        {block ? <>

          <EvaluationTable
            caption={`Block ${block.index}: all four cache conditions`}
            columns={["Condition", "Declared mode", "Status", "Planned offers", "Dispatched", "SLO good", "SLO fraction", "Goodput"]}
          >

            {block.cells.map((item) => <tr key={item.index}>

              <th scope="row" data-label="Condition">
                <span>{item.condition} · Cell {item.index}</span>
                <span className="table-subtext">{humanize(item.workload_family)} document</span>
              </th>

              <td data-label="Declared mode">
                <span>
                  {item.mode === "DECLARED_ENABLED" ? "Cache on (declared)" : "Cache off (declared)"}
                </span>
              </td>
              <td data-label="Status">
                <StatusBadge status={item.status} />
              </td>

              <td data-label="Planned offers">
                <EvaluationValue value={valueAt(item.metrics, "planned_offers")} />
              </td>

              <td data-label="Dispatched">
                <EvaluationValue value={item.population ? valueAt(item.population.metrics, "dispatched_count") : null} />
              </td>

              <td data-label="SLO good">
                <EvaluationValue value={item.population ? valueAt(item.population.metrics, "slo_good_count") : null} />
              </td>

              <td data-label="SLO fraction">
                <EvaluationValue value={item.population ? valueAt(item.population.metrics, "slo_success_fraction") : null} />
              </td>

              <td data-label="Goodput">
                <EvaluationValue value={item.population ? valueAt(item.population.metrics, "slo_goodput_rps") : null} unit="requests/s" />
              </td>
            </tr>)}
          </EvaluationTable>

          <div className="evaluation-controls">
            <label>Cell<select aria-label="Cell" value={condition} onChange={(event) => setCondition(event.target.value)}>
              {block.cells.map((item) => <option key={item.index} value={item.condition}>{item.condition} · Cell {item.index}</option>)}
            </select></label>
          </div>

          {cell ? <CacheCellDetail cell={cell} /> : null}

          <div className="evaluation-section">
            <h3>Potential matching prefix blocks</h3>
            <p className="evaluation-note">Workload verification reported: {humanize(detail.workload_verification)}. Tokenizer reverified here: no. Potential matching token-prefix blocks do not measure cache hits.</p>
            {block.prefix_potential.length ? <EvaluationFacts metrics={block.prefix_potential} /> : <p className="evaluation-note">Prefix potential unavailable.</p>}
          </div>

          {block.output_length_differences.length ? <div className="evaluation-section">
            <h3>Conditional output-length differences</h3>
            <EvaluationFacts metrics={block.output_length_differences} />
            <p className="evaluation-note">Successful request sets can differ. These values do not establish matched outputs.</p>
          </div> : null}
        </> : null}
      </> : <EmptyState title="Cache blocks unavailable" message="No validated block projection is available." />}

      <p className="evaluation-note">Recovery is not supplied by this report. Owned cell elapsed time excludes external preparation and does not establish whole-experiment wall time.</p>
    </Panel>
  </>;
}

function EvaluationDetailContent({ detail, refresh }: {
  readonly detail: EvaluationReportDetail;
  readonly refresh: () => void;
}) {
  return <>

    <PageHeader
      title={detail.summary.label}
      subtitle={detail.kind === "STUDY" ? "Four-policy study report with fixed-window populations and recovery observations." : "Shared / unique × declared cache off / on report."}
      action={<button className="button button-secondary" type="button" onClick={refresh}><RefreshCw aria-hidden="true" />Refresh report</button>}
    />

    <EvaluationTrust summary={detail.summary} />

    <Panel className="evaluation-panel">
      <SectionHeading title="Report status and coverage" />

      <dl className="evaluation-facts">
        <div>
          <dt>Execution status</dt>
          <dd>
            <StatusBadge status={detail.summary.status} />
          </dd>
        </div>
        <div>
          <dt>Comparison status</dt>
          <dd>
            {humanize(detail.summary.comparison_status)}
          </dd>
        </div>
        <div>
          <dt>Reason</dt>
          <dd>
            {detail.summary.reason ? humanize(detail.summary.reason) : "None reported"}
          </dd>
        </div>
        <div>
          <dt>Calibration</dt>
          <dd>
            {humanize(detail.summary.calibration)}
          </dd>
        </div>
      </dl>

      <p className="evaluation-note">Report completion and comparison availability are separate. Unavailable populations remain unavailable, including when other cells or trials completed.</p>

      <EvaluationFacts metrics={detail.coverage} />

      <div className="evaluation-section">
        <h3>Reporting boundaries</h3>
        <EvaluationFacts metrics={detail.reporting} />
      </div>
    </Panel>

    {detail.kind === "STUDY" ? <StudyResults detail={detail} /> : <CacheResults detail={detail} />}

    <Panel className="evaluation-panel">
      <SectionHeading title="Provenance and limitations" />
      <EvaluationProvenance summary={detail.summary} />

      <p className="evaluation-note">Descriptive, uncalibrated rehearsal only. This viewer establishes no GPU authenticity, causal effect, capacity, cost, acceptance or general performance improvement.</p>

      {detail.limitations.length ? <ul className="evaluation-note">
        {detail.limitations.map((item) => <li key={item}>
          {humanize(item)}
        </li>)}
      </ul> : null}
    </Panel>

    <Link className="button button-secondary" to="/evaluations"><ArrowLeft aria-hidden="true" />Back to Evaluations</Link>
  </>;
}

export function EvaluationDetailView() {
  const { reportId } = useParams();
  const request = useEvaluationDetail(reportId);
  const header = <PageHeader
    title="Evaluation report"
    subtitle="Read-only results from one pinned report."
    action={<button type="button" className="button button-secondary" onClick={request.retry} disabled={request.pendingRefresh} aria-busy={request.pendingRefresh}><RefreshCw aria-hidden="true" />Refresh report</button>}
  />;
  if (request.status === "loading") return <>
    {header}
    <LoadingState label="Reading pinned evaluation report…" />
  </>;
  if (request.status === "error" && request.error) return <>
    {header}
    <ErrorState error={request.error} retry={request.retry} title="This evaluation report could not be opened" />
    <Link className="button button-secondary" to="/evaluations">Back to Evaluations</Link>
  </>;
  return request.data ? <EvaluationDetailContent key={request.data.summary.report_id} detail={request.data} refresh={request.retry} /> : <>
    {header}
    <EmptyState title="No evaluation selected" message="Choose a pinned report from Evaluations." />
  </>;
}
