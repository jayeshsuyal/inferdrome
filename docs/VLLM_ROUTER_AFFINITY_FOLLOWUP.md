# Saturation-gated affinity follow-up: diagnosis and operator protocol

This is a prepared experiment, **not a measured speedup**. The previous two
A100 study completed 48 valid evaluation windows. All four policies met the
combined SLO at 2 and 4 requests/second and failed at 6. The boundary within
4–6 remains unresolved. The prior rental was destroyed; its instance ID and
endpoint must not be reused. A new rental requires a new quoted offer and
explicit whole-run spending approval.

## Verified diagnosis and its limit

The private 28 September run's [published report](VLLM_ROUTER_CAPACITY_V2.md)
has SHA-256
`e47a6d46f9ed016bf0b97ccb8b50309dc3c8aa88f10db0e95ba7065c76ff27ca`.
The [aggregate diagnosis](data/vllm-router-followup-diagnosis.json) is generated
by `inferdrome.vllm_router_followup_analysis`. It checks retrieved raw files
against the export inventory, regenerates the v2 report with the original
plan, certificate, client population, ledger, reset and metrics validators,
and requires exact equality with the published report. Its JSON includes the
report, export inventory, session and selected input-file SHA-256 values. It
contains aggregate counters and route states, never prompts, document hashes,
provider addresses, credentials or private engine logs.

At 6 requests/second, across four equal 120-second evaluation windows per
policy:

| Policy | Mean SLO goodput/s | Prefix-token hit fraction | Offered |
| --- | ---: | ---: | ---: |
| Round robin | 5.4729 | 68.58% | 2,880 |
| Least busy | 5.2542 | 68.23% | 2,880 |
| Cache only | 5.6333 | 92.68% | 2,880 |
| Existing cache plus load | 5.5104 | 79.41% | 2,880 |

Hit fractions divide **summed valid hits by summed valid queries** across both
replicas and four blocks. They are not averages of percentages. The hybrid
recorded 399 relative-load escapes and 1,672 estimated-affinity ties among
2,880 routes. Cache only had 2,688 exclusive estimated-affinity observations.
Among the 399 escapes, 300 happened when the preferred replica had at most
nine in-flight router requests; 144 were at eight and 94 at nine. Within the
same document and approximate 40-second router-arrival third, the next route
after an escape had a tie in 286 of 314 observed transitions. This ordering
is **associational**: route and client rows lack a shared request ID, and cache
counters are aggregate snapshots. No per-request cache hit, KV residency or
escape-caused loss can be inferred. The existing control already cycles all
48 documents and shifts a 40% hotspot A→B→A. All observed revisit gaps were
under the history's 60-second TTL.

Across the four 6-rps blocks, mean completed-request p95 values for first
content / completion / post-first-content were approximately 394 / 5,084 /
4,744 ms for cache only and 462 / 5,545 / 5,148 ms for the hybrid. These are
means of per-block p95 values, not pooled quantiles or causal phase measures.
Engine queue duration, prefill and decode phase times, preemption events and
per-request prefix hits were unavailable. Router `busy` counts in-flight
requests, not token-weighted engine work or measured service rate.

Reproduce the diagnosis from the verified **private** archive in a local
checkout with the project Python environment:

```sh
ARCHIVE=/private/verified/capacity-study-aa25f31/operator/live-53303329
PYTHONPATH=src python -m inferdrome.vllm_router_followup_analysis \
  --raw-root "$ARCHIVE/retrieved/private/capacity-run" \
  --published-report "$ARCHIVE/report/report.json" \
  --export-inventory "$ARCHIVE/retrieved/export-inventory.json" \
  --output /private/analysis/affinity-diagnosis.json
```

The output directory must exist and the output file must be new. Compare its
aggregates and hashes with the checked-in JSON. Keep the original archive
read-only. A mismatch stops the command before it publishes a diagnosis.

## One candidate rule

The opt-in `cache_saturation` policy uses the existing binary history score
and the existing relative imbalance of **two** in-flight router requests. It
escapes to the other replica only when both conditions hold:

1. The preferred replica has at least `saturation_active` active router
   requests **before** this request is counted.
2. Its active count is at least two greater than the peer's.

