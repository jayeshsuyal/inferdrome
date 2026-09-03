# GCP private pre-campaign v0.2 runbook

This is a review and future-operator runbook for the additive two-engine
pre-campaign controller. It does not authorize a provider action.

## Preconditions

The only supported profile is one exact project/zone `a2-highgpu-2g` instance
with two `NVIDIA A100-SXM4-40GB` GPUs, no external access, one auto-delete boot
disk, exactly two machine-fixed ephemeral local-NVMe scratch disks, and no
user-created persistent data disks. It runs two digest-pinned Qwen3-8B vLLM
engines on GPU 0/port 8000 and GPU 1/port 8001. They are independently
addressable engines on one host, not independent hosts. The selected pinned
vLLM-compatible adapter must expose the private
`/inferdrome/v2/engine-attestation` contract; a plain healthy OpenAI endpoint
is not enough to establish the engine/GPU mapping.

Before any live execution, obtain a separate exact human authorization that
binds the provider, project, region/zone, machine/GPU profile, boot-image
identity, runner/engine image digests, Qwen3-8B model/tokenizer revisions,
startup-payload digest, R1/PR-B input hashes, quote/rate/currency/expiry,
runtime/cleanup horizons, USD cap, caller UUIDs, and evidence-destination
identity. The quote and cap remain controller estimates, never an invoice
guarantee.

## Offline preparation and review

1. Produce canonical startup and proposal artifacts from local pinned inputs.
   `proposal` and `preview` are local-only; they do not initialize Google SDK
   clients, ADC, a socket, or a provider transport.
2. Review `preview` for the proposal and startup identities. It must report
   `a2-highgpu-2g`, two private engines, and a no-provider-call result.
3. Store the reviewed approval as a distinct canonical artifact. Do not place
   credentials, headers, tokens, private keys, prompts, outputs, or raw
   provider payloads in any input artifact or command argument.
4. Create owner-only local journal and evidence-root directories. The evidence
   root's opaque identity must match the reviewed proposal.

## Future approved execution sequence

Only after the separate authorization may an operator use the guarded
`execute` command. The command validates the exact approval locally, then
durably writes `BACKSTOP_READY` and `CREATE_INTENT` before it initializes the
optional Google adapter. It validates the same approval and quote expiration a
second time immediately before create. The provider request enables maximum
runtime with `DELETE`, boot-disk auto-delete, a private-only NIC, immutable
labels, and the two semantic engine declarations.

The controller reads back the exact VM profile, boot-image and boot-disk
provenance, private-only NIC, startup digest, and hashes the provider instance
identity for later exact cleanup binding. It then makes direct, no-redirect,
bounded probes of both private endpoints for health, a minimally valid
generation completion, model API availability, and the one target
`vllm:num_requests_running{model_name="Qwen/Qwen3-8B"}` gauge. It only then
materializes the existing PR-B `GCP_PRIVATE` runner configuration. PR-B remains
responsible for independent health/load telemetry, age/epoch/freshness records,
the controlled stale-load/fresh-health fault, routing decisions, and terminal
closure.

The controller also requires an exact private engine attestation for each port:
endpoint ID, container name, pinned image, model/runtime, GPU ordinal, private
port, and startup digest must match the approved semantic startup payload. A
missing or mismatched adapter capability blocks campaign handoff. The cleanup
horizon starts when `CREATE_INTENT` is durably recorded; after it expires the
controller records a deadline/unconfirmed state and does not issue a new
create, campaign handoff, or destructive cleanup request.

The runner must be on a reviewed private network path to those internal
addresses. It creates a sealed PR-B evidence package once and retains only its
verification digest in the controller receipt. No public inference endpoint,
SSH control plane, dashboard surface, Kubernetes component, or generic cloud
platform is introduced here.

## Cleanup and recovery

The controller writes cleanup intent before deletion, uses the original caller
delete UUIDs, and re-reads only the exact instance and attached boot-disk
identities. The re-read instance and boot disk must match the journaled hashes
of the originally observed provider identities before any deletion is
attempted. It direct-checks the approved instance and boot-disk names before
it accepts the complete label-scoped residual inventory as absent. The two
machine-fixed scratch disks are expected only while the instance exists; they
are not separate persistent cleanup targets. A failure, partial inventory,
lost journal, ambiguous provider response, or mismatched label remains
`CLEANUP_UNCONFIRMED`.

If launch approval expires after a create boundary, use only the separately
bound `recover-cleanup` authorization. That path cannot create, modify the
topology, replace a resource, or use broad deletion. `discover-orphans` is
read-only and label-scoped; it is a diagnostic, not deletion authority.
