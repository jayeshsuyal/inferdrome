# Inferdrome

Inferdrome is a reproducible evidence pipeline for LLM-serving experiments.

It resolves experiment inputs, invokes a pinned benchmark tool, preserves the
tool's native output, normalizes request-level observations, deterministically
computes measurements, verifies bundle integrity, and hands portable evidence
to an independent acceptance verifier such as ExitSpec.

Inferdrome produces measurements. ExitSpec owns customer acceptance.

## v0.3 development boundary

Inferdrome v0.3 continues the evidence plane for qualifying changes to open-weight
inference systems. It links configuration to observation identity and
freshness, routing decision, selected endpoint, request outcome, and a sealed
evidence package that can be checked offline. Measurements are sensors in that
evidence chain, not a leaderboard or the product itself. Inferdrome is not a
production router, cloud provisioner product, Kubernetes platform, promotion
controller, or `PASS`/`FAIL`/`NOT_PROVEN` authority.

The released [v0.2 capability and limitations contract](docs/V0_2_CAPABILITIES.md)
remains an immutable baseline. The active [v0.3 development capability and
limitations contract](docs/V0_3_CAPABILITIES.md) is deliberately explicit:
local two-endpoint routing execution is proven only at loopback socket level;
the `llm-d-attached-v1` profile is local-fixture validation rather than a live
router integration; historical A10 serving evidence is preserved as
`EXTERNAL_ONLY`; no two-A100 multi-endpoint campaign has executed; and GCP or
Kubernetes production operation is not claimed. Inspect the active closed
claim boundary with `python -m inferdrome capabilities`. The
[v0.2.0 changelog](CHANGELOG.md) records the stable release boundary.
Repository source does not itself authorize an annotated tag or GitHub Release.

For an interviewable local walkthrough, run:

```bash
./scripts/run_local_demo.py
```

It creates and independently rechecks a sealed, deterministic two-endpoint
routing campaign plus an immutable stale-telemetry qualification descriptor
before launching the read-only dashboard. The **Routing campaigns** view
explains every observation age/admissibility, routing decision,
endpoint/fallback, terminal outcome, reset/fault timeline, and complete request
population. The **Causal qualification** view gives a compact, verified
cross-policy explanation of the fixed health-fresh/load-stale observation and
links back to those full receipts. The demonstration is `SYNTHETIC_ONLY`; it
is not GPU or cloud evidence.

The additive [stale-telemetry qualification](docs/STALE_TELEMETRY_QUALIFICATION_V1.md)
reuses that sealed request-level package for the fixed health-fresh/load-stale
experiment. It compares three declared local policy labels under one virtual
fault vector, keeps every six-request terminal population separate, and
replays the source package offline. Its [dashboard projection](docs/ROUTING_QUALIFICATION_DASHBOARD_V1.md)
is read-only and descriptor-only for evidence download. It does not create a live routing data
plane, download a model, or claim a general routing winner.

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
cross-verification succeeds. One genuine A10 single-run and four-run comparison
archive is independently verified and pinned by a standalone capability profile,
an `EXTERNAL_ONLY` publication review, and a deterministic handoff manifest.
The Inferdrome repository is Apache-2.0 licensed, but that choice does not
license the model, workload, vLLM, generated output, or raw archive. The raw
archive remains local pending its separate owner licensing, privacy, and
publication approvals. Independent acceptance remains a separate consumer
boundary; it is not implemented inside this repository.

The frozen Qwen3-8B BF16 profile has now also completed one genuine bounded A10
capability spike: 96/96 measured requests succeeded, the independently
recalculated native-TTFT p95 is `242,426,174 ns`, and the output-token rate is
`28.870215 tokens/s`. The exact archive and post-termination receipts bind to
producer commit `058482df47377aaae6303015746f9a8e05d7e0f7`. Its deterministic
[publication records](evidence/gpu/2026-08-21-qwen3-8b-a10/README.md) remain
`EXTERNAL_ONLY`; this is one runtime observation, not hardware attestation, a
cross-GPU comparison, or an acceptance verdict.

The same-model hardware-control path is locally conformant for exact A100
40 GB PCIe and H100 80 GB PCIe targets. Their generated execution packs freeze
provider-rate and session-cost boundaries, exact `nvidia-smi` product names,
the existing Qwen3-8B profile and workload, and explicit confirmation before
every paid launch. A GET-only Lambda watcher can resolve live capacity without
launching anything. Neither unexecuted pack is a GPU receipt or runtime claim.

