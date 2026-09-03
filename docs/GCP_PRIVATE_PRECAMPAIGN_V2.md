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
| Network exposure | private/internal only; no public inference listener |
| Engine interface | private health, OpenAI-compatible generation, target-specific metrics, and a bounded engine-attestation endpoint |

This is a controlled same-host, two-engine benchmark topology. It lets the
campaign exercise independently addressed engines and their admissible
telemetry; it does **not** establish host, zone, network, or control-plane
failure independence. Different machine types, GPU models/counts, endpoint
counts, public origins, duplicate endpoint identities, missing health/metrics
capabilities, or observed topology drift are rejected before campaign
admission.

Both the runner and the two serving images are repository-and-digest pinned.
The model and tokenizer revisions, serving runtime/adapter identity, startup
payload, fixed workload/trace/fault/policy inputs, source commit, and evidence
destination identity are all content-addressed. The startup payload is an
immutable semantic projection: it specifies exactly the two private engines,
their GPU assignments and ports, and the required endpoint paths. It contains
neither credentials nor prompts, generated text, authentication headers, or
public addresses.

## One-minute explanation

Inferdrome first proves that a very specific two-GPU VM and two private engines
are what an approved campaign asked for. Only then may a deliberately narrow
controller create it. The VM reports what actually became ready, and that
observation is converted into the already fail-closed PR-B routing-execution
configuration. The controller retrieves a sealed producer-side evidence
package, then proves exact resource cleanup. Every other result is an explicit
unconfirmed state rather than a success claim.

## Guarded lifecycle

The pre-campaign command has separate local-only `proposal` and `preview`
modes. They canonicalize and display a redacted, content-addressed proposal;
they do not initialize an SDK, discover ADC, contact a provider, or construct
a transport. The executable path is intentionally separate:

1. Build the exact two-engine proposal from pinned local inputs. It binds the
   provider, project, region/zone, machine and GPU topology, boot-image
   identity, image digests, model/tokenizer revisions, startup-payload digest,
   routing inputs, endpoint contract, request denominator, evidence-destination
   identity, quote identity/rate/currency/expiry, maximum runtime, cleanup
   horizon, and USD cap.
2. Verify a distinct exact human-approval artifact locally. A substituted,
   malformed, expired, or mismatched approval stops here. No provider client,
   network operation, or request dispatch is reachable before this check.
3. Persist a hash-chained `BACKSTOP_READY` record followed by `CREATE_INTENT`.
   The durable backstop projects provider maximum runtime with `DELETE`
   termination where that feature is supported, plus the exact local recovery
   identity. The cleanup horizon begins at that durable create intent: a
   controller deadline can block a late create, handoff, or destructive cleanup
   and record an unconfirmed state, while provider max-runtime `DELETE` remains
   an independent backstop. It is a cleanup backstop, not invoice enforcement.
4. Construct the lazy provider transport only after those checks, then repeat
   the approval and quote-expiry checks immediately before create. The create
   request uses caller-supplied idempotent request IDs and immutable ownership
   labels; the controller records and reconciles exact provider operations.
   After the first exact readback it retains only a hash of the provider
   instance identity, and each later destructive operation re-reads and binds
   that same identity before it can delete anything.
5. Require observed instance topology, boot-disk behavior, two engine
   identities, private readiness, and endpoint capability facts before handing
   the two origins to the PR-B routing runner. The readback independently
   verifies the approved boot-image provenance, owned boot-disk identity,
   private-only network shape, and fixed A2 GPU topology. Readiness performs a
   bounded direct (no proxy and no redirect) health, generation, and
   target-gauge probe. Each engine must also return a bounded private
   attestation matching its endpoint ID, container name, image digest, model,
   runtime, GPU ordinal, port, and startup digest. A healthy port alone is not
   admissible evidence of the GPU/container mapping; an unavailable or
   mismatched attestation blocks admission. The runner retains its own topology
   and telemetry-admissibility checks.
6. Retrieve the producer-side sealed evidence package through its bounded,
   digest-verified route. Receipts retain canonical identities and digests, not
   raw logs, provider payloads, prompts, model output, tokens, instance IDs,
   or public endpoint addresses. Local staging never places a raw workload
   under the evidence root. The preflight root descriptor is retained through
   handoff and receipt retrieval. Reservation, staging, and create-no-replace
   publication are all relative to a duplicate of that held descriptor; the
   visible root is independently rechecked so a same-user replacement yields
   no returned package or receipt.
7. Record cleanup intent, rebind the exact attached auto-delete boot disk
   before instance deletion, delete only exact owned residual disks, reconcile
   their operations, and direct-check the approved instance and boot-disk names
   before label-scoped absence confirmation. The two machine-fixed scratch
   disks are nonpersistent and disappear with the instance; extra or missing
   scratch-disk facts are topology drift. A failed delete, incomplete inventory,
   journal loss, or ambiguous operation is reported as unconfirmed; it is never
   relabeled as clean.

The runnable interface also has a constrained `recover-cleanup` path and a
label-scoped, read-only orphan-discovery path. Cleanup recovery has its own
authorization bound to the original project, zone, instance, disks, labels,
and controller identity, so it remains usable after launch approval expires.
It cannot create a replacement instance, widen discovery, infer ownership from
a hostname/IP, or delete a merely similar resource.

## Local/fake validation boundary

This PR validates the lifecycle only against injected fakes and local files.
Its tests exercise approval-before-factory ordering, crash windows,
idempotency, request/operation reconciliation, deadline handling, startup
integrity, topology drift, stale/malformed input, residual-disk cleanup,
journal-loss recovery, and unconfirmed-cleanup outcomes. The two-endpoint
routing handoff is proved through local loopback fixtures using the existing
PR-B transport contract.

No test, validation command, or action taken for this PR accesses Google
credentials, ADC, provider APIs, SSH, a GPU, a registry, a bucket, or a billable
resource. The reviewed `execute` command is deliberately present but has not
been invoked: it selects the optional SDK adapter only after its exact approval
and durable-backstop gates. A real campaign still requires a separate, exact
user authorization that binds the selected provider, project, zone, topology,
images, workload, deadline, USD ceiling, watchdog, cleanup plan, and evidence
destination. This PR does not grant that authority.

## Operator-facing limits and non-goals

- A quote/rate/cap is a controller estimate and guardrail, never a provider
  invoice, account cap, SKU guarantee, or billing proof.
- Provider maximum-runtime plus `DELETE` reduces known unbounded-instance
  risk; it does not prove cleanup in every theoretical provider or controller
  failure mode.
- The controller has no generic provider platform, autoscaler, GKE integration,
  public serving surface, model zoo, or generalized serving-runtime support.
- There is no dashboard expansion or Kubernetes projection here; those remain
  later, separately bounded work.
- Inferdrome records evidence and limitations. It does not declare a routing
  policy winner, a pass/fail/not-proven outcome, or a promotion decision.
- The required private engine-attestation endpoint is an adapter capability,
  not a claim about stock vLLM. A pinned image without that capability is
  rejected; this PR does not implement or publish a serving runtime.

For the adversarial assumptions and recovery limits, see
[`GCP_PRIVATE_PRECAMPAIGN_V2_THREAT_SUPPLEMENT.md`](GCP_PRIVATE_PRECAMPAIGN_V2_THREAT_SUPPLEMENT.md).
