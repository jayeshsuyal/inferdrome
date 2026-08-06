# vLLM 0.26.0 capability matrix

Status: **Source- and execution-verified**

This matrix describes the saved JSON produced by `vllm bench serve
--save-result --save-detailed` at tag `v0.26.0`. It does not describe in-memory
`RequestFuncOutput` fields unless those fields survive serialization.

Classifications:

- `OBSERVED`: serialized directly in the native result;
- `DERIVABLE`: deterministically recoverable from serialized native data plus
  the frozen request plan and exact adapter contract;
- `CONFIGURED_ONLY`: echoed configuration or invocation evidence, not a measured
  outcome; and
- `UNAVAILABLE`: not recoverable with the required semantics.

The committed
[client fixture](fixtures/client-macos-empty/native/benchmark-result.json)
executes the exact pinned benchmark and serialization sources against a
deterministic endpoint. It uses vLLM's import-only `empty` device mode on macOS;
the recorded compatibility patch changes packaging platform selection only.
Installed hashes for the benchmark, endpoint client, dataset loader, and CLI
wrapper match [producer-pin.json](producer-pin.json).

## Run and producer fields

| Capability | Class | Pinned-source finding | Inferdrome consequence |
|---|---|---|---|
| Producer package version | `CONFIGURED_ONLY` | Not serialized in result JSON | Capture `vllm --version`, package artifact identity, and invocation separately |
| Result timestamp | `OBSERVED` | `date` uses local `YYYYMMDD-HHMMSS` without offset | Retain as producer-local metadata, not canonical UTC evidence |
| Backend | `CONFIGURED_ONLY` | `backend` and legacy `endpoint_type` echo CLI input | Preserve invocation; do not treat as endpoint identity proof |
| Model and tokenizer IDs | `CONFIGURED_ONLY` | `model_id` and `tokenizer_id` echo client resolution | Record as client-selected aliases with field-level provenance |
| Prompt count | `CONFIGURED_ONLY` | `num_prompts` echoes the requested count | Compare against detailed-array cardinality and completed plus failed counts |
| Request rate and burstiness | `CONFIGURED_ONLY` | Top-level values echo CLI controls | Keep separate from achieved timing |
| Maximum concurrency | `CONFIGURED_ONLY` | `max_concurrency` echoes the CLI bound | Never label this achieved concurrency |
| Arbitrary metadata | `CONFIGURED_ONLY` | Metadata keys are inserted into the top-level result and can collide with reserved keys | Allow only an Inferdrome prefix and reject reserved-key collisions |
| Benchmark duration | `OBSERVED` | Monotonic elapsed duration covers measured scheduling and drain | Preserve producer value; define any canonical throughput window independently |

## Request identity and content

| Capability | Class | Pinned-source finding | Inferdrome consequence |
|---|---|---|---|
| Detailed request cardinality | `OBSERVED` | Parallel arrays contain one entry per measured task | Require equal lengths and agreement with `num_prompts` |
| Sequence index | `DERIVABLE` | `asyncio.gather` returns task results in input order | Canonical index maps to the aligned native array index |
| Request ID | `DERIVABLE` | Dataset assigns `request_id_prefix + index` and client sends `x-request-id`; ID is not serialized | Reconstruct only with explicit prefix, frozen order, and pinned source; retain native index locator |
| Response request ID | `UNAVAILABLE` | Response headers and IDs are not serialized | Do not claim server round-trip request-ID confirmation |
| Prompt or chat messages | `UNAVAILABLE` | Detailed output omits request input | Bundle the canonical request plan separately |
| Requested output limit | `UNAVAILABLE` | Per-request requested length is not serialized | Preserve in the request plan and invocation, not as an observation |
| Input token count | `OBSERVED` | `input_lens` serializes each `output.prompt_len`; the top-level total excludes failed requests | Name request values producer-observed and calculate populations explicitly rather than assuming the aggregate covers every attempt |
| Actual output token count | `OBSERVED` | `output_lens` contains producer-calculated actual lengths and zero for failed outputs | Retain producer semantics and test against output/error alignment |
| Generated response text | `OBSERVED` | `generated_texts` is always retained in detailed mode | Detailed native bundles contain response content |
| Success boolean | `DERIVABLE` | No success array is serialized; `errors` and producer invariants distinguish ordinary failures | Define and fixture-test the exact derivation; surface anomalous empty successful streams |
| Error text | `OBSERVED` | `errors` contains response reason or exception traceback | Preserve text as sensitive diagnostic data; do not infer taxonomy from prose |
| HTTP status | `UNAVAILABLE` | Non-200 status code is reduced to response reason before serialization | Omit canonical `http_status` in v0.1 |
| Finish reason | `UNAVAILABLE` | Streaming `finish_reason` is never retained | Omit canonical `finish_reason` in v0.1 |
| Retries or attempt IDs | `UNAVAILABLE` | Producer performs no recorded retry ledger | v0.1 performs no retry and models one logical request per network attempt |

