# Fixed role-image publication preparation v1

Status: **manual worker workflow available; no worker or publication asserted.**
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
owner-approved, manually confirmed worker build + CPU-only smoke
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

## Dedicated temporary CPU build worker

The dispatch guard runs on a standard hosted runner and rejects a wrong mode,
confirmation, repository, reviewed-main ref, source SHA, dirty checkout, or
malformed non-sensitive worker alias. Exactly one subsequent job is selected:
`BUILD_AND_SMOKE_ONLY` has `contents: read` only and contains no GHCR login or
push; `PUBLISH_FIXED_ROLE_IMAGES` alone receives a job-scoped `GITHUB_TOKEN`
with `packages: write` throughout that selected job. Its GHCR login and push
commands run only after the same job's build and smokes succeed. Normal
engineering, dashboard, and synthetic/mock CI remain on GitHub-hosted runners.

Both selected jobs require the fixed `self-hosted`, `Linux`, `X64`, and
`inferdrome-role-image-cpu` labels and run for at most 45 minutes. The worker's
name must exactly equal the dispatch's restricted alias. These are routing and
mismatch checks, not cryptographic proof of a VM, its disposable status, or an
operator approval. The worker must accept only reviewed-main manual dispatches;
untrusted pull-request, fork, or arbitrary workload execution is out of scope.

The same selected worker builds both fixed `linux/amd64` roles and runs every
final container smoke before either role can be pushed. Those smokes are
non-root, read-only, no-network checks of the packaged Python version, CLI,
role adapter help, and the PR74 serving-interpreter metadata binding. The
publication mode then pushes those same local image tags to only the two fixed
repositories, requires a returned `repository@sha256:<digest>` for each,
rejects a digest collapse, and exposes the two immutable references plus the
canonical paired-record SHA-256 as job outputs and in the job summary.

The dedicated-worker monitor is observation-only. Before, during, and after the
child build it records Docker Server and Buildx versions, storage driver and
root, filesystem mapping, free bytes, and free inodes. During samples and the
child deadline are bounded; each Docker observation command has a 10-second
limit. Monitor failure terminates an unfinished child, and a failed build still
records its before/during/after observation before returning its nonzero status.
A monitor-enforced timeout returns a nonzero monitor result even if its child
exits cleanly after termination. The recorded minima are samples, not an exact
peak, capacity admission threshold, or a promise that an image will fit. This
worker does not invoke the GitHub-hosted Android SDK helper, delete SDK files,
prune Docker, or perform any broad host cleanup.

### Operator setup and teardown runbook

This is a non-executable procedure; bracketed values are unresolved external
facts, not a launch proposal or repository defaults.

1. First perform separately authorized, read-only eligibility, quota, and quote
   checks. Record `[project]`, `[region/zone]`, `[boot-image identity]`,
   `[network/firewall path]`, `[runner registration scope]`, and the rate/expiry
   without placing credentials, registration tokens, or provider output in this
   repository, workflow arguments, or job summary.
2. Obtain a separate exact approval binding one temporary Ubuntu 24.04 Linux
   x64 CPU VM, its image, region/zone, disk, lifetime, cost estimate, and
   accountable operator. A suggested starting configuration of 4 vCPU, 16 GB
   RAM, and a 200 GB disposable disk is neither a measured minimum, a fit
   guarantee, a quota result, nor spending authorization.
3. At creation, require the provider-native maximum-runtime action to be
   `DELETE`, read it back, and bind the exact VM and boot-disk identity. Register
   one repository-scoped GitHub runner for one ephemeral job with the fixed
   labels above. The VM receives no long-lived administrator or cloud mutation
   credential; job-scoped GHCR capability exists only in a separately approved
   manual publication-mode job.
4. After success, failure, or timeout, independently deregister the GitHub
   runner and delete/reconcile the exact approved VM and all owned billable
   disks, then verify their scoped absence. Runner deregistration and workflow
   timeout are not VM deletion. Account for setup, teardown, disk, network, and
   registry costs; an estimate or USD cap is not a hard invoice guarantee.

No current manual dispatch, worker registration, VM, running role image, quote,
registry image, published pair, or receipt is asserted by this source change.

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
