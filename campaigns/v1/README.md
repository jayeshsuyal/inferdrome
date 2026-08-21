# Operational GPU campaign plan

The generated documents in this directory freeze Inferdrome's planned Qwen
cross-GPU methodology before producer-profile implementation or paid execution.

- `gpu-campaign-plan.schema.json` is the closed structural schema.
- `qwen-gpu-capability-campaign.json` is the canonical design-only plan.
- `tests/fixtures/campaigns/v1` contains positive and mutation vectors.

The plan separates a same-model hardware-control track from a different-model
capability ladder. The control track does not become a controlled-comparison v1
result: hardware is not a supported v1 treatment and cross-host equality is not
established. The ladder cannot rank GPU speed because its model profiles differ.

Every assignment remains `UNPROVEN_REQUIRES_SPIKE`. This document does not
authorize a Lambda launch, prove model fit, attest execution, modify the genuine
Qwen2.5 canary, or assign an acceptance verdict.

Regenerate or verify the committed bytes with:

```bash
PYTHONPATH=src python3 scripts/generate_gpu_campaign.py
PYTHONPATH=src python3 scripts/generate_gpu_campaign.py --check
```

The governing decision is
[ADR 0010](../../docs/adr/0010-freeze-cross-gpu-capability-campaign.md).
