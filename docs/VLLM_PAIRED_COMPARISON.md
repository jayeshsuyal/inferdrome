# Breakpoint: paired comparisons

**Step 3:** an offline, reproducible noise screen for one proposed timing change.
It consumes [timed client results](VLLM_ARRIVAL_TIMING.md) and
[router request receipts](VLLM_REQUEST_IDENTITY.md). It does not launch trials.
Automated search, reduction, held-out confirmation and GPU validation follow in
later steps. The historical [capacity result](VLLM_ROUTER_RESULTS.md) is unchanged.

## The experiment

Freeze two policies, A and B, and one candidate timing recipe. Each block uses
one distinct workload seed and contains **four trials**:

| Schedule | Policy A | Policy B |
|---|---|---|
| Baseline | Identity timing transform | Identity timing transform |
| Candidate | Frozen timing change | The same timing change |

All four trials share the exact base plan, token certificate and request
population. Across blocks, only seed-dependent trace/prompt-token fields may
vary. Offered count, duration, workload recipe, token budget, SLOs and client
limits remain fixed. Calibration and pilot plans are rejected. Fixture and
evaluation plans cannot mix.

Use 4–32 blocks, in multiples of four. A seeded, deterministic Williams design
balances the four cells across positions and immediate predecessor cells in
each group of four blocks. This is a planned order, not proof that randomization
or independent execution happened. Choose seeds and thresholds before inspecting
outcomes, keep all planned trials, and independently reset before each trial.
Only the original four routing policies are supported; `cache_saturation` needs
a separate frozen threshold contract before it can join this comparison.

## What makes a candidate

For every block and schedule, compute `d = goodput(A) - goodput(B)`, where
**SLO goodput is the number of completed requests meeting both SLOs divided by
the full offered window**. Every offer stays in accounting, including rejected
and failed requests. The block is the replication unit. Thousands of requests do not become thousands of independent
replicates.

A `REVERSAL_CANDIDATE` requires both of these predeclared directions:

1. Baseline: A exceeds B by more than the practical margin.
2. Candidate: B exceeds A by more than that same margin.

For each direction, let `k` be the number of blocks strictly beyond the margin.
The diagnostic tail is `sum(comb(n, j), j=k..n) / 2**n`; it must be at most
`1/40` (0.025). Every planned block stays in `n`; ties count as failures. The
observed mean must also exceed the margin in the required direction. Count and
rate comparisons use exact rational arithmetic; decimal rates are displays.
Margins are integer microrequests/second: `100000` means 0.1 request/second.

