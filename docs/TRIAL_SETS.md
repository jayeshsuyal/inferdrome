# Inferdrome descriptive Trial Sets

Status: **Frozen v0.2 first-slice contract**

Decision record:
[ADR 0007](adr/0007-add-immutable-descriptive-trial-sets.md)

Release scope: **Post-v0.1, v0.2 first vertical slice**

An Inferdrome Trial Set is an immutable grouping of repeated, completed runs of
one execution condition. It answers, "How much did the same run-level
measurement vary across these repetitions?" without pooling request records or
claiming that the repetitions form a predeclared controlled experiment.

Every Trial Set is retrospective. It is created from already completed bundles,
and its dashboard projection is labeled `RETROSPECTIVE` and
`DESCRIPTIVE_ONLY`. A creation timestamp records when the aggregate was made;
it does not prove that a hypothesis, repeat count, outcome, or exclusion policy
was fixed before execution.

## Product boundary

This slice provides:

- one strict public `inferdrome.trial-set.v1` descriptor;
- immutable member references bound by both `run_id` and `bundle_digest`;
- offline verification and authoritative recalculation of every member;
- same-condition membership checks;
- deterministic, equal-per-run descriptive statistics;
- explicit environment-drift disclosure;
- CLI create, verify, and summarize commands; and
- read-only Trial Sets index and detail routes in the local dashboard.

This slice does **not** provide:

- a predeclared controlled-comparison plan or baseline/candidate comparison;
- confidence intervals, p-values, significance, power, or confidence claims;
- causal explanations, winner labels, recommendations, or directionality;
- prefix-caching configuration or a prefix-caching experiment;
- request pooling across runs;
- telemetry-backed explanations of why a measurement changed; or
- ExitSpec-owned `PASS`, `FAIL`, or `NOT_PROVEN` outcomes.

Those boundaries apply even when all member bundles are valid and their values
look consistent.

## Authority and data flow

The normative flow is:

```text
ordered run IDs selected by an operator
        ↓
bounded run lookup beneath one explicit runs root
        ↓
offline bundle verification + deterministic recalculation
        ↓
same experiment / fingerprint / metric definitions / reducer checks
        ↓
canonical immutable inferdrome.trial-set.v1 descriptor
        ↓
domain-separated trial-set digest emitted out of band
        ↓
equal-per-run descriptive summary and read-only dashboard projection
```

The Trial Set is an aggregate outside its member evidence bundles. Creating,
verifying, summarizing, discovering, or displaying it never changes a completed
bundle and never adds the descriptor to a bundle manifest.

## Public descriptor

The reference layout is:

```text
trial-sets/
└── trial-set-<32 lowercase hex>/
    └── trial-set.json
```

`trial-set.json` is canonical RFC 8785 JSON matching the closed Draft 2020-12
schema `schemas/public/v1/trial-set.schema.json`. Unknown fields fail closed.

| Field | Contract |
|---|---|
| `schema_version` | Exact literal `inferdrome.trial-set.v1` |
| `trial_set_id` | `trial-set-` plus 32 lowercase hexadecimal characters |
| `experiment_id` | One shared experiment identity |
| `title` | Bounded human label; never a membership input |
| `hypothesis` | Optional bounded human context; never proof of predeclaration |
| `created_at` | Aware timestamp for aggregate creation |
| `design_status` | Exact literal `RETROSPECTIVE` |
| `membership_policy` | Exact literal `same_execution_fingerprint_v1` |
| `request_population_policy` | Exact literal `separate_per_run_v1` |
| `statistical_unit` | Exact literal `run` |
| `weighting` | Exact literal `equal_per_run` |
| `execution_fingerprint` | Exact fingerprint shared by every member |
| `metric_definitions_digest` | Exact metric-definition set shared by every member |
| `reducer_version` | Exact reducer version shared by every member |
| `members` | Ordered list of 2 through 100 immutable member references |

Each member contains:

```text
repetition_index
run_id
bundle_digest
```

Repetition indices are contiguous, zero-based, and ordered. Run IDs and bundle
digests are each unique inside one Trial Set. Equal numerical results from two
different, valid bundles remain two repetitions; value equality is not
deduplication.

The descriptor intentionally does not require equal `request_plan_digest`
values. Each request plan includes run-specific identity, so repeated runs have
distinct request populations and may have distinct request-plan bytes while
sharing the same measurement-affecting execution fingerprint.

## Membership and verification invariants

A Trial Set is valid only when all of these checks pass:

1. The descriptor contains between 2 and 100 members.
2. Every member resolves by validated run ID beneath the configured runs root;
   no member supplies an arbitrary filesystem path.
3. Every referenced bundle passes the ordinary bounded offline verifier and
   authoritative deterministic recalculation.
4. The recalculated run identity and bundle digest match the member's retained
   `run_id` and `bundle_digest` exactly.
5. All members share the descriptor's `experiment_id`,
   `execution_fingerprint`, `metric_definitions_digest`, and `reducer_version`.
6. Member order and identity are unambiguous and duplicate-free.
7. The descriptor is canonical, read-only, and unchanged across verification.

One failed, missing, mutated, replaced, or incompatible member invalidates the
Trial Set. Verification does not silently remove a member, reduce the repeat
count, repair evidence, or serve a stale aggregate.

The execution fingerprint is necessary but not sufficient to prove a
controlled environment. Observed GPU, CUDA, driver, producer-distribution, or
client-environment fields can differ without becoming a declared treatment.
The dashboard therefore compares allowlisted environment projections across
members and reports `CONSISTENT` or `DRIFT_DETECTED` plus the changed field
names. `CONSISTENT` means no difference was found in the projected fields; it
does not prove that every material environmental fact was observed.

