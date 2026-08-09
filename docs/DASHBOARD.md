# Inferdrome local evidence dashboard

Status: **Frozen initial contract; aggregate, comparison, and execution-progress extensions accepted**

Decision records:
[ADR 0006](adr/0006-local-read-only-evidence-dashboard.md),
[ADR 0007](adr/0007-add-immutable-descriptive-trial-sets.md),
[ADR 0008](adr/0008-add-operator-attested-controlled-comparisons.md), and
[ADR 0009](adr/0009-add-fail-closed-comparison-execution.md)

Release scope: **Post-v0.1 product slices**

The dashboard makes Inferdrome runs understandable without weakening the
evidence model. It is a local, read-only view over verified bundles. Inferdrome
still measures and verifies; ExitSpec still owns customer acceptance.

This contract does not add the dashboard to the v0.1 definition of done. The
dashboard may be implemented before the v0.1 release is complete, but it does
not satisfy or replace any remaining GPU-host, ExitSpec, security, or release
gate.

## Product promise

Given one or more completed Inferdrome bundles beneath a configured runs root,
the dashboard helps a user:

1. See which runs are valid, eligible, and inspectable.
2. Understand the measurements and execution context of one run.
3. Compare two runs only where their evidence is meaningfully comparable.
4. Inspect verification, provenance, artifact, and sensitivity information.
5. Inspect retrospective run-to-run variation across an immutable Trial Set.
6. Inspect a frozen controlled-comparison design and its verified controls.
7. Distinguish Inferdrome evidence status from ExitSpec acceptance outcomes.

The dashboard does not make evidence more trustworthy than the underlying
bundle. It makes the bundle's existing guarantees and limitations visible.

## Authority and data flow

The normative flow is:

```text
configured run, Trial Set, comparison-plan, and comparison-result roots
        ↓
bounded bundle, Trial Set, plan, and result discovery
        ↓
Python offline verification
        ↓
Python deterministic recalculation
        ↓
typed, bounded display projection
        ↓
Runs / Run detail / Compare / Evidence / Trial Sets / Controlled comparisons
```

The following invariants apply:

- Existing Python verification and reduction logic is authoritative.
- No authoritative measurement is calculated from browser state.
- Displayed measurement values preserve metric identity, definition, unit,
  population, aggregation, sample count, and availability semantics.
- Missing and unavailable observations never become zero.
- Trial Set variation uses one run-level scalar per available member with equal
  run weighting; browser state never pools request populations.
- Controlled-comparison outcomes are projected only from an authoritatively
  verified result; incomparable results contain no outcome arithmetic.
- A projection can omit sensitive or oversized data, but it cannot invent or
  rewrite evidence.
- A projection identifies the bundle digest from which it was produced.

Formatting a value, selecting a chart scale, or sorting rows is presentation.
Recomputing a quantile, denominator, throughput, or evidence status is domain
logic and remains in Python.

## Runtime boundary

The initial dashboard is local and single-user:

- It reads bundles and descriptors below explicit configured roots per process.
- It binds to a loopback interface by default.
- It accepts only `127.0.0.1`, `localhost`, and the in-process test host in the
  HTTP `Host` header, closing the DNS-rebinding path around loopback data.
- It requires no database.
- It requires no network access to verify or recalculate a bundle.
- It does not upload bundles or send telemetry.
- It does not expose a hosted, multi-user, or authenticated service.

An in-memory or on-disk derivative cache is permitted only when it is outside
sealed bundles, disposable, and keyed by immutable bundle digest plus projection
version. A cache miss or cache deletion cannot change the evidence result.

Non-loopback binding must be an explicit operator action and is not a supported
security boundary for the initial product. A supported remote deployment needs
a later ADR covering authentication, authorization, transport security,
tenancy, privacy, and resource isolation.

## Read-only evidence handling

The dashboard never:

- writes inside a completed bundle;
- repairs a failed verification;
- changes an artifact or manifest;
- reseals a bundle;
- deletes a run;
- launches or retries a benchmark; or
- stores UI annotations as bundle evidence.

