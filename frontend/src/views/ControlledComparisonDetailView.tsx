import {
  AlertTriangle,
  ArrowLeft,
  ArrowUpRight,
  CalendarClock,
  CircleDotDashed,
  Fingerprint,
  Layers3,
  Scale,
  ShieldCheck,
} from "lucide-react";
import type { CSSProperties, ReactNode } from "react";

import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  Panel,
  SectionHeading,
  StatusBadge,
} from "../components/Primitives";
import { useControlledComparisonDetail } from "../hooks/useControlledComparisonDetail";
import {
  formatDateTime,
  formatRawMetricValue,
  humanize,
  metricLabel,
  shortDigest,
} from "../lib/format";
import { Link, useParams } from "../lib/router";
import type {
  ControlledComparisonCheckId,
  ControlledComparisonControlCheck,
  ControlledComparisonDetail,
  ControlledComparisonOutcomeEstimate,
  ControlledComparisonRunValue,
  ControlledComparisonScheduleSlot,
  ControlledComparisonResultStatus,
  TrialSetSummary,
} from "../lib/types";

const CHECK_COPY: Readonly<Record<ControlledComparisonCheckId, {
  readonly label: string;
  readonly explanation: string;
}>> = {
  LOCAL_PLAN_ORDER: {
    label: "Local plan order",
    explanation: "Checks ordering inside the operator-controlled local workflow. It is not independently trusted chronology proof.",
  },
  EXACT_ARM_MEMBERSHIP: {
    label: "Exact arm membership",
    explanation: "Each verified Trial Set contains exactly the frozen run IDs for its declared arm, without replacement or omission.",
  },
  OBSERVED_SCHEDULE: {
    label: "Observed schedule",
    explanation: "Verified run start order matches every slot in the predeclared permuted-pair schedule.",
  },
  DECLARED_FINGERPRINT_DIFFERENCE: {
    label: "Declared fingerprint difference",
    explanation: "Recomputed arm projections differ only at the reviewed traffic.concurrency treatment path and declared values.",
  },
  COMPLETE_EQUAL_OBSERVED_ENVIRONMENT: {
    label: "Complete and equal observed environment",
    explanation: "Every field in the v1 observed allowlist is complete and equal across arms; unobserved real-world controls remain outside this claim.",
  },
  OUTCOME_COVERAGE_AND_SEMANTICS: {
    label: "Outcome coverage and semantics",
    explanation: "Every planned run supplies the declared outcome under one frozen metric definition and reducer.",
  },
};

function DetailStatus({ status }: { readonly status: ControlledComparisonResultStatus }) {
  if (status === "NO_RESULT") {
    return <StatusBadge status="NO_RESULT" label="No result artifact" tone="neutral" />;
  }
  return (
    <StatusBadge
      status={status}
      label={status === "WITHHELD" ? "Withheld" : status}
      tone={status === "COMPARABLE" ? "info" : status === "WITHHELD" ? "warning" : "neutral"}
    />
  );
}

function armLabel(arm: "BASELINE" | "CANDIDATE"): "Baseline" | "Candidate" {
  return arm === "BASELINE" ? "Baseline" : "Candidate";
}

function DefinitionItem({ label, value, mono = false }: {
  readonly label: string;
  readonly value: ReactNode;
  readonly mono?: boolean;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={mono ? "mono" : undefined}>{value}</dd>
    </div>
  );
}

function ArmPlanCard({ detail, arm }: {
  readonly detail: ControlledComparisonDetail;
  readonly arm: "BASELINE" | "CANDIDATE";
}) {
  const plan = arm === "BASELINE" ? detail.plan.baseline_arm : detail.plan.candidate_arm;
  const value = arm === "BASELINE"
    ? detail.plan.independent_variable.baseline_value
    : detail.plan.independent_variable.candidate_value;
  return (
    <article className={`comparison-arm-plan comparison-arm-${arm.toLowerCase()}`}>
      <div className="comparison-arm-plan-head">
        <span className="comparison-arm-symbol" aria-hidden="true" />
        <div>
          <span>{armLabel(arm)} arm</span>
          <strong>{detail.plan.independent_variable.path} = {value}</strong>
        </div>
      </div>
      <dl>
        <DefinitionItem label="Planned Trial Set" value={plan.planned_trial_set_id} mono />
        <DefinitionItem label="Planned runs" value={plan.run_ids.length} mono />
        <DefinitionItem label="Source spec" value={shortDigest(plan.source_spec_digest)} mono />
        <DefinitionItem label="Execution fingerprint" value={shortDigest(plan.expected_execution_fingerprint)} mono />
      </dl>
    </article>
  );
}

