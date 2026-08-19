# Managed real-GPU proof

Status: **One genuine A10 bundle captured; full comparison capture pending**

Implementation date: **2026-08-06**

This runbook is the Inferdrome-owned portion of the PR 7 real-GPU gate. It
prepares one pinned Linux environment, launches vLLM under Inferdrome's own
supervisor, measures the public flagship workload, seals the evidence, verifies
it offline, and demonstrates corrupted-artifact and synthetic-evidence
rejection without editing the original bundle.

Inferdrome does not emit `PASS`, `FAIL`, or `NOT_PROVEN`. Those are
acceptance-verifier outcomes owned by ExitSpec. Because ExitSpec is a separate
repository and consumer boundary, its three-outcome demonstration and receipt
remain pending after this runbook succeeds.

## Frozen demonstration

The checked-in demonstration uses:

- `Qwen/Qwen2.5-0.5B-Instruct` at model and tokenizer revision
  `7ae557604adf67be50417f59c2c2f167def9a775`;
- vLLM exactly `0.26.0` from the architecture-specific wheel and SHA-256 in
  `spikes/vllm-0.26.0/producer-pin.json`;
- 100 measured requests, 10 warmups, and concurrency 4;
- 32 requested output tokens, temperature 0, and seed 42;
- one non-sensitive, checked-in 100-prompt workload; and
- `http://127.0.0.1:18080` with no remote attachment boundary.

The host pin is
[`examples/real-gpu/host-pin.json`](../examples/real-gpu/host-pin.json), and the
resolved experiment source is
[`examples/real-gpu-smoke.yaml`](../examples/real-gpu-smoke.yaml).

## Compatible host

Use a clean checkout at the commit that will be recorded in release sign-off.
The preparation script requires:

- Linux `x86_64` or `aarch64`;
- Python 3.12 plus its development headers (`Python.h`), Bash, Git, curl,
  Ninja, and GNU `sha256sum`;
- an NVIDIA GPU supported by the pinned vLLM wheel;
- `nvidia-smi` and a driver compatible with the wheel's CUDA runtime; and
- unset `CUDA_VISIBLE_DEVICES` and `NVIDIA_VISIBLE_DEVICES`, so recorded device
  indices retain unambiguous physical identities; and
- enough disk space for the roughly 300 MiB vLLM wheel, its dependencies, the
  model snapshot, and generated evidence.

The script uses no `sudo`, refuses a dirty checkout, refuses to reuse an
existing destination, verifies the exact vLLM wheel hash before installation,
downloads the model at the exact revision, rejects snapshot symlinks, checks
CUDA through the installed Torch runtime, and records the checkout commit and
resolved Python package inventory. It installs Inferdrome with its dashboard
extra so the documented post-run inspection command does not depend on
transitive vLLM packages. Immediately before measurement, the demo
regenerates that inventory byte-for-byte and reruns `pip check`; package drift
or a newly inconsistent environment fails before proof output is reserved.

From the repository root:

```bash
./scripts/prepare_real_gpu_host.sh
```

To place the prepared environment elsewhere, pass a new, nonexistent
destination:

```bash
./scripts/prepare_real_gpu_host.sh /data/inferdrome-gpu-state
```

The default destination is `.inferdrome-gpu/`, which is ignored by Git. Keep
`host-preparation.json` and `python-packages.txt` with the demonstration
receipt; they are supporting reproduction records, not substitutes for the
sealed bundle's own provenance.

## Bounded SSH capture and retrieval

When the compatible GPU is an operator-provided SSH VM, the workstation can
run the full single-proof and controlled-comparison pack without manually
copying commands or evidence paths. The generic SSH path does not create,
resize, stop, or terminate infrastructure. When the optional Lambda flags are
present, the controller adds one deliberately narrow provider integration: it
may list the selected instance and terminate it, but it still cannot launch or
modify an instance.

Before starting the paid host, make sure the intended Inferdrome commit is
committed and the checkout is clean. After the provider reports an SSH
destination, inspect the exact workflow without making a network connection:

```bash
.venv/bin/python scripts/capture_real_gpu_over_ssh.py \
  <user@gpu-host> \
  --identity-file <private-key-path> \
  --expected-commit "$(git rev-parse HEAD)" \
  --dry-run
```

Then start the capture by removing `--dry-run`:

```bash
.venv/bin/python scripts/capture_real_gpu_over_ssh.py \
  <user@gpu-host> \
  --identity-file <private-key-path> \
  --expected-commit "$(git rev-parse HEAD)"
```

### Lambda cost guard

