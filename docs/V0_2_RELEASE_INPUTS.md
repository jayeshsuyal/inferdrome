# Inferdrome v0.2 release inputs

Status: **No v0.2 release is currently authorized.**

This is the active release-series input contract for a future `0.2.0` release.
It is additive to, and does not amend, the frozen v0.1 producer-side release
records. In particular, v0.1 closure records do not authorize a v0.2 release,
complete a v0.2 checklist item, or establish v0.2 release readiness.

## Required v0.2 release inputs

Before a final-pre-tag v0.2 review can be considered ready, the release record
must identify the exact final source commit, package version, and annotated tag.
It must also include fresh v0.2 security and lifecycle review records. The record
must include all three required CI job URLs for the exact final source commit.
Those are release-record inputs; the repository preflight validates only the
bounded local structure around them.

The matching manual requirements are deliberately maintained in
[the v0.2 release checklist](V0_2_RELEASE_CHECKLIST.md). They are separate
from the historical v0.1 checklist so a completed or deferred v0.1 record
cannot be reused as v0.2 authorization.

The active preflight also hashes the named frozen v0.1 closure and review
files against their `v0.1.0` bytes. That is a historical-integrity check only:
it preserves those records without turning them into v0.2 release inputs.

## Authority and integrity boundary

Content-addressed local bindings are not signatures. They detect changes to a
canonical local payload, but they do not prove an operator identity, Jayesh's
authorization, a provider authorization, or a release decision. An explicit
human release record remains required for those claims.

This document does not enable GCP execution, provider credentials, GPU work,
or artifact publication. Any future execution authority remains separately
reviewed and opt-in.
