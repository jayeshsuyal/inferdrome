# Inferdrome CLI and orchestration

Status: **Implemented for v0.1 hardening and v0.2 comparison execution**

Implementation date: **2026-08-06**

The command line is a thin user-facing layer over Inferdrome's frozen library
boundaries. It never implements a second resolver, reducer, bundle reader, or
integrity policy.

## Installation and entry points

```bash
uv lock --check
uv sync --frozen
uv run inferdrome --version
```

The same CLI is available without a console-script installation:

```bash
PYTHONPATH=src python -m inferdrome --help
```

The implementation uses the standard-library argument parser so the executable
surface does not expand the v0.1 dependency or supply-chain budget. The
post-v0.1 dashboard uses a separate optional dependency group.

The runner image's default entry point is the existing `inferdrome` command,
so container execution keeps the same benchmark command and arguments as local
execution. The separately named `inferdrome-runner-probe` entry point calls one
existing endpoint and writes synthetic, evidence-ineligible metadata to an
explicit output directory; it does not launch a serving engine or replace the
`inferdrome run` benchmark workflow. See [RUNNER_IMAGE_V1.md](RUNNER_IMAGE_V1.md).

## Validate and resolve

Validation reads and validates the source YAML plus exact workload bytes. It
does not reserve a run directory, contact an endpoint, or execute a producer.

```bash
inferdrome validate experiment.yaml
```

Resolution prints one canonical JSON document containing the resolved public
experiment, frozen request plan, run ID, and domain-separated digests:

```bash
inferdrome resolve experiment.yaml
inferdrome resolve experiment.yaml \
  --run-id run-0123456789abcdef0123456789abcdef
```

Strict mode is the default. `--no-strict` may calculate an omitted workload
digest or retain unknown attached-target revisions, but it never changes an
unknown fact into verified provenance. Unsupported producer and adapter
versions fail in both modes.

## Execute and seal

The synthetic smoke path requires no endpoint or GPU:

```bash
inferdrome run examples/fake-smoke.yaml --runs-root runs
```

It performs this complete transaction:

```text
resolve source and workload
→ reserve immutable run inputs
→ persist lifecycle states
→ execute the fake producer
→ reduce canonical records
→ stage all normative artifacts
→ hash exact bytes
→ seal the bundle read-only
→ verify it offline
→ mark the workspace COMPLETE
```

The result is always `SYNTHETIC_ONLY`.

The attached-vLLM path additionally requires the local tokenizer directory
used by the pinned producer invocation:

```bash
inferdrome run experiment.yaml \
  --runs-root runs \
  --tokenizer-path /absolute/path/to/pinned-tokenizer
```

It performs redirect-free endpoint preflight, verifies `vllm --version`, builds
one exact no-shell argument vector, supervises the process within the resolved
runtime limit, preserves native output, normalizes only the pinned `0.26.0`
shape, and seals the same public evidence format as the fake path.

Ordinary attached-endpoint evidence remains `INELIGIBLE`. Endpoint model
identity is server-reported; target revisions are configured; GPU, CUDA,
driver, producer-distribution, and launch provenance remain unknown.

The opt-in managed path is narrower and Linux/NVIDIA-only:

```bash
inferdrome run examples/real-gpu-smoke.yaml \
  --runs-root runs \
  --tokenizer-path /absolute/path/to/exact-model-snapshot \
  --managed-local-vllm \
  --managed-model-path /absolute/path/to/exact-model-snapshot \
  --managed-gpu-index 0 \
  --managed-startup-timeout-seconds 900
```

It launches the exact pinned server itself, requires live GPU-process binding,
hashes the model, tokenizer, installed producer, `nvidia-smi`, and generated
launch vector, and checks immutable inputs again after measurement. Only this
managed proof can produce `CUSTOMER_ELIGIBLE` vLLM evidence. It does not turn
eligibility into an acceptance verdict.

The executable host preparation and end-to-end demonstration are in
[REAL_GPU_PROOF.md](REAL_GPU_PROOF.md).

## Inspect, verify, reduce, and summarize

```bash
inferdrome inspect run-0123456789abcdef0123456789abcdef --runs-root runs
inferdrome bundle verify \
  runs/run-0123456789abcdef0123456789abcdef/bundle
inferdrome reduce runs/run-0123456789abcdef0123456789abcdef/bundle
inferdrome summarize runs/run-0123456789abcdef0123456789abcdef/bundle
```

`inspect` validates the complete workspace event chain and verifies any
published bundle. `bundle verify` checks the closed artifact inventory, exact
hashes, schemas, cross-artifact identities, native normalization, and reducer
agreement without mutation or network access.

When a bundle digest has been retained outside the bundle, anchor verification
to it explicitly:

```bash
inferdrome bundle verify \
  runs/run-0123456789abcdef0123456789abcdef/bundle \
  --expected-digest "$BUNDLE_DIGEST"
```

A customer-evidence entry point must also reject synthetic and ordinary
attached bundles:

```bash
inferdrome bundle verify \
  runs/run-0123456789abcdef0123456789abcdef/bundle \
  --expected-digest "$BUNDLE_DIGEST" \
  --require-customer-eligible
```

`reduce` independently reconstructs the frozen measurements from execution
evidence, canonical request records, and metric definitions. It emits the
canonical `inferdrome.measurements.v1` JSON and refuses a stored/recalculated
disagreement. `summarize` emits a stable human-oriented JSON projection without
changing the bundle.

## Create, verify, and summarize a Trial Set

The first v0.2 slice groups 2 through 100 completed runs of one execution
condition into an immutable, retrospective `inferdrome.trial-set.v1`
descriptor. Repeat `--run` in the intended repetition order:

```bash
inferdrome trial-set create \
  --run run-0123456789abcdef0123456789abcdef \
  --run run-11111111111111111111111111111111 \
  --title "Repeated serving condition" \
  --hypothesis "Inspect run-to-run variation" \
  --runs-root runs \
  --trial-sets-root trial-sets
```

Creation verifies and recalculates every member, requires the same experiment
ID, execution fingerprint, metric-definitions digest, and reducer version, and
publishes `trial-sets/<trial-set-id>/trial-set.json` read-only. Every member is
pinned by both `run_id` and `bundle_digest`. The command prints the new Trial
Set ID and out-of-band digest.

Verify the immutable descriptor and all member bundles, optionally anchoring it
to the externally retained digest:

```bash
inferdrome trial-set verify \
  trial-sets/trial-set-0123456789abcdef0123456789abcdef \
  --runs-root runs \
  --expected-digest "$TRIAL_SET_DIGEST"
```

Produce deterministic run-level variation without pooling requests:

```bash
inferdrome trial-set summarize \
  trial-sets/trial-set-0123456789abcdef0123456789abcdef \
  --runs-root runs \
  --expected-digest "$TRIAL_SET_DIGEST"
```

The summary exposes every run-level value and sample count, weights each
available run equally, and reports Decimal minimum, median, maximum, mean, span,
and sample standard deviation. Missing values remain unavailable rather than
becoming zero. All three commands also emit the additive
`controlled_comparison_scope` and
`controlled_comparison_authority_issues` projection. A Trial Set is
`CONTROLLED_OUTCOME_ELIGIBLE` only when every member is customer-eligible and
all members share one non-null ExitSpec contract identity; every other set is
explicitly `DESCRIPTIVE_ONLY_NON_AUTHORITATIVE`. Summary output remains
`DESCRIPTIVE_ONLY`; these commands do not create a predeclared controlled
comparison, infer confidence or causality, configure prefix caching, or issue
ExitSpec outcomes.

The full contract is in [TRIAL_SETS.md](TRIAL_SETS.md).

## Create and verify a controlled comparison

The second v0.2 slice uses a separate immutable design and result. Create a
plan from two source specifications that are identical except for concurrent
`traffic.concurrency`:

```bash
inferdrome comparison-plan create \
  --baseline-source examples/controlled-concurrency-2.yaml \
  --candidate-source examples/controlled-concurrency-4.yaml \
  --title "Concurrency 2 versus 4" \
  --hypothesis "Concurrency may change attempted throughput" \
  --repetitions 2 \
  --primary-outcome attempted_request_throughput_per_s:rate \
  --runs-root runs \
  --comparison-plans-root comparison-plans
```

The command emits preallocated arm run IDs, Trial Set IDs, the exact execution
schedule, and an out-of-band plan digest. Retain that digest.

Verify the immutable design bytes against that retained digest at any time:

```bash
inferdrome comparison-plan verify \
  comparison-plans/comparison-plan-<id> \
  --expected-digest "$PLAN_DIGEST"
```

Execute every missing schedule slot, create both planned Trial Sets, create the
result, and reverify the full chain with one command:

```bash
inferdrome comparison-plan execute \
  comparison-plans/comparison-plan-<id> \
  --expected-digest "$PLAN_DIGEST" \
  --baseline-source examples/controlled-concurrency-2.yaml \
  --candidate-source examples/controlled-concurrency-4.yaml \
  --runs-root runs \
  --trial-sets-root trial-sets \
  --comparison-results-root comparison-results
```

The executor verifies source bytes before reservation, runs only preallocated
IDs in the frozen order, and resumes only an exact independently verified
`COMPLETE` prefix. It never retries `FAILED` or `INTERRUPTED` attempts and never
generates replacement IDs. Its local per-plan lock coordinates cooperating
Inferdrome executors beneath one runs root; it is not distributed execution
attestation.

