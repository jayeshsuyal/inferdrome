# Inferdrome build roadmap

Status: **Canonical v0.1 sequence**

The roadmap is gate-driven. A phase is complete when its invariants are proven,
not when its planned files merely exist.

Current checkpoint: **The original Qwen2.5 producer-side real-GPU gate and the
separate Qwen3-8B A10 capability spike are complete. The Qwen3 run contains
96/96 successes, a valid customer-eligible bundle, independent post-termination
verification, a bounded publication review, and deterministic handoff anchors.
Both raw archives remain EXTERNAL_ONLY; owner license/publication decisions,
security sign-off, ExitSpec outcomes, and release work remain open.**

Next campaign checkpoint: **Preserve the exact Qwen3-8B model, workload,
sampling, producer, and reducer identities while collecting separately reviewed
receipts on the next selected GPU classes. No cross-GPU conclusion exists until
those runs complete. Every unexecuted model/runtime/GPU assignment remains
`UNPROVEN_REQUIRES_SPIKE`; ADR 0010 grants no launch authority, and ADR 0011
keeps warmups aligned with exact vLLM 0.26.0 behavior.**

The numbered PR labels below are original build-sequence milestones, not GitHub
pull-request numbers.

PR 6 is an independent-consumer integration boundary. Its contract remains in
this roadmap, but work on that consumer is outside the Inferdrome repository
and is not an active implementation target here.

## PR 0 — Freeze the foundation

Deliver:

- product charter;
- architecture and ownership boundaries;
- threat model;
- v0.1 non-goals and definition of done;
- architecture decision records; and
- implementation sequence.

Gate:

```text
Mission, claims, trust boundaries, and delayed scope are explicit.
Public wire fields remain provisional.
```

## PR 0.5 — Pinned-vLLM capability spike

This spike comes before public schemas.

Deliver:

- selection and exact pin of one vLLM benchmark version;
- a small attached-endpoint run with preflight, warmup, successful requests, and
  at least one failed request;
- untouched detailed native output;
- producer invocation and version evidence;
- a field-by-field capability matrix;
- documented request ordering and ID behavior;
- documented streaming and TTFT behavior;
- documented warmup visibility;
- documented native response-content behavior; and
- a first native golden fixture.

Gate:

```text
Every proposed v0.1 canonical field is OBSERVED, strictly DERIVABLE,
CONFIGURED_ONLY, or UNAVAILABLE.

No configured-only or unavailable value is represented as an observation.
```

If the pinned producer cannot support the minimum useful evidence contract, the
team revises the provisional architecture before publishing v1 schemas.

## PR 1 — Public schemas and domain model

Deliver:

- experiment schema;
- request-plan schema;
- canonical request-record schema;
- environment and provenance schema;
- metric-definition and measurement schemas;
- evidence-bundle schema;
- typed domain models;
- run-state machine;
- identifier and digest types; and
- cross-validator conformance tests.

Gate:

```text
Schemas express only capabilities established by PR 0.5.
Invalid, ambiguous, and unsupported fields fail closed.
```

Completed on 2026-08-05. Eight Draft 2020-12 schemas, strict Pydantic models,
the frozen run-state graph, domain-separated digest helpers, and cross-validator
conformance vectors are committed as the v1 contract.

## PR 2 — Resolver and immutable run workspace

Deliver:

- source YAML validation;
- explicit default expansion;
- request-plan construction;
- path and producer resolution;
- domain-specific hashing;
- run-directory reservation;
- state persistence;
- secret-bearing URL rejection; and
- bounded cancellation scaffolding.

Gate:

```text
Resolved execution inputs and the request plan cannot change after
measurement begins.
```

Completed on 2026-08-05. The strict YAML resolver reads bounded no-follow
inputs once, verifies exact workload bytes, expands defaults, constructs the
ordered request plan, calculates domain-separated digests, reserves run IDs
atomically, and persists a lock-protected append-only state history. Frozen
inputs are read-only and reverified before every transition.

## PR 3 — Fake adapter and deterministic reducer

Deliver:

- synthetic fake adapter;
- golden request-record fixtures;
- frozen metric-definition implementation;
- deterministic reducer;
- quantile and rounding policy;
- count, error-rate, choices-event latency, and throughput calculations; and
- explicit empty-population behavior; and
- synthetic-eligibility enforcement.

Gate:

```text
Identical canonical inputs produce byte-identical derived output.
Synthetic evidence cannot enter the honest customer-evidence path.
```

