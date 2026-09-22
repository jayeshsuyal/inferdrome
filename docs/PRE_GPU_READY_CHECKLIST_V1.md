# Pre-GPU readiness checklist v1: Lambda manual host

Status: **not GPU-ready.** This checklist separates reviewed source from the
external artifacts and approvals required before the bounded Lambda manual-host
A100 campaign can be proposed. It neither authorizes a workflow dispatch,
image publication, model download, provider action, GPU use, nor spending.

## Required sequence

| Gate | Required fact | Evidence that is acceptable | Current meaning when absent |
| --- | --- | --- | --- |
| 1. Reviewed source | The dashboard, role-image workflow, routing contract, and cleanup boundary are merged at one reviewed main commit. | Exact merge SHA plus green required CI and review record. | A PR head or local checkout is not an operational source identity. |
| 2. Role images | Both fixed roles were built by the manual workflow, passed its CPU-only smokes, and returned distinct immutable GHCR references. | `ghcr.io/jayeshsuyal/inferdrome-private-engine@sha256:…`, `ghcr.io/jayeshsuyal/inferdrome-cpu-runner-observer@sha256:…`, and the paired-record SHA-256 from one exact workflow run. | A Dockerfile, mutable tag, local image ID, or successful unit test is not an image identity. |
| 3. Pinned model | The exact Qwen3-8B bytes were acquired at a separately approved CPU/local staging destination and independently verified. | Model `Qwen/Qwen3-8B`, revision `b968826d9c46dd6066d109eabc6255188de91218`, manifest `sha256:ef291a8dd0f21604c8da3025f5112bd6641e891a3401c8550584725eaabe55cc`, snapshot `sha256:588d19e9e489cccdad793718d8c5efbad0738be717369f9eacb94ce514992d2c`. | A source URL, model name, or unverified cache directory is not a verified staging artifact. |
| 4. Deterministic rehearsal | The exact source/image/model inputs complete the local synthetic/loopback rehearsal and its sealed evidence verifies offline. | Sealed package digest, independent verifier result, and retained redacted run record. | A UI screenshot alone is not a runtime rehearsal. |
| 5. Bounded manual-host procedure | The owner has reviewed the proposed bounded rental and attended exact-instance cleanup procedure before any host exists. | A separate authorization request that names the two-A100 topology, maximum rental time, accountable operator, exact-ID termination/readback procedure, and an operator cost estimate. | A source plan or cost estimate is not provider permission, automatic cleanup, or an invoice guarantee. |

No GPU campaign is ready until gates 1–3 have real, independently checkable
artifacts. Gate 4 establishes reproducibility of the controlled path; gate 5
is a pre-launch procedure review, not a claim that a host already exists.

For the Vast direct-process path, gate 2 additionally requires the engine
digest to carry the exact `vast-ssh-public-v1` labels, to be anonymously
pullable without registry credentials, and to contain build-time SSH and
SSH support. Runtime `apt` or other package bootstrap is forbidden. A
separately observed launch must reach SSH readiness within 180 seconds; engine
readiness retains the existing 300-second bound. The direct foreground sshd
control plane may run as root with public-key authentication only, but the
checked-in wrapper must run vLLM as UID 2000. None of these
facts is established merely by the source contract.
The public-publication plan is engine-only and explicitly forbids changing the
CPU observer package's visibility.

For a future manual role-image publication, retain the dedicated worker's
Docker/Buildx version, storage location, filesystem mapping, and bounded
before/during/after byte and inode observations as diagnostics. They sample
availability; they do not establish a build-fit threshold, an exact peak, a
capacity reservation, worker identity, or gate 2's immutable image references.
The worker monitor does not remove an SDK, prune Docker, or clean broad host
paths. A monitor timeout or observation failure remains a failed operation, not
a retry authorization.

## Fixed local facts for later review

