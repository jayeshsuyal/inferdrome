# Inference evaluation v1

Inferdrome's inference evaluation asks: **when two-replica vLLM serving gets
busy and monitoring becomes delayed, which routing strategy keeps responses
fast without unnecessarily rejecting users?** The deliverable is reproducible
experiments and defensible measurements. The first slice supplies bounded,
concurrent streaming execution and local measurement records.

This path is separate from `routing_execution` and its frozen six-request,
non-streaming correctness test. It does not change the old policy identifiers,
fixtures, receipts, verifiers, or historical evidence. An evaluation result is
not a sealed routing-execution receipt.

## What this slice establishes

The `inferdrome.evaluation` module accepts a finite arrival schedule for exactly
two ordered endpoints and makes streaming OpenAI-compatible chat-completion
requests. Each offer specifies its endpoint directly. That assignment is a
replay input, not an adaptive routing strategy. An injected routing seam supports
the next slice's independently testable policies.

The implementation uses `aiohttp`, locked to **3.13.5** in `uv.lock`, for
cancellable asynchronous HTTP. It owns the small measurement and admission
layer needed to account for all scheduled traffic. It adds no dataset catalog,
model launcher, provider integration, or general benchmark framework.

The CLI writes a single private JSON result with `LOCAL_MEASUREMENT_ONLY`
classification for `run`, `SYNTHETIC_ONLY` for `demo`, and
`evidence_eligible=false` in both modes. Source
commit and endpoint/model identities are declarations, not verified runtime
identities. Local HTTP behavior does not establish GPU compatibility, real
serving capacity, policy superiority, or a completed routing study.

## Run a local demonstration

Use the repository's Python 3.12 environment and locked dependencies. The demo
starts loopback synthetic streaming endpoints, executes a small schedule, and
cleans up those local endpoints:

```sh
evaluation_dir=$(mktemp -d)
uv run --locked python -m inferdrome.evaluation demo \
  --output "$evaluation_dir/demo.json"
```

The demo requires no model, GPU, or provider account. Its synthetic data is a
protocol and concurrency rehearsal, not model performance evidence. Choose a
new output filename for each run: output publication does not replace an
existing file.

## Run against endpoints you already operate

The following small configuration uses local endpoints on ports 8000 and 8001.
It assumes those endpoints are already running and accept the model name
`local-model`; replace that name with their served model name. The forty-zero
`source_commit` is an explicit **local demo placeholder**, not a source identity.
For a real measurement, declare the exact source commit used and retain the
separate verification needed for any later evidence claim.

```sh
evaluation_dir=$(mktemp -d)
cat > "$evaluation_dir/config.json" <<'JSON'
{
  "schema_version": "inferdrome.evaluation-config.v1",
  "source_commit": "0000000000000000000000000000000000000000",
  "model": "local-model",
  "max_tokens": 32,
  "temperature": 0,
  "enable_thinking": false,
  "endpoints": [
    {"endpoint_id": "endpoint-a", "origin": "http://127.0.0.1:8000"},
    {"endpoint_id": "endpoint-b", "origin": "http://127.0.0.1:8001"}
  ],
  "bounds": {
    "max_requests": 4,
    "concurrency": 2,
    "max_queue": 4,
    "duration_ns": 2000000000,
    "request_timeout_ns": 5000000000,
    "drain_ns": 5000000000,
    "cleanup_timeout_ns": 2000000000,
    "max_stream_bytes": 1048576,
    "max_event_bytes": 65536,
    "max_content_events": 1000
  },
  "offers": [
    {"scheduled_ns": 0, "endpoint_id": "endpoint-a", "prompt": "Name a primary color."},
    {"scheduled_ns": 0, "endpoint_id": "endpoint-b", "prompt": "Name a primary color."},
    {"scheduled_ns": 50000000, "endpoint_id": "endpoint-a", "prompt": "Count from one to three."},
    {"scheduled_ns": 100000000, "endpoint_id": "endpoint-b", "prompt": "Count from one to three."}
  ]
}
JSON
uv run --locked python -m inferdrome.evaluation run \
  --config "$evaluation_dir/config.json" \
  --output "$evaluation_dir/measurement.json"
```

This command sends requests to the configured endpoints; it does not create or
qualify them. A runnable example does not confer authority to rent GPUs, create
resources, publish images, obtain credentials, or spend money. Any live
calibration requires its own exact execution proposal and applicable approval.

## Input contract and bounds

