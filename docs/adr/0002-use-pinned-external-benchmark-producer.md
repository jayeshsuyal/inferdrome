# ADR 0002: Use a pinned external benchmark producer first

- Status: Accepted
- Date: 2026-08-05
- Scope: v0.1

## Context

Load scheduling, streaming protocol handling, concurrency control, warmups, and
request timing are substantial systems problems. Mature benchmark producers
already implement these behaviors and have their own user and test ecosystems.

Building a custom load generator in v0.1 would expand scope and make the first
evidence format depend on unproven measurement code.

## Decision

Inferdrome v0.1 invokes exactly one pinned `vllm bench serve` producer version
as an external process. Inferdrome wraps and verifies that producer; it does not
reimplement its load-generation loop.

The producer is supported only through a version-specific capability matrix,
native golden fixtures, a structural output fingerprint, and explicit metric
semantics.

The exact supported version is selected by PR 0.5, not by this ADR.

The upstream interface under evaluation is documented in the
[vLLM benchmark CLI reference](https://docs.vllm.ai/en/latest/cli/bench/serve/).

## Consequences

- Inferdrome inherits upstream measurement semantics and limitations.
- Producer-native TTFT or success semantics must be named honestly.
- Unsupported fields remain unavailable.
- Invocation and parsing work without importing vLLM internals into Inferdrome.
- Normal pull-request tests parse stored fixtures and do not require a GPU.
- Supporting another producer version is an explicit compatibility change.

## Rejected alternatives

### Build an Inferdrome-native load generator immediately

Rejected for v0.1 because it obscures the evidence-pipeline contribution and
creates a much larger validation burden.

### Support several benchmark producers from the start

Rejected because breadth would delay proving one complete evidence path and
prematurely generalize the canonical schema.
