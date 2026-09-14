# Matched inference evaluation studies v1

PR3 adds a bounded study compiler, sequential trial execution, and offline
reporting to the separately versioned evaluation path. It compares the four
existing routing policies against the same finite foreground workload within
each matched block. It supports healthy routing and the existing controlled
stale-load recipe. It preserves the
[PR1 streaming measurement contract](INFERENCE_EVALUATION_V1.md) and
[PR2 routing and fault contract](EVALUATION_ROUTING_FAULTS_V1.md), including their private endpoint restrictions,
deadlines, admission bounds, observation semantics, and cleanup ownership.

Ordinary execution artifacts are `LOCAL_MEASUREMENT_ONLY`, with
`evidence_eligible=false`; injected synthetic test results retain their explicit
`SYNTHETIC_ONLY` label through reporting. The offline plan is a declaration, not
a measurement.
Calibration remains `UNCALIBRATED_REHEARSAL`, runtime identities are unverified,
and cost is unavailable. A reproducible plan does not establish live capacity,
actual overload, policy superiority, independent execution, or a sealed R1
receipt. The frozen `routing_execution` experiment and its policies, fixtures,
receipts, and verifiers retain their existing semantics.

## Entry points and artifacts

```sh
python -m inferdrome.evaluation study-plan --config CONFIG --output PLAN
python -m inferdrome.evaluation study-run --config CONFIG --output-dir NEW_STUDY_DIR
python -m inferdrome.evaluation study-report --config CONFIG --study-dir STUDY_DIR --output-dir NEW_REPORT_DIR
```

`study-plan` is offline. It validates and compiles the input, then writes a
sanitized expected trial ledger to a new private file. It opens no endpoint
connections. `study-run` executes the compiled trials one at a time using the
existing aiohttp transport and public healthy/fault runner APIs. `study-report`
is offline: it recompiles the supplied private config, verifies the saved plan,
manifest, and expected trial artifacts, and writes `report.json` and `report.md`
to a new directory. Existing `run`, `demo`, and `routing-run` commands remain
unchanged.

`study-run` exits with 0 for a completed manifest, 130 for cancellation, and 2
for an aborted study. A successful offline report command exits with 0 even
when its validated input describes an incomplete study; read the report's
study and comparison statuses. Command failures use sanitized messages.

The Python interfaces are `load_study_config_bytes`, `compile_study`,
`run_study`, and `report_study`. `CompiledStudy.trials` is a finite tuple.
Each `CompiledTrial` contains the selected healthy/fault config, generated trial
ID, block/profile/scenario, target, repeat index, seeds, policy, config digest,
matched payload digest, offered window, SLOs, cooldown, and duration allowance.
`CompiledStudy.to_dict()` is the sanitized plan projection. The in-memory
`config` attributes still contain private inputs and are not report payloads.

Schema versions are independent of the earlier evaluation schemas:

| Artifact | Schema |
| --- | --- |
| Study input | `inferdrome.evaluation-study-config.v1` |
| Compiled plan | `inferdrome.evaluation-study-plan.v1` |
| Per-trial envelope | `inferdrome.evaluation-study-trial-result.v1` |
| Final study manifest | `inferdrome.evaluation-study-result.v1` |
| Offline report | `inferdrome.evaluation-study-report.v1` |

The envelope contains the unchanged routing result or the new
`inferdrome.evaluation-healthy-result.v1` result. Files contain canonical JSON
with a trailing newline. Saved plan and envelope identities bind their exact
stored bytes. Config and matched payload digests bind canonical model
projections; they are not runtime attestations.

## One declared finite design

The input has six top-level fields: `schema_version`, `profiles`, `blocks`,
`preparation`, `limits`, and `reporting`. Unknown fields, duplicate JSON keys,
non-finite numbers, invalid UTF-8, ambiguous integer primitives, and inputs
larger than 16 MiB are rejected. Profiles and blocks have bounded identifiers
matching `[a-z][a-z0-9_-]{0,31}`; do not use private descriptions as identifiers.

A profile declares `profile_id`, `load_level`, `window_start_ns`,
`window_end_ns`, `first_content_slo_ns`, and `completion_slo_ns`. The available
load labels are `REHEARSAL`, `MODERATE`, and `NEAR_CAPACITY`; every one retains
`calibration_status=UNCALIBRATED_REHEARSAL`. A label cannot certify a rate.
An authorized later pilot must choose actual workload/rate and concurrency
sweeps, prompt mix, response limits, schedules, and SLOs. Client queue rejection,
capacity rejection, or dispatch lag cannot by itself identify engine capacity.

