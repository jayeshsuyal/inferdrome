# GCP private two-A100 watchdog (v0.3)

This document describes the live-safety boundary for exactly one
`a2-highgpu-2g` Compute Engine VM with two A100-SXM4-40GB GPUs, two private
engine endpoints, and one CPU-only Inferdrome runner. It is not a scheduler,
multi-host controller, Kubernetes integration, or generic cloud framework.

## The boundary

The normal controller owns proposal parsing, human-approval validation, and
the campaign lifecycle. A detached watchdog process owns only cleanup
reconciliation after the create intent is durable. It has no create, runner,
routing, evidence-collection, or broad-account-discovery operation.

```text
frozen proposal + approval + cleanup authority
                    |
                    v
durable CREATE_INTENT_DURABLE / CREATE_INTENT
                    |
                    v
fsync activation record -> detached cleanup-only worker -> READY receipt
                    |
                    v
fresh capability check -> optional Google SDK/client -> exact insert
```

The activation record binds the exact proposal, create request and startup
payload, project/zone, deterministic instance and boot-disk names, ownership
labels, two-engine topology, approval digest, cleanup authorization, execution
deadline, cleanup horizon, and journal-root identity. It is content-addressed
local state, not a signature or evidence of human authorship.

The Google create factory accepts only the opaque capability that the ready
worker issued. It rechecks the worker before importing the optional Google SDK.
The controller repeats that check immediately before `InstancesClient.insert`.
Missing, stale, mismatched, tampered, or dead-worker state fails closed before
the SDK/client/create edge.

## Worker behavior

The worker runs in a new session with a minimal environment and no approval,
credential, proxy, or payload environment values. It receives a verified
watchdog directory descriptor; its durable record contains no secret. Google
imports and ADC resolution occur only in the cleanup-only worker after it has
observed controller death or the execution deadline.

The worker reads only the exact request's predetermined resource identity. It
never selects a hostname, scans an account, or creates a resource. The create
projection writes the exact create-request digest into instance metadata. When
the parent dies after `insert` but before provider IDs are journaled, the
worker requires that metadata plus a fresh full topology/label readback before
binding the hashes used by the exact delete path. A same-name replacement,
request-digest mismatch, or ambiguous ownership never reaches a delete seam.

One apparent absence is a retry, not cleanup confirmation. The worker requires
two exact absence observations before recording pre-bind cleanup confirmation.
Known instance and disk identities use the existing exact delete/reconcile and
absence-verification path. A failed delete keeps retrying through the frozen
cleanup horizon. A restart can resume the durable cleanup-only record only
after the original parent PID is gone; it cannot reissue a create capability.

Provider `maxRunDuration` with `DELETE` remains a provider-side backstop. It
is not a watchdog substitute, an invoice limit, or proof that billing stopped.

## Operator path

`scripts/gcp_private_campaign_v2.py execute` requires all of these local files
and private directories:

- proposal and startup payload;
- exact human campaign approval;
- separately authorized cleanup recovery record;
- journal root, evidence root, and watchdog root.

The command validates approval and cleanup authority before it constructs the
controller. The controller validates them again after journaling the intent
and before the provider factory. `proposal`, `startup`, and `preview` remain
offline. `recover-cleanup` and `discover-orphans` stay separately authorized
no-create paths.

## Limits

- This code is local/fake tested only in this change; it performs no provider,
  ADC, SSH, GPU, registry, bucket, or spending action by itself.
- A local content hash cannot defend against a malicious same-user filesystem
  owner. It detects accidental corruption and binds safe no-follow state; it
  does not replace an operator signature or external host attestation.
- The watchdog is a cleanup actor, not account-level billing enforcement. A
  real campaign still needs a fresh provider quote, explicit commit-bound spend
  authorization, an independently reviewed runtime configuration, and
  post-run exact absence verification.
