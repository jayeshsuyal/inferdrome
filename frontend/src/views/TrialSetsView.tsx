import {
  AlertTriangle,
  ArrowUpRight,
  Layers3,
  Search,
  ShieldCheck,
} from "lucide-react";
import { useMemo, useState } from "react";

import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  Panel,
  SectionHeading,
  StatusBadge,
} from "../components/Primitives";
import { useTrialSets } from "../hooks/useTrialSets";
import { eligibilityLabel, formatDateTime, shortDigest } from "../lib/format";
import { Link } from "../lib/router";
import type { TrialSetSummary } from "../lib/types";

function TrialSetIdentity({ trialSet }: { readonly trialSet: TrialSetSummary }) {
  return (
    <>
      <Link
        className="trial-set-link"
        to={`/trial-sets/${encodeURIComponent(trialSet.trial_set_id)}`}
      >
        <span>{trialSet.title}</span>
        <ArrowUpRight aria-hidden="true" />
      </Link>
      <span className="table-subtext mono" title={trialSet.trial_set_id}>
        {trialSet.trial_set_id}
      </span>
    </>
  );
}

function EvidenceSummary({ values }: { readonly values: readonly string[] }) {
  return (
    <span className="trial-evidence-list">
      {values.map((value) => (
        <span key={value}>{eligibilityLabel(value)}</span>
      ))}
    </span>
  );
}

function TrialSetCard({ trialSet }: { readonly trialSet: TrialSetSummary }) {
  return (
    <article className="trial-set-card">
      <div className="trial-set-card-head">
        <div><TrialSetIdentity trialSet={trialSet} /></div>
        <StatusBadge
          status={trialSet.environment_status}
          label={trialSet.environment_status === "CONSISTENT" ? "No projected drift" : "Environment drift"}
          tone={trialSet.environment_status === "CONSISTENT" ? "neutral" : "warning"}
        />
      </div>
      <dl>
        <div><dt>Experiment</dt><dd>{trialSet.experiment_id}</dd></div>
        <div><dt>Model</dt><dd>{trialSet.model}</dd></div>
        <div><dt>Verified runs</dt><dd>{trialSet.member_count}</dd></div>
        <div><dt>Latest run</dt><dd>{formatDateTime(trialSet.latest_run_at)}</dd></div>
      </dl>
      <EvidenceSummary values={trialSet.evidence_eligibilities} />
    </article>
  );
}

