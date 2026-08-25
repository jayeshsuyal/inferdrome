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
with all cross-input bindings verified. The file-backed arm store atomically
consumes it across controller processes; the in-memory store is test-only. A
replay, altered input, expired arm, copied/constructed
invalid model, or ceiling substitution fails before the Compute transport is
called.

The execution environment and request contracts require an immutable RFC1035
Compute image name plus a separately observed provider image ID and
Inferdrome digest (never an image family or floating tag), runner, and serving
image identities; an explicit private
VPC/subnetwork; no
external access configuration; no IP forwarding; automatic restart disabled;
boot-disk auto-delete; `TERMINATE` GPU maintenance behavior; bounded disk and
service-account scope references; and local ownership bindings containing the
exact controller, arm, and plan IDs. The pinned A100 profile uses the closed A2
fixed-GPU attachment mode; it does not project an N1-style guest-accelerator
attachment. Provider labels contain only compact
lowercase ownership keys; full IDs remain in the local request/lease and are
compared before deletion. The runner and vLLM serving runtime remain separate
boundaries. A startup-script digest may bind an external bootstrap artifact,
but startup bytes and credential values are never part of this contract; PR8
does not launch either runtime.

## Cost, capacity, and lifecycle

Before a provider mutation, an operator-supplied fixed-point micro-USD quote
must bind the exact request/environment, image, network, disk, service-account,
accelerator, provider lifetime, and a billable duration covering the cleanup
and termination-confirmation tail. It must be fresh and no greater than the
exact hard ceiling. Capacity input is a fresh, read-only observation bound to
the exact request and reports zero matching owned resources; it is not a
capacity proof. Pricing and invoice truth remain unavailable.

The controller writes an immutable no-replace intent anchor and an atomic local
lease before insert, under a cross-process journal lock. Immediately before the
first provider insert it durably records `CREATE_SUBMITTED` with the stable
insert UUID, `provider_mutation_ambiguous=true`, and an `UNKNOWN` operation
status; if that write fails, insert is not called. Every lease transition is
also appended as a bounded, canonical, domain-separated hash-chain event. The
event chain is the authoritative recoverable state; the JSON lease is a derived
snapshot that may lag after a replace/fsync crash, but a tampered snapshot that
is not an event prefix is rejected. The controller then calls
only the injected `GcpComputeTransport`. Insert and delete operation identities
and statuses are persisted. A post-send operation timeout is ambiguous: the
controller must first reconcile the exact zonal operation to terminal before
using GET/list, then deletes only an exactly owned target and requires
GET-not-found plus a zero-result owned-label query. If the operation remains
unknown at the cleanup deadline, the lease is `ORPHANED` and cannot be
confirmed absent. Delete attempts are persisted before each call and the limit
is total across the original process and recovery. A late VM is therefore
eligible for exact cleanup after the operation becomes terminal. Recovery is an
explicit exact-lease operation; there is no broad cleanup.

The additive execution outcome is ephemeral controller state, not a benchmark
result, deployment receipt, GPU receipt, evidence bundle, or acceptance
verdict. It always has `evidence_eligible=false` and unavailable invoice truth.
Cleanup errors dominate its terminal `error_code` while preserving the bounded
primary error separately.

## Transport and authentication boundary

Core/local installs do not import a Google package. The optional `gcp` extra
pins `google-cloud-compute==1.50.0` and its transitive dependencies in
`uv.lock`. `create_google_compute_transport()` is the only factory that imports
the SDK and constructs the `InstancesClient` and restart-safe
`ZoneOperationsClient`; it must be selected only after pure preflight, local
lease reservation, and one-shot arm-consumption gates. With the extra absent it
fails with the bounded
`GCP optional dependency unavailable` error. Engineering-gate tests inject SDK
modules, clients, and extended-operation fakes; they do not discover ADC,
metadata, sockets, subprocesses, or a provider.

The concrete client follows Google's Compute Engine `instances.insert`, `get`,
`list(request=ListInstancesRequest(...))`, and `delete` operation model and
bounded extended-operation waits. Raw provider operation names are normalized
to the exact zonal operation resource before they enter the journal. Polling
transport errors remain pending/unknown; only a provider-confirmed terminal DONE
operation may carry a provider error.
Application Default Credentials are an external operator setup, never an
Inferdrome API key or serialized receipt field. See Google's primary
documentation for [Compute Engine insert](https://cloud.google.com/compute/docs/reference/rest/v1/instances/insert),
[Python Compute client reference](https://cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.instances.InstancesClient),
and [Application Default Credentials](https://cloud.google.com/docs/authentication/provide-credentials-adc).

## Offline command surface

The PR8 script commands are offline `arm`, `verify-arm`, `request-preview`,
`recover-preview`, and `fake-run`:

```text
python scripts/gcp_guarded_lifecycle.py arm ... --confirmation EXECUTE_GCP_ONCE
python scripts/gcp_guarded_lifecycle.py verify-arm ...
python scripts/gcp_guarded_lifecycle.py request-preview ...
python scripts/gcp_guarded_lifecycle.py recover-preview ...
python scripts/gcp_guarded_lifecycle.py fake-run ...
```

There is deliberately no live `execute` command in this slice. `request-preview`
projects the exact private provider request without a transport.
`recover-preview` shows the exact journal target and remaining budget without a
provider call. The controller's `recover_cleanup` API is the explicit recovery
boundary for a separately selected injected/live transport. A future live
command must require the exact arm, plan, deployment spec, inventory, context,
environment, quote, journal, and an explicitly selected concrete transport.
`fake-run` is deterministic and non-provider; it cannot produce customer-
eligible evidence.

The implementation proves the provider-envelope contract and fake cleanup only;
it does not claim runtime bootstrap, vLLM readiness, benchmark serving, a live
canary, capacity, pricing, a GPU result, a deployment receipt, or evidence. No
ADC was resolved, no real provider SDK client was constructed by engineering
gate, no network/provider call was made, no VM/GPU was launched or deleted, and
no money or evidence was produced. The optional SDK compatibility check uses
only installed message types and injected clients.
