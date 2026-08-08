# ADR 0007: Add immutable descriptive Trial Sets

- Status: Accepted
- Date: 2026-08-07
- Scope: post-v0.1, v0.2 first vertical slice

## Context

One verified run can establish what was measured in that run, but it cannot show
whether its run-level result is typical across repetitions. The initial
dashboard can compare two runs arithmetically, but its
`COMPARABLE_WITH_CONTEXT_CHANGES` status means only that relevant differences
are disclosed. It is not proof that a variable was predeclared, that all other
conditions were controlled, or that a measured change was caused by that
variable.

Inferdrome therefore needs a repeated-run aggregate before it adds controlled
comparisons or richer charts. That aggregate must preserve v0.1's immutable
evidence, digest, population, and ownership boundaries. In particular, it must
not concatenate request records from separate executions or turn an
after-the-fact grouping into a predeclared experimental design.

## Decision

Inferdrome will add `inferdrome.trial-set.v1` as a ninth, additive public schema.
The eight existing public v1 schema files and their meanings remain
byte-for-byte unchanged.

A Trial Set is an immutable, retrospective grouping of 2 through 100 completed
runs representing one execution condition. It lives outside its member bundles
and contains ordered member references of:

```text
repetition_index
run_id
bundle_digest
```

Both identity fields are mandatory. Verification resolves each run beneath one
explicit runs root, requires the retained bundle digest, applies the ordinary
bounded offline verifier, and authoritatively recalculates measurements. It
then requires every member to share one experiment ID, execution fingerprint,
metric-definitions digest, and reducer version. Run IDs and bundle digests are
unique, and repetition indices are contiguous and ordered.

The descriptor itself declares `RETROSPECTIVE`,
`separate_per_run_v1`, statistical unit `run`, and `equal_per_run` weighting.
Those limits therefore travel with the aggregate instead of existing only as
dashboard copy.

The execution fingerprint is the membership key for measurement-affecting
configuration. Request-plan digests are not required to match because plans
contain run-specific identity and each run owns a separate request population.

Trial Set summaries operate on per-run measurement scalars with equal run
weighting. They expose all run-level values and availability, then derive
deterministic Decimal minimum, median, maximum, arithmetic mean, span, and sample
standard deviation. Requests are never pooled across runs, unavailable values
never become zero, and a run with more requests does not receive more weight.

Environment context is disclosed separately. A shared execution fingerprint
does not guarantee that every observed GPU, driver, CUDA,
producer-distribution, or client-environment fact matches. The dashboard reports
projected environment drift by field. Drift does not become an undeclared
treatment, and the absence of detected drift does not prove a fully controlled
environment.

The canonical descriptor is hashed under the domain separator
`inferdrome:trial-set-v1\0`. Its digest is emitted out of band and may be
required during verification and summarization. It is not embedded in the
descriptor, avoiding a circular dependency. The digest anchors exact bytes but
does not prove authorship, execution truth, or pre-run timing.

The CLI owns three operations:

```text
inferdrome trial-set create
inferdrome trial-set verify
inferdrome trial-set summarize
```

The local dashboard adds read-only browser routes `/trial-sets` and
`/trial-sets/:trialSetId`, backed by GET-only `/api/v1/trial-sets` and
`/api/v1/trial-sets/{trial_set_id}` APIs. Dashboard detail projections are
explicitly `RETROSPECTIVE` and `DESCRIPTIVE_ONLY`.

The implementation applies bounded canonical input, direct-child discovery,
safe ID, no-follow file, immutable descriptor, duplicate-ID, pagination,
snapshot, and recalculation checks. One failed or changed member invalidates the
aggregate; members are never silently removed.

The complete product and contract boundary is defined in
[TRIAL_SETS.md](../TRIAL_SETS.md).

## Explicit non-goals

This decision does not implement:

- a predeclared controlled-comparison design or comparison result;
- confidence intervals, significance, power, or causal inference;
- winner, recommendation, improvement, or regression labels;
- prefix-caching controls or a prefix-caching experiment;
- telemetry-backed explanations;
- pooled request populations; or
- ExitSpec-owned `PASS`, `FAIL`, or `NOT_PROVEN` outcomes.

A separate decision is required before any of those claims or contracts can be
added.

## Consequences

- Repeated runs become a portable, digest-addressable aggregate without
  mutating the underlying evidence.
- Run-to-run variation remains traceable to every member bundle and exact
  run-level value.
- Different per-run request counts cannot silently bias an aggregate through
  request pooling.
- Retrospective grouping remains visibly distinct from predeclaration and
  experimental control.
- Environment drift can be disclosed without pretending the current execution
  fingerprint proves complete environmental equivalence.
- Independent consumers gain one new schema to vendor while all existing v1
  schema bytes remain stable.
- A controlled-comparison slice can later consume Trial Sets as disjoint arms,
  but it must introduce its own plan, digest, policy, and result contract.

## Rejected alternatives

### Group runs automatically by title or experiment ID

Rejected because human labels and broad experiment identity do not prove equal
measurement-affecting execution conditions.

### Require equal request-plan digests

Rejected because request plans contain run-specific identity. Equality would
exclude honest repetitions while still not establishing a controlled design.

### Pool request records and calculate one larger quantile

Rejected because it erases run boundaries, weights runs by request count, and
changes the statistical unit without a reviewed contract.

### Treat a shared execution fingerprint as proof of a controlled experiment

Rejected because the grouping is retrospective, environment evidence may be
incomplete or drift, and no predeclared independent-variable plan exists.

### Add controlled comparisons and prefix caching in the same slice

Rejected because controlled comparison needs a separately frozen design and
prefix caching is not yet a typed, fingerprinted execution control.
