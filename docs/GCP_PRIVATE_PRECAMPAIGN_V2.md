# GCP private pre-campaign deployment closure v0.2

This additive v0.2 contract closes the deployment path needed to hand the
existing routing-execution bridge two real, private serving endpoints. It
does not change frozen v0.1 artifacts, public contracts, `deployment-v1`,
evidence/review history, or `EXTERNAL_ONLY` archives. It also does not make
Inferdrome a production router, an availability system, or an authority that
issues experiment verdicts.

## The deliberately small topology

The only GCP-private campaign profile in this slice is one ephemeral Compute
Engine VM in one exact project and zone:

| Fact | Required value |
| --- | --- |
| Machine type | `a2-highgpu-2g` |
| Accelerator model and count | two `NVIDIA A100-SXM4-40GB` GPUs |
| Machine-fixed scratch storage | exactly two ephemeral local-NVMe scratch disks; no user-created data disk |
| Serving layout | two independently addressed engine containers on that one VM |
| GPU placement | endpoint A uses GPU 0; endpoint B uses GPU 1 |
| Observer layout | one separately pinned CPU-only runner container on the same internal Docker network |
| Network exposure | private/internal only; no public inference listener |
| Engine interface | private health, OpenAI-compatible generation, target-specific metrics, and a bounded Inferdrome engine-attestation endpoint |

This is a controlled same-host, two-engine benchmark topology. It lets the
campaign exercise independently addressed engines and their admissible
telemetry; it does **not** establish host, zone, network, or control-plane
failure independence. Different machine types, GPU models/counts, endpoint
counts, public origins, duplicate endpoint identities, missing health/metrics
capabilities, or observed topology drift are rejected before campaign
admission.

The runner and serving images are distinct, separately digest-pinned OCI
identities; the contract rejects an equal image digest. The two engines use the
serving image while the runner uses the observer image. The approved boot image
must already contain both exact OCI images and the exact revision-pinned
Qwen3-8B snapshot at `/opt/inferdrome/qwen3-8b`, with the declared model
manifest and snapshot digests. Startup uses `--pull never`, verifies those
bytes, and sets offline model loading; it never fetches from a registry or
Hugging Face at boot.

The repository ships the source-owned private engine adapter. For each engine,
the reviewed Docker shape is
`docker run --entrypoint /opt/inferdrome-runtime/bin/python <image@sha256> -m inferdrome.deployment.gcp_private_engine_adapter …`.
The entrypoint is therefore the adapter Python executable and the CMD begins
with its module—not `vllm serve`. That adapter is the only component that
invokes `vllm serve`, exactly once, and it exposes the bounded
`/inferdrome/v2/engine-attestation` route alongside the allowed private
vLLM-compatible routes. Before spawning or attesting, it reads the installed
`vllm` distribution and fails closed unless the observed value is exactly
`0.26.0`; the attestation emits that observed value rather than a source
literal. This PR does not build, publish, or claim the
availability of the required boot image or OCI artifact; a future campaign must
supply and separately approve those immutable preloaded inputs.

The runner image and startup projection share one numeric execution identity:
the image creates `/home/vllm` and `/workspace` owned by `2000:0`, declares
`HOME=/home/vllm`, and startup runs the CPU-only runner as `2000:0` with a
matching `/home/vllm` tmpfs. This is a local build/startup compatibility
contract, covered by a static test; it is not a claim that the image has been
built or run on a GPU host in this PR.

`--publish` makes ports 8000–8002 reachable on the VM's private interfaces.
The controller verifies one exact IAP ingress rule, but it deliberately does
not enumerate every effective VPC firewall rule. Before a live campaign, the
operator must establish that **no other effective ingress rule** admits those
ports for the approval-bound tag/VM from a non-IAP source. “IAP-only” below
means the controller transport under that explicit VPC-firewall precondition;
it is not an account-wide firewall proof supplied by Inferdrome.

The model and tokenizer revisions, serving runtime/adapter identity, startup
payload, fixed workload/trace/fault/policy inputs, source commit, and evidence
destination identity are content-addressed. The startup payload is an immutable
semantic projection: it specifies exactly the two private engines, their GPU
assignments and ports, and the required endpoint paths. It contains neither
credentials nor prompts, generated text, authentication headers, or public
addresses.

## One-minute explanation

