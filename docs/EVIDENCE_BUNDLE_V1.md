# Evidence bundle and offline verification

Status: **Implemented for PR 4 and extended for PR 5**

Implementation date: **2026-08-05**

PR 4 turns finalized execution artifacts into a portable, closed, read-only
evidence directory. A run reaches `COMPLETE` only after the published directory
passes the same offline verifier exposed to consumers.

## Workspace and bundle are different objects

The run workspace contains mutable control state and lock files. It is never
presented as the evidence bundle. The writer creates `bundle.staging` beneath
the reserved run directory, writes only declared public artifacts, verifies the
staging tree, makes every file and directory owner-read-only, atomically renames
it to `bundle`, verifies it again in immutable mode, and only then records the
`COMPLETE` state transition with `IntegrityStatus.VALID`.

An existing staging or final path is never overwritten. A failed staging or
verification attempt remains visibly incomplete for diagnosis.

## Closed v1 layout

The writer emits exactly sixteen artifact roles:

```text
bundle/
├── bundle.json
├── experiment.original.yaml
├── experiment.resolved.json
├── request-plan.json
├── environment.json
├── execution.json
├── native/
│   ├── invocation.json
│   ├── producer-version.txt
│   ├── exit-status.txt
│   ├── benchmark-result.json
│   ├── stdout.log
│   └── stderr.log
├── records/
│   └── requests.jsonl
├── definitions/
│   └── metrics.json
├── derived/
│   └── measurements.json
└── integrity/
    └── artifact-hashes.json
```

`bundle.json` inventories every role exactly once with a unique normalized
relative path, media type, and sensitivity. The writer derives conservative
prompt and response sensitivity from the resolved policies and native-content
declaration.

## Manifest and bundle digest

`inferdrome.integrity-manifest.v1` contains one lexicographically ordered entry
for every role except the integrity manifest itself:

```text
normalized relative path
artifact role
exact byte size
sha256:<lowercase digest of exact bytes>
```

The manifest hashes `bundle.json`; the manifest does not hash itself. The final
`bundle_digest` is:

```text
SHA-256("inferdrome:bundle-manifest-v1\0" || canonical_manifest_bytes)
```

It is emitted out of band and is deliberately absent from `bundle.json`.
Embedding it in a manifest-covered artifact would create a circular dependency.

Changing both an artifact and its manifest entry creates a different internally
consistent bundle with a different digest. This is expected. Integrity proves
consistency with bytes identified by a retained digest; it does not prove
authorship or execution truth. An ExitSpec receipt becomes the external anchor
for the exact digest it evaluated.

## Safe reader

The offline reader performs no network access and executes no bundle content.
Before semantic use it:

- requires a real directory and regular files;
- rejects symlinks, hard-linked files, devices, sockets, and other special
  nodes;
- accepts only bounded ASCII relative paths with no dot segments;
- rejects case-insensitive path collisions;
- applies file-count, directory-count, depth, per-file, total-byte, and JSONL
  line limits;
- opens files with no-follow behavior and verifies scanned inode, device, and
  size identity;
- rejects writable files and directories when verifying a sealed bundle;
- rejects undeclared files and directories; and
- rejects malformed UTF-8, duplicate JSON keys, non-finite numbers, malformed
  JSONL, and non-canonical normative JSON.

The reader limits are explicit `BundleLimits` values and may be tightened by an
importer. ExitSpec adds its own independently maintained reader in PR 6 rather
than importing this implementation.

## Cross-artifact verification

After exact-byte hashes pass, the verifier checks:

- descriptor and manifest role/path agreement;
- source, request-plan, execution-fingerprint, metric-definition, and optional
  ExitSpec-contract digests;
- run, experiment, producer, adapter, traffic, replayability, environment, and
  sensitivity agreement;
- declared environment evidence paths plus server-model, producer-version, and
  configured target-identity cross-binding;
- request-plan, fake native row, canonical record, source locator, prompt hash,
  response hash, timing, token, outcome, and producer-fingerprint agreement;
- exact pinned-vLLM invocation regeneration, raw version identification,
  native-shape enforcement, byte-identical renormalized records, and rebuilt
  execution evidence;
- producer version, exit-status text, native-result hash, and UTF-8 logs;
- canonical request-record ordering and cardinality; and
- independent deterministic recalculation of all stored measurements.

The verifier supports both the complete synthetic path and the pinned vLLM
`0.26.0` path. Producer-specific native normalization remains separate, while
both paths converge on the same public request, execution, and measurement
contracts.

## Mutation guarantees

The adversarial suite covers changed bytes, truncation through size mismatch,
deletion, undeclared injection, duplicate JSON keys, symlinks, hard links,
writable trees, coherent rehashing against a retained digest, and bounded-reader
limits. Verification snapshots prove the verifier itself leaves the bundle
unchanged.

These guarantees are relative to the retained manifest digest. A malicious
producer can still fabricate a new internally consistent bundle before an
external trust anchor exists, exactly as stated in the threat model.

## Python verification

```python
from pathlib import Path

from inferdrome.bundle import verify_bundle

report = verify_bundle(
    Path("runs/run-.../bundle"),
    expected_bundle_digest="sha256:...",
)
print(report.run_id, report.bundle_digest)
```
