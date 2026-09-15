# Read-only evaluation reports in the dashboard

The **Evaluations** view displays retained PR3 study and PR4 prefix-cache
`report.json` outputs in the existing local Inferdrome dashboard. A private
catalog explicitly names up to eight files and their retained SHA-256 digests.
There is no automatic discovery, browser upload, experiment execution, report
generation, source replay, tokenizer loading, or endpoint call in this viewer.

**Pinned report; source inputs not replayed.** Matching a configured digest and
validating a closed report contract establish pinned-byte integrity and internal
consistency. They do not prove reducer authorship, authentic source execution,
independent repetitions, GPU behavior, cache treatment, or acceptance. An
operator who changes both a report and its pin can supply a different coherent
report; this connector cannot authenticate that report's origin.

## Configure retained reports

First produce and review reports using the existing offline commands documented
in [Evaluation studies](EVALUATION_STUDIES_V1.md) and
[Prefix-cache experiments](EVALUATION_PREFIX_CACHE_V1.md). Preserve the exact
canonical `report.json` bytes, including the trailing newline. The viewer does
not accept reserialized JSON, Markdown, raw trial results, or archives.

Record the expected digest when reviewing the retained report, for example:

```bash
shasum -a 256 /absolute/private/study-report/report.json
```

Prefix the retained 64 hexadecimal characters with `sha256:` in a private
catalog. The pin must be retained independently of later viewer reads; do not
automatically update it when an input changes. Create `catalog.json` in an
operator-owned directory with mode `0700`, and give the catalog mode `0600`.
Report directories and files must retain the private ownership/permissions of
the existing report writer (no group/other access). Symlinks, hardlinks, and
special files are rejected.

```json
{
  "schema_version": "inferdrome.dashboard-evaluation-reports-catalog.v1",
  "entries": [
    {
      "kind": "STUDY",
      "report_path": "/absolute/private/study-report/report.json",
      "expected_sha256": "sha256:<retained 64 hexadecimal characters>"
    },
    {
      "kind": "PREFIX_CACHE",
      "report_path": "/absolute/private/cache-report/report.json",
      "expected_sha256": "sha256:<retained 64 hexadecimal characters>"
    }
  ]
}
```

Replace the two digest placeholders with actual reviewed digests. The catalog
contains only these fields; each report path must be absolute and end with
`report.json`. Duplicate paths or public identities are withheld. Missing or
unsupported fields do not receive inferred defaults.

Start the existing dashboard with the additional catalog flag:

```bash
python -m inferdrome dashboard \
  --runs-root /absolute/private/runs \
  --evaluation-reports-catalog /absolute/private/catalog/catalog.json
```

The existing `--keyring` option applies the same `dashboard:read` authentication
to both evaluation endpoints. Serving remains on loopback. Navigate to
**Evaluations**, filter by report kind, and open a report. Without the catalog
flag, the evaluation index is empty. The existing Runs refresh remains scoped to
runs; **Refresh reports** / **Refresh report** reload the evaluation source.

## What the app displays

Study details keep the four new evaluation policies, foreground/background
populations, fixed offered windows, goodput and SLO fractions, outcome counts,
latency sample populations, and matched contrasts from the authoritative report.
The trial selector exposes each returned trial; missing/aborted/not-run trial
coverage remains visible separately. Publication, decision, and dispatch
recovery distinguish observed zero from censored, unobserved restoration, and
not-applicable values. A scheduled restore is not an observed recovery or proof
of actual overload.

Prefix-cache details select a block and a cell among S0/S1/U0/U1. They show the
reported shared/unique and declared off/on conditions, fixed A/B assignment
counts, per-cell populations, and the three supplied matched contrasts. One
block is a low-replication rehearsal/pilot. One/four blocks have no interval;
only eight complete eligible blocks permit the existing descriptive interval.
Output-length diagnostics describe the reported successful populations, not
matched generated outputs. Prefix-block overlap is potential reuse, not hits.