For Lambda, put the API key only in `LAMBDA_CLOUD_API_KEY`. Do not pass it as a
command-line argument, commit it, or paste it into a receipt. The guard projects
only instance ID, endpoint, status, and hourly rate from the API response; it
discards fields such as Jupyter tokens.

Record the provider's UTC launch time before leaving the console. The guarded
controller requires that billing origin, the exact displayed hourly rate, and
an operator spend budget:

```bash
read -r -s -p "Lambda API key: " LAMBDA_CLOUD_API_KEY
echo
export LAMBDA_CLOUD_API_KEY

.venv/bin/python scripts/capture_real_gpu_over_ssh.py \
  ubuntu@<public-ip> \
  --identity-file <private-key-path> \
  --expected-commit "$(git rev-parse HEAD)" \
  --lambda-hourly-rate-usd 1.29 \
  --max-cost-usd 2.58 \
  --lambda-billing-started-at 2026-08-19T00:00:00Z
```

At `$1.29/hour`, a `$2.58` budget yields a 7,200-second cost-limit boundary. The
watchdog deliberately requests termination 60 seconds earlier, at 7,140
seconds, to leave bounded room for network and provider latency. Replace both
values with the console values for the selected instance. The controller
rejects a missing or different API-reported hourly rate, rejects a materially
future billing start, and requires the selected instance endpoint to match the
SSH destination. Pass `--lambda-instance-id <32-hex-id>` as an additional
explicit identity when available.

The controller fails before SSH if the guard cannot be armed. It reports the
detached watchdog as armed only after the child publishes a validated readiness
record; startup failure removes false armed state. The watchdog holds the
buffered termination deadline and calls Lambda's termination API even if the
capture controller fails. On macOS it runs beneath `caffeinate -i` so idle sleep
does not silently suspend the timer. The controller also calls the same
termination API immediately in `finally` after success, failure, or
interruption, polls until the instance is absent or terminal, and only then
disarms the fallback. The API key remains in process environment, never in the
watchdog argument vector or its operational receipts.