Inputs are strict JSON: unknown fields, duplicate keys, non-finite numbers,
invalid types, excessive structure, and out-of-bound values are rejected.
The input file is limited to 16 MiB. Endpoint origins must be distinct canonical
HTTP IPv4 origins with an explicit port and no path: `127.0.0.1`, or an address
in `10.0.0.0/8`, `172.16.0.0/12`, or `192.168.0.0/16`. The endpoint order is
always `endpoint-a`, then `endpoint-b`. Hostnames, public addresses, credentials
inside URLs, and URL paths are not accepted.

The transport sends only `POST /v1/chat/completions` to those origins. It does
not use ambient proxies, netrc credentials, cookies, redirects, authorization
headers or response decompression. It requests identity encoding and accepts
HTTP-200 bodies only as `text/event-stream` with identity encoding. Non-200
bodies are closed without being read into the streaming measurement. The
connection pool follows the concurrency bound, incoming reads are at most
16 KiB, and the client limits header count to 64 with 4 KiB line/field limits.

Offers are nondecreasing in `scheduled_ns`; simultaneous arrivals are allowed.
Every offset is nonnegative and strictly below `duration_ns`. Each prompt is
nonempty and at most 32 KiB of UTF-8; total prompt bytes are at most 8 MiB.
Prompts are input data, not evidence payloads. The study fixes temperature to
zero and uses `enable_thinking=false`; these settings do not by themselves
prove that an arbitrary endpoint follows the requested generation behavior.

| Bound | Maximum accepted value | Purpose |
| --- | ---: | --- |
| `max_requests` | 10,000 | Limits scheduled requests; the offer count must fit. |
| `concurrency` | 64 | Limits simultaneous request execution. |
| `max_queue` | 1,024 | Limits pending requests; zero is allowed. |
| `duration_ns` | 300 seconds | Bounds the arrival window. |
| `request_timeout_ns` | 60 seconds | Sets the deadline relative to scheduled arrival. |
| `drain_ns` | 60 seconds | Bounds the post-arrival drain allowance. |
| `cleanup_timeout_ns` | 5 seconds | Bounds asynchronous cleanup. |
| `max_stream_bytes` | 1 MiB | Bounds each received response body. |
| `max_event_bytes` | 64 KiB | Bounds an individual SSE event. |
| `max_content_events` | 4,096 | Bounds retained content-event timings. |
| `max_tokens` | 4,096 | Bounds the requested generated token count. |

Time fields above are integer nanoseconds in JSON, not floating-point seconds.
`max_event_bytes` cannot exceed `max_stream_bytes`, and
`max_requests * max_content_events` cannot exceed 1,000,000. Maximum allowed
values are safety ceilings, not a recommended campaign size or authorization
to run at those limits.

## Timing and terminal accounting

All recorded request times are integer nanosecond offsets from one runner-local
monotonic start. They are client observations, not wall-clock timestamps,
synchronized server timestamps, or packet-capture measurements. The schedule
is fixed before execution; overload must not quietly rewrite it.

| Record field | Observation |
| --- | --- |
| `request_index` | Zero-based position in the input schedule; result order is schedule order. |
| `scheduled_ns` | Requested arrival offset, fixed by the input. |
| `arrival_observed_ns` | When the coordinator observed this arrival, including arrivals subsequently rejected for capacity. |
| `dispatch_ns` | Immediately before invoking the HTTP client; includes subsequent connection establishment and writing. |
| `dispatch_lag_ns` | `dispatch_ns - scheduled_ns`, or null when never dispatched. |
| `response_headers_ns` | When the HTTP client exposes status/headers. |
| `first_body_byte_ns` | First nonempty response body bytes presented to the streaming parser. |
| `first_content_ns` | Complete SSE frame containing the first nonempty `delta.content`. |
| `content_event_times_ns` | Times of all nonempty content frames, including the first; equal times are allowed. |
| `protocol_done_ns` | Complete required `[DONE]` frame observed after content and a generation finish. |
| `terminal_ns` | Coordinator/request terminalization; it may follow protocol completion. |

Missing observations remain null rather than being fabricated as zero latency.
`http_status`, `finish_reason`, `attempts` and `outcome` retain diagnostic context;
`attempts` is zero or one because the evaluator does not retry. The actual kernel
wire-send time and first wire response byte are unavailable. Output explicitly
marks `wire_first_response_byte` and `exact_token_timing` as `UNAVAILABLE`.

