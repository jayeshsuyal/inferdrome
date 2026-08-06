# ADR 0004: Claim integrity, not execution truth or authorship

- Status: Accepted
- Date: 2026-08-05
- Scope: v0.1

## Context

An exact-byte hash manifest can reveal mutation relative to a known digest. It
cannot prove who created the bundle, whether a described run occurred, whether
an attached server reported truthfully, or whether the host was compromised.

Overstating these guarantees would weaken the product's central credibility.

## Decision

Inferdrome v0.1 describes its hash checks as artifact integrity verification.
It does not describe them as execution proof, trusted provenance, authorship, or
attestation.

Environment facts retain field-level provenance. ExitSpec's ingestion receipt
anchors the exact bundle digest it evaluated, but does not attest to the run.

The threat model is a PR 0 deliverable and a release gate rather than late-stage
hardening documentation.

## Consequences

- Product language is narrower but defensible.
- Attached-endpoint bundles are often only partially complete.
- A malicious producer can fabricate internally consistent evidence before an
  external trust anchor exists.
- Future signing or trusted execution work must state the additional guarantee
  precisely and cannot silently upgrade historical v0.1 evidence.

## Rejected alternatives

### Call the hash manifest cryptographic proof

Rejected because cryptographic hashing proves equality relative to a digest, not
the truth or authorship of the hashed statements.

### Delay the threat model until release hardening

Rejected because trust claims determine schemas, status semantics, tests, and
product language from the beginning.