function PredeclaredDesign({ detail }: { readonly detail: ControlledComparisonDetail }) {
  const { plan, summary } = detail;
  return (
    <Panel className="comparison-design-panel">
      <SectionHeading
        title="Predeclared design"
        headingId="comparison-design-heading"
        meta={formatDateTime(plan.created_at)}
      />
      <div className="comparison-assurance-grid">
        <div>
          <CalendarClock aria-hidden="true" />
          <span><code>OPERATOR_ATTESTED</code></span>
          <p>
            The plan digest is retained by the operator and local ordering is checked. Plan chronology is not independently proven by a trusted timestamp or transparency receipt.
          </p>
        </div>
        <div>
          <Fingerprint aria-hidden="true" />
          <span><code>OBSERVED_V1_ALLOWLIST_ONLY</code></span>
          <p>
            Environmental equality covers Inferdrome’s reviewed v1 observed fields only. It does not assert that every material real-world factor was observed.
          </p>
        </div>
      </div>

      <div className="comparison-hypothesis">
        <span>Recorded hypothesis</span>
        <blockquote>{plan.hypothesis}</blockquote>
      </div>

      <div className="comparison-treatment-card">
        <span>Independent variable</span>
        <code>{plan.independent_variable.path}</code>
        <div className="comparison-treatment-equation" aria-label={`Baseline ${plan.independent_variable.baseline_value}; candidate ${plan.independent_variable.candidate_value}`}>
          <span><small>Baseline</small><strong>{plan.independent_variable.baseline_value}</strong></span>
          <span aria-hidden="true">→</span>
          <span><small>Candidate</small><strong>{plan.independent_variable.candidate_value}</strong></span>
        </div>
      </div>

      <div className="comparison-arm-plan-grid">
        <ArmPlanCard detail={detail} arm="BASELINE" />
        <ArmPlanCard detail={detail} arm="CANDIDATE" />
      </div>

      <dl className="comparison-definition-grid">
        <DefinitionItem label="Planned repeats" value={`${plan.planned_repetitions_per_arm} per arm`} />
        <DefinitionItem label="Schedule" value="Predeclared permuted pairs" />
        <DefinitionItem label="Statistical unit" value="Run" />
        <DefinitionItem label="Weighting" value="Equal per run" />
        <DefinitionItem label="Estimator" value="Paired run mean difference" />
        <DefinitionItem label="Contrast" value="Candidate − baseline" />
        <DefinitionItem label="Missing data" value="Any missing outcome makes arms incomparable" />
        <DefinitionItem label="Exclusions" value="No post-assignment exclusions" />
      </dl>

      <div className="comparison-primary-outcome">
        <div>
          <span>Declared outcome</span>
          <strong>{summary.primary_outcome_label}</strong>
          <code>{plan.primary_outcome.metric}:{plan.primary_outcome.aggregation}</code>
        </div>
        <dl>
          <DefinitionItem label="Unit" value={plan.primary_outcome.unit} mono />
          <DefinitionItem label="Population" value={humanize(plan.primary_outcome.population)} />
          <DefinitionItem label="Definition" value={shortDigest(plan.primary_outcome.definition_id)} mono />
          <DefinitionItem label="Uncertainty" value="None" />
        </dl>
      </div>

      <div className="comparison-digest-line">
        <ShieldCheck aria-hidden="true" />
        <div>
          <span>Predeclaration anchor · operator-retained plan digest</span>
          <code title={summary.comparison_plan_digest}>{summary.comparison_plan_digest}</code>
        </div>
      </div>
    </Panel>
  );
}

