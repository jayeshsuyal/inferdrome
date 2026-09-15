import { ArrowUpRight, RefreshCw } from "lucide-react";
import { useState } from "react";

import { EvaluationTable } from "../components/EvaluationData";
import { EmptyState, ErrorState, LoadingState, PageHeader, Panel, SectionHeading, StatusBadge } from "../components/Primitives";
import { useEvaluations } from "../hooks/useEvaluations";
import type { EvaluationKind } from "../lib/evaluations";
import { humanize, shortDigest } from "../lib/format";
import { Link } from "../lib/router";

export function EvaluationsView() {
  const request = useEvaluations();
  const [kind, setKind] = useState<"ALL" | EvaluationKind>("ALL");
  const header = <PageHeader
    title="Evaluations"
    subtitle="Bounded read-only study and prefix-cache reports, with separate coverage and comparison status."
    action={<button type="button" className="button button-secondary" onClick={request.retry}><RefreshCw aria-hidden="true" />Refresh reports</button>}
  />;
  if (request.status === "loading") return <>
    {header}
    <LoadingState label="Reading pinned evaluation reports…" />
  </>;
  if (request.status === "error" && request.error) return <>
    {header}
    <ErrorState error={request.error} retry={request.retry} title="The evaluation-report index is unavailable" />
  </>;
  const reports = request.data?.reports ?? [];
  const rejected = request.data?.rejected ?? [];
  const visible = reports.filter((report) => kind === "ALL" || report.kind === kind);
  return <>
    {header}

    <div className="evaluation-boundary" role="note">
      <strong>Pinned report; source inputs not replayed</strong>
      <p>Integrity and contract checks do not establish reducer authorship, actual execution or cache treatment. Runtime remains unverified and evidence eligibility remains false.</p>
    </div>

    <Panel className="evaluation-panel">
      <SectionHeading title="Pinned evaluation reports" meta={`${reports.length} available · ${rejected.length} withheld`} />

      <div className="evaluation-controls">
        <label>Report kind<select aria-label="Report kind" value={kind} onChange={(event) => setKind(event.target.value as "ALL" | EvaluationKind)}>
          <option value="ALL">All reports</option>
          <option value="STUDY">Study</option>
          <option value="PREFIX_CACHE">Prefix cache</option>
        </select></label>
      </div>

      {visible.length ? <EvaluationTable caption="Pinned study and prefix-cache reports" columns={["Report", "Kind", "Execution", "Comparison", "Measurements"]}>

        {visible.map((report) => <tr key={report.report_id}>

          <th scope="row" data-label="Report">
            <Link className="evaluation-identity" to={`/evaluations/${report.report_id}`}>
              {report.label}
              <ArrowUpRight aria-hidden="true" />
            </Link>
            <span className="table-subtext mono" title={report.report_sha256}>
              {shortDigest(report.report_sha256)}
            </span>
          </th>

          <td data-label="Kind">
            <span>
              {report.kind === "STUDY" ? "Study" : "Prefix cache"}
            </span>
          </td>

          <td data-label="Execution">
            <StatusBadge status={report.status} label={humanize(report.status)} />
          </td>

          <td data-label="Comparison">
            <span>
              {humanize(report.comparison_status)}
            </span>
          </td>

          <td data-label="Measurements">
            <span>
              {report.returned_records === 0 ? "No returned measurements" : `${report.returned_records} returned records`}
            </span>
            <span className="table-subtext">
              {report.evidence_class === "SYNTHETIC_ONLY" ? "Synthetic only" : report.evidence_class === "LOCAL_MEASUREMENT_ONLY" ? "Local measurement only; execution unverified" : "Evidence class unavailable"}
            </span>
          </td>
        </tr>)}
      </EvaluationTable> : <EmptyState
        title={reports.length ? "No reports match this kind" : "No available evaluation reports"}
        message={rejected.length ? "Configured reports were withheld before their content could be projected." : reports.length ? "Choose another report kind to inspect the available entries." : "No retained evaluation reports are configured. This viewer does not run experiments or generate reports."}
      />}

      {rejected.length ? <div className="evaluation-section">
        <h3>Withheld reports</h3>
        <ul className="evaluation-withheld">
          {rejected.map((item) => <li key={item.entry}>
            <strong>Configured report {item.entry}</strong>
            <StatusBadge status="WITHHELD" tone="warning" />
            <code>
              {item.code}
            </code>
            <span>
              {item.message}
            </span>
          </li>)}
        </ul>
      </div> : null}
    </Panel>
  </>;
}