Inferdrome first verifies a specific approved two-GPU VM, preloaded runtime,
and IAP-only controller path. The controller creates it only after the final
approval/quote/deadline check, reads back the provider facts, then polls both
engines through two supervised IAP TCP tunnels and verifies the CPU-only runner
through a third. The runner performs the existing fail-closed PR-B measurement
over its same-host private Docker-network engine origins, seals the producer
package locally, and the controller retrieves and verifies the fixed archive
before exact cleanup. Any ambiguity is an explicit unconfirmed record, never a
success or routing verdict.

## Guarded lifecycle

The pre-campaign command has separate local-only `proposal` and `preview`
modes. They canonicalize and display a redacted, content-addressed proposal;
they do not initialize an SDK, discover ADC, contact a provider, or construct
a transport. The executable path is intentionally separate:

1. Build the exact two-engine proposal from pinned local inputs. It binds the
   provider, project, region/zone, machine and GPU topology, provider boot-image
   reference and numeric identity, image digests, preloaded model manifest and
   snapshot digests, adapter source digest, startup-payload digest, routing
   inputs, endpoint contract, request denominator, evidence-destination
   identity, IAP principal/firewall/tag, quote identity/rate/currency/expiry,
   maximum runtime, cleanup horizon, and USD cap.
2. Verify a distinct exact human-approval artifact locally. A substituted,
   malformed, expired, or mismatched approval stops here. No provider client,
   network operation, or request dispatch is reachable before this check.
3. Persist a hash-chained `CREATE_INTENT_DURABLE` record followed by
   `CREATE_INTENT`. This is only a fsynced local intent and recovery identity;
   it is not an armed watchdog or provider backstop. The normal execution
   deadline may stop create, handoff, or evidence work, but never exact cleanup
   or recovery. The sole provider backstop is observed Compute
   `maxRunDuration` plus `DELETE`, read back after create and journaled as
   `PROVIDER_BACKSTOP_VERIFIED`. It is not invoice enforcement.
4. Construct the lazy provider transport only after local approval validation.
   It resolves the approved controller service account as the effective Compute
   API identity and constructs every Compute client with that explicit
   credential; every IAP tunnel argv explicitly selects the same identity via
   service-account impersonation. It never relies on `gcloud auth list` as an
   authorization signal. Preflight provider boot-image and IAP-firewall facts, and
   revalidate approval, quote, and execution deadline after every potentially
   slow read and immediately before the GCE insert. The runner image and exact
   command are bound in the startup projection; the controller never starts a
   local Docker runner. The
   request uses caller-supplied idempotent request IDs, a unique
   approval-scoped name, immutable ownership labels, and an exclusive run lease.
5. Create only the observed profile with one explicit user-managed guest service
   account with no OAuth scopes; default service accounts are rejected. Readback
   requires that account, the IAP network tag, `block-project-ssh-keys=TRUE`,
   and `enable-oslogin=FALSE`. The VM has no public inference listener. The
   controller principal and IAP firewall rule identity/tag/CIDR/three-port policy
   are separately bound to the approval; neither SSH nor a direct
   controller-to-RFC1918 path is an admitted transport.
6. Read back and derive boot-image identity from provider reference plus numeric
   ID rather than copying an approved digest into observations. Require observed
   topology, boot-disk behavior, two engine identities, and endpoint capability
   facts before handoff. After the VM is `RUNNING`, open exactly three
   supervised, approval-bound IAP TCP tunnels: two engine readiness paths and
   one runner control/retrieval path. Bounded polling/backoff verifies engine
   health, generation, model, target gauge, and engine attestation, plus the
   runner attestation. Each engine attestation binds endpoint, container, image,
   model, runtime, GPU, port, startup digest, adapter digest, and model snapshot
   digest. The runner attestation binds its separate image, exact command,
   adapter digest, CPU-only/no-credential/no-Docker-socket role, and private
   network. A healthy port is not enough.
7. The controller sends one bounded canonical configuration/workload envelope
   only through the loopback IAP runner tunnel. It never gives the runner a GPU,
   cloud credential, Docker socket, serving role, provider mutation authority,
   or controller-host Docker access. The runner resolves only the two topology-
   admitted logical origins to its fixed same-host Docker-network engine origins,
   performs a bounded co-located freshness admission, and fails closed if any
   observation is older than 5 ms when the admission decision is made. This is
   an observed admission condition, not a promised benchmark result or an
   IAP-latency claim. PR-B repeats its own fail-closed freshness checks per
   request.
