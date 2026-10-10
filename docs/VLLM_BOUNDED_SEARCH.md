# Breakpoint: bounded timing search

**Step 4:** an offline search controller that prepares a finite set of timing
changes, requests comparison evidence in a fixed order, and selects the first
candidate passing the [paired comparison screen](VLLM_PAIRED_COMPARISON.md).
It reads supplied artifacts and emits the next protocol. It launches no serving
process, cloud job or GPU experiment. [Timing witness reduction](VLLM_WITNESS_REDUCER.md)
can follow a finding, then [held-out confirmation](VLLM_HELDOUT_CONFIRMATION.md)
checks the frozen incumbent. Start with the [unified CLI walkthrough](BREAKPOINT.md).

## Search the smallest planned changes first

Freeze the base plans, all PR3 comparison options, three parameter axes, and two
budgets before inspecting outcomes. The Cartesian grid covers `group_size`,
`retained_spacing_bps` and `max_advance_ns` from the
[timing transform](VLLM_ARRIVAL_TIMING.md).

Preparation regenerates each recipe across every block. Recipes that move no
offers in one or more blocks are recorded as excluded. Recipes yielding the
same ordered vector of transformed trace hashes are one candidate; the
lexicographically first parameter tuple represents it and the rest remain
inspectable aliases. Timing artifact hashes cannot deduplicate schedules because
they also include the recipe parameters.

Candidates are ordered lexicographically by:

1. Maximum applied advance across all offers and blocks.
2. Total applied advance across those offers.
3. Number of moved offers.
4. Schedule digest, as a deterministic tie breaker.

These are changes to **planned** arrival times. The distance does not measure
observed router arrival spacing, realism, or every possible workload difference.
Aggregate nanoseconds are decimal strings so canonical JSON never rounds a large
sum. Identical base populations, windows and measurement rules remain fixed.

Each axis has 1–16 distinct integer values; the grid has at most 128 recipes.
Preparation also limits `grid_size × total_offers_across_blocks` to 2,000,000.
These bounds limit deterministic preparation work; they are not GPU budgets.
A grid with no eligible, nonidentity schedule is rejected.

## Budgets and stopping

`max_candidates` limits finalized candidate submissions. `max_trial_slots` limits
reserved trial slots, at `4 × block_count` per candidate. A candidate fits only
if its whole comparison fits both limits. Even a failed or empty submission
reserves every slot; supplied trial artifacts are counted separately. These are
controller accounting units, not observed runs, elapsed time or GPU cost. An
external runner is responsible for its own execution limits.

| Status | Meaning |
|---|---|
| `AWAITING_EVIDENCE` | The next candidate fits; its exact PR3 protocol is returned. |
| `CANDIDATE_FOUND` | The first submitted candidate passed both comparison directions and search eligibility. |
| `BUDGET_EXHAUSTED` | No further complete comparison fits the declared limits. |
| `GRID_EXHAUSTED` | Every unique schedule was submitted without a screened-in candidate. |

Submissions must be an exact contiguous prefix of the frozen order. Skipping,
reordering, duplicate IDs, submissions beyond budget, and evidence after the
first screened-in candidate are rejected. There is no monotonicity assumption
or binary search: every preceding candidate is retained. Original baseline
trials are repeated for each candidate; cross-candidate request/result reuse is
rejected. Shared baseline observations need a separate future design.

Every candidate is rechecked from its raw timed results, token certificates and
router ledgers. Saved comparison reports are not trusted as inputs. Missing or
operationally invalid evidence remains `INELIGIBLE`; a weak signal remains
`INCONCLUSIVE`. Reported cross-candidate window overlap makes that candidate
ineligible for selection even if its standalone PR3 comparison passes. The PR3
report remains intact alongside the additional search eligibility reason.

## Immutable snapshots and resume

A submission **finalizes a candidate**, including an incomplete submission. Wait
until collection is finished before submitting it. Trial-level repair/retry is
not supported by this search format. A new run requires a separately declared
search; it is not independent confirmation merely because its ID changed.

Each report is a new file. To resume, retain earlier raw artifacts, append new
candidate submissions, and provide the immediate previous report. The controller
rebuilds that entire old prefix and requires it to reproduce exactly before
accepting the extension. Removed, edited or replaced earlier evidence is rejected.
The new report binds the previous report's digest. It regenerates the immediate
prior snapshot; it does not traverse earlier ancestor files or authenticate the
chain. Without a previous report, a snapshot still rechecks all its supplied raw
evidence, but makes no claim of continuity with earlier snapshots.

