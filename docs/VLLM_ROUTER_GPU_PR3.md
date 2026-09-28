# Two-replica vLLM router GPU study (PR3)

This is one operator-supplied, same-host experiment. The new coordinator does
not rent a GPU, download a model, install vLLM, contact Vast's API, or destroy
an instance. It starts and owns two local vLLM process groups and an in-process
router on loopback. A provider operator owns the exact instance ID, spending
limit, and destruction/absence readback. No GPU result is included in this PR.

## Frozen method

Use the reviewed PR3 commit, `Qwen/Qwen3-8B` revision
`b968826d9c46dd6066d109eabc6255188de91218`, and stock Vast
`vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77`
(vLLM 0.26.0/CUDA 12.9). The container must expose exactly two identical
`NVIDIA A100-PCIE-40GB` or `NVIDIA A100-SXM4-40GB` GPUs. One Qwen snapshot is
downloaded to one shared directory and checked against the repository's full
snapshot digest before either engine starts. Both engines run BF16, TP=1,
2,048 context, prefix caching enabled, one GPU each, with loopback ports 8001
and 8002. The router binds port 8090 only during a condition.

The exact pinned tokenizer files have SHA-256 values
`aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4`
(`tokenizer.json`) and
`d5d09f07b48c3086c508b30d1c9114bd1189145b74e982a265350c923acd8101`
(`tokenizer_config.json`). With `tokenizers==0.22.1`, **every planned prompt is
286 rendered tokens**. PR2's 277-token exploratory value came from a different
tokenizer version and cannot certify a measured run. The generated certificates
freeze 286 input and 128 output tokens; the client checks live usage for every
successful stream.

Calibration uses seed 17 and 300, 600, then 1,200 offers over separate five
minute windows. Select the highest rate with at least 95% completed, at most 1%
rejected, and completed-request p95 terminal latency at most five seconds. If
none qualifies, stop. Evaluation uses four complete balanced blocks with seeds
29, 31, 37, 41. Each block gives all policies exactly the same offers and
scheduled arrivals. Policy orders are:

```text
round_robin, least_busy, cache_plus_load, cache_only
least_busy, cache_only, round_robin, cache_plus_load
cache_only, cache_plus_load, least_busy, round_robin
cache_plus_load, round_robin, cache_only, least_busy
```

