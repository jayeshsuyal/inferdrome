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
reset rules (including a per-trial warmup/reset time reserve), per-trial and
session output bounds, and the selection rule **before** any
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

## Boundaries

- Declared token lengths are not tokenizer verification; model and trace digests
  are integrity bindings, not runtime attestation.
- Selection is not a PASS/FAIL verdict, comparison winner, independent-endpoint
  proof, or invoice/capacity claim.
- Existing v1 reports and dashboard imports are preserved byte-for-byte. A
  future producer must use a new linked report version before it can display a
  calibrated-confirmation classification.
- The first live Qwen3-8B study remains separately authorized. This code makes
  no GPU, registry, cloud, SSH, credential, provider, or spend action.
