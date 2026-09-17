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

The shared lifecycle gate cleared after the parent independently verified
PR #92's merge `d9798ac6700713af64055f00fe723f4cb9974842` and successful main
Engineering, Dashboard and Deployment jobs in
[run 35268332149](https://github.com/jayeshsuyal/inferdrome/actions/runs/35268332149).
That exact base was merged normally into this branch, preserving checkpoint
`b94a57d54bcdcdce9248579211ea92b8df78cca6`. The old inspected PR head
`1a0cf4fb7c6c5c0739b22df64c32d8f5735245cf` was never used as an integration base.
No other worktree was changed.

The parent subsequently confirmed shell execution was restored. The continuation
verified `/bin/zsh` with `login:false`, inspected this worktree, and preserved the
preparation commit. No process killing, machine cleanup, direct GitHub file
writes, or machine repair was performed. The pinned uv 0.8.17 development tool
was restored from its checksum-verified official release for offline packaging.
Shared process lifecycle extraction uses the verified merged base. Run gates on the
actual completed integration head and obtain independent review before merge.

The preparation's 170 passing SGLang tests are historical preparation results,
not validation of the completed integration. The earlier unchanged auth
concurrency-test timeout is not a product pass.

## Contract and native-session checkpoint

Checkpoint `b94a57d` implemented the following work before the shared lifecycle
gate cleared, without a serving-process lifecycle or a GPU run:

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

## Shared lifecycle integration

After the gate cleared, the integration added the fixed Docker bridge/readonly
artifact projection, exact local snapshot/template verification, and SGLang
readiness/warmup/drain/flush hooks on the shared exact-owned process owner.
Mapped reset receipts distinguish original profile, projected launch and mapped
readiness digests. Every calibration and confirmation recipe is recompiled and
prebound before lifecycle work, with a durable homogeneous engine-choice ledger.
Known cross-engine executor/lifecycle combinations fail before dispatch.

Independent review found two pre-dispatch binding gaps and an inherited snapshot
walker that silently omitted unreadable directories. The integration repairs
those cases and adds regressions. Valid snapshot hash domains and `.cache`
exclusion remain unchanged. Deadline review additionally requires artifact hashing
to remain owned and observable while the async session stays responsive.

See [SGLANG_NATIVE_INTEGRATION.md](SGLANG_NATIVE_INTEGRATION.md) for the API,
mapping, artifacts and remaining runtime/UI limitations. Local Docker/Compose
execution remains explicitly withheld; exact-head PR CI supplies that gate.
No GPU qualification, model/image download, provider/spend or runtime compatibility
claim is made by this integration.

## Completed local validation

The final integration source passed the full Engineering gate: generated checks,
Ruff, strict mypy across 198 modules, and 4,344 Python tests; 21 tests skipped
for optional dependencies/platform support. The Engineering packaging probe
could not find uv on its default path, but the full Dashboard gate separately
used the checksum-verified uv 0.8.17 executable and passed offline sdist/wheel
installation plus installed dashboard/CLI smoke checks.

The full Dashboard gate also passed TypeScript, 177 frontend unit tests,
36 Playwright tests, 217 backend tests and production asset generation. Committed
dashboard assets remain unchanged. The dependency lock passed the pinned uv
offline freshness check. Deployment schema freshness, script compilation and all
26 qualification unit tests passed locally; these checks do not substitute for
the withheld complete Docker Compose gate.

Independent scoped reviews found no remaining confirmed P0/P1 in the final
source, including retained artifact workers and unchanged native lifecycle
behavior. The single draft PR records its exact head, all three GitHub CI jobs
and the parent's final independent review. It may become ready for review only
after those required jobs pass; this handoff does not authorize merging.
