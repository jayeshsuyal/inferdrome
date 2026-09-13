# Vast process adapter v1

This adapter runs the fixed routing experiment as two serving processes and an
observer inside one operator-supplied Vast container. It adds execution config
and manifest v4, mode `VAST_MANUAL_CONTAINER`, while retaining the existing
receipt, sealing, replay, and dashboard interpretation boundaries. The Lambda
manual-host and GCP contracts remain separate and unchanged.

This change supplies source, a Dockerfile, and local synthetic tests. It supplies
no published container image and establishes no real GPU run. No image registry
activity, paid allocation, or live provider API operation was performed for this
change. Commands below describe the interfaces; they do not authorize a paid
launch or registry publication.

## Supported container and process contract

The finite profile is `vast-container-two-h100-sxm5-80gb-v1`:

| Property | Required value |
| --- | --- |
| Provider / provisioning | `VAST_AI` / `OPERATOR_SUPPLIED_CONTAINER` |
| Launch mode / container user | `args` / `2000:0` |
| Runtime host | Linux x86_64, Python 3.12 |
| Container count | 1 |
| Serving engine count | 2, one process per logical endpoint |
| Accelerator declaration | 2 × `NVIDIA H100-SXM5-80GB`, two distinct full GPU UUIDs |
| Tensor parallel size | 1 per engine |
| Serving runtime / model | vLLM 0.26.0 / frozen `Qwen/Qwen3-8B` snapshot and tokenizer |
| Endpoint bindings | `127.0.0.1:8000` and `127.0.0.1:8001` |
| Public mappings / persistent volumes | Empty inventories |
| Isolation boundary | `SEPARATE_PROCESSES_SHARED_CONTAINER` |
| Observer GPU isolation | `ENVIRONMENT_ONLY_NOT_HARDWARE_ENFORCED` |
| Lifecycle protection | `UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY` |

The local GPU parser requires exactly two `nvidia-smi` rows, matching the declared
UUIDs, each named `NVIDIA H100 80GB HBM3`, with `81559` MiB, MIG `Disabled`, and the
same nonempty numeric driver version. This is a deliberately closed profile;
other H100 variants, A100s, MIG configurations, extra visible GPUs, and alternate
memory reports are rejected rather than silently generalized.

Each engine receives its declared UUID in `CUDA_VISIBLE_DEVICES` and
`NVIDIA_VISIBLE_DEVICES`. The observer receives `CUDA_VISIBLE_DEVICES=""` and
`NVIDIA_VISIBLE_DEVICES=void`. All processes share UID 2000, GID 0, the filesystem,
and container device access. These environment settings do not remove device
nodes or enforce a hardware boundary. The local observation explicitly records
`UUID_BOUND_CHILD_ENVIRONMENT_NOT_DRIVER_PROCESS_ATTESTATION`; it does not attest
that a driver assigned each process exclusively to its declared GPU.