Each ordered block declares:

- `block_id`, `profile_id`, `scenario` (`HEALTHY` or `STALE_LOAD`), and
  `repeat_index`.
- `workload_seed` and `order_seed`, each an integer from zero through
  `2**32 - 1`, plus the actual four-entry `policy_order`.
- One complete PR1 `foreground` config and one PR2 `telemetry` config.
- For `STALE_LOAD`, a complete `background` config and `fault` timing; for
  `HEALTHY`, neither population nor fault is supplied.

The four policy IDs appear exactly once per block:

1. `evaluation_round_robin_v1`: rotate across healthy eligible endpoints.
2. `evaluation_least_reported_load_v1`: use minimum last valid reported
   running-plus-waiting load, including retained stale values.
3. `evaluation_freshness_fallback_v1`: use minimum fresh load when available for
   every eligible endpoint; otherwise round-robin across the eligible set.
4. `evaluation_fail_closed_v1`: require fresh valid load for every eligible
   endpoint or reject.

The [PR2 guide](EVALUATION_ROUTING_FAULTS_V1.md) defines health eligibility,
failed-poll retention, equality at freshness boundaries, tie cursors, and
rejection behavior. Those rules apply unchanged in both study scenarios.

The compiler creates four configs from one block, changing only `policy_id`.
Prompt bytes/order, scheduled arrivals, request settings, resource bounds,
telemetry, and any fault/background recipe are identical within the block.
Foreground offer endpoint IDs remain replay placeholders overridden by routing;
they are not recorded assignments. The background stays fixed to the fault
target. All study blocks share endpoints, declared model/source, and response
settings; study foreground/background generation settings also match.

Each full trial config has its own `config_sha256`. The policy-independent
`workload_sha256` also binds block metadata, both offer populations, telemetry,
the profile window/SLOs, and preparation declaration. It excludes the policy
order and replaces endpoint origins with endpoint IDs. Changing an origin
therefore changes the full config binding, while leaving an otherwise identical
matched payload comparable. Neither digest exposes prompt bytes or endpoints.

Seeds describe the externally supplied finite workload/order. The compiler
does not generate prompts or arrival times, randomize during execution, or
send a model-generation seed. Changing only a seed does not establish a new
independent workload. Explicit finite schedules are replayed without adapting
offered rate to responses.

Block IDs are unique and every declared profile is used. Repeat indices start
at zero and are contiguous in each profile/scenario/target stratum. Fault
targets alternate A/B within each profile's ordered fault blocks. Within each
profile/scenario, every policy's count at each order position differs by at most
one. A single four-policy rehearsal block is allowed. This position rule does
not establish balance within each target, balance of predecessor/carry-over
pairs, randomization, or independent repetitions. Record the actual order and
rotate targets before observing outcomes.

## Healthy observations, model warm-up, and cache declarations

Healthy routing uses `HealthyRoutingConfig` and `run_routing_healthy` with the
shared observation/client ownership session. It has no dummy background,
publication freeze, fault schedule, or load-recovery metric. Both scenarios
start fresh policy cursors and observation state for every trial. Both retain
the two endpoints' health, router-load, and independent-load channels, with
separate router/observer clients and dataflow. The independent values cannot
influence routing through the supported adapter.

Telemetry warm-up requires fresh healthy observations and fresh valid router
and independent load for both endpoints at the declared warm-up check. This
checks observation availability; it does not warm the model or verify caches.
Warm-up failure returns all planned offers as cancelled without dispatch and
stops the remaining study trials.

`preparation` explicitly declares `cache_state` (`UNKNOWN`, `DECLARED_COLD`, or
`DECLARED_WARM`) and `prefix_caching` (`UNKNOWN`, `DECLARED_ENABLED`, or
`DECLARED_DISABLED`). Model warm-up is only
`EXTERNALLY_PREPARED_UNMEASURED`, with a required `model_warmup_reference` in
`sha256:<64 lowercase hex digits>` form. `serving_image_reference` may be a
digest or null. These are common declarations for all policies, not proof that
server preparation occurred or remained identical. Runtime verification stays
`UNVERIFIED` and cost stays `UNAVAILABLE`.

There are no preparation hooks, shell commands, arbitrary reset URLs, automatic
model warm-up replays, or server cache controls in the config. External warm-up
requests, tokens, duration, and cost are unknown and never filled with zero.
The campaign request and duration budget covers foreground/background replay
and declared client lifecycle allowances; it excludes unmeasured external
preparation. No whole-operation or GPU-spend bound is inferred from that budget.
The shared-prefix/cache experiment remains outside this slice.

