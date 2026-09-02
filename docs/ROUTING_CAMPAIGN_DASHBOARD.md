# Routing-campaign dashboard projection v1

Status: **Implemented for PR R2**

This is the read-only dashboard projection for one sealed
`routing-campaign-v1` package from R1. It makes the fixed synthetic CPU
experiment inspectable; it does not route traffic, execute an endpoint, or
assign a policy, acceptance, or promotion verdict.

## One-minute explanation

Start the local dashboard with an explicit sealed campaign root. Before the
browser receives a campaign summary or receipt, Python takes a no-follow,
bounded snapshot of that root, canonical-parses every fixed R1 artifact,
checks its manifest hashes, independently replays the campaign, and confirms
the tree identity did not change. The dashboard then projects only those
in-memory typed records. The index shows either one verified campaign or a
minimal withheld entry. The detail page shows the cold reset identity, the
load-pause/fresh-health fault timeline, every request's candidate telemetry
age and admissibility, selection and fallback facts, paired terminal outcome,
and all five terminal-population buckets for every trial.

## Local use

First create a sealed R1 package at a new location:

```bash
PYTHONPATH=src python -m inferdrome.routing_campaign run \
  --campaign-plan campaigns/routing-campaign-v1/stale-load-fresh-health.plan.json \
  --request-trace campaigns/routing-campaign-v1/stale-load-fresh-health.trace.jsonl \
  --fault-schedule campaigns/routing-campaign-v1/stale-load-fresh-health.fault-schedule.json \
  --trial-plan campaigns/routing-campaign-v1/trial-plan.json \
  --output /private/tmp/routing-campaign-v1

PYTHONPATH=src python -m inferdrome dashboard \
  --runs-root runs \
  --routing-campaigns-root /private/tmp/routing-campaign-v1
```

The explicit `--routing-campaigns-root` must name one sealed R1 package, not a
directory to search recursively. If it is omitted, the Routing campaigns view
is empty. A root that is missing, unsafe, malformed, mutable, or fails replay
is withheld without its claimed campaign content or internal verifier error.

The local, GET-only HTTP surface is:

- `GET /api/v1/routing-campaigns` — one bounded index page (limit 1–25).
- `GET /api/v1/routing-campaigns/routing-campaign-v1` — the allowlisted detail.

The frontend routes are `/routing-campaigns` and
`/routing-campaigns/routing-campaign-v1`. When dashboard bearer authentication
is enabled, both API routes require its same read scope.

## Architecture and invariant

```text
sealed R1 package root
        ↓ no-follow bounded scan and byte reads
R1 canonical/hash/replay verifier + final identity scan
        ↓ one stable, typed in-memory snapshot
allowlisted R2 projection keyed by retained digest
        ↓
GET-only index/detail API → local dashboard views
```

The important invariant is that the projection never performs
`verify_campaign(path)` followed by a new read of `path`. The public R1
`load_verified_campaign` reader returns records derived from the same captured
bytes it verified and replayed, only after its final identity scan. R2 neither
opens those paths again nor serves raw package files.

The API resolves the fixed campaign ID only from a verified in-memory snapshot;
an HTTP ID is never a filesystem path. It has one configured package ceiling,
one concurrent snapshot build, a 64 MiB input reservation, the R1 exact
inventory/size/depth limits, and a generic rejection response. A digest-keyed
cache is populated only after verification succeeds.

## Acceptance and code tour

The core slice is deliberately small:

1. `inferdrome.routing_campaign.package.load_verified_campaign` returns the
   R1 verifier's stable typed snapshot while `verify_campaign` retains its
   original report-only behavior.
2. `inferdrome.dashboard.routing_campaign` turns that snapshot into an
   explicit browser allowlist and never computes route behavior in the browser.
3. `inferdrome.dashboard.api` adds the two protected GET routes, and the
   frontend renders reset, fault, telemetry, decision, terminal, and population
   facts in separate routing-only views.

Run the focused checks with:

```bash
PYTHONPATH=src pytest -q \
  tests/dashboard/test_routing_campaign_dashboard.py \
  tests/adversarial/test_routing_campaign_dashboard_security.py
INFERDROME_PYTHON=.venv/bin/python ./scripts/engineering_gate.sh
INFERDROME_PYTHON=.venv/bin/python ./scripts/dashboard_gate.sh
```

The dashboard gate also runs type checks, frontend unit tests, populated
Playwright navigation, Python dashboard tests, and an installed-wheel resource
smoke check.

## Threat boundary, non-goals, and limitations

R2 rejects symlinks, hardlinks, writable package trees, malformed or
noncanonical artifacts, unexpected inventory, oversized files, semantic replay
drift, same-path replacement during a verified read, and root replacement
during a verified read. The response does not expose raw package paths,
artifacts, verifier diagnostics, prompts, endpoint URLs, credentials, or any
unverified claimed receipt.

This is not a routing data plane, production router, real endpoint adapter,
vLLM/GPU collector, cloud provider client, Docker/GCP/Kubernetes execution
path, policy recommendation, statistical result, or PASS/FAIL/NOT_PROVEN
authority. It has no provider, ADC, SSH, credential, GPU, cloud, or spend
surface. Real endpoint and GPU evidence remain for PR B and the separately
approved campaign.
