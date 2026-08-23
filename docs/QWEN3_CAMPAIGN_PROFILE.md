# Qwen3-8B campaign profile

Inferdrome has a locally conformant execution profile for the first Qwen GPU
campaign model and one reviewed genuine A10 runtime observation. It is separate
from the historical Qwen2.5 A10 proof and does not alter that profile or its
evidence bytes. Other GPU assignments and cross-GPU conclusions remain
runtime-unproven.

## Frozen identity

```text
profile: managed-vllm-0.26-qwen3-8b-bf16-v1
model: Qwen/Qwen3-8B
model revision: b968826d9c46dd6066d109eabc6255188de91218
tokenizer revision: b968826d9c46dd6066d109eabc6255188de91218
producer candidate: vLLM 0.26.0
profile implementation state: LOCALLY_CONFORMANT_RUNTIME_UNPROVEN
reviewed external capability: A10_RUNTIME_OBSERVED
A100 execution pack: LOCALLY_CONFORMANT_RUNTIME_UNPROVEN
H100 execution pack: LOCALLY_CONFORMANT_RUNTIME_UNPROVEN
```

The generated profile's implementation-state field remains frozen at its
pre-execution value. Runtime truth is added through the separately hashed
reviewed receipt; captured evidence never rewrites the input contract that
preceded it.

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

## Reviewed A10 runtime observation

On 2026-08-21, producer commit
`058482df47377aaae6303015746f9a8e05d7e0f7` completed the bounded profile on one
Lambda `gpu_1x_a10`. Run `run-fcbd9a0a4031826ea601c5f14637e8dc` contains 96
measured requests, 96 successes, zero failures, and 96 native TTFT samples.
Independent recalculation produced p50/p95/p99 TTFT values of `127,123,958`,
`242,426,174`, and `244,030,050 ns`, plus `28.870215` output tokens/s.

The archive digest is
`sha256:27cdcc0192c5d6f05b5350e380b53caa158c249de28ed6880fe3d7971032172f`;
the bundle digest is
`sha256:48514166f6c284613052fedb0264ed83e2212156233d9f6103cb8946c0211ad6`.
The controller confirmed provider termination before publishing the separate
`VALID_AFTER_PROVIDER_TERMINATION` operational record. See the
[reviewed handoff](../evidence/gpu/2026-08-21-qwen3-8b-a10/README.md).

This closes only compatibility and fit for this exact A10/profile pair. It is
not provider hardware attestation, an acceptance verdict, a cross-GPU result,
or authority to silently change the frozen campaign.

## Zero-spend A100 execution pack

PR16 makes the A100 same-model hardware-control assignment executable without
claiming that it has executed. The generated
[`qwen3-8b-a100.json`](../campaigns/v1/execution-packs/qwen3-8b-a100.json)
reuses the exact Qwen3-8B model revision, profile, workload, sampling controls,
vLLM 0.26.0 producer candidate, and concurrency-one capability spike used by
the reviewed A10 observation. It does not introduce the separate Qwen3-14B
capability-ladder profile.