## Offline commands

Create `search-config.json` with `plans` as ordered local paths, all PR3
`comparison_options` **except** `candidate_parameters`, and these search fields:

```json
{
  "group_sizes": [2, 4, 8],
  "retained_spacing_bps": [0, 2500, 7500],
  "max_advances_ns": [10000000, 100000000, 1000000000],
  "max_candidates": 12,
  "max_trial_slots": 384
}
```

This snippet shows the search fields only; the frozen plans and comparison
options are required. Values illustrate the format, not recommended experiment
settings. Eight blocks cost 32 reserved trial slots per candidate. The comparison
options freeze policies, order seed, effect margin, client delay limits, model,
source revision and environment/reset declaration hashes as documented in PR3.

```bash
PYTHONPATH=src python -m inferdrome.vllm_bounded_search prepare \
  --config /private/study/search-config.json \
  --output /private/study/search-plan.json
```

A search input manifest starts with an empty `candidates` array and grows by
appending a path to each candidate's existing PR3 input manifest:

```json
{
  "schema": "inferdrome.vllm-router-search-inputs.v1",
  "search_plan_sha256": "COPY_FROM_SEARCH_PLAN",
  "plans": ["block-01.json", "block-02.json", "block-03.json", "block-04.json"],
  "candidates": [{"candidate_id": "c001", "inputs": "c001/inputs.json"}]
}
```

Use every frozen plan, in the same order. Paths resolve relative to their own
manifest, including nested PR3 manifests. Candidate IDs come from the search
plan, while PR3 trial IDs remain scoped within each candidate.

```bash
PYTHONPATH=src python -m inferdrome.vllm_bounded_search report \
  --search-plan /private/study/search-plan.json \
  --inputs /private/study/search-inputs.json \
  --output /private/study/search-01.json

# After collecting and appending the next candidate's authorized evidence:
PYTHONPATH=src python -m inferdrome.vllm_bounded_search report \
  --search-plan /private/study/search-plan.json \
  --inputs /private/study/search-inputs.json \
  --previous-report /private/study/search-01.json \
  --output /private/study/search-02.json

PYTHONPATH=src python -m inferdrome.vllm_bounded_search verify \
  --search-plan /private/study/search-plan.json \
  --inputs /private/study/search-inputs.json \
  --previous-report /private/study/search-01.json \
  --report /private/study/search-02.json
```

An awaiting report contains `next_action.protocol`. To export a scheduled
candidate protocol directly, use `candidate --search-plan ... --plans
/path/block-01.json /path/block-02.json ... --candidate-id c001 --output ...`.
This does not execute it. Preparing a protocol cannot prove it was collected in
that order or that an operator respected the budget.

Outputs are exclusive: existing files are never replaced. `AWAITING_EVIDENCE`
writes the report and exits 2. Terminal search states exit 0, including no-hit
budget/grid exhaustion. Exit 0 is not a performance success. Invalid inputs
produce an error before publishing a report. `verify` requires the same immediate
previous report when checking a linked snapshot.

## What a finding establishes

A selected result is the **nearest screened-in candidate among evaluated
schedules under the declared order**. Earlier inconclusive comparisons do not
exclude smaller real reversals; ineligible comparisons leave evidence gaps.
The report exposes both kinds of unresolved earlier candidates, grid coverage,
reserved budget, every regenerated comparison, and artifact hashes. Neither
exhaustion nor complete grid coverage proves absence or global minimality.

Everything remains `EXPLORATORY_SEARCH_ONLY`, with `evidence_eligible=false`,
`independent_execution=UNVERIFIED`, `held_out_confirmation=NOT_IMPLEMENTED`, and
`search_multiplicity=UNCONTROLLED`. PR3's nominal two-direction screen is not a
search-wide error guarantee. Repeated or selected findings require independent
confirmation. Source, reset and environment declarations and reported client
clocks are consistency checks, not authenticated observations.

Keep raw inputs private. Reports omit raw request prompts, router origins,
request IDs and input artifact paths. An awaiting report retains the configured
model identifier in its next protocol; that identifier may itself contain a
private path or URL. Review reports and model identifiers before publication.
Existing historical GPU archives cannot
be relabeled as the new correlated timed evidence. This step runs synthetic and
offline validation only; historical results, frozen release contracts and the
short README are unchanged.
