# Load-calibrated routing on an ordinary Vast container

This is a manual, source-only operator path for the existing load-calibrated
routing rehearsal. It is intended for an already-rented ordinary Linux
container with exactly two usable NVIDIA A100 devices where nested Docker is
unavailable or inappropriate. It does not rent, configure, discover, publish,
or destroy anything at Vast (or any other provider).

The existing `load-calibration-host-*` commands remain the Docker-on-host path.
They are not changed by this document. The direct-process path is separate:
`load-calibration-vast-container-*` starts two exact-owned local process groups
inside the already-present outer container. It is not a VM or Docker
replacement and it does not establish host-failure independence.

## The bounded topology

For the vLLM path, the provider launch image is the public Vast stock image
`vastai/vllm:v0.26.0-cuda-12.9`, pinned for `linux/amd64` to
`vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77`.
Its public OCI config digest is
`sha256:6f07455f6e17001ad04d1ddc7f8331cc29167e748269bfcf6ecb4d23ba24bc0d`.
That config declares Vast's `/opt/instance-tools/bin/entrypoint.sh`, vLLM
`0.26.0`, CUDA `12.9.1`, CUDART `12.9.79-1`, upstream image tag
`vllm/vllm-openai:v0.26.0-cu129`, and vLLM build commit
`ffd46bfab2128bb84146050e98b51a617c6575ab`. The provider entrypoint owns SSH,
portal and supervision startup. Inferdrome does not replace it, build an image,
install a runtime, or inject a second init system.

The exact Python patch is not declared by the public OCI config. The host
receipt therefore requires an observed `3.12.x` patch value instead of
inventing one. The historical raw upstream vLLM digest remains provenance for
older v1 records only; it is not the provider launch image for this path.

After the stock entrypoint is ready and an operator has separately placed the
verified Qwen3-8B snapshot, a direct invocation starts exactly two fresh
serving processes per trial:

```text
ordinary outer container, already supplied by the operator
  ├─ engine A: CUDA_VISIBLE_DEVICES=0 → http://127.0.0.1:<A>
  └─ engine B: CUDA_VISIBLE_DEVICES=1 → http://127.0.0.1:<B>
```

The study coordinator connects only to those literal loopback origins. Every
trial begins only after both declared GPUs are idle and both declared ports are
closed. It verifies both engines' `/health` and `/metrics` endpoints, performs
a fixed no-retention warmup, then dispatches the existing study. Cleanup sends
TERM and, after a bounded wait, KILL only to the two new process groups the
operator started. It never scans or kills unrelated processes.

Fresh process replacement, not an invented cache flush, is the cold-reset
mechanism. The existing protocol, candidate recipes, rates, policies, fixed
trace, request accounting, selection and report formats remain unchanged.

## Required operator packet

Before a stock vLLM engine is allowed to start, create an exact
`inferdrome.load-calibration-vast-container-authorization.v2` packet. It binds:

- the source commit, protocol and all candidate-recipe digests;
- `Qwen/Qwen3-8B`, its pinned revision and preloaded snapshot digest;
- the exact stock Vast vLLM manifest and config identities above;
- a locally observed direct executable metadata digest;
- a five-minute-or-shorter host receipt for the exact Python patch and two
  distinct observed devices of one authorized model: either
  `NVIDIA A100-PCIE-40GB` or `NVIDIA A100-SXM4-40GB`; mixed variants, 80 GB
  variants, and other accelerator names are rejected. The receipt also binds
  idle state, closed
  loopback ports, source/model/path identities, and absence of runtime install
  or image build activity;
- provider/account/location aliases, GPU type/count, maximum runtime, USD cap,
  cleanup deadline/guardian and evidence-destination digest; and
- a unique ownership alias, bounded session deadline and external termination
  guardian handoff; and
- hashes of the selected snapshot path and initially empty output directory.

The previous v1 packet remains parseable as a historical record but cannot
authorize a vLLM launch. `outer_image_state=DECLARED_BY_OPERATOR_UNVERIFIED`
is intentional. The local
executable metadata proves only a file observed in the current container; it
does not prove which OCI image started that container. The outer launch image,
two-A100 allocation, rental identity, external guardian and final destruction
must be checked by the human/operator through separately authorized provider
procedures. A completed local rehearsal is still `evidence_eligible=false` and
is not acceptance or production-routing proof.

## Offline preflight

This command reads protocol/recipe files and local executable metadata only. It
does not initialize a provider client, contact a registry, download a model,
start a process, access a GPU, or invoke Docker:

```bash
python -m inferdrome.evaluation.cli load-calibration-vast-container-preflight \
  --protocol /private/inputs/protocol.json \
  --recipe /private/inputs/load-low.json \
  --recipe /private/inputs/load-high.json \
  --runtime vllm \
  --outer-image-reference 'vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77' \
  --runtime-executable /usr/local/bin/vllm \
  --output /private/outputs/vast-container-preflight.json
```

Use the exact pinned provider image from the versioned source contract. The
preflight fails closed if the image string is mutable or is not that exact
stock manifest,
the executable is not an absolute regular
executable, recipes do not compile, the two origins differ from the declared
study pair, a native SGLang binding is not exact, or the protocol cannot fit
the concrete lifecycle used by execution. With the conservative 300-second
real-GPU startup allowance, that lifecycle requires at least 420 seconds of
prepare/reset reserve and 240 seconds of cleanup reserve for every trial.
Those values are computed by one shared source function used by both preflight
and the live lifecycle; preflight does not construct a process or touch a GPU.

The failed 16-trial pilot declared 120 seconds for prepare/reset and 30 seconds
for cleanup. Correcting only those reservations adds 510 seconds per trial:
the exact 3,562-second envelope becomes 11,722 seconds (3h15m22s), before any
separately approved provider overhead. That cannot fit the original
3,562-second runtime and USD 3.75 approval. Preserving the full two-level,
four-policy calibration plus healthy/stale confirmation matrix therefore
requires a new runtime/quote/cap authorization. Reducing trials, retaining
engines across trials, or lowering safety timeouts changes the methodology or
cold-reset/safety boundary and is not an automatic retry option.

For SGLang, add exactly two `--sglang-profile endpoint-id=/absolute/profile.json`
arguments and pass the absolute Python executable that will invoke the pinned
`sglang.launch_server` module. SGLang artifacts are rehashed immediately before
each fresh pair is started; no downloads are attempted.

## Explicit execution boundary

Only after a separate human approval packet and external termination backstop
exist may the operator use the `...-run` command with its exact literal
confirmation. That command is intentionally omitted here as a copy/paste
production recipe: it invokes two GPU serving processes. It never creates a
provider resource, reads provider credentials, uses a Docker socket or pulls an
image. It fails closed if the executable identity changed after preflight, the
approval/deadline/path hashes differ, GPU/port readback fails, one engine fails
to start, both engines are not ready before the fixed deadline, or cleanup
cannot be confirmed.

Immediately before dispatch, the v2 authorization must still be live and the
stock-host receipt must still be within its bounded validity interval. The
receipt, executable identity, model/path hashes, source commit, image/config,
two loopback origins and provider guardian are all re-bound before any engine
lifecycle is constructed. GPU idleness and closed ports are then observed
again by the existing lifecycle before each pair is spawned.

The output directory must be private and empty. The producer writes the
existing canonical request-level study artifacts plus no-replace direct-process
session, attempt-plan, process-receipt and outcome sidecars. Receipts retain
hashes and bounded state only—not arguments, local paths, prompts, outputs,
tokens, provider payloads or credentials.

## Export and independent retrieval verification

After the direct session has stopped, export and copy the local output to an
operator-controlled destination while the host still exists. The existing
bounded exporter recognizes the direct-process sidecars alongside the
canonical study package:

```bash
PYTHONPATH=src python -m inferdrome.evaluation.cli \
  load-calibration-host-export \
  --output-root /private/outputs/vast-container-rehearsal \
  --archive /private/retrieval/vast-container-rehearsal.tar
```

Record the producer-printed archive digest out of band. At the receiving
location, verify the retrieved bytes before inspecting them and before the
operator destroys the exact rented instance:

```bash
PYTHONPATH=src python -m inferdrome.evaluation.cli \
  load-calibration-host-verify-export \
  --archive /private/retrieval/vast-container-rehearsal.tar \
  --expected-archive-sha256 'sha256:<producer-export-digest>'
```

The exporter is no-replace and reads only the fixed regular-file grammar using
no-follow descriptors, bounded file/count/total-byte limits, and a canonical
inventory. It rejects a mixed Docker/direct operator output or inconsistent
direct authorization identities. `COMPLETE` means only that the local direct
rehearsal recorded completion; it does not claim runtime verification, evidence
eligibility, provider termination, host absence, or campaign success.

After retrieval verification, the operator must perform the separate exact
provider-resource destruction and absence readback procedure. Inferdrome
neither performs nor verifies that obligation. If retrieval fails or cannot
finish before the approved cleanup deadline, that deadline takes precedence:
do not silently extend the rental. Retain any already retrieved partial package
and report the remaining loss or unconfirmed state through the external
operator record.

## Authoring SGLang serving profiles

`--runtime sglang` needs a serving profile per endpoint, and it fails closed
against the study contract long before any GPU is touched. Author these offline
and run the preflight (above) until it passes; every requirement below is a
`prepare_vast_container_rehearsal`/`build_sglang_engine_binding` guard, not a
runtime check, so a rented box is never required to get them right.

### Pass the profiles as an endpoint mapping, not bare paths

`--sglang-profile` is repeatable and expects `endpoint-<x>=<path>`, exactly two
entries:

```bash
python -m inferdrome.evaluation.cli load-calibration-vast-container-preflight \
  --protocol   /private/inputs/protocol.json \
  --recipe     /private/inputs/load-low.json \
  --recipe     /private/inputs/load-high.json \
  --runtime    sglang \
  --sglang-profile endpoint-a=/private/inputs/sglang-endpoint-a.json \
  --sglang-profile endpoint-b=/private/inputs/sglang-endpoint-b.json \
  --outer-image-reference 'lmsysorg/sglang@sha256:<pinned digest from source>' \
  --runtime-executable /usr/local/bin/python \
  --output /private/outputs/vast-container-preflight-sglang.json
```

A bare path (`--sglang-profile /path.json`) is rejected as
`SGLang profile mapping is invalid`.

### The study must be authored for SGLang, not reused from vLLM

This is the non-obvious gate. The engine binding compares each profile against
the compiled study's `preparation`, and a vLLM-shaped recipe leaves the SGLang
serving state `UNKNOWN`, which can never match. Each recipe's `preparation`
must declare:

- `cache_state: "DECLARED_COLD"`;
- `prefix_caching: "DECLARED_ENABLED"` or `"DECLARED_DISABLED"`.

A recipe that omits these produces `engine binding violates its study/profile
contract` at preflight even when the profiles themselves are well-formed.

### Each `SglangServingConfig` must match the study exactly

For the pair to bind, both endpoint profiles must be identical except for
`origin`, and each must satisfy:

- `served_model_name` equals the study model (`Qwen/Qwen3-8B`);
- `origin` equals that endpoint's declared study origin (endpoint-a → the first
  study endpoint, endpoint-b → the second);
- `context_length` is strictly greater than the study's `max_tokens`;
- `prefix_cache` is `RADIX_ENABLED` when the recipe declares
  `prefix_caching: DECLARED_ENABLED`, otherwise `RADIX_DISABLED`;
- `model_revision`, `tokenizer_revision`, `model_snapshot_sha256`,
  `tokenizer_snapshot_sha256` and `chat_template_sha256` are the real digests of
  the preloaded Qwen3-8B snapshot (identity is bound into the sealed evidence);
- the study `preparation.serving_image_reference` is either absent or the pinned
  SGLang image digest (the `sha256:...` portion of the outer image reference).

A passing SGLang preflight writes the same non-executing
`inferdrome.load-calibration-vast-container-preflight.v1` packet the vLLM path
does, with `provider_action_performed=false`, `evidence_eligible=false` and the
runtime left `UNVERIFIED`. It confirms only that the inputs compile and bind; it
still proves nothing about a real server, GPU, or model.

## What this does not prove

- It does not verify availability, price, provider-reported host identity,
  outer OCI image provenance, snapshot immutability after preflight, or provider
  termination.
- It does not expose an inference endpoint beyond loopback, add a scheduler,
  platform control plane, database, queue, registry, bucket or Kubernetes.
- It does not make Inferdrome a production router, a benchmark leaderboard, or
  a PASS/FAIL/NOT_PROVEN authority.

Public image facts above come from the anonymous Docker Hub tag/manifest/config
endpoints ([`vastai/vllm` tags](https://hub.docker.com/r/vastai/vllm/tags))
and Vast's public
[`vast-ai/base-image`](https://github.com/vast-ai/base-image) documentation.
They do not
prove what a future rental actually runs. The next safe gate before any real
operation is an explicitly approved, read-only provider/capacity/identity
check. A separate exact authorization must supply the exact commit,
provider/account/location, GPU type/count, image/model/runtime identities,
maximum runtime, USD cap, cleanup deadline/watchdog, and evidence destination.
