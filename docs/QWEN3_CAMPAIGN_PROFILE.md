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

The generated
[`qwen3-8b-model-files.json`](../campaigns/v1/profiles/qwen3-8b-model-files.json)
freezes all 15 snapshot files, their individual byte sizes and SHA-256 digests,
and the 16,397,461,266-byte aggregate. The generated
[`qwen3-8b-host-dependencies.json`](../campaigns/v1/profiles/qwen3-8b-host-dependencies.json)
separately freezes the architecture-specific `tokenizers==0.22.1` wheels. The
campaign profile binds both manifest digests and the expected aggregate model
snapshot identity.

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
may support a separate reviewed runtime-capability record; this document does
not make that claim or mutate the frozen profile.

## Bounded A10 remote-capture controller

The explicit Qwen3 profile is now wired into the existing SSH transport and
Lambda termination guard. This code does not launch an instance. The operator
must still launch exactly one Lambda Stack 24.04 A10, explicitly confirm that
launch in Lambda, record the provider billing-start timestamp, and supply the
exact instance ID, SSH destination, and capture-only private-key path.

The zero-cost controller preview requires a clean committed checkout, builds
and validates the exact source archive, but does not contact SSH or Lambda:

```bash
PYTHONPATH=src .venv/bin/python scripts/capture_real_gpu_over_ssh.py \
  ubuntu@203.0.113.10 \
  --dry-run \
  --expected-commit 0123456789abcdef0123456789abcdef01234567 \
  --identity-file /absolute/path/to/capture-only-id_ed25519 \
  --managed-capability-profile managed-vllm-0.26-qwen3-8b-bf16-v1 \
  --lambda-instance-id 0123456789abcdef0123456789abcdef \
  --lambda-hourly-rate-usd 1.29 \
  --max-cost-usd 0.75 \
  --lambda-billing-started-at 2026-08-20T20:00:00Z \
  --startup-timeout-seconds 300 \
  --remote-timeout-seconds 1300
```

For the real capture, remove `--dry-run` and use the actual endpoint, instance
ID, and billing-start timestamp. Keep the API secret only in
`LAMBDA_CLOUD_API_KEY`; it is neither placed in SSH arguments nor copied to the
GPU host. The controller ignores the user's SSH configuration, agent, proxy,
forwarding, local-command, and environment-forwarding settings. Qwen3 mode
requires an explicit regular private-key file and sends only the exact committed
source tree—never `.git` or repository history.

The Qwen3 mode fails before remote setup unless all of these remain true:

- the selected profile ID is exact;
- the Lambda API reports the exact frozen A10 rate of `$1.29/hour`;
- the session cap is exactly `$0.75`;
- the explicit instance ID resolves to the SSH endpoint;
- exactly one non-terminal paid Lambda instance exists and its API type is
  exactly `gpu_1x_a10`;
- the independent termination watchdog publishes readiness;
- the remote preflight observes exactly `NVIDIA A10` at the selected physical
  GPU index;
- Python 3.12 development, `venv`/`ensurepip`, and at least 40 GiB free under
  `/tmp` are available before source upload;
- the source tree contains only regular Git blobs and no rejected secret-key
  names, links, submodules, private-key markers, or Git export transformations.

At the frozen rate, `$0.75` permits 2,093 billed seconds. The watchdog requests
termination 300 seconds before that cost boundary, at 1,793 billed seconds. The
frozen phase ledger allocates 2,078 seconds: 90 preflight, 90 source upload,
1,300 remote capture, 60 remote kill grace, 5 SSH-close grace, 30 metadata
transfer, 180 archive transfer, 23 controller handoff, and 300 termination
confirmation. That leaves 15 seconds of theoretical ledger slack. The remote
work is capped at 1,300 seconds and shortened further against the live
termination deadline. Billing time already elapsed before controller startup,
watchdog setup, and the guarded source rebuild is subtracted from that live
window.

This is a client-side fail-safe, not a provider-enforced spending limit or a
guarantee about the final invoice. Provider timing, billing granularity, API
availability, and termination latency remain outside Inferdrome's control. The
controller always attempts termination in `finally`, while the independent
watchdog remains the second termination path.

The lifecycle order is deliberate:

1. before launch, use `--dry-run` to verify the clean checkout and build the
   same size-bounded exact-HEAD source archive at zero cost;
2. for the live capture, arm the independent Lambda watchdog first, bind the
   exact instance/rate/endpoint, and reject any second active paid instance;
3. rebuild the checked source archive under watchdog protection, preflight the
   exact A10, upload the archive, verify its digest, and
   extract it through a traversal/link/device-safe reader;
4. run host preparation under a minimal `env -i` environment, install the
   checksum-pinned vLLM and architecture-specific `tokenizers==0.22.1` wheels,
   and download the exact 15-file Qwen snapshot at revision
   `b968826d9c46dd6066d109eabc6255188de91218`;
5. verify every model file's size and SHA-256, calculate one aggregate snapshot
   identity, and execute exactly one concurrency-1 capability spike;
6. fetch the bounded metadata first, then stream exactly the declared archive
   bytes while checking size and SHA-256, without extracting anything;
7. immediately request and confirm provider termination;
8. only after termination, safely extract the archive, recalculate the bundle,
   recheck all source/model/runtime/hardware bindings, and write a separate
   immutable offline-verification receipt.

Promotion succeeds only if the one bundle is customer-eligible and reports 96
measured requests, 96 successes, zero failures, and 96 observed TTFT samples
for each frozen TTFT reducer. It must also report one CUDA device, the selected
`NVIDIA A10`, concurrency one, and model/tokenizer snapshot identities matching
the host-preparation receipt. Even then, the output is an observation-only
capability spike with no acceptance verdict and no hardware attestation claim.

The source archive is limited to 128 MiB. The returned compressed capture is
limited to 256 MiB. Transfer metadata, file count, member type, member size,
duplicate path, traversal, link, device, and decompression limits all fail
closed.

Failure at any stage still enters controller finalization. A timed-out or failed
remote run is retained only as diagnostics and is never promoted to a Qwen3
capability capture. A checksum-valid archive is also not called verified until
the post-termination semantic pass succeeds.

If the local process dies after retrieval or termination, finalization can be
replayed without contacting Lambda. It requires the retained guard-armed and
termination receipts plus the same clean exact checkout:

```bash
PYTHONPATH=src .venv/bin/python scripts/capture_real_gpu_over_ssh.py \
  --resume-qwen3-finalization /absolute/path/to/retrieved-capture \
  --lambda-guard-state-directory /absolute/path/to/lambda-guard-state \
  --expected-commit 0123456789abcdef0123456789abcdef01234567
```

The replay is idempotent and refuses capture or cloud options. It cannot create
missing termination evidence or turn an incomplete capture into evidence.

This closes the local control-plane implementation only. It does not change
`LOCALLY_CONFORMANT_RUNTIME_UNPROVEN`, authorize a paid launch, or claim that
Qwen3-8B fits the A10. Only a later reviewed genuine receipt can change that
external capability status; the frozen profile and the bytes captured by this
commit must remain immutable.
