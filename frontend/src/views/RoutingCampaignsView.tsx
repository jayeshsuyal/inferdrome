import {
  AlertTriangle,
  ArrowUpRight,
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
import { useRoutingCampaigns } from "../hooks/useRoutingCampaigns";
import { shortDigest } from "../lib/format";
import { Link } from "../lib/router";
import type { RoutingCampaignSummary } from "../lib/types";

function CampaignIdentity({ campaign }: { readonly campaign: RoutingCampaignSummary }) {
  return (
    <>
      <Link
        className="routing-campaign-link"
        to={`/routing-campaigns/${encodeURIComponent(campaign.campaign_id)}`}
      >
        <span>{campaign.campaign_id}</span>
        <ArrowUpRight aria-hidden="true" />
      </Link>
      <span className="table-subtext mono" title={campaign.retained_digest}>
        {shortDigest(campaign.retained_digest)}
      </span>
    </>
  );
}

function CampaignCard({ campaign }: { readonly campaign: RoutingCampaignSummary }) {
  return (
    <article className="routing-campaign-card">
      <div className="routing-campaign-card-head">
        <div><CampaignIdentity campaign={campaign} /></div>
        <StatusBadge status="VERIFIED" label="Verified by replay" tone="valid" />
      </div>
      <dl>
        <div><dt>Execution</dt><dd><code>{campaign.execution_mode}</code></dd></div>
        <div><dt>Cold trials</dt><dd className="mono">{campaign.trial_count}</dd></div>
        <div><dt>Planned requests</dt><dd className="mono">{campaign.planned_request_count}</dd></div>
      </dl>
      <div className="routing-policy-list" aria-label="Recorded policies">
        {campaign.policy_ids.map((policy) => <code key={policy}>{policy}</code>)}
      </div>
    </article>
  );
}

export function RoutingCampaignsView() {
  const request = useRoutingCampaigns();

  const header = (
    <PageHeader
      title="Routing campaigns"
      subtitle="Read-only projections of sealed routing receipts after independent replay."
    />
  );

  if (request.status === "loading") {
    return <>{header}<LoadingState label="Verifying routing-campaign packages…" /></>;
  }
  if (request.status === "error" && request.error) {
    return (
      <>
        {header}
        <ErrorState
          error={request.error}
          retry={request.retry}
          title="The routing-campaign index is unavailable"
        />
      </>
    );
  }

  const campaigns = request.data?.routing_campaigns ?? [];
  const rejected = request.data?.rejected ?? [];
  return (
    <>
      {header}
      <Panel className="routing-campaigns-panel">
        <SectionHeading
          title="Verified routing campaigns"
          headingId="routing-campaigns-heading"
          meta={`${campaigns.length} verified · ${rejected.length} withheld`}
        />
        <div className="routing-campaign-scope-note">
          <Route aria-hidden="true" />
          <div>
            <strong><code>MEASUREMENT_EVIDENCE_ONLY</code></strong>
            <p>
              These receipts describe one synthetic CPU scenario. They make routing observations and terminal populations inspectable; they do not issue an acceptance or policy-quality verdict.
            </p>
          </div>
        </div>

        {campaigns.length === 0 ? (
          <div className="routing-campaign-index-state">
            <EmptyState
              title="No verified routing campaigns"
              message={
                rejected.length
                  ? "Every configured package was withheld before any campaign content was projected."
                  : "Provide one sealed routing-campaign package to inspect its verified receipts here."
              }
              kind={rejected.length ? "warning" : "empty"}
            />
          </div>
        ) : (
          <>
            <div className="table-scroll routing-campaign-table-wrap">
              <table className="routing-campaign-table" aria-labelledby="routing-campaigns-heading">
                <caption>Verified Inferdrome routing campaigns</caption>
                <thead>
                  <tr>
                    <th scope="col">Campaign</th>
                    <th scope="col">Execution</th>
                    <th scope="col">Policies</th>
                    <th scope="col" className="number-cell">Trials</th>
                    <th scope="col" className="number-cell">Requests</th>
                    <th scope="col">Verification</th>
                  </tr>
                </thead>
                <tbody>
                  {campaigns.map((campaign) => (
                    <tr key={campaign.campaign_id}>
                      <td><CampaignIdentity campaign={campaign} /></td>
                      <td><code>{campaign.execution_mode}</code></td>
                      <td>
                        <span className="routing-policy-list">
                          {campaign.policy_ids.map((policy) => <code key={policy}>{policy}</code>)}
                        </span>
                      </td>
                      <td className="number-cell mono">{campaign.trial_count}</td>
                      <td className="number-cell mono">{campaign.planned_request_count}</td>
                      <td><StatusBadge status="VERIFIED" label="Verified by replay" tone="valid" /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="routing-campaign-card-list">
              {campaigns.map((campaign) => <CampaignCard key={campaign.campaign_id} campaign={campaign} />)}
            </div>
          </>
        )}

        {rejected.length ? (
          <div className="routing-campaign-rejections">
            <SectionHeading
              title="Withheld packages"
              headingId="rejected-routing-campaigns-heading"
              meta="Campaign content not projected"
            />
            <div className="rejection-intro">
              <AlertTriangle aria-hidden="true" />
              <p>These configured entries did not satisfy the bounded reader before projection.</p>
            </div>
            <ul className="rejected-list" aria-labelledby="rejected-routing-campaigns-heading">
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
          </div>
        ) : null}
      </Panel>
    </>
  );
}
