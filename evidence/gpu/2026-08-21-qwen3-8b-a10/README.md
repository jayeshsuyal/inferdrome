# 2026-08-21 Qwen3-8B A10 capability evidence

This directory commits deterministic metadata for one genuine, bounded
Qwen3-8B BF16 capability spike on a single NVIDIA A10. The raw archive remains
ignored and `EXTERNAL_ONLY`; no captured byte was redacted, rewritten, or
resealed for publication.

The committed records are:

- `publication-review.json`, the bounded full-archive content, privacy, secret,
  integrity, and license-presence review;
- `operational-summary.json`, a privacy-safe derivation from the exact retrieval,
  semantic-verification, and Lambda termination receipts; and
- `handoff-manifest.json`, the immutable archive, profile, model, workload,
  bundle, metric, operational, publication, and claim-boundary anchors.

## Observed result

```text
capture producer: 058482df47377aaae6303015746f9a8e05d7e0f7
profile: managed-vllm-0.26-qwen3-8b-bf16-v1
model: Qwen/Qwen3-8B
GPU observation: one NVIDIA A10
run: run-fcbd9a0a4031826ea601c5f14637e8dc
measured requests: 96
successful requests: 96
failed requests: 0
TTFT p50 / p95 / p99: 127,123,958 / 242,426,174 / 244,030,050 ns
output throughput: 28.870215 tokens/s
bundle: VALID, CUSTOMER_ELIGIBLE
post-termination semantic verification: VALID_AFTER_PROVIDER_TERMINATION
```

This is an observation-only runtime capability spike. It is not provider
hardware attestation, a cross-GPU comparison, causal attribution, or an
Inferdrome acceptance verdict. The producer-side ExitSpec contract digest is
null and chronology is explicitly `RETROSPECTIVE`.

## Reverification

When the exact ignored capture record is available at its default local path,
recalculate the archive, every bundle artifact, all 96 request records, the
nearest-rank TTFT population, model/profile bindings, and operational receipts:

```bash
PYTHONPATH=src .venv/bin/python \
  scripts/review_qwen3_gpu_evidence_publication.py --check
```

Open the same verified run in Inferdrome's loopback-only read-only dashboard:

```bash
PYTHONPATH=src .venv/bin/python \
  scripts/run_qwen3_evidence_dashboard.py --open
```

The dashboard launcher verifies the exact archive and committed review records,
extracts into an isolated temporary directory, and serves only the verified
`runs` root. It does not import a summary as if it were evidence.

Public CI cannot access an ignored local archive. It therefore validates the
committed shapes and all cross-digests without pretending to have reverified
missing bytes:

```bash
PYTHONPATH=src python \
  scripts/review_qwen3_gpu_evidence_publication.py --check-records
```

The engineering gate additionally performs the full `--check` automatically
when the exact default archive is present locally.

## Publication boundary

The review scanned all 52 regular files and 2,095,892 expanded bytes. It found
no secret-shaped values, email/customer data, public network addresses, binary
blind spots, or oversized unreviewed members. The untouched archive does retain
prompts, generated responses, absolute host paths, one private host-network
address, GPU UUIDs, process identifiers, logs, and package inventory.

Publication remains blocked until the owner explicitly resolves repository,
model, vLLM, workload, and generated-output licensing and approves delivery of
the exact archive. The checksum-pinned proposed release location is recorded in
`handoff-manifest.json`; no release asset was uploaded by this review.
