# Breakpoint: reduce a timing witness

**Step 5:** reduce the timing changes in a candidate found by
[bounded search](VLLM_BOUNDED_SEARCH.md), using fresh
[paired comparisons](VLLM_PAIRED_COMPARISON.md) to decide which changes to keep.
The controller prepares protocols and checks supplied evidence offline. It does
not launch serving processes or GPU experiments.

## Restore whole original groups

The reduction units are the compression groups from the source timing recipe.
Group IDs such as `e1-g0003` identify an epoch and its original group ordinal.
Those boundaries never change when another group is removed.

A retained group keeps its source-compressed arrival times. Every other group
returns to its original arrival times. **Every request remains in the trial**:
prompts, token budgets, indices, document/tenant/class/epoch membership, offered
count, full window, deadlines, concurrency and SLO thresholds stay fixed.
Goodput retains the full offered-window denominator.

The global retained set maps to each block's active original groups. A proposal
that leaves any block with no timing change is structurally skipped. Local
timing artifacts can represent an empty mask, but an identity candidate cannot
enter the reduced comparison screen.

The reducer deterministically partitions the current retained set and proposes
removing each partition. If none is accepted, it tries finer partitions, down
to single-group removals. Each evaluable proposal requires a new four-cell comparison:
both policies under the identity baseline and the proposed reduced schedule,
across every frozen block. Only `REVERSAL_CANDIDATE` accepts a removal.
Inconclusive or ineligible comparisons preserve the current retained set.

This reduces a planned timing intervention. Restoring arrivals can change actual
cache contents, queue occupancy and processing order; the reducer does not
establish equivalence of those runtime states.

## Start from inspectable search evidence

The source must reproduce a bounded-search result with a selected candidate.
Keep its raw input manifests and any immediate previous search report. Create
`source-manifest.json` using paths relative to that manifest:

```json
{
  "schema": "inferdrome.vllm-router-reduction-source.v1",
  "search_plan": "search-plan.json",
  "inputs": "search-inputs.json",
  "report": "search-final.json",
  "previous_report": null
}
```

For a linked search snapshot, replace `null` with its immediate previous report
path. The reducer rechecks the source from raw evidence; a saved positive
comparison report alone is insufficient.

```bash
PYTHONPATH=src python -m inferdrome.vllm_witness_reducer prepare \
  --source /private/study/source-manifest.json \
  --max-comparisons 12 --max-trial-slots 384 \
  --output /private/study/reduction-plan.json
```

The numbers illustrate the format. Each complete comparison reserves
`4 × block_count` trial slots, including fresh baseline trials. Budgets describe
controller accounting, not observed GPU cost or an external runner's limits.
Every finalized submission reserves the full comparison, including missing
evidence. Structural skips and already-tested masks reserve no new slots.

## Submit proposals and resume

Start a reduction input manifest with no proposals:

```json
{
  "schema": "inferdrome.vllm-router-reduction-inputs.v1",
  "reduction_plan_sha256": "COPY_FROM_REDUCTION_PLAN",
  "proposals": []
}
```

```bash
PYTHONPATH=src python -m inferdrome.vllm_witness_reducer report \
  --plan /private/study/reduction-plan.json \
  --source /private/study/source-manifest.json \
  --inputs /private/study/reduction-inputs.json \
  --output /private/study/reduction-00.json
```

When the report requests a comparison, retain its proposal ID and save
`next_action.protocol` as `proposal-protocol.json`.
The timing files for that protocol can be materialized offline:

```bash
PYTHONPATH=src python -m inferdrome.vllm_reduced_protocol materialize \
  --protocol /private/study/proposal-protocol.json \
  --plans /private/study/block-01.json /private/study/block-02.json \
          /private/study/block-03.json /private/study/block-04.json \
          /private/study/block-05.json /private/study/block-06.json \
          /private/study/block-07.json /private/study/block-08.json \
  --output-dir /private/study/proposal-artifacts
```

Supply every frozen block in order. The new directory contains `protocol.json`
and each block's baseline/candidate timing artifacts. It runs no trials.
After collecting separately authorized evidence, append an entry such as
`{"proposal_id":"r001","inputs":"r001/inputs.json"}` to `proposals`. Its `inputs`
path points to the existing paired-comparison manifest format and resolves
relative to the reduction input manifest.

```bash
PYTHONPATH=src python -m inferdrome.vllm_witness_reducer report \
  --plan /private/study/reduction-plan.json \
  --source /private/study/source-manifest.json \
  --inputs /private/study/reduction-inputs.json \
  --previous-report /private/study/reduction-00.json \
  --output /private/study/reduction-01.json

PYTHONPATH=src python -m inferdrome.vllm_witness_reducer verify \
  --plan /private/study/reduction-plan.json \
  --source /private/study/source-manifest.json \
  --inputs /private/study/reduction-inputs.json \
  --previous-report /private/study/reduction-00.json \
  --report /private/study/reduction-01.json
```

Resume rebuilds the source and earlier proposal evidence before extending the
snapshot. Preserve earlier raw artifacts and reports. Submission finalizes an
attempt, including incomplete evidence; repairing an old attempt is not resume.
Outputs use new files and materialization requires a new directory.
The immediate previous snapshot is regenerated; ancestor files are not traversed.

| Status | Meaning |
|---|---|
| `AWAITING_EVIDENCE` | A proposal fits the budget; its fresh comparison protocol is returned. |
| `REDUCTION_COMPLETE` | One group remains, or the current single-group removal round has finished. |
| `BUDGET_EXHAUSTED` | The next complete comparison does not fit the declared limits. |

An awaiting report is saved and exits 2. Terminal states exit 0, including
budget exhaustion. Neither completion nor exit 0 establishes a confirmed or
minimal counterexample. Invalid evidence fails before publishing a report.

## Limits of a reduced witness

A successful removal leaves a smaller screened-in witness under this group
removal procedure; the budget may expire with the original witness retained.
A rejected removal does not prove it is necessary: noise,
ineligible evidence, interaction between groups or a budget limit can prevent
further reduction. There is no claim of minimality, causality or independently
confirmed reversal.

Scope remains `EXPLORATORY_SEARCH_ONLY`. Source/environment/reset declarations
and hashes do not authenticate execution or independent replicates. Adaptive
search and reduction do not obtain a search-wide error guarantee from the paired
screen. Held-out confirmation remains a later step.

Keep raw artifacts private and review reports before publication; protocols can
include a private model identifier. This change is checked with synthetic
evidence and local fake replicas. It adds no GPU result and does not alter the
historical baseline or separate 7.2% capacity observation.
