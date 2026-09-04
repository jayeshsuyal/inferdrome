import {
  AlertTriangle,
  ArrowUpRight,
  GitFork,
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
import { useRoutingQualifications } from "../hooks/useRoutingQualifications";
import { shortDigest } from "../lib/format";
import { Link } from "../lib/router";
import type { RoutingQualificationSummary } from "../lib/types";

function QualificationIdentity({ qualification }: { readonly qualification: RoutingQualificationSummary }) {
  return (
    <>
      <Link
        className="routing-qualification-link"
        to={`/routing-qualifications/${encodeURIComponent(qualification.qualification_id)}`}
      >
        <span>{qualification.qualification_id}</span>
        <ArrowUpRight aria-hidden="true" />
      </Link>
      <span className="table-subtext mono" title={qualification.retained_digest}>
        {shortDigest(qualification.retained_digest)}
      </span>
    </>
  );
}

export function RoutingQualificationsView() {
  const request = useRoutingQualifications();
  const header = (
    <PageHeader
      title="Causal qualification"
      subtitle="A read-only cross-policy explanation of one sealed stale-telemetry experiment."
    />
  );

  if (request.status === "loading") {
    return <>{header}<LoadingState label="Verifying causal qualification…" /></>;
  }
  if (request.status === "error" && request.error) {
    return (
      <>
        {header}
        <ErrorState
          error={request.error}
          retry={request.retry}
          title="The causal qualification index is unavailable"
        />
      </>
    );
  }

  const qualifications = request.data?.routing_qualifications ?? [];
  const rejected = request.data?.rejected ?? [];
  return (
    <>
      {header}
      <Panel className="routing-qualifications-panel">
        <SectionHeading
          title="Verified qualification descriptors"
          headingId="routing-qualifications-heading"
          meta={`${qualifications.length} verified · ${rejected.length} withheld`}
        />
        <div className="routing-qualification-scope-note" role="note">
          <GitFork aria-hidden="true" />
          <div>
            <strong><code>MEASUREMENT_EVIDENCE_ONLY</code></strong>
            <p>
              This view compares declared treatments for one fixed virtual-time scenario. It records causal evidence, not a winner, recommendation, or acceptance verdict.
            </p>
          </div>
        </div>
        {qualifications.length === 0 ? (
          <EmptyState
            title="No verified causal qualification"
            message={
              rejected.length
                ? "The configured descriptor and source package were withheld before any causal facts were rendered."
                : "Configure a sealed source package, qualification root, and retained descriptor digest to inspect this bounded experiment."
            }
            kind={rejected.length ? "warning" : "empty"}
          />
        ) : (
          <div className="table-scroll routing-qualifications-table-wrap">
            <table className="routing-qualifications-table" aria-labelledby="routing-qualifications-heading">
              <caption>Verified stale-telemetry qualification descriptors</caption>
              <thead>
                <tr>
                  <th scope="col">Qualification</th>
                  <th scope="col">Source package</th>
                  <th scope="col">Repetitions</th>
                  <th scope="col">Population accounting</th>
                  <th scope="col">Integrity</th>
                </tr>
              </thead>
              <tbody>
                {qualifications.map((qualification) => (
                  <tr key={qualification.qualification_id}>
                    <td><QualificationIdentity qualification={qualification} /></td>
                    <td>
                      <code>{qualification.source_campaign_id}</code>
                      <span className="table-subtext mono" title={qualification.source_package_retained_digest}>
                        {shortDigest(qualification.source_package_retained_digest)}
                      </span>
                    </td>
                    <td className="number-cell mono">{qualification.repetitions_per_mode} cold trial / mode</td>
                    <td><code>{qualification.population_accounting}</code></td>
                    <td><StatusBadge status="VERIFIED" label="Replay + binding verified" tone="valid" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {rejected.length ? (
          <section className="routing-qualification-rejections" aria-labelledby="rejected-routing-qualifications-heading">
            <SectionHeading
              title="Withheld qualification"
              headingId="rejected-routing-qualifications-heading"
              meta="No causal facts rendered"
            />
            <div className="rejection-intro">
              <AlertTriangle aria-hidden="true" />
              <p>The configured source, descriptor, and retained digest did not form a verifiable pair.</p>
            </div>
            <ul className="rejected-list" aria-labelledby="rejected-routing-qualifications-heading">
              {rejected.map((item) => (
                <li key={`${item.entry}:${item.code}`}>
                  <ShieldCheck aria-hidden="true" />
                  <div>
                    <strong className="mono">{item.entry}</strong>
                    <span>{item.message}</span>
                  </div>
                  <div className="rejected-meta">
                    <StatusBadge status="WITHHELD" label="Withheld" tone="warning" />
                    <code>{item.code}</code>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </Panel>
    </>
  );
}
