import { ArrowRight, Ban, Clock3, Layers3, Quote } from "lucide-react";

import { DistributionBars, MeasurementTable, MetricStrip } from "../components/RunData";
import {
  EmptyState,
  ErrorState,
  EvidenceBadge,
  IntegrityBadge,
  LoadingState,
  PageHeader,
  Panel,
  SectionHeading,
} from "../components/Primitives";
import { useRunDetail } from "../hooks/useRunDetail";
import { Link, useParams } from "../lib/router";
import {
  formatDateTime,
  formatDurationNs,
  formatPrimitive,
  humanize,
  runModelLabel,
  runTargetLabel,
  shortDigest,
} from "../lib/format";
import type { ContextFieldView, EnvironmentFieldView, JsonPrimitive } from "../lib/types";

interface Fact {
  readonly label: string;
  readonly value: JsonPrimitive | undefined;
  readonly mono?: boolean;
}

function FactList({ facts }: { readonly facts: readonly Fact[] }) {
  return (
    <dl className="fact-list">
      {facts.map((fact) => (
        <div key={fact.label}>
          <dt>{fact.label}</dt>
          <dd className={fact.mono ? "mono" : undefined} title={formatPrimitive(fact.value)}>
            {formatPrimitive(fact.value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function ContextFields({ fields }: { readonly fields: readonly ContextFieldView[] }) {
  if (!fields.length) {
    return <div className="inline-empty"><Layers3 aria-hidden="true" /> No frozen context fields were projected.</div>;
  }
  return (
    <dl className="fact-list environment-list">
      {fields.map((field) => (
        <div key={field.key}>
          <dt>
            {field.label}
            <span className="provenance-label">{humanize(field.group)}</span>
          </dt>
          <dd className="mono" title={formatPrimitive(field.value)}>{formatPrimitive(field.value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function EnvironmentFields({ fields }: { readonly fields: readonly EnvironmentFieldView[] }) {
  if (!fields.length) {
    return <div className="inline-empty"><Layers3 aria-hidden="true" /> No environment fields were projected.</div>;
  }
  return (
    <dl className="fact-list environment-list">
      {fields.map((field) => (
        <div key={field.name}>
          <dt>
            {field.label}
            <span className="provenance-label">{humanize(field.provenance)}</span>
          </dt>
          <dd className="mono" title={formatPrimitive(field.value)}>{formatPrimitive(field.value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function RunDetailView() {
  const { runId } = useParams();
  const request = useRunDetail(runId);

  if (!runId) {
    return (
      <>
        <PageHeader title="Run detail" subtitle="Inspect recalculated measurements and frozen execution context." />
        <EmptyState title="No run selected" message="Choose a verified bundle from the Runs view." />
      </>
    );
  }
  if (request.status === "loading") {
    return (
      <>
        <PageHeader title={runId} subtitle="Inspect recalculated measurements and frozen execution context." />
        <LoadingState label="Recalculating and projecting this run…" />
      </>
    );
  }
  if (request.status === "error" && request.error) {
    return (
      <>
        <PageHeader title={runId} subtitle="Inspect recalculated measurements and frozen execution context." />
        <ErrorState error={request.error} retry={request.retry} title="This run could not be opened" />
      </>
    );
  }
  if (!request.data) return null;

  const detail = request.data;
  const run = detail.summary;
  const executionFacts: readonly Fact[] = [
    { label: "Started", value: formatDateTime(detail.execution.started_at) },
    { label: "Ended", value: formatDateTime(detail.execution.ended_at) },
    { label: "Run duration", value: formatDurationNs(detail.execution.duration_ns) },
    { label: "Measured window", value: formatDurationNs(detail.execution.measurement_window_ns) },
    { label: "Traffic", value: humanize(detail.execution.traffic_kind) },
    { label: "Concurrency", value: detail.execution.concurrency },
    { label: "Request rate", value: detail.execution.requests_per_second },
    { label: "Warmup requests", value: detail.execution.warmup_requests },
    { label: "Measured requests", value: detail.execution.measured_requests },
    { label: "Producer exit", value: detail.execution.producer_exit_status },
  ];

  return (
    <>
      <PageHeader
        title={run.run_id}
        subtitle={`${runModelLabel(run)} · ${runTargetLabel(run)} · ${formatDateTime(run.started_at)}`}
        action={
          <div className="badge-group">
            <IntegrityBadge integrity={detail.verification.integrity_status} />
            <EvidenceBadge eligibility={run.evidence_eligibility} />
          </div>
        }
      />

      {detail.hypothesis ? (
        <div className="hypothesis-callout">
          <Quote aria-hidden="true" />
          <div><span>Experiment hypothesis</span><p>{detail.hypothesis}</p></div>
        </div>
      ) : null}

      <Panel className="metric-panel">
        <MetricStrip measurements={detail.measurements} />
      </Panel>

      <div className="detail-grid">
        <Panel className="distribution-panel">
          <SectionHeading
            title="Observed distributions"
            headingId="distributions-heading"
            meta="Canonical request records"
          />
          {detail.distributions.some((distribution) => distribution.bins.length) ? (
            <div className="distribution-list">
              {detail.distributions.map((distribution) =>
                distribution.bins.length ? (
                  <DistributionBars distribution={distribution} key={distribution.metric} />
                ) : null,
              )}
            </div>
          ) : (
            <div className="panel-body">
              <EmptyState
                title="No distributions available"
                message="This bundle has no successful timing observations to bin."
              />
            </div>
          )}
        </Panel>

        <Panel>
          <SectionHeading title="Execution" meta={detail.execution.terminal_state} />
          <FactList facts={executionFacts} />
        </Panel>
      </div>

      <div className="evidence-summary detail-context-grid">
        <Panel>
          <SectionHeading title="Frozen experiment context" meta={`${detail.context.length} fields`} />
          <ContextFields fields={detail.context} />
        </Panel>
        <Panel>
          <SectionHeading title="Execution environment" meta={run.environment_completeness} />
          <EnvironmentFields fields={detail.environment} />
        </Panel>
      </div>

      <Panel className="measurement-panel">
        <SectionHeading
          title="Measurement inventory"
          headingId="measurement-inventory-heading"
          meta={`${detail.measurements.length} projections`}
        />
        <MeasurementTable measurements={detail.measurements} />
      </Panel>

      <Panel className="unavailable-panel">
        <SectionHeading
          title="Unavailable metrics"
          headingId="unavailable-heading"
          meta={`${detail.unavailable.length} explicit exclusions`}
        />
        {detail.unavailable.length ? (
          <ul className="unavailable-list" aria-labelledby="unavailable-heading">
            {detail.unavailable.map((item) => (
              <li key={item.metric}>
                <Ban aria-hidden="true" />
                <div>
                  <strong>{humanize(item.metric)}</strong>
                  <span>{humanize(item.reason)}</span>
                </div>
                <code>{item.capability_matrix}</code>
              </li>
            ))}
          </ul>
        ) : (
          <div className="inline-empty"><Clock3 aria-hidden="true" /> No unavailable metrics were reported.</div>
        )}
      </Panel>

      <div className="view-cta">
        <div>
          <strong>Need the proof behind these values?</strong>
          <span>
            Bundle <code>{shortDigest(detail.verification.bundle_digest)}</code> was verified by authoritative recalculation.
          </span>
        </div>
        <Link className="button button-secondary" to={`/evidence/${encodeURIComponent(run.run_id)}`}>
          Open evidence
          <ArrowRight aria-hidden="true" />
        </Link>
      </div>
    </>
  );
}