Vast documents container execution and does not support Docker-in-Docker for
ordinary instances. Its suggested alternatives include multiple services or a
process manager in one image. That is the reason for this additive process
adapter. [Technical FAQ](https://docs.vast.ai/guides/reference/faq/technical),
[Docker execution environment](https://docs.vast.ai/guides/instances/docker-environment).

Use the strict `args` launch mode. Vast preserves the image entrypoint in this
mode and passes `args_str` as its arguments. SSH and Jupyter modes replace the
entrypoint and inject their own setup. They are outside this adapter's contract;
do not switch to them to obtain a staging shell.
[Creating instances with the API](https://docs.vast.ai/api-reference/creating-instances-with-api),
[Template settings](https://docs.vast.ai/guides/templates/template-settings).

The Dockerfile's final scratch stage copies the runtime filesystem while
discarding inherited `EXPOSE`, `ENTRYPOINT`, `CMD`, and `HEALTHCHECK` metadata. It
then sets the supervisor entrypoint and `USER 2000:0`. No application port should
be requested through image metadata, template options, or launch overrides.
Vast turns image `EXPOSE` entries into public port requests, in addition to
explicit mappings and mode-specific ports. Inspect the final image and the
sanitized launch readback; loopback application bindings alone are not a check
of provider mappings.
[Networking and ports](https://docs.vast.ai/guides/instances/connect/networking).

## Image and artifact identities

`Dockerfile.vast-process` is a new image definition. An existing Lambda/GCP image
or an older image does not acquire these modules when a branch is merged. A new
reviewed build is required, with the exact source commit supplied as
`SOURCE_REPOSITORY_COMMIT` and an immutable resulting OCI reference retained for
the operator declaration. Building, publishing, and authenticating a registry
pull remain separate launch work.

The image records `/opt/inferdrome-vast-build.json` with the source commit and
SHA-256 digests of `vast_process`, `vast_process_runtime`,
`vast_process_observer`, `vast_bootstrap`, `vast_control`, and `vast_transfer`.
The operator workflow checks its own installed module inventory before create.
Preflight requires exact equality between the plan, this
marker, and the installed module bytes. The marker is an
`OPERATOR_DECLARED_BUILD_MARKER`, not an independent assertion that an OCI image
was built from a repository revision.

The v4 config and executed manifest retain one `container_image` reference.
They contain no fabricated `runner_image` or `serving_image`. The
`artifact_provenance` object binds the source commit, observer and supervisor
module digests, frozen model manifest and snapshot digests, and the digest of
the local runtime observation. Its assertions remain
`OPERATOR_DECLARED_NOT_OBSERVED` and
`LOCAL_PROCESS_OBSERVATIONS_NOT_PROVIDER_ATTESTATION`.

The private observation includes host/runtime checks, sanitized GPU observations,
and later process IDs, command digests, and both health results. The sealed
package retains its digest rather than the raw observation. Provider-selected
image resolution, image provenance, capacity, billing, and cleanup are not
attested by that digest. `launch_readback.resolved_image_digest` must remain
`null`; an operator-supplied requested reference cannot populate it as observed.

## Prepare an exact private plan

Use the installed package from the reviewed source. The preparation CLI has no
provider, Docker, GPU, or network side effects:

```sh
python -I -m inferdrome.deployment.vast_process template
python -I -m inferdrome.deployment.vast_process module-digests
python -I -m inferdrome.deployment.vast_process prepare \
  --input /workspace/inputs/vast-input.json \
  --output /workspace/preparation
```

The template intentionally contains invalid `null` placeholders for unknown
live facts. Fill them from reviewed inputs; do not treat the template as runnable
evidence. `prepare` validates the strict `inferdrome.vast-process-input.v1`
object, creates the destination once, writes canonical `plan.json` with mode
0600 in a mode-0700 directory, and prints `plan_sha256`. It does not overwrite an
existing preparation directory.

Required inputs include:

| Input | Binding or limit |
| --- | --- |
| `source_commit` | Exact 40-character lowercase commit hash |
| `instance_id`, `offer_id` | Positive safe integer provider identities, independently retained |
| `container_image.reference` | Immutable OCI digest reference, not a mutable tag |
| `launch_readback` | Same instance and requested image; `args`, user `2000:0`, no public mappings or persistent volumes; `OPERATOR_SUPPLIED_NOT_PROVIDER_ATTESTED` |
| `gpu_uuids` | Exactly two distinct full GPU UUID strings |
| `uid`, `gid` | Exactly 2000 and 0 |
| `model_path`, `preparation_path`, `evidence_path`, `cache_path` | Bounded canonical absolute paths, pairwise distinct and non-overlapping |
| `module_sha256` | Exactly the six module names printed by `module-digests` |
| `request_timeout_ms` | 1–60,000 |
| `readiness_timeout_seconds` | 1–600 |
| `campaign_timeout_seconds` | 1–1,800 |
| `cleanup` | Same instance ID, accountable operator, canonical future UTC deadline, exact-ID destroy/readback method, initial state `NOT_REQUESTED_NOT_VERIFIED` |

The four runtime directories must already exist, resolve without aliases, and
be owned by UID 2000 at execution time; the evidence directory must be empty.
The model snapshot and tokenizer must already be staged and pass the frozen
snapshot verification. Preparation creates only the plan directory. It does not
download a model, mount storage, provision paths elsewhere, or transfer files
into a Vast instance. Keep raw readbacks, credentials, UUIDs, paths, and private
observations outside published evidence.

## Execute once and export verified evidence

For a fresh Vast instance, use the implemented [bounded bootstrap and broker
workflow](VAST_BOOTSTRAP_V1.md). It acquires the instance/GPU facts, stages and
verifies input, obtains exact canonical plan approval, then reaches the
supervisor described here. The direct execute form below assumes preparation
already exists and is not the fresh-instance startup sequence.

The image entrypoint is:

```text
/opt/inferdrome-runtime/bin/python -I -m inferdrome.deployment.vast_process_runtime
```

After the launch gates below are satisfied, its `args_str` corresponds to:

```text
execute --directory /workspace/preparation --execute-plan sha256:PLAN_DIGEST --operator ACCOUNTABLE_OPERATOR --accept-manual-cleanup-risk
```

Replace both placeholders with the exact prepared values. The operator must
match `cleanup.accountable_operator`; the risk flag is mandatory. It acknowledges
the unresolved external cleanup boundary and does not arm a provider watchdog.

`execute` writes a create-only `execution-attempt.json`, runs bounded local
preflight, starts both loopback engines, waits for both health endpoints, then
starts the observer. The observer performs the fixed three policy trials with
six requests each and no retry. GPU/DCGM and KV/cache remain unavailable signals.
The supervisor bounds preflight, readiness, campaign duration, and remaining
time to the declared UTC deadline. It attempts to terminate every owned process
group on success, failure, timeout, and interruption, escalating to SIGKILL even
if a group leader has already exited. Guest process cleanup is separate from
provider destruction.

The supervisor reserves 20 seconds before the declared deadline when calculating
active-work time limits, allowing for bounded teardown. Readiness probes run in disposable
supervised children; the bounded GPU probe stays in its preflight process group.
SIGTERM and SIGINT are deferred while owned groups are being torn down. The
observer requires the matching execution-attempt marker, consumes its own
create-only attempt marker, and applies a wall timer even through its direct CLI.
These guest controls still cannot replace the external deadline controller.

`preflight --directory ... --execute-plan ...` is also available as a one-shot
diagnostic. It writes create-only `runtime-observation.json`. Because `execute`
runs that stage itself, do not run standalone preflight and then execute the
same preparation directory. Repeated attempts are not supported; diagnose a
failure and prepare a new reviewed plan and destination.

On success, the supervisor re-verifies
`EVIDENCE_PATH/routing-execution-package`, binds it to the prepared config, and
returns `status: VERIFIED_PACKAGE`, its retained digest, and
`provider_cleanup: CLEANUP_UNCONFIRMED`. Failure returns exit status 2 with a
bounded failure result and the same unconfirmed provider cleanup state.

Export only the verified package to a new destination before external
destruction:

```sh
python -I -m inferdrome.deployment.vast_process export \
  --package /workspace/evidence/routing-execution-package \
  --expected-digest sha256:RETAINED_DIGEST \
  --output /workspace/exported-package
```

Export revalidates the digest and publishes only canonical executed-manifest,
input-transfer, and producer-receipt records plus their integrity manifest. It
does not crawl or archive the private preparation directory. Exporting within
the guest is not an off-instance backup; verified transfer and receipt readback
remain required before destroying instance storage.

The dashboard reader verifies the sealed package before projecting list/detail
JSON. It shows one container image, declared process topology, bounded artifact
provenance, and the existing measurement-only ledger. It withholds invalid or
tampered packages. Synthetic test packages do not establish GPU serving.

## External destroy and absence readback

The guest adapter has no provider client or credential handling. The new
[operator control interface](VAST_BOOTSTRAP_V1.md) supplies durable journals,
bounded callbacks and a separate deadline-guard service, with live provider and
worker-launch adapters explicitly required. An external
accountable controller must retain the created instance's exact ID and remain
able to act if the guest never starts or becomes unreachable. A create response
returns `new_contract`, the instance ID; it is distinct from the accepted offer
ID. Never substitute a label, offer ID, or another running instance's ID.
[Create instance](https://docs.vast.ai/api-reference/instances/create-instance).

The external controller must request destruction of that exact instance using
`DELETE /api/v0/instances/{id}` (CLI: `vastai destroy instance <id>`), retain the
acknowledgement, then obtain a successful authenticated absence readback for the
same ID. Destruction is irreversible and removes instance data. Guest exit,
engine termination, a stopped status, or a destroy request without acknowledgement
does not satisfy this contract.
[Destroy instance](https://docs.vast.ai/api-reference/instances/destroy-instance).

The current show-instances API is `GET /api/v1/instances`. It supports an exact
`id` filter, has at most 25 results per page, and uses `next_token` /
`after_token`. Check response success, validate the queried identity, and exhaust
pagination before asserting absence; a timeout, auth failure, rate limit,
malformed response, or empty page without completed pagination is not sufficient
evidence.
[Show instances](https://docs.vast.ai/api-reference/instances/show-instances).

`DestroyReadback` and `cleanup_state` are pure Python validation helpers, not CLI
commands or API callers. They accept only sanitized controller observations.
The positive result is
`OPERATOR_REPORTED_DESTROY_AND_ABSENCE_NOT_INDEPENDENTLY_ATTESTED`, requiring all
of the following: expected, receipt, and query IDs agree; destruction was
acknowledged; readback succeeded at or after the destroy request; no matching
instance remains; pagination is exhausted; and no persistent volumes remain.
Every missing, chronologically invalid, partial, conflicting, or failed receipt yields
`CLEANUP_UNCONFIRMED`. Even the positive label is an operator report, not a
provider attestation embedded by the guest.

This profile admits no persistent volumes. Instance storage is lost on destroy,
while stopped instances continue to incur storage charges. Volumes survive
instance destruction and have separate billing, so an unexpected volume is a
cleanup exception requiring separate exact-ID handling; it cannot be cleared
by pretending the instance destroy covered it.
[Storage types](https://docs.vast.ai/guides/instances/storage/types).

## Remaining paid-launch gates

Before accepting an offer or publishing a runnable launch request, resolve and
review these concrete items:

1. Build and verify the new image from the exact source, inspect final metadata
   and user/entrypoint, and separately authorize any registry publication. Retain
   the digest; do not reuse a pre-adapter image or invent a resolved digest.
2. Select a live offer that meets the exact two-H100 profile and has sufficient
   CPU, RAM, shared memory, and instance disk for both engines and the frozen
   model. Review current price, duration, disk charges, and spending limit.
   No availability or cost promise follows from the synthetic tests.
3. Satisfy the enforced prelaunch transfer gates in the implemented
   [bootstrap/broker profile](VAST_BOOTSTRAP_V1.md): independently authenticated
   pinned broker host-key evidence, documented exact-ID/path mapping and
   UID2000:GID0 compatibility. No live enrollment or compatibility follows from
   fakes. Supply reviewed live provider/guard adapters; the source does not
   guess broker negotiation or enroll a host key. Keep strict `args`, no guest
   public ports and no persistent volumes.
4. Retain sanitized create and launch facts for the exact instance; verify the
   effective `args` entrypoint, `2000:0` user, requested immutable image, empty
   mapping/volume inventories, and the declared GPU UUIDs. Plan validation checks
   supplied facts, not a live provider state.
5. Put the external deadline controller in place before launch, including exact
   instance-ID persistence, acknowledgement/readback failure handling, and an
   accountable operator able to destroy the allocation when guest startup,
   process cleanup, transfer, or provider queries fail. The in-guest timeout does
   not protect image pulls or a guest that never starts.
6. Review the exact canonical plan digest, timeout bounds, cleanup deadline,
   export destination, and bounded execution authorization. Execute once; retain
   verified evidence and complete external destruction/readback regardless of
   the experiment's outcome.

Official provider references above were checked on 2026-09-13. Recheck their
launch and lifecycle semantics when preparing a real paid run.