The frontend performs selection and presentation only. Supplied numeric strings,
counts, quantiles, contrasts and nulls remain authoritative; it does not pool
requests, calculate new rates, rerun a bootstrap, infer a winner, or invent an
effect percentage. Nanosecond values retain their stated origin and population.
Unavailable values never become zero. Failed/cancelled requests remain in the
offered denominator. A valid incomplete report can contain invalid source-cell
entries without projecting measurements for those cells.

Report completion and comparison availability are separate. A completed cache
report can have a suppressed comparison. A source evidence class of
`LOCAL_MEASUREMENT_ONLY` does not establish cloud/GPU execution or success;
zero returned-record coverage explicitly says **No returned measurements**.
Synthetic reports remain labeled **Synthetic only**.

All projections retain `source_replay: NOT_PERFORMED`, runtime/cache verification
`UNVERIFIED`, `evidence_eligible: false`, and `tokenizer_reverified_here: false`.
Tokenizer verification is a status reported by the retained report, not a
verification performed by this viewer. No local tokenizer package or model
snapshot is needed to inspect a supported retained report. A source commit or
reducer-build identity absent from the report is not inferred from this app.

## HTTP and resource boundaries

The additive read-only endpoints are:

- `GET /api/v1/evaluation-reports`
- `GET /api/v1/evaluation-reports/{report_id}`

IDs are generated from report kind and pinned digest, not source paths. Public
responses use generated report/block/trial labels and explicit allowed fields.
Raw prompts/output/token IDs, private paths/origins, preparation text,
credentials, server logs, arbitrary exception messages, and EXTERNAL_ONLY
archive metadata are never returned. Aggregate token counts have explicit
server-reported or prefix-potential semantics. The viewer reads no archives.

The catalog is limited to 64 KiB/eight entries. Each report is limited to the
existing 4 MiB writer ceiling, depth 32, one million significant JSON tokens,
bounded integer/decimal strings, and the current report cardinalities (256 study
trials/64 blocks or 32 cache cells/eight blocks). Unknown nested fields,
noncanonical JSON, invalid counts, unsupported states, or non-null suppressed
results are rejected, never repaired.

One active evaluation scan admits at most eight report work units and 32 MiB of
report input plus catalog, with a 30-second cooperative deadline. The index is
limited to 64 KiB, each sanitized detail to 4 MiB, and retained encoded detail
sizes to 32 MiB. Raw reports are processed one at a time. These encoded limits
are not a hard Python RSS limit; the deadline is cooperative between bounded
operations, not preemption inside a JSON parse.

Every read rechecks the currently configured files. Deletion, replacement,
unsafe nodes, or changed bytes invalidate detail; no cached success is served.
Withheld index entries expose fixed reason codes (`CONFIGURATION_INVALID`,
`REPORT_UNAVAILABLE`, `DIGEST_MISMATCH`, `REPORT_INVALID`, `PROJECTION_LIMIT`).
An invalid outer catalog or exhausted work capacity produces a generic 503;
unknown or no-longer-valid report identities produce a generic 404. Retry
buttons repeat read requests only. Refresh is disabled while an evaluation
request is pending. Only an occupied scan slot returns the evaluation-specific
busy header and `Retry-After: 1`: the evaluation client can retry that response
at most four times, waiting one second each time. Navigation and authentication
changes cancel pending retries, and every admitted attempt rechecks the source.
Other errors are not automatically retried; continued contention shows the
explicit error and retry action after the bounded attempts. Auth checks precede
source work, and API responses preserve the app's no-store and response-security
headers.

This connector is additional product integration. The frozen three-policy,
six-request routing views and their evidence contracts are unchanged. PR5
real-evidence closure still requires separately approved experiments and
verified teardown. This view provides no GPU readiness, capacity, cost,
cache-hit, causal performance, or customer-acceptance claim.
