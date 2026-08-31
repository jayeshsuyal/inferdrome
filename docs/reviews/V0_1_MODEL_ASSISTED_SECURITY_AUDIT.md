# Inferdrome v0.1 model-assisted security and privacy closure review

## Disposition

**Repository-finding disposition:** `MODEL_ASSISTED_REPOSITORY_FINDINGS_CLOSED`

**Release disposition:** `RELEASE_BLOCKED_ON_EXTERNAL_HUMAN_AND_FINAL_RELEASE_GATES`

Audited repository commit: `c1668e5c7fa53ea08146ee0cd4f045b0b317bac8`

Audit closure date: 2026-08-31 (America/Los_Angeles)

This model-assisted review found **no remaining repository-owned,
release-blocking implementation finding** in the examined tree. The original
F01--F14 findings and both later SSH findings (N01 and N02) are closed in the
repository at this commit. That conclusion is limited to the code, tests,
tracked records, and CI evidence identified below.

This is **not** human security sign-off, a legal opinion, compliance
certification, penetration test, provider attestation, ExitSpec decision, or
authorization to release. It does not satisfy the release checklist's human
review item and does not complete any external/manual gate.

## Commit, scope, and evidence binding

- The closure branch was reset from a clean worktree after `git fetch origin
  main`; local `main`, `origin/main`, and `HEAD` each resolved to
  `c1668e5c7fa53ea08146ee0cd4f045b0b317bac8` before this report was created.
  This is PR #46's merge commit, with parents
  `15117bb45e0d97549ded08ebbeb338dabc1c5f37` and
  `8ebdd80c0c8d93de276dee30ddbb07823c007f56`.
- The source re-audit record was commit
  `f58e2353801057cf3c9d5d6c159079c97fa6f041` (parent
  `fff81bdfe97c1aa2f060a951e743a65da9ea372d`). It closed F01--F14 on the
  PR #44 tree, found N01, and recorded N02 as a reachable legacy identity
  fallback. This report revalidates the repaired current tree rather than
  treating the prior conclusion as current evidence.
- Scope was repository-only: trust boundaries around evidence import and
  publication, comparison authority, local subprocesses, SSH transport,
  provider guards, dashboard/API, container/Kubernetes/GCP contracts, CI,
  dependency locking, license, and the v0.1 release procedure. No product
  code, test, schema, workflow, package version, tag, release, retained raw
  archive, provider, SSH, GPU, Docker, Kubernetes, or cloud state was changed
  or exercised by this review.