Derive dispatch lag from `dispatch - scheduled`. Keep that lag separate from
`first content - dispatch` and from `terminal - dispatch`; use scheduled arrival
as the origin when reporting the latency experienced by offered traffic. A fast
response after a long client queue is not a fast response to the original offer.

The first response body byte is the first body data exposed by the HTTP client.
Headers and SSE metadata may arrive before generated text. Empty content,
role-only deltas, comments, and usage metadata do not establish first nonempty
content. **Time to first content is not exact time to first token.** A single
content event can contain multiple model tokens, and several events may arrive
in one network read. Subsequent timings are content-event timings, not exact
inter-token latencies or server decode steps.

`prompt_tokens` and `completion_tokens` come from streaming server usage when
available and valid; `usage_provenance` is `SERVER_REPORTED_STREAM_USAGE` or
`UNAVAILABLE`. Counts of network chunks or content events must not substitute
for token counts. Server usage is still an endpoint claim, and the client does
not independently attest its tokenizer or model.

The configured schedule remains the conservative offered-request denominator,
including entries cancelled before their due time. `offered_count` is the
schedule population; `arrivals_observed_count` counts non-null `arrival_observed_ns`
values and includes capacity rejections; `dispatched_count` counts attempts.
Do not interpret arrival observation as acceptance into execution. Retain
rejection, timeout, transport/HTTP/stream failure, incomplete stream,
cancellation, and drain-failure outcomes when analyzing success. Exactly one
terminal outcome belongs to each schedule entry in a returned result; a later
cleanup event must not turn an earlier failure into a second request.

## Admission, deadlines and stream termination

One coordinator owns the finite schedule, active requests and bounded FIFO
queue. It starts work immediately when a concurrency slot is free, otherwise
queues it if capacity remains; excess arrivals receive `REJECTED_CAPACITY`.
With `max_queue=0`, there is no pending queue. Pending requests keep their
original scheduled timestamps. The evaluator does not allocate a waiting task
or semaphore for every future offer.

The request deadline is `scheduled_ns + request_timeout_ns`, so it includes
dispatch lag and queue time. An expired queued request times out without an
HTTP attempt. An active request reaching its deadline is cancelled and its
partial timings remain available. The run-wide drain cutoff is
`duration_ns + drain_ns` from the same start; execution may finish earlier when
the schedule is exhausted and all requests have terminated. Cancellation or
drain expiry stops outstanding work, including undispatched schedule entries.
The final timestamp is sampled once. Observed cancellation takes precedence;
otherwise the earlier of request deadline and drain cutoff takes precedence
over a response/error observed at or after that cutoff, with request timeout
winning an exact tie. Dispatch checks these conditions again after routing.

The injected clock must be monotonic and its sleep cancellable. Cleanup guards
use real event-loop time even when request timing uses a simulated clock.
`cleanup_timeout_ns` applies to each guarded task/transport cleanup operation;
transport close gets one further bounded cancellation grace after a close
timeout. Caller interruption does not abandon the close task or extend its
deadline. This is not additional inference time. If cleanup cannot be confirmed within its
bound, the evaluator raises a sanitized execution error instead of returning a
normal result claiming successful cleanup.

The supported response is one OpenAI chat-completion choice with index zero.
SSE is parsed across split reads and UTF-8 boundaries, accepting LF/CRLF frame
delimiters, comments and joined `data` lines. Completion requires nonempty text,
one supported finish reason (`stop` or `length`), a complete `[DONE]` frame, and
clean framing at EOF. A `length` finish is a supported bounded generation; it
does not mean the HTTP stream was truncated. Tool-call and other finish reasons
are outside this slice's supported response contract.

Optional usage may occur once with or after the finish event. Its prompt,
completion and total counts must be nonnegative integers with consistent
totals. Premature/repeated usage, content after finish, payload after `[DONE]`,
duplicate JSON keys, invalid Unicode and malformed SSE fail the stream. A
stream without usage may succeed with unavailable token-count provenance.
Total framed events are additionally capped at
`4 * max_content_events + 64`, including non-content events. No generated text
or raw server-error message is retained in the result.

