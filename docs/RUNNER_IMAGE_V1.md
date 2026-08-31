# Reproducible Inferdrome runner image

Status: **implemented as a local packaging boundary; no container execution is
claimed in this environment**

The repository's `Dockerfile` packages the Inferdrome runner only. The image's
normal entrypoint is the existing canonical `inferdrome` CLI, so its argument
surface and benchmark methodology remain the same as local execution. It does
not contain or launch vLLM or SGLang, expose a listener, or create a serving
runtime. The separate `inferdrome-runner-probe` command is an explicitly named
one-request synthetic smoke utility; it is not the benchmark runner and cannot
produce eligible evidence.

## Image identity and build modes

Every `FROM` uses the pinned multi-platform digest for the Python
`3.12.12-slim-bookworm` base image. The digest is the base-image identity;
the final built image must still be recorded by its own immutable image digest
when a future proof run uses it. A tag such as `inferdrome-runner:dev` is only
a mutable local alias.

The build accepts these explicit arguments. The proof/release wrapper derives
the source revision and package version; callers cannot override either.

| Argument | Development default | Proof/release requirement |
| --- | --- | --- |
| `SOURCE_REPOSITORY_COMMIT` | `development-unpinned` | 40–64 lowercase hexadecimal characters |
| `INFERDROME_VERSION` | `0.1.0.dev0` | bounded version string |
| `BUILD_FLAVOR` | `development` | `proof` or `release` requires both identities |

Proof and release builds fail closed when the source commit or version is
missing, malformed, or inconsistent with the packaged code. Development builds
are explicitly not proof identities. The committed `uv.lock` is installed with
`uv sync --frozen --no-dev --no-editable`; dependency resolution cannot rewrite
or replace the lock. The uv bootstrap is the fixed Linux/amd64 `0.8.17`
release archive verified by its committed SHA-256 checksum.

The Inferdrome package declares the SPDX expression `Apache-2.0` and includes
the canonical project `LICENSE`, `THIRD_PARTY_NOTICES.md`, and the retained
license texts for bundled dashboard runtime assets. Each Inferdrome image copies
the same material to `/usr/share/licenses/inferdrome`. Apache-2.0 describes the
Inferdrome project work; it does not relicense container bases, Python or
frontend dependencies, serving engines, models, workloads, generated output,
or evidence archives.

For a proof-shaped build, use the clean-context wrapper. It refuses modified,
deleted, staged, untracked, or ignored files in the Docker allowlist
(`Dockerfile`, `.dockerignore`, `pyproject.toml`, `uv.lock`, `README.md`,
`LICENSE`, `THIRD_PARTY_NOTICES.md`, `LICENSES`, and `src`) and also checks the
wrapper itself (`scripts/build_runner_image.py`) as a trust-root input. It
derives the full commit and package version and passes the canonical GPU target
platform:

```bash
python scripts/build_runner_image.py \
  --flavor proof \
  --tag inferdrome-runner:proof
```

The wrapper then materializes a temporary Docker context from a validated
`git archive HEAD` containing exactly that Docker allowlist. The ambient
working tree, ignored files, `.git` directory, and the wrapper itself are not
sent as proof context. Archive members are checked for traversal and links,
and the temporary context is removed after successful, failed, or interrupted
build attempts. The source commit in a proof label is locally verified only
under this clean wrapper; Dockerfile syntax by itself does not bind arbitrary
build-context bytes to Git. The image's OCI labels record the source
repository, verified source commit, Inferdrome version, and `runner-only`
purpose. The source commit is not the image digest, and neither is a
deployment receipt. PR5 does not issue an executed receipt; a later receipt
flow must bind the immutable image digest and source revision after
independently verified execution.

“Reproducible” here means bounded and verified inputs: pinned base digest,
hash-verified uv bootstrap, frozen lockfile, clean relevant context, and an
explicit target platform. This slice does not claim bit-for-bit repeatability
of final image digests.

## Local, non-GPU smoke

The host smoke starts a loopback-only synthetic HTTP endpoint, invokes the
runner CLI, and checks that stdout and the mounted output agree. It proves
configuration, endpoint invocation, bounded metadata, and output publication
only:

```bash
PYTHONPATH=src python scripts/runner_smoke.py
PYTHONPATH=src python scripts/runner_smoke.py --check
```

The output is structurally marked `synthetic_only: true` and
`evidence_eligible: false`. The smoke makes no CUDA, GPU, vLLM, SGLang,
customer-evidence, benchmark, or provider-invoice claim.

The output's `status: SUCCEEDED` means only that the configured request
returned a bounded successful HTTP response. It is not a benchmark result,
evidence verdict, acceptance verdict, or provider attestation.

## Container contract

The final image defaults to UID/GID `10001:10001`, has no serving process, and
expects a read-only root filesystem. Compose overrides that default with the
invoking non-root host UID/GID for bind-mounted runs; direct Docker use must
preserve the same explicit non-root ownership contract. The only intended writable locations are
the explicitly mounted `/evidence` directory and `/tmp/inferdrome` temporary
directory. The canonical command writes its normal run workspace/output under
the explicit mounted path supplied by the caller.

Example canonical invocation with an operator-supplied experiment/config mount:

```bash
mkdir -p runner-input runner-output
docker run --rm --read-only \
  --tmpfs /tmp/inferdrome:rw,noexec,nosuid,size=64m \
  --mount type=bind,src="$PWD/runner-input",dst=/inputs,readonly \
  --mount type=bind,src="$PWD/runner-output",dst=/evidence \
  inferdrome-runner:proof \
  run /inputs/experiment.yaml --runs-root /evidence/runs
```

The canonical command remains responsible for resolving the experiment,
invoking the configured producer, reducing measurements, and sealing/verifying
outputs. A real attached-endpoint run still requires its producer dependency
and endpoint; this image does not bundle vLLM, so that dependency boundary is
not claimed executable until the later runtime-image slice.

To run the explicitly synthetic endpoint probe instead, override the image
entrypoint and use a mounted directory:

```bash
docker run --rm --read-only \
  --tmpfs /tmp/inferdrome:rw,noexec,nosuid,size=64m \
  --mount type=bind,src="$PWD/runner-output",dst=/evidence \
  --entrypoint /opt/inferdrome-runtime/bin/inferdrome-runner-probe \
  inferdrome-runner:proof \
  --endpoint http://host.docker.internal:8000/v1/chat/completions \
  --model inferdrome/mock-model \
  --prompt 'local synthetic smoke' \
  --evidence-dir /evidence
```

The probe output is synthetic and ineligible, not a benchmark result or
evidence bundle. Docker execution is an environment-dependent gate; absence
of Docker must be reported as unavailable rather than treated as a passing
image build.

The `.dockerignore` denies the repository by default and allowlists only the
Dockerfile, lock/configuration files, README, project and third-party license
materials, and source package. It also explicitly excludes common editor,
cache, bytecode, and package-metadata host artifacts as defense in depth. Git
history, virtual environments, credentials, model/cache directories, raw
evidence, test fixtures, and host artifacts are excluded from the build
context. These patterns are not a substitute for the proof wrapper's exact
`HEAD` archive.
