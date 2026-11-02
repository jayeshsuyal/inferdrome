import { ArrowDown, ArrowDownToLine, ArrowUpRight, Check, Timer } from "lucide-react";
import { useState } from "react";

import aggregateUrl from "../../../docs/results/recovery-headroom-2026-10-19.json?url";
import { PageHeader, Panel, SectionHeading } from "../components/Primitives";
import { recoveryMetrics, recoveryReport } from "../lib/recovery-results";
import "./recovery-results.css";

const METHOD_URL = "https://github.com/jayeshsuyal/inferdrome/blob/main/docs/research/RECOVERY_HEADROOM.md";
const number = new Intl.NumberFormat("en-US");
type OrderFilter = "all" | "500" | "400";

export function RecoveryResultsView() {
  const [order, setOrder] = useState<OrderFilter>("all");
  const { design, primary, runtime, pairs } = recoveryReport;
  const metrics = recoveryMetrics;
  const visiblePairs = pairs.filter((pair) => order === "all" || pair.order_ms[0] === Number(order));
  const chartMaximum = Math.ceil(Math.max(...pairs.map((pair) => pair.fewer_misses)) / 10) * 10;

  return (
    <div className="recovery-results">
      <PageHeader
        title="Recovery results"
        subtitle="Can 100 ms of admission headroom help when an LLM worker restarts?"
        action={(
          <a className="button button-secondary" href={aggregateUrl} download="recovery-headroom-2026-10-19.json">
            <ArrowDownToLine aria-hidden="true" /> Download results
          </a>
        )}
      />

      <div className="recovery-source">
        <span className="recovery-source-mark" aria-hidden="true" />
        <span>Recorded GPU experiment</span>
        <span className="recovery-source-date">19 Oct 2026 · Published aggregate · Read only</span>
      </div>

      <Panel className="recovery-hero">
        <div className="recovery-hero-copy">
          <span className="recovery-eyebrow">Primary recovery outcome</span>
          <h2><span>{metrics.relativeMissReductionPercent.toFixed(1)}%</span> fewer SLO misses</h2>
          <p>
            Stop forwarding at 400 ms instead of 500 ms. Keep the same
            500 ms first-token deadline and immediate worker return.
          </p>
          <div className="recovery-pair-proof">
            <Check aria-hidden="true" />
            {metrics.pairsFavoringGuard} of {design.pairs} matched pairs favored the guard
          </div>
        </div>
        <div className="recovery-miss-comparison" aria-label="Recovery misses by forwarding cutoff">
          <div>
            <span>500 ms cutoff <small>Baseline</small></span>
            <strong>{number.format(primary["500ms"].misses)}</strong>
            <div className="recovery-total-track" aria-hidden="true">
              <span className="recovery-baseline-bar" style={{ width: `${primary["500ms"].misses / primary["500ms"].offers * 100}%` }} />
            </div>
          </div>
          <div>
            <span>400 ms cutoff <small>100 ms headroom</small></span>
            <strong>{number.format(primary["400ms"].misses)}</strong>
            <div className="recovery-total-track" aria-hidden="true">
              <span className="recovery-guard-bar" style={{ width: `${primary["400ms"].misses / primary["400ms"].offers * 100}%` }} />
            </div>
          </div>
          <p><ArrowDown aria-hidden="true" /> {number.format(metrics.fewerMisses)} fewer misses across {number.format(design.primary_recovery_offers_per_arm)} recovery offers per arm</p>
        </div>
      </Panel>

      <dl className="recovery-stats">
        <div>
          <dt>SLO attainment</dt>
          <dd>{metrics.baselineAttainmentPercent.toFixed(2)}% <span aria-label="to">→</span> {metrics.guardAttainmentPercent.toFixed(2)}%</dd>
          <span>+{metrics.attainmentGainPoints.toFixed(2)} percentage points</span>
        </div>
        <div>
          <dt>Fixed comparison</dt>
          <dd>{design.trials} trials <span> / </span> {design.pairs} pairs</dd>
          <span>Six pairs in each execution order</span>
        </div>
        <div>
          <dt>Total scheduled offers</dt>
          <dd>{number.format(design.total_offers)}</dd>
          <span>{number.format(design.primary_recovery_offers)} recovery + {number.format(design.history_offers)} history</span>
        </div>
      </dl>

      <Panel className="recovery-pairs-panel">
        <div className="recovery-pairs-heading">
          <div>
            <SectionHeading title="Every matched pair" />
            <p>Same workload. {pairs[0].offers_per_arm} recovery offers per arm, per pair. Lower miss counts are better.</p>
          </div>
          <div className="recovery-order-filter" role="group" aria-label="Filter pairs by execution order">
            {([
              ["all", "All 12"],
              ["500", "500 ms first"],
              ["400", "400 ms first"],
            ] as const).map(([value, label]) => (
              <button key={value} type="button" aria-pressed={order === value} onClick={() => setOrder(value)}>{label}</button>
            ))}
          </div>
        </div>
        <div className="recovery-table-scroll" role="region" aria-label="Matched-pair results" tabIndex={0}>
          <table className="recovery-pair-table">
            <caption className="visually-hidden">
              Recovery SLO misses for the 500 ms baseline and 400 ms guard. Reduction is baseline misses minus guard misses.
            </caption>
            <thead>
              <tr>
                <th scope="col">Pair<span className="recovery-mobile-order">Run order</span></th>
                <th scope="col">Run order</th>
                <th scope="col">500 ms <span>misses</span></th>
                <th scope="col">400 ms <span>misses</span></th>
                <th scope="col" className="recovery-reduction-heading">Fewer misses <span>Scale: 0–{chartMaximum}</span></th>
              </tr>
            </thead>
            <tbody>
              {visiblePairs.map((pair) => (
                <tr key={pair.pair}>
                  <th scope="row">
                    {String(pair.pair).padStart(2, "0")}
                    <span className="recovery-mobile-order">{pair.order_ms.join("→")}</span>
                  </th>
                  <td className="recovery-run-order">{pair.order_ms.join(" → ")} ms</td>
                  <td>{pair.misses_500ms}</td>
                  <td>{pair.misses_400ms}</td>
                  <td>
                    <div className="recovery-pair-reduction">
                      <div className="recovery-pair-track" aria-hidden="true"><span style={{ width: `${pair.fewer_misses / chartMaximum * 100}%` }} /></div>
                      <strong>{pair.fewer_misses}</strong>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="recovery-chart-note" role="status">
          Showing {visiblePairs.length} of {pairs.length} pairs. Bars show 500 ms misses minus 400 ms misses; headline totals always include all pairs.
        </p>
      </Panel>

      <div className="recovery-details-grid">
        <Panel className="recovery-method">
          <SectionHeading title="What changed?" />
          <p>
            The guard stops forwarding once 100 ms remains in the first-token budget.
            Both arms use immediate return after the restarted worker becomes healthy,
            with the same admission cap of {design.per_replica_admission_cap_after_loss} per replica after peer loss.
          </p>
          <div className="recovery-deadline"><Timer aria-hidden="true" /><strong>Same SLO in both arms</strong><span>{design.first_token_deadline_ms} ms first token · {design.completion_deadline_ms / 1000} s completion</span></div>
          <p className="recovery-scoring">Measured from scheduled arrival. Rejections, timeouts, missing outcomes, and errors all count as misses.</p>
          <dl className="recovery-method-facts">
            <div><dt>Model / runtime</dt><dd>Qwen3-8B · vLLM {runtime.vllm}</dd></div>
            <div><dt>Hardware</dt><dd>2 × A100 PCIe 40 GB · one physical host</dd></div>
            <div><dt>Workload</dt><dd>{design.offered_requests_per_second} requests/s · {number.format(design.document_prefix_tokens)}-token prefixes · {design.output_tokens} output tokens</dd></div>
            <div><dt>Each trial</dt><dd>{design.history_seconds_per_trial} s history + {design.recovery_seconds_per_trial} s recovery</dd></div>
          </dl>
          <a className="recovery-method-link" href={METHOD_URL} target="_blank" rel="noreferrer">Read the experiment method <ArrowUpRight aria-hidden="true" /></a>
        </Panel>
        <Panel className="recovery-limits">
          <SectionHeading title="What this result supports" />
          <p>A measured recovery improvement on one host and one synthetic workload.</p>
          <ul>
            <li>The guard reaches <strong>{metrics.guardAttainmentPercent.toFixed(2)}% attainment</strong>, still below the 95% target.</li>
            <li>All {design.pairs} primary pairs favor the guard. In the secondary first 30 seconds, {recoveryReport.secondary_first30.pairs_favoring400ms} of {design.pairs} do.</li>
            <li>Repeated pairs on one host do not establish independent results across hosts or production incidents.</li>
            <li>This view uses the published aggregate. Raw captures and the research runner remain private.</li>
          </ul>
          <p className="recovery-boundary">Historical results, separate from the synthetic local demo. No production SLA or customer acceptance claim.</p>
        </Panel>
      </div>
    </div>
  );
}