8. The runner seals the canonical request-level package and immutable manifest
   in its VM-local evidence directory with create-no-replace semantics. The
   controller retrieves only a bounded fixed-inventory tar over IAP, rejects
   redirects, oversize members, links, duplicates, and malformed content, then
   independently verifies that its transfer/config/workload/endpoint identities
   equal the exact admitted handoff before re-sealing it under the held controller evidence
   root. It durably publishes redacted readiness and sealed-artifact receipts;
   recovery reopens and verifies those deterministic artifacts rather than
   trusting process memory.
9. Record cleanup intent and use only cleanup-capable recovery transport after a
   launch approval expires. Before name-addressed GCE deletion, re-read the
   exact name, labels, run lease, and provider IDs. GCE has no immutable-ID
   conditional delete: the reread detects a mismatch and prevents deletion, but
   cannot remove the residual provider TOCTOU window between reread and delete.
   Exact residual disks receive the same treatment. A failed delete, incomplete
   inventory, journal loss, mismatch, or ambiguous operation is unconfirmed,
   never relabeled as clean.

The runnable interface also has a constrained `recover-cleanup` path and a
label-scoped, read-only orphan-discovery path. Cleanup recovery has its own
authorization bound to the original project, zone, instance, disks, labels,
and controller identity, so it remains usable after launch approval expires.
It cannot create a replacement instance, widen discovery, infer ownership from
a hostname/IP, or delete a merely similar resource.

## Local/fake validation boundary

This PR validates the lifecycle only against injected fakes and local files.
Its tests exercise approval-before-factory ordering and pre-insert expiry,
IAP/firewall/service-account policy drift, crash windows, idempotency,
  readiness polling and timeout, startup/adapter/model integrity, three-
  container GPU isolation, runner command/attestation binding, 5 ms freshness
  admission, bounded IAP input/retrieval rejection, receipt recovery, residual-disk
   cleanup, name-replacement refusal, journal-loss recovery, and unconfirmed
  cleanup outcomes. The two-endpoint routing handoff is proved through local
loopback fixtures using the existing PR-B transport contract.

No test, validation command, or action taken for this PR accesses Google
credentials, ADC, provider APIs, SSH, a GPU, a registry, a bucket, Docker
registry, or a billable resource. The reviewed `execute` command is deliberately
present but has not been invoked: it can select the optional SDK adapter only
  after exact local approval and evidence-destination preflight. A real campaign still
requires a separate, exact authorization that binds the selected provider,
project, zone, topology, preloaded images/model, workload, normal execution
deadline, USD ceiling, observed-provider-backstop expectation, cleanup plan,
IAP identity, and evidence destination. This PR does not grant that authority
or publish any external artifact.

## Operator-facing limits and non-goals

- A quote/rate/cap is a controller estimate and guardrail, never a provider
  invoice, account cap, SKU guarantee, or billing proof.
- Provider maximum-runtime plus `DELETE` is the only provider-side backstop
  observed by this controller. It reduces known unbounded-instance risk; it
  does not prove cleanup in every theoretical provider or controller failure
  mode.
- The controller has no generic provider platform, autoscaler, GKE integration,
  public serving surface, model zoo, or generalized serving-runtime support.
- There is no dashboard expansion or Kubernetes projection here; those remain
  later, separately bounded work.
- Inferdrome records evidence and limitations. It does not declare a routing
  policy winner, a pass/fail/not-proven outcome, or a promotion decision.
- The required private engine-attestation endpoint is an Inferdrome adapter
  capability, not a claim about stock vLLM. This repository ships the adapter
  source and verifies its digest; a separately prepared, pinned image must
  contain it and the preloaded model. This PR does not build or publish that
  image or prove its availability.
- The one-host topology has no host, zone, network, or control-plane failure
  independence. Local fakes do not prove IAP authorization, GCE capacity,
  artifact availability, telemetry freshness, or a GPU campaign.
- The runner has one VM-local evidence bind and no controller-host Docker bind.
  The controller accepts evidence only through the bounded IAP archive and
  re-verifies it under a held descriptor; that does not turn the runner's local
  attestation into a provider or hardware attestation.

For the adversarial assumptions and recovery limits, see
[`GCP_PRIVATE_PRECAMPAIGN_V2_THREAT_SUPPLEMENT.md`](GCP_PRIVATE_PRECAMPAIGN_V2_THREAT_SUPPLEMENT.md).
