# Routing execution dashboard v1

`routing-execution-v1` is a bounded, read-only dashboard projection for one
already sealed routing-execution package. It is an evidence reader, not a
router, a deployment controller, a provider client, or an acceptance authority.

The reader verifies the four-file package inventory, immutability, hashes,
cross-record bindings, terminal-population closure, and deterministic replay
*before* it projects any campaign fact. It keeps the verified records in memory
for the response and does not reopen package paths while rendering a view.

## What the projection can say

The index and detail view expose only a fixed allowlist from the verified
manifest and receipts:

- source commit; role-image identities; Qwen3-8B model/tokenizer revisions;
  runtime and adapter identity; fixed routing/workload identities;
- declared execution mode and topology, including whether a fact is an
  operator declaration rather than a provider observation;
- the three policy trials, runner-state reset scope, controlled fault, complete
  terminal populations, candidate telemetry epochs/ages/freshness and
  admissibility, route selection/fallback, and terminal outcomes;
- unavailable GPU/DCGM and KV/cache capability states as `UNAVAILABLE` rather
  than an invented zero or healthy value.

The view never emits raw prompts, model output, endpoint origins or addresses,
origin/capability/destination hashes, provider/instance IDs, private paths,
headers, tokens, raw transport payloads, or arbitrary package files. It does
not declare a winner, a pass/fail result, provider proof, or a completed live
campaign.

`LAMBDA_MANUAL_HOST` is a declared manual-host mode. A local fixture bearing
that mode is explicitly `FIXTURE_GENERATED_LOCAL_NOT_PROVIDER_EVIDENCE`; it is
not proof of Lambda capacity, a GPU allocation, pricing, cleanup, or an
executed provider campaign. In particular, the declared A100 PCIe profile must
not be relabelled as the separate GCP A100 SXM4 profile.

## Verify first, then serve

Retain the producer's digest outside the package, verify it offline, and bind
the same digest when starting the dashboard:

```sh
python -m inferdrome.routing_execution verify \
  /absolute/path/to/routing-execution-package \
  --expected-digest sha256:<retained-package-digest>

python -m inferdrome dashboard \
  --runs-root /absolute/path/to/runs \
  --routing-execution-root /absolute/path/to/routing-execution-package \
  --routing-execution-digest sha256:<retained-package-digest> \
  --port 8787
```

Both routing-execution arguments are required as a pair. A missing, malformed,
mismatched, unsafe, mutable, tampered, or replay-invalid package is withheld;
the index returns only a bounded rejection without a package path or verifier
detail. The only supported detail identifier is `routing-execution-v1`.

The dashboard is loopback-only. It provides GET-only index/detail APIs and no
arbitrary evidence download route. Existing Runs, Compare, Evidence, synthetic
routing-campaign, and causal-qualification views retain their own contracts.

## Local fixture rehearsal

The repository includes a test/rehearsal fixture generator for the browser E2E
suite. It uses an injected deterministic in-process transport and virtual clock;
it does not contact Docker, Lambda, a GPU, a registry, or any provider:

```sh
python scripts/create_routing_execution_dashboard_fixture.py \
  --output-parent /absolute/path/to/empty-fixture-parent
```

It prints only the generated package root, retained digest, and the explicit
fixture provenance. The output is useful for checking the reader and UI, not
for publication as real serving evidence.

## Manual-host operational boundary

For a separately approved manual-host campaign, verify the sealed package
offline before reading it here. The dashboard does not start, stop, or inspect
containers and does not request VM termination. Follow the exact-ID external
operator termination/readback handoff in
[the manual-host runbook](MANUAL_HOST_LAMBDA_V1.md#cleanup-is-an-operator-handoff-not-a-solved-watchdog):
guest/container cleanup is best effort, it does not end VM billing, and pending,
unknown, inaccessible, or merely requested termination is not confirmed absence.

## Architecture in one minute

```text
sealed four-file package + externally retained digest
                    |
                    v
  strict offline verifier (inventory, hashes, semantics, replay)
                    |
          verified in-memory records only
                    |
                    v
 bounded index/detail API -> sparse causal dashboard projection
```

The useful tradeoff is deliberate: a single configured package and a literal
route ID keep the reader inspectable and prevent it becoming a browsing surface
for arbitrary local files. A later feature that needs multiple packages,
downloads, or live provider data requires a separately reviewed contract.

## Focused checks

```sh
python -m pytest \
  tests/dashboard/test_routing_execution_dashboard.py \
  tests/adversarial/test_routing_execution_dashboard_security.py \
  tests/unit/test_routing_execution_verifier_isolation.py \
  tests/unit/test_manual_host.py

npm --prefix frontend test -- --run
npx --prefix frontend playwright test e2e/dashboard.spec.ts
```

These are local fixture checks only. They do not establish CUDA, Docker,
provider, network, capacity, billing, or live-model behavior.
