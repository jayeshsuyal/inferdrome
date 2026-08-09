# Inferdrome public contracts v1

Status: **Eight v0.1 schemas frozen; three additive v0.2 schemas accepted**

Freeze date: **2026-08-05**

Trial Set extension date: **2026-08-07**

Controlled-comparison extension date: **2026-08-07**

Fail-closed comparison-executor date: **2026-08-08 (no schema change)**

This document defines the public evidence boundary established by PR 1 and the
additive post-v0.1 Trial Set and controlled-comparison contracts. The eight
original evidence contracts
are grounded in the pinned vLLM `0.26.0`
[capability matrix](../spikes/vllm-0.26.0/CAPABILITY_MATRIX.md). They expose
only observations that the spike established as observed or strictly
derivable. The three v0.2 contracts reference immutable evidence objects; they
do not reinterpret any original schema.

The third v0.2 slice automates the frozen controlled-comparison workflow and
adds dashboard-only operational progress. Neither is a public evidence
artifact, so the public schema count remains eleven.

## Normative artifacts

The committed Draft 2020-12 schemas live in [`schemas/public/v1`](../schemas/public/v1):

| Schema | Purpose |
|---|---|
| `experiment.schema.json` | Fully resolved execution intent |
| `request-plan.schema.json` | Ordered measured-request plan |
| `request-record.schema.json` | One normalized measured request |
| `environment.schema.json` | Allowlisted environment facts and provenance |
| `execution.schema.json` | Aggregate execution and phase evidence |
| `metric-definitions.schema.json` | Frozen metric meanings |
| `measurements.schema.json` | Deterministic reducer output |
| `evidence-bundle.schema.json` | Sealed bundle descriptor and inventory |
| `trial-set.schema.json` | Immutable descriptive grouping of repeated runs |
| `controlled-comparison-plan.schema.json` | Frozen two-arm comparison design |
| `controlled-comparison-result.schema.json` | Verified controls and paired estimate |

`trial-set.schema.json` is the ninth public schema; the plan and result schemas
are the tenth and eleventh. All three are additive post-v0.1 contracts.
Publishing them does not change the bytes, fields, validation, or meaning of
any of the eight original v1 schemas. None is added to the closed sixteen-role
evidence-bundle inventory.

The Pydantic models in [`src/inferdrome/domain`](../src/inferdrome/domain) are
the reference implementation. ExitSpec and other independent consumers must
not import Inferdrome runtime modules; they vendor the schemas and conformance
vectors and independently implement the semantic checks described here.

## Validation model

Validation has three layers:

1. JSON Schema validates closed object shapes, required fields, primitive
   types, bounds, patterns, discriminated unions, and enums.
2. Semantic validation enforces relationships across fields, such as derived
   request IDs, exact metric semantics, phase ordering, and evidence
   eligibility.
3. Bundle verification validates exact artifact bytes, manifest coverage,
   producer-specific native normalization, and deterministic recalculation.

Passing JSON Schema alone does not waive a documented semantic invariant.
Conformance fixtures exercise both the independent `jsonschema` validator and
the reference models. Semantic-only mutation tests exercise relationships that
JSON Schema cannot express without duplicating application logic.

All public objects reject unknown fields. Numbers are strict: integer fields do
not accept booleans or floating-point values. Non-integer deterministic values
are decimal strings, never binary JSON floats.

## Versioning and compatibility

- Every document has one exact `inferdrome.*.v1` schema-version literal.
- Unknown schema, producer, definition, or semantics versions fail closed.
- A field addition, removal, type change, enum change, requiredness change, or
  meaning change requires a new public schema version.
- A new version is published alongside v1; readers never silently reinterpret
  or auto-upgrade old evidence.
- Deprecation requires a replacement schema and a documented migration path.
  The v1 bytes and meanings remain available for existing bundles.
- Producer compatibility is exact in v0.1: real evidence names vLLM `0.26.0`
  and the version-specific adapter semantics.
