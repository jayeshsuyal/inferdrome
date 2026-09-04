# Routing qualification dashboard v1

Status: **Implemented for v0.3 PR 5**

This is a focused causal explanation over the sealed R1
`routing-campaign-v1` package and the additive PR4
`stale-telemetry-qualification-v1` descriptor. It does not change the R2
request-receipt projection. R2 remains the place to inspect every request;
this view puts the fixed stale-load/fresh-health observation beside the three
declared policy modes without pooling their terminal populations.

## One-minute explanation

At virtual time 15 ms, the experiment pauses only load collection. At 20 ms,
health is still age 0 ms and admissible while load is age 10 ms and
inadmissible under a 5 ms bound. Every mode started from a separate cold reset
and processed the same six requests. Before rendering anything, the dashboard
replays the sealed R1 source package, reads the descriptor through its bounded
no-follow verifier, requires the externally retained descriptor digest, and
rebinds that descriptor to the same in-memory source snapshot. The screen then
shows the focal candidate state, selected endpoint or fallback, terminal
outcome, and each separate six-request population. It records evidence; it
does not select a winner or issue a verdict.

```text
sealed R1 package ──replay──> held verified source snapshot ─┐
                                                             ├─> allowlisted GET/UI projection
sealed descriptor ──no-follow read + digest rebind───────────┘
```

## Local use

The local product demo prepares both artifacts and passes the retained digest
to the dashboard automatically:

```bash
./scripts/run_local_demo.py
```

For an already-created pair, all three values below are required:

```bash
PYTHONPATH=src python -m inferdrome dashboard \
  --runs-root runs \
  --routing-campaigns-root /private/tmp/routing-campaign-v1 \
  --routing-qualification-root /private/tmp/stale-telemetry-qualification \
  --routing-qualification-digest 'sha256:...'
```

The GET-only API is deliberately bounded to one configured pair:

- `GET /api/v1/routing-qualifications`
- `GET /api/v1/routing-qualifications/stale-telemetry-qualification-v1`
- `GET /api/v1/routing-qualifications/stale-telemetry-qualification-v1/evidence`

The browser routes are `/routing-qualifications` and
`/routing-qualifications/stale-telemetry-qualification-v1`. The evidence route
downloads only the canonical, fixed-size qualification descriptor retained in
the verified in-memory snapshot. It is not a generic file browser and does not
download raw request content or a source-package archive.

## Verification and threat boundary

The projection withholds all causal facts when any one of the source root,
qualification root, or retained descriptor digest is missing, malformed,
unsafe, stale, tampered, or mismatched. A qualification root is not discovered
recursively. Browser IDs are never filesystem paths. The endpoint returns only
an allowlisted typed projection; it does not reveal local paths, raw package
inventory, verifier diagnostics, prompts, outputs, endpoint addresses,
credentials, or provider state.

The dashboard holds the descriptor bytes only after source replay and canonical
descriptor rebinding succeed. The download route serves those held bytes rather
than reopening a path after verification. It is protected by the same optional
local bearer-read scope as the other dashboard routes.

## Non-goals and limitations

- This is one deterministic synthetic CPU vector, not a live endpoint, model,
  GPU, cloud, Kubernetes, or production-router observation.
- The three names are declared treatment labels. Their deterministic terminal
  outcomes do not establish policy superiority, causal generality, or a
  promotion decision outside this fixed scenario.
- The descriptor digest is an externally retained integrity binding; Inferdrome
  does not supply an external timestamping, key-management, or acceptance
  service.
- Complete source-package download would need a separate bounded archive and
  privacy review. This slice intentionally provides only the canonical
  descriptor and the existing full receipt viewer.

## Focused checks

```bash
PYTHONPATH=src pytest -q \
  tests/dashboard/test_routing_qualification_dashboard.py \
  tests/adversarial/test_routing_qualification_dashboard_security.py \
  tests/integration/test_local_demo.py
npm --prefix frontend test
npm --prefix frontend run test:e2e
```
