# ADR 0006: Add a local read-only evidence dashboard

- Status: Accepted
- Date: 2026-08-07
- Scope: post-v0.1 dashboard and later

## Context

Inferdrome's CLI and portable evidence bundles provide the authoritative
measurement and verification path. They do not, by themselves, make it easy to
scan several runs, inspect one run's context, compare two compatible runs, or
understand why evidence was rejected.

A dashboard can improve that inspection workflow, but it can also weaken the
product boundary if it introduces a second metric implementation, silently
accepts malformed artifacts, mutates completed evidence, or presents a
performance delta as a customer acceptance verdict.

The v0.1 product charter deliberately excludes a web dashboard. This decision
adds a post-v0.1 product slice; it does not amend the v0.1 release scope or
definition of done. Implementation may proceed independently of the remaining
v0.1 release gates, but dashboard completion cannot be used to close them.

## Decision

Inferdrome will add a local, read-only evidence dashboard with four primary
views: Runs, Run detail, Compare, and Evidence. The accepted visual direction is
the balanced design: familiar cards and tables establish hierarchy, while
evidence status, provenance, and comparability remain visible without turning
the interface into a dense forensic console.

The dashboard is a projection of Inferdrome evidence, not a separate analytics
system:

1. Python bundle verification and deterministic reduction remain authoritative.
2. The browser receives bounded, typed display projections derived only after
   verification and recalculation.
3. The browser may format values and render charts, but it does not calculate
   authoritative measurements.
4. The dashboard does not modify, repair, reseal, delete, or supplement a
   completed bundle.

The initial dashboard discovers bundles beneath explicitly configured run
roots. It uses no database. An ephemeral cache keyed by immutable bundle digest
is permitted, but it is not a new system of record.

The local service binds to a loopback interface by default. A remotely exposed
or hosted dashboard, including its authentication, authorization, tenancy, and
network threat model, requires a later decision.

Invalid, unsafe, changed, or internally inconsistent bundles fail closed. The
dashboard may show a bounded rejection summary, but it does not expose their
measurements as usable evidence. Bundle selection is identifier-based beneath
configured roots; user-supplied arbitrary filesystem paths are not a dashboard
resource API.

Display projections are allowlisted and bounded by count, size, depth, and
pagination limits. Response-bearing native artifacts are not served directly
by default. Any displayed request or diagnostic content follows an explicit
redaction and sensitivity policy and never changes the preserved bundle bytes.

Comparison is pairwise in the initial dashboard. Before showing a delta, the
dashboard returns an explicit comparability result and its reasons. It checks
the relevant metric identity, definition, unit, population, aggregation,
quantile semantics, request plan, producer, model, and execution context. An
incomparable pair has no calculated delta. A pair with declared context changes
may show matching metrics alongside those changes, without claiming a
controlled experiment.

Deltas are signed arithmetic differences with neutral visual treatment. An
increase or decrease is not labeled better or worse until metric directionality
is represented by a reviewed, versioned, frozen contract. Green and red are not
used to imply performance preference; validity and eligibility states remain
separate from performance values.

The dashboard does not issue `PASS`, `FAIL`, or `NOT_PROVEN`. Those remain
ExitSpec-owned outcomes. If an ExitSpec receipt is displayed, it is presented as
an externally produced artifact with its owner and anchored bundle digest, not
as an Inferdrome calculation.

Real-GPU, attached-endpoint, and synthetic bundles use the same verification,
projection, and view path. Adding a genuine GPU bundle requires no UI-specific
ingestion mode or metric implementation.

The detailed product contract is defined in
[DASHBOARD.md](../DASHBOARD.md).

## Consequences

- Dashboard values stay traceable to the same records and definitions as CLI
  recalculation.
- A bundle can be inspected without being imported into a mutable catalog.
- The initial product remains local and single-user rather than implying a
  hosted security model.
- Some native evidence remains intentionally unavailable through the browser.
- Comparison UX must explain incompatibility instead of always producing a
  number.
- The dashboard cannot claim which configuration is preferable without an
  additional directionality contract or an ExitSpec evaluation.
- Statistical trial sets, uncertainty estimates, and causal explanations remain
  separate roadmap work.

## Rejected alternatives

### Calculate dashboard metrics in the browser

Rejected because it would create a second reducer whose results could drift
from sealed measurements and offline recalculation.

### Import every run into a database first

Rejected for the initial dashboard because it introduces mutable state,
migration, and reconciliation questions without being necessary to inspect
portable immutable bundles.

### Let the dashboard repair or annotate bundles

Rejected because completed bundles are immutable evidence. User annotations, if
added later, must live outside the bundle and have a separate identity model.

### Treat every numerical delta as an improvement or regression

Rejected because metric directionality is not currently a frozen domain
contract, and a numerical change is not an acceptance verdict or causal claim.

### Build a hosted dashboard first

Rejected because remote exposure introduces authentication, authorization,
privacy, and tenancy requirements that are outside this slice's trust model.
