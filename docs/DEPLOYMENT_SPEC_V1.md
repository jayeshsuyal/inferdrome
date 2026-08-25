# Deployment specification v1

Status: **Provider-neutral outer deployment contract; PR4 adds a synthetic
immutable provenance receipt, while cloud/runtime adapters remain
unimplemented**

The deployment specification is an additive control-plane document for future
local, Lambda, GCP, Docker, and Kubernetes adapters. It describes deployment
intent and bounded safety requirements. It does not launch a resource, contact a
provider, resolve a credential, build a container, execute vLLM, or produce an
evidence receipt.

Inferdrome remains the measurement owner. vLLM or SGLang remains the serving
runtime owner. The deployment contract changes where those components run; it
does not copy, edit, or replace the frozen benchmark methodology or any v0.1
evidence schema.

## Files and identity

The closed Draft 2020-12 schema is
[`schemas/deployment/v1/deployment-spec.schema.json`](../schemas/deployment/v1/deployment-spec.schema.json).
The reference model is
[`src/inferdrome/deployment/spec.py`](../src/inferdrome/deployment/spec.py).
The contract is intentionally outside `schemas/public/v1`; it is an outer
deployment capability and does not change the frozen evidence bundle.

The exact schema literal is `inferdrome.deployment.v1`. A deployment document
has no in-band digest field. Its canonical bytes are RFC 8785 JSON over the
Pydantic JSON value, including explicit nulls, and its digest is domain
separated as:

```text
inferdrome:deployment-spec-v1\0 + RFC 8785 canonical JSON bytes
```

Use `canonical_deployment_spec_bytes()` and `deployment_spec_digest()` from the
reference module. Reordering object keys or changing JSON whitespace cannot
change the digest. A future incompatible contract is `v2`; readers reject it
rather than silently upgrading it. The JSON preflight rejects duplicate object
keys at every nesting level before Pydantic validation, so no parser can select
a different last-key-wins interpretation of the deployment.

## Top-level contract

Every field is required unless the schema explicitly permits `null` or an empty
bounded reference collection.

| Section | Contract responsibility |
|---|---|
| `mode` and `execution_intent` | Separates development/mock, local execution, and non-executing provider references |
| `provider` | Provider identity, bounded region, and secret references only |
| `runtime` | Serving engine, exact supported version or reserved reference, pinned model/tokenizer revisions, and structured endpoint |
| `topology` | Registered methodology reference, runner/runtime colocation, and endpoint scope |
| `artifacts` | Separate runner and serving-runtime image identities |
| `resources` | Bounded CPU, memory, storage, and GPU requirements |
| `timeouts` | Bounded startup, request, total, cleanup, and termination-confirmation budgets |
| `cleanup_policy` | Cleanup on every exit, auditable cleanup receipt, bounded attempts, and fail-closed orphan handling |
| `cost_ceiling` | Positive/zero bounded USD ceiling, hard-limit declaration, estimate boundary, and external invoice boundary |

The model and schema reject unknown fields at every object boundary. IDs,
revisions, image references, endpoints, resource numbers, timeout numbers, and
cost strings all have finite bounds. Integer fields are strict and do not accept
booleans.

## Execution intent and capability boundary

The v1 implementation deliberately exposes only these intent values:

| Intent | Allowed provider | Meaning |
|---|---|---|
| `mock_only` | `local` | Development-only local mock. It declares zero GPUs and zero cost and can never be treated as GPU evidence. |
| `local_execute` | `local` | A later local adapter may execute the declared configuration. This contract does not execute it. |
| `dry_run_reference` | `lambda_cloud`, `gcp` | Privacy-safe reference for a future provider adapter. It is non-executing and proves no capacity, launch, runtime, cleanup, cost, or evidence fact. |

`mode: proof` is a configuration gate, not an acceptance verdict. It requires
the stronger proof-safe shape even when the intent is a cloud dry-run
reference. A cloud reference therefore remains explicitly non-executing until a
later lifecycle slice adds an authorized adapter and receipt boundary.

The provider/runtime combinations are intentionally narrow:

