# vLLM 0.26.0 Compose boundary

Status: **implemented as a static/local orchestration contract; Docker and
GPU execution remain unpassed in this environment**

This slice keeps Inferdrome and the serving engine in separate runtime
boundaries:

```text
vllm-engine                 vllm serve Qwen/Qwen3-8B
        │ internal network: http://vllm-engine.internal:8000
vllm-benchmark-runner       inferdrome run → vllm bench serve
```

The benchmark runner image contains the pinned vLLM client package and the
canonical `inferdrome` CLI. Its entrypoint is `inferdrome`; it never launches
`vllm serve`. The separate runtime image keeps the upstream `vllm serve`
entrypoint. The existing vLLM adapter therefore builds the same no-shell
`vllm bench serve` argument vector inside the runner container, targeting the
engine service's private DNS alias. The synthetic `inferdrome-runner-probe`
is used only by the default mock smoke and is never a substitute for this
benchmark path.

## Immutable runtime identity

The serving image is the official `vllm/vllm-openai` image for vLLM `0.26.0`,
referenced by its immutable multi-platform manifest digest:

```text
vllm/vllm-openai@sha256:ffb2d59b1c059a5bd8d781320c9f5189de8293693b7d95da54befddaa54abf52
```

The source release is tag `v0.26.0`, commit
`568afb3a13806beb53bb2e6bd518269357b237c0`, under the upstream Apache-2.0
license. The primary provenance references are the [vLLM 0.26.0 release](https://github.com/vllm-project/vllm/releases/tag/v0.26.0),
the [upstream Docker documentation](https://docs.vllm.ai/en/latest/deployment/docker/),
and the [upstream license](https://github.com/vllm-project/vllm/blob/v0.26.0/LICENSE).
The repository's exact contract is
[`compose/vllm-runtime-contract.json`](../compose/vllm-runtime-contract.json).

The canonical target is `linux/amd64`, matching the frozen Qwen3-8B BF16
capability profile. Its model and tokenizer revision is
`b968826d9c46dd6066d109eabc6255188de91218`; the profile is
`managed-vllm-0.26-qwen3-8b-bf16-v1`. These values are imported from the
existing campaign source of truth, not redefined by Compose.
The same source also supplies model-manifest digest
`sha256:ef291a8dd0f21604c8da3025f5112bd6641e891a3401c8550584725eaabe55cc`
and expected snapshot digest
`sha256:588d19e9e489cccdad793718d8c5efbad0738be717369f9eacb94ce514992d2c`.
The GPU preflight requires the snapshot's bounded file set; full model-byte
verification remains the existing host-preparation/capability gate.

## Default synthetic mock

The default Compose services build a pinned small Python mock engine and the
accepted PR5 runner image. The engine has no model, GPU, credential, host port,
or external network. Its responses and health endpoint are bounded and marked
synthetic. The probe output is always `synthetic_only: true` and
`evidence_eligible: false`.

Use the orchestration wrapper so the one-shot probe drives deterministic stack
cleanup and the bind-mounted output remains on the host:

```bash
mkdir -p .inferdrome-compose/evidence
INFERDROME_COMPOSE_EVIDENCE_DIR="$PWD/.inferdrome-compose/evidence" \
  scripts/run_vllm_compose.sh mock
```

The equivalent structural inspection, when Compose is installed, is:

```bash
docker compose -f compose.yaml config
```

This is a synthetic smoke only. It is not `inferdrome run`, does not invoke
`vllm bench serve`, and cannot produce customer-eligible evidence.

## Armed Linux/NVIDIA path

GPU mode requires all of the following before any container is started:

- Linux, accessible `nvidia-smi`, and an accessible NVIDIA GPU;
- `--confirm-gpu` or `INFERDROME_ALLOW_GPU_COMPOSE=1`;
- immutable runner and runtime image references using `@sha256:...`;
- the exact pinned runtime reference above;
- a locally prepared Qwen3-8B snapshot with the existing model-file and
  tokenizer verification inputs;
- the exact profile, model, and model/tokenizer revision environment values;
- an experiment input directory containing `experiment.yaml` whose attached
  endpoint is `http://vllm-engine.internal:8000`; and
- a real writable evidence output directory.

Example preflight variables are deliberately explicit:

```bash
export INFERDROME_ALLOW_GPU_COMPOSE=1
export INFERDROME_VLLM_RUNTIME_IMAGE='vllm/vllm-openai@sha256:ffb2d59b1c059a5bd8d781320c9f5189de8293693b7d95da54befddaa54abf52'
export INFERDROME_VLLM_RUNNER_IMAGE='registry.example.invalid/inferdrome-vllm-runner@sha256:<operator-supplied-digest>'
export INFERDROME_QWEN3_PROFILE_ID='managed-vllm-0.26-qwen3-8b-bf16-v1'
export INFERDROME_QWEN3_MODEL_ID='Qwen/Qwen3-8B'
export INFERDROME_QWEN3_MODEL_REVISION='b968826d9c46dd6066d109eabc6255189de91218'
export INFERDROME_QWEN3_TOKENIZER_REVISION='b968826d9c46dd6066d109eabc6255189de91218'
export INFERDROME_QWEN3_MODEL_PATH='/absolute/path/to/prepared/qwen3-8b'
export INFERDROME_EXPERIMENT_DIR='/absolute/path/to/compose-input'
export INFERDROME_COMPOSE_EVIDENCE_DIR='/absolute/path/to/evidence-output'

scripts/run_vllm_compose.sh gpu --confirm-gpu
```

The placeholder runner digest above is intentionally not executable. Build the
distinct runner artifact first, then obtain and verify its immutable image
digest. A mutable local tag or a successful image build is not an execution
receipt. The GPU profile has no host `ports` mapping, no host network, no
Docker socket, no restart loop, and uses an internal Compose network. Model
weights are mounted read-only and offline environment flags prevent downloads.

The development artifact boundary is explicit:

```bash
docker build --platform linux/amd64 \
  -f Dockerfile.vllm-benchmark-runner \
  --build-arg BUILD_FLAVOR=development \
  --build-arg SOURCE_REPOSITORY_COMMIT=development-unpinned \
  --build-arg INFERDROME_VERSION=0.1.0.dev0 \
  -t inferdrome/vllm-benchmark-runner:development .
```

This command is an image-build gate only. A proof-shaped invocation must use
operator-supplied immutable source and runner image identities, and the
orchestration wrapper refuses a mutable runner tag before any GPU Compose
container starts.

The runner command is the canonical `inferdrome run` command. It invokes the
existing attached-endpoint adapter, which invokes `vllm bench serve` in the
runner container against `vllm-engine.internal`. The source experiment and
registered workload remain the methodology source; Compose supplies only
deployment topology and paths.

The wrapper traps normal exit, failure, cancellation, and interrupt paths and
calls `docker compose ... down --remove-orphans --volumes`. The evidence bind
directory is not a named volume and is preserved; disposable containers and
networks are removed. Cleanup failure returns a distinct failure code.

## Truth boundaries

| Path | What is tested or measured | What it does not claim |
| --- | --- | --- |
| Static checks | YAML, image identity, roles, security, profiles, and cleanup policy | Container execution |
| Mock Compose | Deterministic synthetic HTTP interaction and output publication | vLLM, CUDA, GPU, benchmark, or eligible evidence |
| Linux/NVIDIA runtime smoke | Pinned engine readiness and attached client behavior | A customer-eligible GPU receipt or acceptance |
| Canonical `inferdrome run` in the runner | The existing resolver, `vllm bench serve`, reducer, and bundle verifier | Eligibility unless the separate managed proof boundary independently verifies all required facts |
| Image build | Layer construction from pinned inputs | Immutable final image digest until captured and verified |
| Image digest | Image identity | Source execution or provider invoice truth |
| Deployment receipt | Future outer provenance binding | Implemented or issued by PR6 |

Docker, Compose, Linux/NVIDIA, model preparation, and image-digest gates are
environment-dependent. This repository makes no claim for any unavailable
gate and publishes no raw evidence in this slice.
