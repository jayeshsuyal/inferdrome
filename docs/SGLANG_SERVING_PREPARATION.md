# SGLang serving preparation

This additive lane prepares SGLang for the same Inferdrome evaluation runner and
dashboard. It is source-reviewed, CPU/fake-testable preparation: runtime remains
`UNVERIFIED`, fixtures are `SYNTHETIC_ONLY`, and outputs are evidence-ineligible.
It does not register a serving backend, launch SGLang, or qualify a GPU campaign.

## Integration gap map

| Existing contract | Gap | Independent preparation |
| --- | --- | --- |
| [Legacy SGLang boundary](SGLANG_0_5_ADAPTER.md) | Native benchmark JSONL lacks request-plan identity and start offsets. | Preserve its existing `UNAVAILABLE` fields and evidence-ineligibility. |
| Native evaluation replay/client | Already owns schedule, request population, streaming times and terminal outcomes. | Exercise its unchanged path using pinned SGLang-shaped synthetic streams. |
| Serving lifecycle | PR2 owns concrete vLLM processes, deadlines, cleanup and calibration execution. | Pure SGLang argv/profile and injected readiness validation; defer lifecycle integration. |
| Evaluation observations | Existing gauges and labels describe vLLM. | Separate bounded SGLang scheduler parser; no renaming or routing registration. |
| Studies/report/dashboard | Frozen study and report contracts do not identify a qualified SGLang campaign. | Document matched comparison and versioned seams; reuse the UI after integration review. |

## Pinned source and image

