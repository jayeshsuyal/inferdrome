# Inferdrome v0.2 capability and limitations contract

Status: **Released v0.2 baseline; retained historical claim boundary**

Inferdrome v0.2 is an evidence plane and qualification harness for changes to
open-weight inference systems. It binds configuration to observation identity
and freshness, routing decisions, selected endpoints, request outcomes, and a
tamper-evident evidence package. It measures and explains those records; it
does not operate a production router, decide promotion, or issue an acceptance
verdict. Measurements are sensors that feed the evidence chain, not a model
leaderboard or standalone product score.

The retained executable contract is `inferdrome.v0_2_capabilities.v1`. The
active `python -m inferdrome capabilities` command reports the current v0.3
development boundary instead; this document preserves the v0.2 release basis.
Inspect the retained exact machine-readable form with:

```bash
python -c 'from inferdrome.v0_2_capabilities import V0_2_CAPABILITY_CONTRACT; print(V0_2_CAPABILITY_CONTRACT.model_dump_json())'
```

## What is established, and what is not

| Capability | Status | Bounded fact | Limitation |
| --- | --- | --- | --- |
| Local routing execution | `PROVEN_LOCAL_SOCKET_LEVEL` | The two-loopback-endpoint bridge seals and offline-replays request-level routing evidence. | This is not GPU, Docker, cloud, or production-routing operation. |
| Historical A10 serving evidence | `PRESERVED_EXTERNAL_ONLY` | The historical Qwen3-8B A10 handoff remains preserved. | Its raw archive is `EXTERNAL_ONLY`; it is not a new v0.2 campaign or an acceptance decision. |
| Two-A100 multi-endpoint campaign | `UNEXECUTED` | A guarded one-host/two-engine pre-campaign shape is local/fake validated. | No same-host or distributed two-A100 run has produced real evidence. |
| GCP operation | `LOCAL_FAKE_VALIDATED` | Offline proposal/preview and constrained cleanup diagnostics are covered locally with fakes; the create-capable `execute` command fails closed pending a reviewed two-A100 watchdog. | No provider operation, billing result, GPU campaign, real-cloud evidence, or live launch authority is claimed. |
| Kubernetes operation | `NOT_CLAIMED` | Static and local-synthetic boundaries are inspectable. | No cluster, GPU workload, or production operation is claimed. |

The contract never has authority for `PRODUCTION_ROUTING`, `POLICY_VERDICT`,
`PROMOTION_CONTROL`, or `ACCEPTANCE_VERDICT`. ExitSpec remains an independent
consumer and is not changed by this repository.

## Local routing-evidence demo

```text
fixed configuration and trace
             ↓
two deterministic local endpoint mocks + virtual time
             ↓
health/load/KV observations with epochs, ages, and admissibility
             ↓
route decision → selected endpoint or no-safe-route → terminal outcome
             ↓
sealed package → independent replay verification → read-only dashboard
```

Run the complete local walkthrough with:

```bash
./scripts/run_local_demo.py
```

It retains one fixed synthetic `routing-campaign-v1` package beside the
existing synthetic comparison workspace. Each invocation verifies the retained
digest before reusing it; a missing, replaced, or tampered package stops the
demo rather than being regenerated. The dashboard's **Routing campaigns** view
then shows each cold reset, the stale-load/fresh-health fault, telemetry
age/admissibility, candidates, selection or fallback reason, terminal outcome,
and the full terminal population for every policy trial.

This dashboard package is a deterministic CPU/virtual-time explanation of the
experiment. The separately tested `python -m inferdrome.routing_execution
demo` exercises the bounded bridge through two actual loopback HTTP endpoints;
it also remains local-only and is not a live GPU or cloud result.

## 60-second explanation

“Inferdrome is the evidence plane around an inference experiment. It records
what was configured, what each sensor observed and when, how those observations
were admitted into a routing decision, which endpoint was selected, and what
happened to every request. It seals those records and independently replays
them before the dashboard renders anything. The local demo proves that evidence
path with two deterministic endpoints. It does not call a winner, operate
production routing, or claim an unexecuted cloud or GPU campaign.”

## Threat boundary and non-goals

The local demo is loopback-only and has no provider, ADC, credential, SSH,
Docker daemon, GPU, registry, bucket, or spend path. It never renders raw
prompts, model outputs, endpoint origins, or unverified claimed receipts.
The immutable v0.1 schemas, evidence history, review records, and
`EXTERNAL_ONLY` archives remain frozen. A live campaign needs both a separately
authorized, exact commit-bound approval and a separately reviewed private
two-A100 durable watchdog bridge; the v0.2 `execute` command intentionally
fails closed until then. This contract neither requests nor grants either.