export function TrialSetsView() {
  const request = useTrialSets();
  const [query, setQuery] = useState("");

  const filteredTrialSets = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!request.data || !needle) return request.data?.trial_sets ?? [];
    return request.data.trial_sets.filter((trialSet) => [
      trialSet.trial_set_id,
      trialSet.title,
      trialSet.experiment_id,
      trialSet.model,
      trialSet.execution_fingerprint,
      ...trialSet.evidence_eligibilities,
    ].join(" ").toLowerCase().includes(needle));
  }, [query, request.data]);

  if (request.status === "loading") {
    return (
      <>
        <PageHeader
          title="Trial sets"
          subtitle="Repeated verified runs grouped by one execution fingerprint."
        />
        <LoadingState label="Verifying trial-set declarations…" />
      </>
    );
  }

  if (request.status === "error" && request.error) {
    return (
      <>
        <PageHeader
          title="Trial sets"
          subtitle="Repeated verified runs grouped by one execution fingerprint."
        />
        <ErrorState
          error={request.error}
          retry={request.retry}
          title="The trial-set index is unavailable"
        />
      </>
    );
  }

  const trialSets = request.data?.trial_sets ?? [];
  const rejected = request.data?.rejected ?? [];

  return (
    <>
      <PageHeader
        title="Trial sets"
        subtitle="Repeated verified runs grouped by one execution fingerprint."
        action={
          <div className="badge-group">
            <StatusBadge status="RETROSPECTIVE" label="RETROSPECTIVE" tone="neutral" />
            <StatusBadge status="DESCRIPTIVE_ONLY" label="DESCRIPTIVE_ONLY" tone="info" />
          </div>
        }
      />

      <Panel className="trial-sets-panel">
        <SectionHeading
          title="Verified trial sets"
          headingId="trial-sets-heading"
          meta={`${filteredTrialSets.length} of ${trialSets.length}`}
        />

        <div className="trial-scope-note">
          <Layers3 aria-hidden="true" />
          <div>
            <strong><code>RETROSPECTIVE</code> · <code>DESCRIPTIVE_ONLY</code></strong>
            <p>
              These sets describe variation across previously completed runs. They do not establish experimental control or acceptance.
            </p>
          </div>
        </div>

        {trialSets.length ? (
          <div className="trial-set-controls" role="search">
            <label className="search-field">
              <span className="visually-hidden">Search trial sets</span>
              <Search aria-hidden="true" />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search title, experiment, model, or identity"
              />
            </label>
          </div>
        ) : null}

        {trialSets.length === 0 ? (
          <div className="trial-index-state">
            <EmptyState
              title="No verified trial sets"
              message={
                rejected.length
                  ? "Every discovered declaration was withheld. Review the bounded reasons below."
                  : "Create an immutable trial-set descriptor from verified runs to make repeated measurements visible here."
              }
              kind={rejected.length ? "warning" : "empty"}
            />
          </div>
        ) : filteredTrialSets.length ? (
          <>
            <div className="table-scroll trial-set-table-wrap">
              <table className="trial-set-table" aria-labelledby="trial-sets-heading">
                <caption>Verified Inferdrome trial sets</caption>
                <thead>
                  <tr>
                    <th scope="col">Trial set</th>
                    <th scope="col">Experiment / model</th>
                    <th scope="col">Evidence</th>
                    <th scope="col">Environment</th>
                    <th scope="col" className="number-cell">Runs</th>
                    <th scope="col">Latest run</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredTrialSets.map((trialSet) => (
                    <tr key={trialSet.trial_set_id}>
                      <td><TrialSetIdentity trialSet={trialSet} /></td>
                      <td>
                        <strong>{trialSet.experiment_id}</strong>
                        <span className="table-subtext">{trialSet.model}</span>
                      </td>
                      <td><EvidenceSummary values={trialSet.evidence_eligibilities} /></td>
                      <td>
                        <StatusBadge
                          status={trialSet.environment_status}
                          label={trialSet.environment_status === "CONSISTENT" ? "No projected drift" : "Drift detected"}
                          tone={trialSet.environment_status === "CONSISTENT" ? "neutral" : "warning"}
                        />
                      </td>
                      <td className="number-cell mono">{trialSet.member_count}</td>
                      <td>
                        {formatDateTime(trialSet.latest_run_at)}
                        <span className="table-subtext mono" title={trialSet.trial_set_digest}>
                          {shortDigest(trialSet.trial_set_digest)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="trial-set-card-list">
              {filteredTrialSets.map((trialSet) => (
                <TrialSetCard key={trialSet.trial_set_id} trialSet={trialSet} />
              ))}
            </div>
          </>
        ) : (
          <div className="trial-index-state">
            <EmptyState
              title="No trial sets match this search"
              message="Clear the search text or use a broader experiment, model, or identity term."
            />
          </div>
        )}

        {rejected.length ? (
          <div className="trial-rejections">
            <SectionHeading
              title="Withheld declarations"
              headingId="rejected-trial-sets-heading"
              meta="Run-level summaries not projected"
            />
            <div className="rejection-intro">
              <AlertTriangle aria-hidden="true" />
              <p>These entries did not satisfy bounded trial-set verification.</p>
            </div>
            <ul className="rejected-list" aria-labelledby="rejected-trial-sets-heading">
              {rejected.map((item) => (
                <li key={`${item.entry}:${item.code}`}>
                  <ShieldCheck aria-hidden="true" />
                  <div>
                    <strong className="mono">{item.entry}</strong>
                    <span>{item.message}</span>
                  </div>
                  <div className="rejected-meta">
                    <StatusBadge status="INVALID" label="Withheld" tone="warning" />
                    <code>{item.code}</code>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </Panel>
    </>
  );
}