Keep the existing release `v0.5.18`, commit
`71de97b264b04dcd514cf904003028aefe9775c8`; the
[official release](https://github.com/sgl-project/sglang/releases/tag/v0.5.18)
identifies that commit. Selected image metadata, read publicly without a pull:

```text
tag:       lmsysorg/sglang:v0.5.18-cu129-runtime
platform:  linux/amd64
reference: lmsysorg/sglang@sha256:291c8e3d2c6268128f1d8fd36455083533ed65e5b6c3f1bb223b096360419374
CUDA:      12.9.2 (published image metadata, not a host compatibility result)
```

[Official Docker Hub metadata](https://hub.docker.com/layers/lmsysorg/sglang/v0.5.18-cu129-runtime/images/sha256-291c8e3d2c6268128f1d8fd36455083533ed65e5b6c3f1bb223b096360419374?tab=layers)
publishes the architecture manifest above and the matching SGLang build commit.
The tag can move; retain both the explanatory tag and immutable architecture
digest. No image layers, model weights, CUDA packages or SGLang were installed.
Model architecture, GPU memory, driver and kernels still require qualification.

Pinned official source catalog (release-tag fallbacks were used where the public
commit URL could not be fetched; the release-to-commit mapping was verified):

- [Launch entrypoint](https://github.com/sgl-project/sglang/blob/71de97b264b04dcd514cf904003028aefe9775c8/python/sglang/launch_server.py): lines 50–65 retain `python -m sglang.launch_server`.
- [Server arguments](https://github.com/sgl-project/sglang/blob/71de97b264b04dcd514cf904003028aefe9775c8/python/sglang/srt/server_args.py): model/tokenizer, dtype, cache, scheduling, streaming and metrics settings.
- [HTTP endpoints](https://github.com/sgl-project/sglang/blob/71de97b264b04dcd514cf904003028aefe9775c8/python/sglang/srt/entrypoints/http_server.py): lines 607–765 readiness/identity; 892–907 flush.
- [Chat streaming](https://github.com/sgl-project/sglang/blob/71de97b264b04dcd514cf904003028aefe9775c8/python/sglang/srt/entrypoints/openai/serving_chat.py#L1415-L1606) and [error envelopes](https://github.com/sgl-project/sglang/blob/71de97b264b04dcd514cf904003028aefe9775c8/python/sglang/srt/entrypoints/openai/serving_base.py).
- [Protocol](https://github.com/sgl-project/sglang/blob/71de97b264b04dcd514cf904003028aefe9775c8/python/sglang/srt/entrypoints/openai/protocol.py) and [usage calculation](https://github.com/sgl-project/sglang/blob/71de97b264b04dcd514cf904003028aefe9775c8/python/sglang/srt/entrypoints/openai/usage_processor.py).
- [Scheduler metrics collector](https://github.com/sgl-project/sglang/blob/71de97b264b04dcd514cf904003028aefe9775c8/python/sglang/srt/observability/metrics_collector.py#L1050-L1062), [release reporter](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/scheduler_components/metrics_reporter.py), and [single-process launch](https://github.com/sgl-project/sglang/blob/71de97b264b04dcd514cf904003028aefe9775c8/python/sglang/srt/entrypoints/engine.py#L824-L865).
- [Scheduler reset](https://github.com/sgl-project/sglang/blob/71de97b264b04dcd514cf904003028aefe9775c8/python/sglang/srt/managers/scheduler.py#L3944-L3970) and [release template loading](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/parser/template_manager.py#L171-L189).

## Prepared code contracts

[`SglangServingConfig` and `build_sglang_serving_profile`](../src/inferdrome/evaluation/sglang_profile.py)
are pure validation and argv construction. They declare local model/tokenizer
snapshots from the same revision, snapshot/template hashes, served name, context,
queue/concurrency bounds, memory fraction, seed and explicit radix-cache policy.
The profile fixes a single CUDA device, BF16 weights and KV cache, safetensors,
explicit unquantized loading, Hugging Face tokenizer, FCFS, no chunked prefill,
stream interval one and decode reporting interval 40. Arbitrary options and environment additions are absent.
These settings describe this preparation slice, not all configurations SGLang
supports. Memory fraction and queue limits are engine settings, not claims of
equivalence to vLLM allocation or scheduler semantics.

The future executor must check real files/directories, hashes, model metadata,
resolved configuration and image identity before launch. Offline environment
declarations must be applied to a sanitized process environment: they do not
themselves attest that no imported component will attempt network access.
Exclude ambient SGLang overrides, including `SGLANG_DP_RANK`, and credentials.
Never pass the argv through a shell. A local Jinja template must end in `.jinja`;
upstream otherwise selects its JSON conversation-template loader, requiring
`.json`. Jinja loading strips edge newlines and expands literal `\n`; compare
the resulting token IDs, not only the raw template hash.

`validate_sglang_evaluation_binding` checks declared endpoint/model/output bounds.
It cannot verify prompt tokenization or prompt-plus-output context fit.
`validate_sglang_readiness` validates bounded injected 200 responses from
`/health`, `/health_generate` and `/model_info`. It requires the selected local
paths/name/weight-version and plain-text, generation, safetensors, no-parser
declarations. The result is `SERVER_DECLARED_MATCH`, not artifact attestation.
`/health` can be liveness-only; `/health_generate` tests scheduler/detokenizer
responsiveness and may affect cache state. Probe before cache preparation.

The selected endpoint is `POST /v1/chat/completions`, text-only and one choice,
with streaming and final usage enabled, continuous usage disabled. The expected pinned
sequence is role-only, content deltas, stop/length finish, usage-only empty
choices, then `[DONE]`. `[DONE]` alone is not success. HTTP errors, nested SSE
errors, incomplete streams, deadline expiry and cancellation retain failure
outcomes and partial diagnostics. Graceful server `abort`, tools, reasoning,
multimodal output and extra choices are outside this narrow successful contract.
The echoed response model does not establish loaded-model identity.

The existing client binds each request by `request_index` within the configuration
digest; that identity is not a wire request ID. Preserve every offered row,
scheduled offset, observed arrival, dispatch, first content, protocol completion
and terminal time, including failures/cancellations. TTFT is the observed first
content delta from dispatch; scheduled-to-first-content also includes client
delay. Chunk events do not become exact token times. Final token counts retain
server-usage provenance; missing usage may still accompany a successful stream, but its counts remain
unavailable. No missing or partial usage is manufactured by the
legacy normalizer or inferred from chunk counts.

[`parse_sglang_metrics`](../src/inferdrome/evaluation/sglang_metrics.py) requires one
`sglang:num_running_reqs` and one `sglang:num_queue_reqs`, with exact labels
`model_name`, `engine_type="unified"`, `tp_rank="0"`, `pp_rank="0"`,
`moe_ep_rank="0"`. Default single-process launch has no `dp_rank` label.
Extra ranks/dimensions, duplicates, malformed/noninteger/nonfinite values and
sample timestamps fail closed; missing values never become zero.
`SGLangSchedulerSample.available_at` rejects stale acquisition using monotonic
time from scrape start. Source-state freshness remains unavailable: gauges hold
phase-dependent snapshots, decode updates default to every 40 iterations, and
idle reporting can wait 30 seconds. Running is the prefill running snapshot or
decode batch count; queued excludes the separate grammar queue. Neither is a
portable vLLM load score, an all-offered population, or a throughput measurement.

## Matched sequential study recipe

This is a proposed recipe, not an executable or qualified campaign. Predeclare
at least eight paired repetitions per workload/load/cache treatment, balanced
AB/BA engine order with a recorded ordering seed. Finish one engine and its
owned process cleanup before starting the other; no concurrent GPU runs.

For every pair freeze the same model revision and full snapshot hashes, BF16,
GPU model/count/memory and host limits, tokenizer revision, rendered template,
token-ID sequence for each prompt, output caps and stop/EOS policy. Freeze exact
arrival offsets, offered window, client in-flight and queue bounds, deadlines,
drain, sampling, repetition count and seeds. Record engine/version/image,
resolved attention/graph/cache settings, batching, scheduler and memory settings
separately. Identical seeds do not imply identical cross-engine outputs.

The current evaluation v1 request fixes temperature zero and thinking disabled;
it has neither explicit per-request seed nor token-ID input. SGLang's process
seed does not add these missing client fields. Before claiming exact matched
token input or seeded sampling, validate rendering externally and obtain a
reviewed new config/plan version for any required seed, sampling or token-input
fields. Do not widen a frozen v1 schema or silently inject unsupported fields.

Calibration seeks each engine's separately confirmed admissible load under
predeclared SLO criteria. Report its tested levels and selection/confirmation
outcomes as calibration, not a same-load win. Fixed-load comparison freezes an
identical arrival schedule before confirmation, using an independently selected
common range; never compare each engine at its own calibrated rate as matched
fixed load. Neither engine is promised to win.

Report all-success TTFT quantiles with population counts and failure context,
plus scheduled-origin first-content/completion SLOs. All-offered SLO goodput is
the count of successful rows meeting both SLOs divided by the fixed offered
window in seconds; its companion success fraction divides by every offered row.
Failure rate is all non-success terminal rows divided by offered rows, broken
down by outcome. Rejections and cancellations remain in those denominators.
Aggregate successful output throughput is the sum of completion tokens with
validated server usage over successful requests divided by the declared actual
measurement elapsed time including drain. Report token coverage and elapsed
denominator; with missing usage, total throughput is unavailable. Never average
per-request token rates or substitute the engine's generation-throughput gauge.
Keep any fixed-window token metric separately named; use paired trial-level
differences and the existing report uncertainty approach, not pooled requests.

## Cache treatments and integration seams

- **Prefix-cache cold:** initialize process/kernels, finish matched warmup and
  readiness, drain completely, require a successful bounded `/flush_cache`, then
  measure. A failed flush invalidates this treatment. Do not probe generation
  between reset and measurement.
- **Prefix-cache warm:** reset from the same documented state, run the exact
  predeclared warming corpus/order, drain, then measure without another flush.
- **Cache disabled:** explicitly disable SGLang radix caching and record the
  corresponding vLLM setting; disabling does not make allocation policies equal.
- **Process cold:** a separate startup experiment includes process/model/graph
  initialization. Record filesystem and compiled-kernel cache state; neither
  restart nor radix flush establishes disk-cache coldness.

SGLang idle flush clears tree/request/KV pools, grammar cache, some internal
counters and optionally allocator cache. It does not reset every Prometheus
counter, filesystem cache or compiled kernel. Record the vLLM reset semantics
independently; a similarly named reset is not proof of identical cache state.

After PR2 merges, review a separate integration PR that plugs the pure profile
into its owned subprocess lifecycle and adds bounded readiness/model checks,
hash preflight, deadlines, drain/reset ordering and complete process cleanup.
Reuse native replay/transport and its one-row-per-offer accounting. Add the
explicit SGLang telemetry acquisition seam without changing vLLM observations
or registering a queue-based routing policy. The existing two-endpoint runner
and native lifecycle topology need an explicit reviewed backend/topology
binding; do not manufacture duplicate endpoints to hide that dependency.

Introduce reviewed backend identity and configuration binding in a new
study/report contract where existing frozen versions cannot represent them;
then reuse the report reader and dashboard through that boundary. Qualified
evidence remains a separately versioned dependency. This preparation never
produces canonical v0.1 request records, acceptance verdicts or GPU receipts.
Preserve public/deployment/evidence/review history and `EXTERNAL_ONLY` raw
archives. No changes to PR2 orchestration are required by this slice.

## Validation and handoff

Worktree: `/Users/jayeshsuyal/.codex/worktrees/6da1/Inferdrome`; branch:
`codex/sglang-backend-profile`; verified remote-main base:
`f018a7987bf31c2811cfdc82f62dc0aa4d733ec9` (PR1 #91 merged). No PR2 worktree
or unpublished source was used. The six new files are this document, the two
modules linked above, and `tests/unit/test_sglang_serving_profile.py`,
`tests/unit/test_sglang_metrics.py`, `tests/unit/test_sglang_evaluation_stream.py`.
Existing runner, observations, normalizer, schemas and UI source remain unchanged.

Local validation uses Python 3.12.12 and dependencies from unchanged `uv.lock`,
synced using uv 0.8.17; no SGLang/CUDA/model installation. Final scoped pytest:
**170 passed**; repository Ruff and strict mypy pass. The full engineering gate
ran all generated/static checks and collected 3,941 tests: **3,919 passed,
21 skipped, 1 failed**. Its skipped checks require optional Google SDK/Linux
support or the uv packaging handoff; the offline package smoke was run separately.

The dashboard gate passed TypeScript, **177 frontend unit tests**, **35 Chromium
journeys**, and **216 of 217 backend tests**. Both gates failed at the unchanged
`tests/dashboard/test_auth.py::test_keyring_create_is_process_safe_on_a_missing_file`:
a spawned worker exceeded its 20-second join deadline and was terminated. An
isolated retry reproduced that failure (25.46 seconds); its remaining process
group was cleaned up at the outer 50-second deadline. Only this task's verified
test processes were stopped. This lane does not modify the auth code or test.
The full gates therefore are **not green**, and integration review must retain
this blocker. A separate offline sdist/wheel build plus installed dashboard and
CLI smoke tests passed.

Deployment qualification gate: **26 unit tests passed**; its Docker Compose
phase was unavailable locally and was not executed. No image pull/build or
cloud resource creation was attempted. The final 170-test SGLang run includes
the last validation hardening after the full-suite collection.

Unverified: actual image/package/kernel behavior, driver/GPU compatibility,
model/tokenizer/template bytes and effective tokenization, live SSE/telemetry
shape and timing, reset completion, and native process cleanup on real devices.

Proposed next PR, only after PR2 merges: connect this profile to its existing
owned native lifecycle, implement bounded model/readiness/reset acquisition and
snapshot preflight, bind backend identity through a reviewed additive/versioned
study seam, and test the complete lifecycle with local fakes. Keep the current
client measurement/report path and UI; retain SGLang metrics as descriptive until
a separate semantics review permits their use. GPU qualification and eligible
evidence remain subsequent explicitly authorized work. This lane is local-only;
no push, integration PR, workflow dispatch, merge, tag or release is authorized.
