# Inferdrome v0.3 release inputs

Status: **No v0.3 release is currently authorized.**

This is the active release-series input contract for a future `0.3.0` release.
It follows the v0.2 release series without rewriting its source history. In
particular, v0.2 release history does not authorize a v0.3 release, complete a
v0.3 checklist item, or establish v0.3 release readiness.

## Required v0.3 release inputs

Before a final-pre-tag v0.3 review can be considered ready, the release record
must identify the exact final source commit, package version, and annotated tag.
It must also include fresh v0.3 security and evidence-integrity review records.
The record must include all three required CI job URLs for the exact final source commit. Those are release-record inputs; the repository preflight
validates only the bounded local structure around them.

The matching manual requirements are maintained in [the v0.3 release
checklist](V0_3_RELEASE_CHECKLIST.md). They are separate from v0.2 history so
no earlier approval, review, or evidence record can be reused as v0.3
authorization.

The active preflight also verifies the exact `v0.1.0` baseline for every file
under `schemas/public/v1`, `schemas/deployment/v1`, `evidence`, and
`docs/reviews`. That is historical-integrity checking only: it preserves those
records without turning them into v0.3 release inputs.

## Authority and integrity boundary

Content-addressed local bindings are not signatures. They detect changes to a
canonical local payload, but they do not prove an operator identity, Jayesh's
authorization, a provider authorization, or a release decision. An explicit
human release record remains required for those claims.

This document does not enable cloud execution, provider credentials, GPU work,
or artifact publication. Any future execution authority remains separately
reviewed and opt-in.