Both engines remain loaded between conditions. Before every condition, the
coordinator sends a disjoint synthetic warmup directly to each engine, checks
zero running and waiting requests from `/metrics` after a bounded five-second
settling window, and POSTs
`/reset_prefix_cache` to each engine. The pinned vLLM 0.26.0 development route
must return **HTTP 200 and exact `{"success":true}`** on both replicas; HTTP
200 alone is insufficient. The route is enabled only for loopback servers by
`VLLM_SERVER_DEV_MODE=1`. If either engine cannot prove a successful reset,
the study stops. Each condition gets a fresh router object, empty affinity
history, and fresh ledger. Prefix-cache counters are retained as per-condition
deltas, not reset totals. [Pinned route source](https://github.com/vllm-project/vllm/blob/v0.26.0/vllm/entrypoints/serve/dev/cache/api_router.py)

## GPU-free preparation

Use an isolated Python 3.12 environment with Inferdrome's normal dependencies
and `tokenizers==0.22.1`. Obtain the two tokenizer files from the exact model
revision and check their hashes. A local Mac spike tokenizer has different
hashes and is unsuitable.

```sh
hf download Qwen/Qwen3-8B tokenizer.json tokenizer_config.json \
  --revision b968826d9c46dd6066d109eabc6255188de91218 \
  --local-dir /private/preflight/qwen3-tokenizer
PYTHONPATH=src python -m inferdrome.vllm_router_gpu prepare \
  --tokenizer-root /private/preflight/qwen3-tokenizer \
  --output-root /private/preflight/router-plans
```

`prepare` creates fifteen plans and fifteen certificates by exclusive file
creation. Keep these 30 JSON files, their hashes, the source commit and the
model snapshot identity. Regenerate them if the source changes. The Python
client and renderer do not store prompts or generations in artifacts.

## Operator launch boundary

Before renting, record the current exact offer, GPU type/count, 60 GB or more
container disk, driver CUDA compatibility, image selection, hourly rate,
bandwidth/storage terms, available credit, an explicit maximum dollar spend,
and a reserve for export and teardown. A roughly two to three hour duration is
an estimate, not a kill timer for healthy downloads. The coordinator checks
the declared rate and cap before startup and each full condition, but this is
only an estimate: the provider's bill may include additional charges. Recheck
the live offer and credit immediately before creation. The provider operator
must retain the exact instance ID and terminate that ID after the run or on a
fatal setup error, then read back its absence. No command in this PR can make
that provider cleanup automatic.

From the operator's authenticated Vast CLI, the explicit teardown is
`vastai destroy instance <exact-instance-id>` followed by `vastai show
instances` and a check that the same ID is absent. Keep the destroy response
and readback with the private run record.
[Vast CLI reference](https://github.com/vast-ai/vast-cli)

On the already-rented, image-verified stock container, place the reviewed
source and 30 plan/certificate JSON files in private paths. Check the stock
image's `/venv/main/bin/python` has the Inferdrome dependencies (`aiohttp`,
`pydantic`, `rfc8785`, `jsonschema`, `PyYAML`) before the run; any missing
dependency is a setup error to resolve before measuring. The pinned image's
`/usr/local/bin/vllm` entry point uses `/venv/main/bin/python`; verify both
paths on the actual host before launching engines.
Download the one pinned Qwen snapshot to a shared directory with visible byte
progress. For example, using the image's Hugging Face client:

```sh
/venv/main/bin/python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="Qwen/Qwen3-8B",
    revision="b968826d9c46dd6066d109eabc6255188de91218",
    local_dir="/private/model/qwen3-8b",
)
PY
```

The coordinator hashes the complete snapshot before starting either engine;
an incomplete or changed download fails with a specific preflight error. The
source package must be importable in the container, for example with
`PYTHONPATH=/private/source/src`. Invoke the following only after the operator
has recorded the actual quote, billing start, dollar cap, exact instance ID
and source commit; substitute those observed values literally:

```sh
PYTHONPATH=/private/source/src /venv/main/bin/python -m inferdrome.vllm_router_gpu run \
  --plans-dir /private/inputs/router-plans \
  --model-dir /private/model/qwen3-8b \
  --vllm-executable /usr/local/bin/vllm \
  --image-reference 'vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77' \
  --source-commit '<reviewed-40-hex-commit>' \
  --instance-id '<observed-numeric-instance-id>' \
  --billing-start-utc '<observed-UTC-timestamp>' \
  --hourly-rate-usd '<quoted-hourly-rate>' \
  --cap-usd '<approved-total-cap>' --reserve-usd '<declared-overhead-reserve>' \
  --output-root /private/outputs/router-gpu-run
```

The output directory must not already exist. Preflight checks all plan and
certificate bindings, the full Qwen snapshot, vLLM version, two distinct idle
A100s, closed ports and the declared cost envelope. It prints engine readiness
progress while the processes load. For each condition it writes reset,
per-offer client, router ledger, metrics and progress files. On a fatal error,
the session records the cause and stops owned process groups; any router ledger
rows already written remain in the private output. A completed client trial is
recorded before the post-trial metrics read; if that read fails, the report
retains the trial and its explicit metrics error. The selected calibration rate
and evaluation schedule are saved before evaluation begins. A missing
`session.json` with a `preflight-error.json` means no engine was started. Any
`CLEANUP_UNCONFIRMED` status requires immediate operator inspection and
provider termination.

After engine cleanup, export the private raw directory and hash-check it at the
receiving destination. Render the report **offline**:

```sh
PYTHONPATH=src python -m inferdrome.vllm_router_gpu report \
  --raw-root /private/retrieved/router-gpu-run \
  --output-root /private/retrieved/router-gpu-report
```

The report verifies client and router hashes, retains an inventory of all raw
files, and writes JSON, Markdown and an SVG chart. SLO goodput uses all offered
requests over each fixed 300-second window; latency/stall p95 values describe
completed requests only. Failed conditions remain visible and make the report
incomplete. Four blocks support a descriptive matched comparison on this host,
not statistical significance or production recommendations. Two repeatedly
shared ~286-token prompts and 128-token decoding may yield little or no gain;
`cache_plus_load` matching `least_busy` is a valid outcome.
