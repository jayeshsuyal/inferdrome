# ADR 0003: Preserve native output and publish canonical records

- Status: Accepted
- Date: 2026-08-05
- Scope: v0.1 and later

## Context

Native benchmark output is necessary for debugging upstream changes and
auditing normalization. Native output alone is unstable across producer
versions and unsuitable as a durable interface for ExitSpec.

Discarding native output would make canonical records impossible to trace.
Using native output directly would couple all consumers to each producer.

## Decision

Every real run preserves untouched native benchmark artifacts and produces
versioned canonical request records.

Every canonical record includes a native source locator. Normalizers are
producer-version-specific and fail explicitly on unsupported structural shapes.
Derived measurements consume canonical records rather than producer summaries.

Detailed native content determines bundle sensitivity. Canonical redaction does
not override content retained in a native artifact.

## Consequences

- Evidence bundles are larger than summary-only reports.
- Normalization can be audited against an exact native artifact.
- ExitSpec can remain producer-agnostic while independently aggregating records.
- A future verifier that wants independent normalization will need the adapter
  specification or its own producer-specific implementation.
- v0.1 demonstration bundles containing detailed generated text must be labeled
  as response-content-bearing.

## Rejected alternatives

### Store only canonical records

Rejected because adapter bugs and upstream-format changes would be difficult to
diagnose or audit.

### Store only native output

Rejected because ExitSpec would become coupled to every producer's unstable
format and aggregation semantics.

### Rewrite native output to remove content

Rejected as the default because the result would no longer be untouched native
evidence. Redacted derivatives or encrypted annexes require a future ADR.
