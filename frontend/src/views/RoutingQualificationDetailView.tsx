import {
  ArrowLeft,
  CheckCircle2,
  Clock3,
  DatabaseZap,
  Download,
  GitFork,
  Route,
  ShieldCheck,
} from "lucide-react";
import { useState } from "react";

import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  Panel,
  SectionHeading,
  StatusBadge,
} from "../components/Primitives";
import { useRoutingQualificationDetail } from "../hooks/useRoutingQualificationDetail";
import { api } from "../lib/api";
import { humanize, shortDigest } from "../lib/format";
import { Link, useParams } from "../lib/router";
import type {
  RoutingQualificationDetail,
  RoutingQualificationTrialView,
} from "../lib/types";

function CandidateState({ trial }: { readonly trial: RoutingQualificationTrialView }) {
  return (
    <div className="qualification-candidate-state" aria-label={`Candidate state for ${trial.policy_id}`}>
      {trial.focal_endpoint_states.map((state) => (
        <div key={state.endpoint_id}>
          <strong><code>{state.endpoint_id}</code></strong>
          <span>health <code>{state.health_age_ms} ms</code> · epoch <code>{state.health_epoch}</code> · <b>admissible</b></span>
          <span>load <code>{state.load_age_ms} ms</code> · epoch <code>{state.load_epoch}</code> · <b>inadmissible</b></span>
        </div>
      ))}
    </div>
  );
}

