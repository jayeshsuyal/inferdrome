import {
  AlertTriangle,
  ArrowUpRight,
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
import { useRoutingExecutions } from "../hooks/useRoutingExecutions";
import { humanize, shortDigest } from "../lib/format";
import { Link } from "../lib/router";
import type { RoutingExecutionSummary } from "../lib/types";

function executionContext(summary: RoutingExecutionSummary): string {
  if (summary.mode === "LAMBDA_MANUAL_HOST") {
    return "Manual host declaration · not provider proof";
  }
  if (summary.mode === "LOCAL_LOOPBACK") return "Local loopback · not GPU evidence";
  return "Private target mode · evidence record only";
}

function ExecutionIdentity({ execution }: { readonly execution: RoutingExecutionSummary }) {
  return (
    <>
      <Link
        className="execution-link"
        to={`/routing-executions/${encodeURIComponent(execution.execution_id)}`}
      >
        <span>{execution.execution_id}</span>
        <ArrowUpRight aria-hidden="true" />
      </Link>
      <span className="table-subtext mono" title={execution.retained_digest}>
        {shortDigest(execution.retained_digest)}
      </span>
    </>
  );
}

function ExecutionCard({ execution }: { readonly execution: RoutingExecutionSummary }) {
  return (
    <article className="execution-card">
      <div className="execution-card-head">
        <div><ExecutionIdentity execution={execution} /></div>
        <StatusBadge status="VERIFIED" label="Offline replay verified" tone="valid" />
      </div>
      <dl>
        <div><dt>Mode</dt><dd><code>{execution.mode}</code></dd></div>
        <div><dt>Profile</dt><dd><code>{execution.topology.accelerator_model}</code></dd></div>
        <div><dt>Trace</dt><dd className="mono">{execution.trial_count} × {execution.request_denominator_per_trial} = {execution.terminal_denominator}</dd></div>
      </dl>
      <p className="execution-card-note">{executionContext(execution)}</p>
    </article>
  );
}

export function RoutingExecutionsView() {
  const request = useRoutingExecutions();
  const header = (
    <PageHeader
      title="Routing executions"
      subtitle="Verified request-level evidence from one sealed two-endpoint execution package."
    />
  );

  if (request.status === "loading") {
    return <>{header}<LoadingState label="Verifying routing-execution package and replay…" /></>;
  }
  if (request.status === "error" && request.error) {
    return <>{header}<ErrorState error={request.error} retry={request.retry} title="The routing-execution index is unavailable" /></>;
  }

  const executions = request.data?.routing_executions ?? [];
  const rejected = request.data?.rejected ?? [];
  return (
    <>
      {header}
      <Panel className="executions-panel">
        <SectionHeading
          title="Verified execution ledger"
          headingId="routing-executions-heading"
          meta={`${executions.length} verified · ${rejected.length} withheld`}
        />
        <div className="execution-scope-note" role="note">
          <ShieldCheck aria-hidden="true" />
          <div>
            <strong>Verification precedes projection</strong>
            <p>
              The reader checks the closed package, digest binding, receipt closure, and offline replay before showing any routing fact. It records evidence; it does not issue a routing, quality, or acceptance verdict.
            </p>
          </div>
        </div>

        {executions.length === 0 ? (
          <div className="execution-index-state">
            <EmptyState
              title="No verified routing execution"
              message={
                rejected.length
                  ? "The configured package was withheld before any execution content was projected."
                  : "Configure one sealed routing-execution package and its retained digest to inspect it here."
              }
              kind={rejected.length ? "warning" : "empty"}
            />
          </div>
        ) : (
          <>
            <div className="table-scroll execution-table-wrap">
              <table className="execution-table" aria-labelledby="routing-executions-heading">
                <caption>Verified routing-execution packages</caption>
                <thead>
                  <tr>
                    <th scope="col">Execution / integrity</th>
                    <th scope="col">Runtime and model</th>
                    <th scope="col">Declared topology</th>
                    <th scope="col">Complete population</th>
                    <th scope="col">Interpretation boundary</th>
                  </tr>
                </thead>
                <tbody>
                  {executions.map((execution) => (
                    <tr key={execution.execution_id}>
                      <td>
                        <ExecutionIdentity execution={execution} />
                        <StatusBadge status="VERIFIED" label="Offline replay verified" tone="valid" />
                      </td>
                      <td>
                        <code>{execution.runtime.runtime_name} {execution.runtime.runtime_version}</code>
                        <span className="table-subtext">{execution.model.model_id}</span>
                        <span className="table-subtext mono" title={execution.source_commit}>{shortDigest(execution.source_commit)}</span>
                      </td>
                      <td>
                        <code>{execution.mode}</code>
                        <span className="table-subtext">{execution.topology.accelerator_count} × {execution.topology.accelerator_model}</span>
                      </td>
                      <td className="execution-population-cell">
                        <strong className="mono">{execution.terminal_denominator}</strong>
                        <span>{execution.trial_count} policy trials · {execution.request_denominator_per_trial} each</span>
                      </td>
                      <td><span className="execution-boundary-label">{executionContext(execution)}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="execution-card-list">
              {executions.map((execution) => <ExecutionCard key={execution.execution_id} execution={execution} />)}
            </div>
          </>
        )}

        {rejected.length ? (
          <div className="execution-rejections">
            <SectionHeading title="Withheld package" meta="No package content was rendered" />
            {rejected.map((entry) => (
              <div className="execution-rejection" key={`${entry.entry}-${entry.code}`}>
                <AlertTriangle aria-hidden="true" />
                <div>
                  <strong>{humanize(entry.code)}</strong>
                  <span>{entry.message}</span>
                </div>
              </div>
            ))}
          </div>
        ) : null}
      </Panel>
      <div className="execution-reading-guide">
        <Route aria-hidden="true" />
        <div>
          <strong>Read the causal chain, not a scorecard.</strong>
          <span>Open a verified package to trace observation age and admissibility through candidate state, endpoint selection, fallback, and terminal outcome.</span>
        </div>
        <Fingerprint aria-hidden="true" />
      </div>
    </>
  );
}
