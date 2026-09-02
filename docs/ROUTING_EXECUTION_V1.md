# Routing execution v1 (PR B)

`routing-execution-v1` is the bounded real-endpoint bridge for Inferdrome
v0.2. It takes the fixed R1 six-request, stale-load/fresh-health experiment
semantics and runs them against exactly two OpenAI-compatible endpoints. It
produces a sealed evidence package. It is not a production router, a provider
control plane, or a verdict/promotion mechanism.

## One-command local demonstration

From a private, owner-controlled checkout directory, run:

```sh
python -m inferdrome.routing_execution demo --output routing-execution-package
```

The command starts two actual ephemeral `127.0.0.1` HTTP servers, runs the
same bridge used by `run`, seals `routing-execution-package`, and prints only
the package path and retained digest. It never starts Docker, contacts a cloud
provider, reads ADC, or uses credentials.

Verify or inspect the evidence later without creating a transport:

```sh
python -m inferdrome.routing_execution verify routing-execution-package
python -m inferdrome.routing_execution inspect routing-execution-package
```

For a separately supplied deployment configuration and the exact fixed first
six Qwen3 workload rows, the production-shaped command is:

```sh
python -m inferdrome.routing_execution run \
  --deployment-config deployment-config.json \
  --workload selected-qwen3-workload.jsonl \
  --output routing-execution-package
```

The configuration is canonical JSON and binds the source commit, immutable
runner and serving image `repository@sha256` identities, Qwen3-8B model and
tokenizer revision, vLLM/adapter identity, frozen R1 plan/trace/fault/trial
digests, full Qwen workload digest plus the fixed selected-six digest, policy
order, endpoint identities, telemetry contract, request denominator, topology
facts, and a destination digest. The declared transfer digest is recomputed
from a canonical config projection plus the selected workload digest before an
output reservation or transport is created. Endpoint origins are input-only
and are never written to a package.

## Architecture

```text
canonical config + six prompt rows
             |
             v
  pure topology admission + input-transfer verification (no network)
             |
             +-- reject public/SSH/duplicate/unpinned/one-A100-two-engine
             |
             v
  fresh transport per policy trial
      |              |                |
      |              |                +-- OpenAI /v1/chat/completions once
      |              +-- vLLM /metrics load observer
      +-- independent /health observer
             |
             v
  R1 policy projection -> decision -> exactly one terminal receipt
             |
             v
  create/no-replace, redacted producer evidence package
             |
             v
  offline verifier + pure receipt replay (no executor or transport imports)
```

The topology admission call happens before the transport factory is called.
It requires exactly `endpoint-a` and `endpoint-b`, canonical distinct origins,
identical declared model/runtime/workload facts, immutable images, health and
vLLM metrics capability, and explicit GPU/DCGM and KV/cache capability states.
Local mode accepts only literal `127.0.0.1` origins with distinct explicit
ports. GCP mode accepts only literal RFC1918 targets; `.internal` DNS is
deliberately deferred until a resolver-and-connection-pinning boundary exists.
It does not perform DNS or any provider action.

The current PR-A profile is one `NVIDIA A100-SXM4-40GB` with
`accelerator_count=1`. Because this contract names two independent serving
engines, that configuration is rejected before transport construction. PR B
does not choose an alternative topology; a future real campaign needs the
separately approved provider/topology decision.

## Campaign behavior

Each named R1 policy gets one fresh runner connection and telemetry state:

- `fail_closed_required_load_v1` emits `NO_SAFE_ROUTE` when load becomes stale.
- `explicit_fail_open_stale_load_v1` permits explicitly stale load and selects
  `endpoint-b`.
- `typed_admissible_state_only_v1` discards stale load and uses a deterministic
  fresh-health tie break to select `endpoint-a`.

At the third request, the injected fault pauses the load observer only.
Health continues to receive new observer epochs, so the resulting receipts
show stale load alongside fresh health. A malformed or missing load metric is
typed `UNAVAILABLE`, not relabeled as stale, and all policies fail closed in
that case. PR B does not claim a separately bound GPU/DCGM collector, so its
GPU/DCGM capability is explicitly and only `UNAVAILABLE`. KV/cache is also
explicitly `UNAVAILABLE` and is never invented.

