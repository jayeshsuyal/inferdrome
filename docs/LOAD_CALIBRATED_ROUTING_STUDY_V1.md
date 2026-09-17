# Load-calibrated routing study protocol v1

This is a bounded two-endpoint measurement protocol, not a production router,
capacity guarantee, promotion gate, or acceptance authority. It does not
contact an endpoint, load a model, create a cloud resource, or relabel a
historical report as calibrated.

## Calibration is distinct from confirmation

Existing v1 study reports remain exactly what they say they are:
`UNCALIBRATED_REHEARSAL`. The additive
`inferdrome.evaluation-load-calibration-protocol.v1` artifact freezes the
Qwen3-8B model/trace/workload hashes, declared input and completion token
lengths, every candidate study-plan digest, latency targets, warmup and cold
reset rules (including independent per-trial reset/warmup and cleanup time
reserves), per-trial and session output bounds, and the selection rule **before** any
observation is accepted.

The calibration phase runs every declared load level in ascending order, for
every declared repeat and all four current routing policies. Its policy order
rotates from the declared base order by repeat so a corresponding repeated
study configuration can retain balanced policy positions. Each observation
retains offered, dispatched, and complete terminal populations; achieved
concurrency; client queue peak; scheduled-offer latency origin; and p95 timing.
The latter values describe what actually happened; they do not prove capacity
on their own.

The frozen primary metric is
`MINIMUM_ALL_OFFERED_SLO_GOODPUT_MILLIRPS`. A level is admissible only if every
trial returns one terminal outcome per offered request, every offered request is
SLO-good, and its scheduled-offer p95 values meet both declared targets. The
deterministic selection is the highest admissible offered rate. Missing,
partial, stale, or merely promising observations are ineligible. Any incomplete
calibration terminal population stops the phase and produces a zero-trial
confirmation plan; a complete SLO miss may still leave a lower completed level
eligible.

Confirmation is materialized only from that selection and reuses the selected
candidate's already pinned study-plan/config identities. Each confirmation
repeat contains a balanced healthy block and then the declared `STALE_LOAD`
fault block; the calibration selector is never applied to those intentionally
degrading fault trials. It cannot silently choose another load level.

## Local-only commands

The commands below only parse, validate, and create no-replace local JSON.
They do not build an image, contact a serving endpoint, use a credential, or
invoke a provider.

```sh
python -m inferdrome.evaluation load-calibration-plan \
  --protocol /private/input/calibration-protocol.json \
  --output /private/output/calibration-plan.json

python -m inferdrome.evaluation load-calibration-select \
  --protocol /private/input/calibration-protocol.json \
  --observations /private/input/calibration-observations.json \
  --selection-output /private/output/calibration-selection.json \
  --confirmation-output /private/output/confirmation-plan.json
```

Operators prepare each candidate study plan first, then bind only its immutable
digest into the protocol. The parser rejects duplicate plans, non-ascending
rates, altered policy order, impossible duration reserves, missing trial
observations, incorrect or non-representable offered-rate arithmetic, and
terminal-population mismatches.

## Native local rehearsal bridge

`inferdrome.evaluation.load_calibration_rehearsal` is the additive local bridge
from this declaration to the existing native `StudyConfig` runner. It requires
one separately supplied lifecycle owner for the two endpoints; the owner must
perform and confirm each trial's reset/warmup and cleanup. The bridge executes
the existing healthy and stale-load study entry points, never a new routing
data plane. The dedicated CPU-loopback browser fixture is explicitly labeled
`SYNTHETIC_ONLY` through the native report and existing dashboard projection;
the bridge itself retains the executor's evidence class and never upgrades it.
The local bridge regression is exercised with:

```sh
PYTHONPATH=src python -m pytest -q tests/integration/test_load_calibration_rehearsal.py
```

Before the first reset, warmup, socket, or request dispatch, the bridge writes
the no-replace `candidate-recipe-bindings.json` ledger. It includes the exact
native config, plan, and recipe hashes for *both* calibration and confirmation
for every candidate level, but retains neither endpoint origins nor request
text. A write failure prevents all dispatch. The following selection-binding
and final linkage sidecars both reference that ledger digest, so the selected
confirmation cannot be presented as an unbound post-calibration substitute.

After the selected confirmation completes, `write_pinned_confirmation_catalog`
independently re-reads its native report and writes a one-entry digest-pinned
catalog for the existing read-only Evaluation Reports API/dashboard. This is a
local rehearsal import, not a new report type, routing control, calibration
verdict, or claim of runtime identity.

The dashboard browser regression first produces that report from two ephemeral
CPU loopback fakes, labels all result populations `SYNTHETIC_ONLY`, closes the
fakes, and then starts the normal local dashboard against the pinned catalog.
The browser sees only the verified report projection; it cannot replay the
study or discover the fixture endpoints.

`TwoEngineVllmSubprocessLifecycle` is the one bounded local operator lifecycle
provided with the bridge. Its default no-shell command runner starts two fresh
exact-owned, digest-pinned `vllm/vllm-openai` containers per native trial,
binding endpoint A to GPU 0 and endpoint B to GPU 1 on two different loopback
ports. It uses `--pull never`, mounts an already-present Qwen3 snapshot
read-only with offline flags, verifies it through the reviewed no-download
snapshot boundary, overrides the entrypoint to the single exact `vllm serve`
command, polls bounded health/metrics and a fixed warmup request, and removes
only those exact names before independent GPU-idle readback. Its declared
startup/cleanup maximum must fit the protocol's per-trial lifecycle reserve or
the rehearsal refuses to dispatch. It does not discover or adopt a container,
download a model, prove an image/model attestation, or make its result eligible
evidence. Its command seam is covered only with CPU fakes; no test launches
Docker or an NVIDIA workload.

One original monotonic session cutoff starts before the candidate ledger is
reserved and is never renewed. Compilation explicitly includes native trial
work, every reset/warmup and cleanup, a five-second final-cleanup reserve, and
a five-second artifact-retrieval reserve. Dispatch stops at the earlier
execution-window cutoff; cleanup/retrieval can consume only their remaining
share of the original hard deadline.

If a local controller restarts after native output is written, the bounded
`recover_pinned_confirmation_catalog` helper re-reads the candidate ledger,
selection, selection-binding, linkage, native manifest, and selected report.
It recreates a catalog only when every canonical digest and selected-level
binding agrees; it never resumes a trial, contacts an endpoint, or replaces an
existing artifact.

## Boundaries

- Declared token lengths are not tokenizer verification; model and trace digests
  are integrity bindings, not runtime attestation.
- Selection is not a PASS/FAIL verdict, comparison winner, independent-endpoint
  proof, or invoice/capacity claim.
- Existing v1 reports and dashboard imports are preserved byte-for-byte. A
  future producer must use a new linked report version before it can display a
  calibrated-confirmation classification.
- The bridge's supplied lifecycle is an explicit operational boundary. The
  reviewed loopback and command-seam tests prove only CPU/socket execution,
  exact local command rendering, and offline import—not a GPU campaign.
- The first live Qwen3-8B study remains separately authorized. This code makes
  no GPU, registry, cloud, SSH, credential, provider, or spend action.
