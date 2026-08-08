import {
  AlertTriangle,
  ArrowLeft,
  ArrowUpRight,
  Equal,
  Quote,
} from "lucide-react";
import { useEffect, useMemo, useState, type CSSProperties } from "react";

import {
  EmptyState,
  ErrorState,
  EvidenceBadge,
  LoadingState,
  PageHeader,
  Panel,
  SectionHeading,
  StatusBadge,
} from "../components/Primitives";
import { useTrialSetDetail } from "../hooks/useTrialSetDetail";
import { ApiError } from "../lib/api";
import { formatDateTime, humanize, runTargetLabel, shortDigest } from "../lib/format";
import { Link, useParams } from "../lib/router";
import type {
  TrialMetricVariationView,
  TrialRunPointView,
  TrialSetDetail,
  TrialSetMemberView,
} from "../lib/types";

interface DefinitionFact {
  readonly label: string;
  readonly value: string;
  readonly title?: string;
  readonly mono?: boolean;
}

function authoritativeValue(value: string | null): string {
  return value ?? "Unavailable";
}

function preferredVariation(variations: readonly TrialMetricVariationView[]) {
  return variations.find((variation) => variation.metric === "ttft_ns" && variation.aggregation === "p50")
    ?? variations[0]
    ?? null;
}

function pointPosition(
  point: TrialRunPointView,
  variation: TrialMetricVariationView,
): number | null {
  if (point.value === null || variation.minimum === null || variation.maximum === null) return null;
  const value = Number(point.value);
  const minimum = Number(variation.minimum);
  const maximum = Number(variation.maximum);
  if (![value, minimum, maximum].every(Number.isFinite)) return 50;
  if (maximum === minimum) return 50;
  return Math.max(0, Math.min(100, ((value - minimum) / (maximum - minimum)) * 100));
}

function DefinitionPanel({ detail }: { readonly detail: TrialSetDetail }) {
  const summary = detail.summary;
  const facts: readonly DefinitionFact[] = [
    { label: "Experiment", value: summary.experiment_id },
    { label: "Model", value: summary.model },
    { label: "Verified runs", value: summary.member_count.toLocaleString() },
    { label: "Created", value: formatDateTime(summary.created_at) },
    { label: "First run", value: formatDateTime(summary.earliest_run_at) },
    { label: "Latest run", value: formatDateTime(summary.latest_run_at) },
    { label: "Membership", value: humanize(detail.membership_policy) },
    { label: "Request populations", value: "Separate per run" },
    { label: "Reducer", value: detail.reducer_version, mono: true },
    {
      label: "Execution fingerprint",
      value: shortDigest(summary.execution_fingerprint),
      title: summary.execution_fingerprint,
      mono: true,
    },
    {
      label: "Metric definitions",
      value: shortDigest(detail.metric_definitions_digest),
      title: detail.metric_definitions_digest,
      mono: true,
    },
    {
      label: "Trial-set digest",
      value: shortDigest(summary.trial_set_digest),
      title: summary.trial_set_digest,
      mono: true,
    },
  ];

  return (
    <Panel className="trial-definition-panel">
      <SectionHeading title="Trial definition" meta="Verified immutable descriptor" />
      <div className="trial-boundary-copy">
        <div>
          <span>Design status</span>
          <strong><code>{detail.design_status}</code></strong>
        </div>
        <div>
          <span>Inference boundary</span>
          <strong><code>{detail.inference}</code></strong>
        </div>
        <p>
          This view describes observed run-level measurements from a set assembled after execution. It does not establish experimental control or acceptance.
        </p>
      </div>

      {detail.hypothesis ? (
        <div className="trial-hypothesis">
          <Quote aria-hidden="true" />
          <div><span>Recorded hypothesis</span><p>{detail.hypothesis}</p></div>
        </div>
      ) : null}

      <dl className="trial-definition-grid">
        {facts.map((fact) => (
          <div key={fact.label}>
            <dt>{fact.label}</dt>
            <dd className={fact.mono ? "mono" : undefined} title={fact.title ?? fact.value}>
              {fact.value}
            </dd>
          </div>
        ))}
      </dl>

      {summary.environment_status === "DRIFT_DETECTED" ? (
        <div className="trial-drift-note" role="note">
          <AlertTriangle aria-hidden="true" />
          <div>
            <strong>Environment drift is visible</strong>
            <span>
              {detail.environment_drift_fields.length
                ? detail.environment_drift_fields.map(humanize).join(", ")
                : "At least one projected environment field changed across member runs."}
            </span>
          </div>
        </div>
      ) : null}
    </Panel>
  );
}