| Outcome | Meaning |
| --- | --- |
| `SUCCESS` | Supported content, finish, terminal marker and EOF completed. |
| `REJECTED_CAPACITY` | Arrival could not enter execution or the bounded queue. |
| `REJECTED_ROUTE` | Injected selector declined the request before dispatch. |
| `TIMEOUT` | Scheduled-origin request deadline expired. |
| `HTTP_ERROR` | Endpoint returned a non-200 HTTP status. |
| `STREAM_ERROR` | SSE or supported chat/usage content violated the protocol contract. |
| `STREAM_LIMIT` | Response bytes, frame bytes/count, content timings or JSON structure exceeded a bound. |
| `INCOMPLETE_STREAM` | EOF/terminal marker arrived without the required complete content and framing. |
| `TRANSPORT_ERROR` | HTTP connection or transport processing failed. |
| `CANCELLED` | Run cancellation stopped an outstanding schedule entry. |
| `DRAIN_TIMEOUT` | Run-wide drain cutoff stopped an outstanding schedule entry. |
| `INTERNAL_ERROR` | Trusted injected code or an unexpected execution path failed. |

### Known HTTP-framing limitation

With the pinned aiohttp 3.13.5 C parser, an invalid HTTP chunk size received
after headers have been delivered can close the socket without waking the
body reader. The evaluator's scheduled-origin deadline still terminates the
request as `TIMEOUT` and closes its client tasks/connections. The loopback test
explicitly gates that sequence and checks this bounded failure. Malformed
framing delivered during initial response parsing may instead surface as
`TRANSPORT_ERROR`; TCP read boundaries are not a reliable error taxonomy.
The pure-Python parser fallback may expose a different error timing; the
compiled-parser regression is marked accordingly. This failure can occupy a
concurrency slot until its deadline and affect subsequent client admission.