## Preflight limits and sequential ownership

The compiler admits at most 16 profiles, 64 blocks, and 256 trial executions.
`limits` may tighten these execution ceilings:

| Limit | Default / hard maximum |
| --- | --- |
| `max_trials` | 256 |
| `max_planned_requests` | 100,000 foreground plus background offers across all four policies |
| `max_duration_ns` | 86,400,000,000,000 ns (24 hours) |
| `cooldown_ns` | 0; maximum 60,000,000,000 ns per between-trial gap |
| `per_trial_result_bytes` | 2 MiB; maximum 64 MiB |
| `total_output_bytes` | 1 GiB |

The total output preflight reserves `trial_count * per_trial_result_bytes` plus
1 MiB for the plan and final manifest. It rejects an impossible combination;
for example, sixteen 64 MiB result allowances leave no metadata space within
1 GiB. Before clients start, actual plan and conservative manifest sizes must
fit that reserve. Actual output is bounded again at publication. A result that
does not fit is a failed trial publication, not a truncated successful result.

Existing per-trial limits remain in force. Fault populations together allow
at most 4,000 declared requests, concurrency 64, queue capacity 1,024, 500,000
stored content-event timing slots, and 8 MiB of prompt bytes. Healthy routing
uses the same 4,000-request/500,000-timing ceilings with PR1's single-population
limits. Both scenarios preflight at most 10,000 six-channel telemetry samples.
Thousands of study requests arise across trials without broadening these bounds.

For each trial, preflight conservatively allows `duration + drain + 7*R + 4*T`,
where `R` is the maximum population cleanup timeout and `T` is the telemetry
cleanup timeout. This covers replay cleanup before controller completion plus
the controller's separate worker, population, and probe cleanup allowances.
The study adds those allowances for every trial and `cooldown_ns` for each of
the `trial_count - 1` gaps. This is conservative admission accounting, not a
hard real-time guarantee for filesystem fsync, event-loop scheduling, injected
code, or independently operated servers.

Execution runs one trial at a time and closes its owned clients before starting
the next. Cooldown is an elapsed wait; it does not establish idle engines or
reset caches. SIGINT/SIGTERM, task cancellation, or the study deadline prevents
later trials and propagates stop to the current owner. Existing per-trial
cleanup/drain semantics still apply. Per-request HTTP, stream, timeout,
rejection, and `INTERNAL_ERROR` outcomes remain measurements. A controller,
observer, cleanup, result-validation, or publication failure aborts later
trials. There is no automatic retry, replacement trial, mutation, or resume.

## Private bundle and strict offline import

Execution reserves a new directory with mode `0700`, then commits `plan.json`
before creating endpoint clients. Returned results are written once as
`trial-0000.json` through at most `trial-0255.json`. A final `manifest.json`
describes every planned slot. Files are committed with mode `0400` through a
held directory descriptor and no-replace writes. Inputs are regular, bounded,
private files whose identity and size are checked while reading. Symlinks,
unsafe ancestors, replaced files, and caller-supplied artifact paths are not
accepted by the supported filesystem path.

The study status is `COMPLETED`, `CANCELLED`, or `ABORTED`. Its ordered trial
ledger distinguishes:

- `RETURNED`: a validated local result was committed; its result status can be
  `COMPLETED`, `WARMUP_FAILED`, or `CANCELLED`.
- `ABORTED`: the attempted trial produced no committed result; the ledger uses
  a finite failure category and leaves cleanup confirmation unknown.
- `NOT_RUN`: no trial started and no result or request rows are invented.

Earlier committed results remain available after an abort. A process crash or
failed final manifest publication may leave an inspection-only partial
directory. Without a valid final manifest it is not accepted by normal
`study-report`. The directory is not silently repaired or resumed.

Offline import requires the original expected config. It recompiles the plan
and compares the saved canonical plan bytes. It verifies the manifest's exact
plan/config bindings and ordered coverage, each expected generated filename
and exact result digest, and each envelope's plan/trial/block/workload/config
bindings. Closed result validation checks request coverage and schedules,
integer/timestamp ordering, attempts, success/protocol consistency, counts,
policy decisions, telemetry, fault/recovery fields, and declared bounds against
that expected trial. Aggregate counts are recomputed rather than trusted.
Unknown fields or versions and contradictory success records fail closed.

