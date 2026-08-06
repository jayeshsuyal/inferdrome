# ADR 0005: Freeze public schemas after the capability spike

- Status: Accepted
- Date: 2026-08-05
- Scope: v0.1

## Context

The proposed canonical record contains observations that may not exist in the
pinned producer's detailed output. Examples include HTTP status, finish reason,
raw streaming events, retry attempts, scheduled offsets, and a complete warmup
ledger.

Freezing schemas before inspecting real producer artifacts would either force a
custom sensor into v0.1 or encourage guessed and mislabeled data.

## Decision

The product charter and architecture boundaries are frozen in PR 0. Exact public
schema fields are frozen only after PR 0.5 executes one pinned vLLM version and
publishes a field-level capability matrix and golden fixture.

Every proposed field is classified as `OBSERVED`, `DERIVABLE`,
`CONFIGURED_ONLY`, or `UNAVAILABLE`. Canonical observation fields use only the
first two categories.

## Consequences

- Schema implementation begins one phase later.
- The v1 format is grounded in actual native artifacts.
- Configured intent remains distinguishable from measured outcome.
- Some attractive canonical fields may be omitted from v0.1.
- Future instrumentation can add new schema versions without falsifying old
  evidence semantics.

## Outcome

PR 0.5 completed on 2026-08-05 with vLLM `0.26.0`, a committed native client
fixture, and a source- and execution-verified capability matrix. PR 1 schema
work completed on 2026-08-05 with eight public Draft 2020-12 schemas, strict
domain models, and cross-validator conformance vectors. HTTP status, finish
reason, raw streaming events, retry
attempts, exact scheduled offsets, request-level warmup rows, terminal
per-request latency, and exact achieved concurrency remain unavailable in the
pinned native output and cannot become canonical observations in v0.1.

## Rejected alternatives

### Freeze the originally proposed request record immediately

Rejected because it overcommits to data the producer may not serialize.

### Populate missing fields from configuration or error text

Rejected because configured values and heuristic parsing are not equivalent to
observations.