- Adding aggregate, plan, or result contracts alongside the frozen schemas does
  not permit an existing v1 evidence document to acquire new fields or meanings.

## Shared conventions

Identifiers and paths are intentionally narrow:

```text
run ID       run- + 32 lowercase hexadecimal characters
trial set ID trial-set- + 32 lowercase hexadecimal characters
comparison plan ID   comparison-plan- + 32 lowercase hexadecimal characters
comparison result ID comparison-result- + 32 lowercase hexadecimal characters
request ID   req- + an eight-digit zero-padded sequence index
digest       sha256: + 64 lowercase hexadecimal characters
path         normalized relative POSIX path with no dot segments
```

Artifact hashes use SHA-256 over exact file bytes. Semantic digests use RFC
8785 canonical JSON where applicable and one of these domain separators:

```text
inferdrome:source-spec-v1\0
inferdrome:execution-fingerprint-v1\0
inferdrome:request-plan-v1\0
inferdrome:metric-definitions-v1\0
inferdrome:bundle-manifest-v1\0
inferdrome:trial-set-v1\0
inferdrome:comparison-plan-v1\0
inferdrome:comparison-result-v1\0
```

The source-spec digest applies its domain separator to the exact original
source bytes, not to the resolved experiment document. The bundle digest is
the domain-separated digest of the canonical artifact-hash manifest. It is
emitted out of band and is deliberately absent from `bundle.json` to avoid a
circular hash dependency.

## Experiment and request plan

`inferdrome.experiment.v1` is the fully resolved document, not the user-authored
YAML shape. Defaults, producer identity, bounded execution limits, target,
traffic, measurement semantics, sensitivity, and acceptance linkage are
explicit before measurement starts.

Attached endpoint URLs permit only `http` or `https` and reject user
information, query strings, and fragments. This prevents API keys and similar
secrets from entering canonical evidence through a URL. vLLM detailed native
output is always classified as response-content-bearing. vLLM `0.26.0` has no
per-request timeout option; v0.1 therefore promises only the total runtime and
bounded process-cancellation limits.

`inferdrome.request-plan.v1` contains only measured requests. Entries are
ordered by contiguous `sequence_index`; canonical and producer request IDs are
verified from that order and the frozen prefix. There are no automatic retries
in v0.1, so one planned logical request maps to one producer request.

An inline prompt is verified against its UTF-8 SHA-256 digest and permits
`FULL` replayability. A digest-only prompt forces `LIMITED` replayability. A
workload-file hash by itself is never presented as a full request plan.

## Canonical request observations

`inferdrome.request-record.v1` maps exactly one measured native-array row to
one canonical request. Its source locator and sequence index must agree. The
synthetic producer uses the same canonical observation shape while identifying
itself as `inferdrome_fake`; it never impersonates vLLM evidence.

The timing semantics are producer-specific:

- `start_offset_ns` is the producer's measured-request start time normalized to
  one producer monotonic origin.
- `ttft_ns` uses `vllm_first_choices_event_v0_26`: the first streamed event
  whose `choices` array is non-empty. A role-only or empty-content choice still
  counts because that is what the pinned producer measures.
- `itl_ns` contains intervals between subsequent non-empty-`choices` events.
  This includes a later finish-reason choice; usage-only events with an empty
  `choices` array do not add an interval.
- `last_choices_event_span_ns`, when reduced, is `ttft_ns + sum(itl_ns)`. It is
  not terminal response latency.

`SUCCESS` requires observed TTFT and a response digest. `FAILED` preserves the
producer error text and may retain observations captured before failure.
`ANOMALOUS_EMPTY_STREAM` represents an error-free native row with no choices
events and an empty response.

The v1 request record intentionally has no attempt ledger, scheduled offset,
completion offset, per-request E2E latency, upstream TPOT, HTTP status, finish
reason, raw event stream, or exact achieved-concurrency observation. These
values are not reconstructed from configuration, aggregate summaries, or
error text.

## Environment and execution

