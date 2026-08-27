# Inferdrome v0.1 release checklist

Status: **Repository gates defined; external acceptance and sign-off pending**

This checklist maps the frozen
[v0.1 definition of done](V0_1_DEFINITION_OF_DONE.md) to reviewable evidence.
An automated check proves only the boundary it exercises. It cannot substitute
for independent ExitSpec demonstrations, owner license/publication decisions, a
security review, or final release sign-off.

## Automated candidate gates

Every pull request and every update to `main` must run all three jobs in
`.github/workflows/ci.yml`:

| Required job | Evidence boundary |
| --- | --- |
| `Engineering gate` | Generated schemas and goldens, static GPU-proof assets, script syntax, Ruff, strict mypy, and the complete Python suite |
| `Deployment qualification gate` | Local Docker Compose synthetic qualification, including bounded cleanup and induced-failure diagnostics; no cloud/GPU/evidence claim |
| `Dashboard gate` | TypeScript, frontend unit tests, populated Playwright navigation against the real server, dashboard backend tests, committed production assets, and installed-wheel smoke |

The workflow uses read-only repository permissions, no secrets, bounded job
timeouts, exact-commit pins for every GitHub Action, lockfile installation for
the frontend, and retained Playwright traces, screenshots, and videos on
failure. Normal pull-request CI remains GPU-free.

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
| 8. ExitSpec integration | Independently owned importer, recalculation, decision table, and receipt | External release blocker |
| 9. Flagship demonstration | Managed runbook, exact A10 handoff, corruption and synthetic rejection demos | Inferdrome producer evidence complete; ExitSpec outcomes pending |
| 10. Security and privacy | Threat model, bounded readers, adversarial tests, exact-archive publication review | `EXTERNAL_ONLY`; human security and owner publication review pending |
| 11. Engineering quality | All three CI jobs, packaging smoke, documentation, contribution guidance | License selection and `v0.1.0` tag pending |

## Release-blocking evidence

- [x] Record one newly generated, customer-eligible bundle from a clean,
  compatible Linux/NVIDIA host.
- [x] Record capture producer commit
  `c08b46d9fbd87477f45d130aa3c63615937c4dc3` and reviewed bundle digest
  `sha256:bae216f2165eb06ae2e0f14d3cd852f8e0ebb381bf1f68c71072769b3c0c1675`.
- [ ] Record the eventual release commit and prove it preserves the capture
  producer commit as an ancestor.
- [ ] Independently demonstrate ExitSpec `PASS`, `FAIL`, and `NOT_PROVEN` using
  frozen contracts.
- [ ] Retain the ExitSpec ingestion receipt digest.
- [x] Independently replay and record corrupted-artifact `INTEGRITY_MISMATCH`
  and synthetic-fixture `EVIDENCE_INELIGIBLE` rejection facts.
- [x] Retain the deterministic GPU capability profile, publication review, and
  handoff manifest. The raw archive remains `EXTERNAL_ONLY` and uncommitted.
- [ ] Owner decides whether to approve public delivery of the exact archive
  after license and privacy review.
- [ ] Complete a human review against `docs/THREAT_MODEL.md` and record the
  reviewer and date.
- [ ] Select and add the repository license. This checklist deliberately does
  not make that legal choice on the owner's behalf.
- [ ] Confirm all required GitHub checks pass at the release commit.
- [ ] Confirm the release commit has no uncommitted or untracked release
  artifacts.
- [ ] Tag that exact commit as `v0.1.0` after final-pre-tag CI passes and all
  prior owner/external items are complete; record post-tag verification.

## Final sign-off record

Fill this section in one reviewable post-tag record commit when all blockers
are closed:

```text
Release commit:
Engineering gate run URL:
Dashboard gate run URL:
GPU provider and declared instance type:
GPU demonstration bundle digest:
ExitSpec receipt digest:
Security reviewer and date:
Selected license:
Release tag: v0.1.0
```

The tag must identify the same commit tested by all three required jobs. The GPU
bundle digest and ExitSpec receipt digest remain out-of-band evidence anchors;
neither may be reconstructed from a summary or edited into a sealed bundle.

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
uv sync --extra dev --extra dashboard
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
post-tag against a deliberately selected ref. Candidate mode does not claim ExitSpec
acceptance, archive publication, security sign-off, license selection, or
final release approval.

### Final pre-tag phase: readiness of the exact release commit

After the external and owner-controlled checklist inputs are genuinely closed
except for the release tag and the not-yet-recorded final CI result, the
release owner creates a separate release commit. That commit (which is not
this development-version PR) changes both package-version locations to
`0.1.0` and includes the owner-selected, non-empty repository license artifact.
No `v0.1.0` tag exists yet.

Submit that commit through the branch-protected pull-request path. Its normal
CI invocation uses `--phase auto`, resolves the exact final versions to
`final-pre-tag`, and must pass all three required jobs before it can merge.
The resulting `main` push invokes the same `auto` phase and must also pass all
three jobs; it resolves to `final-pre-tag` because the package is still
untagged. Do not merge a red release PR or leave `main` red.

After that green `main` run, run the existing GitHub Actions `CI` workflow
manually against the exact `main` release commit, selecting
`release_phase=final-pre-tag`. Its engineering job invokes this exact
repository-only check:

```bash
python scripts/release_preflight.py \
  --phase final-pre-tag --repository-only --require-clean
```

It must pass the final version, license-artifact presence, clean-checkout,
claim-boundary, required-file, CI-inventory, and no-existing-tag checks. All
three jobs must pass at that exact commit. The Engineering checkout fetches
the complete repository tag namespace (`fetch-depth: 0`), so the no-tag check
is against the actual `refs/tags` namespace rather than a shallow clone. The
pre-tag phase deliberately
reports the checklist's `release-tag` item as a manual record that follows
this verification; it does not require a tag to exist and therefore does not
prove the tag in advance. The license check proves only that an artifact is
present; the owner remains responsible for choosing and legally approving its
contents.

For a local release-owner review after the three CI results and other manual
inputs have been recorded, the existing gates may be delegated without
changing their semantics:

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

### Post-tag phase: verification and release record

Only after the final-pre-tag checks on the exact `main` commit pass and the
named owners have completed the remaining checklist decisions may the release
owner create and push the annotated `v0.1.0` tag at that exact commit. This is
the first point at which the tag can be checked; a commit cannot contain proof
of a tag that does not yet exist.

Then run the same `CI` workflow manually against the tag ref with
`release_phase=post-tag`, or run locally on that checked-out tag:

```bash
.venv/bin/python scripts/release_preflight.py \
  --phase post-tag --run-gates --require-clean
```

Post-tag mode requires `v0.1.0` to resolve to the checked `HEAD`, the final
version, the license artifact, and all repository gates. The Engineering
checkout's complete tag visibility allows this `refs/tags/v0.1.0` resolution
to be checked against the actual tag namespace. After all three jobs pass,
record the tag and the three CI run URLs in the final sign-off record and
check the `release-tag` item. This is a post-tag verification and record step,
not a substitute for ExitSpec outcomes, human security review, owner archive
approval, or final release approval. Tagging, publishing, and merging are not
performed by this preflight.

Automation cannot prove the ExitSpec importer or `PASS`/`FAIL`/`NOT_PROVEN`
outcomes, pre-measurement contract chronology, human security approval, owner
license choice, owner archive-publication decision, GitHub check results, or
final release approval. Those inputs must remain explicit checklist evidence;
checking a box does not turn them into Inferdrome evidence.
