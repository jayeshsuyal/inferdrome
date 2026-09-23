# SGLang 0.5.15 direct-host compatibility contract

This additive path targets one already available Linux x86_64 host with exactly two A100-SXM4-40GB cards
and NVIDIA driver **595.84**. It creates two independent BF16, TP=1 replicas,
one per visible GPU, on distinct literal loopback origins. It contains no provider,
SSH, rental, installation, image, registry, Kubernetes or upload operation.

**Status: CPU/fake checked; host dispatch and GPU compatibility remain unverified.**
Every binding and report retains `UNVERIFIED` and `evidence_eligible=false`.
The smallest four-policy block is descriptive and uncalibrated; it cannot satisfy
minimum replication or support a performance conclusion.

## Separate release identities

The release is [SGLang v0.5.15](https://github.com/sgl-project/sglang/releases/tag/v0.5.15),
source commit `f63458b5beaceabbd9d749b9fc956370e1b649e6`.
`sglang_direct_profile.py` shares the finite validated serving arguments with the
existing profile but adds a distinct profile/config digest. It never copies an
image identity. The existing 0.5.18 binding, normalization fixtures, image profile,
trial/report contracts and dashboard reader remain unchanged.

| Artifact | 0.5.18 path | 0.5.15 direct path |
| --- | --- | --- |
| Engine binding | `evaluation-engine-binding.v1` | `evaluation-engine-binding.v2` |
| Trial envelope | `evaluation-study-trial-result.v2` | `evaluation-study-trial-result.v3` |
| Healthy/routing result | `evaluation-{healthy,routing}-result.v2` | `evaluation-{healthy,routing}-result.v3` |
| Study report | `evaluation-study-report.v2` | `evaluation-study-report.v3` |
| Scheduler labels | `SGLANG_0_5_18_SCHEDULER_GAUGES` | `SGLANG_0_5_15_SCHEDULER_GAUGES` |
| Dashboard projection | existing v2 support | `UNSUPPORTED_ENGINE_BINDING` |

The new binding includes the entire expected runtime-manifest digest. Engine-choice
comparison excludes only study config/plan digests, so runtime drift between smoke
and study changes the engine choice. Canonical readers reject cross-version
bindings/results, including artifacts with recomputed outer digests.

The version-specific readiness parser follows the actual
[0.5.15 `/model_info` response](https://github.com/sgl-project/sglang/blob/f63458b5beaceabbd9d749b9fc956370e1b649e6/python/sglang/srt/entrypoints/http_server.py).
It checks Qwen3 architecture/type, model/tokenizer paths, weight revision,
generation mode and disabled multimodal/default sampling settings. It does not
require the extra response fields expected by the old 0.5.18 parser.
`/health_generate` occurs before reset. Warmup is one native streaming request per
replica, fully drained, followed by fresh idle gauges, one bodyless `POST
/flush_cache`, then non-generating health/model/metrics readbacks. Server acceptance
is not observed cache-state proof. Scheduler source age remains unavailable.

## Expected runtime and provenance

`sglang_direct_runtime.py` defines `inferdrome.sglang-direct-runtime.v1`.
A manifest is a **reviewed expected input**, not an attestation generated from
whatever happens to be installed. There is intentionally no host-blessing or
installer command. The synthetic manifest under `tests/fixtures/sglang/direct_0_5_15`
is not usable as a host manifest: its file hashes and paths are placeholders.

The core upstream wheel hashes are fixed in `WHEEL_IDENTITIES`:

| Component | Selected version / lane |
| --- | --- |
| Python | exact recorded 3.12 patch; Linux x86_64 |
| SGLang | 0.5.15, official CPython 3.12 wheel |
| PyTorch | 2.11.0+cu130, official CUDA 13.0 wheel |
| sglang-kernel | 0.4.4+cu130, official release wheel |
| FlashInfer Python / cubin | 0.6.12 / 0.6.12 |
| CUTLASS DSL / base libs / cu13 libs | 4.5.2 |
| cuTile | 1.3.0 |
| cuTile tileiras / PTXAS / NVVM packages | 13.2.86 |
| Triton | 3.6.0; its Ampere PTXAS reports 12.8.93 |
| nvJitLink | 13.0.88 |
| Host C++ JIT toolkit | full CUDA 13.1 Update 1 |

The official +cu130 variants follow the
[tagged installation instructions](https://github.com/sgl-project/sglang/blob/v0.5.15/docs/get_started/install.md).
These are upstream artifact choices, not rewritten packages or lowered dependency
requirements. The reviewed CUTLASS compiler DSO is exactly
`sha256:73b760621e35910305e7bdf8f4c2c0d928c10527a243f8f11a76046edba4f6d8`,
with build marker `Build cuda_13.1.r13.1/compiler.36699951_0`.

Before every fresh pair the lifecycle checks file bytes and exact installed-package
inventory, complete site-package file coverage, selected wheel payload members,
Python version/site root and compiler version outputs. It rejects symlink aliases,
extra import-bearing files, unreviewed `.pth` injection, changed files and missing roles.
The sole `.pth` exception must match the payload of the pinned upstream
[setuptools 81.0.0 wheel](https://pypi.org/project/setuptools/81.0.0/#files); the
required `distutils-precedence.pth` is preserved, not removed or rewritten.
All expected host compiler, interpreter and system-library hashes must be supplied
and reviewed separately. File readbacks are not an immutability guarantee.

The child environment is constructed from scratch. `CUDA_HOME`, `NVCC`,
`CUDACXX` and `PYTORCH_NVCC` select the host toolkit; the pip cuTile tool directory
precedes it on `PATH`; Triton's PTXAS has an explicit path. Library directories come
only from declared roles. Ambient `PYTHONPATH`, `CUDA_PATH`, loader paths, user site
and shared JIT caches are not inherited. Each replica receives a new private cache
directory. Use a venv made with `--copies`: the process owner canonicalizes the
Python executable, so a symlinked venv interpreter loses its selected environment.

After readiness/reset and after each measured trial, `/proc` readbacks cover both
owned process groups, including the supervisor and workers. The gate requires
stable membership/start times, the bound driver/Torch/kernel mappings, and matching
path/device/inode/bytes for every mapped regular file. CUDA character devices
have a separate manifest allowlist with exact major/minor identity; device bytes
are never read. A replica cannot map the other replica’s GPU device. GPU inventory
also requires the two bound UUIDs, 40GB memory range and disabled MIG. Inaccessible, deleted, replaced,
unlisted or racing mappings withhold success. Large files are hashed once per
snapshot. Cancellation retains hashing work until cleanup can account for it.

**Remaining host gates:** the final +cu130 installation dependency closure, actual
compiler-child/library selection, model loading, memory fit, native streaming and
all GPU behavior have not been exercised here. The gate rejects unlisted JIT DSOs;
a fresh FlashInfer/CUTLASS compilation may therefore stop preparation. This slice
does not automatically bless generated native code or claim the host is ready.
Resolve that provenance gate in a separate reviewed host qualification step; do not
relax the allowlist, alter the older packet, or mark these artifacts qualified.

## Local smoke/study entrypoint

`run_sglang_direct_smoke_study(config, profiles, runtime, output)` is an explicit
Python API. It requires a complete expected runtime manifest and a preloaded
Qwen/Qwen3-8B snapshot at revision
`b968826d9c46dd6066d109eabc6255188de91218`. It does not install either.
It accepts exactly one healthy block containing all four existing policies.
The output directory must not already exist.

Two smoke trials each use a fresh pair. Each must complete all foreground offers,
provenance checks and exact-owned cleanup before the study directory is created.
The smoke directory has no completed-study manifest. The subsequent four trials
also each receive a fresh pair: six pairs total. Preparation and cleanup have
separate bounded reservations, including after failure. A failed smoke,
provenance readback or cleanup withholds the study. Pass a caller-owned stop event
for signal handling. Reports remain local and keep the complete engine binding.

The caller reviews a concrete host manifest before invoking this API; this document
and the generated synthetic fixtures do not authorize a host run or spending.

## Reproduce the CPU checks

From the repository root in its development Python environment:

```sh
PYTHONPATH=src:. python scripts/generate_sglang_direct_contracts.py --check
PYTHONPATH=src:. python -m pytest -q tests/unit/test_sglang_direct_contracts.py tests/unit/test_sglang_direct_runtime.py tests/unit/test_sglang_direct_lifecycle.py
python -m ruff check .
python -m mypy
```

The generator owns only the new `schemas/evaluation/sglang-direct-0.5.15` and
`tests/fixtures/sglang/direct_0_5_15` snapshots. Its populations use fake CPU clients
and explicitly carry `SYNTHETIC_ONLY`. No GPU qualification can be inferred from
passing these checks.
