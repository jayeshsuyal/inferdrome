# Inferdrome v0.1 threat model

Status: **Frozen baseline for v0.1; comparison and GPU-publication supplements
accepted**

Threat-model date: **2026-08-05**

## Security objective

Inferdrome v0.1 makes an evidence bundle internally consistent, safely
inspectable, resistant to undetected post-seal mutation, and explicit about the
origin and completeness of its observations.

It is not a trusted execution system.

## Protected assets

- original and resolved experiment specifications;
- canonical request plans;
- native benchmark output;
- canonical request records;
- metric definitions and derived measurements;
- environment and execution metadata;
- artifact inventories and digests;
- retrieved GPU capture archives, publication reviews, and handoff manifests;
- evidence-eligibility labels; and
- ExitSpec ingestion receipts.

## Trust boundaries

### User-authored input

Experiment files and workload files are untrusted input. They may contain
malformed values, unsafe paths, secret-bearing URLs, unsupported fields, or
content that exceeds configured resource limits.

### Local execution host

The v0.1 execution host is operationally trusted. A malicious administrator,
compromised kernel, compromised Python runtime, or modified benchmark binary can
fabricate observations that Inferdrome cannot detect.

### Benchmark producer

The pinned benchmark producer is trusted as a measurement sensor only for the
capabilities established by its fixture-backed adapter contract. Inferdrome does
not reinterpret stronger semantics than the producer exposes.

### Attached endpoint

An attached endpoint is not trusted to report exact server provenance. Model
names and server metadata are `SERVER_REPORTED` unless verified through a
separate local observation.

The managed loopback profile replaces remote launch ambiguity with local file,
process, and NVIDIA observations. Those observations are still made by the
operationally trusted host: they show that the supervised vLLM process group
engaged the selected GPU, but they are not hardware attestation and do not
prove that any particular request executed on that GPU.

### Evidence transport and storage

Bundles may be corrupted or modified after execution. Exact-byte hashing and an
external receipt detect changes relative to the digest that ExitSpec accepted.

Retrieved GPU capture archives are a larger transport boundary than one bundle:
they also contain private workspaces, logs, package inventories, comparison
artifacts, deliberate corruption vectors, and synthetic fixtures. Before any
public delivery, the exact compressed bytes require checksum verification,
path-safe isolated extraction, bounded full-member content review, an explicit
three-state publication decision, and separate owner approval. Review metadata
never authorizes rewriting a sealed archive.

### Kubernetes local orchestration boundary

The Kubernetes contract and wrapper are untrusted-cluster orchestration
inputs, not an execution attestation. The validator is offline and rejects
duplicate/unknown YAML shapes, public or host resources, mutable GPU images,
credential-bearing environment forms, and runner/engine topology drift. The
mock wrapper owns one unique local namespace and cluster, captures one bounded
completed-runner log before cleanup, and publishes the synthetic output outside
the disposable Pod. A Kubernetes log or `emptyDir` is not treated as durable
real evidence. The GPU template requires an operator-provided evidence PVC;
without independent PVC and retrieval verification, persistence and evidence
eligibility remain unknown.

### ExitSpec importer

The importer processes an attacker-controlled directory or transport package.
It must treat paths, file metadata, schemas, and record contents as hostile.

## Threats v0.1 addresses

### Accidental artifact corruption

The hash manifest detects changed, truncated, removed, or substituted material
artifacts after sealing.

### Summary manipulation

ExitSpec independently recalculates any metric used for a verdict from canonical
records. A modified summary that disagrees with those records causes ingestion
to fail.

### Hidden record loss or duplication

Schema and integrity verification check record counts, native-source locators,
identifier uniqueness, expected measured populations, and timestamp ordering.

### Synthetic-evidence promotion through honest flows

The fake adapter writes immutable synthetic execution and eligibility markers.
Inferdrome and ExitSpec reject honest fake-adapter bundles from customer-evidence
flows.

This does not stop a malicious producer from fabricating an entirely different
bundle before an external trust anchor exists.

### Secret leakage through configured inputs

The resolver rejects user-info and query secrets in endpoint URLs. Invocation
capture uses a structured argument vector and redacts configured secret-bearing
arguments. Environment capture is allowlist-based rather than a dump of the
process environment.

### Unsafe bundle import

The importer rejects or constrains:

- absolute paths and `..` traversal;
- symlinks, device files, sockets, and unsupported file types;
- duplicate, case-colliding, or Unicode-confusable artifact paths;
- files not declared by the manifest when policy requires a closed bundle;
- oversized bundles, files, record counts, and JSONL lines;
- malformed UTF-8 and duplicate JSON keys;
- `NaN`, positive infinity, and negative infinity;
- unsupported schemas and producer versions;
- duplicate request identifiers and invalid source locators; and
- impossible timestamp or population relationships.

Verification performs no network access and does not execute bundle content.

## Threats v0.1 does not address

- a producer fabricating a complete, internally consistent run;
- a compromised execution host or benchmark executable;
- a server lying about its model, version, tokenizer, or environment;
- malicious modification followed by regeneration of all hashes before an
  external receipt is created;
- proof that a request reached a particular physical GPU;
- hardware-backed timestamping or attestation;
- cryptographic proof of authorship;
- confidentiality of full native artifacts at rest;
- traffic interception outside the configured TLS and endpoint controls; or
- denial of service beyond local runtime and size limits.

These exclusions must remain visible in product documentation and demos.

## Provenance model

Environment and identity fields retain one of these provenance values:

| Provenance | Meaning |
|---|---|
| `DECLARED` | Supplied by the experiment author without independent observation |
| `CLIENT_OBSERVED` | Observed by the Inferdrome execution process |
| `SERVER_REPORTED` | Returned by the attached endpoint without external verification |
| `LOCALLY_VERIFIED` | Read from the managed local host or artifact under Inferdrome's control |
| `UNKNOWN` | Not available |

Completeness is derived from required fields and their provenance. A scalar
`COMPLETE` label never erases field-level provenance.

## Integrity and receipt semantics

The evidence manifest hashes exact artifact bytes. The `bundle_digest` hashes
the canonical manifest. The manifest does not hash itself.

Before ExitSpec accepts a bundle, a producer can replace both an artifact and
its hashes. After ExitSpec stores an ingestion receipt containing the digest,
the receipt establishes which exact manifest ExitSpec evaluated.

The receipt proves receipt and evaluation of that digest. It does not prove
execution, producer identity, or hardware identity.

## Privacy

### Native output

Detailed upstream output may contain generated response text even when canonical
records omit it. Native preservation therefore controls the bundle's sensitivity
classification.

The v0.1 demonstration uses non-sensitive prompts and responses and labels full
native bundles as containing response content.

### Content hashes

Plain SHA-256 hashes of low-entropy prompts are pseudonymous identifiers, not
anonymization; they may be vulnerable to dictionary guessing. Documentation and
schemas must not claim otherwise.

### Logs

Logs must avoid authorization headers, API keys, signed query strings, cloud
credentials, Hugging Face tokens, complete process environments, and raw content
unless the run's sensitivity policy explicitly permits it.

Redaction failures make the bundle ineligible rather than silently deleting a
sealed native artifact.

### Exact A10 archive publication review

The 2026-08-20 A10 review is recorded at
[`evidence/gpu/2026-08-20-a10/publication-review.json`](../evidence/gpu/2026-08-20-a10/publication-review.json).
Archive path, duplicate-member, link, special-file, member-count, expanded-size,
and compressed-size checks passed, followed by a bounded scan of every regular
file. No secret-shaped values, email addresses, or public network addresses
were detected. Prompts, generated responses, diagnostics, package inventory,
absolute paths, a private host-network address, GPU UUIDs, and process
identifiers remain present because the archive was not rewritten.

The classification is `EXTERNAL_ONLY`. Missing owner-approved repository,
model, workload, vLLM, and generated-output license decisions block
`APPROVED_PUBLIC`, as does the absence of explicit owner publication approval.
The raw archive therefore remains ignored and local. This technical result is
not legal advice and does not replace the final human security and privacy
review.

## Availability controls

Every run configures:

- a maximum total runtime;
- a maximum measured-request count;
- a maximum total producer runtime with bounded cancellation escalation;
- graceful cancellation followed by forced termination bounds;
- bounded captured stdout and stderr behavior; and
- bounded finalization and verification work.

v0.1 performs no automatic retry.

## Security invariants

1. Verification is offline and read-only.
2. No bundle path is trusted before normalization and validation.
3. No derived summary is sufficient for an ExitSpec verdict by itself.
4. Unknown provenance remains unknown.
5. Synthetic eligibility cannot be upgraded by normal Inferdrome transitions.
6. A failed integrity check cannot produce `COMPLETE`.
7. Secrets are never intentionally written to the evidence bundle.
8. Content sensitivity is determined by all artifacts, including native output.
9. Digest verification is never described as execution attestation.
10. Publication review never mutates evidence or substitutes for owner approval.

## Post-v0.1 controlled-comparison supplement

Controlled-comparison plans, results, and their retained out-of-band digests
are additional protected assets. Their roots and descriptors are untrusted
filesystem input; plan authors and operators remain inside the operational
trust boundary rather than becoming independently attested identities.

The comparison reader accepts only validated direct-child IDs and an exact
one-file immutable descriptor directory. It opens directory-relative with
no-follow semantics, requires a regular file with one hard link, enforces byte
and entry bounds, and checks stable file and directory identity around reads.
Canonical bytes, closed schemas, distinct plan/result digest domains, retained
expected digests, exact member IDs, and full bundle re-verification address
accidental drift, substitution, unsafe paths, ambiguous inventory, stale Trial
Sets, invented arithmetic, selective membership, and result/plan mismatch.

The treatment is a closed typed union containing only
`traffic.concurrency`. Full resolved arm specifications and recomputed
fingerprints must differ at exactly that path. Six closed result controls make
schedule, membership, environment, and outcome failures explicit; any failure
returns `INCOMPARABLE` and the public result contains no outcome values. This
prevents a failed control from leaking a selectively filtered estimate.

These controls do not address a malicious operator backdating a plan,
fabricating a complete internally consistent run, manipulating the host clock,
or controlling an unobserved confounder. `PREDECLARED` is therefore paired with
`OPERATOR_ATTESTED`, and local plan-order checks are internal consistency only.
Environment equality is explicitly `OBSERVED_V1_ALLOWLIST_ONLY`; unobserved
host, service, thermal, network, or workload conditions may still differ.

Likewise, plan and result digests prove consistency with retained bytes, not
authorship, chronology, execution truth, causal identification, statistical
significance, preference, or customer acceptance. Independent chronology would
require a future signed plan-to-assignment binding and a trusted timestamp or
transparency receipt. Broader causal claims would require separately reviewed
design and observation controls.

## Future controls

Post-v0.1 work may add signed manifests, Sigstore identities, trusted build
provenance, remote timestamping, encrypted native annexes, and managed execution
attestation. Each control requires a new threat-model revision and architecture
decision; signing alone must not be presented as proof that claimed execution
facts are true.