Completed on 2026-08-05. The fake producer writes immutable synthetic markers,
native rows, canonical records, and completed execution evidence. The reducer
implements the frozen count, error-rate, choices-event latency, and throughput
formulas with nearest-rank quantiles and decimal-half-even rounding. A committed
success/failure golden fixture regenerates byte-identically in the engineering
gate.

## PR 4 — Evidence bundle and offline verification

Deliver:

- staged bundle writer;
- artifact inventory;
- exact-byte hash manifest;
- immutable sealing lifecycle;
- safe bundle reader;
- offline verifier; and
- adversarial mutation tests.

Gate:

```text
Every material post-seal mutation is detected relative to the retained
manifest and bundle digest. Verification never mutates or executes content.
```

Completed on 2026-08-05. The writer stages a closed sixteen-role artifact set,
hashes exact bytes, emits an out-of-band domain-separated bundle digest, seals
the tree read-only, publishes it atomically, verifies again, and only then marks
the workspace complete. The bounded no-follow reader and cross-artifact
verifier reject unsafe nodes, undeclared content, integrity drift, semantic
disagreement, and reducer mismatch.

## PR 5 — Attached endpoint and pinned-vLLM path

Deliver:

- endpoint preflight profile;
- pinned producer invocation;
- native-output preservation;
- version-specific normalizer;
- native source locators;
- capability enforcement;
- golden native-output tests; and
- full non-GPU integration tests over stored fixtures.

Gate:

```text
The real producer and fake adapter converge on the same public evidence
format without pretending to expose identical capabilities.
```

Completed on 2026-08-05. The attached profile performs bounded model-list
preflight, the adapter generates and revalidates one exact no-shell invocation,
the subprocess supervisor preserves bounded diagnostics, and the pinned
normalizer converts the real success/failure capture without guessing missing
fields. Stored native goldens regenerate byte-identically, and a sealed
non-GPU vLLM fixture passes full offline renormalization and metric
recalculation. Coherently rehashed shape, timing, invocation, and version
mutations fail at their semantic boundaries.

## PR 6 — ExitSpec importer

Deliver in ExitSpec:

- vendored public schemas and conformance vectors;
- bounded, path-safe bundle reader;
- hash and eligibility verification;
- canonical-record metric recalculation;
- applicability and sufficiency mapping;
- explicit rejection/verdict decision table; and
- ingestion receipt.

Gate:

```text
ExitSpec never trusts an Inferdrome summary used in a verdict.
```

## Milestone 7 — Real-GPU proof

Deliver:

- exact GPU-host preparation and server-launch instructions;
- one real vLLM evidence bundle;
- environment provenance manifest;
- `PASS`, `FAIL`, and `NOT_PROVEN` demonstrations;
- corrupted-artifact and synthetic-fixture rejection demonstrations; and
- a short repeatable demo script.

Gate:

```text
A clean compatible GPU host can reproduce the measurement procedure and
produce a newly sealed bundle without editing evidence by hand.
```

An attached endpoint cannot by itself prove server launch configuration. The
reproduction guide must therefore preserve the exact launch command and locally
observed environment. A managed local-server mode may be added if that is the
smallest honest way to meet this gate.

Implemented on 2026-08-06: the managed Linux/NVIDIA path hashes pinned model,
tokenizer, and vLLM inputs; launches an exact loopback server argument vector;
binds it to live GPU process evidence; emits a complete provenance manifest;
and permits customer eligibility only after offline cross-verification. The
checked-in host-preparation and demo scripts also exercise corruption and
synthetic-flow rejection without modifying the original bundle.

Operational hardening added on 2026-08-11: an operator-provided SSH controller
transfers the exact clean Git commit, bounds host workload time, runs the
single and controlled-comparison proofs, retrieves a checksumed capture pack,
and independently reverifies every bundle, Trial Set, plan, and result on the
workstation. It does not provision billable cloud infrastructure.

Capture progress on 2026-08-18: one genuine Lambda A10 single-run bundle is
sealed, independently valid, customer-eligible, and visible through the
ordinary dashboard bundle path. The retained outer archive is explicitly
`INCOMPLETE_NOT_EVIDENCE` because a post-run single-proof harness assertion
failed and the controlled comparison never started; the materialized receipt
is correspondingly limited to `SINGLE_BUNDLE_ONLY`.

Lifecycle hardening on 2026-08-19 adds an opt-in Lambda API cost guard. It
requires the actual billing origin, fails closed on missing rate or endpoint
identity, subtracts a fixed termination safety margin, waits for a detached
watchdog readiness handshake, calls provider termination on every controller
exit path, and polls until termination is confirmed. It cannot launch an
instance, and its API key remains environment-only. It is a local circuit
breaker rather than an exact provider-billing guarantee.

