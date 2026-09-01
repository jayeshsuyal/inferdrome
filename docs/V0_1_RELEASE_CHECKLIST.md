# Inferdrome v0.1 release checklist

Status: **Producer-side v0.1.0 release authorized; independent prospective ExitSpec evaluation and receipt remain deferred and NOT_RECORDED**

This checklist maps the frozen
[v0.1 definition of done](V0_1_DEFINITION_OF_DONE.md) to reviewable evidence.
An automated check proves only the boundary it exercises. It cannot substitute
for independent ExitSpec demonstrations, owner archive-publication and
external-material licensing decisions, a security review, or final release
sign-off.

## Automated CI gates

Every pull request and every update to `main` must run all three jobs in
`.github/workflows/ci.yml`:

| Required job | Evidence boundary |
| --- | --- |
| `Engineering gate` | Generated schemas and goldens, static GPU-proof assets, script syntax, Ruff, strict mypy, and the complete Python suite |
| `Deployment qualification gate` | Local Docker Compose synthetic qualification, including bounded cleanup and induced-failure diagnostics; no cloud/GPU/evidence claim |
| `Dashboard gate` | TypeScript, frontend unit tests, populated Playwright navigation against the real server, dashboard backend tests, committed production assets, and installed-wheel smoke |

The workflow uses an auto phase for normal pull-request, merge-queue, and
`main` events, with explicit phase choices for manual release runs. It uses
read-only repository permissions, no secrets, bounded job timeouts, exact-
commit pins for every GitHub Action, a hash-bearing `uv.lock` with a
lock-freshness check and frozen Python sync, lockfile installation for the
frontend, and retained Playwright traces, screenshots, and videos on failure.
Normal pull-request CI remains GPU-free.

## Definition-of-done evidence map

| Definition-of-done section | Repository evidence | Release status |
| --- | --- | --- |
| 1. Public contracts | `schemas/public/v1`, `docs/PUBLIC_CONTRACTS_V1.md`, conformance fixtures, schema generation check | Automated gate |
| 2. Resolution and identity | `docs/RESOLUTION_AND_WORKSPACE.md`, resolver and digest tests | Automated gate |
| 3. Execution lifecycle | State, cancellation, workspace, orchestrator, and adversarial tests | Automated gate |
| 4. Producer adapters | Fake and pinned-vLLM adapters, native goldens, managed-host checks, standalone GPU profile, exact A10 receipt | Automated gate plus genuine host evidence complete |
| 5. Canonical request records | Public schema, pinned normalizer, golden and semantic-invariant tests | Automated gate |
| 6. Measurements | Metric definitions, deterministic reducer, quantile tests, conformance vectors | Automated gate |
| 7. Evidence bundle | Bundle contract, offline verifier, immutable publication, mutation suites | Automated gate |
| 8. ExitSpec integration | Independently owned importer, recalculation, decision table, and receipt | External release blocker for acceptance; deferred post-v0.1 by owner authorization; no outcome or receipt recorded |
| 9. Flagship demonstration | Managed runbook, exact A10 handoff, corruption and synthetic rejection demos | Inferdrome producer evidence complete; independent prospective ExitSpec evaluation deferred and not claimed |
| 10. Security and privacy | Threat model, bounded readers, adversarial tests, exact-archive publication review | `EXTERNAL_ONLY`; owner refusal to publish recorded; human security review approved subject to recorded limitations |
| 11. Engineering quality | All three CI jobs, packaging smoke, documentation, contribution guidance | Apache-2.0 selected and added; `v0.1.0` tag deferred to the release record |

## Release evidence and deferred external work

- [x] Record one newly generated, customer-eligible bundle from a clean,
  compatible Linux/NVIDIA host.
- [x] Record capture producer commit
  `c08b46d9fbd87477f45d130aa3c63615937c4dc3` and reviewed bundle digest
  `sha256:bae216f2165eb06ae2e0f14d3cd852f8e0ebb381bf1f68c71072769b3c0c1675`.
- [ ] Independently demonstrate ExitSpec `PASS`, `FAIL`, and `NOT_PROVEN` using
  frozen contracts.
- [ ] Retain the ExitSpec ingestion receipt digest.
- [x] Independently replay and record corrupted-artifact `INTEGRITY_MISMATCH`
  and synthetic-fixture `EVIDENCE_INELIGIBLE` rejection facts.
- [x] Retain the deterministic GPU capability profile, publication review, and
  handoff manifest. The raw archive remains `EXTERNAL_ONLY` and uncommitted.
- [x] Owner decides whether to approve public delivery of the exact archive: on
  2026-08-31, Jayesh decided it remains `KEEP_EXTERNAL_ONLY` and unpublished.
  This is a refusal to authorize public delivery, not a privacy or licensing
  clearance, redistribution grant, or authorization to distribute. See the
  [A10 archive privacy and licensing decision packet](reviews/V0_1_A10_RAW_ARCHIVE_PRIVACY_LICENSING_REVIEW.md).
