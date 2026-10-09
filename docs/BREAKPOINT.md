# Breakpoint: find, reduce, and recheck a routing reversal

Breakpoint asks whether changing **when the same requests are offered** can
reverse the ordering of two routing policies. It searches a bounded timing
grid, reduces a candidate's timing changes, then checks the frozen intervention
on held-out workload seeds. The commands prepare protocols and verify supplied
artifacts offline; collecting real measurements is a separate step.

## Try it without a GPU

From a checkout with Python 3.12 and the frozen environment installed:

```bash
uv sync --frozen
uv run inferdrome breakpoint demo --output-dir /tmp/inferdrome-breakpoint-demo
```

Use a new output directory. The demo writes fabricated request results and
router ledgers, discovery and held-out plans, timing artifacts, manifests,
verified JSON reports, and readable summaries. Its generated `README.md`
provides the file inventory and commands to verify the reports again.
An interrupted or failed command can leave a partial directory; use a new path
for the next attempt.

Everything is **`SYNTHETIC_ONLY`**, with `evidence_eligible=false`. The demo
constructs a reversal to exercise the workflow; it measures no serving system,
contacts no endpoint, and starts no GPU job. Its all-zero source revision is a
fixture placeholder. It does not replay the historical Qwen3-8B studies.

## One command family

| Command | Purpose | Detailed method |
| --- | --- | --- |
| `breakpoint search` | Freeze a finite timing grid; evaluate supplied comparisons within its budget. | [Bounded search](VLLM_BOUNDED_SEARCH.md) |
| `breakpoint reduce` | Propose fewer timing changes; retain them only after fresh comparisons. | [Witness reduction](VLLM_WITNESS_REDUCER.md) |
| `breakpoint confirm` | Freeze the incumbent and test it on disjoint workload seeds. | [Held-out confirmation](VLLM_HELDOUT_CONFIRMATION.md) |
| `breakpoint summarize` | Reproduce a saved report from raw artifacts, then write a Markdown summary. | Below |
| `breakpoint demo` | Generate and verify the complete synthetic walkthrough locally. | Above |

Prefix these commands with `inferdrome`. Each stage has `--help`; the original
`python -m inferdrome.vllm_bounded_search`, `vllm_witness_reducer`, and
`vllm_heldout_confirmation` entry points remain available with the same actions
and flags. The wrappers use those implementations and their existing JSON
formats.

The normal sequence is:

1. **Search:** `prepare` fixes the grid and budgets; `report` identifies the
   next candidate protocol. Supply all four cells per planned block through
   the existing paired input manifest, then regenerate `report`.
2. **Reduce:** `prepare` fixes the reduction budget from reproducible search
   evidence. Each `report` either proposes a new reduced protocol or records a
   terminal result. Every proposal requires fresh results.
3. **Confirm:** `prepare` freezes a terminal reducer's incumbent and 8–32 fresh
   workload seeds, in multiples of four. `materialize` writes its timing files;
   `report` evaluates the supplied held-out trials.
4. **Inspect:** `verify` regenerates each saved JSON report. `summarize` performs
   that same check before emitting the readable projection.

Source manifests keep the raw evidence chain available locally, including any
linked immediate previous reports. Discovery and reduction select the
intervention; only held-out blocks enter the confirmation statistic. Neither
the CLI nor the demo provides a live experiment scheduler.

## A readable report with a checked source

For example, given a confirmation source and its raw input manifest:

```bash
inferdrome breakpoint summarize confirm \
  --plan /private/study/confirmation-plan.json \
  --source /private/study/confirmation-source.json \
  --inputs /private/study/confirmation-inputs.json \
  --report /private/study/confirmation-report.json \
  --output /private/study/confirmation-summary.md
```

For search, use `summarize search --search-plan ... --inputs ... --report ...
--output ...`. For reduction, use `summarize reduce --plan ... --source ...
--inputs ... --report ... --output ...`. Add `--previous-report ...` when the
saved search or reduction snapshot was linked to one.

The command loads and rechecks the raw artifacts, regenerates the complete
report, and requires equality with the saved JSON before writing. A report's
self-consistent digest alone is insufficient. Missing, malformed or changed
inputs prevent summary publication; existing output files are refused.

The summary contains selected status, evidence class, counts, budget or frozen
criteria, timing recipe, source declarations, and artifact hashes. It omits
model identifiers, local paths, endpoint URLs, request IDs, prompts, and raw
free-form reasons. Review any publication in its context: hashes and declared
source revisions still disclose identity information. The Markdown is a
readable projection, not a sealed evidence bundle or a substitute for the
private source manifests needed to regenerate it.

## Interpret the result

| Result | Meaning |
| --- | --- |
| Search `CANDIDATE_FOUND` | An exploratory candidate passed the paired screen. |
| Reduction `REDUCTION_COMPLETE` | The configured reduction procedure finished; minimality is not established. |
| Confirmation `HELD_OUT_CRITERIA_MET` | Both frozen directional criteria passed on eligible held-out evidence. |
| Confirmation `HELD_OUT_CRITERIA_NOT_MET` | Both criteria did not pass; this does not establish equivalence. |
| `AWAITING_EVIDENCE` / `INELIGIBLE` | More evidence is needed, or supplied evidence cannot support the comparison. |

Search and reduction awaiting evidence exit 2. Confirmation ineligibility exits
2. Valid terminal outcomes, including no candidate and criteria not met, exit
0. Read the status, not just the process exit code. The underlying stage
reports retain their existing limitations and error semantics.

Confirmation requires both a practical mean margin and an exact blockwise
margin-exceedance tail of at most `1/40` in each frozen direction. Ties count
as failures and all planned blocks stay in the denominator. The nominal error
allocation assumes independent blocks and the stated null model; it does not
cover repeated confirmation attempts or the adaptive discovery search.

Independence, preregistration, source/reset declarations, and reported client
clocks are not authenticated. There is no global retry or selective-publication
registry. Outputs remain ineligible for customer acceptance. A real claim
requires a separately planned and executed measurement study.

This development workflow leaves the historical release contracts and
[two-replica routing and capacity results](VLLM_ROUTER_RESULTS.md) unchanged.
The capacity study's 7.2% observation is not a Breakpoint confirmation result.
