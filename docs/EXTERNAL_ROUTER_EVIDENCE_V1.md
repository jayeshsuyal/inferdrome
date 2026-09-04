# External-router evidence v1

`inferdrome.external_router` is an additive attached-evidence boundary. It
records facts supplied by an independently operated external router; it does
not embed, configure, call, replace, or make decisions for that router.

The first profile is `llm-d-attached-v1`. That name identifies an
Inferdrome-owned, local-fixture input envelope. It is not a claim that llm-d
has a native API with this shape, and it has no llm-d, HTTP, Kubernetes, cloud,
or subprocess dependency.

## One-minute architecture

```text
bounded local record + raw router config bytes
                    |
                    v
        verify config digest and strict profile
                    |
                    v
  bind router/topology/request/candidates/observations
                    |
                    v
  retain router-reported selection/policy/reason/outcome
                    |
                    v
    canonical evidence bytes + retained SHA-256 digest
                    |
                    v
             offline verifier (no router access)
```

The adapter requires exactly two logical endpoints, each identified only by an
opaque ID and a digest. The record binds router/profile/version/config identity,
request and correlation identity, ordered candidates, the router-reported
selection, policy and reason, and one endpoint terminal outcome. It retains no
endpoint origin, prompt, completion, header, token, credential, or raw router
payload.

Every retained operator-supplied identifier is a simple pseudonymous alias:
letters, digits, `_`, and `-` only. The contract rejects address syntax
(including IP, hostname, and host:port forms), URL/path/email delimiters, and
representative credential or token prefixes. This is data minimization, not
proof of provenance or perfect secret detection; operators remain responsible
for supplying aliases rather than origins, credentials, or secret-bearing IDs.

Each candidate has independent `HEALTH` and `LOAD` observation records. A
record binds observation and observer IDs, a common monotonic clock domain and
epoch, sample and decision timestamps, exact age, and freshness bound. A stale
sample is explicit. Missing or unsupported telemetry, policy, reason, selection,
or outcome must be explicit `UNAVAILABLE`; malformed, contradictory, duplicate,
or unbound facts are rejected. Inferdrome never derives a routing policy or
reason that the attached record did not report.

## Local demo

The committed fixture is local-only:

```sh
python -m inferdrome.external_router adapt-llmd \
  --record tests/fixtures/external-router/llmd/v1/attached-record.json \
  --router-config tests/fixtures/external-router/llmd/v1/router-config.json \
  > /tmp/external-router-evidence.json
python -m inferdrome.external_router verify /tmp/external-router-evidence.json
```

The second command reports a retained digest and the evidence-admissibility
state. Retain that digest outside the record to detect replacement or tampering.
The receipt is not a sealed campaign package or an execution verdict; PR 4 owns
the deterministic stale-telemetry qualification campaign and sealed campaign
package.

## Threat boundary and non-goals

The CLI accepts bounded regular local files and rejects final-path symlinks,
hardlinks, oversized inputs, duplicate JSON keys, and non-finite JSON values.
It does not establish a live endpoint, controller identity, physical topology,
or router provenance beyond the supplied digest bindings. A genuine external
router attachment needs a separately reviewed source adapter and live campaign
authorization.

This slice does not add a routing data plane, cluster control plane, dashboard
projection, policy verdict, promotion control, GPU run, provider action, or
cloud credential path.
