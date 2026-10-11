# Breakpoint: first Qwen3-8B study plan

This is **preparation for a future measurement study**. Spending is undecided;
the spend cap is `null`. No GPU rental, model serving, traffic collection or
performance finding is authorized or produced by these commands. A prepared
plan records the intended experiment and remains ineligible for customer
acceptance.

The question is narrow: at a fixed offered load, can a bounded change to arrival
timing reverse the ordering of `cache_only` and `least_busy`, and does the
selected timing rule meet the same criteria on fresh workload seeds?

## Frozen method

| Item | Initial design |
| --- | --- |
| Model | Qwen/Qwen3-8B, revision `b968826d9c46dd6066d109eabc6255188de91218`. |
| Serving topology | Two independent BF16, TP=1 vLLM 0.26.0 replicas; one per matched A100 40 GB GPU; prefix caching enabled; context 8,192; GPU memory utilization 0.90. |
| Workload | Capacity `control.v1`: 48 distinct document prefixes near 4,096 tokens; 128 output tokens; 60% document cycle and 40% eight-document hotspot shifting A→B→A. |
| Trial | 720 offers over 120 seconds: 6 offered requests/s, including all offers in accounting. |
| SLOs | First content ≤500 ms and completion ≤5 s, measured from scheduled arrival. |
| Policies | A = `cache_only`; B = `least_busy`. |
| Replication | Eight discovery blocks and eight held-out blocks; four matched trials per block. |
| Timing grid | Group size 8, retained spacing 2,500 basis points, maximum advances 250 ms and 1,000 ms. |
| Practical margin | 100,000 microrequests/s = 0.1 SLO-good request/s. |
| Client limits | Concurrency 256; scheduling-lag p95 and client-queue-delay p95 each ≤10 ms. |
| Router limits | Active 128, queue 256, explicitly supplied for every fresh router. |

Every four-trial block compares both policies under the original schedule and
under the same candidate timing. Timing transforms retain every prompt, token
budget, original epoch, SLO, offer and the full 120-second denominator. The
candidate can change runtime cache and queue state; preserving that state is
not part of the claim. The grid applies to `control.v1`, not the separate
`burst-hot-shift.v1` workload.
The descriptors control planned offers, not observed router-arrival spacing.
The p95 client limits do not bound every request and have not yet been shown
feasible on the intended host.

Discovery seeds are **1009, 1013, 1019, 1021, 1031, 1033, 1039, 1049**. Held-out
seeds are **2003, 2011, 2017, 2027, 2029, 2039, 2053, 2063**. The planned order
seeds are 104729 for discovery/reduction and 130363 for confirmation. The
four-cell Williams design balances positions and immediate predecessor cells.
The held-out seed list is fixed before discovery. Publishing that list does not
prove that its outcomes were unseen or that the blocks were executed
independently.

