# ADR 0013: Authorize a producer-side v0.1 release with deferred ExitSpec evaluation

- Status: Accepted
- Date: 2026-08-31
- Scope: `v0.1.0` release timing only

## Context

Inferdrome and ExitSpec have separate ownership boundaries. Inferdrome can
produce and verify its own evidence, while ExitSpec owns independent import,
recalculation, receipt issuance, and customer-acceptance outcomes. The
original v0.1 definition of done made the three prospective outcomes and an
ingestion receipt release-blocking.

At the v0.1 release decision, those independently owned records are not
recorded. Recasting a producer bundle, frozen contract, rehearsal, or knowledge
of the ExitSpec workflow as an outcome or receipt would violate the evidence
boundary.

## Decision

Jayesh Suyal authorizes `v0.1.0` as an Inferdrome producer-side release without
waiting for independent prospective ExitSpec `PASS`, `FAIL`, and `NOT_PROVEN`
evaluations or an ingestion-receipt digest. The exact authorization and its
limitations are retained in
[the owner release-policy exception record](../reviews/V0_1_OWNER_RELEASE_POLICY_EXCEPTION.md).

This is a narrow exception to v0.1 release timing. It does not supersede the
measurement-versus-acceptance boundary in ADR 0001, change ExitSpec ownership,
or make any missing record present.

## Consequences

- The two ExitSpec checklist items remain unchecked and the four deferred
  records remain `NOT_RECORDED`.
- The final release PR, annotated tag, and GitHub Release must explicitly say
  that independent prospective ExitSpec evaluation and receipt publication are
  deferred post-v0.1.
- No performance acceptance, customer eligibility decision, receipt digest, or
  PASS/FAIL/NOT_PROVEN claim may be inferred from the release.
- The raw A10 archive remains `KEEP_EXTERNAL_ONLY` and `EXTERNAL_ONLY`; this
  decision grants no archive publication or redistribution authority.
- The ordinary repository and CI release gates, independent review, exact-tag
  integrity, security approval, and Apache-2.0 requirements remain unchanged.