The pinned vLLM wire boundary is deliberately small: `/health` is healthy on
an exact HTTP 200, including vLLM's empty response body; the bounded body is
still digested when present. `/metrics` supplies exactly one finite,
non-negative integral `vllm:num_requests_running` sample labelled
`model_name="Qwen/Qwen3-8B"`. Unrelated repeated histogram buckets are outside
that load contract, while a missing, malformed, wrong-model, or duplicate
target gauge is `UNAVAILABLE` without aggregation. A successful completion
also requires a non-empty assistant chat-completion choice with non-empty text;
raw response content is never retained.

Every planned request has one decision and one terminal outcome. A selected
request has exactly one transport attempt unless cancellation is observed
before dispatch, in which case it records zero attempts and no request digest.
There is no retry or failover retry. A timeout, cancellation, malformed/non-2xx
response, and no-safe-route each receive a bounded terminal state. The package
records full terminal populations instead of issuing a conclusion about them.

## Evidence package and verification

The closed package inventory is:

```text
input-transfer-receipt.json
executed-manifest.json
producer-receipt.json
integrity-manifest.json
```

Input transfer verifies bounded regular configuration and workload files via
held, no-follow parent descriptors before any transport exists. Evidence
publication reserves an owner-controlled destination before transport, writes
a private stage through held descriptors, makes it read-only, atomically
publishes with descriptor-anchored no replacement, and re-verifies it. The
outer integrity manifest hashes all non-manifest files.

The independent verifier scans the exact inventory through held descriptors
with link, hard-link, size, mode, canonical-JSON, digest, cross-binding,
decision-to-terminal, full-denominator, redaction, and final identity checks.
It also replays every named policy decision from the sealed candidate state and
checks the reset, fault, epoch, freshness, and terminal semantics. It reads
only the sealed package and cannot construct an HTTP transport.

Packages deliberately omit endpoint origins, authorization headers, raw
prompts, model outputs, provider payloads, instance identifiers, raw logs,
and invoice data. They retain only logical endpoint IDs, digests, bounded
status classes, metric values, epochs, monotonic timestamps, ages, and reasons.

## Threat boundary and limitations

PR B proves local socket-level behavior and configuration/admission behavior;
it does not prove a cloud campaign, GPU/DCGM availability, Docker daemon
execution, registry/bucket transfer, GKE, billing, production availability, or
physical server-cache reset. Its reset receipt states the precise scope:
runner connection and telemetry state are cold per policy, while a separately
operated serving engine reset is `NOT_ASSERTED_SEPARATE_SERVING_ENGINE`.

This boundary is intentional. The runner is separate from the serving engines
and does not obtain SSH, provider, or credential authority. A separately
approved GPU campaign is required before any real-cloud evidence claim.

## Explicit non-goals

PR B does not start Docker, create or mutate GCP resources, read ADC or other
credentials, run a GPU, transfer to a registry or bucket, add Kubernetes,
alter the R2 dashboard projection, or operate a routing data plane. It does
not issue PASS/FAIL/NOT_PROVEN, select a winner, or control promotion.

## Interview guide

In 60 seconds: Inferdrome checks two endpoints before opening a socket,
samples health separately from vLLM load, deliberately stops only load, applies
three fixed R1 policy meanings, records one decision and one terminal per
request, then seals a redacted package that an offline reader can verify.

For a five-minute tour, follow
`routing_execution/contracts.py` (what is bound), `topology.py` (what may run),
`telemetry.py` and `policy.py` (what is observed and selected), `executor.py`
(one-attempt execution), and `package.py`/`replay.py`/`verifier.py` (what can
be trusted offline). The local socket harness is in `loopback.py`.

## Acceptance commands

```sh
python scripts/generate_routing_execution_contracts.py --check
python -m pytest tests/unit/test_routing_execution_schema.py \
  tests/unit/test_routing_execution_admission.py \
  tests/unit/test_routing_execution_executor.py \
  tests/integration/test_routing_execution_loopback.py \
  tests/integration/test_routing_execution_cli.py \
  tests/adversarial/test_routing_execution_security.py
./scripts/engineering_gate.sh
./scripts/deployment_qualification_gate.sh
./scripts/dashboard_gate.sh
```
