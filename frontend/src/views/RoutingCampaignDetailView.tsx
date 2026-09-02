import {
  ArrowLeft,
  CheckCircle2,
  Clock3,
  DatabaseZap,
  Route,
  ShieldCheck,
} from "lucide-react";

import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  Panel,
  SectionHeading,
  StatusBadge,
} from "../components/Primitives";
import { useRoutingCampaignDetail } from "../hooks/useRoutingCampaignDetail";
import { humanize, shortDigest } from "../lib/format";
import { Link, useParams } from "../lib/router";
import type {
  RoutingCampaignDetail,
  RoutingCampaignTrialView,
  RoutingCandidateView,
  RoutingTelemetryView,
} from "../lib/types";

function TelemetryFact({ telemetry }: { readonly telemetry: RoutingTelemetryView }) {
  return (
    <div className="routing-telemetry-fact">
      <strong>{telemetry.signal}</strong>
      <span>
        age <code>{telemetry.age_ms} ms</code> · epoch <code>{telemetry.epoch}</code> · observed <code>{telemetry.observed_at_ms} ms</code>
      </span>
      <span>
        bound <code>{telemetry.freshness_bound_ms} ms</code> · value <code>{String(telemetry.value)}</code>
      </span>
      <StatusBadge
        status={telemetry.admissibility}
        label={telemetry.admissibility === "ADMISSIBLE" ? "Admissible" : "Inadmissible"}
        tone={telemetry.admissibility === "ADMISSIBLE" ? "valid" : "warning"}
      />
    </div>
  );
}

function CandidateReceipt({ candidate }: { readonly candidate: RoutingCandidateView }) {
  return (
    <section className="routing-candidate-receipt" aria-label={`${candidate.endpoint_id} candidate`}>
      <div className="routing-candidate-heading">
        <strong>{candidate.endpoint_id}</strong>
        <StatusBadge
          status={candidate.eligible ? "ELIGIBLE" : "INELIGIBLE"}
          label={candidate.eligible ? "Eligible" : "Ineligible"}
          tone={candidate.eligible ? "valid" : "warning"}
        />
      </div>
      <TelemetryFact telemetry={candidate.health} />
      <TelemetryFact telemetry={candidate.load} />
      <TelemetryFact telemetry={candidate.kv} />
    </section>
  );
}

function ClaimList({ label, values }: { readonly label: string; readonly values: readonly string[] }) {
  return (
    <div className="routing-claim-list">
      <span>{label}</span>
      {values.length ? values.map((value) => <code key={value}>{value}</code>) : <em>None</em>}
    </div>
  );
}

function ResetReceipt({ trial }: { readonly trial: RoutingCampaignTrialView }) {
  return (
    <Panel className="routing-reset-panel">
      <SectionHeading title="Cold reset receipt" meta={`virtual time ${trial.reset.virtual_time_ms} ms`} />
      <div className="routing-reset-grid">
        <div>
          <span>Endpoint instances</span>
          <ul>
            {trial.reset.endpoint_instances.map((endpoint) => (
              <li key={endpoint.endpoint_id}>
                <code>{endpoint.endpoint_id}</code>
                <code title={endpoint.instance_id}>{endpoint.instance_id}</code>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <span>Observer epochs</span>
          <ul>
            {trial.reset.observer_epochs.map((observer) => (
              <li key={observer.observer_id}>
                <code>{observer.observer_id}</code>
                <code>epoch {observer.epoch}</code>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <span>Cleared state</span>
          <ul>
            <li><CheckCircle2 aria-hidden="true" /> Queue cleared</li>
            <li><CheckCircle2 aria-hidden="true" /> Load state cleared</li>
            <li><CheckCircle2 aria-hidden="true" /> KV state cleared</li>
          </ul>
        </div>
      </div>
    </Panel>
  );
}

function TrialReceipts({ trial }: { readonly trial: RoutingCampaignTrialView }) {
  return (
    <section className="routing-trial-section" aria-labelledby={`${trial.trial_id}-heading`}>
      <div className="routing-trial-heading">
        <div>
          <h2 id={`${trial.trial_id}-heading`}>{trial.policy_id}</h2>
          <span className="mono">{trial.trial_id}</span>
        </div>
        <StatusBadge status="RECORDED" label="Recorded receipts" tone="info" />
      </div>
      <ResetReceipt trial={trial} />
      <Panel className="routing-requests-panel">
        <SectionHeading
          title="Per-request routing receipts"
          headingId={`${trial.trial_id}-requests-heading`}
          meta={`${trial.requests.length} decisions · ${trial.terminal_population_total} terminals`}
        />
        <div className="table-scroll routing-requests-table-wrap">
          <table className="routing-requests-table" aria-labelledby={`${trial.trial_id}-requests-heading`}>
            <caption>Candidate state, decision, and terminal receipt for every planned request</caption>
            <thead>
              <tr>
                <th scope="col">Request / virtual time</th>
                <th scope="col">Candidate telemetry and admissibility</th>
                <th scope="col">Selection and fallback</th>
                <th scope="col">Terminal outcome</th>
              </tr>
            </thead>
            <tbody>
              {trial.requests.map((request) => (
                <tr key={request.decision_id}>
                  <th scope="row">
                    <code>{request.request_id}</code>
                    <span className="table-subtext">sequence {request.sequence_index} · <code>{request.decision_time_ms} ms</code></span>
                    <span className="table-subtext mono" title={request.decision_id}>{request.decision_id}</span>
                  </th>
                  <td>
                    <div className="routing-candidate-list">
                      {request.candidates.map((candidate) => <CandidateReceipt key={candidate.endpoint_id} candidate={candidate} />)}
                    </div>
                  </td>
                  <td>
                    <dl className="routing-decision-facts">
                      <div><dt>Selected endpoint</dt><dd><code>{request.selected_endpoint_id ?? "none"}</code></dd></div>
                      <div><dt>Fallback reason</dt><dd><code>{request.fallback_reason}</code></dd></div>
                    </dl>
                    <ClaimList label="Claims used" values={request.claims_used} />
                    <ClaimList label="Stale claims permitted" values={request.claims_permitted_stale} />
                    <ClaimList label="Claims discarded" values={request.claims_discarded} />
                  </td>
                  <td>
                    <StatusBadge status={request.terminal.status} label={humanize(request.terminal.status)} />
                    <span className="table-subtext"><code>{request.terminal.reason}</code></span>
                    <span className="table-subtext">{request.terminal.started_at_ms}–{request.terminal.ended_at_ms} ms</span>
                    <span className="table-subtext mono" title={request.terminal.terminal_outcome_id}>{request.terminal.terminal_outcome_id}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel className="routing-terminal-panel">
        <SectionHeading title="Complete terminal population" meta={`${trial.terminal_population_total} of ${trial.requests.length} planned requests`} />
        <div className="table-scroll routing-terminal-table-wrap">
          <table className="routing-terminal-table">
            <caption>Separate terminal outcome counts for this cold policy trial</caption>
            <thead>
              <tr>
                <th scope="col">Terminal status</th>
                <th scope="col" className="number-cell">Count</th>
              </tr>
            </thead>
            <tbody>
              {trial.terminal_population.map((population) => (
                <tr key={population.status}>
                  <th scope="row"><StatusBadge status={population.status} label={humanize(population.status)} /></th>
                  <td className="number-cell mono">{population.count}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr><th scope="row">Total</th><td className="number-cell mono">{trial.terminal_population_total}</td></tr>
            </tfoot>
          </table>
        </div>
      </Panel>
    </section>
  );
}

function RoutingCampaignDetailContent({ detail }: { readonly detail: RoutingCampaignDetail }) {
  return (
    <>
      <PageHeader
        title={detail.summary.campaign_id}
        subtitle="Sealed synthetic CPU campaign receipts, rendered only after independent replay."
        action={<StatusBadge status="VERIFIED" label="Verified by replay" tone="valid" />}
      />
      <div className="routing-detail-boundary" role="note">
        <ShieldCheck aria-hidden="true" />
        <div>
          <strong><code>{detail.interpretation_boundary}</code></strong>
          <p>
            This projection records observed campaign evidence. It does not recommend a policy or issue an acceptance verdict.
          </p>
        </div>
      </div>
      <div className="routing-overview-grid">
        <Panel>
          <SectionHeading title="Verified package identity" />
          <dl className="routing-fact-list">
            <div><dt>Retained digest</dt><dd className="mono" title={detail.summary.retained_digest}>{shortDigest(detail.summary.retained_digest)}</dd></div>
            <div><dt>Execution mode</dt><dd><code>{detail.summary.execution_mode}</code></dd></div>
            <div><dt>Cold trials</dt><dd className="mono">{detail.summary.trial_count}</dd></div>
            <div><dt>Planned requests</dt><dd className="mono">{detail.summary.planned_request_count}</dd></div>
          </dl>
        </Panel>
        <Panel>
          <SectionHeading title="Fault timeline" meta="Virtual milliseconds" />
          <div className="routing-fault-timeline">
            <div><Clock3 aria-hidden="true" /><span>Load collection paused</span><strong><code>{detail.fault_timeline.load_collection_paused_at_ms} ms</code></strong></div>
            <div><Route aria-hidden="true" /><span>Health collection</span><strong>Continued</strong></div>
            <div><DatabaseZap aria-hidden="true" /><span>Freshness bounds</span><strong><code>load {detail.fault_timeline.load_freshness_bound_ms} ms · health {detail.fault_timeline.health_freshness_bound_ms} ms</code></strong></div>
          </div>
        </Panel>
      </div>
      <div className="routing-policy-summary">
        <span>Recorded policy order</span>
        {detail.summary.policy_ids.map((policy) => <code key={policy}>{policy}</code>)}
      </div>
      {detail.trials.map((trial) => <TrialReceipts key={trial.trial_id} trial={trial} />)}
      <div className="view-cta routing-back-link">
        <div>
          <strong>Need another verified campaign?</strong>
          <span>Return to the bounded campaign index; withheld packages remain non-rendered.</span>
        </div>
        <Link className="button button-secondary" to="/routing-campaigns">
          <ArrowLeft aria-hidden="true" />
          Routing campaigns
        </Link>
      </div>
    </>
  );
}

export function RoutingCampaignDetailView() {
  const { campaignId } = useParams();
  const request = useRoutingCampaignDetail(campaignId);

  if (!campaignId) {
    return (
      <>
        <PageHeader title="Routing campaign" subtitle="Inspect one verified sealed routing campaign." />
        <EmptyState title="No routing campaign selected" message="Choose a verified campaign from the campaign index." />
      </>
    );
  }
  if (request.status === "loading") {
    return (
      <>
        <PageHeader title="Routing campaign" subtitle="Verifying the sealed campaign before projection." />
        <LoadingState label="Verifying routing receipts and independent replay…" />
      </>
    );
  }
  if (request.status === "error" && request.error) {
    return (
      <>
        <PageHeader title="Routing campaign" subtitle="Inspect one verified sealed routing campaign." />
        <ErrorState error={request.error} retry={request.retry} title="This routing campaign could not be opened" />
      </>
    );
  }
  return request.data ? <RoutingCampaignDetailContent detail={request.data} /> : null;
}
