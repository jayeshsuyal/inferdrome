# Load-calibrated routing: bounded manual-host operator path

This is a local execution boundary for the existing load-calibration rehearsal.
It is for one manually rented Linux host with two preloaded, locally available
GPUs. It is not a cloud provider client, host provisioner, deployment platform,
production router, evidence-publication path, or termination controller.

The command can use exactly two local, loopback-addressed engines. The vLLM
path launches the existing digest-pinned `vllm` image twice, on GPU 0 and GPU
1. The SGLang path uses two separately supplied, pinned SGLang profiles. Both
paths retain only sanitized, digest-bound control facts; raw prompts, generated
content, endpoint origins, credentials, provider payloads, and invoice facts
are outside the retained operator sidecars.

## One-minute operator explanation

1. Compile the fixed protocol and two candidate recipes with the offline
   preflight command. It opens no Docker client, socket, provider SDK, or
   network connection.
2. An accountable human separately supplies an exact authorization packet and
   takes the external rental-provider termination action. Inferdrome records
   only the pseudonymous aliases and digests of that external handoff; it does
   not verify that the provider action happened.
3. The explicit run command starts one absolute session clock *before* it reads
   the packet and compiles recipes. It refuses expired approval, a missing
   cleanup reserve, cancellation before dispatch, or an unsafe local Docker
   configuration before constructing a lifecycle or invoking Docker.
4. Each native trial gets new exact-owned containers, one per GPU. The existing
   lifecycle records per-trial preparation/cleanup and, for SGLang, a durable
   `last_reset` sidecar with sanitized server-accepted reset/readiness facts.
5. Export copies only a finite allowlist of regular rehearsal artifacts into a
   no-replace archive. The receiver rehashes every copied byte and verifies its
   embedded canonical inventory. `COMPLETE` means the local rehearsal recorded
   completion only; it never proves provider termination or makes evidence
   eligible.

## Offline preflight

The preflight input consists of one load-calibration protocol and two to eight
operator recipes. A recipe contains one calibration and one confirmation native
`StudyConfig`, plus its level alias. All inputs must be private regular files.

```sh
PYTHONPATH=src python -m inferdrome.evaluation.cli \
  load-calibration-host-preflight \
  --protocol /private/operator-input/protocol.json \
  --recipe /private/operator-input/load-low.json \
  --recipe /private/operator-input/load-high.json \
  --runtime vllm \
  --output /private/operator-output/preflight.json
```

This produces a canonical preflight with the protocol, recipe, duration, and
output reservations. It is deliberately marked
`DECLARED_NOT_OBSERVED`, `provider_action_performed: false`, and
`evidence_eligible: false`. It is safe to run in local CPU CI.

## External facts required before a run

The run authorization is a closed
`inferdrome.load-calibration-host-authorization.v1` document. It requires all
of the following exact, non-placeholder facts:

- the confirmed source commit, runtime, pinned serving image identity, Qwen3-8B
  revision, and frozen snapshot-manifest digest;
- the preflight protocol digest and every recipe digest, plus private absolute
  path identities for the model snapshot, empty Docker config directory, and
  fresh output directory;
- a bounded execution deadline, maximum runtime, final termination margin, and
  an `EXTERNALLY_ARMED_NOT_VERIFIED` Vast manual-host guardian handoff;
- pseudonymous authorization, approval, host, and guardian aliases, with
  digest-bound external identity and cleanup-handoff records; and
- a unique exact-owned local container ownership alias.

Alias syntax is data minimization, not proof of provenance or secret detection.
Operators must never place addresses, credentials, real host IDs, or provider
payloads in this packet. The external guardian remains responsible for exact
host termination and independent readback after every exit condition.

Before execution, create an owner-only Docker config directory containing only
an owner-only `config.json` whose exact bytes are `{}`. The local runner clears
ambient `DOCKER_*` and `COMPOSE_*` selectors and invokes every Docker command
with exactly `--host unix:///var/run/docker.sock --config DIRECTORY`. It never
uses a discovered remote context or an implicit Docker configuration.

## Explicit local execution

The following command is intentionally not an approval. It is usable only
after a separate live-execution authorization and only on the approved manual
host. It has not been run by repository tests.

```sh
PYTHONPATH=src python -m inferdrome.evaluation.cli \
  load-calibration-host-run \
  --protocol /private/operator-input/protocol.json \
  --recipe /private/operator-input/load-low.json \
  --recipe /private/operator-input/load-high.json \
  --runtime vllm \
  --authorization /private/operator-input/authorization.json \
  --model-snapshot /private/preloaded-qwen3-8b \
  --docker-config-directory /private/operator-docker-config \
  --output-root /private/operator-output/rehearsal \
  --execute-approval I_UNDERSTAND_LOCAL_DOCKER_WILL_BE_INVOKED
```

The CLI anchors paired UTC and monotonic timestamps before it reads the
protocol, recipes, or authorization. The hard monotonic cutoff is derived once
from that original anchor and is never renewed. `SIGINT` and `SIGTERM` set the
same stop event that reaches the native rehearsal, preventing later trial
dispatch while the reserved lifecycle cleanup and artifact-retrieval windows
remain bounded by the original cutoff. If preflight consumes the authorization
window, no lifecycle or Docker runner is constructed.

The vLLM lifecycle also verifies the preloaded snapshot through the existing
no-download Qwen3 manifest boundary before it starts an engine. SGLang records
its completed reset/readiness sequence only after both local engine resets
succeed; `SERVER_ACCEPTED` is not cache attestation or runtime proof.

## Retrieval and verification

After the local session has stopped and the external guardian has independently
handled the rental host, copy the local output through a controlled transfer
mechanism outside Inferdrome. On the producing host, use the bounded export:

```sh
PYTHONPATH=src python -m inferdrome.evaluation.cli \
  load-calibration-host-export \
  --output-root /private/operator-output/rehearsal \
  --archive /private/operator-retrieval/rehearsal.tar
```

At the receiving location, reverify the retrieved archive before examining it:

```sh
PYTHONPATH=src python -m inferdrome.evaluation.cli \
  load-calibration-host-verify-export \
  --archive /private/operator-retrieval/rehearsal.tar \
  --expected-archive-sha256 'sha256:<producer-export-digest>'
```

Both operations reject symlinks, hardlinks, FIFOs, directories outside the
declared grammar, changed files, duplicate destinations, and artifacts above
their file/count/total-byte limits. Export reads only regular files through
held directory descriptors, hashes the copied bytes, and writes the archive
create-no-replace. Verification requires the producer-side archive digest,
then rechecks the embedded ordered inventory. It never treats an archive that
is merely self-consistent as the expected transfer.

## Explicit limitations

- This module cannot create, inspect, terminate, or prove absence of a rental
  host. Provider cleanup and readback are external human obligations.
- It does not download a model, pull or publish an image, access a registry,
  use a cloud credential, open SSH, or create a provider resource.
- Two engines on one host are not host-failure independence. A successful
  local run is not a Qwen3 qualification, routing winner, acceptance verdict,
  capacity result, invoice fact, or production-readiness claim.
- A local artifact is `evidence_eligible: false` until a separately reviewed
  live-evidence and privacy/publication path says otherwise.
