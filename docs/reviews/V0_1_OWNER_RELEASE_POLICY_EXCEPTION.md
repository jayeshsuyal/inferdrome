# v0.1 owner release-policy exception: deferred ExitSpec evaluation

**Record date:** 2026-08-31

**Release owner:** Jayesh Suyal

**Applies to:** the `v0.1.0` producer-side release decision only.

## Authorization recorded

On 2026-08-31, Jayesh Suyal explicitly authorized Inferdrome `v0.1.0`
to release without waiting for independently owned prospective ExitSpec `PASS`,
`FAIL`, and `NOT_PROVEN` evaluations or an ExitSpec ingestion-receipt digest.

This is an owner-approved change to the v0.1 release policy and timing. It is
not evidence that any of those evaluations or a receipt exists.

## Exact deferred state

The following are **NOT_RECORDED** as of this release decision and are deferred
to post-v0.1 work:

- independent prospective ExitSpec `PASS` evaluation;
- independent prospective ExitSpec `FAIL` evaluation;
- independent prospective ExitSpec `NOT_PROVEN` evaluation; and
- an ExitSpec ingestion-receipt digest.

The authorization neither changes ExitSpec ownership nor supplies, infers,
reconstructs, or substitutes for any of these records. Knowing that ExitSpec
can perform the work is not recorded evidence that it has done so.

## Release limitation and preserved boundaries

`v0.1.0` is an Inferdrome producer-side release. It must not be described as
an independently accepted customer-evidence release, as satisfying the
deferred evaluations, or as carrying an ExitSpec receipt.

The raw A10 archive
`sha256:f2408fd0649a7c79f5962872003781ebb9c878b802db27d633cf246f13b6f424`
remains `KEEP_EXTERNAL_ONLY`, `EXTERNAL_ONLY`, ignored, and unpublished. This
record grants no privacy, licensing, redistribution, publication, or
external-material right for that archive or any derivative.

This exception does not authorize GPU launch, provider or cloud mutation, SSH,
credential access, paid-resource use, or a change to ExitSpec or any other
external repository.

## Records governed by this exception

This record is the policy source for the linked
[v0.1 definition of done](../V0_1_DEFINITION_OF_DONE.md),
[release checklist](../V0_1_RELEASE_CHECKLIST.md), and
[ADR 0013](../adr/0013-authorize-producer-side-v0-1-release-with-deferred-exitspec.md).
The later annotated tag and GitHub Release must repeat the producer-side scope
and the deferred, not-recorded ExitSpec state.
