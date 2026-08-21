# Operational GPU campaign plan

The generated documents in this directory freeze Inferdrome's planned Qwen
cross-GPU methodology before producer-profile implementation or paid execution.

- `gpu-campaign-plan.schema.json` is the closed structural schema.
- `qwen-gpu-capability-campaign.json` is the canonical design-only plan.
- `profiles/managed-vllm-0.26-qwen3-8b-bf16-v1.json` is the opt-in,
  locally conformant but runtime-unproven Qwen3-8B profile.
- `profiles/qwen3-8b-profile.schema.json` closes that profile structurally;
  semantic mutation vectors live with the campaign fixtures.
- `workloads/qwen-text-mixed-length-v1.jsonl` and its manifest bind the exact
  96-prompt measured population.
- `qwen3-8b-concurrency-{1,4,16}.yaml` are the three exact attached sources.
- `qwen3-workload-synthetic-smoke.yaml` exercises the same workload locally but
  can produce only synthetic evidence.
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
PYTHONPATH=src python3 scripts/generate_qwen3_launch_profile.py
PYTHONPATH=src python3 scripts/generate_qwen3_launch_profile.py --check
```

ADR 0010 governs the two-track design. ADR 0011 supersedes only its per-bucket
warmup allocation after exact vLLM 0.26.0 behavior was reviewed; all twelve
warmups now truthfully bind to repeated sequence index zero.

The local verification and future managed invocation are documented in the
[Qwen3 campaign profile runbook](../../docs/QWEN3_CAMPAIGN_PROFILE.md).