This detects structural/binding errors; it does not prove that recorded events
occurred, that two trials were independently run, or that endpoints served the
declared model/source. Prompt bodies, generated text, endpoint origins, raw
model names, and arbitrary server error strings are absent from published
plans, measurements, reports, and sanitized error categories.

The report is a separate private output directory. Its `report.json` and
`report.md` are each bounded to 4 MiB; their combined 8 MiB allowance is separate
from the execution bundle's configured storage cap. JSON retains full per-trial
population/provenance details; Markdown is a compact view of the same scores
and comparison status.

## Fixed offered-cohort goodput

The foreground measurement window is `[window_start_ns, window_end_ns)` in
nanoseconds from the shared trial epoch, which begins before telemetry warm-up.
Every foreground offer lies in this window. `window_end_ns` equals the PR1
foreground `duration_ns`, and warm-up is no later than the window start.
SLO validation requires:

```text
0 < first_content_slo_ns <= completion_slo_ns < request_timeout_ns
drain_ns >= completion_slo_ns
```

For a returned trial, let `N` be its entire planned foreground offered count,
`W = window_end_ns - window_start_ns`, and `G` be the count satisfying all of:

```text
outcome == SUCCESS
first_content_ns - scheduled_ns <= first_content_slo_ns
terminal_ns - scheduled_ns <= completion_slo_ns
```

The primary rates are `slo_goodput_rps = G * 1_000_000_000 / W` and
`slo_success_fraction = G / N`. `offered_rate_rps` uses `N` over that same fixed
window. Equality meets an SLO only for a request actually recorded as `SUCCESS`;
it does not override the runner's exact deadline behavior. A response that
starts quickly and then fails is not a qualifying success.

All offered foreground requests remain in `N`, including queue/routing
rejections, timeout, partial/failed streams, cancellation, and cancellation
before an offer becomes due. A reject-all returned trial has `G=0`; its time
divisor does not collapse. No successful-only or first-to-last-completion
window replaces `W`. An offer can complete during the declared drain and count
if both SLOs hold. Drain is not added to `W`; completion-during-drain counts,
last offer, and actual elapsed time are reported separately.

Background traffic has a separate population and never enters `N`. External
model warm-up remains unmeasured. A trial with no returned result has unknown
metrics, not an invented zero, and its planned position remains in study
coverage. Incomplete studies suppress the comparative headline and intervals.

## Latency, usage, repeated variation, and recovery

Latency uses scheduled-arrival origin for the main score. Dispatch lag and
dispatch-origin timing are separate. `time to first content` is a client
streaming-content observation; it is not exact TTFT, token decode timing,
TPOT, wire-send time, or first wire byte. Content chunks may contain multiple
tokens. The known pinned aiohttp malformed-body framing timeout remains an
observed client timeout and does not identify slow inference.

Successful latency quantiles include all `SUCCESS` records, including SLO
misses. They use exact integer nearest-rank quantiles with explicit counts.
p99 is withheld below 1,000 successful samples and remains descriptive above
that floor. The floor is not a precision, independence, or statistical power
guarantee. Partial-content timing is retained by non-success outcome and is
not mixed into successful latency. Dispatch lag covers all dispatched outcomes.
Partial-outcome and all-outcome dispatch-lag p99 values are withheld as
`NOT_A_SUCCESS_LATENCY_POPULATION`.
Missing observations stay absent rather than becoming zero. The report does
not average per-trial percentiles into a claimed pooled percentile.

Usage totals include only `SERVER_REPORTED_STREAM_USAGE`, with reported/missing
coverage and separate successful/other-outcome populations. Totals use exact
decimal strings to avoid rounding accumulated counts through JSON binary64.
Content-event counts are not token counts; missing usage is not inferred from
the prompt or generated text. Rational scores retain numerators/denominators
and six-place decimal-half-even strings using the existing unchanged numeric
helpers, without adopting frozen reducer populations or metric IDs.

Comparisons are stratified by profile, scenario, and target endpoint. A matched
block, containing all four completed policy trials, is the replication unit.
The report gives per-policy trial scores and observed variation, plus paired
mean goodput differences against round-robin. It does not treat individual
requests as independent experimental replicates.

