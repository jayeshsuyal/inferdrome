import { Archive, EyeOff, Fingerprint, ShieldCheck } from "lucide-react";

import {
  EmptyState,
  ErrorState,
  EvidenceBadge,
  IntegrityBadge,
  LoadingState,
  PageHeader,
  Panel,
  SectionHeading,
  StatusBadge,
} from "../components/Primitives";
import { useRuns } from "../context/RunsContext";
import { useRunDetail } from "../hooks/useRunDetail";
import { useNavigate, useParams } from "../lib/router";
import {
  formatBytes,
  formatPrimitive,
  humanize,
  shortDigest,
} from "../lib/format";

export function EvidenceView() {
  const params = useParams();
  const navigate = useNavigate();
  const runIndex = useRuns();
  const selectedRunId = params.runId || runIndex.runs[0]?.run_id || null;
  const request = useRunDetail(selectedRunId);

  if (!params.runId && runIndex.status === "loading") {
    return (
      <>
        <PageHeader title="Evidence" subtitle="Verification, provenance, and sealed artifact inventory." />
        <LoadingState label="Selecting the latest verified bundle…" />
      </>
    );
  }
  if (!params.runId && runIndex.status === "error" && runIndex.error) {
    return (
      <>
        <PageHeader title="Evidence" subtitle="Verification, provenance, and sealed artifact inventory." />
        <ErrorState error={runIndex.error} retry={runIndex.retry} title="The evidence index is unavailable" />
      </>
    );
  }
  if (!selectedRunId) {
    return (
      <>
        <PageHeader title="Evidence" subtitle="Verification, provenance, and sealed artifact inventory." />
        <EmptyState title="No verified bundle selected" message="Open a verified run before inspecting its evidence." />
      </>
    );
  }
  if (request.status === "loading") {
    return (
      <>
        <PageHeader title="Evidence" subtitle={`Opening sealed evidence for ${selectedRunId}.`} />
        <LoadingState label="Verifying the bundle projection…" />
      </>
    );
  }
  if (request.status === "error" && request.error) {
    return (
      <>
        <PageHeader title="Evidence" subtitle={`Opening sealed evidence for ${selectedRunId}.`} />
        <ErrorState error={request.error} retry={request.retry} title="This evidence bundle could not be opened" />
      </>
    );
  }
  if (!request.data) return null;

  const detail = request.data;
  const run = detail.summary;

  return (
    <>
      <PageHeader
        title="Evidence"
        subtitle={`Why ${run.run_id} is integrity-valid, what it contains, and where Inferdrome’s authority ends.`}
        action={
          <div className="evidence-header-actions">
            {runIndex.runs.length > 1 ? (
              <label className="compact-select">
                <span className="visually-hidden">Evidence run</span>
                <select
                  value={run.run_id}
                  onChange={(event) => navigate(`/evidence/${encodeURIComponent(event.target.value)}`)}
                  aria-label="Evidence run"
                >
                  {runIndex.runs.map((item) => (
                    <option key={item.run_id} value={item.run_id}>{item.run_id}</option>
                  ))}
                </select>
              </label>
            ) : null}
            <IntegrityBadge integrity={detail.verification.integrity_status} />
          </div>
        }
      />

      <div className="evidence-summary">
        <Panel>
          <SectionHeading title="Offline verification" meta={<StatusBadge status={detail.verification.integrity_status} />} />
          <dl className="verification-stats">
            <div><dt>Bundle digest</dt><dd className="mono" title={detail.verification.bundle_digest}>{shortDigest(detail.verification.bundle_digest)}</dd></div>
            <div><dt>Artifacts</dt><dd>{detail.verification.artifact_count.toLocaleString()}</dd></div>
            <div><dt>Sealed bytes</dt><dd>{formatBytes(detail.verification.total_bytes)}</dd></div>
            <div><dt>Authoritative recalculation</dt><dd>{formatPrimitive(detail.verification.verified_by_recalculation)}</dd></div>
          </dl>
          <div className="inline-note verification-note">
            <ShieldCheck aria-hidden="true" />
            <span>
              <strong>Stored and recalculated measurements agree.</strong>
              This projection is emitted only after the bundle verifier and reducer complete successfully.
            </span>
          </div>
        </Panel>

        <Panel>
          <SectionHeading title="Eligibility" meta={<EvidenceBadge eligibility={run.evidence_eligibility} />} />
          <dl className="fact-list">
            <div><dt>Execution mode</dt><dd>{humanize(run.execution_mode)}</dd></div>
            <div><dt>Environment</dt><dd>{humanize(detail.verification.environment_completeness)}</dd></div>
            <div><dt>Replayability</dt><dd>{humanize(detail.verification.replayability)}</dd></div>
            <div><dt>Producer</dt><dd className="mono">{run.producer_name} {run.producer_version}</dd></div>
            <div><dt>Native response content</dt><dd>{formatPrimitive(detail.sensitivity.native_response_content_present)}</dd></div>
            <div><dt>Canonical response content</dt><dd>{formatPrimitive(detail.sensitivity.canonical_response_content_included)}</dd></div>
          </dl>
          <div className="content-boundary-note">
            <EyeOff aria-hidden="true" />
            Response-bearing artifacts are identified by sensitivity metadata; this dashboard never renders their contents.
          </div>
        </Panel>
      </div>

      <Panel className="provenance-panel">
        <SectionHeading
          title="Environment provenance"
          headingId="provenance-heading"
          meta={`${detail.environment.length} allowlisted fields`}
        />
        {detail.environment.length ? (
          <ul className="provenance-grid" aria-labelledby="provenance-heading">
            {detail.environment.map((field) => (
              <li key={field.name}>
                <div>
                  <strong>{field.label}</strong>
                  <span className="mono" title={formatPrimitive(field.value)}>{formatPrimitive(field.value)}</span>
                </div>
                <StatusBadge
                  status={field.provenance}
                  label={humanize(field.provenance)}
                  tone={field.provenance === "UNKNOWN" ? "neutral" : "info"}
                />
              </li>
            ))}
          </ul>
        ) : (
          <div className="panel-body">
            <EmptyState title="No environment provenance projected" message="No response content has been exposed." />
          </div>
        )}
      </Panel>

      <Panel className="artifact-panel">
        <SectionHeading
          title="Artifact inventory"
          headingId="artifact-heading"
          meta={`${detail.artifacts.length} sealed files`}
        />
        {detail.artifacts.length ? (
          <div className="table-scroll artifact-table-wrap">
            <table aria-labelledby="artifact-heading" className="artifact-table">
              <caption>Files declared by the verified Inferdrome bundle</caption>
              <thead>
                <tr>
                  <th scope="col">Artifact</th>
                  <th scope="col">Role</th>
                  <th scope="col">Sensitivity</th>
                  <th scope="col" className="number-cell">Size</th>
                  <th scope="col">Content</th>
                </tr>
              </thead>
              <tbody>
                {detail.artifacts.map((artifact) => (
                  <tr key={artifact.path}>
                    <td>
                      <strong className="mono">{artifact.path}</strong>
                      <span className="table-subtext">{artifact.media_type}</span>
                    </td>
                    <td>{humanize(artifact.role)}</td>
                    <td>
                      <StatusBadge
                        status={artifact.sensitivity}
                        label={humanize(artifact.sensitivity)}
                        tone={artifact.sensitivity === "PUBLIC" ? "neutral" : "warning"}
                      />
                    </td>
                    <td className="number-cell mono">{formatBytes(artifact.size_bytes)}</td>
                    <td><span className="content-hidden"><EyeOff aria-hidden="true" /> Not exposed</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="panel-body">
            <EmptyState title="No artifact metadata returned" message="No file contents have been exposed." />
          </div>
        )}
      </Panel>

      <Panel className="digest-panel">
        <SectionHeading title="Digest domains" meta="Frozen identities" />
        <dl className="digest-grid">
          <div><dt>Source spec</dt><dd title={detail.digests.source_spec_digest}>{shortDigest(detail.digests.source_spec_digest)}</dd></div>
          <div><dt>Execution fingerprint</dt><dd title={detail.digests.execution_fingerprint}>{shortDigest(detail.digests.execution_fingerprint)}</dd></div>
          <div><dt>Request plan</dt><dd title={detail.digests.request_plan_digest}>{shortDigest(detail.digests.request_plan_digest)}</dd></div>
          <div><dt>Metric definitions</dt><dd title={detail.digests.metric_definitions_digest}>{shortDigest(detail.digests.metric_definitions_digest)}</dd></div>
          <div><dt>ExitSpec contract</dt><dd title={detail.digests.exitspec_contract_digest ?? undefined}>{shortDigest(detail.digests.exitspec_contract_digest)}</dd></div>
        </dl>
      </Panel>

      <section className="acceptance-boundary" aria-labelledby="acceptance-boundary-title">
        <div className="boundary-icon"><Fingerprint aria-hidden="true" /></div>
        <div>
          <h2 id="acceptance-boundary-title">Independent acceptance starts after this point</h2>
          <p>
            Inferdrome supplies measurements, provenance, and integrity. An external ExitSpec process owns PASS, FAIL, and NOT_PROVEN. This dashboard does not infer an acceptance verdict.
          </p>
        </div>
        <StatusBadge status="EXTERNAL_BOUNDARY" label="External boundary" tone="neutral" />
      </section>

      <p className="index-footnote">
        <Archive aria-hidden="true" /> The sealed bundle remains immutable. This view only presents bounded metadata returned by the local API.
      </p>
    </>
  );
}