This is a strong local circuit breaker, not an exact billing cap or availability
guarantee. Provider billing granularity, API latency, network loss, or laptop
failure can still cross the nominal budget. The Mac must remain powered,
connected to the internet, and able to reach Lambda. Keep a separate console
deadline, keep the provider console available, and confirm that no instance
remains after each run. Lambda documents that billing begins after launch health
checks and ends when the instance is terminated; guest `shutdown` or `poweroff`
is not a substitute for provider termination. See Lambda's
[billing overview](https://docs.lambda.ai/public-cloud/billing/) and
[Cloud API](https://docs.lambda.ai/api/cloud).

The controller:

1. refuses a dirty checkout or an unexpected commit;
2. creates and locally verifies a Git bundle for exact `HEAD`;
3. checks Linux, Python 3.12 headers, Ninja, NVIDIA visibility, and required
   host tools;
4. uploads the bundle and clones it into a private temporary directory;
5. gives the host workload a default 9,900-second outer timeout;
6. prepares the pinned environment and runs both proof modes;
7. retrieves `capture.tar.gz` plus its host SHA-256;
8. rejects unsafe archive members before extraction; and
9. independently verifies the single bundle, all four comparison bundles,
   both Trial Sets, the frozen plan, and the comparison result locally; and
10. when Lambda protection is configured, confirms provider termination on
    every controller exit path.

The first SSH connection uses `StrictHostKeyChecking=accept-new` with a
capture-specific `known_hosts` file. Its digest is retained in the local
retrieval receipt. This is SSH trust-on-first-use, not cloud hardware
attestation. If the provider exposes the expected host key through a separate
authenticated channel, pass the lowercase hex digest of the exact
capture-specific `known_hosts` bytes as `--host-key-sha256` to replace that
trust-on-first-use check with an explicit pin.

Successful captures are retained beneath the ignored
`gpu-proof-retrieved/` directory. A failed host run is explicitly labeled
`INCOMPLETE_NOT_EVIDENCE`; when its archive is available, the controller keeps
the diagnostic logs without upgrading them into proof.

The host timeout bounds the proof process, not provider billing. Without the
optional Lambda guard, immediately terminate the instance in the cloud console
after success or failure and at the predeclared deadline. With the guard, still
verify the final provider state in the console after the controller confirms
termination.

For a manually launched Lambda VM, the operator checklist is deliberately
short:

1. resolve billing and tax-address requirements;
2. confirm the displayed rate, GPU type, Ubuntu image, and SSH key;
3. record the UTC launch time, spend budget, and a separate console hard-stop
   deadline before clicking **Launch**;
4. copy the provider's exact SSH destination into the controller command;
5. terminate the instance after success, failure, or deadline—whichever comes
   first; and
6. confirm the console reports no running instances.

## Recovered A10 single-run receipt

The 2026-08-18 Lambda A10 attempt completed one genuine measurement and sealed
its single-run bundle before a post-run tamper-rejection assertion failed. The
controlled comparison never started. The outer capture is therefore correctly
labeled `INCOMPLETE_NOT_EVIDENCE`; it is not a complete proof pack and cannot
support a comparison claim. Its already sealed single bundle is independently
valid and `CUSTOMER_ELIGIBLE`, so it can be materialized as a strictly scoped
dashboard receipt without changing or resealing the bundle.

Recorded anchors:

- repository commit: `209f0bb9f629a3f9577bb702f4d4e867e9a136fb`;
- source archive: `sha256:43f46a88965e2cc47ace6eb0ce0f7b4d013a613344f48f419ca460f41739f510`;
- run ID: `run-1d008f70f18574f215bd6fc213e50348`; and
- bundle digest: `sha256:4ead525398ea65cce04123d824abeac622e8e35eef9c6d1c2e3becb2af54d0ed`.

Materialize it outside a source checkout so filesystem tooling does not relax
the sealed bundle's read-only modes:

```bash
PYTHONPATH=src .venv/bin/python scripts/materialize_real_gpu_receipt.py \
  gpu-proof-retrieved/20260818T202058Z-209f0bb9f629-9d5d748a-FAILED/capture.tar.gz \
  ~/.inferdrome/real-gpu/recovered-a10-20260818 \
  --expected-archive-sha256 sha256:43f46a88965e2cc47ace6eb0ce0f7b4d013a613344f48f419ca460f41739f510 \
  --expected-bundle-digest sha256:4ead525398ea65cce04123d824abeac622e8e35eef9c6d1c2e3becb2af54d0ed \
  --expected-commit 209f0bb9f629a3f9577bb702f4d4e867e9a136fb \
  --expected-run-id run-1d008f70f18574f215bd6fc213e50348
```

The command verifies the archive hash, explicit failure receipt, commit, sealed
bundle digest, and customer eligibility before atomic publication. Its printed
`runs_root` can be passed directly to `inferdrome dashboard --runs-root`. The
generated recovery receipt says `SINGLE_BUNDLE_ONLY`; it never upgrades the
failed outer capture or fabricates the missing four-run comparison.

## Exact managed server launch

The demo never asks the operator to start an unobserved server. During
`PREFLIGHT`, Inferdrome builds and launches this fixed no-shell argument vector
for the default one-GPU run:

```text
<prepared-venv>/bin/vllm
serve
<exact-local-model-snapshot>
--host 127.0.0.1
--port 18080
--served-model-name Qwen/Qwen2.5-0.5B-Instruct
--tokenizer <exact-local-model-snapshot>
--tokenizer-mode auto
--dtype auto
--seed 42
--load-format safetensors
--generation-config vllm
--model-impl vllm
--max-model-len 1024
--gpu-memory-utilization 0.80
--tensor-parallel-size 1
--device-ids 0
--no-enable-log-requests
--disable-uvicorn-access-log
--uvicorn-log-level warning
```

The v0.1 managed profile intentionally supports exactly one physical GPU. The
final vector is stored in `native/invocation.json` and regenerated
byte-for-byte by offline verification. Multi-GPU serving requires a later
capability capture and is not silently inferred from this profile.

Before measurement, the managed path requires all of the following:

- the same Python environment contains Inferdrome and vLLM `0.26.0`, whose
  installation metadata names the exact architecture wheel and pinned archive
  SHA-256, and whose retained source-wheel bytes still match that pin;
- the installed vLLM distribution and executable are hashed from stable regular
  files;
- model and tokenizer snapshots are hashed from stable regular files, excluding
  only `.cache` directories under the named hash policy;
- the selected `nvidia-smi` inventory is strict and complete;
- Torch reports an available CUDA runtime and the selected device;
- loopback model-list preflight returns the exact configured model ID; and
- the selected GPU has no unmanaged compute process at readiness capture, and
  live `nvidia-smi` compute-process rows come only from the managed server's
  isolated process group.

The model, tokenizer, installed vLLM distribution, and resolved `nvidia-smi`
executable are hashed again after the benchmark. Any drift fails the run.
Managed server diagnostics remain in the private run workspace, while the
launch and allowlisted proof needed for offline verification are sealed in the
producer invocation artifact.

The managed server, version probe, and benchmark also share one process
environment policy. Inferdrome removes every inherited `VLLM_*` override and
forces `VLLM_NO_USAGE_STATS=1`, `DO_NOT_TRACK=1`,
`HF_HUB_DISABLE_TELEMETRY=1`, `HF_HUB_OFFLINE=1`, and
`TRANSFORMERS_OFFLINE=1`. The policy identifier and exact overrides are sealed
with the server proof so ambient vLLM configuration cannot silently alter the
demonstration or enable producer telemetry.

## Run the proof and rejection demonstrations

With the default prepared state:

```bash
.inferdrome-gpu/venv/bin/python scripts/run_real_gpu_demo.py
```

For an alternate state directory or physical GPU index:

```bash
/data/inferdrome-gpu-state/venv/bin/python \
  scripts/run_real_gpu_demo.py \
  --state-root /data/inferdrome-gpu-state \
  --gpu-index 1
```

The script creates a fresh directory under `gpu-proof-output/` and performs:

1. strict example validation;
2. one managed local-vLLM run and immutable bundle seal;
3. offline verification anchored to the printed bundle digest with
   `--require-customer-eligible`;
4. independent reduction and deterministic summary;
5. corruption of only a copied native artifact followed by required hash
   rejection; and
6. a separate synthetic run followed by required customer-flow rejection.

The final `demo-receipt.json` records the checkout commit, real bundle path and
digest, eligibility, host-preparation receipt digest, and both rejection
results. `acceptance_boundary` remains `PENDING_EXTERNAL_EXITSPEC` by design.

The native vLLM result includes generated text and is classified
`RESPONSE_CONTENT`. Review sharing and retention accordingly.

## Run the controlled-comparison proof

The same prepared checkout can execute a genuine two-arm comparison with two
preallocated repetitions per arm:

```bash
.inferdrome-gpu/venv/bin/python \
  scripts/run_real_gpu_demo.py --comparison
```

Alternate state, output, GPU-index, and startup-timeout options are identical
to the single-run proof. Comparison mode creates a fresh
`real-gpu-comparison-*` directory beneath `gpu-proof-output/` and performs:

1. strict validation of the pinned concurrency-2 and concurrency-4 sources;
2. immutable plan creation before any run is reserved;
3. retention and independent verification of the exact plan digest;
4. four managed local-vLLM runs in the frozen permuted-pair schedule;
5. customer-eligibility verification of every sealed bundle;
6. independent verification of both planned Trial Sets and the final result;
7. a second executor invocation that must reuse all four verified runs without
   launching another workload; and
8. publication of `comparison-demo-receipt.json`.

The receipt anchors the checkout and host-preparation identities, plan and
result digests, ordered run and bundle identities, Trial Set digests, result
status and bounded outcome, and successful resume reverification. A fresh proof
is expected to execute all four planned runs; the second invocation must report
all four as reused and none as executed.

`COMPARABLE` is emitted only if every frozen and observed v1 control is
satisfied. A fully verified `INCOMPARABLE` result is still a successful proof
of the evidence pipeline and contains no outcome estimate. Neither status is a
winner label, causal claim, significance claim, or ExitSpec acceptance result.

Inspect the finished proof through the locked read-only dashboard by replacing
`<proof-directory>` with the directory containing the printed receipt:

```bash
<prepared-venv>/bin/inferdrome dashboard \
  --runs-root <proof-directory>/runs \
  --trial-sets-root <proof-directory>/trial-sets \
  --comparison-plans-root <proof-directory>/comparison-plans \
  --comparison-results-root <proof-directory>/comparison-results
```

The server remains bound to `127.0.0.1:8787`. For a headless remote GPU host,
forward that loopback port from the reviewing workstation instead of exposing
the dashboard publicly:

```bash
ssh -L 8787:127.0.0.1:8787 <gpu-host>
```

Comparison mode launches one managed server per planned run. Budget host time
for four bounded model loads plus four benchmark executions. Do not interrupt a
reserved run unless you intend the frozen plan to remain blocked.

## Review and promotion gate

Do not hand-edit generated evidence. Retain the printed bundle digest outside
the bundle, inspect the saved command diagnostics, and independently run:

```bash
<prepared-venv>/bin/inferdrome bundle verify \
  <bundle-path> \
  --expected-digest <sha256:bundle-digest> \
  --require-customer-eligible
```

A real bundle may be promoted as a committed example only after review records
the exact checkout commit, engineering-gate result, GPU bundle digest, host
preparation receipt digest, and later the separate ExitSpec receipt digest.
Until that capture exists, PR 7 and the v0.1 release gate remain open.

The comparison receipt and its four retained bundle digests are additional
post-v0.1 proof; they do not replace the single-bundle promotion review or the
separately owned ExitSpec demonstrations.
