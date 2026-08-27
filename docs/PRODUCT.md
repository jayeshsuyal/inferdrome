# Inferdrome product charter

Status: **Frozen for v0.1**

Charter date: **2026-08-05**

This document freezes Inferdrome's mission, ownership boundaries, vocabulary,
product claims, v0.1 scope, and non-goals. The vLLM capability spike defined in
[ROADMAP.md](ROADMAP.md) is complete. Exact public schema shapes were frozen in
PR 1 and are documented in [PUBLIC_CONTRACTS_V1.md](PUBLIC_CONTRACTS_V1.md).
They are bounded by the verified
[capability matrix](../spikes/vllm-0.26.0/CAPABILITY_MATRIX.md).

## Mission

> Inferdrome is a reproducible evidence pipeline for LLM-serving experiments.

Given an experiment specification, Inferdrome:

1. Validates and resolves the execution inputs under its control.
2. Records unresolved, externally declared, and unavailable inputs explicitly.
3. Freezes a canonical execution specification and request plan.
4. Invokes or attaches to a pinned benchmark tool and target endpoint.
5. Preserves the benchmark tool's native output.
6. Normalizes supported request-level observations without inventing data.
7. Deterministically computes versioned measurements.
8. Seals the run as a portable, integrity-verifiable evidence bundle.
9. Hands the bundle to ExitSpec or another independent verifier.

Inferdrome produces measurements. ExitSpec owns customer acceptance.

## The problem

A performance number without its experimental context is not durable evidence.
For example, an output-throughput value alone does not identify:

- the model and tokenizer artifacts;
- the serving engine and launch configuration;
- the benchmark producer and invocation;
- the hardware and software environment;
- the exact request plan and traffic model;
- streaming and metric semantics;
- warmup and measurement populations;
- failures, achieved load, or missing observations; or
- whether the result can be replayed or compared.

Inferdrome exists to preserve that context and make every derived measurement
traceable to canonical request records and their native source artifact.

## Product claims

For v0.1, Inferdrome may claim:

- schema-valid and internally consistent evidence;
- exact-byte artifact integrity after a bundle is sealed;
- explicit provenance for declared and observed environment fields;
- deterministic recalculation from canonical records; and
- a portable handoff to an independent acceptance evaluator such as ExitSpec.

Inferdrome v0.1 must not claim:

- proof that a potentially malicious producer actually performed the run;
- proof of artifact authorship;
- trusted hardware attestation;
- complete server provenance for an attached endpoint;
- response determinism across otherwise equivalent runs; or
- causal explanations for performance changes without telemetry.

When ExitSpec supplies an ingestion receipt, it anchors the bundle digest that
ExitSpec received. It does not attest that the described execution occurred.
The receipt, acceptance outcomes, and importer are separately owned external
ExitSpec work; this repository does not contain them. The complete security
boundary is defined in [THREAT_MODEL.md](THREAT_MODEL.md).

## Ownership boundaries

| Component | Owns | Explicitly does not own |
|---|---|---|
| Inferdrome | Experiment resolution, benchmark execution, normalization, measurements, bundle integrity | Customer acceptance verdicts or trusted execution attestation |
| ExitSpec | Evidence sufficiency and evaluation against a frozen customer contract | Load generation, GPU deployment, or benchmark normalization |
| Benchmark tool | Request scheduling and client-side observations for its supported protocol | Cross-run evidence management or customer acceptance |
| vLLM or another serving target | Model serving, scheduling, batching, and caching | Client-side evidence validity or acceptance policy |
| Future router | Per-request route, model, and provider decisions | Measurement validity or evidence sufficiency |

The allowed outcomes remain deliberately separate:

```text
Inferdrome:
  execution status
  integrity status
  environment completeness
  evidence eligibility

ExitSpec:
  ingestion status
  PASS / FAIL / NOT_PROVEN
```

A successfully completed, integrity-valid Inferdrome run can still produce an
ExitSpec `FAIL` or `NOT_PROVEN` verdict.

## Core vocabulary

### Experiment

A hypothesis or measurement objective and the configurations intended to test
it.

### Source specification

The user-authored experiment document before defaults, path resolution,
capability checks, or hash calculation.

### Resolved specification

The canonical, immutable execution document produced by the resolver. It
contains explicit defaults, resolved references, producer versions, capability
requirements, and provenance declarations.

### Request plan

The ordered, post-resolution workload that the benchmark adapter is expected to
execute. It captures request inputs or approved content references, generation
parameters, traffic semantics, and deterministic ordering information.

A workload source hash alone is not a replayable request plan.

### Run

Exactly one benchmark execution. A repetition is a separate run.

### Logical request

One workload item whose outcome is measured. v0.1 performs no automatic
retries, so one logical request corresponds to one network attempt. A future
retry-capable schema must model attempts separately rather than hiding or
overwriting them.

### Native artifact

Untouched output emitted by the pinned benchmark producer, including its result
file, standard output, standard error, invocation metadata, and exit status.

### Canonical request record

An Inferdrome-owned, versioned representation of one measured logical request.
It contains only observations supported by the adapter capability contract.
Unavailable fields remain null or absent according to the public schema; they
never receive guessed values.

### Measurement

A deterministic, versioned aggregation over a named record population.

### Evidence bundle

The sealed, portable output of one run. It contains the resolved execution
context, request plan or replayability declaration, native artifacts, canonical
records, metric definitions, derived measurements, and integrity manifest.

### Acceptance evaluation

