# Inferdrome v0.1 model-assisted security audit closure

## Disposition

**Recommendation:** `MODEL_AUDIT_NO_REPOSITORY_OWNED_RELEASE_BLOCKER`

Audited repository commit: `c1668e5c7fa53ea08146ee0cd4f045b0b317bac8`

Audit date: 2026-08-31 (America/Los_Angeles)

This is an independent, model-assisted technical review of Inferdrome. It is
not a human security sign-off, legal opinion, compliance certification,
penetration test, provider attestation, or authorization to tag and publish
`v0.1.0`.

No remaining repository-owned release-blocking implementation finding was
established by this audit on the exact merged `main` commit above. The original
findings `F01` through `F14` remain closed in the current tree. The two later
findings raised during re-audit, `N01` and `N02`, are also closed in merged
repairs PR #45 (`15117bb45e0d97549ded08ebbeb338dabc1c5f37`) and PR #46
(`c1668e5c7fa53ea08146ee0cd4f045b0b317bac8`).

This report does not change product code, schemas, tests, evidence, release
versioning, tags, cloud state, provider state, or spending state.

## Scope and trust boundaries

- Exact current-tree review of Inferdrome only, bound to commit `c1668e5`.
- Reconciliation of the previously closed release findings, the late `ssh-keyscan`
  bounded-output repair, and the explicit live-SSH identity repair.
- Review of repository-owned boundaries: bundle/trial/comparison intake,
  immutable publication, dashboard/API projection, local subprocesses, SSH
  transport construction, release preflight, CI integrity, and repository
  licensing state.
- Excluded on purpose: ExitSpec changes, live GPU/provider/cloud/SSH activity,
  Docker/Kubernetes execution, credential use, spending, public publication, and
  tag creation.

## Closure table

| ID | Status on `c1668e5` | Current evidence |
|---|---|---|
| F01 | Closed | `scripts/run_kubernetes_mock_e2e.sh` rejects ambient Docker routing before use; `src/inferdrome/kubernetes.py` accepts only canonical local socket forms; covered by `tests/unit/test_kubernetes.py`. |
| F02 | Closed | `src/inferdrome/dashboard/comparison.py`, `src/inferdrome/comparisons/service.py`, and `src/inferdrome/trials/service.py` preserve verified eligibility and contract identity before exposing comparison outcomes. |
| F03 | Closed for repository metadata; archive bytes still external | `scripts/review_gpu_evidence_publication.py` and `docs/THREAT_MODEL.md` keep the tracked A10 record strict and explicitly `EXTERNAL_ONLY` when the raw archive is absent. |
| F04 | Closed | Early bounded-work reservation remains in `src/inferdrome/limits.py`, `src/inferdrome/bundle/reader.py`, and `src/inferdrome/dashboard/work.py`. |
| F05 | Closed | `src/inferdrome/deployment/qualification.py` drains bounded output and kills process groups on timeout; regression coverage remains in `tests/unit/test_deployment_qualification.py`. |
| F06 | Closed | `scripts/capture_real_gpu_over_ssh.py` and `scripts/lambda_gpu_guard.py` preserve cleanup paths and unresolved-termination reporting under interruption. |
| F07 | Closed | `src/inferdrome/execution/subprocess_runner.py` keeps shell-free argv, executable identity checks, bounded diagnostics, and process-group cleanup; covered in `tests/unit/test_subprocess_runner.py`. |
| F08 | Closed | `src/inferdrome/domain/experiment.py` rejects userinfo/query/fragment-bearing endpoints; resolver and adapter coverage remains in `tests/unit/test_resolver.py` and `tests/unit/test_vllm_adapter.py`. |
| F09 | Closed | `.github/workflows/ci.yml` remains commit-SHA pinned and read-only; `scripts/release_preflight.py` verifies lock/version discipline. |
| F10 | Closed | `src/inferdrome/trials/service.py` rejects extra entries, hard-link tricks, and identity drift; covered by `tests/dashboard/test_trial_set_import_security.py`. |
| F11 | Closed | `src/inferdrome/parsing.py` and `src/inferdrome/bundle/reader.py` keep duplicate-key, non-finite-number, and oversized/deep-value failures inside the public error boundary. |
| F12 | Closed | `src/inferdrome/immutable.py` and `src/inferdrome/bundle/writer.py` retain exclusive publication plus readback verification. |
| F13 | Closed | `docs/DASHBOARD.md` now states the real projection boundary, and `src/inferdrome/dashboard/projection.py` continues to exclude raw prompts/responses while intentionally exposing allowlisted values verbatim. |
| F14 | Closed | Raw-byte host-key pinning plus `ssh`/`scp` config suppression remain in `scripts/capture_real_gpu_over_ssh.py`; option construction is regression-covered in `tests/unit/test_real_gpu_capture.py`. |
| N01 | Closed | `scripts/capture_real_gpu_over_ssh.py:101-102,360-410,1902-1939` now bounds `ssh-keyscan` stdout and stderr independently, kills the isolated process group on overflow/timeout, redacts diagnostics, and refuses publication before pin verification; covered by `tests/unit/test_real_gpu_capture.py:245-347`. |
| N02 | Closed | `scripts/capture_real_gpu_over_ssh.py:1292-1381,5077` now requires an explicit regular, non-symlink identity for every live SSH/SCP transport path and always emits exactly one `-i` with `IdentitiesOnly=yes`; covered by `tests/unit/test_real_gpu_capture.py:132-200,411`. |

