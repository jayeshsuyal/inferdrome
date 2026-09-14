# Evaluation routing and controlled faults v1

This slice runs one bounded routing trial against two endpoints. It compares a
chosen policy's router-visible observations with a separate load-observation
channel while temporarily freezing one endpoint's published load. Background
requests target that endpoint through a separate bounded replay.

The result is a private `LOCAL_MEASUREMENT_ONLY` JSON record with
`evidence_eligible=false`. Endpoint, model, and declared source identities remain
unverified. A configured fault does not establish that an engine was overloaded,
and a local trial does not establish policy superiority or live GPU qualification.
This is separate from the frozen `routing_execution` policies, fixtures,
receipts, and verifiers. It is not a sealed R1 receipt.

The foreground and background populations each retain the
[inference evaluation v1 contract](INFERENCE_EVALUATION_V1.md), including offered
traffic accounting, streaming validation, scheduled-origin deadlines, redaction,
and private output publication. PR2 adds routing and a controlled fault recipe;
calibration, matched repeated trials, campaign ordering, uncertainty estimates,
and comparative report metrics belong to the next slice.

## Pinned endpoint interfaces

The adapter targets independently operated **vLLM 0.26.0** replicas, each serving
one expected model through one engine with label `engine="0"`. It reads
`GET /metrics` and `GET /health` at the same configured origins used by the
streaming client. It adds no vLLM runtime dependency and copies no benchmark
source. The pinned metrics endpoint exposes Prometheus text and accepts the
exact `/metrics` path without requiring a redirect. See
[v0.26.0 metrics endpoint](https://github.com/vllm-project/vllm/blob/v0.26.0/vllm/entrypoints/serve/instrumentator/metrics.py).

The two selected metrics are gauges with `model_name` and `engine` labels.
`model_name` comes from the served model name; the engine label is the decimal
engine index. Running counts requests in execution batches. Waiting includes
both ordinary waiting and skipped/deferred requests. The policy score is their
sum, not GPU utilization, available capacity, or a queue-duration estimate. See
[v0.26.0 metric declarations and updates](https://github.com/vllm-project/vllm/blob/v0.26.0/vllm/v1/metrics/loggers.py).

For a configured model named `local-model`, the selected samples have this shape:

```text
vllm:num_requests_running{model_name="local-model",engine="0"} 2.0
vllm:num_requests_waiting{model_name="local-model",engine="0"} 3.0
```

The parser requires exactly one of each selected gauge, with exactly those two
labels and the expected values. It accepts finite nonnegative integral counts
up to `2**31 - 1`, including integral decimal/scientific notation. A matching
gauge for another model/engine, extra or duplicate labels, duplicate samples,
sample timestamps, missing components, escaped label values, and invalid counts
are outside the contract. It does not sum engines or silently choose among
multiple matching series. Unrelated metrics and comments are ignored within
the body, line, and text-validation bounds. Missing or disabled metrics never
become an invented zero-load observation.

vLLM's `/health` returns HTTP 200 after its engine health check and 503 for an
engine-dead error. Its asynchronous engine check examines the error state; a
render-only server can return 200 without an engine. Here, a completed bounded
HTTP-200 health acquisition is an availability observation. It does not attest
the model, GPU, inference progress, or remaining capacity. See
[v0.26.0 health route](https://github.com/vllm-project/vllm/blob/v0.26.0/vllm/entrypoints/serve/instrumentator/health.py)
and [asynchronous health check](https://github.com/vllm-project/vllm/blob/v0.26.0/vllm/v1/engine/async_llm.py).

A scrape observes the gauges exposed to the client. It does not supply the
scheduler's update timestamp or establish an atomic, exactly contemporaneous
running/waiting measurement. The independent observer has the same limitation.
Interpret both channels as separately acquired reported-load observations.

## Observation age, retention, and health eligibility

One monotonic epoch is captured before warm-up. Request, decision, observation,
and fault-event timestamps are integer nanosecond offsets from that shared
epoch. An observation records acquisition `started_ns`, `completed_ns`, and
`published_ns`. Its age at a routing decision is:

```text
age_ns = decision_ns - started_ns
fresh = 0 <= age_ns <= configured_freshness_ns
```

Equality is fresh. Health and load have separate freshness bounds. Completion
or republication does not reset age: a slow response can already be stale when
published. Future observations, reordered timestamps, regressing sequences,
and rewriting an existing sequence violate the snapshot contract.

Each endpoint snapshot contains its latest health attempt, last valid published
load, and latest published load attempt. A failed load attempt changes the
attempt status but preserves the valid load's values, sequence, and timestamps.
The record distinguishes never-valid load, stale retained load, and an earlier
valid load retained after a later failed attempt. A still-fresh retained value
remains usable by all three load policies after a bad poll; it is not described
as a newly acquired value.

Health has different retention semantics. The latest attempt must be valid,
healthy, and fresh for an endpoint to be eligible. A failed, missing, malformed,
timed-out, unhealthy, or stale latest health observation makes it ineligible;
an older healthy result does not override that failure. All policies reject
when no endpoint is eligible. Only eligible endpoints' load is required.
Health publication continues throughout the load freeze.

## Four explicit policies

Policy identifiers are independent of the frozen R1 identifiers. Each trial
creates a new policy instance.

| `policy_id` | Selection among healthy eligible endpoints |
| --- | --- |
| `evaluation_round_robin_v1` | Ignore load and rotate through eligible endpoints. |
| `evaluation_least_reported_load_v1` | Require a last valid load for every eligible endpoint, then choose the minimum running-plus-waiting score. Retained stale values remain usable. Reject if any required load has never been valid. |
| `evaluation_freshness_fallback_v1` | If every eligible endpoint has fresh valid retained load, choose the minimum score. Otherwise ignore all load scores and round-robin across the full eligible set. |
| `evaluation_fail_closed_v1` | Require fresh valid retained load for every eligible endpoint, then choose the minimum score. Reject if any required load is missing or stale. |

With one eligible endpoint, round-robin selects it. Freshness fallback also
selects it, recording fallback when its load is missing or stale. Least reported
load still requires a valid retained value; fail closed still requires a fresh
one. An ineligible endpoint's missing load does not make the healthy endpoint's
load unavailable.

The ordered ring is `endpoint-a`, then `endpoint-b`. All cursors start at A:

- Round-robin advances to the position after every selected endpoint and skips
  endpoints that are currently ineligible.
- Minimum-load ties use a separate cursor, advanced only when equal minima
  require a tie choice. A unique minimum does not move it.
- Freshness fallback has its own cursor, advanced only on fallback selections.
  It persists across separate fallback episodes; load-mode ties use the tie
  cursor instead.
- Rejections advance no cursor.

A contiguous fallback-only sequence with both endpoints eligible has assignment
counts differing by at most one. This does not promise balance when eligibility
changes or when decisions switch between load and fallback modes. There are no
hidden local in-flight counters or independent-observer values in policy input.

The synchronous selector runs at the PR1 routing seam after a request leaves
the client queue, before dispatch. Decision records retain the request index,
eligible set, selected endpoint, mode/reason, load ages/sequences, and immutable
snapshot used. A selected request may still expire or be cancelled before
dispatch because PR1 rechecks its cutoffs. Capacity rejection or expiry before
selection may have no decision record. Join decisions to foreground request
records by `request_index`; a decision is not an HTTP attempt.

## Scoped fault and independent observer

The router's health/load client and the independent observer's load client are
different owned clients. Each endpoint has three sequential polling channels:
`HEALTH`, `ROUTER_LOAD`, and `INDEPENDENT_LOAD`. The policy adapter receives only
the router publisher and its immutable snapshots. It has no observer client,
history, sink, or getter. Sharing a parser does not share the measured values.
This is logical dataflow separation within trusted Python, not a security
sandbox against arbitrary injected code.

Warm-up lasts until the declared `warmup_ns` check. Both endpoints must then
have fresh healthy router observations, fresh valid router load, and fresh valid
independent load. Failure yields `WARMUP_FAILED`, with every planned foreground
and background request retained as cancelled and no request dispatch. Warm-up
establishes observation availability, not a measured idle baseline or capacity.

After warm-up passes, the recipe starts the foreground replay. At the actual
freeze activation it holds the target endpoint's router-visible load, including
its timestamp and sequence. Suppressed attempts do not update its visible
latest-attempt status. Their load values are discarded, not queued for later
delivery. Observation records mark `published_to_router=false` and erase those
running/waiting values. Health, the other endpoint's router load, and the
independent observer continue normally.

Background traffic can start only after that freeze is activated. Its offers
target only the selected endpoint, and it has separate admission, queue,
concurrency, output, timeout, and drain limits. The background population is
never folded into the foreground offered denominator. Scheduled stop prevents
new background dispatch and requests cancellation of outstanding client work.
Closing a client request is not independent proof of server-side job termination.

Restoration reopens publication while the background phase is scheduled to
continue. It does not replay a suppressed sample or make an old timestamp
fresh. A target load acquisition that started before the actual restore cutoff
is still discarded if it finishes afterward. An acquisition started **at or
after** that cutoff can be published. Subsequent policy use still depends on
health eligibility and the policy's freshness requirements.

The configuration requires:

```text
warmup_ns < freeze_start_ns < restore_ns < background_stop_ns <= duration_ns
```

These are planned offsets. Event-loop lag can delay or compress actual phases.
Use the recorded activation times and first background dispatch to determine
what happened. A started background phase can dispatch no requests, fail to
produce the intended load, or have no active request at restoration. The result
explicitly says `actual_overload=NOT_ESTABLISHED_BY_FAULT_SCHEDULE`.

Polling uses one outstanding acquisition per endpoint/channel, bounded response
bytes and a bounded timeout. Polls skip missed cadence intervals instead of
queuing catch-up requests or retrying failures. Attempt sequences and timestamps
show actual acquisition coverage; absent intervals are not zero load. Keep
cadence and bounds identical when later comparing policies, because polling is
itself endpoint traffic. The poll deadline wins an acquisition-completion tie.
The pinned aiohttp 3.13.5 HTTP-framing limitation also
applies to probes: a body reader can remain pending after certain malformed
chunked responses until the bounded client timeout. Such a timeout does not
identify slow inference.

## One small local recipe

This example assumes two endpoints you already operate at loopback ports 8000
and 8001, serving `local-model` with the pinned metrics contract. Replace those
declarations to match the intended local setup. The forty-zero source commit
is an explicit **local demo placeholder**, not a verified source identity.
The input-generation command opens no endpoint connections.

```sh
evaluation_dir=$(mktemp -d)
uv run --locked python - "$evaluation_dir/config.json" <<'PY'
import json
import sys
from pathlib import Path

common = {
    "schema_version": "inferdrome.evaluation-config.v1",
    "source_commit": "0" * 40,
    "model": "local-model",
    "max_tokens": 32,
    "temperature": 0,
    "enable_thinking": False,
    "endpoints": [
        {"endpoint_id": "endpoint-a", "origin": "http://127.0.0.1:8000"},
        {"endpoint_id": "endpoint-b", "origin": "http://127.0.0.1:8001"},
    ],
    "bounds": {
        "max_requests": 6,
        "concurrency": 2,
        "max_queue": 2,
        "duration_ns": 3_000_000_000,
        "request_timeout_ns": 1_000_000_000,
        "drain_ns": 1_000_000_000,
        "cleanup_timeout_ns": 1_000_000_000,
        "max_stream_bytes": 65_536,
        "max_event_bytes": 8192,
        "max_content_events": 64,
    },
}

def population(offsets, prompt):
    return {
        **common,
        "offers": [
            {"scheduled_ns": ns, "endpoint_id": "endpoint-a", "prompt": prompt}
            for ns in offsets
        ],
    }

config = {
    "schema_version": "inferdrome.evaluation-routing-config.v1",
    "policy_id": "evaluation_freshness_fallback_v1",
    "foreground": population(
        [400_000_000, 800_000_000, 1_200_000_000,
         1_600_000_000, 2_000_000_000, 2_600_000_000],
        "Write one short sentence about a river.",
    ),
    "background": population(
        [800_000_000, 1_100_000_000, 1_400_000_000,
         1_700_000_000, 2_000_000_000, 2_300_000_000],
        "Write a short description of a mountain.",
    ),
    "telemetry": {
        "interval_ns": 100_000_000,
        "poll_timeout_ns": 80_000_000,
        "health_freshness_ns": 300_000_000,
        "load_freshness_ns": 300_000_000,
        "warmup_ns": 300_000_000,
        "max_observations": 240,
        "max_response_bytes": 1_048_576,
        "cleanup_timeout_ns": 1_000_000_000,
    },
    "fault": {
        "target_endpoint_id": "endpoint-a",
        "freeze_start_ns": 800_000_000,
        "restore_ns": 1_500_000_000,
        "background_stop_ns": 2_500_000_000,
    },
}
Path(sys.argv[1]).write_text(json.dumps(config), encoding="utf-8")
PY
uv run --locked python -m inferdrome.evaluation routing-run \
  --config "$evaluation_dir/config.json" \
  --output "$evaluation_dir/result.json"
```

The last command sends bounded traffic to the declared endpoints. It does not
create servers, obtain credentials, rent resources, or grant live-run approval.
The small request population is a mechanism rehearsal, not an overload
calibration. Select another single `policy_id` and a new output path to run
another recipe; PR2 does not automatically execute a multi-policy campaign.

Both nested populations use unchanged `EvaluationConfig` inputs. Foreground
offers retain their required `endpoint_id` field, but the injected policy chooses
the actual dispatched endpoint. Background assignments stay fixed to the fault
target. The populations must share endpoints, model, declared source commit,
duration, and drain allowance. Foreground offers cannot precede warm-up;
background offers must be at or after the scheduled freeze and strictly before
the scheduled background stop.

## Bounds and output handling

The strict top-level schema is `inferdrome.evaluation-routing-config.v1`.
Unknown fields, ambiguous JSON, invalid types, and incompatible populations are
rejected before requests. The input-size ceiling remains 16 MiB, and the PR1
endpoint-origin and per-request limits still apply.

| Combined or telemetry bound | Accepted ceiling |
| --- | ---: |
| Foreground plus background declared `max_requests` | 4,000 |
| Foreground plus background concurrency | 64 |
| Foreground plus background queue capacity | 1,024 |
| Sum of `max_requests * max_content_events` | 500,000 |
| Foreground plus background prompt bytes | 8 MiB |
| Poll interval | 1 ms to 10 seconds |
| Poll timeout | 1 ms to 5 seconds |
| Health/load freshness, separately | 60 seconds |
| Warm-up | 1 ms to 30 seconds |
| Stored observation attempts | 10,000 |
| Each probe response body | 1 MiB |
| Probe/owner cleanup guard | 1 ms to 5 seconds |

There are six polling channels in addition to the bounded request populations.
Preflight requires
`6 * ceil((duration_ns + drain_ns) / interval_ns) <= max_observations`.
The example permits at most 240 attempts across those channels. The metrics
parser additionally limits each line to 16 KiB and the body to 16,384 lines.
HTTP clients use bounded headers/reads and no ambient credentials, proxies,
cookies, redirects, retries, or decompression.

The controller restores the publication gate and stops/drains owned workers,
population clients, and probe clients on completion, cancellation, or failure.
Cleanup uses real event-loop time even for a simulated trial clock. A controller,
observation-worker, or cleanup failure blocks completed-result publication;
it does not produce a record claiming clean completion. Per-request unexpected
failures retain PR1's `INTERNAL_ERROR` outcome. The cleanup assertion concerns
client tasks and connections, not independent verification of a remote engine's
request state.

The CLI reserves a new private output file before opening clients and uses the
PR1 no-replace publication path. Prompts, endpoint origins, generated content,
raw metric bodies, and raw exception messages are not output. The combined
record includes the canonical config digest, declared bounds/schedule,
sanitized observations and decisions, and separate foreground/background
results. A config digest is not runtime identity verification or permission to
publish the private record.

## Read the recovery observations precisely

The result schema is `inferdrome.evaluation-routing-result.v1`. Trial status is
`COMPLETED`, `WARMUP_FAILED`, or `CANCELLED`. `COMPLETED` describes the recipe
lifecycle; inspect both populations' individual outcomes to assess requests.
The CLI exits 0 for completed trials, 2 for warm-up failure or execution/input
failure, and 130 for cancellation. Warm-up failure can produce a completed
measurement record describing that failure; controller, observation-worker,
or cleanup failures cannot.
All PR1 planned-offer denominators, failures, partial timings, and usage
provenance remain intact. Background offered/dispatched/completed/error counts
stay separate from foreground traffic.

| Recovery field | Meaning |
| --- | --- |
| `freeze_started_ns` | Observed activation of the target load-publication freeze. |
| `telemetry_restored_ns` | Observed normal restoration activation. |
| `first_restored_publication_ns` | First valid target router-load publication from an acquisition started at or after restoration. |
| `first_decision_using_restored_load_ns` | First load-mode decision that uses fresh post-restoration target load while the target is healthy and eligible. |
| `first_dispatch_using_restored_load_ns` | First actual foreground dispatch associated with such a decision; a selection alone is insufficient. |
| `first_background_dispatch_ns` | First actual background dispatch, separate from the background-start event. |
| `background_active_at_restore` | Whether a dispatched background request remained nonterminal at the observed restoration time. |

Using restored target load means including it in the eligible minimum-load
comparison; the selected endpoint can still be the other endpoint. It does not
mean traffic has returned to the target or that target load has returned to an
idle baseline. Publication, decision, dispatch, and independently observed
load changes are different events.

Missing timestamps remain null with
`missing_timestamps=UNOBSERVED_OR_CENSORED_NOT_ZERO`. An early stop, failed poll,
stale sample, ineligible target, or lack of a later dispatched foreground offer
can leave recovery unobserved. Round-robin has
`load_recovery_applicable=false`: it does not consume load observations.
Restoration during cleanup is recorded as a cleanup event, not substituted for
normal measured recovery.

Foreground latency remains a client streaming observation. First content is
not exact first-token timing, content-event intervals are not exact inter-token
latencies, and scrape age is not proof of actual contemporaneous engine load.
This slice records the mechanism and its limits; later study analysis must
retain offered traffic, rejected/failed requests, acquisition coverage, actual
fault overlap, and censored recovery before comparing policies.
