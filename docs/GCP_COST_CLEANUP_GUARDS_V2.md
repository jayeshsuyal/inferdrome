# GCP cost and cleanup guards v0.2

This additive v0.2 development-cycle contract adds local-only cost, cleanup,
and kill-switch protection around the frozen v0.1 GCP quote and capacity
records. It does not enable Compute Engine execution, change any public v1
schema, or make a provider billing claim.

## Read-only rate and capability inputs

`GcpReadOnlyQuoteBasis` canonically derives one rational micro-USD-per-second
basis for each frozen v1 quote component: compute, GPU, boot disk, and network.
Its numerator is the component's supplied full-period ceiling, its denominator
is the exact `billable_duration_seconds`, and its component ceiling is rounded
up using integer arithmetic. Components are ordered canonically by component
identity before the v2 basis is hashed. The basis binds the source quote digest, exact
plan/controller/request identity, USD currency, issue/expiry window, and the
declared safety margin.

`GcpCostCleanupGuard` then binds that rate basis to the independently supplied
v1 capacity digest, exact request digest, maximum provider runtime, controller
deadline, distinct watchdog cleanup deadline, fixed-point hard USD ceiling,
maximum estimate, bounded cleanup retry count, cleanup timeout, and
exact-label-only orphan-discovery policy. The watchdog deadline is derived from
the exact quoted billable duration so it extends through the quoted cleanup and
absence-confirmation tail. The guard explicitly labels quote and capacity as
read-only inputs, never launch authority.

The contracts reject a substituted rate basis, quote, capacity observation,
deadline, request, cap, or retry budget before a future transport factory can
be reached. They recheck expiring inputs at the final create edge and before
work; a stale or expired input cannot be converted into a create/work action.
Their models have no provider client, SDK, credential, network, or mutation
operation.

## Cost boundary

The fixed-point maximum is a controller estimate derived from an
operator-supplied quote. It is not a provider rate-card attestation, a provider
account cap, a billing API result, or an invoice. The v1 quote deliberately
does not assert SKU, billing minimums, provider-side rounding, or invoice
truth; this v2 representation cannot manufacture those facts. Compute Engine
maximum-run/delete settings remain a cleanup backstop rather than proof of
invoice control.

## Cleanup and local kill control

The guard requires a `block_next_run` orphan policy, at most three total
cleanup attempts, and exact project/region/zone/instance plus immutable-label
scope. It does not discover by hostname or account-wide before/after state.
The existing guarded controller confirms cleanup only after exact instance,
exact label inventory, and exact named boot-disk absence checks.

`FileGcpKillSwitch` is a read-only local boundary. It reads only the exact
`<controller>.kill-v2.json` path using no-follow file descriptors and fixed
size/permission/change checks. A canonical marker must bind the approval and
request digests plus controller identity exactly. A malformed, unsafe, changed,
or mismatched marker fails closed; an absent exact marker is the only clear
state. Markers carry bounded identity and digest values only, never provider
credentials or secrets.

The future activation path remains disabled in this repository. Local tests use
fakes and temporary files only. In particular, the frozen v1 arm has no setup
margin beyond its maximum provider runtime, so this v0.2 layer intentionally
fail-closes after any elapsed setup time; a future reviewed arm version must
provide that margin without reducing watchdog cleanup coverage.