## Exact late-finding closure notes

### N01: bounded `ssh-keyscan` output before host-pin validation

The earlier issue was real: optional host-key discovery used a network-facing
child process before pin verification and previously buffered untrusted output
without explicit byte caps. The merged repair closes that boundary in the
current tree by:

- setting `_MAX_PINNED_KNOWN_HOSTS_BYTES = 1_048_576` and
  `_MAX_KEYSCAN_DIAGNOSTIC_BYTES = 8_192`;
- streaming stdout and stderr under separate caps in an isolated process group;
- killing the entire group before completion on overflow or timeout;
- redacting captured diagnostics before surfacing them; and
- refusing to write `known_hosts` bytes unless the bounded content survives to
  raw-byte digest verification.

This closure is implemented in PR #45 and present on `main` at merge commit
`15117bb45e0d97549ded08ebbeb338dabc1c5f37`.

### N02: explicit live SSH identity selection

The earlier residual was also real: in preserved legacy live-capture flows, an
omitted identity could have allowed OpenSSH to consult built-in default private
key locations even with `/dev/null` config and `IdentityAgent=none`.

The merged repair closes that boundary in the current tree by:

- making `_require_identity(None)` fail closed;
- rejecting `identity=None` in both `_ssh_options` and `_scp_options`;
- requiring an explicit regular, non-symlink local key path before live
  transport construction; and
- preserving the existing exception for `--check` and offline finalization
  paths, which do not initiate live SSH/SCP activity.

This closure is implemented in PR #46 and present on `main` at merge commit
`c1668e5c7fa53ea08146ee0cd4f045b0b317bac8`.

## Validation record

The report is bound to the current merged `main` tree and the exact GitHub
Actions evidence for that tree.

| Evidence | Result |
|---|---|
| `git rev-parse HEAD` on local `main` before the report branch | `c1668e5c7fa53ea08146ee0cd4f045b0b317bac8` |
| `python3.12 scripts/release_preflight.py --phase candidate --repository-only --require-clean` | Passed with result `REPOSITORY_READY` on the exact candidate repository boundary; confirms candidate-phase versioning, required files, license presence, and repository-owned release inputs without touching providers |
| `git diff --check` on the report branch | Pass |
| Exact merged-main CI run `33367230313` for head `c1668e5c7fa53ea08146ee0cd4f045b0b317bac8` | All green: Engineering gate `99410293234` in 4m19s, Dashboard gate `99410293490` in 1m58s, Deployment qualification gate `99410293450` in 44s |
| PR #45 merged-main review | Repair reviewed, CI green, and later red-team follow-up reported `RED_TEAM_NO_RELEASE_BLOCKING_FINDINGS` for that exact merge |
| PR #46 merged-main review | Identity repair reviewed, exact merged-main CI green, and repository preflight on merged `main` reported `REPOSITORY_READY` |

The failed `python3` invocation during this report pass was an interpreter
mismatch on the local shell, not a repository defect: this project targets the
Python 3.12 runtime and `scripts/release_preflight.py` imports `tomllib`, which
is correct for that pinned version boundary.

## Residual truths and manual or external blockers

No repository-owned implementation blocker remains established by this
model-assisted audit. That does not mean the public `v0.1.0` release is fully
complete today.

The following items remain real release blockers or sign-off requirements
outside this report:

- Human security review is still required. This document is not that signature.
- ExitSpec prospective outcomes remain external: `PASS`, `FAIL`, and
  `NOT_PROVEN` are not produced by Inferdrome.
- The ExitSpec ingestion receipt digest remains externally owned and still must
  be retained separately.
- The raw A10 archive remains `EXTERNAL_ONLY`; privacy, licensing, and
  publication approval for the exact bytes remain an owner decision.
- Live provider, SSH, GPU, cleanup, and cost evidence remain operational and
  external; this report did not exercise those systems.
- Candidate versioning correctly remains `0.1.0.dev0` in
  `src/inferdrome/__init__.py:3`; the deliberate transition to `0.1.0` has not
  happened yet.
- Final-pre-tag CI, authorized annotated tag creation, post-tag verification,
  and the GitHub Release are still future release-owner actions.

## Release-readiness conclusion

Inferdrome is in a strong release-candidate state on the repository boundary.
The codebase is green on the exact merged `main` commit, the late SSH hardening
repairs are merged and verified, the repository license is selected as
Apache-2.0, the package version is still correctly marked as a candidate
(`0.1.0.dev0`), and the remaining blockers are now manual or externally owned
rather than unresolved repository security defects.

That means the repository is ready for the final human review and the remaining
external release steps. It does **not** mean Inferdrome should already be tagged
`v0.1.0` before those blockers are intentionally closed.

## Human review

Reviewer:

Date:

Decision:
