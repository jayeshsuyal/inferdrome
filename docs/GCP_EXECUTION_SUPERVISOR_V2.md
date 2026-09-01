# GCP execution supervisor v0.2

This is an additive v0.2 development-cycle contract. It does not change the
frozen v0.1 plan, request, lease, result, public schema, receipt, or evidence
meanings. It also does not enable Compute Engine execution.

## Approval before any future provider client

`GcpExecutionApproval` is a strict, self-authenticating local artifact. It
binds one operator identity and exact confirmation to the provider, project,
region, zone, fixed single-A100 profile, boot image identity/digest, runner
image digest, serving-runtime image digest, plan ID/hash, request digest,
quote-basis digest, maximum runtime, controller deadline, hard USD ceiling,
issue time, and expiry. It rejects substitutions, stale approvals, malformed
JSON, unknown fields, duplicate keys, and expiry beyond the one-shot arm.

The enforced ordering is:

1. Canonical v1 preflight validates plan, arm, immutable request, quote, and
   read-only capacity input.
2. The existing durable lease journal reserves the exact request.
3. The v0.2 sidecar reserves the exact approval/lease binding.
4. The one-shot arm is consumed and the v1 `ARM_CONSUMED` event is durable.
5. The sidecar records `ARM_CONSUMED`, obtains a watchdog receipt, and records
   `WATCHDOG_READY`.
6. Only then can a future separately reviewed transport/SDK factory be called.

The current factory still rejects before SDK import or ADC construction. Tests
inject only local fakes; no provider, credential, SSH, GPU, billing, or spend
action occurs here.

## Durable supervisor state

The sidecar journal is separate from the v1 lease authority so it cannot alter
frozen v1 records. It uses no-follow file opens, an exclusive lock, fsync after
each append, a hash-linked canonical JSONL chain, and fixed bounds of 64 events
and 256 KiB per exact controller identity. It repairs only an incomplete final
write, rejects malformed/tampered/retargeted entries, and blocks every future
reservation while any sidecar state is unresolved.

Its deterministic states are `PREPARED`, `ARM_CONSUMED`, `WATCHDOG_READY`,
`CREATE_INTENT`, `WORKING`, `CLEANUP_PENDING`, `CLEANUP_CONFIRMED`,
`ORPHANED`, `KILLED`, and `BLOCKED`. A restart may resume exact cleanup only;
it never resumes normal work or constructs a replacement resource identity.

## Watchdog, kill, and cleanup boundary

A watchdog receipt must bind the approval digest, request digest, controller,
deadline, and independently-durable controller-death/hung-work coverage before
create intent is durable. The kill switch is checked before the future factory,
before create intent, and before work; failure or uncertainty blocks the run.

The supervisor delegates discovery and termination to the existing exact-label,
project/region/zone/instance boundaries. It never derives ownership from a
hostname or account-wide before/after differences. `CLEANUP_CONFIRMED` requires
the existing authoritative exact instance-and-boot-disk absence observations;
all other cleanup outcomes remain explicitly unresolved or orphaned.

Maximum-runtime/delete configuration is a cleanup backstop, not an invoice
proof. A quote and hard ceiling are local controller guardrails, not provider or
account billing enforcement. Approval artifacts, journals, fakes, and errors
contain bounded identifiers/digests rather than credentials or secret values.
