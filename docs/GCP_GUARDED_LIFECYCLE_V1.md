# GCP guarded lifecycle v1

PR8 adds the execution boundary for a future, explicitly authorized GCP Compute
canary. It does not run one. The PR7 `gcp-dry-run-plan.v1` remains
`execution_authorized=false`; the plan is verified input, never authority.

## Contract

`inferdrome.gcp-execution-arm.v1` is a one-shot operator capability, not a
signature and not a Google/provider attestation. Its canonical payload binds:

- the plan ID and ordinary SHA-256 of the exact canonical plan bytes;
- deployment-spec and offline-inventory digests, planning scope, and the exact
  selected project/region/zone/machine/GPU tuple;
- one bounded controller ID and nonce, UTC issuance/expiry, and the exact
  controller/provider maximum lifetime; and
- the unchanged hard USD ceiling.

The arm is accepted only from canonical bytes, at the expected clock instant,
with all cross-input bindings verified. An injected, thread-safe arm store
atomically consumes it. A replay, altered input, expired arm, copied/constructed
invalid model, or ceiling substitution fails before the Compute transport is
called.

The execution environment and request contracts require an immutable numeric
Compute image resource identity (never an image family or floating tag) plus its
Inferdrome digest,
runner, and serving image identities; an explicit private VPC/subnetwork; no
external access configuration; no IP forwarding; automatic restart disabled;
boot-disk auto-delete; `TERMINATE` GPU maintenance behavior; bounded disk and
service-account scope references; and ownership labels containing the exact
controller, arm, and plan IDs. The runner and vLLM serving runtime remain
separate boundaries. Startup script bytes and credential values are never part
of the contract.

## Cost, capacity, and lifecycle

Before a provider mutation, an operator-supplied fixed-point micro-USD quote
must cover compute, GPU, boot disk, and network for the maximum lifetime plus a
declared safety margin. It must be fresh and no greater than the exact hard
ceiling. Capacity input must identify the exact selection, claim offline
catalog eligibility, and report zero matching active resources; the controller
still records `capacity_proven=false`. Pricing and invoice truth remain
unavailable.

The controller writes an atomic local lease before insert, then calls only the
injected `GcpComputeTransport`. It waits boundedly for the extended insert
operation. An operation timeout is ambiguous: the controller reconciles only
the exact instance and ownership labels, deletes only an exactly owned target,
and requires GET-not-found plus a zero-result owned-label query. Failure to
confirm absence creates an `ORPHANED` lease and blocks the next run. Recovery is
an explicit exact-lease operation; there is no broad cleanup.

The additive execution outcome is ephemeral controller state, not a benchmark
result, deployment receipt, GPU receipt, evidence bundle, or acceptance
verdict. It always has `evidence_eligible=false` and unavailable invoice truth.
Cleanup errors dominate its terminal `error_code` while preserving the bounded
primary error separately.

## Transport and authentication boundary

Core/local installs do not import a Google package. The optional `gcp` extra
pins `google-cloud-compute==1.50.0` and its transitive dependencies in
`uv.lock`. `create_google_compute_transport()` is the only factory that imports
the SDK and constructs its client; it must be selected only after the pure
preflight and local lease gates. With the extra absent it fails with the bounded
`GCP optional dependency unavailable` error. Engineering-gate tests inject SDK
modules, clients, and extended-operation fakes; they do not discover ADC,
metadata, sockets, subprocesses, or a provider.

The eventual concrete client follows Google's Compute Engine `instances.insert`,
`get`, list, and `delete` operation model and bounded extended-operation waits.
Application Default Credentials are an external operator setup, never an
Inferdrome API key or serialized receipt field. See Google's primary
documentation for [Compute Engine insert](https://cloud.google.com/compute/docs/reference/rest/v1/instances/insert),
[Python Compute client reference](https://cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.instances.InstancesClient),
and [Application Default Credentials](https://cloud.google.com/docs/authentication/provide-credentials-adc).

## Offline command surface

The only PR8 script commands are offline `arm`, `verify-arm`, and `fake-run`:

```text
python scripts/gcp_guarded_lifecycle.py arm ... --confirmation EXECUTE_GCP_ONCE
python scripts/gcp_guarded_lifecycle.py verify-arm ...
python scripts/gcp_guarded_lifecycle.py fake-run ...
```

There is deliberately no live `execute` command in this slice. A future live
command must require the exact arm, plan, deployment spec, inventory, context,
environment, quote, journal, and an explicitly selected concrete transport.
`fake-run` is deterministic and non-provider; it cannot produce customer-
eligible evidence.

The implementation proves contract behavior and fake cleanup only. No ADC was
resolved, no Google SDK client was constructed by tests or engineering gate,
no network/provider call was made, no VM/GPU was launched or deleted, and no
money, GPU result, receipt, or evidence was produced.
