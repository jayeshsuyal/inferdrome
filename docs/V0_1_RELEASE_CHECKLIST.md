# Inferdrome v0.1 release checklist

Status: **Release shield implemented; external proof and sign-off pending**

This checklist maps the frozen
[v0.1 definition of done](V0_1_DEFINITION_OF_DONE.md) to reviewable evidence.
An automated check proves only the boundary it exercises. It cannot substitute
for the genuine GPU receipt, independent ExitSpec demonstrations, a security
review, or the final release sign-off.

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
| 4. Producer adapters | Fake and pinned-vLLM adapters, capability spike, native goldens, managed-host static check | Automated gate; genuine host receipt pending |
| 5. Canonical request records | Public schema, pinned normalizer, golden and semantic-invariant tests | Automated gate |
| 6. Measurements | Metric definitions, deterministic reducer, quantile tests, conformance vectors | Automated gate |
| 7. Evidence bundle | Bundle contract, offline verifier, immutable publication, mutation suites | Automated gate |
| 8. ExitSpec integration | Independently owned importer, recalculation, decision table, and receipt | External release blocker |
| 9. Flagship demonstration | Managed Linux/NVIDIA runbook, SSH capture controller, corruption and synthetic rejection demos | Genuine compatible-host bundle and ExitSpec `PASS`/`FAIL`/`NOT_PROVEN` pending |
| 10. Security and privacy | `docs/THREAT_MODEL.md`, bounded readers, redaction and adversarial tests | Automated evidence plus human security review pending |
| 11. Engineering quality | Both CI jobs, packaging smoke, documentation, contribution guidance | License selection and `v0.1.0` tag pending |

## Release-blocking evidence

- [ ] Record one newly generated, customer-eligible bundle from a clean,
  compatible Linux/NVIDIA host.
- [ ] Record the exact release commit and reviewed GPU bundle digest.
- [ ] Independently demonstrate ExitSpec `PASS`, `FAIL`, and `NOT_PROVEN` using
  frozen contracts.
- [ ] Retain the ExitSpec ingestion receipt digest.
- [ ] Review the corrupted-artifact and synthetic-fixture rejection outcomes.
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