The 2026-08-23 A100 SXM4 Qwen3-8B capability capture now has committed,
privacy-safe outer metadata and an archive-backed local dashboard launcher. Its
exact raw archive remains ignored and `EXTERNAL_ONLY`; the committed records
pin only digests, bounded measurements, provider configuration, controller
termination evidence, and claim boundaries. The controller's cost is an
estimate, not provider invoice truth. See the
[A100 SXM4 evidence handoff](evidence/gpu/2026-08-23-qwen3-8b-a100-sxm4/README.md).

The repository also includes an accepted post-v0.1 local evidence dashboard.
It presents Runs, Run detail, Compare, and Evidence views over the same bounded
verification and deterministic recalculation path. It remains read-only,
loopback-only, database-free, and outside the v0.1 release gate.

Release status is intentionally mixed: genuine Linux/NVIDIA captures are
producer-side evidence; the Docker Compose qualification is local synthetic
qualification; and Lambda/GCP/Kubernetes surfaces are dry-run, reference, or
simulation boundaries. Human security approval is recorded and the raw A10
archive remains `KEEP_EXTERNAL_ONLY`/`EXTERNAL_ONLY` and unpublished. ExitSpec
acceptance remains independently owned: its prospective `PASS`, `FAIL`, and
`NOT_PROVEN` evaluations and receipt are `NOT_RECORDED`, are deferred
post-v0.1 by the [owner policy exception](docs/reviews/V0_1_OWNER_RELEASE_POLICY_EXCEPTION.md),
and are not claimed by the producer-side `v0.1.0` release.

The provider-neutral deployment layer begins with a strict, non-executing
[`inferdrome.deployment.v1` contract](docs/DEPLOYMENT_SPEC_V1.md). It pins
deployment intent, runtime/model identity, benchmark topology, image digests,
resource and cleanup limits, and a controller cost ceiling without changing
the benchmark methodology or frozen evidence schemas. Local mock, Lambda
dry-run/reference, and GCP dry-run/reference examples are non-executing; no
cloud resource or evidence publication is implied.

The reproducible runner image is a separate packaging boundary documented in
[RUNNER_IMAGE_V1.md](docs/RUNNER_IMAGE_V1.md). Its normal entrypoint is the
canonical `inferdrome` CLI with unchanged benchmark semantics; serving engines
remain separate. The explicitly named endpoint probe is synthetic and
evidence-ineligible, and a Docker build or image digest is never presented as
an executed receipt.

The pinned vLLM 0.26.0 runtime and local Compose mock/GPU boundary is documented
in [VLLM_COMPOSE_V1.md](docs/VLLM_COMPOSE_V1.md). The mock path is synthetic
only. The opt-in Linux/NVIDIA profile keeps `vllm serve` in a separate engine
service and runs the canonical `inferdrome run` plus its existing `vllm bench
serve` producer in a distinct runner service; Docker/GPU execution remains an
explicit environment gate. The safe base Compose file contains no GPU
services; the guarded wrapper loads the separately gated GPU override and
selects only the benchmark runner as its root service. Its preflight verifies
the complete frozen Qwen3 snapshot and the versioned private-endpoint binding.

The guarded [Deployment Qualification v1 runbook](docs/DEPLOYMENT_QUALIFICATION_V1.md)
executes the accepted local Compose mock as two separate services, verifies one
bounded deterministic synthetic output, performs exact-project cleanup, proves
zero scoped residue, removes and verifies only its two project-derived image
tags, and publishes a no-replace immutable qualification report. It rechecks
source cleanliness and revision after cleanup before publication.
It never launches cloud/GPU work, issues a receipt, publishes evidence, or
claims vLLM/NVIDIA behavior.

The minimal Kubernetes boundary is documented in
[KUBERNETES_V1.md](docs/KUBERNETES_V1.md). It is one guarded batch Job with a
native serving sidecar and a colocated runner. The local mock wrapper retrieves
one bounded synthetic runner log before deleting its disposable Pod; it never
uses `kubectl cp`/`exec` after completion and never claims Kubernetes evidence.
The GPU template requires operator-provided model, experiment, and evidence
PVCs plus an immutable runner digest, but this repository performs no cluster,
GPU, or evidence execution.

The final producer-capability slice adds a pinned, non-executing SGLang 0.5.18
adapter and additive normalization envelope in
[SGLANG_0_5_ADAPTER.md](docs/SGLANG_0_5_ADAPTER.md). It uses the native
`python -m sglang.benchmark.serving` module and `/generate` semantics, but
does not alter frozen v0.1 vLLM evidence schemas. Because SGLang persisted
output lacks request IDs and start offsets, every report is evidence-ineligible
and leaves request-plan binding unavailable; no SGLang install, server, GPU,
container, Kubernetes, or cloud execution is claimed.

