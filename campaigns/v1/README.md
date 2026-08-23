# Operational GPU campaign plan

The generated documents in this directory freeze Inferdrome's planned Qwen
cross-GPU methodology before producer-profile implementation or paid execution.

- `gpu-campaign-plan.schema.json` is the closed structural schema.
- `qwen-gpu-capability-campaign.json` is the canonical design-only plan.
- `profiles/managed-vllm-0.26-qwen3-8b-bf16-v1.json` is the opt-in,
  locally conformant but runtime-unproven Qwen3-8B profile.
- `profiles/qwen3-8b-profile.schema.json` closes that profile structurally;
  semantic mutation vectors live with the campaign fixtures.
- `execution-packs/qwen3-8b-a100.json` binds the zero-spend, same-model A100
  hardware-control execution boundary.
- `execution-packs/qwen3-a100-execution-pack.schema.json` closes that pack
  structurally and rejects unearned runtime, hardware, or launch claims.
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

The canonical plan preserves every assignment's pre-execution
`UNPROVEN_REQUIRES_SPIKE` state. A separate reviewed receipt records one genuine
Qwen3-8B A10 observation without rewriting that plan. The A100 execution pack
is separately `LOCALLY_CONFORMANT_RUNTIME_UNPROVEN`: it reuses the exact
Qwen3-8B profile and workload for the hardware-control track, targets exactly
one A100 40 GB PCIe reported by `nvidia-smi` as `NVIDIA A100-PCIE-40GB` rather
than an SXM or 80 GB variant, and requires
Lambda's exact API `instance_type_name` as a runtime argument.

The A100 rate and cap are frozen at `$1.99/hour` and `$1.25`. The runtime API
rate must match exactly, one paid instance is allowed, and an operator must
explicitly confirm any future launch. The pack has no launch authorization,
hardware attestation, runtime observation, or acceptance verdict. PR16
performed no cloud launch. It does not modify the reviewed A10 evidence or its
legacy verification path, the genuine Qwen2.5 canary, or any public evidence
schema.

`scripts/watch_lambda_a100_capacity.py` is the launch-free capacity bridge for
that pack. Its fixed API surface contains only `GET /instance-types` and, once
the exact PCIe offer has a capacity-bearing region, `GET /instances`. It emits
the API-resolved `instance_type.name` only after validating the exact provider
descriptions, one-GPU x86 shape, `$1.99/hour` rate, and zero-active-instance
boundary. A ready result still requires separate operator confirmation and is
neither hardware attestation nor launch authorization. See the
[campaign runbook](../../docs/QWEN3_CAMPAIGN_PROFILE.md#read-only-exact-capacity-watcher).

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