The response includes executed and reused run IDs, both Trial Set identities
and digests, and the final result. A finalized `INCOMPARABLE` result exits `0`
with no outcome arithmetic because the pipeline succeeded even though one or
more comparison controls did not.

The low-level assembly path remains available. Create a result only after
manually executing the exact schedule and creating both planned Trial Sets,
using all three retained input digests:

```bash
inferdrome comparison-result create \
  --comparison-plan-id comparison-plan-<id> \
  --expected-plan-digest "$PLAN_DIGEST" \
  --baseline-trial-set-id trial-set-<baseline-id> \
  --expected-baseline-digest "$BASELINE_TRIAL_SET_DIGEST" \
  --candidate-trial-set-id trial-set-<candidate-id> \
  --expected-candidate-digest "$CANDIDATE_TRIAL_SET_DIGEST" \
  --runs-root runs --trial-sets-root trial-sets \
  --comparison-plans-root comparison-plans \
  --comparison-results-root comparison-results
```

Verification is read-only and may anchor the exact result bytes to a retained
digest:

```bash
inferdrome comparison-result verify \
  comparison-results/comparison-result-<id> \
  --runs-root runs --trial-sets-root trial-sets \
  --comparison-plans-root comparison-plans \
  --expected-digest "$COMPARISON_RESULT_DIGEST"
```

Any unsatisfied control yields `INCOMPARABLE` with no outcome arithmetic. The
workflow is `OPERATOR_ATTESTED`; it does not provide trusted chronology,
confidence, causality, preference, or ExitSpec acceptance. The exact contract
is in [CONTROLLED_COMPARISONS.md](CONTROLLED_COMPARISONS.md).

## Local evidence dashboard

Install the optional runtime and start the loopback-only dashboard:

```bash
uv lock --check
uv sync --frozen --extra dashboard
uv run inferdrome dashboard \
  --runs-root runs \
  --trial-sets-root trial-sets \
  --comparison-plans-root comparison-plans \
  --comparison-results-root comparison-results
```

The default address is `http://127.0.0.1:8787`. Use `--port` to select another
loopback port or `--open` to open the URL in the default browser.

The command scans only direct children of the configured root, verifies every
candidate, independently recalculates its measurements, and serves bounded
typed projections. It does not mutate bundles, accept arbitrary bundle paths,
serve response-bearing artifacts, or issue ExitSpec-owned acceptance verdicts.
The run index is cursor-paginated with at most 200 combined entries per
response. The server validates the HTTP `Host` header in addition to binding to
loopback, so non-local hostnames are rejected.

The Trial Set extension adds read-only browser routes `/trial-sets` and
`/trial-sets/:trialSetId`, with GET-only index and detail APIs beneath
`/api/v1/trial-sets`. Trial Set discovery is bounded, cursor-paginated, and
snapshot-bound. Detail projections remain `RETROSPECTIVE` and
`DESCRIPTIVE_ONLY` and disclose projected environment drift.

The controlled-comparison extension adds `/comparisons` and
`/comparisons/:planId`, backed by GET-only APIs beneath
`/api/v1/controlled-comparisons`. The design is shown before result controls;
estimates appear only for verified `COMPARABLE` results. `/compare` remains the
separate retrospective two-run utility. Detail responses also project verified
schedule-prefix progress. These fields are dashboard-only operational state,
not a twelfth public evidence schema or execution attestation.

Its product and comparison boundaries are frozen in
[DASHBOARD.md](DASHBOARD.md), [TRIAL_SETS.md](TRIAL_SETS.md), and
[CONTROLLED_COMPARISONS.md](CONTROLLED_COMPARISONS.md).

## Failure and cancellation behavior

Expected validation, resolution, execution, normalization, reduction, bundle,
Trial Set, controlled-comparison, and verification failures print one bounded
error to stderr and exit nonzero without a traceback. Argument errors use the
parser's exit code `2`.

`SIGINT` and `SIGTERM` request cooperative cancellation. The subprocess
supervisor then performs bounded terminate/kill handling. A safely writable
reserved workspace records `INTERRUPTED`; other execution failures record
`FAILED`. A run reaches `COMPLETE` only through successful sealing and offline
verification.

For `comparison-plan execute`, cancellation between completed slots is
resumable. Once a planned ID has been reserved, an interrupted or failed run is
terminal and the v1 plan blocks rather than retrying or replacing that attempt.

The attached-vLLM workspace preserves invocation evidence, producer-version
output, stdout, stderr, exit status, and any producer-written native result in
its private `native-capture/` directory using exclusive no-follow writes. A
failed run does not relabel those diagnostics as a complete evidence bundle.
