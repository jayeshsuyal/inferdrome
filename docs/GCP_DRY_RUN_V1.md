# GCP read-only inventory and dry-run plan v1

Status: **offline planning only; no GCP lifecycle authority**

PR7 adds a bounded GCP planning boundary in
[`src/inferdrome/deployment/gcp.py`](../src/inferdrome/deployment/gcp.py). It
accepts the existing `provider_id: gcp` and `execution_intent:
dry_run_reference` deployment specification plus two explicit local inputs:

- a privacy-safe `inferdrome.gcp-inventory.v1` snapshot; and
- a `GcpPlanningContext` containing the compute project identifier and region.

The compute project is explicit planning scope. It is never derived from a
Secret Manager reference: a secret resource can live in a separate security
project. Credential references remain unresolved identifiers and cannot affect
resource selection.

## What this slice does

The planner is fully offline. It imports no GCP SDK, opens no network or
metadata-server connection, reads no environment credentials, invokes no
subprocess, and performs no provider mutation. It validates the exact existing
deployment shape, cross-checks the explicit compute scope against the supplied
inventory, and chooses the first stable `(zone, machine type, accelerator)`
candidate after sorting the explicit zone-scoped offerings by their closed
identity fields. An offering binds one exact zone to one machine/accelerator
tuple; the planner never forms a zone/machine Cartesian product.

The inventory is an input claim, not live capacity. The resulting plan marks
all of these as false: `execution_authorized`,
`provider_mutation_performed`, `credentials_resolved`, `capacity_proven`,
`pricing_proven`, and `evidence_eligible`. Pricing is
`not_evaluated_pricing_unavailable`; the plan carries only the deployment's
declared hard USD ceiling. Invoice truth is unavailable. The artifact is not a
lifecycle outcome, deployment receipt, GPU receipt, capacity claim, price
claim, or evidence bundle.

The inventory contract has no endpoint, public-IP, token, credential, raw
provider-payload, billing, or instance-identifier fields. Values and keys are
bounded, object keys are unique, unknown fields are rejected, and non-finite
numbers are forbidden. The checked-in inventory is synthetic and uses opaque
`synthetic-*` resource identities. Each entry uses `catalog_eligibility`, not
provider availability; `catalog_eligible` means only that the supplied offline
catalog permits deterministic selection. It is not an assertion about current
Google Cloud offerings, capacity, or availability.

## Identity and publication

The generated plan is
[`plans/gcp/v1/gcp-dry-run-reference.plan.json`](../plans/gcp/v1/gcp-dry-run-reference.plan.json).
Its canonical bytes are RFC 8785 JSON with no trailing transport newline.
`plan_id` is a domain-separated digest over the canonical plan payload with
`plan_id` excluded, avoiding recursive self-hashing:

```text
inferdrome:gcp-dry-run-plan-v1\0 + RFC 8785 canonical payload bytes
```

`gcp_plan_sha256()` is the ordinary SHA-256 of the exact complete canonical
plan bytes. The plan binds the deployment-spec digest, inventory digest,
explicit compute scope, versioned provider/runtime adapter identities,
immutable runner/serving image digests, model and runtime identity, private
topology, selected/requested resources, timeout/cleanup policy, and declared
cost ceiling.

`publish_gcp_dry_run_plan()` revalidates the supplied models and recomputes the
plan before creating any root or staging directory. It then uses the existing
symlink-safe, fsync-backed, no-replace immutable directory primitive and reads
the exact bytes back. Existing identities, symlinks, tampering, wrong inputs,
duplicate keys, and incompatible schemas fail closed. A plan publication is
separate from PR4's outer deployment receipt and from sealed evidence bundles.

## Local commands

Use the repository's Python 3.12 environment. These commands read only local
files:

```bash
PYTHONPATH=src .venv/bin/python scripts/gcp_dry_run_plan.py plan \
  --deployment-spec deployments/v1/examples/gcp-dry-run-reference.json \
  --inventory tests/fixtures/gcp-v1/synthetic-inventory.json \
  --context tests/fixtures/gcp-v1/synthetic-planning-context.json \
  --output-root plans/gcp/published

PYTHONPATH=src .venv/bin/python scripts/gcp_dry_run_plan.py verify \
  --deployment-spec deployments/v1/examples/gcp-dry-run-reference.json \
  --inventory tests/fixtures/gcp-v1/synthetic-inventory.json \
  --context tests/fixtures/gcp-v1/synthetic-planning-context.json \
  --plan plans/gcp/v1/gcp-dry-run-reference.plan.json
```

The checked-in currentness command is:

```bash
PYTHONPATH=src .venv/bin/python scripts/generate_gcp_dry_run_plan.py --check
```

## Deferred boundary

IAM/authentication, live read-only inventory collection, official provider
resource mappings, pricing snapshots, capacity truth, resource acquisition,
runtime launch, teardown, and provider receipts remain deferred. PR8 may
consume the unchanged deployment-spec/resource/image contract only after a
separate explicit authorization gate and a new mutating adapter review. PR8
must not reinterpret this plan as authorization or a provider attestation.