Capture completion on 2026-08-20: one Lambda Stack 24.04 A10 archive contains a
verified single-run proof and a verified four-run controlled comparison. Every
comparison bundle is customer-eligible with complete observed environment
evidence, all controls are satisfied, and the result is `COMPARABLE`. Archive
verification is anchored to commit `c08b46d9fbd87477f45d130aa3c63615937c4dc3`
and runs in an isolated temporary directory so source-workspace permission
rewrites cannot weaken or spuriously invalidate the sealed bundles.

Producer publication closure on 2026-08-20 adds a standalone closed
`inferdrome.local-gpu-proof.v1` schema, composite managed-vLLM profile,
conformance mutations, exact-archive review, and deterministic handoff. The
review finds no secret, email, or public-IP detector matches and classifies the
unchanged archive `EXTERNAL_ONLY` because owner approval and multiple license
records remain unresolved. Raw bytes were not committed or uploaded.

Still required to close the external acceptance boundary: run the separately
owned ExitSpec `PASS`, `FAIL`, and `NOT_PROVEN` demonstrations against the exact
handoff and retain its receipt. The owner must separately choose a repository
license and approve or reject public archive delivery. Ordinary attached
endpoint runs deliberately remain `INELIGIBLE`.

## PR 8 — v0.1 hardening and release

Deliver:

- complete adversarial suite;
- error taxonomy and CLI polish;
- engineering gate;
- example bundles and receipts;
- documentation review;
- security review against the frozen threat model;
- release checklist evidence; and
- `v0.1.0`.

In progress: the chartered `validate`, `resolve`, `run`, `inspect`, `bundle
verify`, `reduce`, and `summarize` commands now share the same fail-closed
library boundaries used by tests. The release shield runs the engineering and
populated-dashboard gates in GitHub Actions and maps remaining evidence in
`V0_1_RELEASE_CHECKLIST.md`. The genuine GPU producer evidence is complete;
ExitSpec demonstrations, human security sign-off, license selection, owner
archive-publication decision, and the release tag remain open. Remaining polish
is tracked by the release gate rather than by adding new product scope.

Gate:

```text
Every item in the v0.1 definition of done has linked evidence.
```

See [V0_1_DEFINITION_OF_DONE.md](V0_1_DEFINITION_OF_DONE.md).

## Post-v0.1 roadmap

### Product slice — Local evidence dashboard

Status: product contract accepted in
[ADR 0006](adr/0006-local-read-only-evidence-dashboard.md) and
[DASHBOARD.md](DASHBOARD.md).

This slice may be implemented while the remaining v0.1 external demonstrations
are pending, but it is not part of the v0.1 release gate and cannot close those
requirements.

Deliver:

- one local, loopback-default, read-only dashboard;
- bounded bundle discovery without a database;
- authoritative Python verification and recalculation before projection;
- Runs, Run detail, Compare, and Evidence views;
- bounded and redacted browser-facing projections;
- pairwise comparison with explicit, deterministic comparability reasons;
- neutral arithmetic deltas without better-or-worse claims; and
- one ordinary display path for synthetic, attached-endpoint, and real-GPU
  bundles.

Gate:

```text
Every displayed measurement is traceable to authoritative recalculation.
Invalid evidence fails closed, completed bundles remain unchanged, and the
dashboard never issues an ExitSpec-owned acceptance verdict.
```

Pairwise inspection in this slice does not create a trial set, pool request
populations, estimate uncertainty, or make a controlled-experiment claim.

### v0.2 slice 1 — Immutable descriptive Trial Sets

Status: implemented; product and public-contract boundary accepted in
[ADR 0007](adr/0007-add-immutable-descriptive-trial-sets.md) and
[TRIAL_SETS.md](TRIAL_SETS.md).

This is the first v0.2 vertical slice. It adds repeated-run visibility without
claiming that a retrospective grouping is a controlled experiment.

Deliver:

- additive `inferdrome.trial-set.v1` public schema without changing any of the
  eight existing v1 schema files;
- immutable 2-through-100-run membership bound by `run_id` and
  `bundle_digest`;
- one shared experiment ID, execution fingerprint, metric-definition set, and
  reducer version;
- authoritative verification and recalculation of every member;
- separate request populations and equal-per-run descriptive statistics;
- environment-drift disclosure;
- an out-of-band, domain-separated Trial Set digest;
- `trial-set create`, `verify`, and `summarize` CLI operations; and
- local dashboard Trial Sets index and detail routes.

