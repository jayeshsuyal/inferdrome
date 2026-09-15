import { ArrowLeft, RefreshCw } from "lucide-react";
import { useState } from "react";

import {
  EvaluationContrasts, EvaluationDisclosure, EvaluationFacts, EvaluationPopulationData,
  EvaluationProvenance, EvaluationRecoveryData, EvaluationTable, EvaluationTrust,
  EvaluationValue, evaluationPopulationCue, evaluationRecoveryCue,
} from "../components/EvaluationData";
import { EmptyState, ErrorState, LoadingState, PageHeader, Panel, SectionHeading, StatusBadge } from "../components/Primitives";
import { useEvaluationDetail } from "../hooks/useEvaluations";
import { evaluationLabel } from "../lib/evaluation-display";
import type { EvaluationCacheDetail, EvaluationCell, EvaluationMetric, EvaluationReportDetail, EvaluationStudyDetail } from "../lib/evaluations";
import { Link, useParams } from "../lib/router";

const valueAt = (metrics: readonly EvaluationMetric[], key: string) => metrics.find((item) => item.key === key)?.value ?? null;

function StudyResults({ detail }: { readonly detail: EvaluationStudyDetail }) {
  const [trialIndex, setTrialIndex] = useState(detail.trials[0]?.index ?? 0);
  const [population, setPopulation] = useState("foreground");
  const trial = detail.trials.find((item) => item.index === trialIndex);
  const selectedPopulation = trial ? population === "foreground" ? trial.foreground : trial.background : null;
  const trialCue = trial
    ? [evaluationLabel(trial.status), evaluationPopulationCue(selectedPopulation), evaluationRecoveryCue(trial.recovery)].filter(Boolean).join(" · ")
    : "No returned trial measurements";

  return <>
    {detail.strata.map((stratum) => <Panel className="evaluation-panel" key={stratum.index}>
      <SectionHeading title="Four study policies" meta={`Stratum ${stratum.index} · ${evaluationLabel(stratum.scenario)} · Profile ${stratum.profile_index} · Target ${stratum.target_endpoint ?? "not specified"}`} />
      <EvaluationFacts metrics={stratum.metrics} />
      <p className="evaluation-note">Per-trial goodput summaries. Returned trials and completed trials are separate counts.</p>
      <EvaluationTable
        caption="Four evaluation policies and reported goodput summaries"
        columns={["Policy", "Returned trials", "Completed trials", "Mean SLO goodput", "Observed minimum", "Observed maximum"]}
      >
        {stratum.policies.map((policy) => <tr key={policy.policy_id}>
          <th scope="row" data-label="Policy"><span>{evaluationLabel(policy.policy_id)}</span></th>
          <td data-label="Returned trials"><EvaluationValue value={valueAt(policy.metrics, "returned_trials")} /></td>
          <td data-label="Completed trials"><EvaluationValue value={valueAt(policy.metrics, "completed_trials")} /></td>
          <td data-label="Mean SLO goodput"><EvaluationValue value={valueAt(policy.metrics, "mean_goodput_rps")} unit="requests/s" /></td>
          <td data-label="Observed minimum"><EvaluationValue value={valueAt(policy.metrics, "observed_min_goodput_rps")} unit="requests/s" /></td>
          <td data-label="Observed maximum"><EvaluationValue value={valueAt(policy.metrics, "observed_max_goodput_rps")} unit="requests/s" /></td>
        </tr>)}
      </EvaluationTable>
      <EvaluationDisclosure title="Exact policy values" cue="Policy IDs and source precision">
        <p className="evaluation-note">Stratum {stratum.index} · Profile {stratum.profile_index} · Target {stratum.target_endpoint ?? "not specified"} · <code>{stratum.scenario}</code></p>
        <EvaluationFacts metrics={stratum.metrics} exact />
        {stratum.policies.map((policy) => <div className="evaluation-section" key={policy.policy_id}>
          <h3>{evaluationLabel(policy.policy_id)}</h3>
          <p><code className="evaluation-digest">{policy.policy_id}</code></p>
          <EvaluationFacts metrics={policy.metrics} exact />
        </div>)}
      </EvaluationDisclosure>
      <div className="evaluation-section">
        <h3>Paired policy contrasts</h3>
        <EvaluationContrasts contrasts={stratum.contrasts} />
      </div>
    </Panel>)}

    <EvaluationDisclosure title="Trial measurements" cue={trialCue} className="evaluation-major-disclosure">
      {detail.trials.length ? <>
        <div className="evaluation-controls">
          <label>Trial<select aria-label="Trial" value={trialIndex} onChange={(event) => { setTrialIndex(Number(event.target.value)); setPopulation("foreground"); }}>
            {detail.trials.map((item) => <option key={item.index} value={item.index}>Trial {item.index} · Block {item.block_index} · {evaluationLabel(item.policy_id)} · {evaluationLabel(item.status)}</option>)}
          </select></label>
          <label>Population<select aria-label="Population" value={population} onChange={(event) => setPopulation(event.target.value)}>
            <option value="foreground">Foreground</option><option value="background">Background</option>
          </select></label>
        </div>
        {trial ? <>
          <div className="evaluation-badges">
            <StatusBadge status={trial.status} label={evaluationLabel(trial.status)} />
            <span>{evaluationLabel(trial.policy_id)} · Block {trial.block_index} · Profile {trial.profile_index}</span>
          </div>
          <div className="evaluation-section">
            <h3>{population === "foreground" ? "Foreground population" : "Background population"}</h3>
            <p className="evaluation-note">{population === "background"
              ? "Background requests do not inflate foreground success. Only the background statistics supplied by the report are shown."
              : "All offered foreground requests remain in the SLO denominator. Goodput uses the fixed offered window; completions during drain remain separate."}</p>
            {selectedPopulation ? <EvaluationPopulationData population={selectedPopulation} /> : <EmptyState title="Background measurements unavailable" message="This trial report has no background population; unavailable values are not zero measurements." />}
          </div>
          <EvaluationRecoveryData recovery={trial.recovery} />
          <EvaluationDisclosure title="Trial details" cue="Exact policy, result digest and reporting boundaries">
            <p>Policy: <code className="evaluation-digest">{trial.policy_id}</code></p>
            <p>Result digest: <code className="evaluation-digest">{trial.result_sha256}</code></p>
            <p>Status: <code>{trial.status}</code> · Scenario: <code>{trial.scenario}</code></p>
            <EvaluationFacts metrics={trial.metrics} exact />
          </EvaluationDisclosure>
        </> : null}
      </> : <EmptyState title="No returned trial measurements" message="Planned, aborted and not-run coverage remains in the report. Missing measurements do not become successful requests or measured zeros." />}
    </EvaluationDisclosure>
  </>;
}

