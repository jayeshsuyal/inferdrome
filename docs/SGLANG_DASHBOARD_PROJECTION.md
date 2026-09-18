# Engine-bound SGLang reports in the Evaluation dashboard

The existing Evaluation list and detail views can display SGLang 0.5.18 study
reports without dropping their engine identity. This is a read-only projection
of locally produced reports. It does not execute a serving engine, replay raw
sources, verify model artifacts, or establish GPU compatibility or performance.
`SYNTHETIC_ONLY` reports retain that classification; all SGLang reports remain
`UNVERIFIED` and evidence-ineligible.

## Supported report and catalog contracts

New `inferdrome.evaluation-study-report.v2` reports declare
`dashboard_projection=ENGINE_BOUND_V2`. Their closed envelope contains the full
engine binding, the validated statistical report, and each component's digest.
The reader checks canonical bytes, both component digests, shared study config
and plan identities, evidence class, and compatible cache/prefix/image
declarations before projection.

The existing private catalog schema accepts an explicit `SGLANG_STUDY` entry.
Its `expected_sha256` pins the entire v2 report; the source file remains named
`report.json`. A SGLang envelope supplied under a legacy `STUDY` entry is rejected.
Never place the nested statistical report in a catalog by itself.

Historical `UNSUPPORTED_ENGINE_BINDING` envelopes still pass historical source
validation but are withheld from the dashboard. Unsupported versions, omitted
identity, altered components, unavailable files and digest mismatches also fail
closed. Restoring the exact supported file and catalog allows a fresh read;
the index does not retain a stale successful projection.

## Confirmation pinning and recovery

`write_pinned_confirmation_catalog` and `recover_pinned_confirmation_catalog`
accept a completed selected SGLang confirmation. They validate the durable
candidate, selection, linkage and manifest antecedents plus the SGLang engine
ledger. Every candidate's calibration and confirmation phase must retain its
declared config/plan digests, component binding digest and one engine-choice
digest. The selected study's `engine-binding.json` must match that phase exactly.

New SGLang selection/linkage sidecars retain `engine_choice_bindings_sha256`.
Missing ledgers, altered or reordered phases, mismatched study bindings and
unwrapped reports cannot produce a catalog. Candidate calibration reports remain
diagnostics; the catalog writer publishes only the selected confirmation. Catalog
publication uses the existing private, bounded, no-replace file boundary.

The session reserves the additional engine ledger and v2 JSON/Markdown envelope
capacity before lifecycle work. Default native study and catalog output remain
unchanged. The existing native recovery path keeps its original report bound.

## API and presentation

The existing `/api/v1/evaluation-reports` and detail routes return an additive
`inferdrome.evaluation-dashboard.v2` projection when displaying SGLang. A mixed
index retains the unchanged legacy summaries alongside the SGLang summary;
legacy-only indexes and details retain dashboard v1.

SGLang summaries contain a closed `engine_identity` with engine, version,
profile, binding digest, engine-choice digest, telemetry semantics/freshness/source
age, and cache preparation/state declarations. The report digest refers to the
outer envelope, and the existing study configuration digest stays distinct from
the engine configuration digest. Legacy v1 sources have no bound engine identity;
the UI does not infer that they attest vLLM.

The existing list, trust indicators and provenance section show this information.
The trial, coverage, outcome, latency and recovery tables use the existing
statistical reducers. SGLang's reported scheduler running/queued requests retain
their own semantics. Scrape acquisition age does not establish scheduler-state
age, and declared cold-cache preparation does not prove a performed reset.

## Explicit bounds and CPU validation

| Boundary | Legacy | SGLang |
| --- | --- | --- |
| Source report / dashboard detail ceiling | 4 MiB | 4 MiB + 20 KiB |
| Rehearsal confirmation read ceiling | Existing 1 MiB | 4 MiB + 20 KiB |
| Catalog / API index ceiling | 64 KiB, up to eight entries | Same |

The catalog reader reserves work according to each explicit entry kind. The
browser enforces its decoded projection's response limit as well as the streaming
hard cap. Additional SGLang capacity does not widen the legacy report contracts.

CPU fixtures execute the native SGLang path with hand-authored transport/metric
fakes before producing typed trials, a study report, a pinned catalog and backend
projection. Loopback rehearsals use two actual local HTTP listeners for both
engines. Browser tests cover list/detail navigation, reload, selectors, narrow
mobile layouts, rejection of missing/tampered/unsupported sources and recovery
after exact bytes are restored. These checks establish source/read-path behavior;
an actual GPU campaign remains separately authorized and unperformed here.
