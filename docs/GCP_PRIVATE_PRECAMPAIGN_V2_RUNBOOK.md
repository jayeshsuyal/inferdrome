# GCP private pre-campaign v0.2 runbook

This is a review and future-operator runbook for the additive two-engine
pre-campaign controller. It does not authorize a provider action.

## Preconditions

The only supported profile is one exact project/zone `a2-highgpu-2g` instance
with two `NVIDIA A100-SXM4-40GB` GPUs, no external IP or public inference
listener, one auto-delete boot disk, exactly two machine-fixed ephemeral
local-NVMe scratch disks, and no user-created persistent data disks. It runs
two digest-pinned Qwen3-8B vLLM-compatible engines on GPU 0/port 8000 and GPU
1/port 8001. They are independently addressable engines on one host, not
independent hosts.

The approved boot image must already contain distinct, separately digest-pinned
runner and serving OCI images and the revision-pinned Qwen3-8B snapshot at
`/opt/inferdrome/qwen3-8b`, matching the reviewed manifest and snapshot digests.
Startup uses offline loading and `--pull never`; it may not fetch an image or
model from a registry or Hugging Face. The repository supplies the source-owned
engine adapter that exposes the private
`/inferdrome/v2/engine-attestation` capability. Docker overrides the image
entrypoint to the adapter's pinned Python executable and passes the adapter
module as CMD; only the adapter invokes `vllm serve`, exactly once. A stock
healthy OpenAI endpoint or an image without this adapter is not enough to
establish the engine/GPU mapping. This PR neither publishes the image nor proves
that a future boot image/artifact is available.

Before any live execution, obtain a separate exact human authorization that
binds the provider, project, region/zone, machine/GPU profile, provider-native
boot-image reference and numeric identity, runner/engine image digests,
adapter/model snapshot/manifest identities, Qwen3-8B model/tokenizer revisions,
startup-payload digest, R1/PR-B input hashes, IAP controller principal/firewall
identity/tag, quote/rate/currency/expiry, runtime/cleanup horizons, USD cap,
caller UUIDs, ownership labels/run lease, and evidence-destination identity.
The quote and cap remain controller estimates, never an invoice guarantee.

The controller path is exactly three supervised IAP TCP tunnels: one per private
engine readiness port and one bounded runner control/retrieval port. It never
directly calls the VM's RFC1918 address, never opens SSH, and never exposes
public inference. The approved IAP firewall permits only the IAP CIDR to the
approval-bound tag and ports 8000, 8001, and 8002; the controller principal must
match the approved IAP principal. The VM uses one non-default,
least-privilege guest service account with no OAuth scopes, blocks inherited
project SSH keys, and disables OS Login. Each fact is read back before campaign
admission.

## Offline preparation and review

1. Produce canonical startup and proposal artifacts from local pinned inputs.
   `proposal` and `preview` are local-only; they do not initialize Google SDK
   clients, ADC, a socket, Docker, an IAP process, or a provider transport.
2. Review `preview` for the exact A2 profile, two private engines, preloaded
   artifact identities, IAP requirements, and no-provider-call result.
3. Store the reviewed approval as a distinct canonical artifact. Do not place
   credentials, headers, tokens, private keys, prompts, outputs, or raw
   provider payloads in any input artifact or command argument.
4. Create owner-only journal and an exact-mode-`0700` controller evidence-root
   directory. The evidence root's opaque identity must match the reviewed
   proposal. The raw fixed workload is sent only in one bounded canonical IAP
   envelope to the VM runner; it may not appear in command arguments, journals,
   or evidence content. The controller never starts Docker locally. The runner
   is a separate CPU-only container with no GPU, cloud credential, Docker
   socket, serving role, or provider mutation authority.

## Future approved execution sequence

Only after the separate authorization may an operator use the guarded
`execute` command. The command validates approval locally and writes
`CREATE_INTENT_DURABLE` followed by `CREATE_INTENT`. `CREATE_INTENT_DURABLE` is
only fsynced local intent, not an armed watchdog or provider backstop. It then
constructs the optional Google adapter, preflights provider boot-image/IAP
principal/firewall observations, and revalidates
approval, quote expiry, and execution deadline after each potentially slow read
and immediately before GCE insert.