The target is exactly one A100 40 GB PCIe, reported by `nvidia-smi` as
`NVIDIA A100-PCIE-40GB`. `NVIDIA A100-SXM4-80GB`,
another A100 memory size or interconnect, and a generic `NVIDIA A100` runtime
observation all fail closed. Lambda's provider instance-type name is not
guessed or frozen as a static SKU: the operator must supply the exact
`instance_type_name` returned by the Lambda API, and the controller must match
that value exactly against the selected API instance before remote setup.
The machine literal follows NVIDIA's
[supported-GPU identifier table](https://github.com/NVIDIA/open-gpu-kernel-modules#compatible-gpus),
while Lambda's
[instance table](https://docs.lambda.ai/public-cloud/on-demand/#instance-types)
lists the one-GPU A100 PCIe 40 GB shape.

The dated planning rate is exactly `$1.99/hour` and the session cap is exactly
`$1.25`. Both values must match the runtime arguments and the Lambda API rate;
the cap remains a client-side termination boundary rather than a provider
billing guarantee. Exactly one non-terminal paid instance may exist, and the
remote preflight must observe the exact A100 model above on the selected GPU.

The execution pack deliberately records:

```text
capability_state: LOCALLY_CONFORMANT_RUNTIME_UNPROVEN
launch_authorization: EXPLICIT_OPERATOR_CONFIRMATION_REQUIRED
hardware_attestation: false
acceptance_verdict: null
```

Generation, schema validation, mutation tests, and a controller dry run prove
only code and local conformance. They do not prove model fit on A100, provider
hardware, runtime behavior, benchmark success, customer eligibility, or an
acceptance outcome. PR16 performed no cloud launch and produced no A100 runtime
receipt. The reviewed A10 archive, its immutable evidence bytes, and its legacy
verification path remain unchanged.

The zero-cost A100 preview requires a clean committed checkout and uses
placeholders for an instance that an operator might later choose. It builds and
validates the exact source payload but does not contact Lambda or SSH:

```bash
PYTHONPATH=src .venv/bin/python scripts/capture_real_gpu_over_ssh.py \
  ubuntu@203.0.113.10 \
  --dry-run \
  --expected-commit 0123456789abcdef0123456789abcdef01234567 \
  --identity-file /absolute/path/to/capture-only-id_ed25519 \
  --managed-capability-profile managed-vllm-0.26-qwen3-8b-bf16-v1 \
  --qwen3-gpu-tier a100-40gb-pcie \
  --lambda-instance-type-name '<exact Lambda API instance_type_name>' \
  --lambda-instance-id 0123456789abcdef0123456789abcdef \
  --lambda-hourly-rate-usd 1.99 \
  --max-cost-usd 1.25 \
  --lambda-billing-started-at 2026-08-21T20:00:00Z \
  --startup-timeout-seconds 300 \
  --remote-timeout-seconds 1300
```

The quoted instance-type placeholder must be replaced with the exact Lambda
API value before a real attempt. A successful preview is not launch
authorization. Removing `--dry-run` is a separate paid action that still
requires explicit operator confirmation and a fresh review of availability,
rate, endpoint, billing origin, and termination readiness.

### Read-only exact-capacity watcher

The A100 watcher closes the gap between a frozen execution pack and volatile
provider capacity. It has a separate, GET-only client whose entire network
allowlist is Lambda's `/instance-types` and `/instances` endpoints. It cannot
represent a launch, restart, update, or termination request. The provider API
key is accepted only through `LAMBDA_CLOUD_API_KEY` and never appears in output.

Load an existing key without placing it in shell history, then run one
observation:

```bash
read -r -s -p "Lambda API key: " LAMBDA_CLOUD_API_KEY
echo
export LAMBDA_CLOUD_API_KEY

PYTHONPATH=src .venv/bin/python scripts/watch_lambda_a100_capacity.py
unset LAMBDA_CLOUD_API_KEY
```

Or poll once per minute for at most one hour:

```bash
PYTHONPATH=src .venv/bin/python scripts/watch_lambda_a100_capacity.py \
  --watch \
  --poll-seconds 60 \
  --max-wait-seconds 3600
```

Each observation is one secret-free JSON line with a digest of the validated
catalog projection. The watcher requires the provider description
`1x A100 (40 GB PCIe)`, GPU description `A100 (40 GB PCIe)`, one GPU,
`x86_64`, the exact `$1.99/hour` rate, at least one capacity-bearing region,
and zero active instances. Available SXM, 80 GB, multi-GPU, ARM, or rate-drifted
offers never satisfy the target. It queries `/instances` only after the exact
PCIe offer has capacity and spaces those two API requests by more than one
second to respect the general request limit in Lambda's
[Cloud API documentation](https://docs.lambda.ai/api/cloud).

Exit status `0` means only `READY_FOR_OPERATOR_CONFIRMATION`. Exit status `3`
means unavailable or otherwise not launch-ready, and `2` means the observation
failed. Even the ready record says `instance_launch_performed: false`,
`hardware_attestation: false`, and
`launch_authorization: EXPLICIT_OPERATOR_CONFIRMATION_REQUIRED`. The emitted
provider `instance_type.name` and region are inputs to a later, separately
confirmed launch; the watcher never performs that launch.

## Zero-spend H100 execution pack

The generated
[`qwen3-8b-h100.json`](../campaigns/v1/execution-packs/qwen3-8b-h100.json)
extends the unchanged Qwen3-8B hardware-control contract to exactly one H100
80 GB PCIe. It deliberately does not implement the separate Qwen3-32B
capability-ladder assignment. Keeping the first H100 run on the same model,
revision, workload, profile, request order, and concurrency as A10 makes a
future hardware-control comparison methodologically valid.

The remote preflight requires the exact NVIDIA product literal
`NVIDIA H100 PCIe`. Generic H100, H100 NVL, and H100 SXM product names fail
closed. The provider preflight separately requires description
`1x H100 (80 GB PCIe)`, GPU description `H100 (80 GB PCIe)`, one `x86_64` GPU,
26 vCPUs, 200 GiB host memory, 1,024 GiB storage, and exactly `$3.29/hour`.
NVIDIA's
[supported-GPU table](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/README.md?plain=1)
and Lambda's
[instance table](https://docs.lambda.ai/public-cloud/on-demand/#instance-types)
are the primary identity sources; the provider API must still resolve the
volatile instance-type name and capacity-bearing region immediately before a
launch decision. Lambda's dated public table currently lists 225 GiB RAM for
this SKU, while its live launch catalog displayed 200 GiB on 2026-08-22. The
execution preflight binds the live catalog value and fails closed if it changes
again; the discrepancy is not silently normalized.

The `$2.25` session cap permits 2,462 billed seconds at the frozen rate. The
unchanged 2,078-second phase ledger therefore retains 384 seconds of outer
slack, including the 300-second termination-confirmation allocation. This is a
client-side safety boundary, not a provider billing guarantee.

The zero-cost H100 controller preview uses placeholders and contacts neither
Lambda nor SSH:

```bash
PYTHONPATH=src .venv/bin/python scripts/capture_real_gpu_over_ssh.py \
  ubuntu@203.0.113.10 \
  --dry-run \
  --expected-commit 0123456789abcdef0123456789abcdef01234567 \
  --identity-file /absolute/path/to/capture-only-id_ed25519 \
  --managed-capability-profile managed-vllm-0.26-qwen3-8b-bf16-v1 \
  --qwen3-gpu-tier h100-80gb-pcie \
  --lambda-instance-type-name '<exact Lambda API instance_type_name>' \
  --lambda-instance-id 0123456789abcdef0123456789abcdef \
  --lambda-hourly-rate-usd 3.29 \
  --max-cost-usd 2.25 \
  --lambda-billing-started-at 2026-08-23T20:00:00Z \
  --startup-timeout-seconds 300 \
  --remote-timeout-seconds 1300
```

The generic watcher is GET-only and supports the implemented A100 and H100
capacity targets:

```bash
PYTHONPATH=src .venv/bin/python scripts/watch_lambda_gpu_capacity.py \
  --gpu-tier h100-80gb-pcie
```

The catalog validator accepts strictly shaped CPU-only offers with `gpus: 0`
as non-target entries. They can never satisfy a GPU target; malformed, negative,
boolean, or oversized GPU counts still fail closed.

It emits `READY_FOR_OPERATOR_CONFIRMATION` only after exact metadata, rate,
capacity, and zero-active-instance checks pass. It has no launch endpoint and
cannot turn readiness into authorization. A real launch still requires a
separate explicit confirmation after reviewing the emitted instance type,
region, rate, image, SSH key, current instance count, cap, and termination
plan. Local conformance proves no H100 runtime behavior and creates no receipt.

## Zero-cost local checks

```bash
PYTHONPATH=src python3 scripts/generate_qwen3_launch_profile.py --check
PYTHONPATH=src python3 scripts/watch_lambda_a100_capacity.py --check
PYTHONPATH=src python3 scripts/watch_lambda_gpu_capacity.py \
  --gpu-tier h100-80gb-pcie --check
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

This command is not launch authorization. The first real use completed under a
bounded A10 capability spike, Lambda watchdog, exact live-rate check, explicit
operator confirmation, and provider-confirmed termination. Any reproduction or
new GPU assignment still requires a fresh bounded launch decision. The reviewed
runtime-capability record does not mutate the frozen profile.

## Bounded Qwen3 remote-capture controller

The explicit Qwen3 profile is now wired into the existing SSH transport and
Lambda termination guard. This code does not launch an instance. The operator
must still launch exactly one matching Lambda Stack 24.04 GPU instance,
explicitly confirm that launch in Lambda, record the provider billing-start
timestamp, and supply the exact instance ID, API instance-type name, SSH
destination, GPU tier, and capture-only private-key path.

The reviewed A10 path can still be previewed at zero cost. It requires a clean
committed checkout, builds and validates the exact source archive, but does not
contact SSH or Lambda:

```bash
PYTHONPATH=src .venv/bin/python scripts/capture_real_gpu_over_ssh.py \
  ubuntu@203.0.113.10 \
  --dry-run \
  --expected-commit 0123456789abcdef0123456789abcdef01234567 \
  --identity-file /absolute/path/to/capture-only-id_ed25519 \
  --managed-capability-profile managed-vllm-0.26-qwen3-8b-bf16-v1 \
  --qwen3-gpu-tier a10-24gb-pcie \
  --lambda-instance-type-name gpu_1x_a10 \
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

The tier-bound Qwen3 mode fails before remote setup unless all of these remain
true:

- the selected profile ID and GPU tier are exact;
- the Lambda API reports the selected tier's exact frozen rate: `$1.29/hour`
  for A10, `$1.99/hour` for A100, or `$3.29/hour` for H100;
- the session cap is exactly `$0.75` for A10, `$1.25` for A100, or `$2.25`
  for H100;
- the explicit instance ID resolves to the SSH endpoint;
- exactly one non-terminal paid Lambda instance exists and its API
  `instance_type_name` exactly matches the operator's runtime argument;
- the independent termination watchdog publishes readiness;
- the remote preflight observes exactly `NVIDIA A10` for A10,
  `NVIDIA A100-PCIE-40GB` for A100, or `NVIDIA H100 PCIe` for H100 at the
  selected physical GPU index;
- Python 3.12 development, `venv`/`ensurepip`, and at least 40 GiB free under
  `/tmp` are available before source upload;
- the source tree contains only regular Git blobs and no rejected secret-key
  names, links, submodules, private-key markers, or Git export transformations.

At the frozen A10 rate, `$0.75` permits 2,093 billed seconds. The watchdog
requests termination 300 seconds before that cost boundary, at 1,793 billed
seconds. For A100, `$1.25` at `$1.99/hour` permits 2,261 billed seconds and the
same 300-second margin requests termination by 1,961 billed seconds. For H100,
`$2.25` at `$3.29/hour` permits 2,462 billed seconds and the margin requests
termination by 2,162 billed seconds. The frozen
phase ledger allocates 2,078 seconds: 90 preflight, 90 source upload,
1,300 remote capture, 60 remote kill grace, 5 SSH-close grace, 30 metadata
transfer, 180 archive transfer, 23 controller handoff, and 300 termination
confirmation. That leaves 15 seconds of theoretical A10 ledger slack and 183
seconds of theoretical A100 ledger slack and 384 seconds of theoretical H100
ledger slack. The remote work is capped at 1,300
seconds and shortened further against the live termination deadline. Billing
time already elapsed before controller startup, watchdog setup, and the guarded
source rebuild is subtracted from that live window.

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
   exact selected GPU variant, upload the archive, verify its digest, and
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
exact GPU model, concurrency one, and model/tokenizer snapshot identities
matching the host-preparation receipt. Even then, the output is an
observation-only capability spike with no acceptance verdict and no hardware
attestation claim.

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

The reviewed receipt closes the A10 runtime-capability question for this exact
profile and producer commit. It does not authorize another paid launch, prove a
different GPU assignment, or establish a cross-GPU conclusion. The frozen
profile and captured bytes remain immutable. The A100 and H100 execution packs
remain runtime-unproven until each separately authorized, terminated,
retrieved, and independently verified capture produces its own receipt.
