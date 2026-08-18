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

The controlled result is intentionally `INCOMPARABLE`. Five controls close,
while `COMPLETE_EQUAL_OBSERVED_ENVIRONMENT` remains unsatisfied because the
synthetic producer has no locally verified distribution digest. Inferdrome
therefore publishes no controlled outcome estimate. This is a useful
fail-closed demonstration, not a broken run. The separate pairwise Compare view
still shows clearly labeled descriptive arithmetic and configuration diffs; it
does not upgrade the controlled result.

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
   treatment, frozen schedule, five satisfied controls, the unsatisfied complete
   environment control, and the withheld controlled estimate.
6. Open **Compare two runs** to show the clearly labeled pairwise descriptive
   metric and configuration diff, including baseline/candidate swapping.
7. Close on the boundary: the same workflow accepts real managed-GPU bundles,
   but this recording contains no genuine GPU receipt.

Recommended opening line:

> This is a synthetic walkthrough of Inferdrome's real evidence machinery. It
> demonstrates how runs are frozen, verified, grouped, and compared; it is not
> a GPU performance claim.

Do not describe the synthetic values as benchmark results, customer evidence,
proof of execution, a causal effect, or a winner. Do not describe the controlled
result as comparable: the dashboard correctly withholds that estimate.

## Continuous rehearsal

The populated Playwright release test invokes this launcher with
`--prepare-only --json`, boots the real server over its generated roots, clicks
every dashboard route, reloads every deep link, checks the visible synthetic
label, rejects browser/network errors, and requires GET-only API traffic. The
engineering integration test runs the launcher twice and proves exact
four-run reuse on the second invocation.
