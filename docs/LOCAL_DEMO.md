# Inferdrome local product demo

Status: **Recording-ready synthetic product walkthrough**

This demo runs the real Inferdrome CLI, evidence pipeline, comparison executor,
offline verifier, API, and packaged dashboard on one workstation. It does not
mock the UI or return canned API responses.

The claim boundary is absolute: every generated run is `SYNTHETIC_ONLY`. This
demonstrates product mechanics and integrity behavior, not genuine GPU
performance, customer-eligible evidence, trusted execution, causality, or a
preferred configuration.

## One command

Prepare the local development runtime once:

```bash
uv sync --extra dev --extra dashboard
```

Then, from the repository root, run:

```bash
./scripts/run_local_demo.py
```

The command creates a persistent `~/.inferdrome/local-demo` workspace outside
the Git checkout, freezes one two-arm comparison, executes four synthetic runs
through the production CLI, publishes both Trial Sets and the comparison
result, independently reverifies the result, prints the claim boundary, and
opens the loopback dashboard at `http://127.0.0.1:8787`. Press Ctrl-C to stop
the server.

The controlled result is intentionally `INCOMPARABLE`. Four controls close.
`COMPLETE_EQUAL_OBSERVED_ENVIRONMENT` remains unsatisfied because the synthetic
producer has no locally verified distribution digest, and
`OUTCOME_COVERAGE_AND_SEMANTICS` remains unsatisfied because synthetic evidence
with no ExitSpec identity cannot authorize outcome arithmetic. Inferdrome
therefore publishes no controlled outcome estimate. This is a useful
fail-closed demonstration, not a broken run. The separate pairwise Compare view
also returns `INCOMPARABLE`, shows the bounded eligibility/contract reasons and
configuration context, and suppresses all deltas. Neutral run-level exploration
remains available only in explicitly `DESCRIPTIVE_ONLY` Trial Set views.

The second invocation does not create replacement runs. It requires the
retained plan digest, verifies the existing immutable artifacts, and reuses all
four exact run IDs. A missing digest, changed source, damaged artifact,
incomplete schedule, or occupied identity fails closed.

Useful options:

```bash
./scripts/run_local_demo.py --no-open
./scripts/run_local_demo.py --port 8790
./scripts/run_local_demo.py --prepare-only --json
./scripts/run_local_demo.py --workspace /tmp/inferdrome-demo-take-2
```

The launcher never deletes an existing workspace. Use a different explicit
workspace for a fresh take rather than mutating or repairing recorded evidence.

## Recording path

Use a five-minute walkthrough:

1. Start on **Runs** and call out the visible **Synthetic only** badge.
2. Open one run to show observed distributions, immutable execution context,
   and unavailable measurements that Inferdrome refuses to invent.
3. Open **Evidence** to show offline verification, retained digests, provenance,
   and eligibility as separate facts.
4. Open **Trial sets** to show equal-per-run variation without pooling requests.
5. Open **Comparisons** and the local product demo plan to show the predeclared
   treatment, frozen schedule, four satisfied controls, the unsatisfied
   `COMPLETE_EQUAL_OBSERVED_ENVIRONMENT` and
   `OUTCOME_COVERAGE_AND_SEMANTICS` controls, and the withheld estimate.
6. Open **Compare two runs** to show the explicit synthetic/missing-contract
   incompatibility reasons, configuration context, and suppressed deltas.
7. Close on the boundary: the same workflow accepts real managed-GPU bundles,
   but this recording contains no genuine GPU receipt.

Recommended opening line:

> This is a synthetic walkthrough of Inferdrome's real evidence machinery. It
> demonstrates how runs are frozen, verified, grouped, and compared; it is not
> a GPU performance claim.

Do not describe the synthetic values as benchmark results, customer evidence,
proof of execution, a causal effect, or a winner. Do not describe the controlled
result as comparable: the dashboard correctly withholds that estimate.

## Interview script (4–5 minutes)

Use this as a read-aloud guide while moving through the routes above:

1. **0:00 — Thesis and boundary.** “Inferdrome is the measurement and evidence
   layer for LLM-serving experiments. It resolves the experiment, runs a pinned
   benchmark producer, preserves native output, normalizes request observations,
   deterministically reduces measurements, and verifies the sealed bundle. This
   screen is a synthetic walkthrough of that real workflow: the visible runs are
   `SYNTHETIC_ONLY`, so none of these numbers are a genuine GPU performance
   claim.”

2. **0:45 — Runs, detail, and Evidence.** “Runs shows evidence quality before
   performance. In Run detail, each value keeps its metric identity, definition,
   population, unit, aggregation, and sample count. Missing observations stay
   unavailable instead of becoming zeros. Evidence shows the offline verification
   result, bundle digest, provenance, artifact sensitivity, and eligibility as
   separate facts. The dashboard is only a read-only projection of authoritative
   Python verification and recalculation; it never measures from browser state or
   exposes response-bearing artifact contents.”

3. **1:45 — Descriptive versus controlled comparison.** “Compare two runs is a
   descriptive, non-causal check. The controlled comparison adds a predeclared
   treatment, frozen schedule, equal per-run weighting, and explicit controls.
   This demo intentionally ends `INCOMPARABLE`: four controls are satisfied,
   while `COMPLETE_EQUAL_OBSERVED_ENVIRONMENT` and
   `OUTCOME_COVERAGE_AND_SEMANTICS` are not.
   Inferdrome therefore withholds the controlled outcome estimate while still
   showing the immutable schedule and bounded reasons. That is a successful
   fail-closed behavior, not a winner or a recommendation.”

4. **2:45 — Deployment layer.** “Docker changes the packaging and runtime
   boundary: the runner image and serving runtime remain separate, and the local
   Compose qualification is synthetic. A Docker image or digest is not an
   execution receipt. GCP is represented here by offline inventory and guarded
   dry-run/lifecycle contracts; this demo makes no provider call or paid launch.
   Kubernetes is represented by a guarded batch Job shape and a local mock
   simulation; it does not claim a cluster run or Kubernetes evidence. These
   deployment surfaces change where the benchmark components run, not the frozen
   measurement methodology or evidence schemas.”

5. **3:45 — ExitSpec boundary and close.** “Inferdrome supplies measurements,
   provenance, integrity, and a portable bundle. Independent acceptance starts
   after this point: ExitSpec owns evaluation against a customer contract and
   any `PASS`, `FAIL`, or `NOT_PROVEN` outcome. Inferdrome does not issue that
   verdict. A real managed-GPU bundle can use the same ordinary dashboard path
   after its own local verification, but this recording contains no genuine GPU
   receipt.”

## Continuous rehearsal

The populated Playwright release test invokes this launcher with
`--prepare-only --json`, boots the real server over its generated roots, clicks
every dashboard route, reloads every deep link, checks the visible synthetic
label, rejects browser/network errors, and requires GET-only API traffic. The
engineering integration test runs the launcher twice and proves exact
four-run reuse on the second invocation.
