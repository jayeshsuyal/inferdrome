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

One outer, pinned runtime image contains either the pinned vLLM runtime or the
pinned SGLang runtime and a preloaded Qwen3-8B snapshot. A direct invocation
starts exactly two fresh serving processes per trial:

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

Before a direct engine is allowed to start, create an exact
`inferdrome.load-calibration-vast-container-authorization.v1` packet. It binds:

- the source commit, protocol and all candidate-recipe digests;
- `Qwen/Qwen3-8B`, its pinned revision and preloaded snapshot digest;
- the exact pinned vLLM or SGLang outer `repository@sha256` reference;
- a locally observed direct executable metadata digest;
- a unique ownership alias, bounded session deadline and external termination
  guardian handoff; and
- hashes of the selected snapshot path and initially empty output directory.

`outer_image_state=DECLARED_BY_OPERATOR_UNVERIFIED` is intentional. The local
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
  --outer-image-reference 'vllm/vllm-openai@sha256:ffb2d59b1c059a5bd8d781320c9f5189de8293693b7d95da54befddaa54abf52' \
  --runtime-executable /usr/local/bin/vllm \
  --output /private/outputs/vast-container-preflight.json
```

Use the exact pinned reference from the versioned source contract. The
preflight fails closed if the image string is not the pinned source reference,
the executable is not an absolute regular
executable, recipes do not compile, the two origins differ from the declared
study pair, or a native SGLang binding is not exact.

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

The output directory must be private and empty. The producer writes the
existing canonical request-level study artifacts plus no-replace direct-process
session, attempt-plan, process-receipt and outcome sidecars. Receipts retain
hashes and bounded state only—not arguments, local paths, prompts, outputs,
tokens, provider payloads or credentials.

## What this does not prove

- It does not verify availability, price, host identity, GPU model/count, outer
  OCI image provenance, snapshot immutability after preflight, or provider
  termination.
- It does not expose an inference endpoint beyond loopback, add a scheduler,
  platform control plane, database, queue, registry, bucket or Kubernetes.
- It does not make Inferdrome a production router, a benchmark leaderboard, or
  a PASS/FAIL/NOT_PROVEN authority.

The next safe gate before any real operation is an explicitly approved,
read-only provider/capacity/identity check. A separate exact authorization must
cover any actual rental, registry pull, model acquisition, GPU use or spend.
