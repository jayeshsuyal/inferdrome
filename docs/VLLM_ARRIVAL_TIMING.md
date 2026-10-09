# Controlled arrival timing

**Breakpoint, step 2:** change the planned arrival schedule while holding the
request population and measurement contract fixed. This extends the existing
scheduled-arrival client and [request identity](VLLM_REQUEST_IDENTITY.md).
Search, policy comparison, reduction, and held-out confirmation are future work.

## One bounded intervention

Start with a validated, frozen baseline or capacity control plan. Within each
original epoch, split consecutive offers into groups. Each group's first offer
stays at its original time. For every offer in that group:

```text
new_time = max(original_time - max_advance_ns,
               anchor + floor((original_time - anchor) * retained_spacing_bps / 10000))
```

`retained_spacing_bps=10000`, `max_advance_ns=0`, or `group_size=1` reproduces the
original schedule. Zero retained spacing permits simultaneous planned offers,
subject to the advance cap. All arithmetic is integral. No offer moves later,
before its group anchor, or outside its original epoch. The advance cap can
limit how much of the requested compression is applied.

The transform preserves offer count, index order, prompts, per-request token
budgets, document IDs, tenant/class/epoch membership, model inputs, full offered
window, concurrency, deadlines, drain period, and SLO thresholds. Goodput keeps
its original full-window denominator. The first-to-last offer span can change;
instantaneous arrival rate is the intervention. Tied offers retain their index
order in the artifact; actual network or engine processing order is not promised.

Capacity `burst-hot-shift.v1` plans are rejected: that recipe changes document
mix and hotspot size too. Supported capacity sources use the original control
pattern or `control.v1`. Baseline calibration/evaluation and capacity
pilot/evaluation retain their existing phase and seed rules.

## Inspect and run

Preparing and verifying a timing artifact is entirely offline:

```bash
PYTHONPATH=src python -m inferdrome.vllm_arrival_timing prepare \
  --plan /private/study/plan.json --group-size 8 \
  --retained-spacing-bps 2500 --max-advance-ns 1000000000 \
  --output /private/study/timing.json

PYTHONPATH=src python -m inferdrome.vllm_arrival_timing verify \
  --plan /private/study/plan.json --timing /private/study/timing.json \
  --output /private/study/timing-check.json
```

The `inferdrome.vllm-router-arrival-timing.v1` artifact records original and new
times for every indexed offer, source plan/trace hashes, a population hash,
transformed trace hash, parameters, moved count, and maximum applied advance.
The population hash includes prompt hashes, epochs, metadata, and token limits.
Verification regenerates the complete artifact; a rehashed arbitrary schedule
is rejected. Duplicate JSON fields, nonfinite numbers, and bool/float aliases
for integer parameters or arrivals are rejected. Output files are never replaced.

For a future authorized live trial, add both `--timing /private/study/timing.json`
and `--correlate-requests` to the existing `vllm_router_study run` command. Retain
the original plan and token certificate arguments. Invalid timing or supplied
certificates fail before dispatch. Existing GPU campaign commands do not opt in
automatically. This PR ran only local fake replicas and offline checks.

Timed observations use `inferdrome.vllm-router-timed-result.v1` inside the
request-correlation wrapper. `plan_sha256` identifies the timing artifact and
`trace_sha256` its transformed trace. Separate `base_plan_*` and
`base_trace_sha256` fields preserve the original source identity. The original
token certificate remains bound to that unchanged workload; its scope is
explicitly `BASE_WORKLOAD_UNCHANGED_TIMING_ONLY`.

Verify the result offline, then separately check the router ledger:

```bash
PYTHONPATH=src python -m inferdrome.vllm_timed_result \
  --plan /private/study/plan.json --timing /private/study/timing.json \
  --token-certificate /private/study/certificate.json \
  --result /private/study/result.json --output /private/study/result-check.json

PYTHONPATH=src python -m inferdrome.vllm_request_identity \
  --result /private/study/result.json --ledger /private/study/router.jsonl \
  --output /private/study/request-links.json
```

The timed-result check validates provenance, offer coverage, scheduled times,
client-stage ordering, completed token counts, status/accounting consistency,
and independently recomputes summaries. The run CLI also performs this check
after saving the result; invalid results remain inspectable and exit 2.
Interrupted runs retain every offer and the fixed denominator. A structurally
verified interrupted result still has `measurement_comparison_valid=false`.

## What this proves

`VERIFIED` means the supplied timing or client-accounting artifact passed its
stated checks. It does not establish observed router arrival times, engine
scheduling, GPU execution, a policy winner, or causality. Shared IDs and hashes
provide consistency, not authentication. Router receipts require the separate
request-link check; an undispatched offer cannot support valid all-offered
router accounting.

Timed baseline and capacity results both retain `ready_ns` and client scheduling
lag/queue-delay summaries. Requested bursts may be distorted by client limits;
these observations must inform the later comparison procedure. No result here
confirms a Breakpoint counterexample or changes the historical 7.2% capacity
observation. Existing default result schemas, report readers, and release
contracts stay intact. Keep source/result artifacts private until publication
review; the embedded measurement still includes the router origin.