Discovery yields a bounded candidate identity, not permission to trust its
contents. Before projecting measurements, Inferdrome applies the existing safe
reader, artifact limits, schema checks, integrity checks, cross-artifact checks,
and deterministic recalculation.

Invalid, unsafe, mutated, or inconsistent candidates fail closed. Their run
cards may show a minimal rejection state and bounded diagnostic category, but
the dashboard does not expose their claimed measurements, treat them as valid
comparison inputs, or attempt best-effort parsing.

Dashboard resources are addressed by indexed run identity or bundle digest.
An HTTP parameter is never interpreted as an unrestricted filesystem path.
Symlink, hardlink, traversal, undeclared-file, file-count, byte-count, depth,
and text-decoding defenses remain at least as strict as offline verification.

## Projection and privacy contract

Browser-facing projections are allowlisted, typed, versioned, and bounded.
Collection projections are paginated. Text, list, map, artifact, and response
sizes have explicit upper bounds, and an omitted field remains distinguishable
from an observed zero or empty value.

`GET /api/v1/runs` uses an opaque `cursor` and a `limit` from 1 through 200.
Each page contains at most `limit` combined verified and rejected entries and
returns `total`, `returned`, `has_more`, and `next_cursor` metadata. The browser
follows at most five 200-entry pages, matching the 1,000-entry discovery bound;
each cursor is bound to the ordered run/digest snapshot, so if the index changes
between pages the request fails closed and asks for a fresh load.

`GET /api/v1/trial-sets` uses the same 1-through-200 page limit and an opaque,
snapshot-bound cursor. Trial Set discovery examines at most 200 direct entries,
and each immutable descriptor contains at most 100 members. Detail lookup uses
`GET /api/v1/trial-sets/{trial_set_id}`; the identifier is never interpreted as
an arbitrary path.

`GET /api/v1/controlled-comparisons` uses the same bounded 1-through-200 page
contract and a snapshot-bound cursor over at most 200 direct plan entries.
`GET /api/v1/controlled-comparisons/{comparison_plan_id}` resolves only a
validated direct-child plan ID. Matching result declarations are bounded and
fully verified before any result data is projected; duplicate or invalid
results are withheld.

Untouched native artifacts remain part of the evidence bundle, but the
dashboard does not serve arbitrary bundle files. Response-bearing native output
is excluded from browser projections by default. Request prompts, generated
responses, producer stdout, producer stderr, environment values, invocation
arguments, and endpoint metadata are displayed only through specific fields
with documented redaction and sensitivity handling.

Attached-endpoint identity is projected only as a domain-separated SHA-256
identity digest. The raw endpoint URL is not included in browser responses.

Redaction is a display operation. It never rewrites the native artifact or
downgrades the bundle's response-content sensitivity classification.

Controlled-comparison detail exposes the frozen treatment, arm identities,
source and execution digests, memberships, schedule, and analysis policy. It
does not serialize either arm's full `resolved_experiment` into the browser
projection; raw target URLs and other non-allowlisted resolved fields remain in
the immutable plan artifact and CLI verification boundary.

## Information architecture

The accepted visual direction begins with the balanced Runs / Run detail /
Compare / Evidence design. The Trial Sets extension adds one index and one
detail view using the same hierarchy, evidence status, responsive behavior, and
neutral language. This document is the committed reference; implementation
does not depend on an external design file.

### Runs

The default view answers, "What evidence do I have, and is it usable?"

It shows:

- aggregate counts by verification and evidence eligibility;
- a searchable, evidence-filterable run table ordered newest first;
- run identity, creation time, model, producer, target profile, and bundle
  digest summary;
- a compact set of authoritative headline measurements;
- environment completeness and evidence eligibility; and
- clear empty, loading, unavailable, invalid, and partial states.

Measurement cells retain their units and never substitute an em dash, zero, or
blank without an accessible availability explanation.

### Run detail

The detail view answers, "What happened in this run, under what context, and
how were its measurements derived?"

