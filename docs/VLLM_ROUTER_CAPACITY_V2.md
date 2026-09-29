# Two-replica vLLM capacity and cache-pressure study (v2)

This procedure extends the completed 28 September 2026 two-A100 baseline. That
run completed 21,300 requests, including 19,200 evaluation requests, and all
four policies delivered 4 SLO-good requests/second on a two-document,
286-input-token trace. Its 1/2/4 requests/second calibration never found a
failure. The raw evidence remains private in `.inferdrome-gpu`; the public
[baseline summary](VLLM_ROUTER_GPU_PR3.md) describes the original method.

The v2 plan, certificate, session, and report have separate schemas. The old
v1 workload and report stay readable. This is one host's synthetic capacity
experiment; it does not establish production capacity or a cache-aware win.

## Frozen workload and capacity candidate

The default starts with 48 distinct document prefixes nearest 4,096 tokens
under the pinned Qwen3 tokenizer, 128 generated tokens, and `--max-model-len
8192`. Every document begins with a distinct signature before its repeated
record text. Sixty percent of offers cycle through all documents; forty
percent visit a small hot group. The hot group shifts A→B→A over three equal
epochs. The question follows the repeated document prefix and varies by offer.
All randomness comes from frozen seeds and scheduled arrival times; each policy
receives the same trace for a given rate and block.

The successful baseline host reported 138,144 GPU KV-cache tokens per replica.
Forty-eight 4,096-token prefixes are approximately 196,608 tokens: a useful
initial candidate above one observed cache and below two caches. This is not a
residency estimate for another host. At startup, v2 reads **each engine's own
KV-cache size from its log** and records the document-prefix working set. It
stops before pilot traffic unless that set exceeds one replica's reported
capacity and remains below 85% of the pair's reported capacity. The remainder
is headroom for active requests. If this heuristic fails, prepare a new plan
with a different document count before starting evaluation. Cache counter
deltas and latency still decide what was observed; no cache-residency claim is
derived from the heuristic.

`prepare` verifies the exact Qwen3-8B revision tokenizer files and
`tokenizers==0.22.1`. It selects each document's repeat count, checks that
document prefixes diverge in their first sixteen encoded tokens, and certifies
**every rendered prompt length** in each plan. Every planned input plus 128
output tokens must fit the 8,192-token context. A runtime usage mismatch
invalidates the condition. Plans contain digests and per-offer token lengths,
not prompts or generations.

## Pilot and evaluation decision

The default pilot ladder is 2, 4, 6, 8, 12, 16 offered requests/second, each
over a 120-second scheduled window, on `least_busy`. Rates are explicit and
strictly increasing; the operator may change them before preparation. A rate
passes only if all of these hold:

- At least 95% of **all offers** complete and meet both first-content ≤500 ms
  and completion ≤5 s from scheduled arrival.
- At least 95% complete; at most 1% are rejected.
- Completed-request p95 first-content ≤500 ms **and** p95 completion ≤5 s.
- The client and router account for the offered population, engine metrics are
  present, and the generator and router admission bounds do not limit the
  measurement.

The first valid SLO failure brackets the boundary only when two lower ladder
rates have passed. The coordinator then freezes those two passing rates and
the first failing rate. If every ladder rate passes, it reports
`UNBRACKETED` and does not call the highest tested rate a maximum. A failure
at the first or second rate reports `INSUFFICIENT_BELOW`. Client scheduling
lag, client queueing, missing dispatches, router capacity rejection or router
queue timeout report `HARNESS_LIMIT`; they cannot establish an engine
boundary. Token/accounting failure or missing metrics stops as invalid
evidence. These stop rules depend on measurement validity and SLOs, not on a
preferred policy winning.

Once bracketed, a saved schedule crosses three frozen rates with the four
existing policies in four balanced policy-order blocks. The new evaluation
seeds are 101, 103, 107, and 109; pilot seed 71 never enters evaluation.
Rate order rotates across blocks. The runner never selects a trace from the
evaluation result. It saves the rate choice and complete schedule before the
first evaluation condition.

The router limits are frozen in the prepared manifest: default active 128,
queue 256. Client concurrency defaults to 256. Every scheduled offer is
counted, including timeouts, failures, and rejections. SLO goodput divides by
the **scheduled 120-second window**; draining cannot inflate throughput.
Reports show client scheduling lag, client queue delay, router queue delay,
replica distribution, route reasons, per-replica prefix-cache counter deltas,
and document revisit gaps relative to the affinity history's 60-second TTL.
The history is binary routing memory; an overload escape can make both
replicas an affinity tie. It is not KV residency.

## GPU-free preparation

Use an isolated Python 3.12 environment with the project dependencies and the
pinned `tokenizers==0.22.1`. Download `tokenizer.json` and
`tokenizer_config.json` from Qwen/Qwen3-8B revision
`b968826d9c46dd6066d109eabc6255188de91218`; the repository checks their
SHA-256 values. From the reviewed source checkout:

```sh
PYTHONPATH=src python -m inferdrome.vllm_router_capacity prepare \
  --tokenizer-root /private/preflight/qwen3-tokenizer \
  --rates-rps 2,4,6,8,12,16 --duration-s 120 \
  --document-count 48 --target-prefix-tokens 4096 \
  --context-length 8192 --client-concurrency 256 \
  --router-max-active 128 --router-max-queue 256 \
  --output-root /private/preflight/capacity-prepared
```

The output directory must be new. Retain its manifest, plans and certificates
with the exact source commit; the runner copies all inputs into its raw output.
The 48-document default is a candidate. A small GPU-free preparation with the
pinned tokenizer produced document prefixes of 4,084–4,107 tokens. GPU-free
tests and this preparation do **not** prove vLLM startup or capacity on a
future host.

