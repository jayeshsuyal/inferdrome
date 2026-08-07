import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  DatabaseZap,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import type { ReactNode } from "react";

import { eligibilityLabel, humanize } from "../lib/format";
import type { EvidenceEligibility, IntegrityStatus } from "../lib/types";

export type StatusTone = "valid" | "warning" | "info" | "danger" | "neutral";

function toneForStatus(status: string): StatusTone {
  if (status === "VALID" || status === "CUSTOMER_ELIGIBLE" || status === "PASSED") return "valid";
  if (status === "INELIGIBLE" || status === "PARTIAL" || status === "COMPARABLE_WITH_CONTEXT_CHANGES") return "warning";
  if (status === "SYNTHETIC_ONLY" || status === "COMPARABLE") return "info";
  if (status === "INVALID" || status === "FAILED") return "danger";
  return "neutral";
}

export function StatusBadge({
  status,
  label,
  tone,
}: {
  readonly status: string;
  readonly label?: string;
  readonly tone?: StatusTone;
}) {
  const resolvedLabel =
    label ||
    (["CUSTOMER_ELIGIBLE", "SYNTHETIC_ONLY", "INELIGIBLE"].includes(status)
      ? eligibilityLabel(status)
      : humanize(status));
  return (
    <span className={`status-badge status-${tone ?? toneForStatus(status)}`}>
      <span className="status-dot" aria-hidden="true" />
      {resolvedLabel}
    </span>
  );
}
export function EvidenceBadge({ eligibility }: { readonly eligibility: EvidenceEligibility }) {
  return <StatusBadge status={eligibility} />;
}

export function IntegrityBadge({ integrity }: { readonly integrity: IntegrityStatus }) {
  return <StatusBadge status={integrity} label={`Integrity ${integrity.toLowerCase().replaceAll("_", " ")}`} />;
}

export function PageHeader({
  title,
  subtitle,
  action,
}: {
  readonly title: string;
  readonly subtitle: string;
  readonly action?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        <p>{subtitle}</p>
      </div>
      {action ? <div className="page-header-action">{action}</div> : null}
    </header>
  );
}

export function Panel({
  children,
  className = "",
  as: Element = "section",
}: {
  readonly children: ReactNode;
  readonly className?: string;
  readonly as?: "section" | "article" | "div";
}) {
  return <Element className={`panel ${className}`.trim()}>{children}</Element>;
}

export function SectionHeading({
  title,
  meta,
  headingId,
}: {
  readonly title: string;
  readonly meta?: ReactNode;
  readonly headingId?: string;
}) {
  return (
    <div className="section-heading">
      <h2 id={headingId}>{title}</h2>
      {meta ? <div className="section-meta">{meta}</div> : null}
    </div>
  );
}

export function LoadingState({ label = "Reading verified evidence…" }: { readonly label?: string }) {
  return (
    <div className="state-panel" role="status" aria-live="polite">
      <CircleDashed className="state-icon loading-icon" aria-hidden="true" />
      <strong>{label}</strong>
      <span>Inferdrome is preparing a read-only projection.</span>
    </div>
  );
}

export function ErrorState({
  error,
  retry,
  title = "Evidence could not be loaded",
}: {
  readonly error: Error;
  readonly retry: () => void;
  readonly title?: string;
}) {
  return (
    <div className="state-panel state-error" role="alert">
      <ShieldAlert className="state-icon" aria-hidden="true" />
      <strong>{title}</strong>
      <span>{error.message}</span>
      <button className="button button-secondary" type="button" onClick={retry}>
        <RefreshCw aria-hidden="true" />
        Try again
      </button>
    </div>
  );
}

export function EmptyState({
  title,
  message,
  kind = "empty",
}: {
  readonly title: string;
  readonly message: string;
  readonly kind?: "empty" | "warning" | "success";
}) {
  const Icon = kind === "warning" ? AlertTriangle : kind === "success" ? CheckCircle2 : DatabaseZap;
  return (
    <div className={`state-panel state-${kind}`}>
      <Icon className="state-icon" aria-hidden="true" />
      <strong>{title}</strong>
      <span>{message}</span>
    </div>
  );
}
