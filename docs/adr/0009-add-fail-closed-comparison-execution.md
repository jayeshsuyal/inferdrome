# ADR 0009: Add fail-closed controlled-comparison execution

- Status: Accepted
- Date: 2026-08-08
- Scope: post-v0.1, v0.2 third vertical slice

## Context

ADR 0008 freezes a controlled-comparison design and independently verifies its
result, but operators still have to translate the schedule into individual run
commands, preserve arm selection, create two exact Trial Sets, and pass three
retained digests into result creation. That manual path is useful as a protocol
surface but creates avoidable opportunities for ordering, identity, and
finalization mistakes.

Automation must not silently weaken the frozen design or upgrade its assurance.
In particular, it must not reread changing source paths between slots, reuse a
schedule hole, retry a failed assigned run under the same or a replacement ID,
trust a bundle merely because its own manifest verifies, or leave a public
artifact identity poisoned after a crash during publication.

## Decision

Inferdrome adds `comparison-plan execute` as an orchestration layer over the
existing public contracts. No public schema is added or changed.

The executor:

- requires the externally retained comparison-plan digest;
- resolves and verifies both arm sources against the frozen plan before a
  planned run is reserved;
- executes from private snapshots of the exact source and workload bytes read
  during that verification;
- holds one nonblocking advisory lock beneath the runs root, keyed by plan ID
  and digest, through final result reverification;
- follows only the explicit preallocated schedule;
- reuses only an exact leading prefix of independently verified `COMPLETE`
  workspaces;
- binds each reusable bundle to its workspace metadata and exact frozen source,
  resolved-spec, request-plan, and workload bytes;
- creates or reuses the two exact planned `RETROSPECTIVE` Trial Sets;
- creates or reuses one deterministic comparison-result identity; and
- independently reverifies the result before returning success.

There is no retry or replacement policy in v1. A `FAILED`, `INTERRUPTED`,
invalid, or abandoned nonterminal planned workspace consumes its preallocated
attempt and blocks the comparison. Cancellation is resumable only when it is
observed before reservation or between completed boundaries.

Trial Set, plan, and result descriptor publication uses private unique staging.
The complete descriptor is written and fsynced, the file and directory are
made read-only, and the directory is published without replacement before the
artifact root is fsynced. Private orphan stages are excluded from artifact
discovery. Existing public destinations are accepted only through ordinary
independent verification; unrecognized partial public directories are never
repaired in place.

The dashboard derives operational progress from the immutable plan and current
run workspaces. Its statuses and per-slot states are private projection fields,
not a public evidence contract. A nonterminal workspace does not prove that an
executor is alive.

## Claim boundary

Execution automation changes convenience and failure handling, not scientific
or trust claims:

- `PREDECLARED` remains `OPERATOR_ATTESTED`;
- local timestamps remain internal consistency rather than trusted chronology;
- generated Trial Sets remain `RETROSPECTIVE` and `DESCRIPTIVE_ONLY`;
- synthetic evidence remains `SYNTHETIC_ONLY`;
- `COMPARABLE` remains `POINT_ESTIMATE_ONLY` over the observed v1 allowlist;
- an `INCOMPARABLE` result can be a successful pipeline finalization; and
- ExitSpec continues to own customer acceptance.

The advisory locks coordinate cooperating local Inferdrome processes only.
They are not distributed locks, execution attestation, or protection against a
hostile filesystem writer. Direct low-level run commands remain available and
can consume a planned ID outside the executor; the next executor invocation
will verify that state and fail closed.

## Consequences

- The normal controlled-comparison workflow becomes one command after plan
  creation while preserving every immutable evidence boundary.
- A crash after a complete slot can resume without rerunning that slot.
- A crash or cancellation during a reserved attempt intentionally requires a
  new plan rather than a hidden retry.
- Workspace and bundle verification become one bound decision instead of two
  independently valid but unassociated checks.
- Publication failures cannot reserve public Trial Set or result identities
  before complete artifacts are visible.
- The dashboard can explain local progress and blockers without becoming a
  write surface or a twelfth schema.

## Rejected alternatives

### Retry failed slots or allocate replacement run IDs

Rejected because either behavior changes the predeclared membership and can
introduce post-assignment selection.

### Resume any set of completed planned runs

Rejected because accepting holes would violate the observed frozen schedule.

### Execute by rereading each original source path for every slot

Rejected because source or workload mutation could change later runs after the
plan check.

### Store mutable executor state as evidence

Rejected because a progress file would create another trust and recovery
surface. Progress is instead derived from existing plan and workspace facts.

### Repair partial public descriptor directories

Rejected because repair cannot reliably distinguish an interrupted trusted
publisher from unrelated or hostile bytes. Private staging plus no-replace
publication keeps recovery unambiguous.
