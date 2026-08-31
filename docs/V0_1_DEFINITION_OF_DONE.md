# Inferdrome v0.1 definition of done

Status: **Frozen release gate**

Inferdrome v0.1 ships only when every mandatory criterion below is satisfied.
An unmet criterion is release-blocking; it cannot be waived by relabeling a
demonstration or manually editing an evidence bundle.

## 1. Public contracts

- Public v1 schemas are versioned, documented, and closed to ambiguous fields.
- Schemas are based on the completed pinned-vLLM capability spike.
- JSON Schema and Python model validation agree on all conformance fixtures.
- Unknown producer output shapes fail normalization.
- Schema, adapter, normalizer, metric-definition, reducer, and producer versions
  are independently identifiable.
- Compatibility and deprecation rules are documented.

## 2. Resolution and identity

- The original specification bytes are preserved.
- The resolved specification contains explicit defaults and provenance.
- The request plan is frozen before measurement begins.
- `source_spec_digest`, `execution_fingerprint`, `request_plan_digest`,
  `metric_definitions_digest`, and `bundle_digest` have documented, disjoint
  domains.
- Secret-bearing endpoint URLs are rejected.
- Unpinned or unverifiable inputs either fail strict mode or remain visibly
  incomplete; they are never silently upgraded.

## 3. Execution lifecycle

- Run-state transitions are validated and crash-tested.
- Every run has maximum runtime and measured-request limits.
- Cancellation is bounded and preserves diagnostic artifacts when safe.
- Preflight, warmup, and measurement phases are distinguishable.
- Request-level endpoint failures do not incorrectly become harness failures.
- v0.1 performs no automatic retry.
- A run cannot reach `COMPLETE` until its sealed bundle verifies successfully.

## 4. Producer adapters

- The fake adapter always produces `SYNTHETIC_ONLY` evidence.
- Exactly one vLLM benchmark version is supported and pinned.
- The supported vLLM version has a published capability matrix.
- Invocation uses a structured argument vector and records the redacted command,
  tool identity, exit status, stdout, stderr, and native result.
- Native output is preserved byte-for-byte.
- Golden fixtures cover successful, failed, malformed, and changed upstream
  outputs.
- Missing upstream observations remain unavailable.

## 5. Canonical request records

- Every measured request has exactly one canonical v0.1 record.
- Every record has a stable run-scoped identity and native source locator.
- Failed measured requests are retained.
- Warmup observations never enter measured populations.
- Timing values use documented source semantics and integer canonical units.
- Redundant values are either derived or checked for exact consistency.
- Token-count origin is explicit.
- Unsupported HTTP status, finish reason, retry, and streaming-event details are
  absent rather than reconstructed from error strings.

## 6. Measurements

- Request counts, error rate, producer first-choices TTFT,
  last-choices-event span, and attempted, successful, and output-token
  throughput are implemented with precisely named definitions.
- Every measurement identifies its population, unit, sample count, definition,
  quantile method, reducer version, and rounding policy.
- Attempted and successful throughput are reported separately.
- Missing metrics are absent or null, never zero.
- Native upstream E2E and TPOT summaries without request-level source values
  remain diagnostic rather than verdict evidence.
- Empty latency populations are omitted rather than represented by fabricated
  zero values.
- Stable inputs produce byte-identical derived outputs.
- Independent conformance vectors cover edge cases and denominator choices.

## 7. Evidence bundle

- The bundle includes all normative artifact roles or an explicit, schema-valid
  reason an optional artifact is unavailable.
- Replayability is `FULL` only when the canonical request plan is included.
- The manifest uses normalized relative paths, exact sizes, roles, and SHA-256
  hashes.
- Completed bundles are immutable.
- Verification is offline and read-only.
- Mutation, deletion, duplication, truncation, and undeclared-artifact tests fail
  safely relative to the retained manifest and digest. Replacing both an
  artifact and its manifest entry necessarily changes `bundle_digest`.
- Full detailed vLLM native output causes a visible response-content sensitivity
  classification.
- A bundle never claims cryptographic authorship or execution attestation.

## 8. ExitSpec integration

- ExitSpec imports bundles without importing Inferdrome runtime modules.
- Import limits path traversal, symlinks, file types, sizes, counts, malformed
  text, duplicate keys, and invalid numeric values.
- ExitSpec verifies the manifest before using evidence.
- ExitSpec independently recalculates every metric used for a verdict.
- A summary/record disagreement is rejected as inconsistent evidence.
- The receipt stores the accepted `bundle_digest`, contract digest, verifier
  version, receipt timestamp, ingestion status, and optional verdict.
- Workload, target, environment, and achieved-load mismatches follow a documented
  applicability decision table.

This section is separately owned ExitSpec work. Inferdrome can preserve the
handoff boundary and verify its own bundle, but its gates cannot prove the
importer, receipt, or acceptance outcomes.

## 9. Flagship demonstration

A clean GPU host can follow the reproduction guide and produce a new bundle for
the pinned demonstration workload.

The demo shows five distinct outcomes:

1. `PASS`: valid, applicable, sufficient evidence meeting the frozen threshold.
2. `FAIL`: valid, applicable, sufficient evidence violating the threshold.
3. `NOT_PROVEN`: valid evidence that is insufficient or inapplicable to a
   required condition, such as achieved concurrency.
4. `REJECTED`: one material artifact was corrupted after sealing.
5. `REJECTED`: a synthetic fixture was submitted to the customer-evidence path.

PASS and FAIL contracts are frozen before their corresponding demonstrations.
The failure case is produced honestly through the workload or threshold, not by
editing measurements.

Current implementation checkpoint: Inferdrome's managed Linux/NVIDIA harness,
complete local provenance path, repeatable flagship run, corrupted-copy
rejection, and synthetic customer-flow rejection are implemented. A genuine
compatible-host A10 bundle is retained as producer-side evidence, but it does
not close archive publication, human sign-off, or acceptance. The separately
owned ExitSpec `PASS`, `FAIL`, and `NOT_PROVEN` demonstrations remain
release-blocking.

## 10. Security and privacy

- The v0.1 threat model is linked from the README and generated documentation.
- Import verification requires no network access or code execution.
- Environment capture is allowlist-based.
- Secret-redaction tests cover arguments, URLs, headers, environment metadata,
  stdout, and stderr handling.
- Low-entropy content hashes are not described as anonymization.
- The demonstration workload is explicitly approved as non-sensitive.
- Resource limits and cancellation behavior have adversarial tests.

## 11. Engineering quality

- Supported Python and operating-system targets are documented.
- Formatting, linting, static typing, unit, integration, golden, and adversarial
  tests pass in the engineering gate.
- A GPU is not required for normal pull-request tests.
- At least one opt-in real-GPU smoke test produces a stored example bundle.
- Documentation commands are tested or copied from executable demo scripts.
- The owner-selected Apache License 2.0 is added for Inferdrome with matching
  package metadata and scoped third-party notices, contribution guidance is
  present, and a tagged `v0.1.0` release identifies the reviewed commit. This
  repository license does not resolve archive-bound or external-material
  licensing and publication decisions.

## Release sign-off

The release checklist must link to evidence for each section above. The sign-off
records the exact commit, engineering-gate result, GPU demonstration bundle
digest, and ExitSpec receipt digest.
