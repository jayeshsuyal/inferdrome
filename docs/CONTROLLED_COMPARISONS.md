# Inferdrome controlled comparisons

Status: **Frozen v0.2 contract; fail-closed executor implemented**

Decision records:
[ADR 0008](adr/0008-add-operator-attested-controlled-comparisons.md) and
[ADR 0009](adr/0009-add-fail-closed-comparison-execution.md)

Release scope: **Post-v0.1, v0.2 second and third vertical slices**

An Inferdrome controlled comparison freezes one narrow two-arm design before
its local workflow executes, then independently recalculates every planned run
and either publishes one neutral run-level point estimate or returns
`INCOMPARABLE` with no estimate.

The word `PREDECLARED` describes the recorded design workflow. Its assurance is
`OPERATOR_ATTESTED`: current evidence does not include a trusted timestamp,
transparency receipt, or signed per-run binding to the plan digest. It must not
be presented as adversarial proof of chronology.

## Product boundary

This slice provides:

- immutable, separate plan and result public contracts;
- domain-separated plan and result digests emitted out of band;
- exact baseline and candidate source, resolved-spec, and fingerprint pins;
- preallocated, disjoint run IDs and Trial Set IDs;
- one reviewed typed treatment: `traffic.concurrency`;
- seeded permuted-pair execution scheduling;
- one fully typed primary outcome;
- exact complete-case arm membership and outcome coverage;
- full member-bundle verification and deterministic recalculation;
- complete and equal observed-v1-environment checks;
- equal-per-run paired candidate-minus-baseline arithmetic;
- fail-closed execution of the exact frozen schedule;
- verified-prefix resume with no retries or replacement runs;
- crash-safe immutable Trial Set and result publication;
- CLI plan/result creation, execution, and verification; and
- read-only dashboard index and detail views.

This slice does **not** provide trusted timing or authorship, causal inference,
confidence intervals, p-values, significance, winner labels, recommendations,
model prefix-caching controls, retry or replacement policies, distributed
orchestration, request pooling, telemetry-backed explanations, or ExitSpec
acceptance.

## Artifact layout

```text
comparison-plans/
└── comparison-plan-<32 lowercase hex>/
    └── comparison-plan.json

comparison-results/
└── comparison-result-<32 lowercase hex>/
    └── comparison-result.json
```

Descriptors are canonical RFC 8785 JSON in read-only directories. The safe
reader uses direct-child IDs, directory-relative no-follow opens, byte limits,
single-link regular files, stable file and directory identity checks, and an
exact one-file inventory. Plan and result digests are absent from their own
documents to avoid circular hashing.

## Plan contract

`inferdrome.controlled-comparison-plan.v1` freezes:

| Area | v1 contract |
|---|---|
| Arms | Exactly `BASELINE` and `CANDIDATE` |
| Treatment | Exactly `traffic.concurrency`, integer 1–100,000 |
| Repetitions | 2–100 per arm |
| Membership | Exact preallocated ordered run IDs and Trial Set IDs |
| Configurations | Full resolved experiment, source digest, and expected fingerprint per arm |
| Schedule | Seeded, explicit, one baseline and one candidate in each pair |
| Outcome | Exactly one frozen metric/aggregation/definition/unit/population tuple |
| Estimator | `paired_run_mean_difference_v1` |
| Statistical unit | `run` |
| Weighting | `equal_per_run` |
| Missing data | Any unavailable planned outcome makes the result incomparable |
| Exclusions | No post-assignment exclusions |
| Uncertainty | `none_v1` |
| Environment | Complete and equal observed v1 allowlist |
| Assurance | `OPERATOR_ATTESTED` |

The full baseline and candidate resolved specifications must differ at exactly
`traffic.concurrency`. The same exact one-path condition must hold for their
recomputed execution-fingerprint projections. A changed target, workload hash,
seed, output-token request, traffic population, measurement definition,
execution limit, evidence policy, or acceptance link invalidates the plan.

Plan creation generates all run IDs before execution and refuses a plan if any
of those IDs is already reserved beneath the selected runs root. The generated
schedule orders pairs from a 256-bit seed. Within each pair, both arms occur
exactly once; pair order is deterministic and independently reproducible.

## Fail-closed executor

`comparison-plan execute` treats the immutable plan as the only schedule. It
requires the externally retained plan digest and both original arm sources,
then resolves and verifies both sources before any planned run is reserved.
The already-read source and workload bytes are copied into private temporary
snapshots; every scheduled run resolves from those snapshots, so later edits to
the original paths cannot change the active comparison.