It shows:

- run, execution, verification, completeness, and eligibility status;
- authoritative measurements with definitions and populations;
- distributions derived by the authoritative Python projection path;
- request outcome counts and server-derived bounded distributions;
- resolved workload, model, producer, adapter, target, and environment context;
- unavailable capabilities and replayability limitations; and
- links into the corresponding Evidence sections.

Charts are alternate presentations of projected values. They do not perform
hidden aggregation over raw browser data.

### Compare

The compare view answers, "Can these two runs be compared, what changed, and
what is the arithmetic difference?"

It accepts exactly one baseline and one candidate. It always presents the
comparability result before any deltas and shows the context fields responsible
for that result.

The initial dashboard does not pool requests across runs, create a trial set,
estimate uncertainty, infer causality, or recommend a winner. Those are
separate controlled-comparison and acceptance concerns.

### Evidence

The Evidence view answers, "Why should this bundle be considered internally
consistent, and what does it still not prove?"

It shows:

- bundle digest and offline verification result;
- artifact inventory and bounded metadata;
- producer and adapter identity plus frozen digest domains;
- environment provenance and completeness;
- replayability and response-content sensitivity;
- evidence eligibility and synthetic markers;
- artifact sensitivity metadata and trust limitations; and
- the ExitSpec contract digest when the bundle declares one.

Integrity is not labeled authorship, execution truth, trusted attestation, or
customer acceptance.

### Trial Sets

The Trial Sets index answers, "Which immutable repeated-run groupings are
available, and can every member still be verified?"

The browser route is `/trial-sets`. It shows:

- searchable verified Trial Sets ordered by latest member run;
- Trial Set identity, title, experiment, model, member count, and short
  fingerprint and digest context;
- evidence-eligibility diversity and projected environment status; and
- bounded rejection codes for unsafe, unavailable, duplicate, or unverifiable
  declarations.

The index does not infer membership from run title or experiment ID. It reads
only explicit immutable `inferdrome.trial-set.v1` descriptors beneath the
configured Trial Set root.

### Trial Set detail

The detail route `/trial-sets/:trialSetId` answers, "What was grouped, how did
the run-level measurements vary, and what prevents stronger interpretation?"

It shows:

- `RETROSPECTIVE` design status and `DESCRIPTIVE_ONLY` inference status;
- the out-of-band Trial Set digest, membership policy, execution fingerprint,
  metric-definitions digest, and reducer version;
- ordered member runs with exact run and bundle identities;
- total and available run counts for each metric;
- backend-derived minimum, median, maximum, arithmetic mean, span, and sample
  standard deviation;
- every run-level point with its exact value and sample count; and
- `CONSISTENT` or `DRIFT_DETECTED` environment status plus changed field names.

Every point represents one verified run-level measurement and has equal run
weight. Request records remain separate. Missing values remain unavailable;
they do not become zero. The browser renders the backend projection and does
not recalculate variation.

Environment consistency means only that projected fields did not differ. It is
not proof that all material environment facts were observed or controlled.
Drift is disclosed, not promoted into a declared treatment.

Trial Set validity, environment consistency, descriptive sufficiency, and
customer acceptance remain separate concepts. No chart or label claims
confidence, significance, stability, causality, improvement, regression,
winner status, recommendation, or an ExitSpec outcome.

The full aggregate contract is in [TRIAL_SETS.md](TRIAL_SETS.md).

### Controlled comparisons

The route `/comparisons` is the index for immutable
`inferdrome.controlled-comparison-plan.v1` designs. It keeps the plan state
separate from result state and shows:

- title, plan identity, treatment path and baseline/candidate values;
- `PREDECLARED` with explicit `OPERATOR_ATTESTED` assurance;
- planned pair count and primary outcome;
- `COMPARABLE`, `INCOMPARABLE`, `NO_RESULT`, or `WITHHELD`; and
- bounded rejection codes without claimed outcome values.