`inferdrome.environment.v1` enumerates every field in the fixed v1 allowlist.
Each field carries its own provenance. `UNKNOWN` requires both value and
evidence path to be absent. `COMPLETE`, `PARTIAL`, and `UNKNOWN` are derived
from those field-level states rather than asserted independently.

`inferdrome.execution.v1` is final evidence for a `COMPLETE` execution. It
records configured traffic, four ordered phase declarations, zero producer exit
status, exact native-result hash, and the producer's aggregate
measurement-window duration. Each phase marks timing as `OBSERVED` with both
aware UTC boundaries or `UNAVAILABLE` with both boundaries null. The fake
adapter observes its synthetic boundaries; the pinned vLLM command does not
expose structured preflight, warmup, measuring, or finalizing UTC boundaries.
Failed and interrupted orchestration state is retained in the run workspace but
cannot masquerade as this completed public artifact. Latency observations
remain in the separate monotonic domain. The aggregate window does not create
per-request completion times or exact concurrency. The fake adapter has a
separate explicit measurement-window definition.

## Frozen metric semantics

Every metric definition is checked as one indivisible tuple of definition ID,
unit, population, allowed aggregation, source observations, quantile method,
and rounding policy. A familiar metric name with any mismatched component is
invalid.

| Metric | Definition |
|---|---|
| `measured_request_count` | Count of all measured request records |
| `successful_request_count` | Count with status `SUCCESS` |
| `failed_request_count` | Count with status `FAILED` or `ANOMALOUS_EMPTY_STREAM` |
| `error_rate` | Failed count divided by measured count |
| `ttft_ns` | Producer first-choices-event TTFT over successes with observed TTFT |
| `last_choices_event_span_ns` | `ttft_ns + sum(itl_ns)` over the same population |
| `attempted_request_throughput_per_s` | Measured count divided by measurement-window seconds |
| `successful_request_throughput_per_s` | Successful count divided by measurement-window seconds |
| `output_token_throughput_per_s` | Successful output-token sum divided by measurement-window seconds |

Counts and nearest-rank quantiles are integer values. Means, ratios, and rates
are non-negative decimal strings rounded with decimal half-even to six places.
Latency quantiles use `nearest_rank_v1`. A count's `sample_count` equals the
size of its named population. Undefined empty-population latency aggregates
are omitted rather than serialized as zero.

The measurements artifact always carries the ordered explicit exclusion set
for first-content TTFT, terminal E2E latency, upstream TPOT, exact achieved
concurrency, scheduled offsets, HTTP status, and finish reason. Upstream
aggregate summaries may remain in native diagnostics but are not verdict-grade
canonical measurements.

## Evidence descriptor

`inferdrome.evidence.v1` describes one complete, integrity-valid bundle. Every
normative artifact role appears exactly once under a unique safe relative path.
The descriptor distinguishes execution state, integrity status, environment
completeness, replayability, and evidence eligibility.

The fake producer is always `SYNTHETIC_ONLY`. Real vLLM evidence uses attached
endpoint mode and cannot hide that detailed native output contains response
content. Integrity proves consistency with the received bytes; it does not
prove authorship, trusted execution, or that an attached server reported truth.

## Descriptive Trial Set aggregate

`inferdrome.trial-set.v1` is an immutable aggregate over 2 through 100 completed
runs of one execution condition. It is created after its member runs and is
therefore retrospective. Its timestamp and optional hypothesis are metadata,
not evidence that a design, outcome, repeat count, or exclusion policy was
predeclared.

Each ordered member carries a contiguous zero-based `repetition_index`, one
`run_id`, and that run's retained `bundle_digest`. Run IDs and bundle digests
are independently unique. Verification resolves IDs beneath one explicit runs
root, requires each digest, applies full offline bundle verification, and
authoritatively recalculates every member.

Every member must share the descriptor's experiment ID, execution fingerprint,
metric-definitions digest, and reducer version. Request-plan digests need not
match because each run has run-specific request identity. Canonical request
records remain separate per run and are never pooled into one synthetic
population.

