# Qwen3-8B campaign profile

Inferdrome now has a locally conformant, runtime-unproven execution profile for
the first Qwen GPU campaign model. It is separate from the historical Qwen2.5
A10 proof and does not alter that profile or its evidence bytes.

## Frozen identity

```text
profile: managed-vllm-0.26-qwen3-8b-bf16-v1
model: Qwen/Qwen3-8B
model revision: b968826d9c46dd6066d109eabc6255188de91218
tokenizer revision: b968826d9c46dd6066d109eabc6255188de91218
producer candidate: vLLM 0.26.0
state: LOCALLY_CONFORMANT_RUNTIME_UNPROVEN
```

The profile freezes BF16, a 2,048-token model limit, 0.90 GPU memory
utilization, tensor parallel size one, 128 requested output tokens,
temperature 0.7, top-p 0.8, top-k 20, min-p 0, seed 42, ignored EOS, and
non-thinking Qwen chat-template behavior.

The benchmark's `--seed 42` controls its own deterministic machinery. The same
value is also injected as `seed: 42` in the exact OpenAI request body, so the
profile does not mislabel a benchmark-only seed as model sampling state.

## Frozen workload

The exact JSONL contains 96 unique, ordered public synthetic prompts. Under the
exact tokenizer and non-thinking one-user-message rendering, sequence indexes
0-31 contain 128 input tokens, 32-63 contain 512, and 64-95 contain 1,024.

The workload manifest binds every prompt digest, both tokenizer-file digests,
raw and rendered token counts, and exact workload bytes. Managed execution now
rejects before server startup unless `tokenizer.json` and
`tokenizer_config.json` are independent regular files with those exact hashes.
That verification is repeated while building the invocation and recorded in
its campaign-profile binding.

The standalone verifier additionally reproduces every token count with exactly
`tokenizers==0.22.1`; it rejects another package version, links, path swaps, and
unbounded files:

```bash
PYTHONPATH=src python3 scripts/verify_qwen3_workload_tokenization.py \
  /absolute/path/to/Qwen3-8B-snapshot
```

Pinned vLLM repeats sequence index zero for all twelve warmups. Warmups are
excluded from the measured population. ADR 0011 records why the earlier
per-bucket warmup assumption was superseded before execution.

Before those warmups, vLLM may retry the same sequence-zero request until one
request succeeds, bounded by its five-second ready-check timeout. That traffic
is also explicitly excluded from measurement; the profile does not claim that
the readiness phase always requires exactly one attempt.

Profile and manifest digests are SHA-256 over RFC 8785 canonical JSON bytes;
the workload digest is SHA-256 over the exact UTF-8 JSONL bytes. Those policies
are embedded in the generated profile so independent consumers do not have to
infer whitespace or serialization rules.

## Zero-cost local checks

```bash
PYTHONPATH=src python3 scripts/generate_qwen3_launch_profile.py --check
PYTHONPATH=src python3 -m inferdrome validate \
  campaigns/v1/qwen3-8b-concurrency-1.yaml
PYTHONPATH=src python3 -m inferdrome run \
  campaigns/v1/qwen3-workload-synthetic-smoke.yaml \
  --runs-root /tmp/inferdrome-qwen3-smoke
```

The synthetic run proves resolver, workload, request-plan, reducer, bundle, and
offline-verification wiring. It remains `SYNTHETIC_ONLY` and cannot prove GPU
capability.

## Real managed invocation

Once a clean Linux NVIDIA host contains the exact snapshot and pinned vLLM
environment, the managed CLI selection is explicit:

```bash
python3 -m inferdrome run campaigns/v1/qwen3-8b-concurrency-1.yaml \
  --runs-root /absolute/path/to/runs \
  --tokenizer-path /absolute/path/to/Qwen3-8B-snapshot \
  --managed-local-vllm \
  --managed-model-path /absolute/path/to/Qwen3-8B-snapshot \
  --managed-gpu-index 0 \
  --managed-capability-profile managed-vllm-0.26-qwen3-8b-bf16-v1
```

The stored invocation binds the generated profile digest and workload digest.
Missing profile evidence, altered sampling arguments, thinking-mode drift,
request-seed drift, tokenizer-file drift, model drift, workload drift, or
server-argument drift rejects offline.
The named campaign source also rejects before server launch if the explicit
`--managed-capability-profile` selection is omitted, preventing silent fallback
to the historical Qwen2.5 server controls.

This command is not launch authorization. The first real use must be a bounded
A10 capability spike under the Lambda watchdog, exact live-rate check, explicit
operator confirmation, and provider-confirmed termination. A successful spike
may upgrade runtime compatibility in a later reviewed commit; this document
does not do so.
