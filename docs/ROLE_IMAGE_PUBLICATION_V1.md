# Fixed role-image publication preparation v1

Status: **local preparation only.** This repository currently has no checked-in
registry-publishing workflow, no published role-image receipt, and no claim
that a serving image, runner image, boot image, or model snapshot exists in an
external registry or provider environment.

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
separate exact approval for any registry-capable workflow
          |
          v
future returned repository@sha256 receipt -> paired-digest validation
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

## Required future approval boundary

Adding or dispatching a GitHub workflow that can authenticate to GHCR, build
images, or write packages is a distinct external-publication capability. It
requires an exact owner/user authorization covering the source commit, the two
fixed repositories, manual confirmation/protected environment, use of the
ephemeral GitHub token over stdin only, image/runtime/model inputs, and the
expected receipt handling. That future operation must remain manual-only and
must run both non-GPU adapter `--help` smokes before either image is pushed.

Until that exact authorization is granted and a genuine returned digest is
recorded, deployment and campaign inputs remain unresolved. A local plan,
label check, tag, Docker image ID, or build log is never a registry identity,
serving proof, provider fact, or evidence-package receipt.
