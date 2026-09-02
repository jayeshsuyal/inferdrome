# GCP guarded lifecycle v0.2 supplement

This additive v0.2 development-cycle note describes the local provider-boundary
hardening around the existing v0.1 guarded lifecycle. It does not amend the
frozen v0.1 plan, request, lease, result, evidence, or public-schema meanings.

## Current activation state

The generic/default Compute Engine factory is permanently fail-closed. It can
build exact `InsertInstanceRequest` and `DeleteInstanceRequest` objects for
local plan/check tests, but its mutation methods return `LIVE_MUTATION_DISABLED`
and it rejects before SDK import or ADC client construction.

Separately, v0.2 provides one private sealed route for an already-consumed
supervisor-issued activation guard. That route accepts only an injected lazy
component supplier, revalidates the exact request and opaque startup-projection/request/
execution-payload identifiers immediately before each bounded SDK call, and
projects only those identifiers as GCE metadata. It has no global enable
switch, no generic-client bypass, and no default credential discovery. This
task exercises it solely with injected local fake SDK clients; it performs no
provider action.

Any future operator use of that sealed route must validate its separate
approval artifact before it can construct an SDK/ADC client. It must retain the
existing ordering: canonical preflight, durable lease reservation, one-shot arm
consumption and `ARM_CONSUMED` journal event, then transport activation. This
repository provides no unchecked later activation path. The additive approval, watchdog,
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

For the one bounded canary boundary, provider `maxRunDuration` plus `DELETE`
is a cleanup backstop while the controller reconciles exact instance and disk
identities. It is not proof that a provider cleanup occurred, invoice
enforcement, or a universal controller-crash cleanup guarantee.

If a process crashes after successful insert but before an exact boot-disk
binding is fsync-recorded, v0.2 makes the narrow, deliberate claim only of a
durable orphan report. It never derives a disk name, performs a broad scan, or
claims exact disk absence, disk-cleanup binding, or disk-cleanup intent for
that window. A later exact recovery is permitted only after a separately
observed, complete, label-scoped inventory has been durably bound.

## Boundary of the claims

The Compute Engine maximum-runtime/delete settings are cleanup backstops, not
invoice proof. A controller quote or hard cap is not provider/account billing
enforcement. No GCP API, credential, SSH, GPU, billing, or artifact action is
performed by this implementation or its tests.
