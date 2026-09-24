# Fixed four-cell Vast budget pilot

This source-only preparation defines the smallest declared real-GPU pilot that
preserves the existing two-endpoint Qwen3-8B workload and stale-load evidence
semantics. It does not execute a model, contact Vast, approve spend, or turn an
uncalibrated load into a capacity claim.

## Design

The pilot fixes the lower already-declared load, `load_low`: 12,000 millirps
(12 requests/second), 96 foreground requests in an eight-second offered
window. This is the conservative member of the frozen two-level design, not a
selection based on a completed calibration result.

It runs one cold-reset trial for each ordered cell:

1. `HEALTHY` × `evaluation_round_robin_v1`;
2. `HEALTHY` × `evaluation_freshness_fallback_v1`;
3. `STALE_LOAD` × `evaluation_round_robin_v1`;
4. `STALE_LOAD` × `evaluation_freshness_fallback_v1`.

Round robin is the load-independent reference. Freshness fallback is the
telemetry-aware candidate: it uses minimum fresh load only when every eligible
load observation is admissible and otherwise ignores load and falls back to
round robin. The pair therefore isolates the contribution of admissible load
telemetry without adding a new routing algorithm.

The shared recipe retains the exact two loopback endpoint IDs/origins, model,
128-token completion request shape, foreground offers, eight-request
background population, health/load freshness bounds, and stale-load fault
timing. The compiler expands those shared bytes into the four existing native
healthy/fault configurations. It does not relax or rewrite the frozen full
load-calibration and study schemas.

## Exact bounded envelope

Each native trial has a 73-second replay bound. The existing direct-process
lifecycle source computes 420 seconds for prepare/reset and 240 seconds for
cleanup with the real-GPU startup bound. Four trials therefore reserve:

```text
4 × (73s replay + 420s prepare/reset + 240s cleanup) = 2,932s
+ 5s final cleanup reserve + 5s retrieval reserve       = 2,942s
                                                           49m02s
```

The exact output reserve is 23,068,672 bytes: four 2 MiB trial results, 1 MiB
plan/manifest, 8 MiB report allowance, 1 MiB sidecar allowance, and 4 MiB
control allowance. These are storage ceilings, not expected output sizes.

The 49m02s bound covers Inferdrome's session after dispatch. A future human
approval must separately include any provider allocation/boot overhead and a
termination safety margin. A dollar estimate requires an externally observed
price and is not derivable from repository source.

## Offline preflight

From this exact source tree, in an already installed environment:

```bash
python -m inferdrome.evaluation.cli vast-budget-pilot-preflight \
  --protocol examples/vast-budget-pilot/protocol.json \
  --recipe examples/vast-budget-pilot/recipe.json \
  --output /private/owned-empty-directory/pilot-plan.json
```

The command parses bounded local files, constructs no transport or runtime,
and uses the same `direct_process_lifecycle_reservation()` function as the live
two-process lifecycle. It fails closed on changed policy/condition order,
source/model/endpoints/request shape, offered rate, lifecycle reserve, protocol
binding, or duration/output ceiling.

## Limitations and next gate

- This slice is compilation and admission only; it adds no pilot executor.
- One repetition per cell is descriptive and cannot support generalization or
  uncertainty claims.
- `load_low` is declared, not calibrated or proven safe/near-capacity.
- Runtime, image, model-snapshot, GPU, provider, guardian, price, and final
  evidence-destination identities remain external approval inputs.
- `evidence_eligible=false`; Inferdrome emits measurements, never an acceptance
  verdict or a winning-policy claim.

No live operation is authorized by these files. Before execution, a separate
exact approval must bind the source commit, provider/account/location, two-A100
allocation, immutable runtime/image/model identities, maximum runtime, USD cap,
cleanup deadline/guardian, and evidence destination.