The executor holds one nonblocking advisory lock beneath the selected runs
root, keyed by plan ID and retained digest, until final result reverification.
This excludes a second cooperating local Inferdrome executor even if it chooses
a different result root. It is not a distributed lock or a defense against a
hostile filesystem writer, and an independent manual `inferdrome run` process
does not participate in the plan lock.

Resume is deliberately narrower than retry. Existing planned workspaces are
accepted only when they form the exact leading schedule prefix, are `COMPLETE`,
and their bundles independently recalculate and exact-match their frozen
workspace inputs. A missing bundle, tampered input, schedule hole,
`FAILED`/`INTERRUPTED` run, or abandoned nonterminal workspace blocks the plan.
No consumed run ID is retried and no replacement identity is generated.

Cancellation before reservation or between completed slots can resume from the
last verified boundary. Cancellation after reservation terminalizes that run as
`INTERRUPTED` when possible and therefore blocks this v1 plan. Hard termination
during a run can leave a nonterminal workspace, which also blocks rather than
being guessed safe.

After all runs verify, the executor creates or reuses the exact planned Trial
Sets and one deterministic result ID. Descriptor directories are fully written,
fsynced, frozen, and then published with no-replace semantics. Private orphan
stages are ignored, so a crash before publication cannot reserve the public
artifact identity. A crash after publication is recovered by independently
verifying the existing artifact.

These controls automate the workflow but do not upgrade its claims:
`PREDECLARED` remains `OPERATOR_ATTESTED`, Trial Sets remain `RETROSPECTIVE`,
synthetic runs remain `SYNTHETIC_ONLY`, and `COMPARABLE` remains a neutral
point-estimate eligibility state rather than causality, preference, or
acceptance.

## Result contract and controls

`inferdrome.controlled-comparison-result.v1` pins the plan digest and both Trial
Set digests. Result creation requires the operator to supply all three retained
digests. Verification recalculates every member bundle before deriving the
result.

Six controls are closed and ordered:

1. `LOCAL_PLAN_ORDER` — local plan timestamp precedes every member execution
   start. This is an internal consistency check, not trusted chronology.
2. `EXACT_ARM_MEMBERSHIP` — experiment, preallocated Trial Set IDs, ordered run
   IDs, bundle digests, source digests, and fingerprints match exactly; arms are
   disjoint.
3. `OBSERVED_SCHEDULE` — unique execution starts occur in the frozen schedule
   order.
4. `DECLARED_FINGERPRINT_DIFFERENCE` — every actual resolved specification
   equals its planned arm, each arm is internally identical, and cross-arm
   fingerprint differences equal exactly `traffic.concurrency`.
5. `COMPLETE_EQUAL_OBSERVED_ENVIRONMENT` — every environment is `COMPLETE` and
   all allowlisted values, provenance kinds, and evidence paths are equal.
6. `OUTCOME_COVERAGE_AND_SEMANTICS` — the primary outcome exists in every run
   and its metric definition, unit, population, reducer, and rounding semantics
   match the plan.

All six `SATISFIED` states produce `COMPARABLE`. Any `UNSATISFIED` state
produces `INCOMPARABLE`. The result stores only closed unsatisfied-control codes;
authoritative free-form result prose is not part of the contract.

`COMPARABLE` is deliberately narrow: the declared and observed v1 controls
matched. `OBSERVED_V1_ALLOWLIST_ONLY` means unobserved real-world confounders
may still exist. Neither term means causal, significant, preferred, accepted,
or customer-eligible.

## Deterministic estimator

For each pair `b`, Inferdrome preserves:

```text
d_b = candidate_run_value_b - baseline_run_value_b
```

The published estimate is:

```text
estimate = arithmetic_mean(d_b)
```

With complete balanced pairs, this equals candidate arm mean minus baseline
arm mean. Decimal arithmetic uses fixed high precision and half-even rounding
to six places. Every run contributes one scalar with equal weight. Requests
from separate runs are never concatenated. Run-level values, sample counts,
means, paired differences, and the estimate are all checked for arithmetic
consistency.

There is no confidence interval or hypothesis test in v1. Two or one hundred
pairs do not automatically create a confidence, power, significance, or causal
claim.

## CLI workflow

Prepare two source specifications that are identical except for concurrent
traffic concurrency, then create the plan:

```bash
inferdrome comparison-plan create \
  --baseline-source examples/controlled-concurrency-2.yaml \
  --candidate-source examples/controlled-concurrency-4.yaml \
  --title "Concurrency 2 versus 4" \
  --hypothesis "Concurrency may change attempted throughput." \
  --repetitions 2 \
  --primary-outcome attempted_request_throughput_per_s:rate \
  --runs-root runs \
  --comparison-plans-root comparison-plans
```

Retain the emitted plan digest and use it to verify the immutable design:

```bash
inferdrome comparison-plan verify \
  comparison-plans/comparison-plan-<id> \
  --expected-digest sha256:<plan-digest>
```

Execute and finalize the exact frozen workflow with the retained digest:

```bash
inferdrome comparison-plan execute \
  comparison-plans/comparison-plan-<id> \
  --expected-digest sha256:<plan-digest> \
  --baseline-source examples/controlled-concurrency-2.yaml \
  --candidate-source examples/controlled-concurrency-4.yaml \
  --runs-root runs \
  --trial-sets-root trial-sets \
  --comparison-results-root comparison-results
```

The JSON response distinguishes `executed_run_ids` from independently verified
`reused_run_ids` and includes both Trial Set digests plus the final result
digest and status. Exit code `0` for an `INCOMPARABLE` result means the pipeline
finalized and verified; it does not mean the experimental comparison succeeded.

On a prepared Linux/NVIDIA host, the pinned managed-vLLM proof runner exercises
this full workflow with two concurrency arms and two repetitions per arm:

```bash
.inferdrome-gpu/venv/bin/python \
  scripts/run_real_gpu_demo.py --comparison
```

It independently verifies all four customer-eligible bundles, both Trial Sets,
and the result, then invokes the executor again and requires exact reuse without
new execution. See [REAL_GPU_PROOF.md](REAL_GPU_PROOF.md) for the receipt and
review boundary.

The lower-level commands remain available for manual protocol inspection. If
used, run every preallocated ID in schedule order, create both exact planned
Trial Sets, then create the result with all retained digests:

```bash
inferdrome run examples/controlled-concurrency-2.yaml \
  --runs-root runs --run-id run-<planned-baseline-id>
inferdrome trial-set create \
  --trial-set-id trial-set-<planned-baseline-id> \
  --run run-<baseline-repeat-1> --run run-<baseline-repeat-2> \
  --title "Baseline arm" --runs-root runs --trial-sets-root trial-sets
```

Create the result using all retained digests:

```bash
inferdrome comparison-result create \
  --comparison-plan-id comparison-plan-<id> \
  --expected-plan-digest sha256:<digest> \
  --baseline-trial-set-id trial-set-<baseline-id> \
  --expected-baseline-digest sha256:<digest> \
  --candidate-trial-set-id trial-set-<candidate-id> \
  --expected-candidate-digest sha256:<digest> \
  --runs-root runs --trial-sets-root trial-sets \
  --comparison-plans-root comparison-plans \
  --comparison-results-root comparison-results
```

Independent verification accepts an optional externally retained result digest:

```bash
inferdrome comparison-result verify \
  comparison-results/comparison-result-<id> \
  --runs-root runs --trial-sets-root trial-sets \
  --comparison-plans-root comparison-plans \
  --expected-digest sha256:<result-digest>
```

## Dashboard

Start the loopback-only dashboard with all roots:

```bash
inferdrome dashboard \
  --runs-root runs \
  --trial-sets-root trial-sets \
  --comparison-plans-root comparison-plans \
  --comparison-results-root comparison-results
```

`/comparisons` lists verified plans and separates `PREDECLARED` design state
from `COMPARABLE`, `INCOMPARABLE`, no-result, and withheld result states.
`/comparisons/:planId` shows the frozen design first, then all controls, then a
single run-level dot plot only when a comparable result exists. The schedule
also shows bounded operational states—`NOT_STARTED`, `PARTIAL`, `BLOCKED`,
or `EVIDENCE_COMPLETE`—and marks a run verified only after workspace and bundle
recalculation. Result publication is shown separately from current workspace
progress, so a valid portable result can coexist with blocked local inspection.
This projection is local observation, not portable evidence or proof that an
executor is currently alive. `/compare` remains an ad hoc two-run arithmetic
utility.

All APIs are GET-only. Invalid plans or results are withheld with bounded reason
codes. Result detail lookup resolves one validated plan ID directly and never
treats a URL segment as a filesystem path.