- Exact merged-main CI for this commit was green: [run
  33367230313](https://github.com/jayeshsuyal/inferdrome/actions/runs/33367230313)
  completed Engineering (4m19s), Dashboard (1m58s), and Deployment
  qualification (44s) successfully. It is candidate/main CI, not final-pre-tag
  or post-tag release evidence.

## Repair and CI provenance

| Repair PR | Findings reconciled | Head / merged `main` commit | CI evidence |
| --- | --- | --- | --- |
| [#40](https://github.com/jayeshsuyal/inferdrome/pull/40) | F02, F03, F10, F12, F13 | `e695c38717124ea72f5c86e5c941b759d1e0dd89` / `1b854a2ba117ab7b819ecd8b953faf9b3cf2e819` | [run 33356032997](https://github.com/jayeshsuyal/inferdrome/actions/runs/33356032997), green |
| [#41](https://github.com/jayeshsuyal/inferdrome/pull/41) | F01, F09 | `77a379ec4fee60961998e83a35b7a0e41575f481` / `dee649ad6aa9176585bddc4dbc18cdeb10b21e2c` | [run 33352980732](https://github.com/jayeshsuyal/inferdrome/actions/runs/33352980732), green |
| [#42](https://github.com/jayeshsuyal/inferdrome/pull/42) | F04, F11 | `912b7a62074fe93dfa40a4c4e43a34b155f6c122` / `f57855c1a3f2a993de1228327c60e6215de3aae1` | [run 33360058326](https://github.com/jayeshsuyal/inferdrome/actions/runs/33360058326), green |
| [#43](https://github.com/jayeshsuyal/inferdrome/pull/43) | F05, F06, F07, F08, F14 | `7624955a820607ac3681792b5a2ed73a588b5447` / `137655aace7e71ce0edddd312b0fccf927025990` | [run 33355816643](https://github.com/jayeshsuyal/inferdrome/actions/runs/33355816643), green |
| [#44](https://github.com/jayeshsuyal/inferdrome/pull/44) | Release-version/tag-record coherence supporting the closure review | `f533702e4280b3ccbb87f15a55e9ee370bdc01f4` / `fff81bdfe97c1aa2f060a951e743a65da9ea372d` | [run 33361964299](https://github.com/jayeshsuyal/inferdrome/actions/runs/33361964299), green |
| [#45](https://github.com/jayeshsuyal/inferdrome/pull/45) | N01 | `eb8b1d6eac9e3a2e18f05ad79ea624de763f530f` / `15117bb45e0d97549ded08ebbeb338dabc1c5f37` | [run 33364104863](https://github.com/jayeshsuyal/inferdrome/actions/runs/33364104863), green; its merged-main successor is included in run 33367230313 |
| [#46](https://github.com/jayeshsuyal/inferdrome/pull/46) | N02 | `8ebdd80c0c8d93de276dee30ddbb07823c007f56` / `c1668e5c7fa53ea08146ee0cd4f045b0b317bac8` | [run 33366892898](https://github.com/jayeshsuyal/inferdrome/actions/runs/33366892898), green; exact merged-main run 33367230313 green |

PR #44's head-to-merge and PR #46's head-to-merge tree comparisons were empty;
the cited PR-head checks therefore applied to the same trees merged by those
PRs. The current merged-main CI exercised the complete current tree.

## Finding-by-finding closure

The remediation locations below are current paths and stable symbols; line
references are included where the boundaries are particularly important.
"Closed" means the original repository-owned defect has an implementation and
test/gate basis. It does not turn a residual operational limitation into a
release approval.

| ID | Severity and finding | Remediation and current evidence | Tests/gates and residual risk | Closure state |
| --- | --- | --- | --- | --- |
| F01 | **HIGH** — ambient Docker routing could redirect Kubernetes qualification. | `scripts/run_kubernetes_mock_e2e.sh:50-54` rejects then clears ambient `DOCKER_HOST`/`DOCKER_CONTEXT`; `:106-113` validates and pins the active endpoint before image/kind work. `inferdrome.kubernetes.validate_local_docker_endpoint` (`src/inferdrome/kubernetes.py:102-152`) accepts only canonical local Unix/npipe transports. | `test_fake_wrapper_rejects_ambient_docker_routing_before_any_tool_call`, `test_fake_wrapper_rejects_unverified_context_before_image_or_kind_actions`, and `test_fake_wrapper_pins_verified_local_endpoint_for_docker_and_kind` prove order with fake tools. A local socket can still front a proxy; this is routing qualification, not daemon or workload attestation. | **Closed** |
| F02 | **MEDIUM** — comparison projection omitted eligibility/contract authority. | `dashboard.comparison._hard_incompatibilities` and `compare_runs` (`src/inferdrome/dashboard/comparison.py:49-290`) suppress deltas unless both runs are customer eligible and share an ExitSpec contract identity. `trials.service._comparison_authority` and `comparisons.service._controlled_outcome_authority_satisfied`/`_evaluate_result` require the same authority for controlled outcomes and publish `INCOMPARABLE` with no outcomes otherwise. | Dashboard and integration regressions `test_ineligible_evidence_is_incomparable_and_suppresses_deltas`, `test_cross_contract_evidence_is_incomparable_and_suppresses_deltas`, `test_ineligible_evidence_suppresses_controlled_outcomes`, and `test_missing_exitspec_identity_suppresses_controlled_outcomes`; exact-main dashboard CI passed. ExitSpec authority itself remains separately owned and absent. | **Closed** |
| F03 | **MEDIUM** — A10 publication status depended on unavailable raw archive bytes. | `scripts/review_gpu_evidence_publication.py` stable symbols `validate_publication_review`, `validate_handoff_manifest`, and `check_committed_records` (`:1016-1328`) strictly pin tracked review/handoff/profile/schema identities, `EXTERNAL_ONLY`, null producer/Inferdrome acceptance authority, and outstanding owner approval. `scripts/engineering_gate.sh:36` always runs the records gate. | `test_committed_record_gate_rejects_duplicate_keys`, `test_committed_record_gate_rejects_noncanonical_numeric_lexemes`, and `test_committed_record_gate_rejects_self_consistent_state_mutation`; local `--check-records` passed. The raw archive was not present or byte-re-reviewed; metadata validation neither authorizes publication nor substitutes for archive privacy/licensing review. | **Closed for repository metadata** |
| F04 | **MEDIUM** — expensive directory/index work could occur before aggregate bounds. | `inferdrome.limits.collect_bounded` and `WorkBudget.reserve` (`src/inferdrome/limits.py:11-99`) enforce sentinel, unit, byte, and deadline reservations before downstream work. `BundleReader` and `DashboardWorkController` propagate bounded snapshot work (`bundle/reader.py`, `dashboard/work.py:13-185`). | `test_collect_bounded_consumes_only_limit_plus_one_sentinel`, `test_work_budget_accepts_exact_unit_and_byte_limits_then_fails_atomically`, `test_jsonl_over_cap_is_rejected_before_any_record_is_parsed`; dashboard work-limit regressions are covered by exact-main Dashboard CI. Ceilings intentionally reject large inputs earlier; cooperative deadlines may allow one already-started bounded unit to finish. | **Closed** |
| F05 | **MEDIUM** — qualification timeout could leave pipe-holding descendants. | `deployment.qualification.BoundedSubprocessRunner.run`, `_has_live_execution`, and `_terminate` (`src/inferdrome/deployment/qualification.py:429-697`) use one hard deadline, isolated process groups, bounded drain/join, then TERM/KILL escalation. | `test_bounded_subprocess_terminates_timed_out_process` and `test_bounded_subprocess_kills_pipe_holding_descendant_group`; exact-main Deployment qualification CI passed. Coverage is synthetic/local; it does not attest a live Compose daemon or external cleanup. | **Closed** |
| F06 | **MEDIUM** — SIGTERM could bypass capture/provider cleanup. | `capture_real_gpu_over_ssh._guarded_interruptible`, `_guarded_cleanup_boundary`, `_capture_interrupt_boundary`, and `_terminate_guarded_capture` route interrupts through cleanup; `lambda_gpu_guard.terminate_guarded_instance` and `retain_unresolved_termination` distinguish confirmed termination from unresolved state. | Signal/cleanup regressions including `test_sigterm_enters_immediate_guard_cleanup_for_nonprospective_modes`, `test_repeated_sigterm_cannot_interrupt_guard_finalization`, and `test_sigterm_with_unconfirmed_termination_retains_unresolved_state`, plus Lambda guard tests. Provider termination, billing, and watchdog results were not live-verified here. | **Closed** |
| F07 | **MEDIUM** — ambient credentials and executable resolution reached local children. | `execution.subprocess_runner.resolve_executable_identity`, `_minimal_process_environment`, and `run_captured_process` validate executable identity, use shell-free argv/minimal environments, bound capture, and terminate groups. SSH local helpers use `_local_executable`/`_local_child_environment` (`capture_real_gpu_over_ssh.py:439-475`); qualification has its private environment. | `test_default_environment_does_not_inherit_ambient_values`, `test_changed_executable_identity_fails_before_launch`, `test_parent_cannot_leave_pipe_holding_descendants_running`, and `test_local_helpers_drop_provider_and_agent_environment`. A compromised allowed local executable or OS remains outside this repository boundary. | **Closed** |
| F08 | **MEDIUM** — secret-bearing endpoint paths could leak through args or evidence. | `domain.experiment._endpoint_input_must_be_root`, `_endpoint_must_be_secret_free_root`, and `validated_endpoint_base_url` (`src/inferdrome/domain/experiment.py:47-107`) reject userinfo, query, fragment, and non-root paths before binding. Adapter revalidation prevents bypasses. | `test_secret_bearing_urls_fail_without_echoing_secret`, `test_endpoint_paths_fail_before_workspace_reservation`, `test_endpoint_path_is_rejected_before_transport_construction`, and `test_builder_rechecks_unvalidated_endpoint_path`. Endpoint host identity remains intentionally represented by a digest; downstream providers can still emit sensitive data outside this validation boundary. | **Closed** |
| F09 | **MEDIUM** — CI could run without proving the dependency lock. | `.github/workflows/ci.yml` has read-only permissions, full-SHA actions, frozen bootstrap, and lock-keyed caches. `release_preflight._check_python_dependency_lock` and `_check_ci_gate_inventory` enforce package/lock identity, hashed registry artifacts, no mutable pip route, and CI inventory. Canonical Apache-2.0 package/license checks are in the same preflight. | `test_missing_uv_lock_fails_closed`, dependency/CI-control drift tests, and tag/phase regressions in `tests/unit/test_release_preflight.py`; local preflight reported 56 registry packages and 269 SHA-256-bound artifacts. Lock integrity is not a current vulnerability-advisory scan; repository Apache-2.0 does not license external materials. | **Closed** |
| F10 | **LOW** — Trial Set imports accepted hard links or extra entries. | `trials.service._assert_single_trial_descriptor`, `_load_descriptor`, and `verify_trial_set` (`src/inferdrome/trials/service.py`) require one direct regular descriptor, no extra sibling, one hard link, no-follow access, and stable path/handle identities. | `test_import_rejects_hardlinked_descriptor`, `test_import_rejects_undeclared_sibling`, `test_import_rejects_descriptor_replacement_during_read`, and `test_import_rejects_directory_path_replacement_during_read`; exact-main dashboard CI covers this optional dashboard suite. Same-user hostile or distributed-filesystem races remain an OS/filesystem residual. | **Closed** |
| F11 | **LOW** — parser-edge failures escaped stable public domain errors. | `inferdrome.parsing` bounded JSON/YAML lexical validators, `bundle.reader.strict_json_value`/`strict_jsonl_lines` (`bundle/reader.py:303-389`), and `resolution.yaml_loader.load_strict_yaml` reject duplicate keys, non-finite values, depth/token/integer overage, and normalize parser exceptions to domain errors. | `test_json_boundaries_raise_only_stable_domain_errors`, `test_yaml_boundaries_raise_only_stable_domain_errors`, `test_parser_failure_cannot_reserve_a_partial_run_workspace`, and bounded-input tests. Limits are deliberate availability policy and may reject otherwise parseable large documents. | **Closed** |
| F12 | **LOW** — immutable publication could replace an existing name. | `immutable._rename_no_replace` and `publish_immutable_directory` (`src/inferdrome/immutable.py:109-207`) use platform no-replace primitives and identity/readback checks; `bundle.writer.seal_bundle` publishes through that boundary. | `test_existing_artifact_is_never_replaced`, `test_darwin_no_replace_has_no_check_then_rename_window`, and bundle sealing regressions. Platform/filesystem support is deliberately fail-closed; no claim is made against every hostile same-user or distributed-filesystem mutation. | **Closed** |
| F13 | **INFORMATIONAL** — dashboard "display redaction" claim had no implementation. | The disclosure contract in `docs/DASHBOARD.md` now states that raw prompts/responses are excluded while allowlisted environment values are intentionally verbatim. `dashboard.projection._context` implements the documented projection boundary. | `test_projections_never_include_native_or_canonical_response_bodies`, `test_attached_endpoint_is_exposed_only_as_an_identity_digest`, and `test_allowlisted_environment_values_are_intentional_verbatim_disclosures`; exact-main Dashboard CI passed. Allowlisted values remain a deliberate browser-disclosure decision, not redaction. | **Closed by correcting the claim and testing the actual boundary** |
| F14 | **LOW** — SSH host pinning and ambient agent/config suppression did not match claims. | `_prepare_pinned_known_hosts` (`scripts/capture_real_gpu_over_ssh.py:1896-1936`) checks raw bytes against the supplied digest before writing `known_hosts`; `_ssh_options`/`_scp_options` enforce `/dev/null` config, strict host checking, no agent/proxy/forwarding, and pinned known-hosts use. | `test_mismatched_host_pin_fails_before_ssh_or_scp`, `test_ssh_transport_ignores_user_config_and_disables_forwarding`, and current exact-main CI. Optional key discovery remains an unauthenticated, network-facing step before pin verification; N01 bounds its output and N02 eliminates implicit identity selection. No live host was contacted. | **Closed** |
| N01 | **MEDIUM** — optional live `ssh-keyscan` discovery buffered untrusted stdout/stderr without byte limits before digest validation. | PR #45 adds `_run_bounded_capture` (`scripts/capture_real_gpu_over_ssh.py:309-436`): selector-drained isolated process group, independent 1 MiB stdout/8 KiB stderr limits (`:100-102`), immediate group kill on overflow, bounded cleanup, and redacted diagnostics. `_prepare_pinned_known_hosts` now uses it before digest/write (`:1896-1936`). | `test_live_keyscan_overflow_fails_before_host_pin_publication` exercises stdout and stderr floods, redacted error, no `known_hosts` publication, and child reaping; `test_live_keyscan_accepts_bounded_pinned_host_key_bytes` proves a valid pinned path. The dedicated test does not separately create a pipe-holding descendant or wait for the 30-second timeout; those branches were inspected and broader subprocess descendant tests cover the analogous control. Discovery itself intentionally remains pre-pin network exposure. | **Closed** |
| N02 | **LOW** — legacy live SSH could consult default identity files when `--identity-file` was omitted. | PR #46 makes `_require_identity` reject omission and non-regular/symlink paths (`scripts/capture_real_gpu_over_ssh.py:1292-1302`); `_validate_capture_mode` rejects every live/dry-run mode before action (`:2674-2680`); `_ssh_options` and `_scp_options` reject `None` and always include exactly one `IdentitiesOnly=yes`/`-i` pair (`:1313-1381`). | `test_ssh_and_scp_options_reject_omitted_identity`, `test_identity_file_must_be_explicit_regular_and_non_symlink`, `test_every_capture_mode_rejects_omitted_identity_before_action`, and `test_qwen3_fake_ssh_retrieval_stops_at_checksum_before_semantics`; PR #46 and exact merged-main CI are green. The operator remains responsible for protecting the explicit key and authorizing the target; no live SSH exchange was performed. | **Closed** |

## Current security and authority conclusions

- Bundle, Trial Set, comparison, and dashboard paths fail closed for mutation,
  malformed/bounded input, unknown authority, insufficient eligibility, and
  cross-contract substitution. A projection cannot create customer eligibility,
  comparison authority, or an ExitSpec verdict.
- Inferdrome does not issue ExitSpec `PASS`, `FAIL`, or `NOT_PROVEN`. The
  independently owned ExitSpec importer, outcomes, contract chronology, and
  receipt remain outside the repository proof boundary.
- The tracked A10 review and handoff records remain bound to `EXTERNAL_ONLY`,
  null producer-side ExitSpec authority, owner approval required, and
  retrospective chronology. The record-only gate validates committed metadata;
  it does not re-review or publish the ignored raw archive.
- Local process, SSH, provider, container, and qualification controls are
  bounded repository behavior. They are not evidence that a provider,
  host, daemon, cluster, bill, termination, or live cleanup has actually been
  observed in the present release process.

## Validation record

| Evidence | Result |
| --- | --- |
| Clean-base resolution after fetch/reset | `HEAD`, `main`, and `origin/main` each resolved to `c1668e5c7fa53ea08146ee0cd4f045b0b317bac8` before the report edit. |
| `python3.12 scripts/release_preflight.py --phase candidate --repository-only --require-clean` | `REPOSITORY_READY`: required files, exact `0.1.0.dev0` versions, canonical Apache-2.0 artifact/metadata, lock, claim boundaries, CI inventory, and clean base passed; four external/manual items remained `PENDING`. |
| Finding-focused local matrix | 760 passed, 4 skipped in 182.75s: publication, immutable publication, bounded input/parser, qualification/subprocess, real-GPU transport/guard, resolver/vLLM, Kubernetes, preflight, Compose, GCP guard/dry-run, bundle sealing, and controlled-comparison tests. The four skips require optional `google.cloud.compute_v1`; they are not passes. |
| Targeted N01/N02 regression selection | 15 passed, 82 deselected from `tests/unit/test_real_gpu_capture.py` for `live_keyscan`, `identity`, or `every_capture_mode`. |
| `scripts/review_gpu_evidence_publication.py --check-records` | Passed: tracked A10 records and cross-identities valid; metadata-only validation because the raw archive is absent. |
| Generator/static checks | Public schemas current (11 files); fake goldens current (6 files); `capture_real_gpu_over_ssh.py --check` passed; `compileall` and `bash -n scripts/*.sh` passed. |
| Exact current `main` CI | Run 33367230313 green for Engineering, Dashboard, and Deployment qualification. Dashboard-specific local tests were not collected because the bundled local runtime lacks optional `fastapi`; this is a local environment limitation, not a passing result. |

No dependency was installed or upgraded. The failed local attempt to collect
dashboard tests with the bundled runtime stopped at missing optional `fastapi`;
it made no repository change. Dashboard evidence above is therefore the exact
current-main CI job, not a local dashboard assertion.

## Explicit remaining release blockers and operational limits

The closed findings above do not clear any of the following:

- ExitSpec must independently demonstrate prospective `PASS`, `FAIL`, and
  `NOT_PROVEN` outcomes against frozen contracts and retain the ingestion
  receipt digest.
- The exact raw archives remain ignored and `EXTERNAL_ONLY`. The owner must
  make the archive-bound privacy, external-material licensing, and publication
  decision; repository Apache-2.0 does not make that decision or grant those
  rights.
- Any prospective provider/SSH/GPU/cost-cleanup evidence must be actually
  produced and independently reviewed where applicable. This report ran no
  SSH, provider, GPU, cloud, paid-resource, Docker, or Kubernetes operation
  and proves no current provider state, capacity, charge, or termination.
- A human security reviewer must review `docs/THREAT_MODEL.md` and record a
  decision. Model-assisted review is not a substitute for that sign-off.
- The release owner must make the final version transition from `0.1.0.dev0`
  to `0.1.0` on a separate release commit, obtain green final-pre-tag PR,
  `main`, and explicitly selected final-pre-tag CI for that exact commit, then
  create and authorize an annotated `v0.1.0` tag at that exact SHA.
- After tagging, the owner must obtain post-tag verification/CI and publish the
  exact commit, CI URLs, tag verification, and required sign-off in a GitHub
  Release or another external immutable release record. The annotated tag must
  not be amended to add post-tag facts.

Candidate `main` is still at `0.1.0.dev0`; no final-version transition,
authorized tag, post-tag verification, or GitHub Release is claimed here.

## Human security review (blank; model does not complete this section)

Reviewer:

Date:

Decision:

Approval:
