# Prospective contract-linked GPU capture

Status: **plumbing implemented; no prospective ExitSpec contracts supplied**

The historical real-GPU examples and publication records remain retrospective.
The prospective wrapper is a separate path and does not rewrite or relink
those files.

## Required external inputs

An ExitSpec owner must first freeze three contracts outside Inferdrome and give
the operator their exact SHA-256 digests. Inferdrome does not create, freeze,
interpret, or verdict those contracts. The operator must then provide three
source YAML files whose `links.exitspec_contract_digest` fields contain exactly
the supplied digests:

- `native-p95-under-20ms`;
- `native-p95-under-10ms`; and
- `semantic-first-nonempty-under-20ms`.

Each source must otherwise be the existing pinned Qwen2.5 0.5B methodology:
vLLM `0.26.0`, the pinned model and tokenizer revisions, local endpoint
`127.0.0.1:18080`, concurrency 4, 10 warmups, 100 measured requests, 32
requested output tokens, temperature 0, seed 42, and the checked-in workload
bytes. The source YAML bytes remain the original-spec artifact in every sealed
bundle.

The repository intentionally contains no runnable prospective source files or
ExitSpec digests. The historical sources are not valid substitutes because
their linkage is null and their captures are retrospective.

## Read-only preflight

The check path performs static asset, source resolution, methodology, and exact
digest checks only. It never prepares a host, contacts a provider, starts a
model, reserves a run workspace, or writes a receipt.

With future owner-supplied files and digests, use the following shape (the
angle-bracket values are required inputs, not placeholders that Inferdrome
will fill):

```bash
PYTHONPATH=src .venv/bin/python scripts/prospective_real_gpu_capture.py \
  --check \
  --case native-p95-under-20ms=<source-yaml-path> \
  --case native-p95-under-10ms=<source-yaml-path> \
  --case semantic-first-nonempty-under-20ms=<source-yaml-path> \
  --expected-contract-digest native-p95-under-20ms=<exact-external-digest> \
  --expected-contract-digest native-p95-under-10ms=<exact-external-digest> \
  --expected-contract-digest semantic-first-nonempty-under-20ms=<exact-external-digest>
```

Running `--check` without those inputs is deliberately inert and reports
`INERT_NO_CONTRACTS`. It is suitable for the GPU-free engineering gate.

## Capture and verification

Prepare the existing pinned host separately, then run the prospective wrapper
with the same explicit source/digest arguments. Preflight completes before the
prepared host is inspected for execution, and the core orchestrator receives
the expected digest again immediately before it can reserve a run workspace or
start vLLM:

```bash
PYTHONPATH=src .venv/bin/python scripts/prospective_real_gpu_capture.py \
  --case native-p95-under-20ms=<source-yaml-path> \
  --case native-p95-under-10ms=<source-yaml-path> \
  --case semantic-first-nonempty-under-20ms=<source-yaml-path> \
  --expected-contract-digest native-p95-under-20ms=<exact-external-digest> \
  --expected-contract-digest native-p95-under-10ms=<exact-external-digest> \
  --expected-contract-digest semantic-first-nonempty-under-20ms=<exact-external-digest> \
  --state-root /absolute/path/to/.inferdrome-gpu \
  --output-root /absolute/path/to/prospective-output
```

The session contains an additive capture manifest, handoff manifest,
publication-review metadata, and operator-visible receipt. Each records the
case's external contract digest, source-spec digest, execution fingerprint,
request-plan digest, bundle digest, and preserved original-spec digest. The
publication metadata remains `EXTERNAL_ONLY`, and all Inferdrome acceptance
verdict fields remain null.

The session can be checked later without provider, GPU, network, or model
mutation. Verification requires the same externally retained expected
digests:

```bash
PYTHONPATH=src .venv/bin/python scripts/prospective_real_gpu_capture.py \
  --verify /absolute/path/to/prospective-output/prospective-real-gpu-<session> \
  --expected-contract-digest native-p95-under-20ms=<exact-external-digest> \
  --expected-contract-digest native-p95-under-10ms=<exact-external-digest> \
  --expected-contract-digest semantic-first-nonempty-under-20ms=<exact-external-digest>
```

Cancellation and managed-vLLM cleanup use the existing `inferdrome run`
supervision path. No shell command is assembled from the source contents or
digest values; subprocess arguments remain a structured vector. No cloud/GPU
run or receipt is performed as part of repository validation.

## Prepared-host transport boundary

The controller can transport the complete ExitSpec P1 handoff directory as one
no-follow, byte-snapshotted archive. It creates a second, distinct exact-HEAD
Inferdrome source archive, pins both digests remotely, invokes the merged
prospective wrapper once per case, and retrieves a bounded immutable session
archive. The controller does not pool cases or emit an ExitSpec acceptance
verdict. A non-Lambda host is explicitly an operator-provided boundary: the
receipt says `OPERATOR_PROVIDED_HOST_NO_COST_TERMINATION_CLAIM`, and the
operator owns any provider cleanup.

The following is intentionally a placeholder operator command. Replace every
angle-bracket value with an independently retained input; the two observed
handoff digests are not Inferdrome production constants.

```bash
PYTHONPATH=src .venv/bin/python scripts/capture_real_gpu_over_ssh.py \
  --prospective \
  --handoff-root /absolute/path/to/ExitSpec/examples/inference-performance/inferdrome-p1 \
  --expected-handoff-manifest-sha256 <operator-observed-manifest-sha256> \
  --expected-workload-sha256 <operator-observed-workload-sha256> \
  --expected-commit <exact-clean-inferdrome-commit> \
  --identity-file /absolute/path/to/operator-ssh-key \
  --host-key-file /absolute/path/to/pinned-known-hosts \
  --host-key-sha256 <sha256-of-pinned-known-hosts-bytes> \
  --remote-state-root /absolute/path/to/prepared/.inferdrome-gpu \
  --output-root /absolute/path/to/prospective-retrieved \
  <operator-user>@<prepared-nvidia-host>
```

The guarded Lambda variant additionally requires the existing complete cost
guard inputs (`--lambda-instance-id`, `--lambda-instance-type-name`,
`--lambda-hourly-rate-usd`, `--max-cost-usd`, `--lambda-billing-started-at`,
and `--lambda-guard-state-root`). Its provider termination confirmation is
recorded before local semantic verification. `--dry-run` performs the local
snapshot, exact archive construction, and static currentness checks only; it
does not run `ssh-keyscan`, SSH, a provider API, or a GPU workload.