The provider request uses the approved unique name, immutable labels, exclusive
run lease, caller request IDs, private NIC, boot-disk auto-delete, exact guest
service account/metadata, and two semantic engine declarations. Only an
observed Compute `maxRunDuration` plus `DELETE`, read back after creation, is
the provider-side cleanup backstop. It is an estimate/cleanup control, not an
invoice guarantee.

The controller reads back the exact VM profile and derives boot-image identity
from the provider reference plus numeric image ID. It verifies boot-disk
behavior, service account/scopes, SSH/OS Login metadata, private network facts,
IAP tag, and startup digest before opening the three IAP tunnels. It polls with
bounded backoff under the startup deadline for engine health, a minimally valid
generation completion, model API availability, the exact target
`vllm:num_requests_running{model_name="Qwen/Qwen3-8B"}` gauge, and the adapter
attestation. The attestation must bind endpoint, container, image, model,
runtime, GPU ordinal, port, startup digest, adapter digest, and model snapshot
digest. It also verifies the CPU-only runner attestation, including its separate
image, command digest, and no-GPU/no-credential/no-Docker-socket role. Missing
or mismatched facts block handoff.

The separately launched, pinned CPU-only runner—not the host controller—receives
the already-bound configuration and fixed workload as one bounded canonical IAP
envelope and executes the existing PR-B campaign command over only its two fixed
same-host Docker-network engine origins. Before it makes any request, it samples
health and load locally and admits execution only when every observation is at
most 5 ms old at decision time. This is an observed, fail-closed admission check,
not a promised pass or IAP-latency claim. PR-B remains responsible for its own
per-request health/load telemetry, age/epoch/freshness/admissibility, controlled
stale-load/fresh-health fault, routing decisions, and terminal closure.

The runner seals canonical request-level evidence and immutable package manifest
under its VM-local evidence directory. The controller retrieves only the fixed
artifact inventory through the third IAP tunnel, rejects redirects, oversized or
unsafe archives, re-verifies the package under its held evidence root, and then
publishes the redacted receipts. A runner crash after sealing is recovered by
re-verifying the existing package, never by silently re-running the workload.

The controller durably publishes redacted readiness and sealed-artifact receipts
with create-no-replace semantics under the held evidence root, journals their
digests, and recovery reopens and verifies them rather than trusting in-memory
state. No public inference
endpoint, SSH control plane, dashboard surface, Kubernetes component, or
generic cloud platform is introduced here.

## Cleanup and recovery

The controller writes cleanup intent before deletion, uses the original caller
delete UUIDs, and re-reads the approved name, labels, run lease, and exact
instance/boot-disk provider identities. GCE deletion is name-addressed and has
no immutable-ID conditional delete. The reread therefore detects a replacement
and refuses deletion, but cannot erase the provider TOCTOU window between that
reread and the provider delete. This limitation remains explicit and produces
an unconfirmed outcome rather than an ownership claim.

It direct-checks the approved instance and boot-disk names before accepting a
complete label-scoped residual inventory as absent. The two machine-fixed
scratch disks are expected only while the instance exists; they are not separate
persistent cleanup targets. A failure, partial inventory, lost journal,
ambiguous provider response, or mismatch remains `CLEANUP_UNCONFIRMED`.

If launch approval or the normal execution deadline expires after a create
boundary, use only the separately bound `recover-cleanup` authorization.
Cleanup/recovery deliberately remains allowed after the normal execution
deadline and uses a no-create transport surface. It cannot create, modify the
topology, replace a resource, or use broad deletion. `discover-orphans` is
read-only and label-scoped; it is a diagnostic, not deletion authority.

## 60-second review sequence

1. Verify the local, content-addressed proposal and human approval.
2. Confirm the approved preloaded images/model, IAP rule/principal, guest account,
   bounded runner-envelope contract, and evidence root.
3. Create exactly one two-A100 VM and read back provider topology/backstop facts.
4. Open three supervised IAP tunnels and poll both adapter-attested engines and
   the CPU-only runner.
5. Run the existing campaign through the co-located runner; admit only observed
   <=5 ms freshness and retrieve/re-verify its sealed evidence.
6. Re-read exact owned resources, attempt bounded cleanup, and record confirmed
   absence or an explicit unconfirmed limitation.

The controller avoids raw-input and Docker binds altogether. It accepts evidence
only through the bounded runner archive and re-verifies it locally; this does not
make the runner a provider or hardware attestor.
