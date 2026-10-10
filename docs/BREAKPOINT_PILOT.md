# Breakpoint: prepare the first affordable GPU pilot

**Spending is undecided.** These preparations do not rent GPUs. The pilot is
an exploratory measurement of one timing recipe, with a **3½-hour ceiling for
one two-GPU session**. It is a separate, smaller experiment from the
[full study design](BREAKPOINT_STUDY.md); that design and earlier release
contracts remain unchanged.

## What the pilot can answer

At 6 offered requests/s, does the ordering of `cache_only` and `least_busy`
reverse when we apply one predetermined arrival-timing transformation?

| Item | Frozen pilot |
| --- | --- |
| Replicas | Two independent Qwen3-8B BF16 vLLM processes, TP=1, one per matched A100 40 GB. |
| Workload | `control.v1`; 48 document prefixes near 4,096 tokens, 128 output tokens, hotspot A→B→A. |
| Replication | Eight workload seeds, four policy/timing cells each: **32 trials**. |
| Trial | 720 offers over 120 seconds; **23,040 total offers**. |
| Timing recipe | Groups of 8, 25% retained spacing, maximum advance 1 second; each original workload epoch preserved. |
| Order | Seeded Williams order, seed 104729. |
| Seeds | 1009, 1013, 1019, 1021, 1031, 1033, 1039, 1049. |
| Practical margin | 0.1 SLO-good request/s; first content ≤500 ms and completion ≤5 s from scheduled arrival. |
| Client/router | Client concurrency 256; router active 128, queue 256; client lag/queue p95 each ≤10 ms. |

Every block measures both policies with the original and transformed timing.
The same prompts, output budgets, offers, SLOs and 120-second denominator remain
in both schedules. Eight blocks require 8/8 margin exceedances in each direction
under the existing [paired screening rule](VLLM_PAIRED_COMPARISON.md). Individual
requests are not independent replications. A negative or ineligible result is
a valid outcome to retain; no replacement blocks or adaptive recipe changes
are allowed inside this pilot.

