# SGLang on the native evaluation path

SGLang 0.5.18 uses the existing Inferdrome request runner, four routing policies,
healthy/stale-load sessions, sequential study owner, calibration selection, and
statistical reducers. The default vLLM path and frozen v1 artifacts are unchanged.
The SGLang path exports explicitly engine-bound v2 results and reports.

This integration is qualified only by CPU fakes and local HTTP tests. It does
not establish image availability, model compatibility, GPU behavior, capacity,
cost, actual overload, or live SGLang qualification. Runtime identity remains
`UNVERIFIED`; every artifact is evidence-ineligible. See
[serving preparation](SGLANG_SERVING_PREPARATION.md) for the pinned source/image
and the scheduler and streaming limitations.

## Identity before dispatch

`build_sglang_engine_binding(study, profiles)` binds the two native endpoint IDs,
their distinct declared origins, complete serving-profile digests, model and
tokenizer snapshot identities, template hash, cache preparation, and telemetry
semantics to the exact native study configuration and plan. Persisted identities
contain hashes, not raw origins, paths, model names or prompts.

The default binding has `execution_mode=NATIVE_PROCESS`. Passing
`containerized=True` selects `DOCKER_BRIDGE` and binds each endpoint's projected
launch digest. No process starts while building or validating these declarations.

`run_study(..., engine_binding=binding, executor=...)` writes the immutable
`engine-binding.json` before calling the executor. `SGLangStudyExecutor` checks
the original serving profiles and exact compiled trial before creating native
clients. Its output uses healthy/routing-result v2 and study-trial-result v2.
Each serialized scheduler observation and policy snapshot retains the names
`reported_running_requests` and `reported_queued_requests`.

The existing policies consume a private mathematical sum of those two counts.
That adaptation does not make them equivalent to vLLM gauges: SGLang reports
phase-dependent scheduler snapshots, excludes the separate grammar queue, and
does not expose source-state age through these metrics. Freshness is measured
from local scrape start only. The v2 verifier checks binding and semantics before
replaying the existing policy and population invariants. Native request indices,
offered schedules, dispatch/response/content/terminal timing, and all rejection,
error and cancellation populations are retained.

## Owned container preparation

`TwoEngineSGLangSubprocessLifecycle` reuses the exact-owned process lifecycle
merged in PR #92. The SGLang hooks change image/argv, artifact preflight, and
readiness preparation. Pending creates, original operation/session deadlines,
exact-ID removal, and independent port/GPU readbacks remain shared.

The container projection is explicit:

| Declaration on host | Container launch/readiness |
| --- | --- |
| `127.0.0.1:<distinct-port>` | Publish to port 8000; listen on `0.0.0.0:8000` |
| Declared model directory | Read-only `/model` |
| Declared tokenizer directory | Read-only `/tokenizer` |
| Declared Jinja file | Read-only `/chat-template.jinja` |
| Existing profile flags/environment | Preserved except the listed host/port/path projection |

The launch uses the pinned Linux/amd64 image digest, explicit `python3` entrypoint,
GPU 0/1, and `--pull never`. The launch digest covers source-profile identity,
projected argv, environment, mounts, published port, GPU index and image/platform.
Ownership names and attempt labels remain the shared owner's responsibility.

Before each launch, artifact preflight compares full model/tokenizer snapshot
hashes using the existing `regular-files-excluding-dot-cache-v1` policy and hashes
the exact Jinja bytes. Traversal errors, links, hardlinks, special files, mismatched
hashes and detected changes fail closed. This is a local read-time check; a
read-only container mount does not make host files immutable after preflight.
Hashing runs as retained, read-only owned work under the original deadline.
Cancellation or timeout cannot dispatch an engine. A filesystem read that is
still running keeps cleanup unconfirmed until it finishes; its task is never
cancelled merely to make it appear finished. No runtime readback is issued when
artifact verification failed before any launch attempt.

