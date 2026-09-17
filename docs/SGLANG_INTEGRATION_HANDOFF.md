# SGLang integration continuation

The parent task accepted the minimal integration proposal after preparation
commit `126c5a35cff416ea7016eab4383b1037c95991e4` on
`codex/sglang-backend-profile`. This approval permits source integration and
CPU/fake/loopback validation, followed by one review PR. It does not authorize
GPU execution, images or model downloads, provider/credential/ADC/SSH/cloud or
registry actions, spending, tags, releases, or merging without exact-head review.

## Approved bounded implementation

1. Add a pre-dispatch `inferdrome.evaluation-engine-binding.v1` artifact binding
   the selected profile/version/source/image, model/tokenizer identity digests,
   health and telemetry semantics, cache preparation, and the two existing
   endpoint IDs to native configuration and plan digests. Do not persist raw
   origins, paths, or prompts. Require one engine choice throughout calibration
   and confirmation. Runtime remains `UNVERIFIED` and evidence-ineligible.
2. Add explicitly versioned healthy/routing raw results and study-trial
   envelopes carrying the binding digest and typed SGLang observations.
   Preserve `reported_running_requests` and `reported_queued_requests`; never
   serialize them as frozen vLLM `running`/`waiting` observations. Reuse native
   request records, terminal populations, and the existing policy algorithms.
   A corresponding verifier must check the selected semantics before replaying
   decisions. Acquisition freshness does not establish scheduler-state age.
3. Reuse the repaired PR #92 ownership, deadline, cleanup, and readback
   implementation through narrow launch/image/readiness seams. Add bounded
   SGLang identity/readiness and warmup, drain, reset checks. Keep default vLLM
   behavior and every frozen v1 byte/semantic boundary unchanged. Use two real,
   distinct endpoints; do not manufacture a duplicate endpoint.
4. Project validated populations through existing summaries and dashboard
   structures only when engine identity remains attached and unambiguous.
   Bind the report digest to its execution binding. If the existing reader/UI
   cannot convey this honestly, report the exact limitation; do not present a
   SGLang report as vLLM or rebuild the UI.

No new policies, Triton work, provider adapters, generic plugin framework,
comparison platform, or duplicate lifecycle are in scope.

## Integration and execution gates

Shared lifecycle integration and the final PR must wait for the parent to
verify PR #92's merge SHA and successful main CI. At this handoff, the parent
reports PR #92 remains unmerged and is receiving a bounded repair for a
delayed-create absence finding. The previously inspected head
`1a0cf4fb7c6c5c0739b22df64c32d8f5735245cf` is not an approved integration base.
Do not modify its worktree or depend on unpublished changes.

The parent subsequently confirmed shell execution was restored. The continuation
verified `/bin/zsh` with `login:false`, inspected this worktree, and preserved the
preparation commit. No process killing, machine cleanup, direct GitHub file
writes, or environment repair was performed. Shared process lifecycle extraction
still requires the verified merged base. Re-run the required gates against the
actual completed integration head and obtain independent review before merge.

The preparation's 170 passing SGLang tests are historical preparation results,
not validation of the completed integration. The earlier unchanged auth
concurrency-test timeout is not a product pass.

## Contract and native-session continuation

The following work is implemented on the same branch, without a serving-process
lifecycle or a GPU run:

- `engine_binding.py` builds and verifies the closed pre-dispatch engine binding,
  exact compiled study/trial membership, two distinct endpoint identities, and
  homogeneous serving settings. `engine_choice_sha256` excludes only the study
  configuration/plan digests for the forthcoming phase-equivalence check.
- `sglang_execution.py` parses SGLang scheduler gauges inside the existing native
  healthy/fault session. A narrow parser method and private result-construction
  helpers reuse the same request replay, policies, fault timing, cancellations,
  task ownership and client cleanup. The numerical adaptation is private and is
  never exported as vLLM telemetry.
- `sglang_results.py` writes and verifies healthy/routing raw v2 and study-trial
  v2 artifacts. Observation and decision snapshots retain
  `reported_running_requests` and `reported_queued_requests`. Binding and
  selected semantics are checked before the existing mathematical policy replay.
- `run_study(..., engine_binding=binding)` requires an explicit executor, writes
  `engine-binding.json` before dispatch, and only accepts matching typed v2
  results. The default absent-binding vLLM path and frozen v1 contracts remain.
- `report_study(..., engine_binding=binding)` validates the persisted binding and
  all v2 trials before reusing existing statistical reducers. Its self-contained
  report v2 and Markdown retain engine identity, component digests and source
  evidence class. An omitted binding fails closed, including for empty/aborted
  bound studies. The current dashboard reader rejects this report version;
  catalog exposure and UI contract widening are not enabled.
- `sglang_readiness.py` provides one bounded health/generation/model acquisition
  and a warmup/drain-owner-gated reset helper. The helpers take one absolute
  deadline, require the pinned successful flush response, and perform only
  non-generating post-reset readbacks. Cancellation or uncertain flush returns
  no receipt and triggers no retry. The future shared lifecycle owns retries,
  warmup, draining, transport close and process ownership.

The binding and reports remain `UNVERIFIED` and evidence-ineligible. Declared
snapshot identities and cold-cache preparation do not attest files or show that
a reset occurred. Scheduler scrape age does not establish source-state age.

Continuation verification so far: 642 focused SGLang and native-session/study/
report regression tests passed; full-source strict mypy passed for 194 modules,
and repository Ruff passed. The readiness helper separately passed 69 fake and
loopback tests, and a two-server loopback test passed eight actual HTTP trials
(healthy and stale-load, four policies each), matching native request records and
checking bound reports and client/socket/task cleanup. This totals 712 passing
focused tests. These are bounded continuation checks, not a claim that all three
required repository gates passed. Independent scoped review found no remaining
P0/P1 after a strict copied-primitive preflight fix. The preparation commit also
received independent static review with no confirmed P0/P1.

Still required: obtain the parent-confirmed #92 merge SHA and green main CI;
integrate only the repaired
shared lifecycle's narrow engine seams; enforce one engine choice through
calibration and confirmation; rerun all required gates and exact-head review;
then create the one integration review PR. No PR, push, merge, GPU qualification
or runtime compatibility claim has been made by this continuation.
