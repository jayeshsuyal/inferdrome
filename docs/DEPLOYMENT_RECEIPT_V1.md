# Deployment receipt v1

Status: **Additive outer provenance contract; PR4 issues only synthetic local
receipts**

The deployment receipt is a tamper-evident, provider-neutral report about one
validated deployment intent and one bounded lifecycle outcome. It is not an
evidence bundle, a measurement result, an invoice, or an acceptance verdict.
Inferdrome still measures through the existing benchmark pipeline; a receipt
never assigns `PASS`, `FAIL`, or `NOT_PROVEN`.

## Contract and boundaries

The closed Draft 2020-12 structural schema is
[`schemas/deployment/v1/deployment-receipt.schema.json`](../schemas/deployment/v1/deployment-receipt.schema.json).
The reference implementation is
[`src/inferdrome/deployment/receipt.py`](../src/inferdrome/deployment/receipt.py).
The schema is additive and deliberately outside `schemas/public/v1` and every
sealed evidence bundle. It does not mutate the frozen v0.1 evidence schemas.

The receipt binds:

- the exact `inferdrome.deployment.v1` canonical digest;
- the exact bounded lifecycle outcome canonical digest and full trace;
- source repository commit and Inferdrome version;
- explicit versioned provider/runtime adapter contract identities;
- declared model, revisions, runtime, images, topology, resources, timeouts,
  cleanup policy, and cost ceiling;
- locally observed controller cleanup/final-confirmation state; and
- reserved external/provider, invoice, and evidence-anchor sections.

`synthetic_local` is the only issuable kind in PR4. It must be development
`mock_only`, `synthetic_only: true`, `evidence_eligible: false`, have no
evidence anchor or provider attestation, and report invoice truth as
unavailable. A failed or unconfirmed local lifecycle can produce an explicit
synthetic diagnostic receipt, but it cannot become executed evidence. The
`executed` shape is reserved and fails closed because PR4 has no independently
verified proof-anchor or provider-attestation issuer. No cloud, GPU, Docker, or
network action is implied by any committed receipt fixture.

The receipt includes only bounded codes and typed projections. It excludes
secret values, credential resolution, raw provider payloads, exception text,
prompts, generated text, IP addresses, instance identifiers, and invoice
amounts that were not externally reported. Unknown fields and duplicate JSON
object keys are rejected before model validation; sensitive field names are
rejected without echoing their values.

## Canonical identity and verification

Receipt identity is non-recursive and has two distinct forms:

```text
receipt_id = SHA-256("inferdrome:deployment-receipt-v1\0" +
                     RFC 8785(canonical receipt payload without receipt_id))

receipt_sha256 = SHA-256(exact RFC 8785(canonical complete receipt JSON bytes))
```

The first is domain-separated and identifies the logical payload. The second
is the exact-byte hash returned by publication and is intentionally not stored
inside the receipt, avoiding a self-hash cycle. Reordered input JSON is parsed
once by the duplicate-detecting preflight and then validated; verification
requires the published bytes themselves to equal the canonical bytes.

Cross-input verification recomputes the deployment-spec and lifecycle-outcome
digests, compares their canonical values, checks source/version and adapter
identities, checks the declared projection and expected anchor, and recomputes
`receipt_id`. It therefore rejects payload mutation, stale or substituted
inputs, incompatible schema versions, duplicate keys, unknown fields, and
wrong publication-directory identities.

## Immutable publication

`publish_deployment_receipt()` delegates to the existing local immutable
publication primitive. It validates safe components, stages under a private
unique directory, writes with exclusive no-follow semantics, fsyncs the file
and directories, freezes the staged descriptor, publishes with no replacement,
and reads back the exact bytes. A destination already bearing the receipt ID
is never overwritten. A failed stage remains private and can be retried; it is
never treated as a published receipt. Verification opens the final descriptor
without following symlinks, enforces the size and read-only bounds, and checks
all canonical and cross-input invariants.

The publication path is local and offline. It does not create provider
resources, contact a network, expose credentials, or publish raw evidence.