- [x] Complete a human review against `docs/THREAT_MODEL.md` and record the
  reviewer and date: Jayesh Suyal, 2026-08-31; see the
  [human security-review approval record](reviews/V0_1_HUMAN_SECURITY_REVIEW_APPROVAL.md).
- [x] Select and add the repository license. The owner selected Apache License
  2.0 for Inferdrome; `LICENSE`, package metadata, and third-party notices
  record that choice without licensing external materials or raw archives.
- [x] Jayesh Suyal authorizes `v0.1.0` as a producer-side release while the
  two ExitSpec items above remain `NOT_RECORDED`; see the
  [owner release-policy exception record](reviews/V0_1_OWNER_RELEASE_POLICY_EXCEPTION.md).

The remaining unchecked items are ExitSpec outcomes and receipt. The
archive-publication decision gate is closed only as a
refusal: the raw archive remains `EXTERNAL_ONLY` and must not be published.
They remain required for independent customer acceptance, but the owner has
authorized the `v0.1.0` producer-side tag while they remain deferred
post-v0.1. The exception neither checks those boxes nor makes an outcome or
receipt exist; the final release record must state their absence.

## Repository machine checks

The release preflight checks these conditions on the current `HEAD`; they are
not manual checklist boxes:

- final phases require both package-version locations to match the phase;
- every phase requires the exact canonical Apache-2.0 repository license and
  matching package/repository metadata;
- every phase requires a present hash-bearing `uv.lock` that matches
  `pyproject.toml`, plus lock-keyed CI cache and non-editable frozen sync;
- final phases verify frozen capture producer commit
  `c08b46d9fbd87477f45d130aa3c63615937c4dc3` is an ancestor of `HEAD`;
- required release files, claim-boundary labels, all three CI jobs, and a clean
  worktree are present; and
- final-pre-tag verifies `v0.1.0` is absent, while post-tag verifies it points
  to the checked `HEAD`.

These checks prove only repository state. The preflight does not aggregate the
three GitHub job results for itself.

## Deferred release-record facts

The exact release SHA, the three aggregate CI job URLs, the authorized
annotated tag, and the post-tag verification result are recorded by the
GitHub/tag/release process. They are not pending inputs for the Engineering
job to prove about itself. The annotated tag message may contain only facts
known at tag creation; the post-tag verification result must not be added to
that tag message.

After post-tag verification, record the post-tag result and its URL in a
GitHub Release or another explicit external immutable release record. It must
not be added to the already-created annotated tag message. Do not change the
repository after tagging solely to populate it.

## Final sign-off record template

The authorized annotated tag message may contain these facts known before
tagging:

```text
Release commit:
Engineering gate run URL(s):
Deployment qualification gate run URL(s):
Dashboard gate run URL(s):
GPU provider and declared instance type:
GPU demonstration bundle digest:
ExitSpec status: DEFERRED POST-v0.1; outcomes and receipt NOT_RECORDED
ExitSpec deferral authorization: docs/reviews/V0_1_OWNER_RELEASE_POLICY_EXCEPTION.md
Security reviewer and date:
Selected license: Apache License 2.0 (`Apache-2.0`)
Release tag: v0.1.0
```

The later GitHub Release or another explicit external immutable release record
may contain the post-tag facts:

```text
Post-tag workflow run URL:
Tag verification:
```

The final external record must also cite the exact release commit whose
final-pre-tag pull-request and `main` workflows passed all three jobs, plus the
post-tag workflow result. The annotated tag itself must not be amended or
recreated to append post-tag facts.
The GPU bundle digest remains an out-of-band evidence anchor. A future ExitSpec
receipt digest must likewise remain out-of-band and may not be reconstructed
from a summary or edited into a sealed bundle; no such digest is recorded for
this release.

## Frozen GPU producer anchors

- archive: 689,272 bytes,
  `sha256:f2408fd0649a7c79f5962872003781ebb9c878b802db27d633cf246f13b6f424`;
- capture manifest:
  `sha256:1d4ea1e251c5a84a104333ab8579d580838701a70cc38b64b68c88f66266e0cb`;
- standalone profile:
  [`profiles/v1/managed-vllm-0.26-evidence-profile.json`](../profiles/v1/managed-vllm-0.26-evidence-profile.json);
- publication review:
  [`evidence/gpu/2026-08-20-a10/publication-review.json`](../evidence/gpu/2026-08-20-a10/publication-review.json),
  status `EXTERNAL_ONLY`; and
- handoff manifest:
  [`evidence/gpu/2026-08-20-a10/handoff-manifest.json`](../evidence/gpu/2026-08-20-a10/handoff-manifest.json).

These anchors close only Inferdrome's producer-side evidence work. They do not
create an ExitSpec receipt, prove pre-measurement contract chronology, approve
public archive delivery, or authorize a release tag.

## Offline candidate and final release procedure

The repository-owned preflight has explicit candidate, final-pre-tag, and
post-tag phases. It is deliberately separate from the release decision and
never creates a tag, publishes an artifact, or approves a release.

### Candidate phase: normal pull requests and `main`

From a clean checkout at a development candidate commit, install the locked
Python and frontend dependencies, then run:

```bash
uv lock --check
uv sync --frozen --extra dev --extra dashboard
npm ci --prefix frontend
npx --prefix frontend playwright install chromium
.venv/bin/python scripts/release_preflight.py \
  --phase candidate --repository-only --require-clean
```

That command must report `REPOSITORY_READY`. Candidate mode requires both the
package metadata and `src/inferdrome/__init__.py` to remain at
`0.1.0.dev0`. Normal pull-request, merge-queue, and `main` CI invoke the
`auto` phase: exact development versions resolve to candidate, while any
other or mismatched versions fail closed. The workflow's manual
`release_phase` input can explicitly run auto, candidate, final-pre-tag, or
post-tag against a deliberately selected ref. Candidate mode does not claim
ExitSpec acceptance, archive publication, security sign-off, licensing
approval for external materials, or final release approval.

### Final pre-tag phase: readiness of the exact release commit

After the remaining ExitSpec blockers are genuinely recorded by their named
owners, or after the bounded owner exception is recorded for this producer-side
`v0.1.0` release, and while the recorded human security review remains
applicable and the archive refusal remains recorded, the release owner creates
the final-version release commit. That commit changes both package-version locations to `0.1.0`
while preserving the canonical Apache-2.0 license, matching metadata, and
current dependency lock. No
`v0.1.0` tag exists yet.

Submit that commit through the branch-protected pull-request path. Its normal
CI invocation uses `--phase auto`, resolves the exact final versions and
missing tag to `final-pre-tag`, and must pass all three required jobs before it
can merge. The resulting `main` push invokes the same `auto` phase and must
also pass all three jobs. Do not merge a red release PR or leave `main` red.

After that green `main` run, run the existing GitHub Actions `CI` workflow
manually against the exact `main` release commit, selecting
`release_phase=final-pre-tag`. Its engineering job invokes this exact
repository-only check:

```bash
python scripts/release_preflight.py \
  --phase final-pre-tag --repository-only --require-clean
```

It must pass the final version, exact license and metadata, Python lock
freshness and hashes, frozen capture producer ancestry, clean-checkout,
claim-boundary, required-file, CI-inventory, and no-existing-tag checks. All
three jobs must pass at that exact commit. The Engineering checkout fetches
the complete repository tag namespace
(`fetch-depth: 0`), so the no-tag check is against actual `refs/tags` rather
than a shallow clone. The aggregate three-job result is recorded by GitHub,
not self-verified by the Engineering preflight. The license check proves the
repository bytes and metadata are the recorded Apache-2.0 choice; it does not
make a legal decision for external materials or authorize archive publication.

For a local release-owner review on the exact final commit, the existing gates
may be delegated without changing their semantics:

```bash
.venv/bin/python scripts/release_preflight.py \
  --phase final-pre-tag --run-gates --require-clean
INFERDROME_PYTHON=.venv/bin/python ./scripts/deployment_qualification_gate.sh
git status --short --branch
git diff --check
```

The preflight delegates engineering and dashboard work to their existing gate
scripts; the deployment qualification job remains the separate local Docker
Compose CI gate because it creates disposable local resources and is not an
offline provider check. Its result remains `SYNTHETIC_ONLY` and does not
replace genuine GPU evidence or ExitSpec review.

### Post-tag phase: verification and external release record

The release sequence ends with the green final-pre-tag pull-request and
`main` SHA, an authorized annotated `v0.1.0` tag at that exact SHA, and the
post-tag workflow against the tag. A commit cannot contain proof of a tag that
does not yet exist, so do not create a repository commit after tagging merely
to record these facts.

After the authorized tag is created and pushed, run the same `CI` workflow
manually against the tag ref with `release_phase=post-tag`, or run locally on
that checked-out tag:

```bash
.venv/bin/python scripts/release_preflight.py \
  --phase post-tag --run-gates --require-clean
```

Post-tag mode requires `v0.1.0` to resolve to the checked `HEAD`, the final
version, exact Apache-2.0 artifact and metadata, a current hash-bearing Python
lock, frozen capture ancestry, and all repository gates. The Engineering
checkout's complete tag visibility allows this
`refs/tags/v0.1.0` resolution to be checked against the actual tag namespace.
After all three jobs pass, put the exact release SHA, three CI run URLs, tag
verification, producer-side limitation, owner-exception source, and remaining
external sign-off in the GitHub Release or another explicit external immutable
release record. Post-tag verification facts belong in that external record and
must never be added to the annotated tag message. This record is not a
substitute for ExitSpec outcomes, human security review, the recorded owner
archive-publication decision, or final release approval.
Publishing and merging are not performed by this preflight.

Automation cannot prove the ExitSpec importer or `PASS`/`FAIL`/`NOT_PROVEN`
outcomes, pre-measurement contract chronology, human security approval, owner
archive-publication decision, licensing or publication rights for external
materials, GitHub check results, or final release approval. Those inputs must
remain explicit checklist evidence; the bounded owner exception changes release
timing only and does not turn missing inputs into Inferdrome evidence.
