# Inferdrome CLI and orchestration

Status: **Implemented for v0.1 hardening**

Implementation date: **2026-08-06**

The command line is a thin user-facing layer over Inferdrome's frozen library
boundaries. It never implements a second resolver, reducer, bundle reader, or
integrity policy.

## Installation and entry points

```bash
uv sync
uv run inferdrome --version
```

The same CLI is available without a console-script installation:

```bash
PYTHONPATH=src python -m inferdrome --help
```

The implementation uses the standard-library argument parser so the executable
surface does not expand the v0.1 dependency or supply-chain budget.

## Validate and resolve

Validation reads and validates the source YAML plus exact workload bytes. It
does not reserve a run directory, contact an endpoint, or execute a producer.

```bash
inferdrome validate experiment.yaml
```

Resolution prints one canonical JSON document containing the resolved public
experiment, frozen request plan, run ID, and domain-separated digests:

```bash
inferdrome resolve experiment.yaml
inferdrome resolve experiment.yaml \
  --run-id run-0123456789abcdef0123456789abcdef
```

Strict mode is the default. `--no-strict` may calculate an omitted workload
digest or retain unknown attached-target revisions, but it never changes an
unknown fact into verified provenance. Unsupported producer and adapter
versions fail in both modes.

## Execute and seal

The synthetic smoke path requires no endpoint or GPU:

```bash
inferdrome run examples/fake-smoke.yaml --runs-root runs
```

It performs this complete transaction:

```text
resolve source and workload
→ reserve immutable run inputs
→ persist lifecycle states
→ execute the fake producer
→ reduce canonical records
→ stage all normative artifacts
→ hash exact bytes
→ seal the bundle read-only
→ verify it offline
→ mark the workspace COMPLETE
```

The result is always `SYNTHETIC_ONLY`.

The attached-vLLM path additionally requires the local tokenizer directory
used by the pinned producer invocation:

```bash
inferdrome run experiment.yaml \
  --runs-root runs \
  --tokenizer-path /absolute/path/to/pinned-tokenizer
```

It performs redirect-free endpoint preflight, verifies `vllm --version`, builds
one exact no-shell argument vector, supervises the process within the resolved
runtime limit, preserves native output, normalizes only the pinned `0.26.0`
shape, and seals the same public evidence format as the fake path.

Ordinary attached-endpoint evidence remains `INELIGIBLE`. Endpoint model
identity is server-reported; target revisions are configured; GPU, CUDA,
driver, producer-distribution, and launch provenance remain unknown.

The opt-in managed path is narrower and Linux/NVIDIA-only:

```bash
inferdrome run examples/real-gpu-smoke.yaml \
  --runs-root runs \
  --tokenizer-path /absolute/path/to/exact-model-snapshot \
  --managed-local-vllm \
  --managed-model-path /absolute/path/to/exact-model-snapshot \
  --managed-gpu-index 0 \
  --managed-startup-timeout-seconds 900
```

It launches the exact pinned server itself, requires live GPU-process binding,
hashes the model, tokenizer, installed producer, `nvidia-smi`, and generated
launch vector, and checks immutable inputs again after measurement. Only this
managed proof can produce `CUSTOMER_ELIGIBLE` vLLM evidence. It does not turn
eligibility into an acceptance verdict.

The executable host preparation and end-to-end demonstration are in
[REAL_GPU_PROOF.md](REAL_GPU_PROOF.md).

## Inspect, verify, reduce, and summarize

```bash
inferdrome inspect run-0123456789abcdef0123456789abcdef --runs-root runs
inferdrome bundle verify \
  runs/run-0123456789abcdef0123456789abcdef/bundle
inferdrome reduce runs/run-0123456789abcdef0123456789abcdef/bundle
inferdrome summarize runs/run-0123456789abcdef0123456789abcdef/bundle
```

`inspect` validates the complete workspace event chain and verifies any
published bundle. `bundle verify` checks the closed artifact inventory, exact
hashes, schemas, cross-artifact identities, native normalization, and reducer
agreement without mutation or network access.

When a bundle digest has been retained outside the bundle, anchor verification
to it explicitly:

```bash
inferdrome bundle verify \
  runs/run-0123456789abcdef0123456789abcdef/bundle \
  --expected-digest "$BUNDLE_DIGEST"
```

A customer-evidence entry point must also reject synthetic and ordinary
attached bundles:

```bash
inferdrome bundle verify \
  runs/run-0123456789abcdef0123456789abcdef/bundle \
  --expected-digest "$BUNDLE_DIGEST" \
  --require-customer-eligible
```

`reduce` independently reconstructs the frozen measurements from execution
evidence, canonical request records, and metric definitions. It emits the
canonical `inferdrome.measurements.v1` JSON and refuses a stored/recalculated
disagreement. `summarize` emits a stable human-oriented JSON projection without
changing the bundle.

## Failure and cancellation behavior

Expected validation, resolution, execution, normalization, reduction, bundle,
and verification failures print one bounded error to stderr and exit nonzero
without a traceback. Argument errors use the parser's exit code `2`.

`SIGINT` and `SIGTERM` request cooperative cancellation. The subprocess
supervisor then performs bounded terminate/kill handling. A safely writable
reserved workspace records `INTERRUPTED`; other execution failures record
`FAILED`. A run reaches `COMPLETE` only through successful sealing and offline
verification.

The attached-vLLM workspace preserves invocation evidence, producer-version
output, stdout, stderr, exit status, and any producer-written native result in
its private `native-capture/` directory using exclusive no-follow writes. A
failed run does not relabel those diagnostics as a complete evidence bundle.
