# Fixed role-image publication preparation v1

Status: **manual publication workflow available; no publication asserted.**
`.github/workflows/role-image-publish.yml` is an opt-in `workflow_dispatch`
workflow for exactly the two fixed GHCR repositories below. It has no
push/pull-request/tag/schedule trigger, requires the dispatch confirmation and
an exact checked-out source SHA, and grants `packages: write` only to its one
publication job. Checking in that workflow does not dispatch it,
publish an image, create a receipt, or claim that a serving image, runner
image, boot image, or model snapshot exists in an external registry or
provider environment.

The narrow preparation contract keeps a future manual operation inspectable
without granting publication authority to normal CI, a pull request, or local
development commands. It is not a campaign, a provider controller, or evidence
of a successful image build or upload.

```text
fixed role + source identity
          |
          v
pure local plan -> normalized local inspection
          |
          v
owner-approved, manually confirmed build + CPU-only smoke
          |
          v
returned repository@sha256 outputs -> paired-digest validation
```

## Fixed roles and identities

The existing `Dockerfile.vllm-benchmark-runner` is the only build input for the
two runtime roles:

| Role | Fixed later repository | Runtime boundary |
| --- | --- | --- |
| `private-engine` | `ghcr.io/jayeshsuyal/inferdrome-private-engine` | one separately operated private vLLM engine |
| `cpu-runner-observer` | `ghcr.io/jayeshsuyal/inferdrome-cpu-runner-observer` | CPU-only measurement/observer role |

Both plans require the existing clean-context release wrapper, `linux/amd64`,
a full lowercase source commit, the packaged Inferdrome version, vLLM `0.26.0`,
UID/GID `2000:0`, and the Dockerfile's role-specific labels. The role labels
are intentionally different, and any future paired receipt rejects equal OCI
content digests. A mutable tag is only a transient transport reference; a
usable runtime identity must be the returned `repository@sha256:<digest>`.

## Local-only planner and validators

These commands use only local JSON and standard-library validation. They never
start Docker, pull a base, contact GHCR, authenticate, publish, construct a
provider client, or expose a credential. Paths below are illustrative and must
be supplied by the operator; they are not a publication proposal.

```sh
python scripts/role_image_publish.py plan \
  --role private-engine \
  --source-commit <exact-main-commit> \
  --version 0.3.0.dev0 \
  --workflow-run-id <future-manual-run-id> \
  --output /private/tmp/private-engine-plan.json

python scripts/role_image_publish.py verify-local-inspection \
  --plan /private/tmp/private-engine-plan.json \
  --labels-json /private/tmp/normalized-local-image-labels.json \
  --platform linux/amd64 \
  --user 2000:0
```

`verify-local-inspection` accepts only the exact role labels from the plan; it
does not inspect a Docker daemon itself. `validate-registry-digest` accepts only
one matching fixed `repository@sha256:<64-lowercase-hex>` value and, when given
`--output`, creates a no-replace *local validation record*. It deliberately
labels that record `LOCAL_FORMAT_AND_ROLE_BINDING_ONLY`: it cannot establish
that an upload succeeded. Only a separately approved manual workflow may emit
an externally observed registry receipt after a successful push and returned
registry digest. `validate-digest-pair` then requires two local validation
records to share one source identity and have different content digests.

## Manual publication boundary

The checked-in workflow is a *capability*, not dispatch authorization. Before
an owner dispatches it, the owner must separately approve the exact source
commit and the two fixed repositories. The operator dispatches the reviewed
`main` branch ref that resolves to that approved commit and provides the exact
SHA as the `source_commit` input. The job rejects anything other than a full
lowercase 40-character SHA equal to both `GITHUB_SHA` and the checked-out
`HEAD`. A GitHub Actions workflow-dispatch ref is a branch or tag name, not a
raw commit SHA; this workflow requires `main` and never needs a release tag.

This workflow does not claim GitHub environment protection, required reviewers,
or any GitHub-side approval mechanism. The explicit owner and commit-bound
dispatch approval is the authorization boundary. A manual dispatch remains
prohibited until an owner gives that separate approval for the exact source
commit and the two fixed repositories.

The job builds both fixed `linux/amd64` roles from the reviewed release wrapper
with immutable Dockerfile base inputs, validates their normalized identity
labels, and runs a no-network, read-only, CPU-only Python/import plus adapter
`--help` smoke *before either push*. Only then does the publication job receive
its ephemeral job-scoped `GITHUB_TOKEN` through stdin for GHCR login. It pushes
only the two fixed repositories, requires a returned
`repository@sha256:<digest>` for each, rejects a digest collapse, and exposes
the two immutable references plus the canonical paired-record SHA-256 as job
outputs and in the job summary. The engine role probes only the private-engine
adapter `--help`; the CPU runner/observer probes only its separate runner
adapter `--help`.

Before the vLLM base extraction begins, the workflow writes the available-byte
counts for the runner root filesystem, Docker's `DockerRootDir` filesystem, and
the actual `RUNNER_TEMP` filesystem, plus the Docker root path and Docker's
storage report, to the job log and summary. A missing mandatory observation
stops the job before the build. On a GitHub-hosted Linux runner only, it then
checks the actual host platform as well as the runner context, and validates
every component and canonical identity of the one disposable path
`/usr/local/lib/android/sdk`; a missing path is a disclosed no-op, while a
symlink, non-directory, unsafe context, or failed removal stops the job before
the build. The removal command is no-shell and limited to that one directory's
filesystem. It does not prune Docker or any other runner content. These context
checks constrain the one deletion; they are not an attestation of the runner or
host's broader state.

If the build step itself fails, a separate diagnostic-only step recollects the
same capacity observations after the failure. It cannot reclaim the SDK, is
allowed to fail without replacing the original build failure, and runs before
the default failure handling leaves the GHCR login/push step skipped. Every
available-byte delta after SDK reclamation is an observation, not a guaranteed
reclaimed-byte count under concurrent runner writes; neither observation nor
reclamation establishes that either image build will fit.

The workflow cannot make a pair of pushes atomic: a later registry failure may
leave an earlier role published, in which case it emits no validated pair
output and the owner must inspect or clean up under a separate authorization.
The CPU smoke proves only the packaged Inferdrome Python/adapter import path;
it neither starts vLLM nor proves GPU, serving, model, registry, provider, or
campaign readiness.

After both returned digests have passed paired validation, the workflow retains
only the validator-produced canonical `digest-pair.json` as a pinned-action
artifact alongside its SHA-256 job output. It does not retain Docker
configuration, login material, raw Docker inspection output, tokens,
credentials, plans, or labels. The SHA-256 is calculated over the exact
retained file bytes, including its canonical final newline; it is not a
substitute for inspecting the pair record itself.

Until a genuine manual run returns and records both distinct digests,
deployment and campaign inputs remain unresolved. A local plan, label check,
tag, Docker image ID, or build log is never a registry identity, serving proof,
provider fact, or evidence-package receipt.

For the required source/image/model/rehearsal/authorization sequence and the
minimal separately approved artifact-operation request, see
[`PRE_GPU_READY_CHECKLIST_V1.md`](PRE_GPU_READY_CHECKLIST_V1.md).
