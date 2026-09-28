# Changing-hotspot vLLM router study (PR2)

This PR adds an executable synthetic routing study, not GPU findings. The live
router now has four policies: `round_robin`, router-observed `least_busy`,
`cache_only`, and `cache_plus_load`. The latter two hash the document before
`\n\nQuestion:\n` in the fixed one-user-message workload. Other chat shapes get no
affinity hint. Each hash retains only the last routing timestamp per replica in
a 4,096-key LRU, with a 60-second TTL. The cache-plus-load policy escapes to
the other replica when its preferred replica has at least two more active
requests. Ties follow a deterministic round-robin cursor. These are **estimated
affinity scores from prior routing**, not observed cache hits or evidence that
KV blocks remain resident. Actual KV-event integration is outside the first
three PRs.

The deterministic workload has three equal time epochs over a five-minute
offered window: document A hot, then B hot, then A hot. In each epoch, 70% of
offers use the current hot document, 20% the other shared document, and 10%
unique controls. The same seed produces identical scheduled arrivals, prompts,
classes, and tenant labels across policies. Calibration applies an independent
seed transform, so even an accidentally reused integer seed cannot reproduce
the evaluation trace. Prompts are regenerated from the frozen seed; plan and
result artifacts contain only SHA-256 trace/prompt digests and metadata, not
prompt or generation text. This content is reconstructed synthetic text, not a
production trace.

## Local commands

Prepare separate plans before calibration and before evaluation. The requested
input length below is a declaration until `verify-tokens` succeeds with the
pinned Qwen3 tokenizer files and `tokenizers==0.22.1`. Exploratory local
tokenization of 10,000 offers with the available 0.23.1 package yielded 277
rendered input tokens for every prompt; that check is not the pinned
certificate. A measured run refuses to start without a certificate matching
its plan and trace hashes. The certificate is a local integrity record, not
remote attestation of the serving model; PR3 must compare live usage and
runtime identity before making a GPU claim.

```sh
python -m inferdrome.vllm_router_study prepare \
  --phase calibration --seed 17 --offers 1200 \
  --expected-prompt-tokens 277 --output calibration-plan.json
python -m inferdrome.vllm_router_study prepare \
  --phase evaluation --seed 29 --offers 1200 \
  --expected-prompt-tokens 277 --output evaluation-plan.json
python -m inferdrome.vllm_router_study verify-tokens \
  --plan evaluation-plan.json --tokenizer-root /absolute/pinned/tokenizer \
  --output evaluation-token-certificate.json
python -m inferdrome.vllm_router_study run \
  --plan evaluation-plan.json --token-certificate evaluation-token-certificate.json \
  --router http://127.0.0.1:8090 --model Qwen/Qwen3-8B \
  --policy cache_plus_load --output evaluation-cache-plus-load.json
```

Start a **fresh router with a fresh ledger** for each policy condition. The
client checks its declared policy and zero prior offers before dispatch. At
completion it records the router's counters and requires offered, terminal,
and flushed ledger rows to equal the planned offer count, with no accounting
failure or in-flight requests. A failed post-run check remains visible in the
result as `router_accounting_valid: false` and invalidates that condition.
`comparison_valid` also fails if any successful stream reports a mismatched
input or output length. The client retains all request rows even when the
post-run stats endpoint is unavailable.
Client output files use exclusive creation, and the client refuses a router
whose ledger had prior bytes at startup; keep raw files and their hashes for
PR3. The router ledger records hashed document keys, affinity
scores, active-request counts at decision, selected replica, and route reason.

## Frozen measurement rules

- Each measured trace has a 300-second scheduled offer window, followed by at
  most 60 seconds of drain. Every offered request has one terminal client row;
  timeout, rejection, cancellation, protocol error, and length mismatch stay
  in the denominator. Goodput divides SLO-good requests by the fixed 300-second
  offered window, not drain time or a configured rate mislabeled as throughput.
- First-content and terminal latency start at **scheduled arrival**, including
  client queue, upload, router queue, replica work, and streaming. The parser
  timestamps complete SSE content frames. Response-header and first-body-byte
  timestamps are separate; neither is a wire-level first-byte measurement or
  exact token time. The maximum interval between content frames describes a
  streaming stall, not token cadence.
- Requests set `stream: true`, `stream_options.include_usage: true`,
  `ignore_eos: true`, `max_tokens: 128`, temperature zero, one choice, and
  Qwen3 thinking disabled. A completed request must report exactly the frozen
  277 input tokens and 128 completion tokens; missing or different usage is a
  length mismatch, not a comparable success. The configured context limit is
  2,048 tokens. The pinned tokenizer certificate verifies all rendered input
  lengths before measured dispatch; runtime usage checks the engine's count.
  The pinned [vLLM 0.26.0 chat request schema](https://github.com/vllm-project/vllm/blob/v0.26.0/vllm/entrypoints/openai/chat_completion/protocol.py)
  admits `ignore_eos`; calibration still has to confirm Qwen3 actually emits
  the requested count under this configuration.
- The primary all-offered SLO uses first content within 500 ms and terminal
  completion within 5,000 ms. Reports also retain all-offered outcomes and
  p95 latency/stall summaries by epoch, traffic class, and tenant. No missing
  request is dropped to improve a percentile. The plan hash freezes seed,
  trace, token settings, affinity parameters, concurrency, deadline, and SLOs.

PR3 must freeze a separate calibration trace and selection rule, replica
reset/warmup procedure, GPU/model/image pins, policy condition order, repeat
count, quote, and spending ceiling **before** measured evaluation. The initial
target is two full replicas, one per GPU with TP=1, five-minute windows and at
least three independent balanced blocks; three blocks alone do not establish
statistical sufficiency. PR2's fake-replica tests demonstrate that an overload
escape executes and that every offered request is accounted for. They are
functional fixtures and make no claim of lower GPU latency or higher goodput.

```sh
PYTHONPATH=src python -m pytest -q \
  tests/integration/test_vllm_router_hotspot.py
```

The prior one-A100 cache sweep is separate short-burst evidence. Its configured
arrival rate is not achieved throughput, and its actual generated output
lengths varied; no result from that sweep is relabeled as a router comparison.
