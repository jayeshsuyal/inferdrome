# GCP execution supervisor v0.2

This is an additive v0.2 development-cycle contract. It does not change the
frozen v0.1 plan, request, lease, result, public schema, receipt, or evidence
meanings. It also does not enable Compute Engine execution.

## Approval before any future provider client

`GcpExecutionApproval` is a strict, self-authenticating local artifact. It
binds one operator identity and exact confirmation to the provider, project,
region, zone, fixed single-A100 profile, boot image identity/digest, runner
image digest, serving-runtime image digest, plan ID/hash, request digest,
v1 quote/capacity digests, v2 rate-basis/cost-guard digests, USD currency,
maximum runtime, controller deadline, independently durable watchdog cleanup
deadline, exact fixed-point hard USD ceiling, maximum estimate, issue time,
and expiry. It rejects substitutions, stale approvals, malformed JSON, unknown
fields, duplicate keys, and expiry beyond the one-shot arm. The rate and
cost/cleanup details are defined in
[`GCP_COST_CLEANUP_GUARDS_V2.md`](GCP_COST_CLEANUP_GUARDS_V2.md).

The enforced ordering is:

1. Canonical v1 preflight validates plan, arm, immutable request, quote, and
   read-only capacity input.
2. The existing durable lease journal reserves the exact request.
3. The v0.2 sidecar reserves the exact approval/lease binding.
4. The one-shot arm is consumed and the v1 `ARM_CONSUMED` event is durable.
5. The sidecar records `ARM_CONSUMED`, obtains an independently durable
   watchdog receipt through the quote-bound cleanup/absence-confirmation tail,
   and records `WATCHDOG_READY`.
6. Only then can a future separately reviewed transport/SDK factory be called.

The sidecar repeats exact approval, rate-basis, quote, capacity, cost-guard,
deadline, and kill validation immediately before `CREATE_INTENT`; it repeats
the absolute deadline/kill checks immediately before work. A future lazy
factory or non-fake transport construction also requires this supervisor.
Tests inject only local fakes; no provider, credential, SSH, GPU, billing, or
spend action occurs here.

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
it never resumes normal work or constructs a replacement resource identity. An
`ORPHANED` event carries an exact report for the bound project/region/zone,
instance, matching boot-disk identity, and immutable labels, never a hostname
guess or broad account inventory assertion.

## Watchdog, kill, and cleanup boundary

A watchdog receipt must bind the approval digest, request digest, controller,
the distinct watchdog cleanup deadline, and independently-durable
controller-death/hung-work coverage before create intent is durable. The
watchdog horizon includes the immutable quote's cleanup and exact-absence tail,
not only the provider runtime. The kill switch is checked before the future
factory, before create intent, and before work; failure or uncertainty blocks
the run.

The supervisor delegates discovery and termination to the existing exact-label,
project/region/zone/instance boundaries. It never derives ownership from a
hostname or account-wide before/after differences. `CLEANUP_CONFIRMED` requires
the existing authoritative exact instance-and-boot-disk absence observations;
all other cleanup outcomes remain explicitly unresolved or orphaned.

Maximum-runtime/delete configuration is a cleanup backstop, not an invoice
proof. A quote and hard ceiling are local controller guardrails, not provider or
account billing enforcement. Approval artifacts, journals, fakes, and errors
contain bounded identifiers/digests rather than credentials or secret values.
The separate v0.2 threat analysis is
[`THREAT_MODEL_V2_GCP_SUPERVISOR.md`](THREAT_MODEL_V2_GCP_SUPERVISOR.md).

## Deliberate non-activation posture

The frozen v1 arm intentionally makes its controller deadline equal to its
maximum provider runtime. This v0.2 guard refuses a create unless the full
provider runtime remains before that absolute arm deadline, while its watchdog
also has the separately bound cleanup tail. Consequently any real elapsed
setup time fail-closes this development-cycle path; deterministic local fake
tests use a constant clock solely to exercise state transitions. This is not an
activation-ready execution design. A future, separately reviewed and approved
version would need a new arm/deadline contract with explicit setup margin and
the same-or-stronger cleanup watchdog coverage; it must not weaken these
checks.