- `local` has no cloud region and cannot use `dry_run_reference`.
- `lambda_cloud` and `gcp` require a region and can only use
  `dry_run_reference` in v1.
- `vllm` is pinned to the repository's supported `0.26.0`
  `vllm_bench_serve` shape.
- `sglang` has a versioned `sglang_reference_v1` shape, but validation permits
  it only as a development dry-run reference. No SGLang lifecycle, serving,
  normalization, or evidence adapter is present.
- Other providers, engines, versions, and adapters fail closed.

## Lifecycle interfaces and local mock

The execution-side interface is in
[`src/inferdrome/deployment/lifecycle.py`](../src/inferdrome/deployment/lifecycle.py).
It consumes a validated `DeploymentSpec` but does not alter this contract or
the frozen benchmark/evidence contracts.

`ProviderAdapter` owns only provider resource state:

- `validate(spec)` checks capability without side effects;
- `acquire(spec)` returns opaque in-memory provider state;
- `cleanup(spec, handle)` is attempted once when acquisition was attempted;
- `confirm_cleanup(spec, handle)` is the final bounded confirmation.

`RuntimeAdapter` owns only serving state:

- `validate(spec)` checks the engine capability without side effects;
- `start()` and `wait_ready()` establish the serving endpoint;
- `stop()` is attempted once whenever runtime start was attempted.

`LifecycleCoordinator` executes the fixed validation, acquisition, runtime
start/readiness, injected benchmark callback, runtime stop, provider cleanup,
and final-confirmation phases. The callback receives only a bounded endpoint;
it remains responsible for invoking the existing benchmark methodology. The
coordinator injects process control, readiness probing, a monotonic clock, and
the callback so tests do not require CUDA, a subprocess, or a network.

The runtime adapter's returned endpoint must exactly match the spec's scheme,
host, port, and path before readiness or benchmark. If it drifts, the actual
returned handle is retained and stopped, then provider cleanup and final
confirmation still run. A stop, cleanup, or confirmation result is successful
only when `confirmed` is true, `orphaned` is false, and `error_code` is null;
unconfirmed, orphaned, or contradictory results produce a failed phase.

The ephemeral outcome has separate bounded `primary_error_code` and
`cleanup_error_code` fields. Its effective `error_code` is the cleanup error
when cleanup is unconfirmed, and the terminal status is `FAILED`; this keeps a
cancellation, interrupt, benchmark, or runtime-start cause without allowing a
safety-critical cleanup failure to appear successful or merely cancelled.

PR3 rejects Lambda/GCP provider execution and SGLang execution before any
provider or runtime side effect. The committed local adapter accepts only
`local` + `mock_only` + pinned vLLM-shaped development specifications. It
provisions no resource and uses an in-memory process/readiness boundary. Its
outcome is explicitly `synthetic_only: true` and `evidence_eligible: false`;
it is not a deployment receipt and does not attest to serving, latency, GPU,
cost, cleanup, or provider state. The existing `inferdrome run` CLI and local
measurement pipeline remain backward compatible and are not routed through
this non-evidence-producing mock path.

## Methodology and topology

`topology.methodology` contains only:

```text
source = registered_manifest
methodology_id
manifest_sha256
```

It is a pointer to an existing registered benchmark methodology. Prompts,
sampling values, request records, workload text, and copied campaign content
are not legal deployment fields. A future adapter must resolve the registered
manifest and compare its exact digest before execution. Deployment configuration
cannot mutate the benchmark methodology.

`runner_runtime_colocation` is fixed to `colocated`. The benchmark runner and
serving runtime must share the declared execution boundary for valid latency
topology. `endpoint_scope` must equal the runtime endpoint visibility:

- `loopback` uses exactly `127.0.0.1`;
- `private` uses an explicit RFC1918 IPv4 address or an explicitly private DNS
  suffix (`.internal`, `.local`, or `.private`); all other IPv4 literals,
  including documentation, benchmark, carrier-grade NAT, reserved, multicast,
  unspecified, link-local, and broadcast ranges, are rejected;
