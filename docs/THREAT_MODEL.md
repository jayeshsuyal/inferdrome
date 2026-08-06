# Inferdrome v0.1 threat model

Status: **Frozen baseline for v0.1**

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

### Evidence transport and storage

Bundles may be corrupted or modified after execution. Exact-byte hashing and an
external receipt detect changes relative to the digest that ExitSpec accepted.

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

## Future controls

Post-v0.1 work may add signed manifests, Sigstore identities, trusted build
provenance, remote timestamping, encrypted native annexes, and managed execution
attestation. Each control requires a new threat-model revision and architecture
decision; signing alone must not be presented as proof that claimed execution
facts are true.