## Timing and metric fields

| Capability | Class | Pinned-source finding | Inferdrome consequence |
|---|---|---|---|
| Request start time | `OBSERVED` | `start_times` stores `time.perf_counter()` immediately before `session.post` | Normalize to integer offsets within the producer clock domain |
| Scheduled offset | `UNAVAILABLE` | Generated request delays are not serialized | Preserve configured traffic controls; do not claim exact scheduled offsets |
| TTFT | `OBSERVED` | `ttfts` stores time to the first streaming event containing a non-empty `choices` array | Name semantics `vllm_first_choices_event_v0_26`; it is not first non-empty content |
| First non-empty content time | `UNAVAILABLE` | Raw SSE events and their timestamps are not serialized | Cannot support `first_nonempty_content_delta` from native JSON |
| Inter-event latency | `OBSERVED` | `itls` stores intervals for subsequent `choices` events | Call these producer ITLs; one event need not equal one token |
| Per-request terminal E2E latency | `UNAVAILABLE` | In-memory `latency` is used for summaries but omitted from detailed output | ExitSpec cannot independently reproduce upstream per-request E2E summaries |
| Last-choices-event span | `DERIVABLE` | `ttft + sum(itls)` reaches the last retained `choices` event | May be a separately named canonical metric; it is not terminal response latency |
| Upstream TPOT per request | `UNAVAILABLE` | Producer uses omitted in-memory latency divided by output tokens minus one | Do not claim exact independent reproduction of native TPOT summaries |
| Choices-span per output token | `DERIVABLE` | Can use retained TTFT, ITLs, and output length | If useful, publish under a distinct Inferdrome definition rather than calling it upstream TPOT |
| TTFT/ITL summary statistics | `OBSERVED` | Selected aggregate fields are serialized when a tokenizer is initialized | Treat as diagnostic summaries; recompute verdict metrics from canonical records |
| E2E/TPOT summary statistics | `OBSERVED` | Aggregates may be serialized, but required per-request latency is absent | Cannot be accepted as independently reproducible verdict evidence |
| Producer peak concurrency | `OBSERVED` | `max_concurrent_requests` is a coarse one-second-bucket summary and can exceed the configured semaphore bound | Do not present as exact peak or time-weighted achieved concurrency |
| Exact achieved concurrency | `UNAVAILABLE` | Per-request terminal intervals are incomplete | Remove from required v0.1 evidence or add a different instrumented producer |

## Populations and lifecycle

| Capability | Class | Pinned-source finding | Inferdrome consequence |
|---|---|---|---|
| Preflight request configuration | `CONFIGURED_ONLY` | Ready-check timeout controls an initial request | Capture invocation and phase event |
| Preflight request details | `UNAVAILABLE` | Output is discarded | Do not claim a complete preflight request ledger |
| Warmup count | `CONFIGURED_ONLY` | `num_warmups` controls repeated copies of the first test request | Capture invocation and phase event |
| Warmup request details | `UNAVAILABLE` | Gathered warmup outputs are discarded | Canonical ledger covers measured requests only |
| Measured failures | `OBSERVED` | Failed outputs remain aligned in detailed arrays and aggregate `failed` | Retain every measured failure |
| Completed/failed counts | `OBSERVED` | Top-level counts are calculated from in-memory producer success | Verify against detailed-array invariants where possible |