function CacheCellDetail({ cell }: { readonly cell: EvaluationCell }) {
  return <>
    <h3>{cell.condition} · Cell {cell.index} details</h3>
    <div className="evaluation-badges">
      <StatusBadge status={cell.status} label={evaluationLabel(cell.status)} />
      <StatusBadge status={cell.declarations_consistent ? "CONSISTENT" : "UNVERIFIED"}
        label={cell.declarations_consistent ? "Declarations consistent" : "Declarations incomplete or inconsistent"}
        tone={cell.declarations_consistent ? "neutral" : "warning"} />
    </div>
    <p className="evaluation-note">Reason: {evaluationLabel(cell.reason)}. Cleanup: {evaluationLabel(cell.cleanup)}. Declared preparation does not verify cache treatment.</p>
    {cell.declaration_reasons.length ? <ul className="evaluation-note">{cell.declaration_reasons.map((reason) => <li key={reason}>{evaluationLabel(reason)}</li>)}</ul> : null}
    {cell.population ? <EvaluationPopulationData population={cell.population} /> : <EmptyState title="Cell measurements unavailable" message="Missing, invalid, cancelled or aborted input does not supply a successful population or a measured zero." />}
    <EvaluationDisclosure title="Cell details" cue="Full digests, assignment and conditional output lengths">
      <p>Config digest: <code className="evaluation-digest">{cell.config_sha256}</code></p>
      <p>Result digest: <code className="evaluation-digest">{cell.result_sha256 ?? "Unavailable"}</code></p>
      <p>Source status: <code>{cell.status}</code> · Reason: <code>{cell.reason}</code> · Cleanup: <code>{cell.cleanup}</code></p>
      <p>Mode: <code>{cell.mode}</code> · Family: <code>{cell.workload_family}</code></p>
      {cell.declaration_reasons.length ? <p>Source declaration reasons: {cell.declaration_reasons.map((reason) => <code key={reason}>{reason} </code>)}</p> : null}
      <EvaluationFacts metrics={cell.metrics} exact />
      {cell.assignment.length ? <div className="evaluation-section"><h3>Fixed endpoint assignment</h3><EvaluationFacts metrics={cell.assignment} exact /></div> : null}
      {cell.output_lengths.length ? <div className="evaluation-section"><h3>Reported output lengths</h3><p className="evaluation-note">Conditional usage diagnostics do not establish matched generated outputs.</p><EvaluationFacts metrics={cell.output_lengths} exact /></div> : null}
    </EvaluationDisclosure>
  </>;
}

function CacheResults({ detail }: { readonly detail: EvaluationCacheDetail }) {
  const [blockIndex, setBlockIndex] = useState(detail.blocks[0]?.index ?? 0);
  const [condition, setCondition] = useState("S0");
  const block = detail.blocks.find((item) => item.index === blockIndex);
  const cell = block?.cells.find((item) => item.condition === condition);
  const cellCue = cell
    ? `${cell.condition} · ${evaluationLabel(cell.status)} · ${evaluationLabel(cell.reason)} · ${evaluationPopulationCue(cell.population)}`
    : "Measurements unavailable";
  return <>
    <Panel className="evaluation-panel">
      <SectionHeading title="Four-cell comparison" meta="S0 / S1 / U0 / U1" />
      <p className="evaluation-note">Shared and unique documents, declared cache off/on. Cache treatment remains unverified; token-prefix potential is not cache hits.</p>
      {detail.low_replication ? <p className="evaluation-note"><StatusBadge status="LOW_REPLICATION" label="Low-replication rehearsal / pilot" tone="warning" /> Fewer than eight complete eligible blocks do not supply an interval.</p> : null}
      <EvaluationContrasts contrasts={detail.contrasts} />
      {detail.preparation_issues.length ? <div className="evaluation-section"><h3>Preparation consistency issues</h3><ul className="evaluation-note">{detail.preparation_issues.map((reason) => <li key={reason}>{evaluationLabel(reason)}</li>)}</ul></div> : null}
    </Panel>
    <Panel className="evaluation-panel">
      <SectionHeading title="Cache blocks and cells" meta="Separate fixed-window populations" />
      {block ? <>
        <div className="evaluation-controls"><label>Block<select aria-label="Block" value={blockIndex} onChange={(event) => { setBlockIndex(Number(event.target.value)); setCondition("S0"); }}>
          {detail.blocks.map((item) => <option key={item.index} value={item.index}>Block {item.index}</option>)}
        </select></label></div>
        <EvaluationTable caption={`Block ${block.index}: all four cache conditions`} columns={["Condition", "Declared mode", "Status", "Planned offers", "Dispatched", "SLO good", "SLO fraction", "Goodput"]}>
          {block.cells.map((item) => <tr key={item.index}>
            <th scope="row" data-label="Condition"><span>{item.condition} · Cell {item.index}</span><span className="table-subtext">{evaluationLabel(item.workload_family)} document</span></th>
            <td data-label="Declared mode"><span>{item.mode === "DECLARED_ENABLED" ? "Cache on (declared)" : "Cache off (declared)"}</span></td>
            <td data-label="Status"><StatusBadge status={item.status} label={evaluationLabel(item.status)} /></td>
            <td data-label="Planned offers"><EvaluationValue value={valueAt(item.metrics, "planned_offers")} /></td>
            <td data-label="Dispatched"><EvaluationValue value={item.population ? valueAt(item.population.metrics, "dispatched_count") : null} /></td>
            <td data-label="SLO good"><EvaluationValue value={item.population ? valueAt(item.population.metrics, "slo_good_count") : null} /></td>
            <td data-label="SLO fraction"><EvaluationValue value={item.population ? valueAt(item.population.metrics, "slo_success_fraction") : null} unit="ratio" /></td>
            <td data-label="Goodput"><EvaluationValue value={item.population ? valueAt(item.population.metrics, "slo_goodput_rps") : null} unit="requests/s" /></td>
          </tr>)}
        </EvaluationTable>
        <EvaluationDisclosure title="Cell measurements" cue={cellCue}>
          <div className="evaluation-controls"><label>Cell<select aria-label="Cell" value={condition} onChange={(event) => setCondition(event.target.value)}>
            {block.cells.map((item) => <option key={item.index} value={item.condition}>{item.condition} · Cell {item.index} · {evaluationLabel(item.status)}</option>)}
          </select></label></div>
          {cell ? <CacheCellDetail cell={cell} /> : null}
        </EvaluationDisclosure>
        <EvaluationDisclosure title="Workload and output diagnostics" cue={`Reported: ${evaluationLabel(detail.workload_verification)} · Tokenizer not reverified here`}>
          <h3>Potential matching prefix blocks</h3>
          <p>Source workload verification: <code>{detail.workload_verification}</code></p>
          <p className="evaluation-note">Potential matching token-prefix blocks do not measure cache hits. Workload status is reported by the source; tokenizer reverified here: no.</p>
          {block.prefix_potential.length ? <EvaluationFacts metrics={block.prefix_potential} exact /> : <p className="evaluation-note">Prefix potential unavailable.</p>}
          {block.output_length_differences.length ? <div className="evaluation-section"><h3>Conditional output-length differences</h3><EvaluationFacts metrics={block.output_length_differences} exact /><p className="evaluation-note">Successful request sets can differ. These values do not establish matched outputs.</p></div> : null}
        </EvaluationDisclosure>
      </> : <EmptyState title="Cache blocks unavailable" message="No validated block projection is available." />}
      <p className="evaluation-note">Recovery is not supplied. Owned cell elapsed time excludes external preparation and does not establish whole-experiment wall time.</p>
    </Panel>
  </>;
}

