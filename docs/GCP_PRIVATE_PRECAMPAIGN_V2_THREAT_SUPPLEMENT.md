# GCP private pre-campaign v0.2 threat supplement

This is an additive threat supplement for the bounded same-host
`a2-highgpu-2g` pre-campaign controller. It preserves the frozen v0.1 threat
model and the existing v0.2 guarded-lifecycle documents. Its purpose is to
state what must fail closed before a future, separately authorized two-endpoint
Qwen3-8B campaign can reach GCP.

## Assets and trust boundaries

The assets are the exact approved topology, image/model/runtime identities,
startup payload, routing-input hashes, evidence destination identity, resource
ownership identity, cleanup state, and sealed evidence-package identity.
Human approval is a local authorization input, not a signature, provider
attestation, or credential. Provider responses are observations that must be
bound to the original exact identity; they are not trusted merely because they
contain a familiar instance name or address.

The controller trusts neither a hostname/IP-derived ownership guess nor an
account-wide before/after diff. It uses caller-supplied idempotent request IDs,
immutable labels, exact project/zone/resource references, bounded operation
reconciliation, and a hash-chained local journal. Records contain bounded
identifiers, hashes, state codes, and redacted capability facts only.

## Required fail-closed properties

| Threat or fault | Required boundary |
| --- | --- |
| Approval substitution, expiry, duplicate keys, or malformed JSON | Exact local approval validation occurs before the lazy provider factory and immediately before create; the factory must remain uncalled for invalid input. Expiry is half-open: the expiry instant is not valid. |
| Topology/image/model/runtime drift | The proposal and observed readiness both require the exact two-A100, two-engine profile and digest/revision bindings. |
| Public or ambiguous endpoint | Only two distinct private origins with unique identities, health, generation, and target-specific metrics capabilities may reach runner admission. |
| Stale/missing telemetry | The established PR-B runner independently samples health/load and keeps age, epoch, freshness, and admissibility checks; this controller cannot override them. |
| Two ports secretly sharing one GPU/container | Each private engine must return a bounded attestation that exactly binds endpoint/container/image/model/runtime/GPU/port/startup identity. Missing, duplicate, or mismatched facts block admission. |
| Controller crash around create | A durable max-runtime/`DELETE` backstop and hash-chained `BACKSTOP_READY`/`CREATE_INTENT` records precede transport construction; recovery is exact-identity cleanup only. |
| Cleanup horizon expiry | The horizon starts at durable create intent. Once expired, the controller refuses a new create, handoff, or destructive cleanup action and records an unconfirmed deadline state; provider max-runtime `DELETE` remains separate. |
| Lost/replayed provider response | Caller request IDs, exact operation reconciliation, and durable journal records distinguish submitted, observed, and unconfirmed states. |
| Same-name resource replacement | The first trusted readback binds hashes of the exact provider instance and boot-disk identifiers; each cleanup/reconciliation readback must match them before deletion. Final absence directly reads the approved resource names before label-scoped inventory. |
| Boot-image, network, or disk provenance drift | Readback independently verifies the approved image identity, private-only NIC, boot-disk source/attachment, labels, fixed A2 GPU facts, and exactly two machine-fixed ephemeral local-NVMe scratch disks; invented or partial facts are rejected. |
| Residual boot/persistent disk | Exact owned disks are independently inventoried and deleted/reconciled; the A2 machine-fixed scratch disks are nonpersistent and expected to disappear with the instance. Incomplete inventory or absence uncertainty remains unconfirmed. |
| Broad destructive cleanup | Label-scoped discovery is read-only; deletion requires a cleanup authorization and exact project/zone/resource/ownership match. |
| Startup-payload replacement | The payload digest and its two-engine GPU/endpoint semantics are verified before create and again at readiness/campaign admission. |
| Redirect, proxy, or shallow endpoint readiness | Health, generation, and target-gauge probes are direct/no-redirect bounded requests. A generation success needs a minimally valid non-empty completion choice. |
| Secret or raw-result disclosure | Proposals, journal, receipts, manifests, errors, and evidence retain no credentials, auth headers, private keys, raw prompts, model outputs, raw provider payloads, or public addresses. |
| Evidence-root replacement race | Evidence root preflight binds and retains a safe local root before the lazy factory. Handoff and receipt retrieval use duplicates of that held descriptor; the routing package reserves, writes, and publishes relative to it, then rejects a visible-path identity replacement rather than returning a receipt. |

## Crash and recovery behavior

The controller deliberately prefers an unresolved state to a broad or
invented cleanup action. If it crashes before create intent, no provider
transport should exist. If it crashes after create intent or after a provider
operation becomes ambiguous, later recovery may query only the original exact
ownership scope. If the journal is missing, tampered, incomplete beyond the
repairable tail, or cannot prove the resource/disk identity, normal campaign
execution is blocked. A read-only orphan report may still identify the bounded
label scope; it is not permission to delete a guessed resource.

Cleanup-only authorization is intentionally separate from launch authorization:
launch approval can expire, while a recovery operator may still remove only the
original exact owned resources. Recovery cannot launch, provision a substitute,
change an image/model/topology, or turn incomplete discovery into confirmed
absence.

## Explicit limitations

The profile has two GPUs but one host, so it provides no independent host
failure signal. Provider maximum runtime and `DELETE` are best-effort cleanup
backstops, not guarantees of provider cleanup or billing cessation. Quote and
USD-cap calculations are local controller estimates, not invoice guarantees.
Local fakes prove ordering and state-machine behavior, not Google API behavior,
GCE capacity, runtime image availability, VLLM performance, network isolation,
or a successful GPU campaign.

The engine-attestation contract proves only what the pinned adapter reports at
its private endpoint. It is an admission precondition, not a stock-vLLM,
hardware, or provider attestation guarantee.

The current PR performs no cloud action at all: no credential/ADC lookup,
provider API call, SSH, GPU use, registry or bucket access, or spending. Any
real execution remains gated by a separate explicit authorization and is out of
scope for this code review.