PR7 adds an offline, read-only GCP inventory and deterministic dry-run plan in
[GCP_DRY_RUN_V1.md](docs/GCP_DRY_RUN_V1.md). It consumes an explicit compute
project planning context and a synthetic/local inventory snapshot; it performs
no credential resolution, provider call, mutation, capacity/pricing claim,
runtime launch, receipt issuance, or evidence publication.

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
is `COMPARABLE` only when all run IDs are distinct, every member is
`CUSTOMER_ELIGIBLE`, both arms and all bundles share one non-null ExitSpec
contract identity, and every planned bundle, schedule, fingerprint, allowlisted
environment field, and outcome semantic verifies; otherwise all outcome
arithmetic is suppressed. Predeclaration is `OPERATOR_ATTESTED`, not trusted
proof of chronology, and comparability is not causality or acceptance.

The third v0.2 vertical slice adds a fail-closed executor for that frozen
design without adding another public schema. One command verifies the retained
plan digest and exact arm inputs, executes only the preallocated schedule,
reuses only an independently verified `COMPLETE` prefix, creates both planned
Trial Sets, publishes the result, and reverifies the full evidence chain. The
dashboard exposes this operational progress without treating it as execution
attestation or portable evidence.

## Quick start

Install the pinned Python environment first:

```bash
uv lock --check
uv sync --frozen --extra dev --extra dashboard
```

The offline candidate preflight checks repository-owned release inputs and
reports manual release blockers without treating them as proven. Normal CI uses
the `auto` phase: it selects candidate only when both version locations are
exactly `0.3.0.dev0`; for exact `0.3.0`, it selects `final-pre-tag` only while
`v0.3.0` is absent, selects `post-tag` when that tag resolves to `HEAD`, and
fails closed if the tag resolves elsewhere. Run it from a clean checkout when
checking a development candidate commit:

```bash
.venv/bin/python scripts/release_preflight.py \
  --phase auto --repository-only --require-clean
```

The final release-closure mode additionally delegates to the existing
engineering and dashboard gates and fails closed while manual or external
checklist inputs remain open. It is run only on a deliberate final release
candidate commit after both version locations have been changed to `0.3.0`:

```bash
.venv/bin/python scripts/release_preflight.py \
  --phase final-pre-tag --run-gates --require-clean
```

It performs no cloud/provider operation, GPU launch, deployment, publication,
tagging, or merge. A v0.3.0 candidate is not an authorization to create a
tag or GitHub Release; the exact final release procedure is recorded in the
[v0.3 release checklist](docs/V0_3_RELEASE_CHECKLIST.md).

For the specifically authorized producer-side `v0.1.0` release, CI uses the
repository-only preflight so the still-pending ExitSpec inputs are reported,
not hidden. The bounded exception is recorded in
[V0_1_OWNER_RELEASE_POLICY_EXCEPTION.md](docs/reviews/V0_1_OWNER_RELEASE_POLICY_EXCEPTION.md);
it does not make an ExitSpec result or receipt exist.

```bash
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
uv lock --check
uv sync --frozen --extra dashboard
uv run inferdrome dashboard \
  --runs-root runs --trial-sets-root trial-sets \
  --comparison-plans-root comparison-plans \
  --comparison-results-root comparison-results --open
```

Authentication is opt-in for a local loopback session. Create a `0600`
keyring with the CLI (the bearer secret is printed exactly once), then pass it
to the server:

```bash
uv run inferdrome dashboard-keyring create \
  --keyring .local/dashboard-keyring.json --label workstation
uv run inferdrome dashboard --keyring .local/dashboard-keyring.json \
  --runs-root runs --trial-sets-root trial-sets
```

`list`, `revoke`, and `rotate` are CLI-only and never print stored secrets.
This is not public hosting, TLS, or multi-user SaaS; the server remains
loopback-only. See [the authentication ADR](docs/adr/0012-opt-in-local-dashboard-authentication.md)
for the exact token and browser-memory boundary.

For a recording-ready Mac walkthrough, one command prepares an exact four-run
synthetic comparison, independently reverifies it, and opens the populated
dashboard:

```bash
./scripts/run_local_demo.py
```

This path is always labeled `SYNTHETIC_ONLY`; it demonstrates the real product
workflow without claiming genuine GPU performance. See the
[local product demo runbook](docs/LOCAL_DEMO.md) for the recording route, a
five-minute interview script, and the approved claim boundary.

When the reviewed Qwen3 capture is present locally, one command independently
reverifies the exact archive and post-termination receipts before opening that
genuine run through the same read-only dashboard:

```bash
PYTHONPATH=src .venv/bin/python \
  scripts/run_qwen3_evidence_dashboard.py --open
```

This path never substitutes committed summary metadata for the evidence bundle.
Public CI validates the committed handoff cross-digests; full archive
reverification remains local until the owner approves delivery of the exact
`EXTERNAL_ONLY` bytes.

