# ADR 0001: Separate measurement from customer acceptance

- Status: Accepted
- Date: 2026-08-05
- Scope: v0.1 and later

## Context

Benchmark execution and customer acceptance answer different questions.
Inferdrome can determine what observations were captured and how metrics were
calculated. A customer contract determines whether those measurements are
sufficient and acceptable for a particular target.

Combining both responsibilities would make benchmark code depend on customer
policy, encourage mutable thresholds, and blur invalid evidence with valid
evidence that demonstrates failure.

## Decision

Inferdrome owns experiment resolution, execution, normalization, deterministic
measurement, eligibility labeling, and evidence integrity.

ExitSpec owns contract applicability, evidence sufficiency, and the final
`PASS`, `FAIL`, or `NOT_PROVEN` verdict.

Inferdrome does not expose a customer `PASS` or `FAIL` state. ExitSpec does not
generate benchmark traffic or import Inferdrome runtime modules.

## Consequences

- Valid evidence may legitimately produce `FAIL`.
- Integrity failure is an ingestion rejection, not a failed performance test.
- ExitSpec must independently recalculate metrics used for a verdict.
- The integration contract is the public evidence format and conformance suite.
- Both repositories carry some schema and metric compatibility work.

## Rejected alternatives

### Inferdrome evaluates customer thresholds

Rejected because it couples measurement production to acceptance policy and
makes evidence less portable.

### ExitSpec trusts Inferdrome summaries

Rejected because a corrupted or buggy summary would become an unchecked verdict
input.
