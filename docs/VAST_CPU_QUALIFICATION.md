# Vast CPU image qualification and publication

This is a manual execution route defined in source. It does not establish a
current worker, dispatch authorization, qualified image, registry access, or
publication. The legacy role-image publication workflow remains separate.

The only publication destination is the private package
`ghcr.io/jayeshsuyal/inferdrome-vast-process`. The source is the exact reviewed
commit containing `Dockerfile.vast-process` and the qualification helpers. The
image platform is `linux/amd64`; this route never attaches GPU devices, calls
Vast, rents a GPU, serves inference, merges a PR, or changes a default branch.

## Source review and read-only prerequisites

Review the final commit and CI results, including the workflow, build helper,
real Linux qualification probes, and fake policy tests. Record the source SHA
after all source changes. The selected GitHub ref must be exactly
`refs/heads/main` or `refs/heads/codex/add-vast-process-adapter`; it must resolve
to the same commit as `source_commit`, `GITHUB_SHA`, and the clean checkout's HEAD.

Verify that GitHub exposes `vast-cpu-qualification.yml` for manual dispatch at
that ref. GitHub requires a manually dispatched workflow to exist on the default
branch; candidate source and a green PR alone do not establish dispatch
availability. See [GitHub's manual workflow requirements](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).
Any needed merge or default-branch change requires separate
authorization. Do not dispatch another workflow or reuse the role-image targets
as a substitute.

Check current project eligibility, quota, worker availability, and pricing using
existing authenticated access. The proposed CPU worker is one
`e2-standard-4` with a 200 GiB `pd-balanced` auto-deleting disk in
`voltaic-signal-483602-p7/us-central1-a`. Verify the selected immutable Ubuntu
24.04 image, no attached service account, the current VM/disk/IPv4 quote, registry
upload egress, logs/taxes assumptions, and the approved outbound byte ceiling.
Historical quotes and a previous deleted worker do not authorize this attempt.

Verify private package visibility and the repository's ability to publish to the
single destination using a job-scoped GitHub token. Do not acquire additional
credentials or alter package visibility to make the check pass. Package-write
permission applies throughout the selected publication job; explicit
`GHCR_TOKEN` exposure occurs only in its publish step after qualification.
Checkout credentials are not persisted. Qualification mode has contents-read
permission only.

## One consolidated live authorization

After the source and read-only checks are complete, present one concrete packet:

- exact source SHA/ref, chosen mode and matching confirmation, fixed image target,
  source/run-ID tag binding, and exact unique worker alias;
- one CPU worker's immutable image, project/zone/type, disk size and auto-delete
  policy, current quote, total spending ceiling, and maximum outbound bytes;
- authorization for this one worker/runner setup, dispatch, image downloads/build,
  real loopback SSH/SFTP and UID-drop tests, the pinned model download/hash/copy,
  and optional publication/private registry readback;
- the original UTC attempt start, execution deadline at start plus 105 minutes,
  cleanup deadline at start plus 120 minutes, provider-native maximum-runtime
  DELETE, and exact resource IDs and cleanup/absence-verification responsibility.

No resource, credential, runner, SSH, build, download, publication, or spending
action is authorized by this document. A source-only approval remains source
only. There is one attempt, `GITHUB_RUN_ATTEMPT=1`, with no automatic retry or
deadline renewal. A new attempt requires a new packet and original clock.

## Execution after that approval

The operator provisions the approved fresh worker and its provider-native DELETE
bound outside this workflow. Register only that worker with the labels
`self-hosted`, `Linux`, `X64`, `inferdrome-vast-cpu`, a unique approved name, and a
clean workspace. This repository contains no runner-registration or provider
creation step in the workflow.

Dispatch the reviewed workflow once with all nine required inputs:
`source_commit`, `expected_ref`, `execution_mode`, `confirmation`,
`expected_runner_name`, `attempt_start`, `execution_deadline`,
`cleanup_deadline`, and `max_egress_bytes`. Choose `QUALIFY_ONLY`, or choose
`PUBLISH_QUALIFIED_VAST` only when the packet authorizes publication; the
confirmation must be the identical string. The attempt ID is the GitHub run ID.

Both mutually exclusive worker jobs reject the wrong repository/event/ref/SHA,
mode, worker name, platform, and rerun before checkout. The helper validates
the clean source and original clock before the pinned Python and frozen
development environment are prepared. Qualification and publication receive
the same arguments and original deadlines. Queue delays consume the existing
attempt budget; they do not move its start. The 110-minute job timeout is an
additional limit, not the provider deletion bound. The pre-checkout clock gate
and validation step each derive the next action's timeout from the original
15-minute setup window. Checkout and Python setup each allow at most five whole
minutes, reserving five seconds; they refuse to start when less than one whole
minute plus that reserve remains. Dependency bootstrap recomputes the same
absolute setup deadline, allows at most 600 seconds, and reserves five seconds
for forced termination.

Every phase consumes the original attempt clock:

| Phase | Bound from the original attempt start |
| --- | --- |
| Checkout, dependencies, base pull, OS resolution and source tests | Finish by 15 minutes |
| Build and static image checks | Finish by 55 minutes |
| Stock loopback SSH/SFTP qualification | At most 10 minutes, within qualification |
| Pinned model download, snapshot and hashes | At most 35 minutes, within qualification |
| All qualification | Finish by 100 minutes |
| Publication, when selected | At most 5 minutes; finish by 105 minutes |
| Cleanup and absence verification | Finish by 120 minutes |

Queue delays and earlier phases reduce the time left for later work; completion
of a phase never renews a deadline.

Qualification resolves and records actual snapshot/package inputs; no OS lock
or resolved inventory is claimed before that happens. Build only from the
validated source, then bind the image labels, source/module markers, package
inventory and test results to the local image ID. Random transfer-account hashes
mean this process does not claim byte-identical rebuilds.

Real Linux checks must prove root-owned stock SSH startup, key pinning, transfer
UID2001 permissions, private UID2000 paths, rejected shells/root login/forwarding,
and permanent UID/GID/capability drop. Use loopback for CPU transport; preserve
the production global-address validator. Fake rehearsals exercise the complete
protocol without a provider. The real pinned model stage downloads and hashes
15 files totaling 16,397,461,266 bytes and creates the verified snapshot. It does
not prove GPU execution.

Measure image logical size, actual allocated storage and free disk. Model staging
needs at least 37,864,915,100 bytes beyond the measured image and working storage
(two model copies, the largest-file scratch allowance, and 1 GiB headroom).
A nominal 200 GiB disk is a proposed starting capacity, not a verified fit.
The helper samples host network transmitted-byte counters only after checkout,
when validation creates the attempt baseline. It observes subsequent setup,
build, qualification and publication traffic, but cannot account for earlier
worker setup, runner registration or checkout traffic. This host counter is
an observation limit, not a complete provider egress or spending cap. The
operator's separate accounting must include those earlier bytes and all worker,
disk, network and other approved charges.

Publication consumes the successful local qualification receipt on that same
worker, pushes only the fixed target with its unique source/run tag, and verifies
the private registry's immutable digest before recording success. A tag or
successful push alone is not qualified publication evidence. There is no
separate artifact-fed publish shortcut or automatic retry.

## Evidence and cleanup

The helper's private attempt directory is
`$RUNNER_TEMP/inferdrome-vast-$GITHUB_RUN_ID`. Only bounded sanitized
`evidence/*.json` files are retained for 14 days. Never upload raw logs, keys,
tokens, Docker configuration, downloaded packages, model files, or the work root.

After successful validation, cleanup runs on success or failure and acts only on
resources identified by the attempt's bound ownership manifest. It preserves
sanitized evidence for the final artifact upload. It never prunes Docker or
deletes resources by broad labels. The operator must also deregister the exact
runner and verify the exact VM, disk and any other approved temporary resources
are absent. Runner deregistration, container exit, job cancellation and workflow
timeout are not VM deletion. Provider-native DELETE remains the external bound
if the worker or controller is lost; unknown IDs or unconfirmed deletion must be
reported truthfully and require authorized reconciliation.

Only after successful image, real CPU model/SSH checks, publication when selected,
and confirmed cleanup can this route's pre-GPU work be called complete. GPU
execution and its separate live authorization remain outstanding.
