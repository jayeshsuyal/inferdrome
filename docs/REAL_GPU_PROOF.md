# Managed real-GPU proof

Status: **Historical Qwen2.5 proof and Qwen3-8B A10 capability archive verified;
both raw archives EXTERNAL_ONLY and ExitSpec acceptance pending**

Implementation date: **2026-08-06**

Producer handoff closure date: **2026-08-20**

Qwen3-8B A10 capability closure date: **2026-08-21**

This runbook is the Inferdrome-owned portion of the real-GPU evidence gate. It
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
- Python 3.12 plus its development headers (`Python.h`), Bash, Git, curl, and
  GNU `sha256sum`;
- an NVIDIA GPU supported by the pinned vLLM wheel;
- `nvidia-smi` and a driver compatible with the wheel's CUDA runtime; and
- unset `CUDA_VISIBLE_DEVICES` and `NVIDIA_VISIBLE_DEVICES`, so recorded device
  indices retain unambiguous physical identities; and
- enough disk space for the roughly 300 MiB vLLM wheel, its dependencies, the
  model snapshot, and generated evidence.

For Lambda Cloud, select **Lambda Stack 24.04**. Lambda documents Python 3.12
for that image family, while Lambda Stack 22.04 provides Python 3.10 and cannot
satisfy this repository's host contract. Do not run a full distribution upgrade
as part of capture preparation; use the image's shipped toolchain and let the
preflight fail closed if any required development header is absent. See
[Lambda's base-image matrix](https://docs.lambda.ai/public-cloud/on-demand/#base-images).

The script uses no `sudo`, refuses a dirty checkout, refuses to reuse an
existing destination, verifies the exact vLLM wheel hash before installation,
downloads the model at the exact revision, rejects snapshot symlinks, checks
CUDA through the installed Torch runtime, and records the checkout commit and
resolved Python package inventory. It requires the installed vLLM environment
to provide its own Ninja executable rather than depending on an unrecorded
system copy. It installs Inferdrome with its dashboard extra so the documented
post-run inspection command does not depend on transitive vLLM packages.
Immediately before measurement, the demo regenerates that inventory
byte-for-byte and reruns `pip check`; package drift or a newly inconsistent
environment fails before proof output is reserved.

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

The historical commands in this document continue to run the preserved
Qwen2.5 proof pack. The separate, explicit Qwen3/A10 capability-spike mode is
documented in
[`QWEN3_CAMPAIGN_PROFILE.md`](QWEN3_CAMPAIGN_PROFILE.md#bounded-a10-remote-capture-controller);
it cannot silently replace this legacy path.

Before starting the paid host, make sure the intended Inferdrome commit is
committed and the checkout is clean. After the provider reports an SSH
destination, inspect the exact workflow without making a network connection:

```bash
.venv/bin/python scripts/capture_real_gpu_over_ssh.py \
  <user@gpu-host> \
  --identity-file <private-key-path> \
  --host-key-file <pinned-known-hosts-path> \
  --host-key-sha256 <sha256-of-exact-known-hosts-bytes> \
  --expected-commit "$(git rev-parse HEAD)" \
  --dry-run
```

Then start the capture by removing `--dry-run`:

```bash
.venv/bin/python scripts/capture_real_gpu_over_ssh.py \
  <user@gpu-host> \
  --identity-file <private-key-path> \
  --host-key-file <pinned-known-hosts-path> \
  --host-key-sha256 <sha256-of-exact-known-hosts-bytes> \
  --expected-commit "$(git rev-parse HEAD)"
```

### Lambda cost guard

For Lambda, put the API key only in `LAMBDA_CLOUD_API_KEY`. Do not pass it as a
command-line argument, commit it, or paste it into a receipt. The guard projects
only instance ID, endpoint, status, and hourly rate from the API response; it
discards fields such as Jupyter tokens.

For an implemented frozen Qwen3 PCIe tier, check volatile provider capacity
before launch with the separate GET-only watcher:

```bash
PYTHONPATH=src .venv/bin/python scripts/watch_lambda_gpu_capacity.py \
  --gpu-tier h100-80gb-pcie \
  --watch \
  --poll-seconds 60 \
  --max-wait-seconds 3600
```

The watcher cannot launch an instance. A zero exit status means only that the
selected tier's exact provider description, one-GPU shape, frozen rate,
capacity region, and zero-active-instance preflight were observed. For H100
PCIe those values are `1x H100 (80 GB PCIe)` and `$3.29/hour`. The returned
instance-type name and region still require a separate paid-launch
confirmation. The A100-specific command remains available for compatibility.

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
  --host-key-file <pinned-known-hosts-path> \
  --host-key-sha256 <sha256-of-exact-known-hosts-bytes> \
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
capture controller fails. On macOS a separately launched, fixed
`/usr/bin/caffeinate -w <watchdog-pid>` helper inhibits idle sleep without
receiving the provider key. Every guarded mode translates SIGINT/SIGTERM into
the same `finally` cleanup; signals are deferred while watchdog ownership is
being established or provider finalization is already in progress. The
controller calls the termination API immediately, polls until the instance is
absent or terminal, and only then disarms the fallback. If confirmation fails,
it retains an immutable `UNRESOLVED` state record, does not disarm the
watchdog, and records whether that watchdog is still live; failure to retain
that state is itself surfaced as a hard error. The API key is limited to the
provider controller and direct Python
watchdog child. It is absent from Git/SSH/SCP/GPU/producer and sleep-inhibitor
children, argument vectors, operational receipts, and child logs.

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
3. checks Linux, Python 3.12 headers, NVIDIA visibility, and required host
   tools;
4. uploads the bundle and clones it into a private temporary directory;
5. gives the host workload a default 9,900-second outer timeout;
6. prepares the pinned environment and runs both proof modes;
7. retrieves `capture.tar.gz` plus its host SHA-256;
8. rejects unsafe archive members before extraction; and
9. independently extracts and verifies the archive in an isolated system
   temporary directory, including the single bundle, all four comparison
   bundles, both Trial Sets, the frozen plan, and the comparison result; and
10. when Lambda protection is configured, attempts and polls provider
    termination after normal completion, handled failures, SIGINT, and SIGTERM,
    retaining explicit unresolved state with the fallback watchdog armed if
    confirmation fails. Abrupt controller death relies on that detached
    watchdog.

Before any SSH or SCP process starts, the controller materializes exact
`known_hosts` bytes from `--host-key-file` (recommended) or `ssh-keyscan` and
requires their raw-byte SHA-256 to match the independently retained lowercase
`--host-key-sha256` pin. Every handshake, including the first, uses
`StrictHostKeyChecking=yes`, the capture-specific `UserKnownHostsFile`, and
`IdentityAgent=none`; the user's SSH agent, configuration, proxy, forwarding,
and environment are unavailable. This authenticates the pinned SSH host key,
not cloud hardware.

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

## Verified 2026-08-20 A10 comparison capture

The 2026-08-20 Lambda Stack 24.04 run completed the entire Inferdrome-owned
proof pack on one NVIDIA A10. The host prepared the pinned environment, the
single-run demonstration passed, and the predeclared four-run schedule
completed in `BASELINE`, `CANDIDATE`, `CANDIDATE`, `BASELINE` order. Offline
archive verification reports `valid: true`; all four comparison bundles are
`CUSTOMER_ELIGIBLE` with `COMPLETE` observed environments, the result is
`COMPARABLE`, and every control is satisfied.

Recorded anchors:

- repository commit: `c08b46d9fbd87477f45d130aa3c63615937c4dc3`;
- source archive: `sha256:f2408fd0649a7c79f5962872003781ebb9c878b802db27d633cf246f13b6f424`;
- capture manifest: `sha256:1d4ea1e251c5a84a104333ab8579d580838701a70cc38b64b68c88f66266e0cb`;
- single run: `run-533c9f5f783958fb6077069a6c577144`, bundle
  `sha256:bae216f2165eb06ae2e0f14d3cd852f8e0ebb381bf1f68c71072769b3c0c1675`;
- comparison plan: `comparison-plan-5f4abd9b24ab717e910b166c5b793038`,
  digest `sha256:25dd7f87d02572b6c3f992014944241595e8240d7301a58cba55da11eae1c60e`;
  and
- comparison result: `comparison-result-5f4abd9b24ab717e910b166c5b793038`,
  digest `sha256:6943eb577b368f036b4536626076d7b7a4f23caf8df7f839e5a1248dbaae774a`.

The primary point estimate is a candidate-minus-baseline increase of
`17.258428 requests/s` in attempted measured-request throughput. The two
baseline run values are `17.532296` and `17.553803 requests/s`; the two
candidate values are `34.794865` and `34.808091 requests/s`. This is a
`POINT_ESTIMATE_ONLY` result with two repetitions per arm, not an uncertainty
claim or an ExitSpec acceptance outcome.

The first convenience extraction beneath the source workspace was correctly
withheld after workspace tooling relaxed its sealed directory modes. The
SHA-anchored archive itself subsequently passed complete verification in an
isolated system temporary directory. The controller now always performs its
authoritative archive verification in that isolated location before publishing
a retrieval receipt. Reproduce the offline verdict with:

```bash
PYTHONPATH=src .venv/bin/python scripts/real_gpu_capture.py verify-archive \
  <capture.tar.gz> \
  --expected-sha256 sha256:f2408fd0649a7c79f5962872003781ebb9c878b802db27d633cf246f13b6f424 \
  --expected-commit c08b46d9fbd87477f45d130aa3c63615937c4dc3
```

The capture remains `PENDING_EXTERNAL_EXITSPEC`. It proves the Inferdrome
measurement, integrity, provenance, comparison, and rejection machinery; it
does not manufacture `PASS`, `FAIL`, or `NOT_PROVEN` for the separately owned
acceptance boundary.

## Publication review and ExitSpec handoff

The exact archive was reviewed without rewriting, redacting, resealing, or
regenerating any captured byte. The committed producer-side records are:

- local proof schema:
  [`profiles/v1/local-gpu-proof.schema.json`](../profiles/v1/local-gpu-proof.schema.json),
  canonical-document digest
  `sha256:cf83bbdea2bba4c30b8f0e2c5f34f34a4077501207881fdbdab021571d665547`;
- composite managed-vLLM profile:
  [`profiles/v1/managed-vllm-0.26-evidence-profile.json`](../profiles/v1/managed-vllm-0.26-evidence-profile.json),
  canonical-document digest
  `sha256:9d03b5d0822ed829ddbfa4c87c75530885b9ad51ee2c0cb7c5e31a075996fe34`;
- publication review:
  [`evidence/gpu/2026-08-20-a10/publication-review.json`](../evidence/gpu/2026-08-20-a10/publication-review.json),
  canonical-document digest
  `sha256:7f1b3be53695e9e3a2009eb28ce008bb2486ae882e52364e26bece770a6d33ff`;
  and
- handoff manifest:
  [`evidence/gpu/2026-08-20-a10/handoff-manifest.json`](../evidence/gpu/2026-08-20-a10/handoff-manifest.json),
  canonical-document digest
  `sha256:bc90ac7d0044b32556ce8e78181635f2a2d218e3de7a793062e5dc2b3d6cd4bd`.

The review scanned all 310 regular files and 3,137,959 expanded bytes under
stricter 16 MiB per-file and 256 MiB total review limits after the ordinary
archive-safety and isolated integrity checks passed. It found no secret-shaped
values, email addresses, or public network addresses. It did retain and
disclose prompts, generated responses, stdout/stderr, package inventory,
absolute paths, one private host-network address repeated across server logs,
GPU UUIDs, and process identifiers.

The result remains `EXTERNAL_ONLY`, not `APPROVED_PUBLIC`. At review time the
repository had no selected license; the owner has since selected Apache-2.0
for Inferdrome source and package metadata. That later repository choice does
not alter the reviewed archive, provide its missing owner-approved license
records for the model, workload, vLLM, and generated output, or approve public
delivery. Therefore `capture.tar.gz` remains ignored and was neither committed
nor uploaded. The proposed future release-asset URL and exact required checksum
are recorded in the handoff manifest; vendoring the same reviewed bytes in
ExitSpec remains an alternative owner decision.

The engineering gate always validates both tracked records, their duplicate-free
closed JSON shapes, fixed canonical-document hashes, cross-document identities,
profile/schema pins, `EXTERNAL_ONLY` owner-required state, retrospective
chronology, and null Inferdrome/ExitSpec authority without requiring the raw
archive:

```bash
PYTHONPATH=src .venv/bin/python \
  scripts/review_gpu_evidence_publication.py --check-records
```

When the exact external archive is available locally, re-run the complete
archive review and independently recalculate the 100/100 native TTFT population
and nearest-rank p95 of `14,797,213 ns` with:

```bash
PYTHONPATH=src .venv/bin/python scripts/review_gpu_evidence_publication.py --check
```

That deterministic recheck freezes the repository-license fact recorded by the
historical review; the current Apache-2.0 file does not recategorize or rewrite
the sealed review.

The handoff explicitly records a null producer-side ExitSpec contract digest
and `RETROSPECTIVE` chronology. A future contract can be frozen before
evaluation, but this capture does not prove that contract preceded measurement.
Its preserved `comparison_context.status: COMPARABLE` is a historical, neutral
measurement-compatibility label, not an evidence-authoritative comparison or
an ExitSpec outcome. Under the current authority gate, the same null contract
identity would produce `INCOMPARABLE` and withhold all controlled outcomes; the
immutable handoff record remains unchanged.
The capture producer commit, later profile/publication commits, and eventual
merge commit are separate identities; the eventual owner merge must preserve
`c08b46d9fbd87477f45d130aa3c63615937c4dc3` as an ancestor rather than
squashing it away.

## Qwen3-8B A10 reviewed capability spike

The separate frozen Qwen3-8B profile completed one bounded genuine A10 run on
2026-08-21. It produced 96/96 successful measured requests, zero failures, 96
native TTFT samples, nearest-rank p50/p95/p99 TTFT values of `127,123,958`,
`242,426,174`, and `244,030,050 ns`, and `28.870215` output tokens/s.

The canonical anchors are:

- producer commit
  `058482df47377aaae6303015746f9a8e05d7e0f7`;
- archive digest
  `sha256:27cdcc0192c5d6f05b5350e380b53caa158c249de28ed6880fe3d7971032172f`;
- bundle digest
  `sha256:48514166f6c284613052fedb0264ed83e2212156233d9f6103cb8946c0211ad6`;
- [publication review](../evidence/gpu/2026-08-21-qwen3-8b-a10/publication-review.json);
- [privacy-safe operational summary](../evidence/gpu/2026-08-21-qwen3-8b-a10/operational-summary.json); and
- [handoff manifest](../evidence/gpu/2026-08-21-qwen3-8b-a10/handoff-manifest.json).

The controller retrieved the checksum-verified archive, confirmed the Lambda
instance absent, and only then published `VALID_AFTER_PROVIDER_TERMINATION`.
That termination record is operational evidence, not provider attestation. The
capture itself records one locally verified CUDA device reporting `NVIDIA A10`;
`hardware_attestation` remains false.

Recalculate the exact local archive and receipts without rewriting them:

```bash
PYTHONPATH=src .venv/bin/python \
  scripts/review_qwen3_gpu_evidence_publication.py --check
```

The historical A10 and A100 review renderers likewise freeze their review-time
repository-license fact, so a current checkout cannot silently authorize or
rewrite an `EXTERNAL_ONLY` record.

Open that same verified bundle in the loopback-only dashboard:

```bash
PYTHONPATH=src .venv/bin/python \
  scripts/run_qwen3_evidence_dashboard.py --open
```

The archive remains `EXTERNAL_ONLY` because owner publication approval and
archive-bound repository, model, workload, vLLM, and generated-output license
decisions are unresolved. Apache-2.0 now covers Inferdrome repository-authored
source and package metadata, but it neither rewrites that sealed archive nor
licenses its external materials or generated output. Public CI validates the
committed record shapes and cross-digests; it does not claim to possess or
reverify ignored local bytes. This spike closes A10 runtime compatibility for
the exact profile only. It is not a cross-GPU result or an Inferdrome
acceptance verdict.

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

The managed server, version probe, benchmark, and `nvidia-smi` probes share
validated absolute executable identities and one minimal process environment.
Inferdrome does not inherit compiler, loader, cloud, SSH-agent, or arbitrary
shell variables. It supplies fixed standard CUDA/tool/library locations on the
Linux proof host, a run-private `HOME`, and forces
`VLLM_NO_USAGE_STATS=1`, `DO_NOT_TRACK=1`,
`HF_HUB_DISABLE_TELEMETRY=1`, `HF_HUB_OFFLINE=1`, and
`TRANSFORMERS_OFFLINE=1`. The verified vLLM directory and fixed system/CUDA
directories form `PATH`; the operator's ambient `PATH`, `LD_LIBRARY_PATH`,
`CC`, and `CXX` are ignored. The policy identifier and exact overrides are
sealed with the server proof. Credential-shaped producer output fails closed
before sealing rather than silently changing the upstream bytes.

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

`COMPARABLE` is emitted only if all run IDs are distinct, every member is
`CUSTOMER_ELIGIBLE`, both planned arms and all bundles share one non-null
ExitSpec contract digest, and every frozen and observed v1 control is satisfied.
A fully verified `INCOMPARABLE` result is still a successful proof of the
evidence pipeline and contains no outcome estimate. Neither status is a winner
label, causal claim, significance claim, or ExitSpec acceptance result.

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