Environment drift does not get relabeled as an experimental variable. The
Trial Set remains retrospective and descriptive, and it cannot support a
controlled or causal claim.

## Trial-set digest

The trial-set digest is SHA-256 over the exact canonical descriptor bytes under
this domain separator:

```text
inferdrome:trial-set-v1\0
```

It is emitted out of band and is not embedded in `trial-set.json`, avoiding a
circular digest dependency. Operators and independent consumers should retain
it externally and pass it back through `--expected-digest` when verifying or
summarizing.

The digest anchors exact descriptor bytes. Like the bundle digest, it does not
prove authorship, execution truth, trusted timing, or that the Trial Set was
declared before its runs occurred.

## Descriptive statistics

Statistics operate on run-level measurement scalars. For each compatible
metric-and-aggregation key:

- every member retains its own value and sample count;
- one available scalar from one run receives one unit of weight;
- a run with more requests does not receive more weight;
- canonical request records from different runs are never concatenated;
- unavailable values remain visible and never become zero; and
- `available_run_count` remains distinct from the total member count.

The versioned summary method is
`per_run_scalar_sample_variation_v1`. It reports minimum, median, maximum,
arithmetic mean, span, and sample standard deviation over available run-level
values. Calculations use Decimal arithmetic and decimal-half-even rounding to
six places. Sample standard deviation is unavailable when fewer than two values
are available; it is never represented as zero merely because one value exists.

These are descriptive summaries, not uncertainty estimates. The UI and CLI do
not generate confidence, significance, stability, superiority, regression,
causality, or acceptance language from them.

## CLI

Create a Trial Set by repeating `--run` in the intended repetition order:

```bash
inferdrome trial-set create \
  --run run-0123456789abcdef0123456789abcdef \
  --run run-11111111111111111111111111111111 \
  --title "Repeated serving condition" \
  --hypothesis "Run-level measurements should be inspected for variation" \
  --runs-root runs \
  --trial-sets-root trial-sets
```

Creation verifies and recalculates every member before publishing the immutable
descriptor. It returns the Trial Set ID, path, member count, and out-of-band
digest.

Verify the descriptor, every member, and an externally retained digest:

```bash
inferdrome trial-set verify \
  trial-sets/trial-set-0123456789abcdef0123456789abcdef \
  --runs-root runs \
  --expected-digest "$TRIAL_SET_DIGEST"
```

Produce the deterministic descriptive projection:

```bash
inferdrome trial-set summarize \
  trial-sets/trial-set-0123456789abcdef0123456789abcdef \
  --runs-root runs \
  --expected-digest "$TRIAL_SET_DIGEST"
```

`summarize` identifies its output as `DESCRIPTIVE_ONLY`, names the separate
request-population policy and summary method, exposes every run-level point,
and reports `EQUAL_PER_RUN` weighting.

## Dashboard

Start the local service with explicit run and Trial Set roots:

```bash
inferdrome dashboard \
  --runs-root runs \
  --trial-sets-root trial-sets
```

The read-only browser routes are:

```text
/trial-sets
/trial-sets/:trialSetId
```

The corresponding GET-only API routes are:

```text
GET /api/v1/trial-sets?limit=100&cursor=...
GET /api/v1/trial-sets/{trial_set_id}
```

The index shows verified Trial Sets and bounded rejection categories. The detail
view shows the immutable definition, `RETROSPECTIVE` and `DESCRIPTIVE_ONLY`
labels, member runs, environment drift, and backend-derived run-level
variation. Each plotted point represents one run and repeats its exact value in
accessible member data. The browser does not aggregate requests or recalculate
authoritative statistics.

Collection pagination is bounded and snapshot-bound. A stale cursor fails
closed and requires a fresh load.

## Security and resource bounds

- A descriptor contains 2 through 100 members and is at most 262,144 bytes.
- Trial Set discovery examines at most 200 direct root entries.
- Collection limits range from 1 through 200 entries; cursors are opaque,
  snapshot-bound, and at most 128 characters.
- IDs and direct-child lookup replace arbitrary path parameters.
- A Trial Set directory has an exact one-file inventory. Its descriptor is
  opened directory-relative with no-follow semantics and must be a bounded,
  owner-read-only regular file with exactly one hard link.
- Initial path, opened handle, and final path identities for both the directory
  and descriptor must agree. Symlinks, hardlinks, extra siblings, duplicate
  IDs, unsafe entries, noncanonical JSON, writable nodes, oversized data,
  replacement races, and stale snapshots fail closed.
- Verification reads each bundle through the existing bounded evidence path;
  Trial Set APIs do not serve raw bundle files or response-bearing content.
- Cached projections are disposable and keyed by immutable digest. Refresh
  revalidates members before a result remains usable.

## Controlled-comparison consumer

The second v0.2 slice now defines separate immutable plan and result contracts
in [CONTROLLED_COMPARISONS.md](CONTROLLED_COMPARISONS.md). It consumes one exact
Trial Set per arm, verifies every retained member and digest, and requires the
ordered membership to equal the run IDs frozen in the plan.

This does not change Trial Set semantics: a Trial Set remains retrospective and
descriptive when used alone. It does not become evidence of pre-run chronology,
assignment, control, uncertainty, or causality. Prefix caching remains
unsupported because it lacks a reviewed typed execution control and
fingerprint-capable contract.

Verification also derives a non-schema comparison-authority projection. A set
is `CONTROLLED_OUTCOME_ELIGIBLE` only when every member is customer-eligible and
all members share one non-null ExitSpec contract identity. Otherwise it is
explicitly `DESCRIPTIVE_ONLY_NON_AUTHORITATIVE`, with closed reason codes, and
cannot contribute outcome arithmetic to a controlled comparison. This
projection leaves the frozen Trial Set v1 bytes unchanged.
