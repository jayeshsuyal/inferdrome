# Load-calibrated routing: local rehearsal and live-campaign boundary

This runbook is for the additive `load-calibration` bridge. Inferdrome records
measurements and evidence; it does not decide acceptance, promote a policy, or
authorize infrastructure.

## Safe local rehearsal

The complete local proof uses two temporary CPU loopback OpenAI-compatible
fakes, an injected command seam, and the real native report-to-dashboard read
path. It does not start a container, load Qwen3, access a GPU, or contact a
provider.

```sh
PYTHONPATH=src python -m pytest -q \
  tests/integration/test_load_calibration_rehearsal.py

fixture_root="$(mktemp -d /private/tmp/inferdrome-load-calibration-fixture.XXXXXX)"
PYTHONPATH=src python -m tests.load_calibration_rehearsal_dashboard_support \
  --root "$fixture_root"
```

The fixture command creates a native `SYNTHETIC_ONLY` confirmation report plus
a one-entry digest-pinned dashboard catalog. It closes the fakes before the
browser reads that catalog. It is test support, not a live campaign command.

## Bound local engine lifecycle

The code-level `TwoEngineVllmSubprocessLifecycle` is deliberately explicit:

1. The caller supplies exactly two `http://127.0.0.1:PORT` origins, a safe
   ownership alias, and an existing non-symlink Qwen3 snapshot directory.
2. Before the first start it verifies the already-present snapshot with the
   reviewed no-download Qwen3 manifest/identity boundary. Before each native
   trial it confirms GPU 0 and GPU 1 have no compute processes, then starts two
   new exact-owned local containers using the checked-in
   `vllm/vllm-openai@sha256` identity with `--pull never`.
3. It binds GPU 0/1 and two different loopback ports, then performs bounded
   polling for health, metrics, and a fixed-shape warmup probe before running
   the native trial. The protocol must reserve the lifecycle's declared
   startup/cleanup maximum; otherwise no trial dispatches.
4. It force-removes only immutable container IDs returned by a create or later
   reconciled through that exact name, image, owner label, and attempt label;
   it never removes by name. Cleanup independently confirms both declared
   loopback ports are closed and both GPUs are idle. An absent unknown-ID
   create remains unresolved until an exact later reconciliation. Any failed
   startup, readiness, warmup, or cleanup fails closed.

This lifecycle is not exposed as an unguarded campaign CLI. Calling it for a
real model requires the separately approved campaign path and all inputs below.
The test command above uses a fake command runner, never the concrete runner.

## Live campaign proposal boundary

[`../examples/load-calibrated-routing-live-proposal.template.json`](../examples/load-calibrated-routing-live-proposal.template.json)
is intentionally unparseable by any execution contract: every unresolved value
is an object where an actual typed value would be required. Copying it does not
create an approval, quote, host selection, source identity, or spending right.

Before an authorized live proposal can be formed, a human must provide and
review all of the following exact values:

- source commit, protocol/config/plan/workload/trace/model snapshot identities;
- two serving-image `repository@sha256` values and the model snapshot presence
  and identity proof on the exact host;
- provider project, region/zone, machine/GPU topology, private network path,
  guest/controller principals, and firewall/IAP facts;
- an expiring quote identity/rate/currency, maximum runtime, cleanup horizon,
  and USD cap; and
- an exact user authorization bound to those values, approval timestamps, and
  exact-owned cleanup/recovery identity.

No source change is required to create a *draft* from fully supplied values.
Quote, capacity, image availability, host/network/principal facts, and any
live execution remain external preparation and require separate authorization.

## Limitations

- A local loopback proof is not a Qwen3 download, two-A100 result, distributed
  failure-independence claim, capacity finding, or provider verification.
- The lifecycle's `UNVERIFIED` runtime identity is not runtime attestation.
- The session limit reserves cleanup/retrieval time but cannot make filesystem
  scheduling or a remote server hard real-time.
- Raw prompts, completion bodies, endpoint origins, credentials, provider
  payloads, and invoice claims are not stored in rehearsal sidecars.

For the separately authorized, bounded manual-host command that owns an
external cutoff, explicit local-Docker target, durable SGLang reset sidecars,
and bounded retrieval export, see
[the host operator runbook](LOAD_CALIBRATED_ROUTING_HOST_OPERATOR.md). That
path remains local-only and does not create or terminate a provider resource.
