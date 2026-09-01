# GCP execution supervisor v0.2

This is an additive v0.2 development-cycle contract. It does not change the
frozen v0.1 plan, request, lease, result, public schema, receipt, or evidence
meanings. It also does not enable Compute Engine execution.

## Approval before any future provider client

`GcpExecutionApproval` is a strict, content-addressed local binding artifact.
It binds asserted operator identity and exact confirmation to the provider,
project, region, zone, fixed single-A100 profile, boot image identity/digest,
runner image digest, serving-runtime image digest, plan ID/hash, request
digest, v1 quote/capacity digests, v2 rate-basis/cost-guard digests, USD
currency, a frozen v1 controller-deadline observation, v2 authorization expiry,
explicit setup deadline/margin, provider runtime, independently durable
watchdog-cleanup deadline, exact fixed-point hard USD ceiling, maximum estimate,
issue time, and expiry. Its digest is not a signature or proof that Jayesh (or any
other person) authorized the action; it only detects a change to the canonical
local payload. It rejects substitutions, stale approvals, malformed JSON,
unknown fields, and duplicate keys. The v2 authorization and setup horizons are
explicitly versioned; they are not inferred from or required to fit within the
frozen v1 arm deadline. The rate and cost/cleanup details are defined in
[`GCP_COST_CLEANUP_GUARDS_V2.md`](GCP_COST_CLEANUP_GUARDS_V2.md).

The additive `GcpV2StartupProjection` separately binds the frozen request and
environment digests, the compatible `startup_script_digest`, and a
domain-separated `execution_payload_digest` over opaque bytes. The startup
script digest remains a distinct compatibility fact and is never treated as the
full payload. This contract records no payload bytes and specifies no command,
shell, transfer, credential, evidence, or execution behavior; a future producer
must only prove exact digest equality before it reaches the provider boundary.

The enforced ordering is:

1. Canonical v1 preflight validates plan, arm, immutable request, quote, and
   read-only capacity input.
2. The existing durable lease journal reserves the exact request.
3. The v0.2 sidecar reserves the exact approval/lease binding.
4. The one-shot arm is consumed and the v1 `ARM_CONSUMED` event is durable.
5. The sidecar records `ARM_CONSUMED`, obtains an independently durable
   file-watchdog receipt through the bound cleanup/absence-confirmation tail,
   and records `WATCHDOG_READY`.
6. At the v2 setup edge, one exact capability is issued, the watchdog records
   `ACTIVE` and returns an activation receipt, and the controller consumes both
   into a one-shot sealed factory proof.
7. Only that proof can reach the capability-bound future transport factory;
   the production/default factory remains disabled in this development cycle.

The sidecar repeats exact approval, rate-basis, quote, capacity, cost-guard,
setup/authorization deadline, and kill validation immediately before
`CREATE_INTENT`. After activation, work is bounded by the capability's provider
runtime and watchdog-cleanup horizon; the frozen v1 arm timestamp remains an
audit binding, not a second runtime clock. A future lazy factory or non-fake
transport construction also requires this supervisor.
Tests inject only local fakes; no provider, credential, SSH, GPU, billing, or
spend action occurs here.

## Durable supervisor state

The sidecar journal is separate from the v1 lease authority so it cannot alter
frozen v1 records. It uses no-follow file opens, an exclusive lock, fsync after
each append, a hash-linked canonical JSONL chain, and fixed bounds of 64 events
and 256 KiB per exact controller identity. It repairs only an incomplete final
write, rejects malformed/tampered/retargeted entries, and blocks every future
reservation while any sidecar state is unresolved. Pre-mutation crashes and a
core `CLEANUP_CONFIRMED`/sidecar mismatch have explicit local reconciliation
paths, so a durable partial prefix never becomes a permanent blocker.

Its deterministic states are `PREPARED`, `ARM_CONSUMED`, `WATCHDOG_READY`,
`CREATE_INTENT`, `WORKING`, `CLEANUP_PENDING`, `CLEANUP_CONFIRMED`,
`ORPHANED`, `KILLED`, and `BLOCKED`. A restart may resume exact cleanup only;
it never resumes normal work or constructs a replacement resource identity. An
`ORPHANED` event carries an exact report for the bound project/region/zone,
instance, and immutable labels, never a hostname guess or broad account
inventory assertion. An exact boot-disk identity appears only in the separate
provider-observed v2 disk-cleanup binding.

## Watchdog, kill, and cleanup boundary

A watchdog receipt must bind the approval digest, request digest, controller,
the distinct watchdog cleanup deadline, and independently-durable
controller-death/hung-work coverage before create intent is durable. The
concrete file watchdog persists a complete exact disk-cleanup binding before
any cleanup intent, bounds a separate worker process group, retains the full
authoritative instance-inventory and named-disk absence result, and resumes a
timed-out intent only after its bounded lease expires. The watchdog horizon
includes the v2 cleanup tail after the provider runtime. The kill switch is
checked before the future factory, before create intent, and before work;
failure or uncertainty blocks the run.

The supervisor delegates discovery and termination to exact-label,
project/region/zone/instance boundaries. The v2 disk sidecar additionally
requires a complete label-scoped inventory, observed provider disk ID/self link,
stable named-delete request identity, operation reconciliation, and named-disk
absence confirmation. It never derives ownership from a hostname or account-wide
before/after differences. All other cleanup outcomes remain explicitly
unresolved or orphaned.

Maximum-runtime/delete configuration is a cleanup backstop, not an invoice
proof. A quote and hard ceiling are local controller guardrails, not provider or
account billing enforcement. Approval artifacts, journals, fakes, and errors
contain bounded identifiers/digests rather than credentials or secret values.
The separate v0.2 threat analysis is
[`THREAT_MODEL_V2_GCP_SUPERVISOR.md`](THREAT_MODEL_V2_GCP_SUPERVISOR.md).

## Deliberate non-live posture

The v2 contract is activation-capable only as a local, injected-fake boundary:
its explicit setup margin gates activation, provider runtime starts at that
activation edge, and a distinct watchdog-cleanup horizon is durably covered
before a future create could be enabled. The frozen v1 arm remains unchanged and
is retained as canonical audit evidence; it does not require the entire future
provider runtime to remain at activation. `GCP_LIVE_MUTATIONS_ENABLED` remains
`False`, so no SDK/ADC initialization, provider mutation, or default execution
route is enabled by this work.
