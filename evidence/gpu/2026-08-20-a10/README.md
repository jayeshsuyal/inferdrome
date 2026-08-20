# 2026-08-20 A10 evidence handoff

This directory commits only the deterministic review and handoff metadata for
the exact 689,272-byte capture. The raw `capture.tar.gz` remains ignored and was
not added to Git or uploaded.

- `publication-review.json` records the bounded full-archive review and its
  three-state publication decision.
- `handoff-manifest.json` pins the producer commit, archive, bundle, capability
  profile, workload, model/tokenizer revisions, request population, native TTFT
  semantics, independently recalculated p95, rejection facts, chronology, and
  acceptance boundary.

Reproduce both documents from the unchanged local archive:

```bash
PYTHONPATH=src .venv/bin/python scripts/review_gpu_evidence_publication.py --check
```

The current review result is `EXTERNAL_ONLY`: no secret-shaped values, email
addresses, or public network addresses were detected, but the archive contains
prompts, generated responses, host paths, a private host-network address, GPU
UUIDs, process identifiers, diagnostics, and package inventory. Model, workload,
vLLM, generated-output, and repository-license decisions are not all resolved,
and the owner has not approved public delivery.

The proposed future release-asset location is checksum-pinned in the handoff
manifest. A release asset is detectably replaceable through that checksum, not
intrinsically immutable. Vendoring the exact reviewed bytes in ExitSpec remains
an alternative owner decision after license and privacy review.