An ExitSpec-owned evaluation of an evidence bundle against a frozen contract.

### Trial set and comparison

Post-v0.1 concepts for repeated runs and controlled comparisons. Separate runs
must never be pooled into one fabricated request population.

## State model

### Run state

```text
CREATED
PREFLIGHT
WARMUP
MEASURING
FINALIZING
COMPLETE
FAILED
INTERRUPTED
```

`FAILED` means the harness could not complete its run lifecycle. Measured HTTP
errors do not automatically make a run failed; an overloaded endpoint can be a
successfully completed measurement.

`COMPLETE` means final artifacts were written, the run was sealed, and the
sealed bundle passed Inferdrome's own offline verification.

### Integrity status

```text
NOT_CHECKED
VALID
INVALID
```

Integrity covers declared artifacts, exact-byte hashes, schema validity,
identifier uniqueness, timestamp ordering, record populations, and reducer
consistency. It does not prove authorship or execution truth.

### Environment completeness

```text
COMPLETE
PARTIAL
UNKNOWN
```

This is a derived summary. Each environment field retains its own provenance,
such as `DECLARED`, `CLIENT_OBSERVED`, `SERVER_REPORTED`, `LOCALLY_VERIFIED`, or
`UNKNOWN`.

An attached endpoint will normally be `PARTIAL` because exact launch flags,
model bytes, or GPU state may be unavailable.

### Evidence eligibility

```text
CUSTOMER_ELIGIBLE
SYNTHETIC_ONLY
INELIGIBLE
```

The fake adapter always produces `SYNTHETIC_ONLY`. Eligibility is policy input,
not an acceptance verdict.

## v0.1 scope

v0.1 contains:

- source-spec validation and canonical resolution;
- a frozen request plan or an explicit limited-replayability declaration;
- one fake adapter for golden and integration tests;
- one exact, pinned vLLM benchmark compatibility target;
- an attached OpenAI-compatible endpoint profile;
- native-output preservation;
- canonical measured-request records;
- deterministic request counts, error rate, producer first-choices TTFT,
  last-choices-event span, attempted and successful request throughput, and
  successful output-token throughput;
- explicit unavailability for terminal per-request latency, upstream TPOT,
  first-content TTFT, exact concurrency, scheduled offsets, HTTP status, and
  finish reason;
- a portable directory bundle with exact-byte hashes;
- offline bundle verification;
- an external ExitSpec handoff boundary; importer, receipt, and acceptance
  evaluation are not Inferdrome implementation; and
- producer-side genuine GPU evidence plus corrupted-evidence and
  synthetic-evidence rejection demonstrations. ExitSpec `PASS`, `FAIL`, and
  `NOT_PROVEN` demonstrations remain external release blockers.

## v0.1 non-goals

The following are explicitly delayed:

- a custom load generator;
- retries;
- GuideLLM and executable or eligible second-serving-engine adapters;
- statistical trial-set comparison;
- Prometheus, vLLM, DCGM, or Nsight telemetry;
- router-decision analysis;
- Parquet, DuckDB, Pandas, or Polars;
- remote Docker, Kubernetes, or cloud provisioning;
- a hosted API, database, multi-user authentication system, or hosted dashboard
  (the accepted local dashboard is post-v0.1 and outside the release gate);
- automatic optimization or AI-generated tuning advice;
- quality evaluation;
- artifact signing or trusted execution attestation; and
- claims explaining why a latency change occurred.

## Product invariants

1. Original and resolved specifications are both retained.
2. The resolved specification and request plan cannot change after measurement
   begins.
3. Native output is never silently rewritten.
4. Canonical records identify their native source location.
5. Missing observations are never represented as zero.
6. Warmup, preflight, and measured populations are never silently mixed.
7. Failed measured requests are retained.
8. Derived outputs identify their population, units, definition version,
   quantile method, reducer version, and rounding policy.
9. Completed bundles are immutable; verification and recalculation are
   read-only operations.
10. ExitSpec never trusts an Inferdrome summary used for a verdict without
    recalculating it from canonical records.
11. Synthetic fixtures cannot pass through the honest customer-evidence path.
12. Unknown upstream formats fail explicitly rather than being guessed.

## What is frozen

The following decisions are frozen through v0.1:

- the mission and Inferdrome/ExitSpec ownership boundary;
- one run per benchmark execution;
- native output plus canonical records plus deterministic derivation;
- integrity verification rather than authorship claims;
- immutable completed bundles;
- explicit evidence eligibility and provenance;
- vLLM as the first real benchmark producer; and
- the delayed-feature list above.

Changing a frozen decision requires a new architecture decision record.

## Capability constraints established by PR 0.5

The completed spike established:

- vLLM `0.26.0` as the exact producer target;
- first-nonempty-`choices` TTFT semantics rather than first-content TTFT;
- aligned native arrays and frozen-order request-ID derivation;
- producer error text without native HTTP status or finish reason;
- measured-request rows without request-level preflight or warmup rows;
- response-content-bearing detailed native output; and
- no native per-request terminal latency, exact scheduled offsets, retry
  ledger, or exact achieved-concurrency evidence.

The exact `inferdrome.*.v1` field names and object shapes are frozen in
[PUBLIC_CONTRACTS_V1.md](PUBLIC_CONTRACTS_V1.md). They express only `OBSERVED`
and strictly specified `DERIVABLE` capabilities from the matrix. This prevents
the public evidence format from promising data the upstream benchmark does not
expose.
