# Stale-telemetry qualification v1

`stale-telemetry-qualification-v1` is Inferdrome's first small, falsifiable
routing-state experiment. It asks one specific question: what happens when
independent health remains fresh while load telemetry becomes stale? It is a
local, deterministic evidence exercise, not a production routing algorithm,
a faithful reproduction of any paper, a benchmark result, or a policy verdict.

The implementation reuses the sealed `routing-campaign-v1` source package
rather than creating a second simulator or copying raw receipts. The source
package is the sole owner of request-level observations, decisions, outcomes,
the fault schedule, and reset receipts. The additive qualification descriptor
binds the exact package digest, source-input digests, receipt digests, counts,
separate terminal populations, and the specific fault facts.

```text
fixed inputs -> virtual-time R1 engine -> sealed request-level package
                                              |  independent replay
                                              v
                                      immutable qualification descriptor
                                              |  retained descriptor digest
                                              v
                                      offline source-and-overlay verification
```

## Declared experiment

The contract admits only one local synthetic vector:

| Fact | Fixed value |
| --- | --- |
| Endpoints | `endpoint-a`, `endpoint-b` deterministic in-process mocks |
| Clock | virtual time at 0, 10, 20, 30, 40, and 50 ms |
| Fault | pause load collection at 15 ms; independent health continues |
| Freshness bound | 5 ms for health and load |
| Falsifying observation | at 20 ms, health age is 0 ms/admissible and load age is 10 ms/inadmissible at both endpoints |
| Policies | `fail_closed_required_load_v1`, `explicit_fail_open_stale_load_v1`, `typed_admissible_state_only_v1` |
| Repetitions | one cold reset trial (`repetition_index: 0`) per policy |
| Denominator | six requests per trial; 18 terminal receipts total, retained as three separate populations |
| Retry behavior | `NO_RETRY` |

The policy names are declared treatment labels, not an evaluation or a winner.
The descriptor does not aggregate terminal populations across policies: each
trial retains all five terminal status keys and must total exactly six.

At the stale request, the fixed source package records these three observable
outcomes:

| Declared policy | Selection/fallback | Terminal result |
| --- | --- | --- |
| `fail_closed_required_load_v1` | no endpoint / `REQUIRED_LOAD_STALE` | `NO_SAFE_ROUTE` |
| `explicit_fail_open_stale_load_v1` | `endpoint-b` / `STALE_LOAD_FAIL_OPEN` | `TIMED_OUT` |
| `typed_admissible_state_only_v1` | `endpoint-a` / `HEALTH_ONLY_TIE_BREAK` | `SUCCEEDED` |

These are deterministic properties of the declared mock scenario. They do not
establish causal or statistical superiority outside it.

## One-command local execution

No model is downloaded or run. No endpoint, GPU, container, cloud, provider,
SSH, credential, or network operation is performed.

```sh
python -m inferdrome.routing_qualification run \
  --campaign-plan campaigns/routing-campaign-v1/stale-load-fresh-health.plan.json \
  --request-trace campaigns/routing-campaign-v1/stale-load-fresh-health.trace.jsonl \
  --fault-schedule campaigns/routing-campaign-v1/stale-load-fresh-health.fault-schedule.json \
  --trial-plan campaigns/routing-campaign-v1/trial-plan.json \
  --campaign-output /private/tmp/routing-campaign-v1 \
  --qualification-output-root /private/tmp/stale-telemetry-qualification
```

The command prints the retained source-package digest and the retained
qualification-descriptor digest. Keep both values with the local evidence.
The source package is sealed with its own closed inventory and replay verifier.
The descriptor is published as the read-only, no-replace file:

```text
/private/tmp/stale-telemetry-qualification/
  stale-telemetry-qualification-v1/qualification.json
```

The descriptor root must be separate from the source package; adding it to the
source package would correctly make the R1 inventory verifier reject that
package.

Publication walks every output-path component with no-follow directory
descriptors. It may create a fresh `0700` root beneath a system temporary
ancestor, but treats that ancestor as untrusted; all staging and the atomic
no-replace publication remain anchored to the held private root. Readback
reopens and compares the visible root and artifact identities, so a symlink or
replacement race fails closed rather than redirecting evidence.

Reverify the pair with the qualification digest printed above:

```sh
python -m inferdrome.routing_qualification verify \
  --campaign-package /private/tmp/routing-campaign-v1 \
  --qualification-root /private/tmp/stale-telemetry-qualification \
  --expected-qualification-digest 'sha256:...'
```

Verification safe-reads the immutable descriptor, checks canonical bytes and
the externally retained descriptor digest, then independently verifies and
replays the bound R1 source package. Missing, noncanonical, oversized,
symlinked, hard-linked, FIFO, replaced, tampered, or source-mismatched inputs
fail closed.

## Threat boundary and limitations

- The contracts prevent the local descriptor from silently becoming a different
  fault, policy set, trace, denominator, repetition plan, or pooled result.
- The descriptor digest detects later changes only when the verifier receives
  the expected digest retained from capture; Inferdrome does not provide an
  external timestamping or key-management service.
- The simulated endpoint outcomes and virtual clock are not real inference
  runtime observations. Qwen3-8B remains only a later pinned experimental
  identity; this qualification does not download, execute, or observe it.
- Inferdrome records and explains evidence. It does not issue `PASS`, `FAIL`,
  `NOT_PROVEN`, a promotion decision, or an operational recommendation.

## 60-second explanation

“We run two deterministic endpoints on a virtual clock. At 15 ms we stop only
the load observer; health keeps reporting. Every trial starts cold and receives
the same six requests. The sealed source package retains every observation,
decision, and terminal receipt. A separate immutable qualification descriptor
then proves which exact package, fault, three policy labels, and separate
terminal populations we inspected. Offline verification replays the source
package before trusting the descriptor. It is a bounded measurement exercise,
not a live router or a claim that one policy wins generally.”
