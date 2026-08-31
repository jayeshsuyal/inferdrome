# Inferdrome v0.1 model-assisted security audit

## Disposition

Audited repository commit: `c1668e5c7fa53ea08146ee0cd4f045b0b317bac8`

Audit date: 2026-08-31 (America/Los_Angeles)

This report updates the detailed re-audit previously recorded in unmerged commit
`f58e235` and re-checks the current `main` implementation at the exact audited
merge commit above.

Disposition: no remaining repository-owned release-blocking implementation
finding was established by this model-assisted audit at `c1668e5`. That is not
human sign-off, legal advice, compliance certification, provider attestation, a
penetration test, or authorization to release. The blank human security review
item in the v0.1 checklist remains blank and open.

The original findings `F01` through `F14` remain closed on current `main`. The
two residual findings from the prior re-audit are also closed on current
`main`: `N01` was closed by PR #45, merge `15117bb45e0d97549ded08ebbeb338dabc1c5f37`
("Bound live SSH keyscan output"), and `N02` was closed by PR #46, merge
`c1668e5c7fa53ea08146ee0cd4f045b0b317bac8` ("Require explicit SSH identity for
live capture").

No product code, test, schema, evidence, checklist, version, tag, or release
state was changed by this audit beyond recording this one review document.

## Commit and scope binding

- The audit worktree began at the exact requested clean base
  `c1668e5c7fa53ea08146ee0cd4f045b0b317bac8`. `HEAD`, local `main`, and
  `origin/main` all resolved to that SHA before this review write-up was added.
- The review reused `f58e235` as a detailed source document, but conclusions
  were not carried forward blindly. Current symbols, current tests, merged
  repair PRs, and exact `main` CI evidence were re-checked against the current
  tree.
- Scope included re-validation of the original `F01`-`F14` closures; the prior
  residuals `N01` and `N02`; bundle, Trial Set, comparison-plan, archive,
  immutable publication, dashboard/API, SSH, Lambda, GCP, Compose,
  Kubernetes, CI, dependency-lock, licensing, and release-preflight
  repository-owned boundaries.
- Scope excluded live Docker, Kubernetes, provider API, GPU, SSH host,
  credential, billing, publication, tag, and GitHub Release operations. No
  external infrastructure or long end-to-end reruns were performed for this
  update.

## Reconstructed trust boundaries

| Boundary and assets | Untrusted actor or entry point | Implementation-backed controls | Residual or unsupported claim |
|---|---|---|---|
| Bundle, Trial Set, comparison-plan, and handoff intake; canonical evidence and digests | Producer/operator filesystem, imported directories, JSON/JSONL/YAML, archives | Closed schemas; duplicate-key and non-finite-number rejection; depth/token/number/line/member/byte bounds; component-only paths; no-follow and regular-file checks; stable inode/link checks; canonical domain-separated digests; immutable readback | A malicious producer can still fabricate internally consistent pre-receipt evidence; external receipt and independent recalculation remain necessary |
| Evidence authority and comparability | Producer summaries, dashboard requests, cross-run substitution | Full bundle re-verification; exact source/request/workload/execution/plan identities; eligibility and contract gates; fail-closed `INCOMPARABLE`; projections cannot add authority; Inferdrome does not issue ExitSpec verdicts | ExitSpec outcomes, receipt, and contract chronology remain externally owned and are still absent here |
| Local subprocess execution | Repository inputs, executable replacement, inherited environment, child output and descendants | Shell-free argv; validated executable identity; small environments; bounded/process-group execution on hardened runners; timeout/cancel cleanup and redacted diagnostics | Static review plus focused tests do not attest any live external subprocess boundary beyond the local code paths exercised |
| SSH and paid-provider boundary | Operator arguments, remote host, provider responses, interruption | Pinned raw host-key bytes, bounded `ssh-keyscan` discovery, strict host checking, explicit identity requirement, config/agent/proxy/forwarding suppression, exact single-target checks, rate/type/identity validation, watchdog readiness, confirmed-cleanup state | Cost protection is a bounded operational control, not a billing guarantee; provider state and cleanup evidence remain external |
| Dashboard/API and browser | Local browser/client, query parameters, bearer token | Loopback-only bind, optional hashed token keyring with bounded input and timing-safe verification, trusted-host middleware, GET-only evidence routes, bounded pagination/cursors, no-store/security headers, fixed static path, verified projections, raw-content exclusion | No TLS or multi-user authorization; intended only for the documented local boundary; allowlisted environment values remain intentionally verbatim |
| Container/Compose/Kubernetes/GCP qualification | Images, daemon/cluster topology, deployment inputs | Digest-pinned bases/images, non-root and capability restrictions, no socket mounts, internal networks, read-only/tmpfs/resource controls, fixed GPU request, ambient Docker routing rejection, no ambient kubeconfig, explicit synthetic/qualification labels | A local socket may proxy elsewhere; qualification/simulation is not execution or provider attestation; no live boundary was exercised here |
| CI, release, and publication | Pull request code, actions, caches/artifacts, release operator, raw archive | Read-only workflow permissions; commit-pinned actions; locked/hash-bound installs; no `pull_request_target`; candidate/final/tag phase coherence; canonical Apache-2.0 repository license | Dependency advisory reachability was not freshly network-scanned; repository license does not license external archive material; owner publication and immutable external release record remain outstanding |

## Repair and release-coherence PR reconciliation

| PR | Merged repair scope checked at the audited tree | Result |
|---|---|---|
| #40, merge `1b854a2` | Evidence authority, comparability, archive-independent A10 record gate, and immutable publication | Present; closes `F02`, `F03`, `F10`, `F12` and supports the comparison/evidence portions of `F04` and `F11` |
| #41, merge `dee649a` | Ambient Docker routing, CI/lock enforcement, Apache-2.0 repository licensing | Present; closes `F01` and `F09` |
| #42, merge `f57855c` | Early input/work-budget enforcement and parser-domain errors | Present; closes `F04` and `F11` |
| #43, merge `137655a` | Subprocess bounds, signal cleanup, environment/executable hardening, endpoint secrecy, SSH pin/config behavior, dashboard claim correction | Present; closes `F05` through `F08` and `F13` through `F14` at the original-audit scope |
| #44, merge `fff81bd` | Compose package-version flow and candidate/final/tag release coherence | Present; Compose derives `INFERDROME_VERSION` from package metadata and the preflight distinguishes candidate, final-pre-tag, and post-tag states |
| #45, merge `15117bb` | Bound `ssh-keyscan` discovery before host-key pin publication | Present; closes residual `N01` |
| #46, merge `c1668e5` | Require explicit live SSH identity for SSH/SCP transport and capture entry paths | Present; closes residual `N02` |

## Original 14-finding closure table

| ID | Original severity and finding | Current status | Current symbols and tests re-checked on `main` |
|---|---|---|---|
| F01 | HIGH — ambient Docker routing could redirect Kubernetes qualification | **Closed** | `scripts/run_kubernetes_mock_e2e.sh`, `src/inferdrome/kubernetes.py::validate_local_docker_endpoint`, and `tests/unit/test_kubernetes.py::test_fake_wrapper_rejects_ambient_docker_routing_before_any_tool_call`, `test_fake_wrapper_pins_verified_local_endpoint_for_docker_and_kind`, `test_fake_wrapper_forces_docker_kind_provider_and_clears_network_override` |
| F02 | MEDIUM — comparison projection omitted eligibility/contract authority | **Closed** | `src/inferdrome/dashboard/comparison.py`, `src/inferdrome/comparisons/service.py`, `src/inferdrome/trials/service.py`, and `tests/integration/test_controlled_comparisons.py::test_missing_exitspec_identity_suppresses_controlled_outcomes` |
| F03 | MEDIUM — A10 publication status depended on the unavailable raw archive | **Closed for repository metadata; archive review remains external** | `scripts/review_gpu_evidence_publication.py`, `docs/THREAT_MODEL.md`, and `tests/unit/test_gpu_evidence_publication.py::test_publication_decision_table_is_fail_closed`, `test_publication_review_mutations_fail_closed`, `test_publication_review_rejects_integer_to_float_type_drift` |
| F04 | MEDIUM — expensive directory/index work occurred before aggregate bounds | **Closed** | `src/inferdrome/limits.py`, `src/inferdrome/bundle/reader.py`, `src/inferdrome/dashboard/work.py`, and `tests/unit/test_bounded_inputs.py::test_collect_bounded_consumes_only_limit_plus_one_sentinel`, `test_vllm_json_helpers_bound_depth_and_integer_size` |
| F05 | MEDIUM — qualification timeout could leave pipe-holding descendants | **Closed** | `src/inferdrome/deployment/qualification.py` and `tests/unit/test_deployment_qualification.py::test_bounded_subprocess_terminates_timed_out_process`, `test_bounded_subprocess_kills_pipe_holding_descendant_group` |
| F06 | MEDIUM — SIGTERM could bypass capture/provider cleanup | **Closed** | `scripts/capture_real_gpu_over_ssh.py`, `scripts/lambda_gpu_guard.py`, and `tests/unit/test_real_gpu_capture.py::test_sigterm_enters_immediate_guard_cleanup_for_nonprospective_modes`, `test_sigterm_during_guard_arming_is_deferred_until_cleanup_owns_handle`, `test_sigterm_at_cleanup_entry_cannot_bypass_guard_finalization`, `test_sigterm_with_unconfirmed_termination_retains_unresolved_state`; plus `tests/unit/test_lambda_gpu_guard.py::test_failed_termination_request_is_rechecked_before_failure`, `test_deadline_watch_writes_operational_termination_receipt`, `test_unconfirmed_provider_termination_keeps_watchdog_armed` |
| F07 | MEDIUM — ambient credentials and executable resolution reached local children | **Closed** | `src/inferdrome/execution/subprocess_runner.py` and `tests/unit/test_subprocess_runner.py::test_changed_executable_identity_fails_before_launch`, `test_start_observer_receives_isolated_process_identity`, `test_deadline_terminates_process_with_bounded_wait` |
| F08 | MEDIUM — secret-bearing endpoint paths could leak through arguments/records | **Closed** | `src/inferdrome/domain/experiment.py`, `tests/unit/test_resolver.py::test_endpoint_paths_fail_before_workspace_reservation`, `test_resolver_expands_defaults_and_freezes_request_order`, and `tests/unit/test_vllm_adapter.py::test_endpoint_path_is_rejected_before_transport_construction`, `test_endpoint_path_is_rejected_before_argv_or_evidence_serialization` |
| F09 | MEDIUM — CI could run without proving the dependency lock | **Closed** | `.github/workflows/ci.yml`, `scripts/release_preflight.py`, and `tests/unit/test_release_preflight.py` lock/CI inventory coverage including `_check_ci_gate_inventory` and repeated phase checks |
| F10 | LOW — Trial Set imports accepted hard-linked or extra entries | **Closed** | `src/inferdrome/trials/service.py`, `tests/dashboard/test_trial_service.py::test_verification_requires_immutable_trial_set_directory`, `test_verification_bounds_undeclared_trial_set_directory_entries`, and `tests/unit/test_trial_set_contract.py::test_trial_set_rejects_duplicate_member_identity` |
| F11 | LOW — parser edge failures escaped the public domain error | **Closed** | `src/inferdrome/parsing.py`, `src/inferdrome/bundle/reader.py`, `src/inferdrome/kubernetes.py`, and `tests/unit/test_parser_domain_errors.py::test_parser_failure_cannot_reserve_a_partial_run_workspace`, `tests/unit/test_kubernetes.py::test_yaml_parser_rejects_duplicate_nested_keys_extra_documents_and_bounds` |
| F12 | LOW — immutable publication could replace an existing name | **Closed** | `src/inferdrome/immutable.py`, `src/inferdrome/bundle/writer.py`, and `tests/unit/test_immutable_publication.py::test_publication_freezes_one_complete_descriptor_directory`, `test_failed_private_stage_cannot_poison_public_identity_and_retry_succeeds`, `test_publication_rejects_unsafe_path_components` |
| F13 | INFORMATIONAL — dashboard “display redaction” claim lacked implementation | **Closed by correcting the claim and testing the actual boundary** | `docs/DASHBOARD.md`, `src/inferdrome/dashboard/projection.py`, `src/inferdrome/dashboard/api.py`, and the dashboard gate recorded in exact-tree CI run `33367230313` |
| F14 | LOW — SSH host pinning and ambient agent/config suppression did not match claims | **Closed** | `scripts/capture_real_gpu_over_ssh.py::_prepare_pinned_known_hosts`, `_ssh_options`, `_scp_options`, and `tests/unit/test_real_gpu_capture.py::test_mismatched_host_pin_fails_before_ssh_or_scp`, `test_ssh_transport_ignores_user_config_and_disables_forwarding` |

## Prior residuals now closed

### N01 — closed on current `main`

The prior re-audit identified unbounded output before SSH host-key pin
validation when the optional discovery path used `ssh-keyscan`. That path is
now bounded on current `main`.

- Merge that closed the issue: PR #45, merge
  `15117bb45e0d97549ded08ebbeb338dabc1c5f37`.
- Current implementation: `scripts/capture_real_gpu_over_ssh.py::_prepare_pinned_known_hosts`
  now routes discovery through `_run_bounded_capture` with explicit
  `stdout_limit`, `stderr_limit`, and timeout before any `known_hosts`
  publication.
- Current tests: `tests/unit/test_real_gpu_capture.py::test_live_keyscan_overflow_fails_before_host_pin_publication`
  and `test_live_keyscan_accepts_bounded_pinned_host_key_bytes`.

No current-code evidence in this tree re-established the prior `N01` behavior.

### N02 — closed on current `main`

The prior re-audit identified a legacy live-SSH path that could consult default
identity files when the operator omitted `--identity-file`. Current `main`
requires an explicit identity before live SSH/SCP transport or live capture
continues.

- Merge that closed the issue: PR #46, merge
  `c1668e5c7fa53ea08146ee0cd4f045b0b317bac8`.
- Current implementation: `scripts/capture_real_gpu_over_ssh.py::_require_identity`,
  `_ssh_options`, and `_scp_options` fail closed when the identity is omitted.
- Current tests: `tests/unit/test_real_gpu_capture.py::test_ssh_transport_ignores_user_config_and_disables_forwarding`,
  `test_ssh_and_scp_options_reject_omitted_identity`,
  `test_identity_file_must_be_explicit_regular_and_non_symlink`, and
  `test_every_capture_mode_rejects_omitted_identity_before_action`.

No current-code evidence in this tree re-established the prior `N02` behavior.

## Current findings

No BLOCKER, HIGH, MEDIUM, or LOW repository-owned implementation finding was
established by this model-assisted audit at
`c1668e5c7fa53ea08146ee0cd4f045b0b317bac8`.

That conclusion is intentionally narrow. It does not close the separately owned
human security review, ExitSpec evidence, owner publication/privacy/licensing
decisions for external materials, live provider cleanup proof, version/tag
transition, or release-record work described below.

## Evidence integrity, privacy, and authority conclusions

- Mutation, missing artifacts, unsupported schema/profile/provenance,
  synthetic adapters, incomplete populations, stale metadata, and
  cross-run/cross-contract substitution continue to fail closed in the bundle,
  Trial Set, comparison, publication, and dashboard paths re-checked here.
- Dashboard/API projection retains the verified bundle's authority and
  eligibility. It does not create customer eligibility, comparability, or an
  ExitSpec verdict. Inferdrome still does not issue ExitSpec `PASS`, `FAIL`, or
  `NOT_PROVEN`.
- Canonical request, workload, source, execution, bundle, comparison-plan, and
  comparison-result identities remain domain-separated and checked on read.
  Immutable publication remains exclusive-placement plus readback.
- The tracked A10 review and handoff records remain bound to
  `EXTERNAL_ONLY`, outstanding owner approval, retrospective chronology, and
  null producer-side ExitSpec authority. This review did not and cannot imply a
  fresh byte-level raw-archive re-review.
- Apache-2.0 covers Inferdrome repository material only. It does not resolve
  licensing or publication rights for the archive-bound model, workload, vLLM,
  or generated output.

## Validation record

Local tool versions used for this update included Git 2.50.1 (Apple
Git-155), Python 3.12.12 for repository scripts, GNU Bash 3.2.57, and GitHub
CLI 2.86.0.

| Command or evidence | Result |
|---|---|
| `git rev-parse HEAD` plus current branch/base inspection | Exact audited SHA confirmed: `c1668e5c7fa53ea08146ee0cd4f045b0b317bac8` |
| `git diff --check` | Pass |
| `python3.12 scripts/release_preflight.py --phase candidate --repository-only --require-clean` | `REPOSITORY_READY`. PASS: required files, package version, license artifact, Python dependency lock, claim boundaries, CI gate inventory, working tree. SKIPPED: engineering and dashboard delegated gates. PENDING: ExitSpec outcomes, ExitSpec receipt, archive publication, human security review. MANUAL: recorded repository-owner license selection |
| GitHub Actions run `33367230313` for exact `main` head `c1668e5c7fa53ea08146ee0cd4f045b0b317bac8` | Green from `2026-08-31T07:10:47Z` to `2026-08-31T07:15:10Z` on workflow `.github/workflows/ci.yml` |
| Job `99410293234` ("Engineering gate") | Success in 4m19s |
| Job `99410293490` ("Dashboard gate") | Success in 1m58s |
| Job `99410293450` ("Deployment qualification gate") | Success in 44s |
| Current `main` implementation for former residuals | `N01` closure re-checked in `scripts/capture_real_gpu_over_ssh.py::_prepare_pinned_known_hosts` and `tests/unit/test_real_gpu_capture.py::test_live_keyscan_overflow_fails_before_host_pin_publication`; `N02` closure re-checked in `_require_identity`, `_ssh_options`, `_scp_options`, and their exact transport/capture omission tests |

The complete engineering, dashboard, and deployment jobs were not rerun locally
for this documentation-only audit update. That was intentional: the exact same
tree already has the recorded green merged-`main` CI evidence above, and this
task was constrained to safe diff/preflight checks without long reruns or
external infrastructure.

## Limitations and remaining release blockers

- No Docker daemon, Compose runtime, Kubernetes cluster, provider API, GPU, SSH
  host, credential, paid resource, or live dashboard/browser boundary was used.
  Static review, exact-tree CI evidence, and safe local preflight checks do not
  attest those external systems.
- ExitSpec outcomes remain external and release-blocking. This review did not
  produce, import, or verify independent ExitSpec `PASS`, `FAIL`, or
  `NOT_PROVEN` demonstrations.
- The ExitSpec ingestion receipt remains external and release-blocking. No
  receipt digest was produced or verified by this task.
- The actual raw archives remain `EXTERNAL_ONLY`. Their byte-level privacy,
  licensing, publication authorization, and public-release decision remain an
  owner decision outside this repository audit.
- Actual provider, SSH, GPU, billing, and cleanup evidence remains external.
  Repository controls and tests do not prove a live host existed, stayed within
  budget, or was fully cleaned up in the provider.
- The blank human review fields below remain intentionally blank. This report is
  not a substitute for a named reviewer, a review date, or a human release
  decision.
- Candidate metadata correctly remains `0.1.0.dev0` in both version locations
  (`pyproject.toml` and `src/inferdrome/__init__.py`). The future transition to
  exact `0.1.0`, green final-pre-tag CI on that exact release commit,
  authorized annotated `v0.1.0` tag creation, post-tag verification, and the
  GitHub Release or equivalent immutable external release record remain future
  release-owner actions.
- This report does not merge, tag, publish, or authorize release. It records
  repository-owned audit conclusions and leaves external/manual blockers
  explicit.

## Human review (blank; not completed by the model)

Reviewer:

Date:

Decision:
