# ADR 0010: Freeze a two-track cross-GPU capability campaign

- Status: Accepted
- Date: 2026-08-20
- Scope: post-v0.1 GPU campaign design

## Context

Inferdrome has one genuine A10 receipt for a pinned Qwen2.5 0.5B workload. That
receipt proves the managed producer path can measure and seal locally observed
GPU evidence, but it is intentionally a small historical canary. It is not a
modern model-capacity study and must not be rewritten to become one.

A new GPU campaign has two different questions:

1. what happens when one frozen model, producer, and workload are exercised on
   several GPU tiers; and
2. what distinct model capacity can each GPU tier practically attempt?

Those questions cannot share one interpretation. A same-model lane removes
model identity as an intended difference, but it does not by itself prove that
GPU hardware caused an observed difference. Cloud hosts also vary in CPU,
driver, topology, thermals, and other observed or unobserved conditions. The
current controlled-comparison v1 contract is narrower still: it supports only
`traffic.concurrency` as the reviewed treatment and requires equal observed
environments. It cannot be reused to certify a cross-hardware contrast.

A different-model ladder has an even stronger boundary. Latency or throughput
from Qwen3-8B, Qwen3-14B, Qwen3-32B, and an FP8 Qwen3.5 MoE checkpoint cannot be
ranked as if only GPU hardware changed.

The design must be frozen and fail closed before new producer profiles are
implemented or paid hosts are launched. It must also distinguish configured
model metadata from model bytes and runtime behavior later observed on a host.

## Decision

Inferdrome adds the operational contract
`inferdrome.gpu-campaign-plan.v1`. It is committed beneath `campaigns/v1`, with
a closed generated JSON Schema and mutation vectors. It is not added to the
public evidence-bundle schema registry and does not change any v1 evidence
contract.

The canonical plan has two tracks over four one-GPU Lambda targets:

| GPU tier | Hardware-control model | Capability-ladder model |
|---|---|---|
| A10 24 GB PCIe | Qwen3-8B BF16 | Qwen3-8B BF16 |
| A100 40 GB PCIe | Qwen3-8B BF16 | Qwen3-14B BF16 |
| H100 80 GB PCIe | Qwen3-8B BF16 | Qwen3-32B BF16 |
| B200 180 GB SXM6 | Qwen3-8B BF16 | Qwen3.5-122B-A10B official FP8 |

Every model and tokenizer uses an exact 40-character Hugging Face revision.
The Qwen3.5 profile is text-only and must disable the vision path. Every planned
assignment names vLLM 0.26.0 only as a candidate producer and remains
`UNPROVEN_REQUIRES_SPIKE`. Incompatibility requires a new reviewed runtime
profile and, if it changes this plan, a new campaign version. A floating branch,
nightly build, or silent fallback is forbidden.

The workload design freezes:

- the same ordered public, synthetic, non-sensitive UTF-8 prompt bytes across
  both tracks;
- Qwen3-8B as the token-length sizing reference, with non-reference token counts
  remaining observed rather than configured;
- 32 measured and four warmup requests in each 128, 512, and 1,024-token target
  bucket;
- 96 measured requests, 12 warmups, and 128 requested output tokens per run;
- concurrency 1, 4, and 16, with three repetitions per condition;
- non-thinking mode, temperature 0.7, top-p 0.8, top-k 20, min-p 0, seed 42,
  and ignored EOS; and
- fixed request ordering.

The plan freezes a USD 9.50 compute ceiling divided into per-tier session caps.
The prices are dated planning inputs, not future provider facts. Execution must
require an exact Lambda API rate match, one explicit operator confirmation per
launch, one paid instance at a time, a ready independent watchdog before remote
setup, termination in controller finalization, and provider-confirmed terminal
state. Filesystem spend requires separate approval.

The existing Qwen2.5 A10 bundle is embedded only as a historical canary anchor.
It is excluded from both tracks and retains a preserve-existing-bytes policy.

## Claim boundary

The campaign plan is a design artifact. It does not authorize a paid launch,
attest hardware or execution, prove model compatibility, establish chronology,
or make evidence customer-eligible.

The hardware-control track is descriptive until a future reviewed contract can
bind hardware as an intended treatment while disclosing host differences.
Cross-hardware ratios are therefore withheld by this version.

The capability ladder demonstrates separately verified serving attempts. It
cannot publish a cross-profile speed ranking because model identity, parameter
count, architecture, checkpoint precision, and potentially runtime capability
differ.

Only independently verified real bundles may populate generated campaign
metrics. Synthetic results are excluded. Inferdrome assigns no `PASS`, `FAIL`,
or `NOT_PROVEN` acceptance verdict.

## Consequences

- The model ladder and same-model control can be implemented without conflating
  their interpretations.
- Exact model revisions, workload shape, cost ceilings, and safety gates become
  reviewable before cloud spend.
- The public evidence schemas and the genuine Qwen2.5 receipt remain unchanged.
- Model fit, runtime compatibility, host observations, and benchmark results
  remain unavailable until later bounded capability spikes and real captures.
- Dashboard and README work must preserve two separate presentation classes and
  generate numbers only from verified evidence.
- A future cross-hardware comparison contract requires a separate ADR and
  implementation; this plan cannot smuggle that capability into comparison v1.

## Rejected alternatives

### Replace the existing canary with Qwen3 evidence

Rejected because historical evidence is immutable and records what actually
ran, not what later became preferable.

### Use only the different-model ladder

Rejected because it provides no common workload lane and invites unsupported
hardware-speed interpretations.

### Use only Qwen3-8B on every GPU

Rejected because it does not demonstrate the practical model-capacity frontier
that motivated the campaign.

### Treat a same-model workload as a completed controlled comparison

Rejected because controlled-comparison v1 does not support hardware as a
treatment and because cloud-host differences do not disappear when the model
is held constant.

### Pin `main`, `latest`, or an unbounded nightly runtime

Rejected because an unrecoverable producer identity defeats independent replay
and capability verification.

### Launch hosts while profiles are still being implemented

Rejected because setup and debugging time would be billable and an incomplete
profile could produce artifacts that look more authoritative than they are.