- public addresses, link-local metadata addresses, `0.0.0.0`, user-info URLs,
  query strings, fragments, unsafe path segments, and public DNS are not
  representable by the structured endpoint contract.

These checks describe a safe intent. They do not attest that DNS, routing,
firewalls, or a provider actually enforced the declared boundary.

## Images, model pins, resources, and lifecycle

`artifacts.runner_image` and `artifacts.serving_runtime_image` are separate
identities because later Docker/runtime work has two runtime boundaries. A
development mock may use a bounded non-floating tag. Proof mode requires a
SHA-256 digest for both images; a tag may remain as human metadata but is never
the proof identity. `latest` is rejected everywhere.

Proof mode also requires:

- a full hexadecimal model revision and tokenizer revision;
- the supported pinned runtime identity;
- a positive explicit GPU count and model;
- loopback/private endpoint semantics;
- runner/runtime colocation;
- immutable image digests; and
- the mandatory cleanup and positive paid-provider cost boundaries.

All timeouts are finite positive integers. The total timeout must cover startup
and cleanup. Cleanup is always declared on every exit, always requires a
cleanup receipt, allows at most three attempts, and blocks the next run when an
orphan remains unresolved. This is a policy input for later lifecycle code, not
evidence that cleanup happened.

`cost_ceiling` is always a hard controller ceiling. The value is an estimate,
not provider invoice truth. Paid proof references require a positive value;
local mock requires exactly `"0"`. Provider invoice truth remains external and
must be recorded in a later provider receipt if that adapter is authorized.

## Secret boundary

The schema contains no secret-value field. Credential and model-registry access
use a discriminated reference union. An environment-variable reference has
only:

```json
{"kind": "environment_variable", "name": "INFERDROME_PROVIDER_TOKEN"}
```

A GCP Secret Manager reference has only bounded resource components:

```json
{
  "kind": "gcp_secret_manager",
  "project_id": "inferdrome-example",
  "secret_id": "provider-token",
  "version": "1"
}
```

The reference is not resolved by this module. Credential-shaped keys such as
`api_key`, `access_token`, `password`, `private_key`, and `secret_value` are
rejected by the non-disclosing JSON loader. Common Lambda/OpenAI, GitHub,
private-key, and high-entropy credential-shaped components are also rejected
at the structured reference boundary. Unknown fields are rejected by the model
as well. Neither reference variant nor its canonical output can serialize a
secret value.

Never put a credential, prompt, generated response, operational identifier, or
raw provider response in a deployment example. Examples are privacy-safe
references only.

## Non-executing examples

The committed examples are deliberately explicit about their intent:

- [`local-mock.json`](../deployments/v1/examples/local-mock.json) — local,
  development, mock-only, zero GPU, zero cost.
- [`lambda-dry-run-reference.json`](../deployments/v1/examples/lambda-dry-run-reference.json)
  — Lambda identity, private topology, proof-safe shape, dry-run only.
- [`gcp-dry-run-reference.json`](../deployments/v1/examples/gcp-dry-run-reference.json)
  — GCP identity, private topology, proof-safe shape, dry-run only.

The image digests in the cloud examples are non-executing reference values;
they are not claims that those images exist, were pulled, or ran. The examples
do not authorize a cloud launch, publish evidence, or establish an acceptance
outcome.

## Later adapter obligations

This contract is the input boundary for later reviewable slices. An adapter must
validate the exact spec and retained digest before mutation, resolve only the
declared secret references, colocate the runner and serving runtime, enforce the
timeouts and cost ceiling, attempt cleanup on every exit, and emit an immutable
outer deployment receipt. It must not change the benchmark command or frozen
evidence schemas.

This v1 contract intentionally includes no cloud SDK, network call, Dockerfile,
Compose file, Kubernetes manifest, cloud provider lifecycle implementation,
runtime image, public API, authentication path, raw evidence publication path,
or acceptance verdict. PR3's local in-memory lifecycle interfaces and mock
adapter are the only execution-side additions; PR4 owns the immutable outer
deployment receipt described in
[`DEPLOYMENT_RECEIPT_V1.md`](DEPLOYMENT_RECEIPT_V1.md).