Preparation then performs bounded `/health`, `/health_generate` and `/model_info`
checks against the mapped paths and original served name/revision. Both one-token
warmup streams must successfully finish through the native SSE parser, and their
owned client must close before reset. Each endpoint gets one `/flush_cache`
attempt with the pinned success response plus non-generating readbacks. No
generation probe follows reset. An uncertain/cancelled flush is never retried.
Fresh zero scheduler gauges do not themselves prove the server is drained.

`last_reset` is available only after both reset/readback sequences and client
closure succeed. Each receipt identifies the original profile digest, projected
launch digest, mapped-readiness digest and server-accepted reset. It remains
unverified and cannot attest loaded model/template bytes or actual cache state.

## Calibration and confirmation

The existing `compile_rehearsal` defines all native candidate recipes. The
SGLang path revalidates/recompiles those recipes, trial mappings and reserves
before any ledger or lifecycle operation. `bind_sglang_rehearsal` binds every
candidate's calibration and confirmation plan, including candidates that will
not be selected, and requires a single engine-choice digest. Only native config
and plan digests are excluded when comparing that choice.

Code-level preparation for a separately authorized operator is:

```python
engines = bind_sglang_rehearsal(rehearsal, profiles, containerized=True)
owner = TwoEngineSGLangSubprocessLifecycle(
    profiles,
    contexts=engines.contexts,
    ownership_id=ownership_id,
)
# Construction above is inert. This call would execute the owned runtime:
result = await run_rehearsal(
    rehearsal,
    output_root,
    lifecycle=owner,
    sglang_profiles=profiles,
    containerized=True,
)
```

The durable `engine-choice-bindings.json` links all phase bindings to the native
candidate-recipe ledger before prepare/dispatch. Its digest accompanies the
returned rehearsal result. Per-phase studies retain their own bindings, and
calibration reduces only semantically validated v2 records. Supplying both a
SGLang profile choice and a native executor is rejected before lifecycle work;
a declared SGLang lifecycle cannot enter the unbound vLLM path either.

The session reserves the additional engine ledger within the declared output
limit. Cleanup/readback uncertainty aborts later work using the existing owner.
No new policy, generic provider/lifecycle registry, campaign CLI, or permission
to run infrastructure is introduced.

## Reports and dashboard limitation

`report_study(..., engine_binding=binding)` verifies the persisted binding and all
trial envelopes before using the existing statistical reducers. The report v2
contains the validated statistical report, its digest, the complete engine
binding and its digest. Markdown carries the same provenance and limitations.
Declared cold-cache preparation does not by itself establish a performed reset.

The current dashboard/catalog reader accepts only the existing frozen v1 report
contracts. It cannot carry this engine identity, so SGLang reports explicitly
declare `dashboard_projection=UNSUPPORTED_ENGINE_BINDING` and are rejected by
that reader. No SGLang catalog is published, and the nested statistical report
must not be exported on its own as an unbound v1 result.

## CPU verification

The dedicated container/owner tests inject commands and HTTP clients; they never
invoke Docker or NVIDIA tools. Local HTTP tests own two actual distinct loopback
listeners and mark their output `SYNTHETIC_ONLY`:

```sh
PYTHONPATH=src:. .venv/bin/python -m pytest -q \
  tests/unit/test_sglang_container.py \
  tests/unit/test_sglang_lifecycle.py \
  tests/unit/test_sglang_lifecycle_artifacts.py \
  tests/unit/test_snapshot_traversal.py \
  tests/unit/test_sglang_readiness.py \
  tests/integration/test_sglang_study_loopback.py \
  tests/integration/test_sglang_rehearsal.py
```

These focused tests supplement, rather than replace, the three repository gates
in [CONTRIBUTING.md](../CONTRIBUTING.md). Local Docker/Compose qualification is
withheld when runtime execution is not authorized; exact-head CI must supply
that gate before the integration PR is ready for independent review.
