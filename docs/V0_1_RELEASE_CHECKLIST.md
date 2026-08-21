# Inferdrome v0.1 release checklist

Status: **GPU producer evidence complete; external acceptance and sign-off
pending**

This checklist maps the frozen
[v0.1 definition of done](V0_1_DEFINITION_OF_DONE.md) to reviewable evidence.
An automated check proves only the boundary it exercises. It cannot substitute
for independent ExitSpec demonstrations, owner license/publication decisions, a
security review, or final release sign-off.

## Automated candidate gates

Every pull request and every update to `main` must run both jobs in
`.github/workflows/ci.yml`:

| Required job | Evidence boundary |
| --- | --- |
| `Engineering gate` | Generated schemas and goldens, static GPU-proof assets, script syntax, Ruff, strict mypy, and the complete Python suite |
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
| 11. Engineering quality | Both CI jobs, packaging smoke, documentation, contribution guidance | License selection and `v0.1.0` tag pending |

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
- [ ] Confirm both required GitHub checks pass at the release commit.
- [ ] Confirm the release commit has no uncommitted or untracked release
  artifacts.
- [ ] Tag that exact commit as `v0.1.0` only after every item above is complete.

## Final sign-off record

Fill this section in one reviewable commit when all blockers are closed:

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

The tag must identify the same commit tested by both required jobs. The GPU
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
