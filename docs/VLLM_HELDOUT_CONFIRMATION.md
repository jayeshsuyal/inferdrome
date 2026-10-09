# Breakpoint: held-out confirmation

**Step 6:** freeze the incumbent from [timing witness reduction](VLLM_WITNESS_REDUCER.md)
and check it on fresh workload seeds. This offline workflow prepares a protocol
and verifies supplied raw evidence. It adds no GPU result. The
[unified CLI walkthrough](BREAKPOINT.md) connects this step to search and
reduction and provides a complete synthetic example.

## Freeze the question before collection

Start from a reproducible terminal reducer report: `REDUCTION_COMPLETE` or
`BUDGET_EXHAUSTED`. The latter may retain the original search candidate. An
awaiting reducer cannot enter confirmation. The source search and reduction
reports must regenerate from their raw artifacts, including any supplied
immediate previous snapshots; a positive report alone is insufficient.

The plan fixes the incumbent's timing parameters and retained epoch/group IDs,
two policies, practical margin, latency gates, source revision, environment and
reset declarations. Only the order seed is chosen anew. Supply **8–32 distinct
workload seeds, in multiples of four**, all disjoint from discovery/reduction
seeds and with the same workload contract. Calibration and pilot plans are
ineligible. Fixture and evaluation evidence cannot mix.

The mask transfers a rule such as “retain `e1-g0003`” to each new seed's original
epoch-local groups. It does not transfer the same concrete requests or runtime
history. An unmappable mask, or a candidate with no moved offers in any block,
is rejected before collection. All offers, the full offered window, token
budgets, SLOs and client limits remain in the comparison.

Every block contains four fresh trials: both policies under identity timing
and under the frozen reduced timing. The seeded Williams order balances cells
and immediate predecessors. Discovery and reduction results select the
candidate; they contribute no observations to the confirmation statistic.

## Inspectable offline workflow

Keep all input paths local and private. A confirmation source manifest refers
to the existing reduction source and artifacts; paths resolve relative to each
manifest:

```json
{
  "schema": "inferdrome.vllm-router-confirmation-source.v1",
  "search_source": "source-manifest.json",
  "reduction_plan": "reduction-plan.json",
  "inputs": "reduction-inputs.json",
  "report": "reduction-final.json",
  "previous_report": null
}
```

For a linked reducer snapshot, set `previous_report` to its immediate previous
report path. Keep any linked search snapshot in `search_source` as well.

```bash
PYTHONPATH=src python -m inferdrome.vllm_heldout_confirmation prepare \
  --source /private/study/confirmation-source.json \
  --plans /private/study/heldout-01.json /private/study/heldout-02.json \
          /private/study/heldout-03.json /private/study/heldout-04.json \
          /private/study/heldout-05.json /private/study/heldout-06.json \
          /private/study/heldout-07.json /private/study/heldout-08.json \
  --order-seed 130363 --output /private/study/confirmation-plan.json
```

The plan embeds the full `protocol` and binds its source report, retained mask,
held-out plan hashes and criteria. Materialize its timing files with the same
held-out plans in order:

```bash
PYTHONPATH=src python -m inferdrome.vllm_heldout_confirmation materialize \
  --plan /private/study/confirmation-plan.json \
  --source /private/study/confirmation-source.json \
  --plans /private/study/heldout-01.json /private/study/heldout-02.json \
          /private/study/heldout-03.json /private/study/heldout-04.json \
          /private/study/heldout-05.json /private/study/heldout-06.json \
          /private/study/heldout-07.json /private/study/heldout-08.json \
  --output-dir /private/study/confirmation-artifacts
```

This rechecks the source and creates a new directory with `protocol.json` and
`bNN-baseline-timing.json` / `bNN-candidate-timing.json` files. After
separately authorized collection, use the existing
[paired input manifest](VLLM_PAIRED_COMPARISON.md#offline-workflow) with the
confirmation protocol hash, all held-out plans and every planned trial.

```bash
PYTHONPATH=src python -m inferdrome.vllm_heldout_confirmation report \
  --plan /private/study/confirmation-plan.json \
  --source /private/study/confirmation-source.json \
  --inputs /private/study/confirmation-inputs.json \
  --output /private/study/confirmation-report.json

PYTHONPATH=src python -m inferdrome.vllm_heldout_confirmation verify \
  --plan /private/study/confirmation-plan.json \
  --source /private/study/confirmation-source.json \
  --inputs /private/study/confirmation-inputs.json \
  --report /private/study/confirmation-report.json
```

Preparation and reporting refuse existing output files. Verification regenerates
the full report from source and confirmation artifacts. Plan hashes bind the
declared procedure; they do not authenticate advance registration.

## Criteria and limitations

The [paired comparison arithmetic](VLLM_PAIRED_COMPARISON.md#what-makes-a-candidate)
is unchanged. Both frozen directions must pass: A exceeds B beyond the practical
margin at baseline, and B exceeds A beyond that margin under the candidate.
Each direction uses an exact binomial tail threshold of `1/40`; ties count as
failures, every planned block stays in the denominator, and the observed mean
must also exceed the margin. The hypothesis concerns directional blockwise
margin-exceedance probability, not population mean significance. The nominal
two-direction error allocation assumes independent blocks and a null exceedance
probability at most one half.

| Status | Meaning |
|---|---|
| `HELD_OUT_CRITERIA_MET` | Eligible held-out evidence passes both fixed directional criteria. |
| `HELD_OUT_CRITERIA_NOT_MET` | Eligible evidence does not pass both criteria; this does not establish equivalence. |
| `INELIGIBLE` | The evidence cannot support the planned comparison. |

Both criteria statuses exit 0. `INELIGIBLE` writes the report and exits 2;
malformed inputs fail without publishing one. Exit 0 alone is not a positive
finding.

Raw client results, router receipts, timings and token certificates are checked
again. Result hashes and request IDs must be disjoint from **all** supplied
search/reduction trials, including failed attempts. Reported confirmation
windows must follow discovery and reduction, and satisfy the protocol's order
and nonoverlap checks. Client clocks provide consistency checks, not trusted
chronology.

The wrapper's scope is `FIXED_BATCH_HELD_OUT_CONFIRMATION`; the nested paired
report retains its original `EXPLORATORY_SEARCH_ONLY` contract. Reports remain
`evidence_eligible=false`, with independence and preregistration `UNVERIFIED`.

There is no global registry preventing another confirmation plan, retries,
selective publication or seed reuse outside the supplied source. Repeated
confirmation attempts are not covered by the two-direction adjustment.
Preregistration, execution independence, provenance and resets remain
unauthenticated. A positive fixture result is `SYNTHETIC_ONLY`; it cannot become
a GPU finding. Local evaluation also requires these assumptions and does not
establish customer acceptance, causality or a minimal counterexample.

Keep raw archives private and review intended public reports for private model
identifiers. Historical campaigns cannot be relabeled as confirmation evidence.
This step does not change the historical two-replica study or its separate
7.2% capacity observation.