Descriptive variation gives one available run-level measurement scalar one
unit of weight, regardless of that run's request sample count. It preserves all
member points and availability, then uses deterministic Decimal arithmetic for
minimum, median, maximum, arithmetic mean, span, and sample standard deviation.
Unavailable measurements remain unavailable rather than becoming zero.

The execution fingerprint does not include every observed environment field.
Consumers must keep evidence integrity, same-condition membership, environment
drift, and statistical interpretation separate. The dashboard discloses
projected environment differences; it does not turn them into experimental
variables or a causal explanation.

The Trial Set digest is SHA-256 over the exact canonical descriptor bytes under
`inferdrome:trial-set-v1\0`. It is emitted and retained out of band rather than
embedded in the descriptor. Expected-digest verification anchors the exact
aggregate bytes but does not prove authorship, execution truth, or pre-run
timing.

The descriptor is bounded to 262,144 bytes and 100 members. It is strict,
closed, canonical, read-only, and addressed by a narrow Trial Set ID. Safe
readers reject arbitrary paths, symlinks, writable or nonregular descriptors,
duplicate identities, changed bytes, invalid members, and stale aggregate
state.

The Trial Set contract alone does not define a controlled comparison,
confidence or significance, causality, prefix caching, metric directionality,
or ExitSpec-owned `PASS`, `FAIL`, and `NOT_PROVEN` outcomes. See
[TRIAL_SETS.md](TRIAL_SETS.md) and
[ADR 0007](adr/0007-add-immutable-descriptive-trial-sets.md).

## Controlled-comparison plan and result

`inferdrome.controlled-comparison-plan.v1` freezes exactly two arms before the
operator executes the local workflow. It contains full resolved specifications,
source digests, expected fingerprints, preallocated run and Trial Set IDs, a
seeded permuted-pair schedule, one reviewed `traffic.concurrency` treatment,
one fully typed primary outcome, a complete-case paired estimator, no
post-assignment exclusions, and no uncertainty method.

The plan's `PREDECLARED` state has assurance `OPERATOR_ATTESTED`. Its local
timestamp, reserved-run check, and retained digest express the intended
workflow but are not trusted timestamping, authorship, or adversarial proof
that the plan preceded execution.

`inferdrome.controlled-comparison-result.v1` pins the exact plan and both exact
Trial Set digests. Authoritative verification recalculates every member bundle
and derives six closed controls covering local plan order, exact membership,
observed schedule, declared fingerprint difference, complete and equal
observed-v1 environment, and outcome coverage and semantics.

All controls must be `SATISFIED` for `COMPARABLE`. Any failure produces
`INCOMPARABLE` and the schema requires every outcome, arm summary, paired
difference, and estimate to be absent. A comparable estimate is the Decimal
arithmetic mean of equal-weight candidate-minus-baseline run-pair differences,
rounded half-even to six places. Separate request populations are never pooled.

The plan digest and result digest hash exact canonical descriptor bytes under
their respective domains and remain out of band. They anchor received bytes;
they do not prove execution truth or chronology. `COMPARABLE` covers only the
declared and observed `OBSERVED_V1_ALLOWLIST_ONLY` scope. It does not mean
causal, significant, preferred, customer-eligible, or accepted. See
[CONTROLLED_COMPARISONS.md](CONTROLLED_COMPARISONS.md) and
[ADR 0008](adr/0008-add-operator-attested-controlled-comparisons.md).

## Developer commands

From the repository root after installing the locked development environment:

```bash
PYTHONPATH=src python scripts/generate_schemas.py
PYTHONPATH=src python scripts/generate_schemas.py --check
INFERDROME_PYTHON=.venv/bin/python ./scripts/engineering_gate.sh
```

The generator is deterministic. A schema change is incomplete until generated
bytes, valid fixtures, invalid fixtures, semantic mutations, typing, and the
full test suite all agree.