function VariationPlot({ variation }: { readonly variation: TrialMetricVariationView }) {
  return (
    <figure className="trial-variation-figure" aria-labelledby="trial-variation-caption">
      <div className="trial-point-list">
        {variation.points.map((point) => {
          const position = pointPosition(point, variation);
          const style = position === null
            ? undefined
            : ({ "--trial-point-position": `${position}%` } as CSSProperties);
          return (
            <div className="trial-point-row" key={`${point.repetition_index}:${point.run_id}`}>
              <Link to={`/runs/${encodeURIComponent(point.run_id)}`}>
                Repeat {point.repetition_index + 1}
                <ArrowUpRight aria-hidden="true" />
              </Link>
              <div
                className={`trial-point-track${position === null ? " trial-point-track-unavailable" : ""}`}
                aria-hidden="true"
              >
                {position === null ? null : <span className="trial-point-marker" style={style} />}
              </div>
              <span className="trial-point-value mono">
                {authoritativeValue(point.display_value)}
              </span>
            </div>
          );
        })}
      </div>
      <figcaption id="trial-variation-caption">
        Each point is one verified run-level scalar with equal run weighting. Request populations remain separate. This retrospective view is descriptive only and makes no attribution or acceptance claim.
      </figcaption>
    </figure>
  );
}

function VariationPanel({
  variation,
  variations,
  onSelect,
}: {
  readonly variation: TrialMetricVariationView | null;
  readonly variations: readonly TrialMetricVariationView[];
  readonly onSelect: (key: string) => void;
}) {
  return (
    <Panel className="trial-variation-panel">
      <SectionHeading title="Run-to-run variation" meta="Backend-derived · equal per run" />
      {variation ? (
        <>
          <div className="trial-metric-controls">
            <label className="compact-select">
              <span>Metric</span>
              <select value={variation.key} onChange={(event) => onSelect(event.target.value)}>
                {variations.map((item) => (
                  <option value={item.key} key={item.key}>
                    {item.label} · {humanize(item.aggregation)}
                  </option>
                ))}
              </select>
            </label>
            <span className="trial-metric-status" role="status" aria-live="polite">
              {variation.available_run_count} of {variation.total_run_count} run-level values available
            </span>
          </div>

          <dl className="trial-summary-grid">
            <div><dt>Minimum</dt><dd>{authoritativeValue(variation.minimum_display_value)}</dd></div>
            <div><dt>Median</dt><dd>{authoritativeValue(variation.median_display_value)}</dd></div>
            <div><dt>Maximum</dt><dd>{authoritativeValue(variation.maximum_display_value)}</dd></div>
            <div><dt>Span</dt><dd>{authoritativeValue(variation.span_display_value)}</dd></div>
            <div><dt>Mean</dt><dd>{authoritativeValue(variation.mean_display_value)}</dd></div>
            <div>
              <dt>Sample standard deviation</dt>
              <dd>{authoritativeValue(variation.sample_standard_deviation_display_value)}</dd>
            </div>
          </dl>

          {variation.available_run_count ? (
            <VariationPlot variation={variation} />
          ) : (
            <div className="trial-inline-state">
              <EmptyState
                title="No run-level values available"
                message="The backend withheld this variation because no member exposed the selected measurement."
              />
            </div>
          )}

          {variation.available_run_count < variation.total_run_count ? (
            <div className="trial-availability-note" role="note">
              <AlertTriangle aria-hidden="true" />
              <span>Unavailable member values remain visible and are never replaced with zero.</span>
            </div>
          ) : null}
        </>
      ) : (
        <div className="trial-inline-state">
          <EmptyState
            title="No shared metric variation"
            message="The definition and member runs remain available, but no compatible run-level metric series was projected."
          />
        </div>
      )}
    </Panel>
  );
}

function memberPoint(
  member: TrialSetMemberView,
  variation: TrialMetricVariationView | null,
): TrialRunPointView | null {
  if (!variation) return null;
  return variation.points.find((point) => (
    point.repetition_index === member.repetition_index && point.run_id === member.run.run_id
  )) ?? null;
}

function MemberCard({
  member,
  variation,
}: {
  readonly member: TrialSetMemberView;
  readonly variation: TrialMetricVariationView | null;
}) {
  const point = memberPoint(member, variation);
  return (
    <article className="trial-member-card">
      <div className="trial-member-card-head">
        <span>Repeat {member.repetition_index + 1}</span>
        <EvidenceBadge eligibility={member.run.evidence_eligibility} />
      </div>
      <Link className="run-link" to={`/runs/${encodeURIComponent(member.run.run_id)}`}>
        <span className="run-link-label">{member.run.run_id}</span>
        <ArrowUpRight aria-hidden="true" />
      </Link>
      <span>{formatDateTime(member.run.started_at)} · {runTargetLabel(member.run)}</span>
      <dl>
        <div><dt>Measured requests</dt><dd>{member.run.measured_requests.toLocaleString()}</dd></div>
        <div><dt>{variation?.label ?? "Selected metric"}</dt><dd>{authoritativeValue(point?.display_value ?? null)}</dd></div>
      </dl>
    </article>
  );
}

