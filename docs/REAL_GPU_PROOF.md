# Managed real-GPU proof

Status: **Harness implemented; compatible-host capture pending**

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
- Python 3.12, Bash, Git, curl, and GNU `sha256sum`;
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
resolved Python package inventory.

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
