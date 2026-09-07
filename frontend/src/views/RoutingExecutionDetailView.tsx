import {
  ArrowLeft,
  CheckCircle2,
  Clock3,
  DatabaseZap,
  Fingerprint,
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
import { useRoutingExecutionDetail } from "../hooks/useRoutingExecutionDetail";
import { formatBytes, formatDurationNs, humanize, shortDigest } from "../lib/format";
import { Link, useParams } from "../lib/router";
import type {
  RoutingExecutionCandidateView,
  RoutingExecutionDetail,
  RoutingExecutionTelemetryView,
  RoutingExecutionTrialView,
} from "../lib/types";

function DataCell({ label, value, title }: { readonly label: string; readonly value: string; readonly title?: string }) {
  return (
    <div className="execution-data-cell">
      <span>{label}</span>
      <code title={title ?? value}>{value}</code>
    </div>
  );
}

function telemetryLabel(telemetry: RoutingExecutionTelemetryView): string {
  if (telemetry.state === "UNAVAILABLE") return "Unavailable";
  return telemetry.admissibility === "ADMISSIBLE" ? "Admissible" : "Inadmissible";
}

function TelemetryLine({ telemetry }: { readonly telemetry: RoutingExecutionTelemetryView }) {
  const unavailable = telemetry.state === "UNAVAILABLE";
  return (
    <div className="execution-telemetry-line">
      <strong>{telemetry.signal}</strong>
      <span>
        {unavailable
          ? telemetry.source === "UNAVAILABLE_CAPABILITY"
            ? "capability unavailable"
            : "observation unavailable"
          : <>
              age <code>{formatDurationNs(telemetry.age_ns)}</code> / bound <code>{formatDurationNs(telemetry.freshness_bound_ns)}</code>
            </>}
      </span>
      <span>epoch <code>{telemetry.epoch}</code> · {telemetry.source}</span>
      <StatusBadge
        status={telemetry.state}
        label={telemetryLabel(telemetry)}
        tone={unavailable ? "neutral" : telemetry.admissibility === "ADMISSIBLE" ? "valid" : "warning"}
      />
    </div>
  );
}

function CandidateLedger({ candidate }: { readonly candidate: RoutingExecutionCandidateView }) {
  return (
    <section className="execution-candidate" aria-label={`${candidate.endpoint_id} candidate state`}>
      <div className="execution-candidate-heading">
        <strong>{candidate.endpoint_id}</strong>
        <StatusBadge
          status={candidate.eligible ? "ELIGIBLE" : "INELIGIBLE"}
          label={candidate.eligible ? "Eligible" : "Ineligible"}
          tone={candidate.eligible ? "valid" : "warning"}
        />
      </div>
      <TelemetryLine telemetry={candidate.health} />
      <TelemetryLine telemetry={candidate.load} />
      <TelemetryLine telemetry={candidate.gpu_dcgm} />
      <TelemetryLine telemetry={candidate.kv_cache} />
    </section>
  );
}

function ClaimRow({ label, values }: { readonly label: string; readonly values: readonly string[] }) {
  return (
    <div className="execution-claim-row">
      <span>{label}</span>
      {values.length ? values.map((value) => <code key={value}>{value}</code>) : <em>None</em>}
    </div>
  );
}

function TrialReset({ trial }: { readonly trial: RoutingExecutionTrialView }) {
  return (
    <Panel className="execution-reset-panel">
      <SectionHeading
        title="Trial reset and fault receipt"
        meta={`fault activates at request ${trial.fault.activated_at_sequence_index + 1}`}
      />
      <div className="execution-reset-grid">
        <div>
          <span>Runner reset</span>
          <strong><CheckCircle2 aria-hidden="true" /> Connection and telemetry state cleared</strong>
          <small><code>{trial.reset.endpoint_engine_reset_assertion}</code></small>
        </div>
        <div>
          <span>Observer epochs</span>
          <div className="execution-epoch-list">
            {trial.reset.observer_epochs.map((observer) => <code key={observer.signal}>{observer.signal} · epoch {observer.epoch}</code>)}
          </div>
        </div>
        <div>
          <span>Controlled fault</span>
          <strong>Load collection paused · health continued</strong>
          <small>activated <code>{formatDurationNs(trial.fault.activated_at_monotonic_ns)}</code></small>
        </div>
      </div>
    </Panel>
  );
}

function TrialLedger({ trial }: { readonly trial: RoutingExecutionTrialView }) {
  return (
    <section className="execution-trial-section" aria-labelledby={`${trial.trial_id}-heading`}>
      <div className="execution-trial-heading">
        <div>
          <span className="execution-section-kicker">Policy trial</span>
          <h2 id={`${trial.trial_id}-heading`}>{humanize(trial.policy_id)}</h2>
          <span className="mono">{trial.trial_id}</span>
        </div>
        <StatusBadge status="RECORDED" label={`${trial.terminal_population_total} terminal receipts`} tone="info" />
      </div>
      <TrialReset trial={trial} />
      <Panel className="execution-request-panel">
        <SectionHeading
          title="Observation → decision → outcome"
          headingId={`${trial.trial_id}-requests-heading`}
          meta="Complete six-request population"
        />
        <div className="table-scroll execution-request-table-wrap">
          <table className="execution-request-table" aria-labelledby={`${trial.trial_id}-requests-heading`}>
            <caption>Every request receipt with candidate telemetry, route decision, and terminal outcome</caption>
            <thead>
              <tr>
                <th scope="col">Request / decision clock</th>
                <th scope="col">What the router knew</th>
                <th scope="col">Selection and fallback</th>
                <th scope="col">Terminal outcome</th>
              </tr>
            </thead>
            <tbody>
              {trial.requests.map((request) => (
                <tr key={request.decision_id}>
                  <th scope="row">
                    <code>{request.request_id}</code>
                    <span className="table-subtext">sequence {request.sequence_index + 1} · <code>{formatDurationNs(request.decision_at_monotonic_ns)}</code></span>
                    <span className="table-subtext mono" title={request.decision_id}>{request.decision_id}</span>
                  </th>
                  <td>
                    <div className="execution-candidate-list">
                      {request.candidates.map((candidate) => <CandidateLedger key={candidate.endpoint_id} candidate={candidate} />)}
                    </div>
                  </td>
                  <td>
                    <dl className="execution-decision-facts">
                      <div><dt>Selected endpoint</dt><dd><code>{request.selected_endpoint_id ?? "none"}</code></dd></div>
                      <div><dt>Fallback reason</dt><dd><code>{request.fallback_reason}</code></dd></div>
                    </dl>
                    <ClaimRow label="Claims used" values={request.claims_used} />
                    <ClaimRow label="Stale claims permitted" values={request.claims_permitted_stale} />
                    <ClaimRow label="Claims discarded" values={request.claims_discarded} />
                  </td>
                  <td>
                    <StatusBadge status={request.terminal.status} label={humanize(request.terminal.status)} />
                    <span className="table-subtext"><code>{request.terminal.reason}</code></span>
                    <span className="table-subtext">attempts <code>{request.terminal.attempt_count}</code>{request.terminal.http_status === null ? " · no HTTP status" : <> · HTTP <code>{request.terminal.http_status}</code></>}</span>
                    <span className="table-subtext">{formatDurationNs(request.terminal.started_at_monotonic_ns)}–{formatDurationNs(request.terminal.ended_at_monotonic_ns)}</span>
                    <span className="table-subtext mono" title={request.terminal.terminal_outcome_id}>{request.terminal.terminal_outcome_id}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel className="execution-population-panel">
        <SectionHeading title="Separate terminal population" meta={`${trial.terminal_population_total} of 6 requests`} />
        <div className="execution-population-strip" aria-label="Terminal population by status">
          {trial.terminal_population.map((population) => (
            <div key={population.status}>
              <StatusBadge status={population.status} label={humanize(population.status)} />
              <strong className="mono">{population.count}</strong>
            </div>
          ))}
          <div className="execution-population-total"><span>Total</span><strong className="mono">{trial.terminal_population_total}</strong></div>
        </div>
      </Panel>
    </section>
  );
}

function executionBoundary(detail: RoutingExecutionDetail): string {
  if (detail.summary.mode === "LAMBDA_MANUAL_HOST") {
    return "Manual-host facts are operator-declared. This is not provider, capacity, GPU allocation, price, or cleanup proof; a locally generated fixture is not a real campaign.";
  }
  if (detail.summary.mode === "LOCAL_LOOPBACK") {
    return "This is a local loopback execution record. It establishes neither GPU serving nor a provider campaign.";
  }
  return "This package records a private-target execution boundary. It does not authorize a provider action or issue an acceptance verdict.";
}

function RoutingExecutionContent({ detail }: { readonly detail: RoutingExecutionDetail }) {
  return (
    <>
      <PageHeader
        title={detail.summary.execution_id}
        subtitle="A sealed two-endpoint routing record, rendered only after digest-bound offline replay."
        action={<StatusBadge status="VERIFIED" label="Offline replay verified" tone="valid" />}
      />
      <div className="execution-detail-boundary" role="note">
        <ShieldCheck aria-hidden="true" />
        <div>
          <strong><code>{detail.interpretation_boundary}</code></strong>
          <p>{executionBoundary(detail)}</p>
        </div>
      </div>

      <div className="execution-overview-grid">
        <Panel>
          <SectionHeading title="What ran" meta="Immutable identities" />
          <div className="execution-data-grid">
            <DataCell label="Source commit" value={shortDigest(detail.summary.source_commit)} title={detail.summary.source_commit} />
            <DataCell label="Model / tokenizer" value={detail.summary.model.model_id} />
            <DataCell label="Runtime / adapter" value={`${detail.summary.runtime.runtime_name} ${detail.summary.runtime.runtime_version}`} />
            <DataCell label="Execution mode" value={detail.summary.mode} />
            <DataCell label="Runner image" value={shortDigest(detail.evidence.runner_image)} title={detail.evidence.runner_image} />
            <DataCell label="Serving image" value={shortDigest(detail.evidence.serving_image)} title={detail.evidence.serving_image} />
          </div>
        </Panel>
        <Panel>
          <SectionHeading title="How it was verified" meta="No raw package reopen" />
          <div className="execution-data-grid">
            <DataCell label="Retained digest" value={shortDigest(detail.summary.retained_digest)} title={detail.summary.retained_digest} />
            <DataCell label="Input transfer" value="Verified before transport" />
            <DataCell label="Workload bytes" value={formatBytes(detail.evidence.input_transfer.workload_size_bytes)} />
            <DataCell label="Terminal closure" value={`${detail.summary.terminal_denominator} / ${detail.summary.terminal_denominator}`} />
            <DataCell label="Replay" value="Complete receipt replay" />
            <DataCell label="Package access" value="One verified in-memory snapshot" />
          </div>
        </Panel>
      </div>

      <Panel className="execution-instrument-panel">
        <SectionHeading title="Observed admission conditions" meta="Measured at observation and decision" />
        <div className="execution-instrument-strip">
          <div><Clock3 aria-hidden="true" /><span>Freshness bound</span><strong><code>5 ms</code></strong></div>
          <div><DatabaseZap aria-hidden="true" /><span>Load collection</span><strong>Paused after request 2</strong></div>
          <div><Route aria-hidden="true" /><span>Health collection</span><strong>Continued independently</strong></div>
          <div><Fingerprint aria-hidden="true" /><span>Unavailable signals</span><strong>GPU/DCGM and KV/cache retained as unavailable</strong></div>
        </div>
      </Panel>

      <div className="execution-policy-strip">
        <span>Recorded policy order</span>
        {detail.summary.policy_ids.map((policy) => <code key={policy}>{policy}</code>)}
      </div>
      {detail.trials.map((trial) => <TrialLedger key={trial.trial_id} trial={trial} />)}
      <div className="view-cta execution-back-link">
        <div>
          <strong>Need the execution ledger?</strong>
          <span>Return to the single verified package index. Withheld packages remain unreadable.</span>
        </div>
        <Link className="button button-secondary" to="/routing-executions">
          <ArrowLeft aria-hidden="true" />
          Routing executions
        </Link>
      </div>
    </>
  );
}

export function RoutingExecutionDetailView() {
  const { executionId } = useParams();
  const request = useRoutingExecutionDetail(executionId);
  if (!executionId) {
    return <><PageHeader title="Routing execution" subtitle="Inspect one verified sealed routing-execution package." /><EmptyState title="No routing execution selected" message="Choose the bounded execution from its verified index." /></>;
  }
  if (request.status === "loading") {
    return <><PageHeader title="Routing execution" subtitle="Verify a sealed execution package before projecting it." /><LoadingState label="Verifying execution receipts and offline replay…" /></>;
  }
  if (request.status === "error" && request.error) {
    return <><PageHeader title="Routing execution" subtitle="Inspect one verified sealed routing-execution package." /><ErrorState error={request.error} retry={request.retry} title="This routing execution could not be opened" /></>;
  }
  return request.data ? <RoutingExecutionContent detail={request.data} /> : null;
}