This pilot contains **no search, reduction, fresh-seed confirmation or new
calibration**. It cannot establish a universal routing winner or customer
acceptance. A favorable screen would motivate a separately planned held-out
study. The historical 7.2% SLO-goodput result remains part of the
[earlier capacity study](VLLM_ROUTER_RESULTS.md#separate-capacity-sweep).

## Time and cost envelope

| Allowance | Elapsed time |
| --- | ---: |
| 32 × 120-second traffic windows | 64 min |
| 32 × 60-second drain limits | 32 min |
| 32 × 60-second warm/reset limits | 32 min |
| Setup, export and teardown reserve | 60 min |
| Planned total | **188 min (3 h 08 min)** |
| Session ceiling | **210 min (3 h 30 min)** |

These are planning allowances, not observed A100 timings. The ceiling starts at
the recorded provider billing origin, including all billable setup and model
transfer after that point. Wall time since instance creation can differ.
Two concurrent GPUs for 3½ hours is at most **7 GPU-hours** if teardown succeeds
within the intended envelope. The quote must state the price for the **pair**,
storage, ingress/egress and a total approved cap. No price or dollar cap is
assumed here. Slow setup consumes the same envelope; it does not extend it.

The collector bounds network stages and refuses another trial when its whole
allowance no longer fits. Its collection cutoff is 3 h 10 min at the maximum
envelope, leaving 10 min for local cleanup/export before provider cleanup begins
at 3 h 20 min. Router setup, verification and final quiescence checks consume
additional session headroom. Startup must finish before roughly 62 billable
minutes to retain all 128 minutes of trial reservations. Synchronous disk I/O
can still block local progress; the [external cleanup guard](BREAKPOINT_PILOT_GUARD.md)
must run on the operator's machine independently of the GPU host. Local process
cleanup is not a provider teardown receipt. API failures can defeat a deadline;
an unverified teardown requires immediate operator action and cannot establish
a hard invoice cap.

## Prepare before rental

Use a clean, committed checkout of the pilot implementation. Record its exact
40-character commit in the private plan; do not reuse the full study's older
source pin. The source pin is supplied after committing to avoid a plan that
must contain the hash of its own commit.

```bash
export PYTHONPATH=src
python -m inferdrome breakpoint pilot plan \
  --config examples/breakpoint-qwen3-pilot.json \
  --source-revision '<reviewed-full-commit>' \
  --output /private/pilot/plan.json
python -m inferdrome breakpoint pilot prepare \
  --plan /private/pilot/plan.json \
  --tokenizer-dir /private/preflight/qwen3-tokenizer \
  --output-dir /private/pilot/prepared
python -m inferdrome breakpoint pilot verify \
  --prepared-dir /private/pilot/prepared \
  --tokenizer-dir /private/preflight/qwen3-tokenizer
python -m inferdrome breakpoint pilot rehearse \
  --output-dir /private/pilot/rehearsal
```

Preparation uses the real pinned Qwen tokenizer locally and contacts no serving
endpoint. Rehearsal uses two local fake SSE engines and the real timed client,
router and ledger across all 32 trial slots. Its deliberately short fixture
windows and generated responses are **SYNTHETIC_ONLY**. A successful rehearsal
does not establish GPU performance, cache behavior, host eligibility or the
feasibility of the 10 ms client limits on that host.

Stage the reviewed Git source, `uv.lock`, frozen runtime requirements, compatible
Linux x86-64 CPython 3.12 wheels and tokenizer files on the operator machine.
Use an isolated collector environment so its dependency installation does not
alter vLLM's environment. The source needs Git metadata: a plain source tarball
will fail the clean-checkout check. Prepare a Git bundle from the reviewed
commit, then clone it on the future host. With the staged files transferred to
`/private/staged`:

```bash
git clone /private/staged/source.bundle /private/source
git -C /private/source checkout --detach '<reviewed-full-commit>'
python3.12 -m venv /private/collector
/private/collector/bin/python -m pip install \
  --no-index --find-links /private/staged/linux-cp312-wheels \
  --require-hashes -r /private/staged/runtime-requirements.txt
```

Verify the staging manifest's retained file hashes after transfer. Python 3.12
with venv/pip support and the compatible Linux platform remain host checks;
the offline wheel resolution check on the operator machine does not execute
those Linux binaries. Use the prebuilt serving image:

```text
vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77
```

No custom Docker image is required. The public model snapshot is still needed:
Qwen/Qwen3-8B revision `b968826d9c46dd6066d109eabc6255188de91218`, 15 files,
16,397,461,266 bytes. The tokenizer-only preparation is not a model download.
Verify the entire snapshot against `qwen3_model_manifest()` before launch.
Check image availability/cache and transfer expectations before choosing an
offer; fetching this model or image while rented uses the session allowance.

## Checks tied to the actual offer and host

Before approval, review the exact quote, pair price, storage and transfer
allowances, total cap, two-GPU availability, and intended image. After rental,
record the exact instance ID and provider billing start immediately and arm the
external guard before starting collector setup. It must remain running through
export and independently verified destruction. Do not attach separately billed
volumes or other resources outside this single-instance guard's scope.

On the selected host, verify Python/runtime identity, clean source revision,
full model hashes, disk and ports, two matched idle GPUs with distinct UUIDs,
engine readiness and KV-cache capacity. Each trial needs bounded disjoint
warmup, zero queued/running requests, exact successful prefix-cache resets on
both replicas, and another quiescence check. The collector retains reset
responses and failed attempts, then stops on an unverified prerequisite.
Those host facts cannot be established by a CPU rehearsal.

Once the separately approved instance is guarded and all staged assets are on
the host, use the collector's isolated Python environment and the serving
image's existing vLLM executable:

```bash
PYTHONPATH=/private/source/src /private/collector/bin/python \
  -m inferdrome breakpoint pilot run \
  --prepared-dir /private/pilot/prepared \
  --tokenizer-dir /private/preflight/qwen3-tokenizer \
  --model-dir /private/models/qwen3-8b \
  --source-dir /private/source \
  --vllm-executable /absolute/image/path/to/vllm \
  --approval /private/pilot/approved-instance.json \
  --guard-receipt /private/pilot/guardian-ready.json \
  --output-dir /private/pilot/collection
```

Resolve the actual image executable and paths during setup; these are example
paths. Refresh the copied guard receipt throughout model/runtime preflight:
the check occurs after full model hashing, and a single early copy can become
older than 60 seconds. On the external machine, with the actual SSH target and
port configured, a separate supervised terminal can keep replacing it atomically:

```bash
while test -f /private/pilot/guardian/ready.json; do
  scp -o BatchMode=yes -o ConnectTimeout=10 -P "$PILOT_SSH_PORT" \
    /private/pilot/guardian/ready.json \
    "$PILOT_SSH_TARGET:/private/pilot/guardian-ready.json.next" &&
  ssh -o BatchMode=yes -o ConnectTimeout=10 -p "$PILOT_SSH_PORT" \
    "$PILOT_SSH_TARGET" \
    'mv /private/pilot/guardian-ready.json.next /private/pilot/guardian-ready.json'
  sleep 15
done
```

Start this after the external guard has written its first readiness receipt.
Copy failure leaves the collector unable to accept a fresh receipt. The receipt
binds the intended deadline but cannot prove future guardian uptime; supervise
the guardian independently of this transfer terminal and the GPU host.

## Keep and retrieve every attempt

The immutable preparation and separate collection directory remain private.
The collector writes partial observations as they occur and a terminal
`session-status.json` only after local cleanup. Preserve failed sessions as well
as completed ones. Export only after the collector exits:

```bash
python -m inferdrome breakpoint pilot export \
  --prepared-dir /private/pilot/prepared \
  --collection-dir /private/pilot/collection \
  --output /private/pilot/attempt-001.zip
```

Save the returned archive SHA-256 outside the archive. Copy the archive to a
private operator directory and independently verify the **retrieved copy**:

```bash
python -m inferdrome breakpoint pilot verify-export \
  --archive /private/retrieved/attempt-001.zip \
  --sha256 'sha256:<retained-archive-digest>'
```

The archive verifier checks exact membership, bounded regular files, byte sizes
and hashes without extracting or executing anything. This is transfer integrity,
not authentication or evidence eligibility. Replay the prepared protocol and
paired comparison separately before making a measurement claim. Keep the guard
receipts on the operator machine as a separate private record. Export trouble
must not postpone the provider cleanup deadline. Public documentation should
contain reviewed aggregate results and source revisions, never these raw
archives, provider identities or credentials.