Gate:

```text
Every Trial Set remains traceable to immutable verified member bundles.
No request pooling, controlled-experiment claim, confidence claim, causal
claim, prefix-caching claim, or ExitSpec outcome is introduced.
```

### v0.2 slice 2 — Operator-attested controlled comparisons

Status: implemented; product and public-contract boundary accepted in
[ADR 0008](adr/0008-add-operator-attested-controlled-comparisons.md) and
[CONTROLLED_COMPARISONS.md](CONTROLLED_COMPARISONS.md).

This slice introduces separate immutable plan and result contracts. It:

- freeze the hypothesis, disjoint arm membership policy, planned repeat count,
  ordered schedule, outcome selectors, estimator, and exclusion policy before
  execution;
- allow only reviewed, typed independent-variable paths;
- prove that cross-arm execution-fingerprint differences are exactly the
  predeclared differences;
- require complete material control context or return `INCOMPARABLE`;
- preserve one-run statistical units and neutral candidate-minus-baseline
  arithmetic; and
- remain separate from ExitSpec acceptance.

The v1 implementation is limited to one typed `traffic.concurrency` treatment,
2–100 seeded permuted pairs, one primary outcome, no exclusions, complete-case
paired mean-difference arithmetic, and no uncertainty method. Any failed
control returns `INCOMPARABLE` and suppresses every outcome value.

`PREDECLARED` has machine-readable assurance `OPERATOR_ATTESTED`: local
timestamps and retained digests record the workflow but do not independently
prove chronology. `COMPARABLE` covers complete equality of the observed v1
allowlist, not every possible real-world confounder, and is never a causal,
preference, significance, or acceptance claim.

Confidence intervals require a separately reviewed repeat-count, estimator,
and uncertainty contract. Three runs are an example design, not an automatic
statistical guarantee.

Prefix caching remains outside slices 1 through 3 and is not a representable
treatment.
It first requires an explicit typed execution control, fingerprint coverage,
managed-server invocation evidence, and offline verification. A future
prefix-caching comparison cannot be represented as an unbound label on a
Trial Set.

### v0.2 slice 3 — Fail-closed controlled-comparison execution

Status: implemented; execution boundary accepted in
[ADR 0009](adr/0009-add-fail-closed-comparison-execution.md).

This slice automates the already frozen slice-2 protocol without changing any
of the eleven public schemas. It adds:

- retained-plan-digest and exact arm-source verification before reservation;
- immutable private source/workload snapshots for the whole schedule;
- one cooperating-process lock keyed by plan identity beneath the runs root;
- exact preallocated schedule execution with no retries or replacements;
- resume from every independently verified `COMPLETE` schedule prefix;
- exact workspace-to-bundle byte binding before a run can be reused;
- crash-safe no-replace publication for Trial Sets and results;
- automatic Trial Set and result finalization with full reverification; and
- dashboard-only per-slot operational progress.

Gate:

```text
Automation may reduce operator error but cannot increase assurance. A hole,
tampered run, failed/interrupted attempt, or abandoned reserved workspace
blocks the frozen plan. OPERATOR_ATTESTED, RETROSPECTIVE, SYNTHETIC_ONLY,
POINT_ESTIMATE_ONLY, and ExitSpec ownership remain unchanged.
```

The "verified completed prefix" above refers only to completed run workspaces
at the front of the comparison schedule. It is unrelated to model KV prefix
caching, which remains outside slices 1 through 3 and is not a representable
treatment.

The managed real-GPU proof runner now has an opt-in four-run comparison mode
that verifies every customer-eligible bundle, both Trial Sets, the result, and
an all-reused second executor invocation. A genuine A10 single-run and full
four-run comparison archive is retained and independently valid. ExitSpec
acceptance remains separate and pending. Linux/NVIDIA receipts remain
host-executed evidence and are not claimed by the static engineering gate.

### v0.2 — Telemetry

- vLLM metrics collection;
- queue, prefill, decode, and KV-cache observations;
- NVIDIA DCGM GPU observations;
- clock-domain and sampling metadata; and
- telemetry capability declarations.

Only after telemetry is present may Inferdrome make evidence-backed claims about
why latency changed.

### v0.3 — Router experiments

- request-linked route-decision artifacts;
- cost and quality references;
- premium, economy, and routed baselines; and
- route-policy provenance.

### v0.4 — Second serving engine

- SGLang producer and normalizer;
- engine-specific capability declaration; and
- proof that the evidence model tolerates genuinely different native outputs.

Unsupported observations remain unavailable rather than becoming zero.