- The active path is one operator-supplied Lambda manual host with **two
  `NVIDIA A100-PCIE-40GB` GPUs**, two TP=1 private engines, and one distinct
  CPU-only observer. It is a same-host benchmark topology, not host-failure
  independence. The frozen GCP SXM4 contracts remain separate supported code;
  they are not campaign prerequisites here.
- This repository intentionally does not invent a provider machine, boot image,
  staging destination, or future host path. Its committed manifest totals
  `16,397,461,266` bytes. A staging operation therefore needs that complete
  immutable snapshot plus enough separate verified staging space; two complete
  copies require at least `32,794,922,532` bytes before filesystem overhead.
- A later, separately authorized host transfer must make both immutable role
  images and the verified snapshot available before startup. It may not
  build/pull an image or fetch the model at startup.
- Lambda manual-host cleanup is attended: an accountable operator must request
  exact-instance-ID termination and independently read back termination. The
  UTC deadline is not a provider-enforced TTL, and this path does not claim an
  automatic prelaunch watchdog, hard invoice cap, or guaranteed cleanup.
- The role-image workflow has a separate proposed temporary CPU build-worker
  path. Its suggested Ubuntu 24.04 x64, 4 vCPU, 16 GB RAM, and 200 GB
  disposable-disk configuration is an external starting point only—not a
  measured minimum, capacity proof, quote, or permission to create a VM.

## Minimal separate operational request

The following request is deliberately incomplete until an owner fills each
bracketed external value. It is a request for authority, not a command to run.

> Authorize one manual role-image publication at reviewed main commit
> `[40-lowercase-hex SHA]`. Dispatch only the reviewed `main` branch ref that
> resolves to that SHA, then provide the same SHA as the workflow input; do not
> create a tag for this operation. Permit only the checked-in manual workflow to build and
> CPU-smoke `linux/amd64` images and publish them to
> `ghcr.io/jayeshsuyal/inferdrome-private-engine` and
> `ghcr.io/jayeshsuyal/inferdrome-cpu-runner-observer`. The workflow's selected
> one-job ephemeral CPU worker is bounded to 45 minutes and must return both
> distinct `repository@sha256` outputs plus their paired-record SHA-256. Before
> creation, separately approve `[project]`, `[region/zone]`, `[boot image]`,
> `[disk]`, `[maximum VM lifetime]`, `[cost estimate/cap]`, and exact cleanup
> verification; runner deregistration is not VM deletion. Confirm available
> GHCR package storage and all setup, disk, network, registry, and teardown
> costs. The repository does not know those values or guarantee an invoice cap.
>
> Separately authorize one pinned Qwen3-8B acquisition and staging operation
> from the revision above into an exact private CPU/local staging destination
> `[staging destination]`.
> Reserve at least the 32,794,922,532-byte
> two-copy staging minimum plus filesystem overhead, require the two committed
> SHA-256 identities above before accepting it, and bind `[maximum staging
> duration]` plus `[currency/USD cap]`. This request does not authorize a VM,
> GPU, registry mutation beyond the two image repositories, or provider
> resource creation.

## After-launch host admission (not a prelaunch prerequisite)

Only after a separately approved bounded rental has created one manual host may
an operator finalize a manual-host input/run. That after-launch record must bind
the observed exact Lambda instance ID, region, `gpu_2x_a100` API type, two
observed PCIe GPU UUIDs, the accountable operator, an exact UTC termination
deadline, the two returned image digests, and the verified model transfer/load
receipt. It must then verify that the two TP=1 engines and distinct CPU-only
observer use the intended private local endpoints. These are host-admission
facts, not portable prelaunch facts; this checklist neither supplies them nor
authorizes their collection.

The subsequent manual-host authorization must bind the observed host-admission
facts, evidence/preparation paths, quote or rate, and USD cap. It must
explicitly accept attended exact-ID termination/readback risk; it must not claim
a prelaunch watchdog, automatic provider termination, or a hard invoice cap.
Nothing in this document supplies the unresolved external values or
self-authorizes that campaign.
