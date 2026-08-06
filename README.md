# Inferdrome

Inferdrome is a reproducible evidence pipeline for LLM-serving experiments.

It resolves experiment inputs, invokes a pinned benchmark tool, preserves the
tool's native output, normalizes request-level observations, deterministically
computes measurements, verifies bundle integrity, and hands portable evidence
to an independent acceptance verifier such as ExitSpec.

Inferdrome produces measurements. ExitSpec owns customer acceptance.

## Project status

The v0.1 product charter, pinned-vLLM `0.26.0` capability contract, and eight
public v1 schemas are frozen. The Inferdrome producer path now includes strict
resolution, immutable workspaces, deterministic reduction, staged read-only
bundles, exact-byte manifests, attached-endpoint preflight, bounded no-shell
vLLM execution, version-specific normalization, offline cross-artifact
recalculation, and the chartered command-line workflow.

The opt-in managed local-vLLM path now launches a pinned server on Linux,
captures locally verified GPU, CUDA, driver, model-snapshot, producer, and
launch evidence, and permits customer eligibility only when offline
cross-verification succeeds. The compatible-NVIDIA-host capture and reviewed
example bundle are still pending, so the PR 7 gate is not yet complete.
Independent acceptance remains a separate consumer boundary; it is not
implemented inside this repository.

## Quick start

```bash
uv sync

uv run inferdrome validate examples/fake-smoke.yaml
uv run inferdrome run examples/fake-smoke.yaml --runs-root runs \
  --run-id run-0123456789abcdef0123456789abcdef
uv run inferdrome inspect run-0123456789abcdef0123456789abcdef \
  --runs-root runs
uv run inferdrome bundle verify \
  runs/run-0123456789abcdef0123456789abcdef/bundle
uv run inferdrome reduce \
  runs/run-0123456789abcdef0123456789abcdef/bundle
uv run inferdrome summarize \
  runs/run-0123456789abcdef0123456789abcdef/bundle
```

The fake path is always marked `SYNTHETIC_ONLY`. An attached-vLLM run requires
an explicit local tokenizer directory and remains `INELIGIBLE` until the
real-GPU provenance gate is satisfied; the CLI never upgrades configured or
server-reported facts into locally verified evidence.

The managed NVIDIA reproduction path and its one-command rejection demo are
documented in [Managed real-GPU proof](docs/REAL_GPU_PROOF.md).

## v0.1 principles

- Preserve native benchmark output.
- Never silently invent unavailable observations.
- Keep raw observations separate from derived measurements.
- Make reducers deterministic and independently reproducible.
- Treat integrity, provenance, eligibility, and acceptance as different facts.
- Reject synthetic fixtures from customer-evidence flows.
- Keep completed evidence bundles immutable.
- Prefer one narrow, proven backend over broad adapter coverage.

## Canonical documentation

- [Product charter](docs/PRODUCT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Threat model](docs/THREAT_MODEL.md)
- [v0.1 definition of done](docs/V0_1_DEFINITION_OF_DONE.md)
- [Build roadmap](docs/ROADMAP.md)
- [Public contracts v1](docs/PUBLIC_CONTRACTS_V1.md)
- [Resolution and run workspace](docs/RESOLUTION_AND_WORKSPACE.md)
- [Deterministic reduction and fake adapter](docs/DETERMINISTIC_REDUCTION.md)
- [Evidence bundle and offline verification](docs/EVIDENCE_BUNDLE_V1.md)
- [Pinned vLLM 0.26.0 adapter](docs/VLLM_0_26_ADAPTER.md)
- [Managed real-GPU proof](docs/REAL_GPU_PROOF.md)
- [CLI and orchestration](docs/CLI.md)
- [Architecture decision records](docs/adr/README.md)
- [Pinned-vLLM capability spike](spikes/vllm-0.26.0/README.md)

## Naming

```text
Product: Inferdrome
Repository: inferdrome
CLI: inferdrome
Public schemas: inferdrome.*
```

## Deliberate v0.1 limits

Inferdrome v0.1 does not include a web dashboard, hosted service, cloud or
Kubernetes orchestration, GPU telemetry, statistical A/B comparisons, router
analysis, automatic optimization, or a second serving engine.

The first release proves the evidence pipeline before expanding the product.