The fake path is always marked `SYNTHETIC_ONLY`. An attached-vLLM run requires
an explicit local tokenizer directory and remains `INELIGIBLE` until the
real-GPU provenance gate is satisfied; the CLI never upgrades configured or
server-reported facts into locally verified evidence.

The managed NVIDIA reproduction path and its one-command rejection demo are
documented in [Managed real-GPU proof](docs/REAL_GPU_PROOF.md).
The additive prospective, three-case ExitSpec-linked path is documented in
[Prospective contract-linked GPU capture](docs/PROSPECTIVE_REAL_GPU_CAPTURE.md).
It has no checked-in contract digests or runnable source files until the
external contracts are genuinely frozen.
The same pinned host can produce a four-run controlled-comparison proof pack:

```bash
.inferdrome-gpu/venv/bin/python scripts/run_real_gpu_demo.py --comparison
```

That mode retains the plan digest before execution, verifies every
customer-eligible bundle and both Trial Sets, reverifies the result, and proves
that a second executor invocation reuses the exact completed schedule.
The 2026-08-20 Lambda A10 capture completed and independently verified that
entire pack; its exact hashes and remaining ExitSpec boundary are recorded in
the runbook.
The separate 2026-08-21 Qwen3-8B A10 spike closes runtime compatibility and fit
for that exact profile only. Other GPUs and cross-GPU conclusions remain
unproven until the frozen campaign is executed without changing the model or
workload.
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
- [Deployment specification v1](docs/DEPLOYMENT_SPEC_V1.md)
- [Deployment receipt v1](docs/DEPLOYMENT_RECEIPT_V1.md)
- [Reproducible runner image v1](docs/RUNNER_IMAGE_V1.md)
- [Threat model](docs/THREAT_MODEL.md)
- [v0.1 definition of done](docs/V0_1_DEFINITION_OF_DONE.md)
- [v0.1 release checklist](docs/V0_1_RELEASE_CHECKLIST.md)
- [Build roadmap](docs/ROADMAP.md)
- [Public contracts v1](docs/PUBLIC_CONTRACTS_V1.md)
- [Prospective contract-linked GPU capture](docs/PROSPECTIVE_REAL_GPU_CAPTURE.md)
- [Prospective capture rehearsal packet](docs/PROSPECTIVE_CAPTURE_REHEARSAL_V0_1.md)
- [Resolution and run workspace](docs/RESOLUTION_AND_WORKSPACE.md)
- [Deterministic reduction and fake adapter](docs/DETERMINISTIC_REDUCTION.md)
- [Evidence bundle and offline verification](docs/EVIDENCE_BUNDLE_V1.md)
- [Pinned vLLM 0.26.0 adapter](docs/VLLM_0_26_ADAPTER.md)
- [Minimal Kubernetes Job v1](docs/KUBERNETES_V1.md)
- [SGLang 0.5.18 producer boundary](docs/SGLANG_0_5_ADAPTER.md)
- [Managed real-GPU proof](docs/REAL_GPU_PROOF.md)
- [CLI and orchestration](docs/CLI.md)
- [Local evidence dashboard](docs/DASHBOARD.md)
- [Local product demo](docs/LOCAL_DEMO.md)
- [Repeated trial sets](docs/TRIAL_SETS.md)
- [Controlled comparisons](docs/CONTROLLED_COMPARISONS.md)
- [Architecture decision records](docs/adr/README.md)
- [Pinned-vLLM capability spike](spikes/vllm-0.26.0/README.md)
- [Contribution guide](CONTRIBUTING.md)

## License

Inferdrome's repository-authored source and packaged Python project are
licensed under the [Apache License 2.0](LICENSE). Packaged dashboard assets
retain the licenses listed in [Third-party notices](THIRD_PARTY_NOTICES.md).

This repository license does not grant or imply rights to separately supplied
models or tokenizers, benchmark workloads or datasets, vLLM or other serving
engines, generated output, user content, raw evidence archives, container base
images, or other external materials. Those materials remain subject to their
own terms and approvals. In particular, the reviewed A10 raw archive remains
ignored, local, and `EXTERNAL_ONLY`; adding Apache-2.0 to Inferdrome does not
authorize its publication.

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
significance claims, hosted service, live cloud or Kubernetes orchestration,
GPU telemetry, router analysis, automatic optimization, and an executable or
eligible second serving engine remain outside the implemented product; PR11's
SGLang slice is a non-executing, evidence-ineligible producer/normalization
capability boundary. The additive Kubernetes Job
contract is a guarded static/local simulation boundary, not a platform.

The first release proves the evidence pipeline before expanding the product.