function MembersPanel({
  detail,
  variation,
}: {
  readonly detail: TrialSetDetail;
  readonly variation: TrialMetricVariationView | null;
}) {
  return (
    <Panel className="trial-members-panel">
      <SectionHeading
        title="Member runs"
        headingId="trial-member-runs-heading"
        meta={`${detail.members.length} independent request populations`}
      />
      <div className="table-scroll trial-member-table-wrap">
        <table className="trial-member-table" aria-labelledby="trial-member-runs-heading">
          <caption>Verified member runs and selected run-level values</caption>
          <thead>
            <tr>
              <th scope="col">Repeat</th>
              <th scope="col">Run</th>
              <th scope="col">Evidence</th>
              <th scope="col" className="number-cell">Measured requests</th>
              <th scope="col" className="number-cell">{variation?.label ?? "Selected metric"}</th>
            </tr>
          </thead>
          <tbody>
            {detail.members.map((member) => {
              const point = memberPoint(member, variation);
              return (
                <tr key={`${member.repetition_index}:${member.run.run_id}`}>
                  <td className="mono">{member.repetition_index + 1}</td>
                  <td>
                    <Link className="run-link" to={`/runs/${encodeURIComponent(member.run.run_id)}`}>
                      <span className="run-link-label">{member.run.run_id}</span>
                      <ArrowUpRight aria-hidden="true" />
                    </Link>
                    <span className="table-subtext">
                      {formatDateTime(member.run.started_at)} · {runTargetLabel(member.run)}
                    </span>
                  </td>
                  <td><EvidenceBadge eligibility={member.run.evidence_eligibility} /></td>
                  <td className="number-cell mono">{member.run.measured_requests.toLocaleString()}</td>
                  <td className="number-cell mono">{authoritativeValue(point?.display_value ?? null)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="trial-member-card-list">
        {detail.members.map((member) => (
          <MemberCard
            key={`${member.repetition_index}:${member.run.run_id}`}
            member={member}
            variation={variation}
          />
        ))}
      </div>
    </Panel>
  );
}

function TrialSetDetailContent({ detail }: { readonly detail: TrialSetDetail }) {
  const defaultVariation = useMemo(() => preferredVariation(detail.variations), [detail.variations]);
  const [selectedKey, setSelectedKey] = useState(defaultVariation?.key ?? "");

  useEffect(() => {
    setSelectedKey((current) => (
      detail.variations.some((variation) => variation.key === current)
        ? current
        : defaultVariation?.key ?? ""
    ));
  }, [defaultVariation, detail.variations]);

  const variation = detail.variations.find((item) => item.key === selectedKey) ?? defaultVariation;

  return (
    <>
      <PageHeader
        title={detail.summary.title}
        subtitle={`${detail.summary.experiment_id} · ${detail.summary.model} · ${detail.summary.member_count} verified runs`}
        action={
          <div className="badge-group">
            <StatusBadge status="RETROSPECTIVE" label="RETROSPECTIVE" tone="neutral" />
            <StatusBadge status="DESCRIPTIVE_ONLY" label="DESCRIPTIVE_ONLY" tone="info" />
          </div>
        }
      />
      <DefinitionPanel detail={detail} />
      <VariationPanel variation={variation} variations={detail.variations} onSelect={setSelectedKey} />
      <MembersPanel detail={detail} variation={variation} />
      <div className="trial-detail-footnote">
        <Equal aria-hidden="true" />
        <span>Every member contributes at most one run-level scalar to each projected series.</span>
      </div>
    </>
  );
}

export function TrialSetDetailView() {
  const { trialSetId } = useParams();
  const request = useTrialSetDetail(trialSetId);

  if (!trialSetId) {
    return (
      <>
        <PageHeader title="Trial set detail" subtitle="Inspect repeated verified runs without pooling requests." />
        <EmptyState title="No trial set selected" message="Choose a verified trial set from the Trial sets view." />
      </>
    );
  }

  if (request.status === "loading") {
    return (
      <>
        <PageHeader title={trialSetId} subtitle="Inspect repeated verified runs without pooling requests." />
        <LoadingState label="Verifying members and projecting run-level variation…" />
      </>
    );
  }

  if (request.status === "error" && request.error) {
    if (request.error instanceof ApiError && request.error.status === 404) {
      return (
        <>
          <PageHeader title="Trial set not found" subtitle="That identity is not present in the verified trial-set index." />
          <div className="trial-not-found">
            <EmptyState
              title="No verified trial set at this identity"
              message="It may have been removed, replaced, or withheld after member verification."
            />
            <Link className="button button-secondary" to="/trial-sets">
              <ArrowLeft aria-hidden="true" />
              Back to Trial sets
            </Link>
          </div>
        </>
      );
    }
    return (
      <>
        <PageHeader title={trialSetId} subtitle="Inspect repeated verified runs without pooling requests." />
        <ErrorState error={request.error} retry={request.retry} title="This trial set could not be opened" />
      </>
    );
  }

  if (!request.data) return null;
  return <TrialSetDetailContent detail={request.data} />;
}