The detail route `/comparisons/:planId` presents the frozen design first:
arm digests, preallocated memberships, schedule, outcome, estimator,
exclusion policy, environment scope, and assurance limitation. It then presents
all six closed result controls. A single run-level paired plot and exact Decimal
arithmetic appear only for a verified `COMPARABLE` result. `INCOMPARABLE`,
missing, duplicate, or withheld results expose no estimate, arm mean, paired
difference, or run-level outcome value.

The frozen schedule includes a dashboard-only operational projection. It shows
the verified completed count, next frozen slot when execution can continue,
and each planned workspace state. `COMPLETE` is displayed as verified only
after bundle recalculation and exact workspace binding. A schedule hole,
terminal failed/interrupted attempt, invalid workspace, or chronology conflict
is `BLOCKED`. A nonterminal workspace is merely observed; the dashboard does
not claim that an executor is currently running. These fields are not a public
evidence schema and do not alter any result control.

Result publication is projected as a separate fact from operational workspace
status. A valid immutable result remains visible even if later local workspace
inspection is blocked or unavailable; the dashboard never fabricates completed
slots from the existence of that result.

`COMPARABLE` means the declared and observed v1 controls matched. It does not
prove chronology, authorship, causality, significance, preference, complete
real-world confounder control, customer eligibility, or acceptance. The full
contract is in [CONTROLLED_COMPARISONS.md](CONTROLLED_COMPARISONS.md).

## Pairwise comparison contract

Every pair receives one of three conceptual outcomes:

- **Comparable:** the relevant measurement and execution contracts align.
- **Comparable with declared context changes:** matching measurements may be
  placed side by side, and every material context change is visible.
- **Incomparable:** a safe arithmetic comparison is not supported; deltas are
  suppressed and reasons are shown.

The policy evaluates, where relevant:

- metric identifier and definition version;
- unit, population, aggregation, rounding, and quantile method;
- reducer and canonical-record contract versions;
- request-plan identity and traffic semantics;
- producer, adapter, and timing semantics;
- model and tokenizer identity;
- target and execution configuration;
- environment completeness and material hardware or software context; and
- evidence eligibility, integrity, and missing observations.

Comparability policy is versioned and deterministic. The UI cannot override an
incomparable result. A context change being visible does not make it controlled,
causal, or acceptable.

The verified execution fingerprint is part of the comparison contract. Every
fingerprint input is either checked as a hard measurement contract or projected
as an explicit context field. A fingerprint change without a corresponding
declared contract or context change is incomparable and suppresses all deltas.

This pairwise policy remains independent of Trial Sets and controlled
comparison results. Neither
`COMPARABLE_WITH_CONTEXT_CHANGES` nor membership in a valid retrospective Trial
Set is upgraded into a predeclared controlled-comparison claim.

For supported matching values, a delta is the candidate value minus the
baseline value, with unit and calculation displayed. Relative percentages are
shown only when their denominator is defined and nonzero.

## Directionality and color

The current metric contracts do not freeze whether a higher or lower value is
desirable. Therefore:

- deltas use neutral styling;
- up and down indicators mean mathematical direction only;
- the dashboard does not label a delta as better, worse, improved, or regressed;
- green and red do not encode performance preference; and
- evidence validity, eligibility, and execution status are visually distinct
  from metric movement.

Performance preference may be added only after directionality is represented by
a reviewed, versioned, frozen contract. Customer threshold outcomes remain
ExitSpec-owned even after such a contract exists.

## Inferdrome and ExitSpec boundary

Inferdrome may display:

- execution status;
- integrity status;
- environment completeness;
- evidence eligibility;
- measurements and availability; and
- a declared ExitSpec contract digest.

The initial dashboard does not ingest or display ExitSpec receipts. A later
receipt view requires a separately reviewed import and attribution contract.

Inferdrome does not calculate or issue:

- `PASS`;
- `FAIL`;
- `NOT_PROVEN`;
- customer threshold compliance; or
- evidence sufficiency for a customer contract.