## Privacy and import concerns

| Capability | Class | Pinned-source finding | Inferdrome consequence |
|---|---|---|---|
| Response-content exclusion | `UNAVAILABLE` | Detailed mode serializes `generated_texts` with no selective exclusion | v0.1 detailed bundles must be marked response-content-bearing |
| Prompt-content exclusion | `OBSERVED` | Native result does not serialize prompts | Request-plan policy determines overall prompt sensitivity |
| Native schema version | `UNAVAILABLE` | Result JSON has no producer schema identifier | Gate on exact package version plus structural fingerprint |
| Secret-safe stdout | `UNAVAILABLE` by default | Producer prints the full parsed argument namespace | Never pass secrets through `--header`, URL query, metadata, or extra-body; use controlled environment injection and redact capture |
| Reserved metadata namespace | `UNAVAILABLE` by default | Metadata can overwrite some top-level setup fields | Adapter must allowlist names and verify expected native values |

## Verified schema impact

The source audit supports a narrow v0.1 measured-request core containing:

- run-scoped request identity derived from frozen order;
- native array index;
- producer-observed input and output token counts;
- producer start offset, TTFT, and ITL observations;
- generated-response digest or content according to bundle policy; and
- producer error text plus a carefully defined derived success state.

It does not support canonical claims for HTTP status, finish reason, retry
attempts, exact scheduled offsets, first non-empty content timing, terminal
per-request E2E latency, exact upstream TPOT reconstruction, or exact achieved
concurrency.

The executable fixture confirmed those exclusions. PR 1 schema work is now
unblocked, but only within this narrow field set; execution verification does
not make unavailable observations available.

## Execution reconciliation

The committed fixture contains one preflight request, two warmup requests, and
four measured requests. The mock trace is diagnostic evidence for the fixture;
it is not a proposed production artifact contract.

| Claim under test | Executed observation | Result |
|---|---|---|
| Measured ordering and request IDs | Native arrays remained in workload order; the mock received `inferdrome-spike-0` through `inferdrome-spike-3` | Confirmed as derivable from frozen order and prefix, not natively serialized |
| Preflight and warmup visibility | Three requests with no measured request ID appear in the mock trace; no corresponding native detailed rows exist | Confirmed unavailable as request-level native evidence |
| Failure retention | Measured index 2 received HTTP 503; native arrays retain index 2 with zero output, empty text, and `Service Unavailable` | Confirmed, while numeric HTTP status remains unavailable |
| TTFT event semantics | The mock emitted a role-only `choices` event before content; native TTFT is positive and begins at that first event | Confirmed as `vllm_first_choices_event_v0_26`, not first content |
| ITL event semantics | Each successful row has three ITLs for the later alpha, beta, and finish-reason `choices` events; the empty-choices usage event is excluded | Confirmed event-based, not token-based |
| Terminal latency omission | Native output contains E2E aggregates but no per-request latency array | Confirmed independently unreproducible from native detailed data |
| Input-token populations | `input_lens` is `[4, 4, 14, 4]`, while `total_input_tokens` is 12 because the failed 14-token request is excluded | Confirmed aggregate and detailed populations differ |
| Response-content behavior | Successful `generated_texts` entries contain `alpha beta` | Confirmed response-content-bearing |
| Concurrency summary semantics | Configured `max_concurrency` is 2 while native `max_concurrent_requests` is 3 | Confirmed unsuitable as exact achieved concurrency |
| Native shape | Every expected detailed array has four aligned entries; request IDs, HTTP statuses, finish reasons, retries, scheduled offsets, and warmup outputs are absent | Confirmed structural fingerprint |