function PopulationTable({ detail }: { readonly detail: RoutingQualificationDetail }) {
  const statuses = detail.trials[0].terminal_population.map((entry) => entry.status);
  return (
    <Panel className="qualification-populations-panel">
      <SectionHeading
        title="Separate terminal populations"
        meta="Six requests per cold trial; never pooled across modes"
      />
      <div className="table-scroll qualification-populations-table-wrap">
        <table className="qualification-populations-table">
          <caption>Complete non-pooled terminal populations for each declared mode</caption>
          <thead>
            <tr>
              <th scope="col">Policy mode</th>
              {statuses.map((status) => <th scope="col" key={status}>{humanize(status)}</th>)}
              <th scope="col">Total</th>
            </tr>
          </thead>
          <tbody>
            {detail.trials.map((trial) => (
              <tr key={trial.trial_id}>
                <th scope="row"><code>{trial.policy_id}</code></th>
                {trial.terminal_population.map((entry) => <td key={entry.status} className="number-cell mono">{entry.count}</td>)}
                <td className="number-cell mono">{trial.terminal_population_total}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function ColdResetTable({ detail }: { readonly detail: RoutingQualificationDetail }) {
  return (
    <Panel className="qualification-resets-panel">
      <SectionHeading title="Cold-reset receipts" meta="One trial-scoped reset for each declared mode" />
      <div className="table-scroll qualification-resets-table-wrap">
        <table className="qualification-resets-table">
          <caption>Reset identities and cleared state used before every six-request trial</caption>
          <thead>
            <tr>
              <th scope="col">Policy mode</th>
              <th scope="col">Endpoint instances</th>
              <th scope="col">Observer epochs</th>
              <th scope="col">Cleared state</th>
            </tr>
          </thead>
          <tbody>
            {detail.trials.map((trial) => (
              <tr key={trial.trial_id}>
                <th scope="row"><code>{trial.policy_id}</code></th>
                <td>
                  <span className="table-subtext mono" title={trial.reset.endpoint_a_instance_id}>endpoint-a · {shortDigest(trial.reset.endpoint_a_instance_id)}</span>
                  <span className="table-subtext mono" title={trial.reset.endpoint_b_instance_id}>endpoint-b · {shortDigest(trial.reset.endpoint_b_instance_id)}</span>
                </td>
                <td className="mono">{trial.reset.observer_epochs.map((epoch) => `epoch ${epoch}`).join(" · ")}</td>
                <td><CheckCircle2 aria-hidden="true" /> Queue, load, and KV cleared</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function CausalComparison({ detail }: { readonly detail: RoutingQualificationDetail }) {
  return (
    <Panel className="qualification-comparison-panel">
      <SectionHeading
        title="What the router knew at the focal request"
        meta={`request-002 · virtual time ${detail.fault_timeline.focal_decision_time_ms} ms`}
      />
      <div className="table-scroll qualification-comparison-table-wrap">
        <table className="qualification-comparison-table">
          <caption>Candidate state, selection reason, and terminal receipt for the same stale-load/fresh-health observation</caption>
          <thead>
            <tr>
              <th scope="col">What changed</th>
              <th scope="col">Declared policy mode</th>
              <th scope="col">Candidate state</th>
              <th scope="col">Selection / fallback</th>
              <th scope="col">Observed terminal outcome</th>
            </tr>
          </thead>
          <tbody>
            {detail.trials.map((trial, index) => (
              <tr key={trial.trial_id}>
                <th scope="row">
                  {index === 0 ? (
                    <span className="qualification-change-copy">
                      <strong>Load observation paused at <code>{detail.fault_timeline.load_observer_pause_at_ms} ms</code></strong>
                      <span>Health continued. At 20 ms: health age 0 ms; load age 10 ms; both share a 5 ms bound.</span>
                    </span>
                  ) : <span className="table-subtext">Same fixed fault and trace</span>}
                </th>
                <td><code>{trial.policy_id}</code></td>
                <td><CandidateState trial={trial} /></td>
                <td>
                  <dl className="routing-decision-facts">
                    <div><dt>Selected</dt><dd><code>{trial.selected_endpoint_id ?? "none"}</code></dd></div>
                    <div><dt>Reason</dt><dd><code>{trial.fallback_reason}</code></dd></div>
                  </dl>
                </td>
                <td>
                  <StatusBadge status={trial.terminal_status} label={humanize(trial.terminal_status)} />
                  <span className="table-subtext"><code>{trial.terminal_reason}</code></span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function RoutingQualificationDetailContent({ detail }: { readonly detail: RoutingQualificationDetail }) {
  const [downloadState, setDownloadState] = useState<"idle" | "working" | "complete" | "error">("idle");
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const download = async () => {
    setDownloadState("working");
    setDownloadError(null);
    try {
      await api.downloadRoutingQualification(
        detail.summary.qualification_id,
        detail.summary.retained_digest,
      );
      setDownloadState("complete");
    } catch (error) {
      setDownloadState("error");
      setDownloadError(error instanceof Error ? error.message : "The descriptor download failed.");
    }
  };
  return (
    <>
      <PageHeader
        title={detail.summary.qualification_id}
        subtitle="The causal cross-policy view is rendered only after source replay and descriptor rebinding."
        action={<StatusBadge status="VERIFIED" label="Replay + binding verified" tone="valid" />}
      />
      <div className="routing-detail-boundary qualification-boundary" role="note">
        <ShieldCheck aria-hidden="true" />
        <div>
          <strong><code>{detail.interpretation_boundary}</code></strong>
          <p>This records a fixed local experiment. It does not identify a winner, recommend a policy, or issue an acceptance verdict.</p>
        </div>
      </div>
      <div className="qualification-overview-grid">
        <Panel>
          <SectionHeading title="Integrity and replay state" />
          <dl className="routing-fact-list">
            <div><dt>Source package</dt><dd className="mono" title={detail.summary.source_package_retained_digest}>{shortDigest(detail.summary.source_package_retained_digest)}</dd></div>
            <div><dt>Qualification descriptor</dt><dd className="mono" title={detail.summary.retained_digest}>{shortDigest(detail.summary.retained_digest)}</dd></div>
            <div><dt>Source replay</dt><dd><StatusBadge status="VERIFIED" label="Independently replayed" tone="valid" /></dd></div>
            <div><dt>Descriptor binding</dt><dd><StatusBadge status="VERIFIED" label="Canonical digest matched" tone="valid" /></dd></div>
          </dl>
        </Panel>
        <Panel>
          <SectionHeading title="Fault timeline" meta="Virtual milliseconds" />
          <div className="routing-fault-timeline">
            <div><Clock3 aria-hidden="true" /><span>Load collection paused</span><strong><code>{detail.fault_timeline.load_observer_pause_at_ms} ms</code></strong></div>
            <div><Route aria-hidden="true" /><span>Health collection</span><strong>Continued</strong></div>
            <div><DatabaseZap aria-hidden="true" /><span>Focal observation</span><strong><code>health {detail.fault_timeline.health_age_ms} ms · load {detail.fault_timeline.load_age_ms} ms</code></strong></div>
          </div>
        </Panel>
      </div>
      <CausalComparison detail={detail} />
      <PopulationTable detail={detail} />
      <ColdResetTable detail={detail} />
      <Panel className="qualification-evidence-panel">
        <SectionHeading title="Evidence and full receipts" meta="Bounded read-only actions" />
        <div className="qualification-evidence-actions">
          <div>
            <GitFork aria-hidden="true" />
            <div>
              <strong>Full request-level receipts</strong>
              <span>Inspect every observation, decision, fallback, and terminal receipt in the verified source campaign.</span>
            </div>
          </div>
          <Link className="button button-secondary" to={detail.source_receipts_path}>
            <Route aria-hidden="true" />
            Open source receipts
          </Link>
        </div>
        <div className="qualification-evidence-actions">
          <div>
            <Download aria-hidden="true" />
            <div>
              <strong>Canonical qualification descriptor</strong>
              <span>Download the fixed-size descriptor that bound this verified source package and non-pooled populations.</span>
            </div>
          </div>
          <button className="button button-secondary" type="button" onClick={() => { void download(); }} disabled={downloadState === "working"}>
            <Download aria-hidden="true" />
            {downloadState === "working" ? "Preparing download…" : "Download verified descriptor"}
          </button>
        </div>
        {downloadState === "complete" ? <p className="qualification-download-state" role="status">Verified descriptor download started.</p> : null}
        {downloadState === "error" && downloadError ? <p className="qualification-download-state qualification-download-error" role="alert">{downloadError}</p> : null}
      </Panel>
      <div className="view-cta routing-back-link">
        <div>
          <strong>Inspect another causal qualification?</strong>
          <span>Return to the bounded verified descriptor index.</span>
        </div>
        <Link className="button button-secondary" to="/routing-qualifications">
          <ArrowLeft aria-hidden="true" />
          Causal qualification
        </Link>
      </div>
    </>
  );
}

export function RoutingQualificationDetailView() {
  const { qualificationId } = useParams();
  const request = useRoutingQualificationDetail(qualificationId);
  if (request.status === "loading") return <LoadingState label="Verifying causal qualification…" />;
  if (request.status === "error" && request.error) {
    return <ErrorState error={request.error} retry={request.retry} title="The causal qualification is unavailable" />;
  }
  if (!request.data) {
    return <EmptyState title="No causal qualification selected" message="Return to the qualification index and choose a verified descriptor." />;
  }
  return <RoutingQualificationDetailContent detail={request.data} />;
}