A valid Inferdrome bundle can still receive any applicable ExitSpec outcome.
The dashboard never converts `COMPLETE`, `VALID`, or `CUSTOMER_ELIGIBLE` into an
acceptance verdict.

## Real-GPU parity

A genuine managed-GPU bundle is not a special dashboard type. Once present
beneath a configured runs root, it follows the same safe discovery,
verification, recalculation, projection, comparison, and rendering path as any
other bundle.

The UI may display additional environment fields that are already present and
allowlisted, but it does not add a GPU-specific parser, reducer, import mode, or
trust claim. Attached-endpoint and synthetic eligibility differences remain
visible through the ordinary evidence contract.

## Initial non-goals

- Hosted or multi-user service operation.
- Authentication, authorization, tenancy, or sharing workflows.
- A database-backed run catalog.
- Bundle upload, mutation, repair, deletion, or resealing.
- Benchmark execution or orchestration from the browser.
- Raw arbitrary-file browsing or unrestricted native-content download.
- Trusted comparison chronology or authorship, additional treatments,
  confidence intervals, significance, or pooled request populations.
- Prefix-caching controls or a canonical prefix-caching experiment.
- Causal explanations, automatic optimization, or AI tuning advice.
- Customer acceptance authoring or evaluation.
- Replacement of the CLI, public evidence schemas, or ExitSpec importer.

## Product acceptance criteria

The initial dashboard contract is satisfied only when:

1. Every displayed measurement matches authoritative Python recalculation.
2. A corrupted or inconsistent bundle exposes no usable measurement projection.
3. Completed bundle bytes are unchanged by discovery and inspection.
4. Arbitrary paths and unsafe filesystem nodes cannot be read through the UI.
5. Browser responses obey documented projection, pagination, size, and redaction
   limits.
6. Incomparable pairs show reasons and no deltas.
7. Comparable deltas use neutral semantics and make no acceptance claim.
8. Runs, Run detail, Compare, and Evidence work across empty, partial,
   unavailable, invalid, and valid states.
9. Status is understandable without relying on color alone, and primary flows
   support keyboard and narrow-screen use.
10. A genuine real-GPU bundle appears through the ordinary bundle path without
    UI-specific ingestion or measurement code.
11. Every displayed Trial Set is traceable to 2 through 100 currently verified
    `run_id` and `bundle_digest` member pairs.
12. Trial Set summaries preserve all run-level points, use equal run weighting,
    and never pool request records.
13. Environment drift is disclosed independently from integrity and does not
    become a controlled or causal claim.
14. Trial Set views remain `RETROSPECTIVE` and `DESCRIPTIVE_ONLY` and never
    issue confidence, significance, prefix-caching, or ExitSpec claims.
15. Every comparison design is traceable to one immutable plan and shows
    `OPERATOR_ATTESTED` beside `PREDECLARED`.
16. A controlled result is projected only after the plan, both Trial Sets, and
    every member bundle verify against retained digests.
17. Any unsatisfied comparison control suppresses all outcome arithmetic.
18. Comparable paired points preserve one equal-weight scalar per planned run
    and never imply confidence, causality, preference, or acceptance.
19. Operational schedule progress labels a slot verified only after workspace,
    bundle, arm, and prefix checks succeed.
20. Progress remains GET-only local observation and never becomes execution
    attestation, trusted chronology, or a substitute for a result artifact.

## Development gate

Install the optional Python runtime and locked frontend dependencies, then run
the dedicated dashboard gate:

```bash
uv sync --extra dev --extra dashboard
npm ci --prefix frontend
INFERDROME_PYTHON=.venv/bin/python ./scripts/dashboard_gate.sh
```

The gate type-checks, tests, and builds the frontend, then runs the dashboard's
Python projection, discovery, pairwise comparison, Trial Set,
controlled-comparison, API, packaging, and server tests. It also builds a wheel,
installs that wheel into an isolated target, and proves the installed HTML, deep links, API, and
referenced assets are served. The repository's existing
`scripts/engineering_gate.sh` remains the authoritative v0.1 evidence pipeline
gate.
