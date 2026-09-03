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
| Approval substitution, expiry, duplicate keys, or malformed JSON | Exact local approval validation occurs before the lazy provider factory. Approval, quote, and normal execution deadline are rechecked after each potentially slow pre-insert read and immediately before GCE insert; the factory/create path stays unreachable for invalid authority. Expiry is half-open. |
| Unbounded controller intent | `CREATE_INTENT_DURABLE` is only a fsynced local intent and recovery record, never a watchdog or provider backstop. Only observed Compute `maxRunDuration` plus `DELETE`, read back after create, is the provider backstop. |
| Topology/image/model/runtime drift | The proposal, startup script, and observed readiness require the exact two-A100, two-engine profile, source-owned adapter digest, one preloaded offline Qwen3 snapshot/manifest, and pinned OCI image identities. Startup uses `--pull never`; it cannot invent a registry or Hugging Face acquisition path. |
| Runner provenance, privilege, or evidence-output mismatch | GCP_PRIVATE accepts only the separately pinned CPU-only runner's bounded IAP mode. The runner attestation and command bind its image, source digest, private Docker network, no-GPU/no-cloud-credential/no-Docker-socket/no-provider-mutation role, and the proposal. The controller never starts Docker locally. Sealed evidence is fetched only as a bounded archive and independently reverified under the held controller evidence root. |
| Public, direct, or ambiguous endpoint | Only two distinct private origins with unique identities, health, generation, and target-specific metrics capabilities may reach runner admission. The controller opens exactly three approval-bound supervised IAP TCP tunnels after exact instance readback: two engine readiness paths and one runner control/retrieval path. It never directly calls RFC1918 addresses, opens SSH, or exposes public inference. |
| IAP/guest-control-plane drift | The approved controller principal, IAP firewall identity/CIDR/tag/three-port policy, non-default zero-scope guest service account, blocked inherited project SSH keys, disabled OS Login, and private NIC must all be read back. Any mismatch blocks admission. |
| Stale/missing telemetry | The runner first performs a co-located health/load admission over its fixed internal Docker-network origins and fails closed unless every observation is at most 5 ms old when the decision is made. PR-B independently repeats its health/load age, epoch, freshness, and admissibility checks per request. Neither condition is an IAP-latency promise. |
| Two ports secretly sharing one GPU/container | Each private engine must return a bounded attestation that exactly binds endpoint/container/image/model/runtime/GPU/port/startup/adapter/model-snapshot identity. Missing, duplicate, or mismatched facts block admission. |
| Delayed or shallow readiness | Bounded polling/backoff through the IAP tunnels probes health, model, generation, target gauge, and adapter attestation. A generation success needs a minimally valid non-empty completion choice. |
| Lost/replayed provider response | Caller request IDs, exact operation reconciliation, immutable labels, an approval-scoped unique name/run lease, and durable journal records distinguish submitted, observed, and unconfirmed states. |
| Same-name resource replacement | GCE deletion is name-addressed and has no immutable-ID conditional delete. The controller re-reads approved name/labels/run lease and exact provider IDs immediately before deletion; a detected replacement is never deleted. The final reread cannot eliminate the remaining provider TOCTOU window, so ambiguity is unconfirmed rather than a false cleanup guarantee. |
| Boot-image, network, or disk provenance drift | Readback derives boot-image identity from the provider reference plus numeric image ID, and verifies private NIC, boot-disk source/attachment, labels, fixed A2 GPU facts, and exactly two machine-fixed ephemeral local-NVMe scratch disks. Invented or partial facts are rejected. |
| Residual boot/persistent disk | Exact owned disks are independently inventoried and deleted/reconciled using the same name/identity safeguards; the A2 machine-fixed scratch disks are nonpersistent. Incomplete inventory or absence uncertainty remains unconfirmed. |
| Broad destructive cleanup or normal-deadline expiry | Label-scoped discovery is read-only. Cleanup requires cleanup-only authorization and exact project/zone/resource/ownership binding, uses a no-create transport, and remains permitted after the normal execution deadline. |
| Startup-payload or controller-input replacement | The payload digest, two-engine GPU slots, and CPU-only runner command are checked before create and at admission. The raw workload travels only in a strict bounded canonical IAP envelope to the runner; no controller-host staging directory, raw-input Docker bind source, or arbitrary endpoint map exists. |
| Secret or raw-result disclosure | Proposals, journal, receipts, manifests, errors, and evidence retain no credentials, auth headers, private keys, raw prompts, model outputs, raw provider payloads, or public addresses. |
| Evidence-root replacement, archive tamper, or post-seal crash | The runner seals a fixed immutable package inventory with create-no-replace semantics. The controller accepts only a bounded IAP archive with no redirect, duplicate member, link, unsafe mode, oversized member, or malformed manifest, then re-verifies the package under its held evidence root. Redacted readiness and sealed-artifact receipts are durably published and journaled; recovery reopens and verifies artifacts rather than using in-memory state or rerunning a sealed workload. |

## Crash and recovery behavior

The controller deliberately prefers an unresolved state to a broad or invented
cleanup action. If it crashes before `CREATE_INTENT_DURABLE`, no provider
transport should exist. If it crashes after local intent but before provider
readback, the local intent itself does not prove that a provider backstop was
armed; later recovery may only use the original exact ownership scope and must
not guess a deletion target. After a successful create readback, the observed
provider `maxRunDuration` plus `DELETE` is the distinct provider backstop.

If the journal is missing, tampered, incomplete beyond the repairable tail, or
cannot prove resource/disk identity, normal campaign execution is blocked. A
read-only orphan report may still identify the bounded label scope; it is not
permission to delete a guessed resource. Where a redacted readiness or sealed
artifact receipt was recorded, recovery reopens and verifies it from the held
evidence location; it does not trust a process-local `_sealed` value.

Cleanup-only authorization is intentionally separate from launch authorization:
launch approval and the normal execution deadline can expire, while a recovery
operator may still remove only the original exact owned resources. Recovery uses
a no-create transport; it cannot launch, provision a substitute, change an
image/model/topology, or turn incomplete discovery into confirmed absence.

## Explicit limitations

The profile has two GPUs but one host, so it provides no independent host,
zone, network, or control-plane failure signal. Provider maximum runtime and
`DELETE` are the only observed provider cleanup backstop in this design, not a
guarantee of provider cleanup or billing cessation. Quote and USD-cap
calculations are local controller estimates, not invoice guarantees. GCE
name-addressed deletion retains a provider TOCTOU boundary despite the final
identity reread.

Local fakes prove ordering and state-machine behavior, not Google API behavior,
GCE capacity, IAP authorization, runtime image/model availability, vLLM
performance, network isolation, telemetry freshness, or a successful GPU
campaign. The 5 ms condition is checked only when a future runner observes it
co-located with the engines; it is not a general feasibility or IAP-latency
claim.

The engine-attestation contract proves only what the source-owned adapter in a
separately prepared pinned image reports at its private endpoint. It is an
admission precondition, not a stock-vLLM, hardware, or provider attestation
guarantee. This PR ships adapter source but does not build, publish, or prove
availability of the required boot image or OCI artifact.

The current PR performs no cloud action at all: no credential/ADC lookup,
provider API call, SSH, GPU use, registry or bucket access, or spending. Any
real execution remains gated by a separate explicit authorization and is out of
scope for this code review.