The historical [capacity study](VLLM_ROUTER_RESULTS.md#separate-capacity-sweep)
motivates the 6 req/s workload and baseline direction: it observed higher
descriptive SLO-goodput for `cache_only` than `least_busy` on four blocks. Its
7.2% difference belongs to that earlier study. It supplies no observations to
this study's discovery or confirmation. There is no new calibration result or
guarantee that the historical ordering will recur on another host.

## Criteria, budgets and stopping rules

The existing [paired criteria](VLLM_PAIRED_COMPARISON.md) remain fixed:

1. Under baseline timing, A exceeds B beyond the practical margin.
2. Under candidate timing, B exceeds A beyond that same margin.

Each direction needs the exact one-sided binomial tail ≤`1/40` and an observed
mean beyond the margin. At 120 seconds, 0.1 req/s equals 12 net SLO-good requests;
strictly exceeding it requires **at least 13** in the declared direction.
Eight blocks require **8/8 margin exceedances in each direction**: 7/8 gives
`9/256`, which exceeds `1/40`. This small design is stringent and may not find a
candidate. The inference concerns directional blockwise margin exceedance,
with independent blocks and the stated null model assumed; it does not establish
population-mean significance.

| Stage | Maximum comparisons | Trial slots | Planned offers | Scheduled traffic |
| --- | ---: | ---: | ---: | ---: |
| Search | 2 | 64 | 46,080 | 128 min |
| Reduction | 2 | 64 | 46,080 | 128 min |
| Held-out confirmation | 1 | 32 | 23,040 | 64 min |
| Total | 5 | **160** | **115,200** | **320 min** |

Search stops at the first screened-in candidate or its finite grid/budget end.
Without a candidate, reduction and confirmation do not proceed. Reduction
permits at most two new comparisons and may end at `BUDGET_EXHAUSTED`; its
incumbent remains usable without a minimality claim. Each tested mask requires
fresh four-cell evidence. A terminal incumbent is then frozen for one full
held-out batch. Do not change rates, seeds, directions, margins, timing rules,
sample size or eligibility limits after inspecting outcomes.

Every supplied attempt stays in the record, including failed or ineligible
attempts. Do not replace unfavorable blocks, retry until a pass, or count
individual requests as independent replications. Missing trials and failed
eligibility checks cannot support a complete comparison. Malformed artifacts,
token/accounting failures or an unverified reset stop collection for inspection;
they are not evidence of a policy loss. Existing protocol and budget checks do
not implement a global retry registry or enforce a provider's bill.

The time worksheet reserves the following **planning allowances**:

- Scheduled traffic: 160 × 120 s = 19,200 s.
- Drain allowance: 160 × 60 s = 9,600 s.
- Warmup, reset and fresh-router preparation: 160 × 60 s = 9,600 s.
- Session overhead: 3,600 s, including a 600-second teardown reserve.

That totals **42,000 s (11 h 40 min)** within the declared **43,200 s (12 h)**
ceiling. These are neither observed durations nor enforced runtime limits. The
existing primitives can spend longer on setup/reset, and model transfer,
verification or export may exceed an allowance. Scheduled traffic alone is
5 h 20 min at the full budget; it is not a whole-session estimate. A future
execution procedure must enforce the approved wall-clock/spending envelope and
reserve teardown time. No current GPU price or dollar cap is assumed.

## Offline preparation and source pins

The intended collection implementation is pinned to reviewed source
[`14df96b93ff1c476483de51d4c4e1fa9d6f10d34`](https://github.com/jayeshsuyal/inferdrome/commit/14df96b93ff1c476483de51d4c4e1fa9d6f10d34).
The intended image remains
`vastai/vllm@sha256:39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77`.
Environment and reset descriptors record intended conditions, not observations
of an available machine. Their hashes and the source revision do not
authenticate execution or prove that the declared implementation was used.

Planning requires no GPU or tokenizer. Preparation requires already available
local tokenizer files from the pinned model revision and `tokenizers==0.22.1`.
The verifier checks these exact SHA-256 values:

| File | SHA-256 |
| --- | --- |
| `tokenizer.json` | `aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4` |
| `tokenizer_config.json` | `d5d09f07b48c3086c508b30d1c9114bd1189145b74e982a265350c923acd8101` |

The checked-in [study configuration](../examples/breakpoint-qwen3-study.json)
uses schema `inferdrome.breakpoint-study-config.v1`,
`execution_authorized=false`, and spending decision `UNDECIDED` with a null cap.
From the checkout, create a new plan file in an existing private directory,
then prepare with the verified local tokenizer:

```bash
inferdrome breakpoint study plan \
  --config examples/breakpoint-qwen3-study.json \
  --output /private/study/plan.json

inferdrome breakpoint study prepare \
  --plan /private/study/plan.json \
  --tokenizer-root /private/preflight/qwen3-tokenizer \
  --output-dir /private/study/prepared

inferdrome breakpoint study verify \
  --prepared-dir /private/study/prepared \
  --tokenizer-root /private/preflight/qwen3-tokenizer
```

The planning artifact has schema `inferdrome.breakpoint-study-plan.v1` and
status `PLANNED_NOT_EXECUTED`. A successful preparation inventory has schema
`inferdrome.breakpoint-study-inventory.v1` and status `PREPARED_NOT_EXECUTED`.
Neither status authorizes collection or supplies a measurement.

Preparation creates an immutable bundle:

| Paths under `prepared/` | Contents |
| --- | --- |
| `plan.json`, `environment.json`, `reset-procedure.json` | Frozen recipe and intended declarations. |
| `plans/discovery-bNN-plan.json`, `plans/heldout-bNN-plan.json` | Eight discovery and eight held-out evaluation workload plans. |
| `plans/discovery-bNN-certificate.json`, `plans/heldout-bNN-certificate.json` | Matching prompt-token certificates. |
| `search-plan.json`, `search-inputs.empty.json`, `search-report.initial.json` | Bounded search with no supplied results, initially awaiting evidence. |
| `initial/protocol.json`, `initial/timings/` | The first candidate's complete order and timing descriptors. |
| `initial/inputs.empty.json`, `initial/inputs.template.json`, `initial/trial-commands.json` | Empty inputs and collection instructions/templates, without fabricated outcomes. |
| `inventory.json`, `README.md` | Completion inventory and generated guide. |

The timing filenames are `bNN-baseline.json` and `bNN-candidate.json`.
The later-stage budgets remain frozen in `plan.json`; preparation cannot yet
select a reducer incumbent or create its confirmation protocol.

Every rendered prompt is tokenized locally; the plan binds its individual input
length and 128-token output budget within the 8,192-token context. Verification
retokenizes and regenerates the preparation, rejecting missing, changed or
additional files. Keep all future raw results, edited input manifests and
reports outside the bundle, for example in sibling `/private/study/collection/`.
No file is downloaded and no serving endpoint is contacted. The output
directory must be new; interrupted preparation can leave partial files.
The payload and guide are reread and checked before `inventory.json` is written
as the final completion marker. Failure can leave payload files without a
completion marker; regenerate in a new directory.

The generated trial argv arrays resolve paths from the prepared bundle root
and name future outputs in `../collection/c001/`. The input template resolves
its paths from `prepared/initial/`; when making a working copy elsewhere,
update every relative path to keep pointing at the same plans, certificates,
timings and raw outputs. Its reset declarations start as `false`.

An initial search report awaiting evidence is valid. A reducer source,
incumbent, held-out confirmation protocol and measurement reports can only be
derived after the required raw results exist. Preparation must not create
successful trial outcomes, reset receipts or a positive comparison report.

## Collection procedure still required

The legacy capacity/follow-up GPU coordinators do **not** pass timing artifacts
or enable request correlation in their trial calls. Their output cannot be
relabeled as Breakpoint evidence. Before any paid collection, review a procedure
that joins the existing primitives, owns cleanup, enforces the whole-session
envelope and records each planned trial without changing its design. The study
preparer is not that execution controller.

The collection primitive already accepts the necessary flags. For one future
authorized trial, start a **new** router with the protocol's policy and a new
ledger, supplying both admission limits explicitly:

```bash
python -m inferdrome.vllm_router \
  --replica-a http://127.0.0.1:8001 --replica-b http://127.0.0.1:8002 \
  --host 127.0.0.1 --port 8090 --policy '<policy-from-protocol>' \
  --max-active 128 --max-queue 256 \
  --ledger /private/run/trial-router.jsonl
```

With the fresh router running, collect the planned timed, correlated result:

```bash
python -m inferdrome.vllm_router_study run \
  --plan /private/run/block-plan.json \
  --token-certificate /private/run/block-certificate.json \
  --timing /private/run/trial-timing.json --correlate-requests \
  --router http://127.0.0.1:8090 --model Qwen/Qwen3-8B \
  --policy '<policy-from-protocol>' --output /private/run/trial-result.json
```

These commands illustrate the primitive interface; they do not perform the
required engine setup/reset, protocol sequencing or budget enforcement. The
reviewed procedure must also establish all of the following:

- Validate the actual model snapshot, vLLM/image/source pins, two idle matched
  GPUs and one independent replica per device. Record each engine's observed
  KV-cache capacity; the intended prefix working set must exceed the smaller
  replica's reported capacity and remain below 85% of the pair's aggregate
  capacity. This is the existing capacity heuristic, not a residency observation.
  Stop and replan before collecting if it fails. A prior host's memory capacity
  is not a new observation.
- Match `environment.json`, including the 0.90 memory setting, loopback engine
  binding, disabled request logging, `HF_HUB_OFFLINE=1` and
  `VLLM_SERVER_DEV_MODE=1`. The latter enables the pinned development reset
  route and belongs only on the intended loopback servers. Retain observed
  runtime/driver details separately from these intended declarations.
- Before every trial, perform the disjoint direct warmup, observe zero running
  and waiting requests on each engine, and require HTTP 200 with exact
  `{"success":true}` from both `/reset_prefix_cache` calls. Recheck quiescence
  and retain raw responses/metrics. Warmup traffic is outside the measurement
  population. Start the fresh router after this reset.
- Follow the protocol's complete four-cell sequence. Keep the entire offered
  window and drain clear before another trial starts, even if client responses
  finish early. Close the prior router and preserve its ledger before creating
  the next one. Client clocks are only a consistency check on that chronology.
- Retain each raw timed client result, matching request-ID ledger, timing
  artifact, base plan and token certificate. Record source/environment/reset
  declarations matching the protocol. Declare `reset_completed=true` only after
  the reset observations exist; the current comparator does not authenticate
  them or join their contents automatically.
- Validate each stage from these raw inputs. Respect the two-candidate search
  and two-comparison reduction budgets. Freeze confirmation only from a verified
  terminal reducer source; use its exact retained group IDs and the eight
  predeclared held-out plans, without remapping or substituting seeds.
- Stop before another trial if its full allowance and cleanup reserve no
  longer fit. Preserve failed attempts, export/hash-check the private record,
  stop owned processes and follow the approved exact-instance teardown and
  absence-readback procedure.

A future rental additionally requires a current quoted offer, explicit spending
approval, an observed billing start and a concrete cleanup owner. Preparing this
design leaves those decisions open.

## Reporting boundary

Use the existing [search, reduction and confirmation workflow](BREAKPOINT.md)
after collection. The verifier must regenerate the complete source chain,
including unsuccessful attempts. The finite grid and small budgets provide no
guarantee of finding a reversal, a globally minimal witness or a robust effect
across other hosts and workloads. A failed criterion does not prove equivalence.
One fixed held-out batch does not cover repeated confirmation attempts or
selective publication.

Keep private raw archives, prompts, request identities, endpoint details and
engine logs private. Review any public summary with its source hashes and
limitations. Future outputs remain subject to the existing independence,
preregistration, execution-provenance and customer-acceptance boundaries.
Historical releases and the separate capacity study's 7.2% observation are
unchanged.