This is a conservative binomial margin-exceedance screen related to the
[paired sign test](https://www.itl.nist.gov/div898/software/dataplot/refman1/auxillar/signtest.htm).
It assumes independent blocks and a null directional exceedance probability
at most one half. The two thresholds allocate a nominal 0.05 familywise budget
across the two fixed directions using the
[Bonferroni principle](https://itl.nist.gov/div898/handbook/prc/section4/prc473.htm).
Those assumptions are **not authenticated**. The test concerns blockwise
exceedance probability, not the population mean; the additional observed-mean
guard does not establish mean significance.

Four unanimous blocks have a tail of 0.0625 and cannot pass. Eight unanimous
blocks have a tail of 0.00390625 and can pass. If either directional screen or
observed-mean guard fails, the result is `INCONCLUSIVE`. Ties reduce the number
of exceedances; they do not automatically rule out a signal at larger sample
sizes. Inconclusive results do not establish equivalence. The report includes every block,
mean/median/range, exact fractions, sign counts and diagnostic tails; it provides
no confidence interval. Repeated searches across candidates or optional stopping
are not covered by the two-direction adjustment. Held-out confirmation remains
necessary before a research claim.

## Eligibility before arithmetic

The comparator regenerates the entire protocol and timing artifacts, validates
each supplied token certificate, recomputes client summaries and joins **raw**
router receipts by request ID. Prior verifier reports are not substitutes.

- Missing trials or receipts, invalid measurement status, undeclared reset
  completion, overlapping/reordered reported windows, and excessive client
  scheduling lag/queue delay make the whole comparison `INELIGIBLE`.
- Completed requests and router capacity/queue-timeout rejections are admissible.
  Other operational failures or contradictory client/router outcomes make the
  comparison ineligible. No failing block is silently removed.
- Duplicate trials, reused request IDs, wrong policies/models, mismatched source
  declarations, ambiguous JSON, rehashed edited schedules and invalid artifacts
  are rejected as input errors. Source result schemas remain unchanged.

Latency gates use the frozen p95 limits over all offers with observed stage
samples. They do not bound every request or establish actual router arrival
spacing. Missing stage observations fail eligibility.

## Offline workflow

Keep inputs in a private directory. A `config.json` supplies the keyword
arguments to `vllm_paired_protocol.make_protocol`, replacing the `plans` objects
with an ordered list of local plan paths:

```json
{
  "plans": ["block-01.json", "block-02.json", "block-03.json", "block-04.json"],
  "policy_a": "cache_only",
  "policy_b": "least_busy",
  "candidate_parameters": {
    "group_size": 8, "retained_spacing_bps": 2500, "max_advance_ns": 1000000000
  },
  "order_seed": 104729,
  "minimum_effect_microrps": 100000,
  "max_scheduling_lag_p95_ns": 10000000,
  "max_client_queue_p95_ns": 10000000,
  "model": "Qwen/Qwen3-8B",
  "source_revision": "REPLACE_WITH_40_LOWERCASE_HEX",
  "environment_sha256": "REPLACE_WITH_SHA256_PREFIX_AND_64_LOWERCASE_HEX",
  "reset_procedure_sha256": "REPLACE_WITH_SHA256_PREFIX_AND_64_LOWERCASE_HEX"
}
```

These numbers illustrate the format, not recommended experimental settings.
Use eight or more blocks for a design capable of passing this screen. Supply the
actual code revision and hashes of the retained environment/reset descriptions.
The tool checks declaration consistency, not their truth. Environment records
must cover serving-engine/model revisions, hardware/topology, router bounds and
other settings the timed client artifact does not observe.

```bash
PYTHONPATH=src python -m inferdrome.vllm_paired_comparison prepare \
  --config /private/study/config.json --output /private/study/protocol.json
```

For each protocol trial, use its block's base plan and token certificate. Generate
its timing with `make_timing(plan, group_size=1, retained_spacing_bps=10000,
max_advance_ns=0)` for baseline, or the frozen candidate parameters. The resulting
hash must match the trial's `timing_sha256`. Existing trial execution must opt
into `--timing` and `--correlate-requests`; it is not invoked by this tool. Keep
the full offered window and drain clear before the next trial, even if all
responses finish early. This step adds no GPU campaign or reset automation.

After collecting authorized runs, `inputs.json` has this shape (include every
trial in the protocol's order):

```json
{
  "schema": "inferdrome.vllm-router-paired-inputs.v1",
  "protocol_sha256": "COPY_FROM_PROTOCOL",
  "plans": ["block-01.json", "block-02.json", "block-03.json", "block-04.json"],
  "trials": [{
    "trial_id": "COPY_FROM_PROTOCOL",
    "timing": "trial-timing.json",
    "result": "trial-correlated-result.json",
    "ledger": "trial-router.jsonl",
    "token_certificate": "block-01-certificate.json",
    "execution": {
      "source_revision": "COPY_FROM_PROTOCOL",
      "environment_sha256": "COPY_FROM_PROTOCOL",
      "reset_procedure_sha256": "COPY_FROM_PROTOCOL",
      "reset_completed": true
    }
  }]
}
```

Paths resolve relative to each config/manifest. Fixture certificates may be
`null`; evaluation requires the matching certificate. Files are read locally,
without fetching URLs.

```bash
PYTHONPATH=src python -m inferdrome.vllm_paired_comparison compare \
  --protocol /private/study/protocol.json --inputs /private/study/inputs.json \
  --output /private/study/comparison.json

PYTHONPATH=src python -m inferdrome.vllm_paired_comparison verify \
  --protocol /private/study/protocol.json --inputs /private/study/inputs.json \
  --report /private/study/comparison.json
```

Prepare/compare refuse to replace existing outputs. Verify regenerates the full
report. `INELIGIBLE` writes an inspectable report and exits 2; `INCONCLUSIVE` is a
valid result and exits 0. Malformed inputs raise an error without publishing a
new report. Python callers use `compare(protocol, plans, inputs)`, with parsed
objects and `ledger_rows` replacing the CLI's ledger path.

## Evidence and release boundaries

Every report remains `EXPLORATORY_SEARCH_ONLY`, `evidence_eligible=false`,
`independent_execution=UNVERIFIED`, and `held_out_confirmation=NOT_IMPLEMENTED`.
Fixture results stay `SYNTHETIC_ONLY`; evaluation stays `LOCAL_MEASUREMENT_ONLY`.
Hashes and declared revisions do not authenticate GPU execution, environmental
control, independent resets, source authorship, chronology, or advance
registration. Reported client clocks provide a consistency check only.

Reports bind the protocol, measurements, certificates, timing checks, router
ledger contents and execution declarations. They omit prompts, endpoint URLs,
raw request IDs and source paths. Keep raw inputs private and review any intended
publication. The saved historical campaigns lack the new correlated timed
artifacts and cannot be retroactively relabeled as paired Breakpoint evidence.
No GPU experiments or new policy-performance result were produced for this step.
Frozen release schemas and the existing capacity/baseline report readers are
unchanged.