Therefore `TIMEOUT` means the client did not complete by its declared deadline;
it is not proof of slow model inference or an overloaded engine. Failed and
partial requests remain in the offered population. No successful latency or
token measurements are inferred from this failure. This dependency limitation
does not justify using private client protocol state to invent a more precise
diagnosis. The relevant pinned paths are aiohttp's
[HTTP parser error handling](https://github.com/aio-libs/aiohttp/blob/v3.13.5/aiohttp/_http_parser.pyx)
and [response exception propagation](https://github.com/aio-libs/aiohttp/blob/v3.13.5/aiohttp/client_proto.py).

## Why the measurement adapter is separate from benchmark tooling

The integration decision was checked against **vLLM 0.26.0** documentation and
source, and **GuideLLM v0.7.3**. No benchmark source code is copied into this
implementation.

vLLM already supplies custom workloads, arrival-rate controls, timed trace
replay, warm-up, detailed results and latency-target goodput. These are useful
for calibration and independent cross-checks. Its CLI explicitly notes that a
concurrency cap can reduce achieved request rate. See the pinned
[benchmark guide](https://docs.vllm.ai/en/v0.26.0/benchmarking/cli/) and
[serve CLI](https://docs.vllm.ai/en/v0.26.0/cli/bench/serve/).

In that version, the benchmark scheduler acquires its concurrency semaphore
before the request function starts timing, so the returned request result does
not expose the required offered-time accounting. Its scheduler also uses wall
time for trace delays. Importing it brings in vLLM dataset and tokenizer
utilities. Inferdrome's small explicit schedule makes its own clock, admission,
and cancellation semantics inspectable. See
[v0.26.0 scheduler source](https://github.com/vllm-project/vllm/blob/v0.26.0/vllm/benchmarks/serve.py).

The pinned chat request implementation timestamps an initial choices event
even if content is empty, records event gaps as ITL, retains generated text and
error tracebacks, and can treat HTTP-200 EOF as success without requiring a
terminal marker. Its returned record cannot recover the first-body-byte or
missing stream-termination observations needed here. This is a mismatch with
this experiment's contract, not a claim that its throughput benchmark has no
value. See
[v0.26.0 request source](https://github.com/vllm-project/vllm/blob/v0.26.0/vllm/benchmarks/lib/endpoint_request_func.py).

GuideLLM v0.7.3 supports OpenAI HTTP backends, fixed-rate and Poisson traffic,
trace workloads, and duration/request/error constraints. It remains a candidate
external campaign tool. Making it a core dependency would also introduce
mandatory Torch, Transformers, datasets, NumPy, Sanic and uvloop dependencies.
PR1 uses the HTTP library directly to keep the measurement slice small. See
[pinned CLI examples](https://github.com/vllm-project/guidellm/blob/v0.7.3/README.md),
[pinned dependencies](https://github.com/vllm-project/guidellm/blob/v0.7.3/pyproject.toml),
and the [v0.7.3 release](https://github.com/vllm-project/guidellm/releases/tag/v0.7.3).

A later calibration recipe can use the installed vLLM 0.26.0 CLI against an
already approved local endpoint. For example, this is a planned bounded
calibration command shape, **not an executed integration or final study**:

```sh
vllm bench serve \
  --backend openai-chat \
  --base-url http://127.0.0.1:8000 \
  --endpoint /v1/chat/completions \
  --model local-model \
  --dataset-name random \
  --input-len 128 --output-len 32 \
  --num-prompts 20 --num-warmups 2 \
  --request-rate 1 --max-concurrency 2 \
  --extra-body '{"temperature":0,"chat_template_kwargs":{"enable_thinking":false}}'
```

That synthetic calibration workload is not the matched routing-study workload.
The final recipe must freeze and replay identical prompt bytes, arrival offsets,
model/tokenizer identities and generation settings across policies. Benchmark
raw output or detailed responses remain subject to the existing external-only
data handling policy; they are not automatically publishable evidence.

## Five delivery slices and their acceptance boundaries

| Slice | Implementation boundary | Acceptance and dependency |
| --- | --- | --- |
| 1. Execution and measurement | `src/inferdrome/evaluation/`, focused local tests, this guide, and the locked HTTP dependency. | Local fake and loopback endpoints exercise concurrency, scheduling, framing, timing, terminal accounting and bounded cleanup. Frozen routing-execution tests still pass. No claim of live GPU qualification. |
| 2. Routing and controlled faults | Four new policy semantics and scoped load-observation/fault modules beside the evaluator. | Depends on slice 1. Define ties, freshness boundaries, missing/malformed observations and health eligibility. Prove independent observer separation, exact fault timing, restoration and bounded background-load cleanup locally. |
| 3. Reproducible study and reporting | Calibration/campaign recipes, trace replay, repeated trials and machine-readable/readable analysis. | Depends on slices 1–2. Freeze moderate and near-capacity settings after the pilot; use matched traffic, controlled warm-up/cache state, recorded order/seeds and rotating overloaded engines. Report uncertainty, failures and achieved versus offered traffic. |
| 4. Bounded prefix-cache experiment | One shared-document versus unique-prompt recipe with prefix caching enabled/disabled. | Follows main-study qualification; safe local recipe work may proceed while live approval is pending. Verify exact token-prefix identity and cache state; measure prefill-related effects. No cache-affinity router or Preble reproduction claim. |
| 5. Real evidence and closure | Permitted sanitized evidence, reproducible commands/configs, analysis, limitations and sign-offs. | Requires separately approved execution and independently verified resource termination. Preserve external-only archives and historical evidence. Paid evaluation outstanding means this slice is outstanding; no release/tag is implied. |

The four planned policies are round-robin, least reported load retaining its
last valid observation, freshness-aware fallback balancing across healthy
engines, and fail-closed rejection when required observations are stale or
unavailable. Their identifiers and semantics will be separate from the frozen
R1 policies. During the controlled fault, only the load information exposed to
the router is frozen. An independent actual-load observer continues measuring;
the router must never consume that ground-truth channel. Health stays distinct.

The primary study metric is successful offered requests meeting predefined
latency targets per second, accompanied by rejection/error/timeout rates,
streaming latency, recovery time and measured or clearly estimated cost.
Rejections stay in the denominator. Successful-request percentiles must include
their sample counts and failure context; tiny samples cannot support a headline
p99 claim. Trial repetitions, rather than many correlated requests alone,
provide the independent replication needed for uncertainty estimates.

vLLM already supports chunked prefill and automatic prefix caching. Later
recipes must record the exact settings and distinguish prefill reuse from
decode work. The cache experiment is inspired by
[Preble](https://arxiv.org/html/2407.00023v1), not its reproduction. See vLLM's
pinned [optimization guide](https://docs.vllm.ai/en/v0.26.0/configuration/optimization/)
and [prefix-caching guide](https://docs.vllm.ai/en/v0.26.0/features/automatic_prefix_caching/).

Early live calibration is useful after local qualification, but starting it
requires a consolidated proposal identifying the source commit, immutable
image/model identities, verified compatibility, host/provider, current quote,
duration and total-cost cap, staging/network/storage costs, abort conditions,
evidence retrieval, destruction procedure and independent absence check.
Previous run approvals do not carry forward. Outstanding live authority blocks
live actions; it does not block the remaining authorized local implementation.