The reporting convention is fixed before results: `p99_min_successes=1000`,
`bootstrap_resamples=2000`, `confidence_percent=90`,
`minimum_complete_blocks=8`, and an explicit `bootstrap_seed`. For each
eligible stratum, bootstrap draws resample whole paired blocks with replacement;
the 5th/95th nearest-rank bootstrap percentiles form the reported 90% interval.
Fewer than eight complete blocks in that stratum, or any incomplete study,
withholds the interval. Eight total fault blocks split across two targets do
not supply eight blocks per target. The interval assumes independent,
representative blocks; fixed order, cache carry-over, common server state, or
reused workload seeds can violate that assumption. These are descriptive
uncalibrated comparisons, not population-wide reliability claims or a basis
for declaring one strategy a winner.

Fault recovery starts at the actual telemetry-restored event. Publication,
fresh load-based decision use, and dispatch following such a decision are
distinct observations. Missing restoration or recovery remains
`RESTORE_NOT_OBSERVED` or `UNOBSERVED_OR_CENSORED`, with the available observation
horizon, rather than zero duration. Round-robin has no load-use recovery metric;
publication remains descriptive. Healthy trials have no fault recovery.
Coverage retains observed/censored/not-applicable counts, and background-active
state at restore; a configured fault still does not establish actual overload.

## Small public synthetic plan

This offline example creates one healthy block with four public synthetic
prompts per policy. The model, loopback endpoints, forty-zero source commit,
and zero warm-up digest are explicit rehearsal placeholders. It is not a
calibration, model warm-up receipt, or approved live execution. The shell/Python
below only creates a private input and compiles a plan; it starts no server and
sends no requests.

```sh
study_dir=$(mktemp -d)
python - "$study_dir/config.json" <<'PY'
import json
import os
import sys

second = 1_000_000_000
policies = [
    "evaluation_round_robin_v1",
    "evaluation_least_reported_load_v1",
    "evaluation_freshness_fallback_v1",
    "evaluation_fail_closed_v1",
]
foreground = {
    "schema_version": "inferdrome.evaluation-config.v1",
    "source_commit": "0" * 40,
    "model": "public-synthetic-model",
    "max_tokens": 16,
    "temperature": 0,
    "enable_thinking": False,
    "endpoints": [
        {"endpoint_id": "endpoint-a", "origin": "http://127.0.0.1:8000"},
        {"endpoint_id": "endpoint-b", "origin": "http://127.0.0.1:8001"},
    ],
    "bounds": {
        "max_requests": 4, "concurrency": 2, "max_queue": 2,
        "duration_ns": 2 * second, "request_timeout_ns": second,
        "drain_ns": second, "cleanup_timeout_ns": second,
        "max_content_events": 64,
    },
    "offers": [
        {"scheduled_ns": second // 2 + index * second // 4,
         "endpoint_id": "endpoint-a",
         "prompt": f"Public synthetic rehearsal: name the number {index}."}
        for index in range(4)
    ],
}
config = {
    "schema_version": "inferdrome.evaluation-study-config.v1",
    "profiles": [{
        "profile_id": "public-synthetic", "load_level": "REHEARSAL",
        "window_start_ns": second // 2, "window_end_ns": 2 * second,
        "first_content_slo_ns": second // 4,
        "completion_slo_ns": second // 2,
    }],
    "blocks": [{
        "block_id": "block-0", "profile_id": "public-synthetic",
        "scenario": "HEALTHY", "repeat_index": 0,
        "workload_seed": 17, "order_seed": 23,
        "policy_order": policies, "foreground": foreground,
        "telemetry": {"warmup_ns": second // 2},
    }],
    "preparation": {
        "cache_state": "UNKNOWN", "prefix_caching": "UNKNOWN",
        "model_warmup_reference": "sha256:" + "0" * 64,
    },
    "limits": {"max_trials": 4, "max_planned_requests": 16},
    "reporting": {"bootstrap_seed": 37},
}
descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    json.dump(config, output)
PY
python -m inferdrome.evaluation study-plan --config "$study_dir/config.json" --output "$study_dir/plan.json"
```

The compiled plan contains four trials and sixteen planned foreground offers.
Its tiny sample cannot publish a successful-latency p99 or a block-bootstrap
interval. For later authorized execution, supply a reviewed config matching
endpoints you already operate, their serving declarations, and actual external
preparation. Run it into a new directory and report into another new directory:

```sh
python -m inferdrome.evaluation study-run --config REVIEWED_CONFIG --output-dir NEW_STUDY_DIR
python -m inferdrome.evaluation study-report --config REVIEWED_CONFIG --study-dir NEW_STUDY_DIR --output-dir NEW_REPORT_DIR
```

These commands add no provider, deployment, spend, or broad campaign framework.
No live pilot, capacity threshold, cost estimate, or serving result is supplied
by this guide.
