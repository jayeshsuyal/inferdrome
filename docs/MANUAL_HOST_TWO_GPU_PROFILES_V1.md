# Selectable two-GPU manual-host profiles v1

This is one local, operator-attended preparation path for the existing
two-endpoint Qwen3-8B routing-evidence campaign. It selects one closed hardware
profile in the input; it does not provision a host, discover capacity, read
credentials, start Docker, pull or publish images, download a model, or claim
GPU evidence.

The existing A100 preparation packet and its v1/v2 schemas remain unchanged.
This document adds the H100 input v2 and routing-execution v3 profile; it does
not reinterpret prior A100 evidence.

## Closed profile selection

Generate an intentionally invalid template with unresolved values:

```sh
python -m inferdrome.deployment.manual_host template
python -m inferdrome.deployment.manual_host template \
  --profile lambda-manual-two-h100-sxm5-80gb-v1
```

The first command deliberately preserves the legacy A100 default. Fill a
template with exact operator-reviewed inputs, then use the unchanged shared
preparation command:

```sh
python -m inferdrome.deployment.manual_host prepare \
  --input operator-host.json --output prepared-manual-host
```

| Profile | Input / execution versions | Required shape |
| --- | --- | --- |
| `lambda-manual-two-a100-pcie-40gb-v1` | manual-host input v1; routing config/manifest v2 | one host, two A100 PCIe 40 GB physical GPUs |
| `lambda-manual-two-h100-sxm5-80gb-v1` | manual-host input v2; routing config/manifest v3 | one host, two H100 SXM5 80 GB physical GPUs |

Both profiles retain Qwen/Qwen3-8B and tokenizer revision
`b968826d9c46dd6066d109eabc6255188de91218`, vLLM 0.26.0, BF16,
`--max-model-len 2048`, TP1, one distinct UUID per private engine, a CPU-only
observer, the fixed three-policy/six-request trials (18 terminal receipts),
the stale-load/fresh-health fault, 5 ms observation admission, no retries, and
the existing sealed-package/offline-verifier flow.

## H100 acceptance boundary

The H100 profile's finite local preflight contract is the documented H100-SXM5
80 GB product, the `nvidia-smi` product label `NVIDIA H100 80GB HBM3`, exactly
81,559 MiB, two plan-bound distinct UUIDs, and `MIG` exactly `Disabled`.
NVIDIA documents H100-SXM5 as an 80 GB MIG-capable product and its MIG guide
uses that `nvidia-smi` product label; Lambda documents a two-H100-SXM 80 GB
single-host shape. See the [NVIDIA supported GPU table](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/supported-gpus.html)
and [Lambda on-demand instance table](https://docs.lambda.ai/public-cloud/on-demand/).

These are documented acceptance values and synthetic-test fixtures, **not** an
attestation, a capacity observation, a driver provenance record, or real GPU
evidence. A later host reporting another product string, memory size, UUID
population, MIG state, image/source label, Tensor Parallel value, allocation,
or private Compose plan fails closed. Its authorized execution must record the
actual runtime observations separately.

Each engine must receive exactly one configured UUID. The generated observer
has no `deploy` GPU reservation, `NVIDIA_VISIBLE_DEVICES=void`, and an empty
`CUDA_VISIBLE_DEVICES`; it remains a separate measurement runtime, not a
serving engine. The host preflight runtime-inspects the two engines, not the
ephemeral observer; observer isolation is enforced by the generated Compose
specification and executor boundary.

## Image and execution boundary

The profile code changes source-bound role labels. Before a future execution,
both fixed role images require separately authorized rebuild and publication
from the merged source; older source-bound digests cannot be substituted. Once
both updated images are available, choosing A100 versus H100 is a configuration
choice and does not require another image build solely to switch profile.

The source does not establish a private host's availability, price, immutable
host image/driver, Lambda account or credential, model presence, cleanup
success, or billing limit. A future separately authorized operation must bind
the exact instance ID and operator cleanup handoff, use private engine
endpoints, and read back termination by exact ID. No automatic fallback,
arbitrary GPU passthrough, cross-host claim, model change, SGLang support,
performance claim, or acceptance verdict is added.
