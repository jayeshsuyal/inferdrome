# Routing campaign v1

Status: **Implemented for PR R1**

routing-campaign-v1 is a sealed deterministic synthetic-CPU experiment. It records three explicit policy implementations when load telemetry becomes stale while independent endpoint health stays fresh. Inferdrome is a measurement and evidence producer here: it does not emit a PASS, FAIL, or NOT_PROVEN verdict.

## One-minute explanation

Two fresh in-process mock endpoints run on a virtual millisecond clock. Six fixed requests arrive at 0, 10, 20, 30, 40, and 50 ms. Endpoint B initially has lower observed load, then load updates pause at 15 ms. B becomes saturated at 20 ms, but health keeps reporting HEALTHY at five-millisecond intervals. Each policy starts with a cold reset and records every observation, route decision, and exactly one terminal outcome. The completed package can be replayed by an independent offline verifier.

## Frozen input contract

| Artifact | Contract |
| --- | --- |
| campaigns/routing-campaign-v1/stale-load-fresh-health.plan.json | inferdrome.routing-campaign-plan.v1; two endpoints, virtual request times, observers, saturation point, policies, and no-retry setting. |
| campaigns/routing-campaign-v1/stale-load-fresh-health.trace.jsonl | Six contiguous JSONL request rows, each with request_id, sequence_index, and decision_time_ms. |
| campaigns/routing-campaign-v1/stale-load-fresh-health.fault-schedule.json | inferdrome.routing-fault-schedule.v1; pause load at 15 ms while health continues. |
| campaigns/routing-campaign-v1/trial-plan.json | inferdrome.routing-trial-plan.v1; one cold-reset trial per policy. |

The plan names exactly endpoint-a and endpoint-b; both are in-process mocks. The virtual trace is exactly 0, 10, 20, 30, 40, and 50 ms. Health has an observer interval and freshness bound of 5 ms. The machine-readable load_observer.update_times_ms value is exactly [0, 10]: both endpoints update at those times only, with A=4 and B=1 and a 5 ms freshness bound, then the observer pauses at 15 ms. Endpoint B is saturated at 20 ms. No warmups, retries, substitute requests, or pooled trial populations are allowed.

The three named policies are:

- fail_closed_required_load_v1: stale required load produces a null selection and NO_SAFE_ROUTE.
- explicit_fail_open_stale_load_v1: fresh health is required, stale load is deliberately allowed and recorded.
- typed_admissible_state_only_v1: stale load and unsupported KV state are inadmissible; a declared static endpoint-A tie break is used only after fresh-health admission.

Every trial has fresh mock endpoint instances and observer epochs, zero virtual time, and cleared queue/load/KV state before request zero. The reset is sealed as a receipt. Per request, the package binds candidate observations and their age/epoch/admissibility to one decision and one terminal receipt. The verifier requires a bijection among the trace, decisions, and terminal receipts.

## Expected fixture vector

| Policy | Requests 0–1 | Requests 2–5 | Terminal population |
| --- | --- | --- | --- |
| fail_closed_required_load_v1 | B / SUCCEEDED | null / NO_SAFE_ROUTE | 2 succeeded, 4 no-safe-route |
| explicit_fail_open_stale_load_v1 | B / SUCCEEDED | B / TIMED_OUT | 2 succeeded, 4 timed out |
| typed_admissible_state_only_v1 | B / SUCCEEDED | A / SUCCEEDED | 6 succeeded |

This is a falsifiable fixture vector only. It is not an SLO, availability, causal, statistical, or policy-quality assertion.

## Architecture and acceptance

The routing namespace owns the virtual clock, mock endpoints, policy implementations, package writer, and offline verifier. The verifier replays frozen inputs and requires a one-to-one correspondence among planned trace requests, route-decision receipts, and terminal-outcome receipts. It rejects holes, duplicates, substitutions, implicit retries, stale state used by the typed policy, reset leakage, altered input inventory, and altered sealed content.

After the namespace is installed, run:

    PYTHONPATH=src python -m inferdrome.routing_campaign run \
      --campaign-plan campaigns/routing-campaign-v1/stale-load-fresh-health.plan.json \
      --request-trace campaigns/routing-campaign-v1/stale-load-fresh-health.trace.jsonl \
      --fault-schedule campaigns/routing-campaign-v1/stale-load-fresh-health.fault-schedule.json \
      --trial-plan campaigns/routing-campaign-v1/trial-plan.json \
      --output /private/tmp/routing-campaign-v1
    PYTHONPATH=src python -m inferdrome.routing_campaign verify \
      /private/tmp/routing-campaign-v1
    PYTHONPATH=src pytest -q tests/unit/test_routing_campaign_*.py \
      tests/integration/test_routing_campaign_*.py \
      tests/adversarial/test_routing_campaign_*.py
    INFERDROME_PYTHON=.venv/bin/python ./scripts/engineering_gate.sh

The run output must be a new empty location. The routing package is a new, routing-specific sealed artifact family; it does not alter the frozen v0.1 evidence bundle model.

## Threat boundary, non-goals, and limitations

The verifier is offline and read-only. It rejects changed inputs or sealed bytes, duplicate or missing receipts, trial-state leakage, implicit retries, and stale state used by the typed policy. It cannot attest to a real endpoint, runtime, cloud provider, or hardware.

This slice has no sockets, subprocesses, providers, credentials, GPUs, containers, SSH, Docker, GCP, Kubernetes, dashboard, or production traffic. It is not a faithful Preble reproduction, novel routing algorithm, production router, real vLLM/GPU experiment, or policy recommendation. It makes no production-safety, performance, availability, causal, statistical, or acceptance claim. Real endpoint/GPU evidence is deferred to the later PR B and separately authorized campaign.
