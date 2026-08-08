# ADR 0008: Add operator-attested controlled comparisons

- Status: Accepted
- Date: 2026-08-07
- Scope: post-v0.1, v0.2 second vertical slice

## Context

Trial Sets expose repeated run-level variation, but they are retrospective and
cannot establish that a hypothesis, treatment, schedule, outcome, or exclusion
policy was fixed before execution. The dashboard's two-run comparator is also
descriptive: it can disclose context changes, but it does not prove that only
one reviewed treatment changed.

Inferdrome needs a stricter comparison workflow without absorbing ExitSpec's
acceptance authority or claiming causal inference. The workflow must remain
honest about a second limitation: current bundles have no trusted timestamp or
signed assignment binding to a comparison-plan digest. Local timestamps and a
retained digest can record an operator workflow, but cannot prove chronology to
an adversarial verifier.

## Decision

Inferdrome adds two additive public contracts:

```text
inferdrome.controlled-comparison-plan.v1
inferdrome.controlled-comparison-result.v1
```

The plan and result remain separate immutable artifacts. Both use canonical
JSON, closed schemas, narrow identifiers, and distinct domain-separated
digests. Neither is inserted into an existing evidence bundle.

The first plan version is deliberately narrow:

- exactly two disjoint arms, baseline and candidate;
- 2 through 100 planned runs per arm;
- preallocated run IDs and Trial Set IDs;
- exactly one reviewed treatment, `traffic.concurrency`;
- complete resolved specifications, source-spec digests, and expected
  execution fingerprints for both arms;
- exactly one fully typed primary outcome;
- one metric-definition digest and reducer version;
- seeded, auditable within-pair execution ordering;
- statistical unit `run` and equal-per-run weighting;
- no post-assignment exclusions;
- complete-case outcome policy; and
- no uncertainty method in v1.

Plan validation recomputes both execution fingerprints and requires the two
complete resolved specifications to differ at exactly
`traffic.concurrency`, with the declared baseline and candidate integer values.
Arbitrary JSON paths and unreviewed treatment names are rejected.

Plan creation refuses any run ID that already exists beneath the selected runs
root. The artifact records `PREDECLARED`, but its assurance is explicitly
`OPERATOR_ATTESTED`. This is a workflow property, not trusted proof of pre-run
chronology. A plan digest must be retained out of band and supplied when a
result is created. Independent chronology would require a future signed
assignment binding and trusted timestamp or transparency receipt.

Each result references the exact plan digest and one exact Trial Set digest per
arm. Verification recalculates every member bundle and derives six closed
controls:

```text
LOCAL_PLAN_ORDER
EXACT_ARM_MEMBERSHIP
OBSERVED_SCHEDULE
DECLARED_FINGERPRINT_DIFFERENCE
COMPLETE_EQUAL_OBSERVED_ENVIRONMENT
OUTCOME_COVERAGE_AND_SEMANTICS
```

`COMPARABLE` means all six declared and observed v1 controls are satisfied.
`INCOMPARABLE` means at least one is not. An incomparable result contains no
outcome estimate. These statuses are comparison-scope states, not ExitSpec
`PASS`, `FAIL`, or `NOT_PROVEN` outcomes.

Environment comparison requires `COMPLETE` evidence and exact equality across
the current v1 allowlist, including values, provenance, and evidence paths. The
contract names this scope `OBSERVED_V1_ALLOWLIST_ONLY`; it does not claim that
every real-world confounder was observed. Ordinary attached-endpoint and
synthetic runs with unknown provenance therefore remain incomparable.

For a comparable result, Inferdrome preserves every run-level primary value,
sample count, and paired candidate-minus-baseline difference. The estimator is
the arithmetic mean of paired run differences. With a complete balanced design
this equals candidate arm mean minus baseline arm mean. No request records are
pooled and no run receives weight from its request count.

The local dashboard adds `/comparisons` and `/comparisons/:planId`. It presents
the frozen design before comparability controls, suppresses estimates for
incomparable or withheld results, and retains `/compare` as a separate ad hoc
two-run utility.

The complete contract is documented in
[CONTROLLED_COMPARISONS.md](../CONTROLLED_COMPARISONS.md).

## Explicit non-goals

This decision does not add:

- trusted predeclaration chronology, authorship, signing, or execution
  attestation;
- arbitrary or multiple treatments;
- multiple outcomes, replacement runs, imputation, or available-case dropping;
- confidence intervals, p-values, significance, power, or causal attribution;
- winner, recommendation, optimization, improvement, or regression labels;
- prefix-caching controls;
- telemetry-backed explanations; or
- customer acceptance verdicts.

## Consequences

- Controlled comparisons become portable and independently recalculable while
  preserving the immutable run and Trial Set boundaries.
- The narrow treatment union prevents labels from masquerading as execution
  controls.
- Full resolved specifications catch changes outside the existing execution
  fingerprint projection.
- A failed control suppresses all arithmetic rather than publishing a reduced
  or selectively filtered estimate.
- `OPERATOR_ATTESTED` and `OBSERVED_V1_ALLOWLIST_ONLY` make current assurance
  limits machine-readable and visible in the UI.
- Future treatments, uncertainty methods, or trusted chronology require new
  reviewed contract versions.

## Rejected alternatives

### Extend the ad hoc two-run comparator

Rejected because it has no preallocated membership, frozen schedule, exact
treatment-diff requirement, or complete-case repeated-run estimator.

### Accept arbitrary independent-variable paths

Rejected because a string path is not evidence that execution and fingerprint
semantics exist for that control.

### Treat local timestamps as proof of predeclaration

Rejected because an operator can backdate self-reported timestamps. The v1
claim remains operator-attested.

### Publish estimates when only some planned runs are available

Rejected because post-assignment missingness would silently change the design
and sample.

### Call a comparable result a winner or acceptance outcome

Rejected because arithmetic direction does not encode preference, causality,
customer thresholds, or evidence sufficiency.
