# Deployment Qualification v1

Deployment Qualification v1 is a local, synthetic orchestration check for the
accepted Docker Compose mock. It is a qualification of this repository's
Compose boundary, not a serving-engine benchmark, deployment receipt, hardware
attestation, or customer-acceptance decision.

## Guarded command

The only entrypoint is the explicitly confirmed command below. The output root
must already exist, be absolute, and be a real directory.

```bash
PYTHONPATH=src python scripts/run_deployment_qualification.py \
  --confirm-synthetic-compose \
  --output-root /absolute/path/to/qualification-reports
```

The command requires Python 3.12 and Docker Compose v2. It accepts no Compose
file or endpoint override. It reads the repository's local mock deployment
specification, `compose.yaml`, and the checked-in vLLM Compose/runtime
contracts, then verifies their exact digests before starting anything.

The command selects one validated unique project name of the form
`inferdrome-qual-<random-hex>`. It runs only the `synthetic-smoke` root service;
Compose starts the separate `mock-engine` service required by its healthcheck.
Before Compose starts, it writes a private, mode-0600 override containing only
the qualification-owned image references
`inferdrome/compose-mock:<project>` and `inferdrome/runner:<project>`. Those
exact references are checked for pre-existing tags and are used for both the
build and subsequent image observations; fixed global development tags are not
used. The runner writes one bounded JSON payload to the private evidence mount.
qualification code compares canonical bytes and the complete deterministic
mock vector, including endpoint, model, request, response, status, and runner
version identities.

## Cleanup and publication

After a workflow has started, every success, expected failure, cancellation,
and interrupt attempts this exact command, using the generated project and
fixed Compose file plus its private override:

```text
docker compose -p <validated-project> -f <repository>/compose.yaml -f <private-override> down --remove-orphans --volumes
```

Cleanup is retried only within the local deployment policy. The command then
lists containers, networks, and volumes using the exact
`com.docker.compose.project=<project>` label and verifies each returned
resource's label before declaring zero residuals. A cleanup, residual, or
temporary-directory failure overrides a workflow failure or success. It then
removes only the two exact project-derived image tags with
`docker image rm --no-prune` and verifies both exact references are absent. It
never removes an image ID, uses a broad prune, or targets another tag sharing
an image ID. Image-tag cleanup failure also dominates publication. A project or
image-reference collision is rejected before workflow start, so it cannot
trigger cleanup of an existing project or tag.

On complete success, the command publishes one canonical `qualification.json`
under a versioned digest directory using the repository's no-replace immutable
publisher. It reads the bytes back and independently verifies canonical form,
strict closed parsing, the domain-separated qualification identity, source
revision, specification/contract/file digests, output digest, cleanup project,
exact image references, image-tag cleanup confirmation, and zero residual
counts. Immediately before report construction, it re-observes a clean source
checkout and requires the exact same revision observed before the workflow.
Failed, drifted, or interrupted attempts publish no report. Docker failure
diagnostics are bounded and redacted before they are printed.

Docker and Compose are resolved once from fixed system/Homebrew search
directories and every later start must match that absolute filesystem
identity. Each child receives a small environment with a qualification-private
`HOME`, `TMPDIR`, and initially empty `DOCKER_CONFIG`; ambient Docker contexts,
credential helpers, cloud variables, and the operator's `PATH` are not passed.
Process runtime, pipe draining, descendant process-group termination, and final
reader joins share one monotonic hard deadline. A leader that exits while a
descendant still holds stdout or stderr is a timeout failure; TERM/KILL and
final joins use a small fixed grace inside that deadline, and the controller
never closes a stream underneath a live reader.

The report is always `SYNTHETIC_ONLY` with `evidence_eligible=false`,
`provider_execution=NOT_PERFORMED`, `gpu_execution=NOT_PERFORMED`,
`deployment_receipt_issued=false`, and `evidence_published=false`. It contains
no acceptance verdict or hardware claim. Docker image IDs and repository
digests are observations only when Docker returns valid identities; unavailable
image identity is recorded as unavailable rather than inferred from the
Compose configuration.

## CI gate and local limits

`scripts/deployment_qualification_gate.sh` checks the generated schema, runs
the injected unit/contract suite, and runs the real Docker Compose workflow
when Docker is available. It also runs a deterministic output-mismatch case to
exercise failure cleanup. Local hosts without Docker report the E2E as
unavailable and pass the non-Docker portion; CI requires Docker and fails if
the real gate cannot run.

This check proves only local Docker/Compose synthetic orchestration. It does
not prove Linux/NVIDIA/CUDA/vLLM behavior, cloud capacity or pricing, model
availability, provider execution, evidence eligibility, or ExitSpec acceptance.
