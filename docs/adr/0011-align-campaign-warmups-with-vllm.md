# ADR 0011: Align campaign warmups with pinned vLLM semantics

- Status: Accepted
- Date: 2026-08-20
- Scope: Qwen GPU campaign execution profile
- Supersedes: ADR 0010 only for per-bucket warmup allocation

## Context

ADR 0010 assigned four warmup requests to each of the 128, 512, and 1,024
token buckets. Implementation review against the exact upstream vLLM 0.26.0
producer showed that the stock benchmark cannot make that statement.

In `vllm/benchmarks/serve.py` at tag `v0.26.0`, `benchmark()` builds one
`test_input` from `input_requests[0]`. The initial readiness request and every
request created by `--num-warmups` reuse that same input. Warmups do not rotate
through the sampled dataset. The custom dataset loader separately preserves
input order under `--disable-shuffle`.

The readiness phase may retry that first input until one request succeeds, but
only within the producer's five-second ready-check timeout. It happens before
the twelve explicit warmup requests and before the measured request loop.

No campaign GPU execution or evidence capture occurred under the earlier
warmup assumption. The mismatch was found while implementing the local profile,
before paid execution.

The same review established another boundary. Inferdrome uses
`--skip-chat-template`, so the benchmark calculates a raw prompt length while
the OpenAI-compatible server applies the Qwen chat template. Server usage is
the source of the eventual observed input-token count. The workload therefore
needs to bind both raw prompt bytes and the exact non-thinking rendered form.

## Decision

The measured population remains unchanged: 96 ordered prompts, with 32 each at
128, 512, and 1,024 rendered input tokens under the exact Qwen3-8B tokenizer.

The warmup allocation is replaced with the producer's executable semantics:

```text
strategy: vllm_first_measured_request_repeated_v0_26
sequence_index: 0
requests: 12
population: EXCLUDED_FROM_MEASUREMENTS
```

The preceding readiness traffic is frozen separately:

```text
policy: vllm_first_measured_request_until_success_bounded_v0_26
timeout_seconds: 5
population: EXCLUDED_FROM_MEASUREMENTS
```

The first measured prompt is the 128-token sentinel. vLLM repeats that exact
request for the bounded readiness check and twelve explicit warmups before the
measured loop. Inferdrome does not claim exactly one readiness attempt and does
not describe those requests as four warmups per bucket.

The generated workload manifest binds:

- all 96 ordered prompt digests;
- exact workload bytes and SHA-256;
- the Qwen3-8B model and tokenizer revision;
- exact `tokenizer.json` and `tokenizer_config.json` SHA-256 values;
- raw and non-thinking rendered token counts per prompt; and
- the one-user-message rendering prefix and suffix.

The Qwen3-8B producer profile is opt-in and invocation-bound. It freezes BF16,
2,048 maximum model length, 0.90 GPU memory utilization, tensor parallel size
one, top-p 0.8, top-k 20, min-p 0, temperature 0.7, seed 42, ignored EOS, and
`chat_template_kwargs.enable_thinking=false`. Its state remains
`LOCALLY_CONFORMANT_RUNTIME_UNPROVEN` until a bounded real-GPU capability spike
succeeds.

The request body explicitly carries both
`chat_template_kwargs.enable_thinking=false` and `seed=42`; vLLM's benchmark
`--seed` alone is not treated as a model-sampling seed. Before managed server
startup, Inferdrome also verifies the exact `tokenizer.json` and
`tokenizer_config.json` hashes with bounded no-follow reads. The same result is
bound into invocation evidence, and the standalone token-count verifier
requires exactly `tokenizers==0.22.1`.

## Consequences

- The campaign plan now states behavior the pinned producer can actually
  execute and independently replay.
- Measured bucket balance and request order are preserved.
- Readiness and warmup requests remain stabilization traffic and cannot enter
  measured metrics.
- The historical Qwen2.5 profile and evidence bytes are unchanged.
- Runtime compatibility, A10 memory fit, and observed token counts remain
  unproven until the bounded spike.

## Rejected alternatives

### Keep the per-bucket wording as an intended approximation

Rejected because producer behavior would contradict the frozen method.

### Patch or fork vLLM only to rotate warmup prompts

Rejected for the first campaign because it would create a new producer identity
before the upstream path has been proven.

### Run three hidden benchmark commands before the measured command

Rejected because those extra commands would not fit the current one-invocation
evidence contract and would make the captured lifecycle ambiguous.
