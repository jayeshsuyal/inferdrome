# Deterministic reduction and fake adapter

Status: **Implemented for PR 3**

Implementation date: **2026-08-05**

PR 3 implements the frozen metric definitions, exact numeric primitives,
deterministic reducer, and synthetic producer used by normal pull-request tests.
It makes no acceptance decision.

## Reducer boundary

The reducer consumes only:

```text
completed execution record
ordered canonical request records
exact metric-definition set v1.0.0
```

It performs no network access, reads no host state, uses no wall clock, and
does not inspect native text to invent missing observations. It requires record
cardinality to match configured measured requests, contiguous sequence order,
one run ID, one producer-semantics tuple, and a producer compatible with the
execution-window definition.

The output binds itself to the SHA-256 of canonical request-record JSONL, the
SHA-256 of canonical execution JSON, and the domain-separated digest of the
exact metric-definition artifact.

## Populations

Each measured request belongs to exactly one reducer outcome population:

```text
success   status == SUCCESS
failure   status == FAILED or ANOMALOUS_EMPTY_STREAM
```

An endpoint failure is therefore retained in the measured population. It does
not automatically turn a completed harness execution into a failed run.

Latency populations contain successful requests with observed TTFT. The public
request-record invariant already requires TTFT for `SUCCESS`, but the reducer
still names this narrower population explicitly.

## Exact formulas

For `N` measured requests, `S` successes, `F = N - S` failures, measurement
window `W` nanoseconds, and successful output-token sum `O`:

```text
measured_request_count                 N
successful_request_count               S
failed_request_count                   F
error_rate                             F / N
attempted_request_throughput_per_s      N × 1,000,000,000 / W
successful_request_throughput_per_s     S × 1,000,000,000 / W
output_token_throughput_per_s           O × 1,000,000,000 / W
```

For each successful request:

```text
ttft_ns                        observed canonical TTFT
last_choices_event_span_ns     ttft_ns + sum(itl_ns)
```

`last_choices_event_span_ns` ends at the last non-empty-`choices` event. It is
not terminal E2E latency and is not TPOT.

The reducer emits mean, p50, p95, and p99 for both implemented latency metrics.
Nearest-rank is one-indexed:

```text
rank = ceil(percentile / 100 × sample_count)
value = sorted_values[rank - 1]
```

Counts and nearest-rank latency quantiles remain integers. Means, ratios, and
rates are computed from integers with arbitrary-precision decimal arithmetic,
rounded half-even to six places, and serialized as fixed decimal strings.
Binary floating point never enters canonical output.

If the success population is empty, TTFT and choices-span aggregates are
omitted. They are never written as zero. Successful request and output-token
throughput remain mathematically defined as `0.000000` over a positive measured
window. The seven producer observations excluded by the capability spike remain
listed explicitly as unavailable.

## Synthetic producer

`inferdrome_fake` is a deterministic producer and adapter for tests. It emits:

- a row-oriented `inferdrome.fake-native.v1` native artifact;
- one canonical request record per planned request;
- a completed synthetic execution record; and
- a canonical structural fingerprint of its native schema.

Default observations are deterministic, and tests may inject an exact script of
successes, failures, and anomalous empty streams. Every native artifact contains
both:

```text
execution_mode: synthetic_fixture
evidence_eligibility: SYNTHETIC_ONLY
```

Canonical records independently identify `inferdrome_fake`; the producer never
impersonates vLLM. The fake adapter intentionally emulates the pinned vLLM
canonical token and choices-event semantics so the same reducer path is tested.
That compatibility does not make synthetic evidence customer-eligible.

Canonical response-retention policy controls only the normalized copy. The
fake native artifact still preserves its response content, mirroring the rule
that retention settings cannot rewrite native producer output.

## Golden artifacts

The committed fixture at [`tests/fixtures/fake/v1`](../tests/fixtures/fake/v1)
uses a fixed run ID and UTC start time with one success and one failure. It
contains native output, canonical JSONL records, execution evidence, metric
definitions, measurements, and an accidental-drift checksum manifest.

The engineering gate regenerates the complete fixture in memory and requires
byte-for-byte equality. A separate integration test parses every artifact,
recalculates measurements, verifies source hashes and schema fingerprints, and
requires exact output bytes.

```bash
PYTHONPATH=src python scripts/generate_fake_golden.py
PYTHONPATH=src python scripts/generate_fake_golden.py --check
INFERDROME_PYTHON=.venv/bin/python ./scripts/engineering_gate.sh
```
