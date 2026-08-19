# Inferdrome

Inferdrome is a reproducible evidence pipeline for LLM-serving experiments.

It resolves experiment inputs, invokes a pinned benchmark tool, preserves the
tool's native output, normalizes request-level observations, deterministically
computes measurements, verifies bundle integrity, and hands portable evidence
to an independent acceptance verifier such as ExitSpec.

Inferdrome produces measurements. ExitSpec owns customer acceptance.

## Project status

The v0.1 product charter, pinned-vLLM `0.26.0` capability contract, and its
eight original public v1 schemas are frozen. The Inferdrome producer path
includes strict resolution, immutable workspaces, deterministic reduction, staged read-only
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

The repository also includes an accepted post-v0.1 local evidence dashboard.
It presents Runs, Run detail, Compare, and Evidence views over the same bounded
verification and deterministic recalculation path. It remains read-only,
loopback-only, database-free, and outside the v0.1 release gate.

The first v0.2 vertical slice adds an additive ninth public schema for immutable
same-configuration trial sets. Each set pins 2–100 independently verified runs
by run ID and bundle digest, keeps every request population separate, and
reports equal-per-run descriptive variation. These groupings are explicitly
retrospective; they do not claim experimental control, causality, significance,
or customer acceptance.

The second v0.2 vertical slice adds separate immutable controlled-comparison
plan and result contracts, bringing the public schema count to eleven. The
initial design is intentionally narrow: two arms, one typed
`traffic.concurrency` treatment, 2–100 permuted run pairs, one frozen primary
outcome, complete-case paired arithmetic, and no uncertainty estimate. A result
is `COMPARABLE` only when every planned bundle, schedule, fingerprint,
allowlisted environment field, and outcome semantic verifies; otherwise all
outcome arithmetic is suppressed. Predeclaration is `OPERATOR_ATTESTED`, not
trusted proof of chronology, and comparability is not causality or acceptance.

The third v0.2 vertical slice adds a fail-closed executor for that frozen
design without adding another public schema. One command verifies the retained
plan digest and exact arm inputs, executes only the preallocated schedule,
reuses only an independently verified `COMPLETE` prefix, creates both planned
Trial Sets, publishes the result, and reverifies the full evidence chain. The
dashboard exposes this operational progress without treating it as execution
attestation or portable evidence.

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

uv run inferdrome trial-set create \
  --run run-0123456789abcdef0123456789abcdef \
  --run run-fedcba9876543210fedcba9876543210 \
  --title "Repeated serving treatment" \
  --runs-root runs --trial-sets-root trial-sets

uv run inferdrome comparison-plan create \
  --baseline-source examples/controlled-concurrency-2.yaml \
  --candidate-source examples/controlled-concurrency-4.yaml \
  --title "Concurrency 2 versus 4" \
  --hypothesis "Concurrency may change attempted throughput" \
  --repetitions 2 \
  --primary-outcome attempted_request_throughput_per_s:rate \
  --runs-root runs --comparison-plans-root comparison-plans

uv run inferdrome comparison-plan execute \
  comparison-plans/comparison-plan-<id> \
  --expected-digest "$PLAN_DIGEST" \
  --baseline-source examples/controlled-concurrency-2.yaml \
  --candidate-source examples/controlled-concurrency-4.yaml \
  --runs-root runs --trial-sets-root trial-sets \
  --comparison-results-root comparison-results
```

Install the optional dashboard runtime and inspect those bundles locally:

```bash
uv sync --extra dashboard
uv run inferdrome dashboard \
  --runs-root runs --trial-sets-root trial-sets \
  --comparison-plans-root comparison-plans \
  --comparison-results-root comparison-results --open
```

For a recording-ready Mac walkthrough, one command prepares an exact four-run
synthetic comparison, independently reverifies it, and opens the populated
dashboard:

```bash
./scripts/run_local_demo.py
```

This path is always labeled `SYNTHETIC_ONLY`; it demonstrates the real product
workflow without claiming genuine GPU performance. See the
[local product demo runbook](docs/LOCAL_DEMO.md) for the recording route and
approved claim boundary.

The fake path is always marked `SYNTHETIC_ONLY`. An attached-vLLM run requires
an explicit local tokenizer directory and remains `INELIGIBLE` until the
real-GPU provenance gate is satisfied; the CLI never upgrades configured or
server-reported facts into locally verified evidence.

The managed NVIDIA reproduction path and its one-command rejection demo are
documented in [Managed real-GPU proof](docs/REAL_GPU_PROOF.md).
The same pinned host can produce a four-run controlled-comparison proof pack:

```bash
.inferdrome-gpu/venv/bin/python scripts/run_real_gpu_demo.py --comparison
```

That mode retains the plan digest before execution, verifies every
customer-eligible bundle and both Trial Sets, reverifies the result, and proves
that a second executor invocation reuses the exact completed schedule.
For an operator-provided SSH GPU VM, the runbook also includes one bounded
controller that transfers the exact clean Git commit, runs both proof modes,
retrieves a checksumed archive, and independently verifies it on the local
workstation. It never creates cloud instances. For Lambda, optional cost-guard
flags require the actual billing origin, fail closed on missing rate or endpoint
identity, arm a readiness-confirmed buffered termination deadline, and confirm
termination on every controller exit path; the generic SSH path remains
provider-neutral.

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
- [v0.1 release checklist](docs/V0_1_RELEASE_CHECKLIST.md)
- [Build roadmap](docs/ROADMAP.md)
- [Public contracts v1](docs/PUBLIC_CONTRACTS_V1.md)
- [Resolution and run workspace](docs/RESOLUTION_AND_WORKSPACE.md)
- [Deterministic reduction and fake adapter](docs/DETERMINISTIC_REDUCTION.md)
- [Evidence bundle and offline verification](docs/EVIDENCE_BUNDLE_V1.md)
- [Pinned vLLM 0.26.0 adapter](docs/VLLM_0_26_ADAPTER.md)
- [Managed real-GPU proof](docs/REAL_GPU_PROOF.md)
- [CLI and orchestration](docs/CLI.md)
- [Local evidence dashboard](docs/DASHBOARD.md)
- [Local product demo](docs/LOCAL_DEMO.md)
- [Repeated trial sets](docs/TRIAL_SETS.md)
- [Controlled comparisons](docs/CONTROLLED_COMPARISONS.md)
- [Architecture decision records](docs/adr/README.md)
- [Pinned-vLLM capability spike](spikes/vllm-0.26.0/README.md)
- [Contribution guide](CONTRIBUTING.md)

## Naming

```text
Product: Inferdrome
Repository: inferdrome
CLI: inferdrome
Public schemas: inferdrome.*
```

## Deliberate v0.1 limits

Inferdrome v0.1 does not include the dashboard in its release gate. The accepted
post-v0.1 dashboard remains local and read-only. Descriptive Trial Sets and the
narrow operator-attested controlled-comparison workflow are available.
Trusted chronology or authorship, additional treatments, confidence or
significance claims, hosted service, cloud or Kubernetes orchestration, GPU
telemetry, router analysis, automatic optimization, and a second serving engine
remain outside the implemented product.

The first release proves the evidence pipeline before expanding the product.