## Provider boundary and smoke sequence

A future rental needs its own explicit whole-run dollar cap. Before creating
an instance, record the exact offer, two matched A100 40 GB GPUs, disk, image
digest, driver/CUDA compatibility, hourly rate, storage and transfer terms,
available credit, billing start, and teardown reserve. The image remains
`vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77`.
The previous instance was destroyed; its old SSH endpoint is not a run target.

On an already-rented host, first check two idle GPUs with `nvidia-smi` and a
CUDA allocation smoke on both devices. Verify `/usr/local/bin/vllm --version`
reports 0.26.0 and `vllm serve --help=all` contains
`--enable-prefix-caching`, `--no-enable-prefix-caching`,
`--no-enable-log-requests`, and `--max-model-len`. These flags are also listed
in the [pinned vLLM 0.26.0 CLI reference](https://docs.vllm.ai/en/v0.26.0/cli/serve/).
The image's vLLM launcher uses `/venv/main/bin/python`; check the actual
paths and install any coordinator dependencies from an operator-reviewed
wheel bundle in a separate environment. Download one pinned Qwen model
snapshot to a shared private directory with visible byte progress. The
coordinator hashes the full snapshot before starting engines. Healthy
download or model startup progress alone is not a short timeout reason; the
approved overall spend envelope still applies.

The optional small APC diagnostic starts **one** GPU-0 engine with APC on,
checks an exact successful reset, sends the same 4K-prefix request twice and
records first-content times and cache counters. It stops that process group
and verifies ports close before starting a fresh APC-off engine for the same
two requests. It uses the pinned CLI's `--no-enable-prefix-caching`; simply
omitting the APC-on flag would not disable APC. The run is descriptive,
order-sensitive, and valid even when no speedup appears. Missing counters are
reported as incomplete metrics. Use the same observed billing start and
overall cap as the main run:

```sh
PYTHONPATH=/private/source/src /private/coord-venv/bin/python \
  -m inferdrome.vllm_router_capacity diagnostic \
  --prepared-dir /private/inputs/capacity-prepared \
  --model-dir /private/model/qwen3-8b \
  --vllm-executable /usr/local/bin/vllm \
  --image-reference 'vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77' \
  --instance-id '<observed-numeric-instance-id>' \
  --billing-start-utc '<observed-UTC-timestamp>' \
  --hourly-rate-usd '<quoted-hourly-rate>' \
  --cap-usd '<approved-total-cap>' --reserve-usd '<teardown-reserve>' \
  --output-root /private/outputs/capacity-apc-diagnostic
```

Then run the measured pilot and, only if validly bracketed, the frozen
evaluation. It starts two independent BF16, TP=1, APC-on vLLM 0.26.0
replicas at context 8,192, one per GPU. The engines stay loaded between
conditions. Before each condition, the coordinator sends a disjoint direct
warmup, checks quiescence, requires exact `{"success":true}` from each
`/reset_prefix_cache`, and starts a fresh router and ledger:

```sh
PYTHONPATH=/private/source/src /private/coord-venv/bin/python \
  -m inferdrome.vllm_router_capacity run \
  --prepared-dir /private/inputs/capacity-prepared \
  --model-dir /private/model/qwen3-8b \
  --vllm-executable /usr/local/bin/vllm \
  --image-reference 'vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77' \
  --source-commit '<reviewed-40-hex-commit>' \
  --instance-id '<observed-numeric-instance-id>' \
  --billing-start-utc '<observed-UTC-timestamp>' \
  --hourly-rate-usd '<quoted-hourly-rate>' \
  --cap-usd '<approved-total-cap>' --reserve-usd '<teardown-reserve>' \
  --output-root /private/outputs/capacity-run
```

With six full pilot windows and 48 two-minute evaluation conditions, the
scheduled traffic ceiling is 108 minutes. Add model transfer, two diagnostic
starts, pair startup, resets, drain/export and teardown to estimate the whole
session. For example, a three-hour reservation at quoted total hourly rate
`R` has base compute estimate `3×R`; add quoted bandwidth/storage and a
separate teardown reserve, then compare the total with available credit and
the explicit cap. The actual live offer and bill can differ. The runner checks
projected time plus 300 seconds per condition and the reserve before each
condition; it cannot enforce provider transfer charges. An unbracketed pilot
ends sooner, while repeated invalid setup can consume the envelope.

## Export, report and exact-ID teardown

Export the private diagnostic and run directories after local process cleanup.
Verify file counts and SHA-256 hashes at the receiving destination before
destroying the exact provider instance. The report verifies client, router,
input-plan, certificate, reset and metric bindings and inventories every raw
file. Keep request rows, logs, and any credentials private:

```sh
PYTHONPATH=src python -m inferdrome.vllm_router_capacity report \
  --raw-root /private/retrieved/capacity-run \
  --output-root /private/retrieved/capacity-report
```

The JSON, Markdown and SVG report show offered rate against SLO goodput,
first-content and completion tails, attainment/errors, client and router
queueing, per-replica load and cache counters, per-block results, and
descriptive differences from `least_busy`. It marks ties, regressions,
unbracketed capacity, incomplete evidence and missing metrics. Four blocks
are repetitions; correlated request rows are not thousands of independent
trials. No statistical significance or production-wide benefit is claimed.

Finally, from the operator's authenticated Vast environment, run `vastai
destroy instance <exact-instance-id>` and then `vastai show instances`.
Retain the destroy response and verify that the exact ID is absent. A fatal
setup or study error requires the same exact-ID teardown path. No code in
this PR rents or destroys a provider resource.
