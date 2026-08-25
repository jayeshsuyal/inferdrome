# Reproducible Inferdrome runner image

Status: **implemented as a local packaging boundary; no container execution is
claimed in this environment**

The repository's `Dockerfile` packages the Inferdrome runner only. The image
contains a narrow `inferdrome-runner` client that makes one configured request
to an already-running inference endpoint and writes bounded, synthetic,
evidence-ineligible metadata to `/evidence`. It does not contain or launch
vLLM or SGLang, implement benchmark methodology, expose a listener, or create
an Inferdrome evidence bundle. The existing `inferdrome` command and local
non-container execution path are unchanged.

## Image identity and build modes

Every `FROM` uses the pinned multi-platform digest for the Python
`3.12.12-slim-bookworm` base image. The digest is the base-image identity;
the final built image must still be recorded by its own immutable image digest
when a future proof run uses it. A tag such as `inferdrome-runner:dev` is only
a mutable local alias.

The build accepts these explicit arguments:

| Argument | Development default | Proof/release requirement |
| --- | --- | --- |
| `SOURCE_REPOSITORY_COMMIT` | `development-unpinned` | 40–64 lowercase hexadecimal characters |
| `INFERDROME_VERSION` | `0.1.0.dev0` | bounded version string |
| `BUILD_FLAVOR` | `development` | `proof` or `release` requires both identities |

Proof and release builds fail closed when the source commit or version is
missing or malformed. Development builds are explicitly not proof identities.
The committed `uv.lock` is installed with `uv sync --frozen --no-dev
--no-editable`; dependency resolution cannot rewrite or replace the lock.

For a proof-shaped build, supply the exact source revision and use a local
tag only as a convenience:

```bash
SOURCE_COMMIT="$(git rev-parse HEAD)"
INFERDROME_VERSION="$(PYTHONPATH=src python -c 'from inferdrome import __version__; print(__version__)')"
docker build --pull=false \
  --build-arg SOURCE_REPOSITORY_COMMIT="$SOURCE_COMMIT" \
  --build-arg INFERDROME_VERSION="$INFERDROME_VERSION" \
  --build-arg BUILD_FLAVOR=proof \
  -t inferdrome-runner:proof .
```

The image's OCI labels record the source repository, supplied source commit,
Inferdrome version, and `runner-only` purpose. The source commit is not the
image digest, and neither is a deployment receipt. PR5 does not issue an
executed receipt; a later receipt flow must bind the immutable image digest
and source revision after independently verified execution.

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

The final image runs as UID/GID `10001:10001`, has no serving process, and
expects a read-only root filesystem. The only intended writable locations are
the explicitly mounted `/evidence` directory and `/tmp/inferdrome` temporary
directory. The runner refuses to create an absent evidence directory and
publishes a new output file without replacing an existing file.

Example invocation against an already-running local endpoint:

```bash
mkdir -p runner-output
docker run --rm --read-only \
  --tmpfs /tmp/inferdrome:rw,noexec,nosuid,size=64m \
  --mount type=bind,src="$PWD/runner-output",dst=/evidence \
  inferdrome-runner:proof \
  --endpoint http://host.docker.internal:8000/v1/chat/completions \
  --model inferdrome/mock-model \
  --prompt 'local synthetic smoke' \
  --evidence-dir /evidence
```

This is a one-request client smoke, not the benchmark command and not a
serving-runtime deployment. Docker execution is an environment-dependent
gate; absence of Docker must be reported as unavailable rather than treated as
a passing image build.

The `.dockerignore` denies the repository by default and allowlists only the
Dockerfile, lock/configuration files, README, and source package. Git history,
virtual environments, credentials, model/cache directories, raw evidence,
test fixtures, and host artifacts are excluded from the build context.