At exactly the threshold and exactly a two-request difference, it escapes.
Below either bound it retains the estimated-affinity choice. Ties use the
existing round-robin ring to choose a preferred replica before applying the
same gate. There is no retry or failover on an unavailable upstream. Ledger
rows record the turn, both busy counts, estimated affinity, preferred and
selected replicas, threshold, rule version, both Boolean predicates and
reason; the offline report recomputes each decision. The router's existing
`finally` path releases work on stream completion, errors, timeouts and
disconnects. The four older policies retain their decisions and remain
available; the v2 workload/report path remains readable.

Threshold candidates **8, 10, 12 active requests on the preferred replica**
span the common archived escape counts. This is a bounded heuristic derived
from the archive, not physical saturation calibration. The new run tests all
three at 5 requests/second on both patterns, using calibration seed 211. It
chooses the threshold with the highest *minimum* all-offered SLO goodput
across patterns, then highest sum, then the larger threshold. Every
calibration condition must have valid client/router accounting, counters and
unlimited generator/admission behavior. The chosen value and rule are saved
before pilot or evaluation. Performance of the chosen threshold is
GPU-uncalibrated until this future run occurs; profiling or engine phase
instrumentation would be a separate diagnostic, not silently mixed into
normal trial outcomes.

## Frozen traffic and finite matrix

Both patterns use the same 48 distinct approximately 4K-token document
prefixes, 128 output tokens, context 8,192, SLOs, model, two BF16 TP1 vLLM
0.26 replicas and prefix-cache reset protocol. For each pattern/rate/seed,
every policy receives the **identical** scheduled arrivals, prompts, token
lengths and output work. Plan and certificate hashes freeze these inputs.

- `control.v1` is exactly the v2 trace recipe: 60% cycle across 48 documents,
  40% eight-document hotspot shifting A→B→A, approximately even scheduled
  arrivals. It explicitly retains the earlier shift.
- `burst-hot-shift.v1` keeps the same documents and A→B→A shift, with 20%
  cycle and 80% two-document hotspot. Eight arrivals are scheduled in each
  200 ms burst, followed by a gap determined by the offered rate. The mean
  offered rate is still 4, 5 or 6 requests/second. This tests whether an
  absolute active-work gate matters during concentrated burst load.

Calibration is 3 thresholds × 2 patterns × 120 seconds = **12 traffic
minutes**. A cache-only pilot runs both patterns at 4 and 5 rps (8 minutes).
Both must pass at 4. If either fails at 5, evaluation rates freeze to 4 and
5. Otherwise both patterns also run at 6 (another 4 minutes), and rates
freeze to 5 and 6. This gives **8–12 pilot traffic minutes**. Pilot seed 223
never appears in evaluation. The run saves threshold, rate and schedule
freezes before evaluation.

Evaluation has 4 policies (`round_robin`, `cache_only`, `cache_plus_load`,
`cache_saturation`) × 2 patterns × 2 frozen rates × 4 balanced blocks × 120
seconds = **64 windows, 128 traffic minutes**. Seeds 301, 307, 311 and 313
are distinct from calibration and pilot. Each policy occupies every position
once per block rotation; pattern and rate order rotate. Total scheduled
traffic is **148–152 minutes**, followed by drain and preceded by startup,
reset and model transfer. The study stops on invalid metrics/accounting or a
generator/admission bound. It never calls invalid evidence an engine or policy
loss, and never treats an unbracketed rate as maximum capacity.

## Local preparation and checks

Use the exact Qwen/Qwen3-8B revision
`b968826d9c46dd6066d109eabc6255188de91218`, pinned tokenizer files and
`tokenizers==0.22.1`. A local full preparation verified all 36 plan and
certificate pairs; sampled 5-rps control and burst plans each contain 600
offers, 4,119–4,142 rendered input tokens and 128 output tokens. Input plus
output stays under 8,192. On a separate private preparation machine, an
operator can obtain pinned model and tokenizer snapshots with:

```sh
python -c 'from huggingface_hub import snapshot_download; snapshot_download(repo_id="Qwen/Qwen3-8B", revision="b968826d9c46dd6066d109eabc6255188de91218", local_dir="/private/model/qwen3-8b", token=False)'
mkdir -p /private/preflight/qwen3-tokenizer
cp /private/model/qwen3-8b/tokenizer.json /private/model/qwen3-8b/tokenizer_config.json /private/preflight/qwen3-tokenizer/
PYTHONPATH=src python -m inferdrome.vllm_router_followup prepare \
  --tokenizer-root /private/preflight/qwen3-tokenizer \
  --output-root /private/preflight/affinity-prepared
PYTHONPATH=src python -m pytest \
  tests/integration/test_vllm_router_followup.py \
  tests/integration/test_vllm_router_hotspot.py \
  tests/integration/test_vllm_router_capacity.py
```

The `prepare` and `report` commands need no GPU. Run the repository gate with
`scripts/engineering_gate.sh` before using reviewed source. The runner
copies the frozen input files into its raw output and rejects source image,
model snapshot, CLI flag, GPU, port or budget mismatches before engine start.

## Future operator handoff (requires new spending approval)

Use a quoted host with **two idle A100 40 GB GPUs**, one replica per GPU,
matching topology and usable memory. The previously successful A100-SXM4
host reported 138,144 KV-cache tokens per replica; the runner reads each new
engine's actual startup value and checks the 48-prefix working-set heuristic
before calibration. It uses image
`vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77`
and vLLM `0.26.0+cu129`. Verify the local driver/CUDA pairing and both GPU
allocations, `nvidia-smi`, `/usr/local/bin/vllm --version`, and
`vllm serve --help=all` for the exact APC, no-log and context flags. Download
the model with visible byte progress and verify its snapshot hash before
running. Healthy transfer or model loading has no arbitrary short timeout;
the approved overall budget remains the limit. The `run` command checks
engine model endpoints, quiescent metrics and exact prefix-cache reset on
both replicas before every window. A short APC-on/off diagnostic is available
through the separate v2 procedure if engine behavior needs a smoke test; its
profiled or APC-off results do not enter these performance comparisons.

At the previous observed two-GPU rate of **$1.482/hour**, 148–152 traffic
minutes alone would cost about **$3.66–$3.75**. A provisional 3–3.5-hour
whole session, including setup, reset, drain, export and teardown, would
cost **$4.45–$5.19** in compute at that *historical* rate, plus quoted
storage/transfer and a teardown reserve. The actual new offer, spending cap,
available credit and reserve must be recorded and approved before renting.

On an already approved, running host with reviewed source and exact pinned
model files, use its **new** instance ID, observed billing start and quote:

```sh
PYTHONPATH=/private/source/src /private/coord-venv/bin/python \
  -m inferdrome.vllm_router_followup run \
  --prepared-dir /private/inputs/affinity-prepared \
  --model-dir /private/model/qwen3-8b \
  --vllm-executable /usr/local/bin/vllm \
  --image-reference 'vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77' \
  --source-commit '<reviewed-40-hex-commit>' \
  --instance-id '<new-observed-numeric-id>' \
  --billing-start-utc '<new-observed-UTC-timestamp>' \
  --hourly-rate-usd '<new-quoted-total-hourly-rate>' \
  --cap-usd '<new-approved-whole-run-cap>' \
  --reserve-usd '<new-teardown-reserve>' \
  --output-root /private/outputs/affinity-run
```

After local process cleanup, export the private raw directory. Compare every
file's size and SHA-256 against an export inventory at the receiving end
before tearing down the **exact new instance ID**. Keep raw ledgers, client
rows, logs, request bodies and any connection details private. Offline:

```sh
PYTHONPATH=src python -m inferdrome.vllm_router_followup report \
  --raw-root /private/retrieved/affinity-run \
  --output-root /private/retrieved/affinity-report
```

The report revalidates plan/certificate hashes, every offered client row,
client summaries, router accounting and rule decisions, reset results,
counter deltas, calibration/rate freezes and all 64 scheduled cells. It shows
per-block goodput, SLO passing/failing rates, completed/rejected/error work,
first-content and completion latency, client and router queueing, replica
distribution, route reasons and cache counters. It compares blocks with
`cache_only`; round robin and the existing hybrid remain serious baselines.
More hits alone do not constitute success. A valid regression or negative
result is reported. Four blocks on one host do not establish significance or
production-wide behavior. Follow the v2 runbook's exact-ID destroy and
zero-instance/zero-storage verification; no command here rents or destroys a
provider resource.
