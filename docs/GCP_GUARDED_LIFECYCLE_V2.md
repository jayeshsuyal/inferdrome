# GCP guarded lifecycle v0.2 supplement

This additive v0.2 development-cycle note describes the local provider-boundary
hardening around the existing v0.1 guarded lifecycle. It does not amend the
frozen v0.1 plan, request, lease, result, evidence, or public-schema meanings.

## Current activation state

Live Compute Engine execution is disabled. The transport can build exact
`InsertInstanceRequest` and `DeleteInstanceRequest` objects for local
plan/check tests, but its mutation methods return `LIVE_MUTATION_DISABLED`.
The default transport factory also rejects before SDK import or ADC client
construction. Tests use injected local fakes only.

Any later activation path must validate its separate approval artifact before
it can construct an SDK/ADC client. It must retain the existing ordering:
canonical preflight, durable lease reservation, one-shot arm consumption and
`ARM_CONSUMED` journal event, then transport activation. This repository does
not provide that later activation path. The additive local approval, watchdog,
and recovery state contract is documented in
[`GCP_EXECUTION_SUPERVISOR_V2.md`](GCP_EXECUTION_SUPERVISOR_V2.md).
Its separate read-only pricing, cleanup-cap, and exact local kill-marker
contracts are documented in
[`GCP_COST_CLEANUP_GUARDS_V2.md`](GCP_COST_CLEANUP_GUARDS_V2.md).

## Exact ownership and reconciliation

The controller uses caller-generated, stable insert/delete request IDs and
matches project, region, zone, instance name, and immutable ownership labels.
It has distinct create projection, instance read, label-scoped owned-inventory,
operation reconciliation, and terminate projection boundaries.

Owned-resource discovery is bounded to 256 exact-label results and must prove
pagination completion. An incomplete or invalid inventory blocks creation and
prevents cleanup confirmation. The controller never infers ownership from a
hostname or from account-wide resource differences.

After a future create succeeds, the provider observation must confirm boot-disk
auto-delete, deletion protection off, automatic restart off, `TERMINATE`
maintenance behavior, exact maximum runtime, and `DELETE` termination action.
Final cleanup requires both exact instance absence and exact named boot-disk
absence. A residual disk, ambiguous operation, incomplete inventory, or failed
read remains explicitly unconfirmed/orphaned.

## Boundary of the claims

The Compute Engine maximum-runtime/delete settings are cleanup backstops, not
invoice proof. A controller quote or hard cap is not provider/account billing
enforcement. No GCP API, credential, SSH, GPU, billing, or artifact action is
performed by this implementation or its tests.