function EvaluationDetailContent({ detail, refresh }: {
  readonly detail: EvaluationReportDetail;
  readonly refresh: () => void;
}) {
  const overviewKeys = detail.kind === "STUDY"
    ? ["planned_trials", "returned_trials", "foreground_planned_offers", "foreground_returned_records"]
    : ["planned_blocks", "complete_blocks", "planned_cells", "completed_cells", "planned_offers", "returned_records"];
  return <>
    <PageHeader title={detail.summary.label}
      subtitle={detail.kind === "STUDY" ? "Four-policy study · fixed-window populations and recovery observations." : "Shared / unique documents × declared cache off / on."}
      action={<button className="button button-secondary" type="button" onClick={refresh}><RefreshCw aria-hidden="true" />Refresh report</button>} />
    <EvaluationTrust summary={detail.summary} />
    <Panel className="evaluation-panel">
      <SectionHeading title="Report overview" />
      <dl className="evaluation-facts evaluation-overview-status">
        <div><dt>Reported completion</dt><dd><StatusBadge status={detail.summary.status} label={evaluationLabel(detail.summary.status)} /></dd></div>
        <div><dt>Comparison availability</dt><dd>{evaluationLabel(detail.summary.comparison_status)}</dd></div>
        {detail.summary.reason ? <div><dt>Reported reason</dt><dd>{evaluationLabel(detail.summary.reason)}</dd></div> : null}
      </dl>
      <p className="evaluation-note">Completion and comparison availability are separate. Returned records are not a success count.</p>
      <EvaluationFacts metrics={detail.coverage.filter((item) => overviewKeys.includes(item.key))} />
      <EvaluationDisclosure title="Coverage and reporting details" cue="All planned, returned and missing populations; exact source values">
        <EvaluationFacts metrics={detail.coverage} exact />
        <div className="evaluation-section"><h3>Reporting boundaries</h3><EvaluationFacts metrics={detail.reporting} exact /></div>
        <p className="evaluation-note">Calibration: {evaluationLabel(detail.summary.calibration)}. Counts with no measurements remain unavailable where the report supplies no value.</p>
      </EvaluationDisclosure>
    </Panel>
    {detail.kind === "STUDY" ? <StudyResults detail={detail} /> : <CacheResults detail={detail} />}
    <EvaluationDisclosure title="Provenance and report details" cue="Full identities, source statuses and limitations" className="evaluation-major-disclosure">
      <EvaluationProvenance summary={detail.summary} />
      {detail.kind === "PREFIX_CACHE" ? <p>Source cache treatment attribution: <code>{detail.cache_treatment_attribution}</code></p> : null}
      <p className="evaluation-note">Descriptive, uncalibrated rehearsal only. This viewer establishes no GPU authenticity, causal effect, capacity, cost, acceptance or general performance improvement.</p>
      {detail.kind === "PREFIX_CACHE" && detail.preparation_issues.length ? <p>Source preparation issues: {detail.preparation_issues.map((item) => <code key={item}>{item} </code>)}</p> : null}
      {detail.limitations.length ? <ul className="evaluation-note">{detail.limitations.map((item) => <li key={item}>{evaluationLabel(item)} <code>{item}</code></li>)}</ul> : null}
    </EvaluationDisclosure>
    <Link className="button button-secondary" to="/evaluations"><ArrowLeft aria-hidden="true" />Back to Evaluations</Link>
  </>;
}

export function EvaluationDetailView() {
  const { reportId } = useParams();
  const request = useEvaluationDetail(reportId);
  const header = <PageHeader title="Evaluation report" subtitle="Read-only results from one pinned report."
    action={<button type="button" className="button button-secondary" onClick={request.retry} disabled={request.pendingRefresh} aria-busy={request.pendingRefresh}><RefreshCw aria-hidden="true" />Refresh report</button>} />;
  if (request.status === "loading") return <>{header}<LoadingState label="Reading pinned evaluation report…" /></>;
  if (request.status === "error" && request.error) return <>
    {header}<ErrorState error={request.error} retry={request.retry} title="This evaluation report could not be opened" />
    <Link className="button button-secondary" to="/evaluations">Back to Evaluations</Link>
  </>;
  return request.data ? <EvaluationDetailContent key={request.data.summary.report_id} detail={request.data} refresh={request.retry} />
    : <>{header}<EmptyState title="No evaluation selected" message="Choose a pinned report from Evaluations." /></>;
}
