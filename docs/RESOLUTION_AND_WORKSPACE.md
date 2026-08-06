# Resolution and run workspace

Status: **Implemented for PR 2**

Implementation date: **2026-08-05**

PR 2 turns a user-authored YAML file and custom JSONL workload into immutable,
fully resolved execution inputs. It does not contact an endpoint, launch a
producer, or mutate a completed evidence bundle.

## Source format

The authoring format is `inferdrome.source-experiment.v1`. It is intentionally
separate from the fully resolved public `inferdrome.experiment.v1` contract.
The source model permits documented defaults; the resolved model does not.

A runnable synthetic example is
[`examples/fake-smoke.yaml`](../examples/fake-smoke.yaml). Workload paths are
safe relative paths resolved from the source file's directory. Absolute paths,
dot segments, symlinks, non-regular files, duplicate YAML keys, YAML aliases,
unknown fields, non-UTF-8 input, and oversized input fail closed.

The custom workload is UTF-8 JSONL with exactly one object per non-empty line:

```json
{"prompt":"One non-empty prompt"}
```

Each object must contain only `prompt`. Duplicate JSON keys, non-finite numbers,
blank lines, non-text prompts, and oversized lines are rejected. The resolver
reads the workload once, hashes those exact bytes, parses those same bytes, and
persists those bytes in the workspace. It never hashes one read and builds the
request plan from a later read.

## Strict mode

Strict mode is the default. It requires:

- an expected exact-byte workload SHA-256 digest;
- exact model and tokenizer revisions for an attached vLLM target;
- the pinned vLLM `0.26.0` producer and adapter contract;
- bounded total runtime and measured-request count values; and
- source/target and execution-mode compatibility.

`strict=False` may calculate a missing workload digest and permit unknown
attached-target revisions. The resolved environment remains incomplete; the
resolver never upgrades unknown provenance to verified provenance.

Endpoint URLs accept `http` or `https` but reject user information, query
strings, and fragments. Resolver errors do not echo rejected URL values, which
keeps a secret-bearing input out of normal error output.

## Resolution outputs

One call produces a frozen `ResolutionResult` containing:

- the exact source and workload bytes read;
- a strict `inferdrome.experiment.v1` model and canonical RFC 8785 bytes;
- an ordered `inferdrome.request-plan.v1` model and canonical bytes;
- `source_spec_digest` over exact source bytes;
- a measurement-affecting `execution_fingerprint`; and
- `request_plan_digest` over the canonical plan.

The execution-fingerprint projection includes producer, target, workload hash
and generation controls, traffic, and measurement semantics. It excludes the
human title, hypothesis, source path, response-retention policy, and ExitSpec
linkage. Changing presentation metadata therefore does not make otherwise
equivalent runs incomparable.

The request plan selects the first `measured_requests` prompts in file order,
matching the pinned adapter's no-shuffle invocation. IDs are derived as:

```text
canonical request ID   req-00000000, req-00000001, ...
producer request ID    <run-id>-0, <run-id>-1, ...
```

`prompt_content_policy: include` embeds prompt text and yields `FULL`
replayability. `hash_only` stores only each UTF-8 digest and forces `LIMITED`
replayability.

## Reserved workspace

`RunWorkspace.reserve()` atomically claims one `run-<32 hex>` directory and
never reuses an existing reservation:

```text
runs/<run-id>/
├── inputs/
│   ├── experiment.original.yaml
│   ├── workload.source.jsonl
│   ├── experiment.resolved.json
│   └── request-plan.json
└── control/
    ├── resolution.json
    ├── state.json
    ├── state.lock
    └── events/
        ├── 00000000-CREATED.json
        └── ...
```

Input files are created exclusively with read-only owner permissions. Their
exact sizes and hashes are recorded, and every state transition rechecks them.
A writable, missing, symlinked, resized, or changed input blocks progress.
Later bundle construction copies verified bytes into a separate staging area;
control files do not silently become public evidence artifacts.

State changes hold an operating-system file lock, validate the frozen state
graph, append one canonical immutable event, fsync it, and atomically replace
the current snapshot. Reopening a workspace validates the complete event chain,
filenames, predecessors, timestamps, canonical bytes, current snapshot, and
frozen inputs.

`COMPLETE` requires `IntegrityStatus.VALID`; the bundle writer and offline
verifier own that authorization path. `FAILED` means harness failure, while
measured request errors remain observations inside an otherwise successful
lifecycle.

## Cancellation scaffold

The cancellation token is thread-safe and first-writer-wins across user,
signal, and deadline requests. Process shutdown first requests graceful
termination, waits for a positive finite bound, escalates to kill, and waits for
a second positive finite bound. An unkillable process fails explicitly rather
than allowing an unbounded wait.

## Python usage

```python
from pathlib import Path

from inferdrome.resolution import resolve_experiment
from inferdrome.workspace import RunWorkspace

resolution = resolve_experiment(Path("examples/fake-smoke.yaml"))
workspace = RunWorkspace.reserve(Path("runs"), resolution)
print(workspace.run_id)
```

The checked-in example is synthetic. It can test resolution and lifecycle code,
but any future evidence bundle produced by the fake adapter remains
`SYNTHETIC_ONLY`.
