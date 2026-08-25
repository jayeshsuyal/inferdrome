# 2026-08-23 Qwen3-8B A100 SXM4 capability evidence

This directory commits privacy-safe metadata for one bounded Qwen3-8B BF16
runtime observation on a single reported NVIDIA A100-SXM4-40GB. The exact raw
archive remains ignored and `EXTERNAL_ONLY`; no captured byte was redacted,
rewritten, or resealed for publication.

The committed records are:

- `publication-review.json`: bounded full-archive integrity, content, secret,
  and publication review results;
- `operational-summary.json`: privacy-safe controller receipts and an estimated
  cost window; and
- `handoff-manifest.json`: immutable archive, model, workload, bundle, runtime,
  provider, and claim-boundary anchors.

## Facts and claim boundaries

Measured facts from the sealed Inferdrome bundle:

```text
GPU observation: NVIDIA A100-SXM4-40GB, one CUDA device
model: Qwen/Qwen3-8B
runtime: vLLM 0.26.0
run: run-9a01c9d8b4044e56eb68b2cf0345f5e0
measured requests: 96
successful requests: 96
failed requests: 0
TTFT p50 / p95 / p99: 42.974685 / 79.279716 / 80.570049 ms
output throughput: 73.377319 tokens/s
bundle: VALID, CUSTOMER_ELIGIBLE
```

Locally verified facts are the exact archive SHA-256, capture-manifest SHA-256,
bundle digest, model/workload/profile digests, offline recalculation, and
post-termination semantic verification. These are integrity and provenance
checks, not provider hardware attestation or an Inferdrome acceptance verdict.

The operational summary reports a controller-observed estimate of `$0.432165`
for a 781.806878-second window at the captured `$1.99/hour` rate, bounded by a
`$1.25` session ceiling. This is an estimate derived from local controller
receipts. Provider billing granularity and the final provider invoice are
external truth and are not represented as verified by this repository.

The capture is retrospective: no producer-side ExitSpec contract digest was
frozen before measurement. It is not a cross-GPU comparison, causal result,
hardware attestation, or `PASS`/`FAIL`/`NOT_PROVEN` acceptance verdict.

## Reverification and dashboard

When the exact ignored archive and retrieval receipts are available locally:

```bash
PYTHONPATH=src .venv/bin/python \
  scripts/review_qwen3_a100_sxm4_evidence.py \
  /path/to/capture.tar.gz \
  --capture-record /path/to/retrieved-record \
  --check

PYTHONPATH=src .venv/bin/python \
  scripts/run_qwen3_a100_sxm4_evidence_dashboard.py \
  --archive /path/to/capture.tar.gz \
  --capture-record /path/to/retrieved-record
```

Both paths hash the archive, extract into an isolated temporary directory,
reject unsafe archive members, verify the A100-tier capture and sealed bundle,
and only then expose the verified `runs` root to the loopback-only read-only
dashboard. A writable retained extraction is never treated as evidence.

Public CI validates the committed metadata and cross-digests with
`--check-records`; it does not pretend to possess the ignored archive. The raw
archive is not distributable under this record and no release asset was
uploaded.