function ComparabilityResult({ detail }: { readonly detail: ControlledComparisonDetail }) {
  const { result, result_issue: issue, summary } = detail;
  return (
    <Panel className="comparison-controls-panel">
      <SectionHeading
        title="Comparability result"
        headingId="comparison-controls-heading"
        meta={result ? formatDateTime(result.created_at) : "No verified result"}
      />
      {!result ? (
        <div className="comparison-inline-state">
          <EmptyState
            title={issue ? "Result artifact withheld" : "No result artifact"}
            message={
              issue === "DUPLICATE_RESULT_FOR_PLAN"
                ? "More than one result targets this plan, so Inferdrome withholds every estimate."
                : issue === "RESULT_VERIFICATION_FAILED"
                  ? "The linked result did not satisfy verification, so Inferdrome withholds every estimate."
                  : "The frozen design is visible, but no verified comparability result has been published."
            }
            kind={issue ? "warning" : "empty"}
          />
        </div>
      ) : (
        <>
          <div className={`comparison-result-banner comparison-result-${result.status.toLowerCase()}`}>
            {result.status === "COMPARABLE" ? <Scale aria-hidden="true" /> : <AlertTriangle aria-hidden="true" />}
            <div>
              <strong>{result.status}</strong>
              <p>
                {result.status === "COMPARABLE"
                  ? "The verified arms satisfy every frozen comparison control. Estimates below are candidate arm mean minus baseline arm mean."
                  : "The verified arms do not satisfy every frozen comparison control. Outcome estimates are suppressed."}
              </p>
            </div>
            <DetailStatus status={summary.result_status} />
          </div>
          <div className="table-scroll comparison-check-table-wrap">
            <table className="comparison-check-table" aria-labelledby="comparison-controls-heading">
              <caption>Controlled-comparison comparability checks</caption>
              <thead><tr><th scope="col">Control</th><th scope="col">Status</th><th scope="col">Explanation</th></tr></thead>
              <tbody>
                {result.control_checks.map((check) => {
                  const copy = CHECK_COPY[check.check];
                  return (
                    <tr key={check.check}>
                      <th scope="row">{copy.label}</th>
                      <td>
                        <StatusBadge
                          status={check.status}
                          label={check.status === "SATISFIED" ? "Satisfied" : "Unsatisfied"}
                          tone={check.status === "SATISFIED" ? "info" : "warning"}
                        />
                      </td>
                      <td>{copy.explanation}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Panel>
  );
}

function finiteValue(value: string): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function displayValue(value: string, unit: string, signed = false): string {
  const formatted = formatRawMetricValue(value, unit);
  const number = finiteValue(value);
  const prefix = signed && number !== null && number > 0 ? "+" : "";
  return `${prefix}${formatted.value}${formatted.unit ? ` ${formatted.unit}` : ""}`;
}

function exactValue(value: string, unit: string, signed = false): string {
  const isZero = /^0(?:\.0+)?$/.test(value);
  const prefix = signed && !value.startsWith("-") && !isZero ? "+" : "";
  return `${prefix}${value}${unit ? ` ${unit}` : ""}`;
}

function OutcomeDotPlot({ outcome }: { readonly outcome: ControlledComparisonOutcomeEstimate }) {
  const allValues = [...outcome.baseline_values, ...outcome.candidate_values]
    .map((point) => finiteValue(point.value))
    .filter((value): value is number => value !== null);
  const rawMinimum = Math.min(...allValues);
  const rawMaximum = Math.max(...allValues);
  const padding = rawMinimum === rawMaximum ? Math.max(Math.abs(rawMinimum) * 0.05, 1) : 0;
  const minimum = rawMinimum - padding;
  const maximum = rawMaximum + padding;
  const span = maximum - minimum || 1;
  const position = (value: string) => {
    const numeric = finiteValue(value) ?? minimum;
    return Math.min(98, Math.max(2, ((numeric - minimum) / span) * 100));
  };
  const rows: readonly ["BASELINE" | "CANDIDATE", readonly ControlledComparisonRunValue[], string][] = [
    ["BASELINE", outcome.baseline_values, outcome.baseline_mean],
    ["CANDIDATE", outcome.candidate_values, outcome.candidate_mean],
  ];

  return (
    <figure className="comparison-dot-plot">
      <div className="comparison-dot-plot-rows">
        {rows.map(([arm, points, mean]) => (
          <div className={`comparison-dot-row comparison-dot-${arm.toLowerCase()}`} key={arm}>
            <div className="comparison-dot-label">
              <span className="comparison-arm-symbol" aria-hidden="true" />
              <strong>{armLabel(arm)}</strong>
              <small>n = {points.length}</small>
            </div>
            <div className="comparison-dot-track">
              {points.map((point) => (
                <span
                  className="comparison-run-dot"
                  key={point.run_id}
                  style={{ "--comparison-position": `${position(point.value)}%` } as CSSProperties}
                  tabIndex={0}
                  title={`Repeat ${point.repetition_index + 1}: ${displayValue(point.value, outcome.unit)} · ${point.sample_count} samples`}
                  aria-label={`${armLabel(arm)} repeat ${point.repetition_index + 1}, ${displayValue(point.value, outcome.unit)}, ${point.sample_count} samples`}
                  role="img"
                />
              ))}
              <span
                className="comparison-mean-marker"
                style={{ "--comparison-position": `${position(mean)}%` } as CSSProperties}
                title={`${armLabel(arm)} mean: ${displayValue(mean, outcome.unit)}`}
                aria-hidden="true"
              />
            </div>
            <strong className="comparison-dot-mean">{displayValue(mean, outcome.unit)}</strong>
          </div>
        ))}
      </div>
      <div className="comparison-dot-axis" aria-hidden="true">
        <span>{displayValue(String(minimum), outcome.unit)}</span>
        <span>Shared scale · diamond marks mean</span>
        <span>{displayValue(String(maximum), outcome.unit)}</span>
      </div>
      <figcaption>
        Each point is one verified run-level measurement. The two arms share one axis; diamond markers show arithmetic means.
      </figcaption>
    </figure>
  );
}

function EstimateSection({ outcome }: { readonly outcome: ControlledComparisonOutcomeEstimate }) {
  const label = metricLabel(outcome.selector.metric);
  return (
    <Panel className="comparison-estimate-panel">
      <SectionHeading
        title="Outcome estimate"
        headingId="comparison-estimate-heading"
        meta={<><code>POINT_ESTIMATE_ONLY</code> · equal per run</>}
      />
      <div className="comparison-estimate-title">
        <div>
          <span>Declared outcome</span>
          <strong>{label} · {humanize(outcome.selector.aggregation)}</strong>
        </div>
        <StatusBadge status="NEUTRAL" label="Arithmetic direction only" tone="neutral" />
      </div>
      <div className="comparison-estimate-grid">
        <div className="comparison-mean-card comparison-mean-baseline">
          <span>Baseline mean</span>
          <strong>{displayValue(outcome.baseline_mean, outcome.unit)}</strong>
          <small>{outcome.baseline_values.length} verified runs</small>
        </div>
        <div className="comparison-mean-card comparison-mean-candidate">
          <span>Candidate mean</span>
          <strong>{displayValue(outcome.candidate_mean, outcome.unit)}</strong>
          <small>{outcome.candidate_values.length} verified runs</small>
        </div>
        <div className="comparison-contrast-card">
          <span>Candidate − baseline</span>
          <strong>{displayValue(outcome.estimate, outcome.unit, true)}</strong>
          <small>Paired run mean difference</small>
        </div>
      </div>

      <OutcomeDotPlot outcome={outcome} />

      <div className="table-scroll comparison-paired-table-wrap">
        <table className="comparison-paired-table" aria-labelledby="comparison-estimate-heading">
          <caption>Exact paired run differences</caption>
          <thead><tr><th scope="col">Block</th><th scope="col">Baseline run</th><th scope="col">Candidate run</th><th scope="col">Baseline value</th><th scope="col">Candidate value</th><th scope="col">Candidate − baseline</th></tr></thead>
          <tbody>
            {outcome.paired_differences.map((difference) => {
              const baseline = outcome.baseline_values[difference.block_index];
              const candidate = outcome.candidate_values[difference.block_index];
              return (
                <tr key={difference.block_index}>
                  <th scope="row">Pair {difference.block_index + 1}</th>
                  <td><Link to={`/runs/${encodeURIComponent(difference.baseline_run_id)}`} title={difference.baseline_run_id}>{shortDigest(difference.baseline_run_id)}<ArrowUpRight aria-hidden="true" /></Link></td>
                  <td><Link to={`/runs/${encodeURIComponent(difference.candidate_run_id)}`} title={difference.candidate_run_id}>{shortDigest(difference.candidate_run_id)}<ArrowUpRight aria-hidden="true" /></Link></td>
                  <td>{exactValue(baseline.value, outcome.unit)}</td>
                  <td>{exactValue(candidate.value, outcome.unit)}</td>
                  <td>{exactValue(difference.candidate_minus_baseline, outcome.unit, true)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="table-scroll comparison-outcome-table-wrap">
        <table className="comparison-outcome-table" aria-labelledby="comparison-estimate-heading">
          <caption>Controlled-comparison point estimate</caption>
          <thead><tr><th scope="col">Outcome</th><th scope="col">Baseline mean (n)</th><th scope="col">Candidate mean (n)</th><th scope="col">Candidate − baseline</th></tr></thead>
          <tbody><tr>
            <th scope="row">{label} · {humanize(outcome.selector.aggregation)}</th>
            <td>{displayValue(outcome.baseline_mean, outcome.unit)} <span>({outcome.baseline_values.length}; exact {exactValue(outcome.baseline_mean, outcome.unit)})</span></td>
            <td>{displayValue(outcome.candidate_mean, outcome.unit)} <span>({outcome.candidate_values.length}; exact {exactValue(outcome.candidate_mean, outcome.unit)})</span></td>
            <td>{displayValue(outcome.estimate, outcome.unit, true)} <span>(exact {exactValue(outcome.estimate, outcome.unit, true)})</span></td>
          </tr></tbody>
        </table>
      </div>

      <div className="comparison-inference-boundary">
        <CircleDotDashed aria-hidden="true" />
        <p>
          Each point represents one verified run-level measurement. Estimate = candidate arm mean − baseline arm mean. Signs show arithmetic direction only; colors represent arm roles only. No confidence interval, significance test, causal attribution, recommendation, winner, or acceptance verdict is produced.
        </p>
      </div>
    </Panel>
  );
}

function TrialSetEvidenceCard({ arm, plannedId, reference = null, trialSet }: {
  readonly arm: "BASELINE" | "CANDIDATE";
  readonly plannedId: string;
  readonly trialSet: TrialSetSummary | null;
  readonly reference?: {
    readonly trial_set_id: string;
    readonly trial_set_digest: string;
  } | null;
}) {
  return (
    <article className={`comparison-trial-card comparison-arm-${arm.toLowerCase()}`}>
      <div className="comparison-trial-card-head">
        <span className="comparison-arm-symbol" aria-hidden="true" />
        <div>
          <span>{armLabel(arm)} Trial Set</span>
          {trialSet ? (
            <Link to={`/trial-sets/${encodeURIComponent(trialSet.trial_set_id)}`}>
              {trialSet.title}<ArrowUpRight aria-hidden="true" />
            </Link>
          ) : <strong>{reference ? "Projection withheld" : "No verified result link"}</strong>}
        </div>
      </div>
      <dl>
        <DefinitionItem label="Trial Set ID" value={trialSet?.trial_set_id ?? reference?.trial_set_id ?? plannedId} mono />
        <DefinitionItem label="Member runs" value={trialSet?.member_count ?? (reference ? "Withheld" : "Planned")} mono />
        <DefinitionItem label="Digest" value={trialSet ? shortDigest(trialSet.trial_set_digest) : reference ? shortDigest(reference.trial_set_digest) : "Not available"} mono />
        <DefinitionItem label="Environment" value={trialSet ? humanize(trialSet.environment_status) : reference ? "Withheld" : "Not evaluated"} />
      </dl>
    </article>
  );
}

function scheduleEvidence(
  detail: ControlledComparisonDetail,
  slot: ControlledComparisonScheduleSlot,
): ReactNode {
  const progress = detail.execution.slots[slot.sequence_index];
  if (!detail.result) {
    if (progress?.state === "COMPLETE" && progress.verified_bundle) {
      return (
        <Link className="comparison-run-link" to={`/runs/${encodeURIComponent(slot.run_id)}`}>
          Verified<ArrowUpRight aria-hidden="true" />
        </Link>
      );
    }
    if (progress && ["FAILED", "INTERRUPTED", "INVALID"].includes(progress.state)) {
      return (
        <span className="comparison-schedule-state comparison-schedule-state-blocked">
          {humanize(progress.state)}
        </span>
      );
    }
    return (
      <span className="comparison-schedule-state">
        {progress && progress.state !== "PENDING" ? `Workspace · ${humanize(progress.state)}` : "Planned"}
      </span>
    );
  }
  if (detail.result.status === "INCOMPARABLE") {
    return <span className="comparison-schedule-state">Projection withheld</span>;
  }
  const observed = detail.result.control_checks.find((check) => check.check === "OBSERVED_SCHEDULE");
  if (observed?.status !== "SATISFIED") {
    return <span className="comparison-schedule-state">Not confirmed</span>;
  }
  return (
    <Link className="comparison-run-link" to={`/runs/${encodeURIComponent(slot.run_id)}`}>
      Observed<ArrowUpRight aria-hidden="true" />
    </Link>
  );
}

function ArmEvidence({ detail }: { readonly detail: ControlledComparisonDetail }) {
  return (
    <Panel className="comparison-evidence-panel">
      <SectionHeading
        title="Arm evidence"
        headingId="comparison-evidence-heading"
        meta={`${detail.plan.ordered_schedule.length} frozen schedule slots`}
      />
      <div className="comparison-trial-grid">
        <TrialSetEvidenceCard
          arm="BASELINE"
          plannedId={detail.plan.baseline_arm.planned_trial_set_id}
          trialSet={detail.baseline_trial_set}
          reference={detail.result?.baseline_trial_set}
        />
        <TrialSetEvidenceCard
          arm="CANDIDATE"
          plannedId={detail.plan.candidate_arm.planned_trial_set_id}
          trialSet={detail.candidate_trial_set}
          reference={detail.result?.candidate_trial_set}
        />
      </div>
      {detail.result?.status === "INCOMPARABLE" ? (
        <div className="comparison-evidence-withheld">
          <AlertTriangle aria-hidden="true" />
          <p>Trial Set summaries and run-level measurements are withheld because the comparison is <code>INCOMPARABLE</code>. Only immutable evidence references and the frozen schedule remain visible.</p>
        </div>
      ) : null}
      <div className="comparison-schedule-heading">
        <div>
          <Layers3 aria-hidden="true" />
          <div>
            <strong>Ordered execution schedule</strong>
            <span>
              {detail.execution.completed_run_count} of {detail.execution.planned_run_count} workspaces verified · {humanize(detail.execution.status)}
              {detail.execution.result_published ? " · result published" : ""}
            </span>
          </div>
        </div>
        <code>predeclared_permuted_pairs_v1</code>
      </div>
      <div className="table-scroll comparison-schedule-table-wrap">
        <table className="comparison-schedule-table" aria-labelledby="comparison-evidence-heading">
          <caption>Predeclared controlled-comparison execution schedule</caption>
          <thead><tr><th scope="col">Sequence</th><th scope="col">Block</th><th scope="col">Arm</th><th scope="col">Repeat</th><th scope="col">Run</th><th scope="col">Evidence</th></tr></thead>
          <tbody>
            {detail.plan.ordered_schedule.map((slot) => (
              <tr key={slot.run_id}>
                <td className="mono">{slot.sequence_index + 1}</td>
                <td className="mono">{slot.block_index + 1}</td>
                <td><span className={`comparison-arm-label comparison-arm-${slot.arm.toLowerCase()}`}><span className="comparison-arm-symbol" aria-hidden="true" />{armLabel(slot.arm)}</span></td>
                <td className="mono">{slot.repetition_index + 1}</td>
                <td><code title={slot.run_id}>{slot.run_id}</code></td>
                <td>{scheduleEvidence(detail, slot)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

export function ControlledComparisonDetailView() {
  const { comparisonPlanId } = useParams();
  const request = useControlledComparisonDetail(comparisonPlanId);

  if (request.status === "loading" || request.status === "idle") {
    return (
      <>
        <PageHeader title="Controlled comparison" subtitle="Reading the frozen plan and verified result." />
        <LoadingState label="Verifying the controlled comparison…" />
      </>
    );
  }
  if (request.status === "error" && request.error) {
    return (
      <>
        <Link className="back-link" to="/comparisons"><ArrowLeft aria-hidden="true" />Controlled comparisons</Link>
        <PageHeader title="Controlled comparison unavailable" subtitle="The requested plan is not present in the verified index." />
        <ErrorState error={request.error} retry={request.retry} title="The controlled comparison could not be loaded" />
      </>
    );
  }
  if (!request.data) return null;

  const detail = request.data;
  const outcome = detail.result?.status === "COMPARABLE" ? detail.result.outcomes[0] : null;

  return (
    <>
      <Link className="back-link" to="/comparisons"><ArrowLeft aria-hidden="true" />Controlled comparisons</Link>
      <PageHeader
        title={detail.summary.title}
        subtitle={`${detail.summary.experiment_id} · ${detail.summary.comparison_plan_id}`}
        action={
          <div className="badge-group">
            <StatusBadge status="PREDECLARED" label="PREDECLARED" tone="neutral" />
            <DetailStatus status={detail.summary.result_status} />
          </div>
        }
      />

      <PredeclaredDesign detail={detail} />
      <ComparabilityResult detail={detail} />
      {outcome ? <EstimateSection outcome={outcome} /> : null}
      <ArmEvidence detail={detail} />
    </>
  );
}
