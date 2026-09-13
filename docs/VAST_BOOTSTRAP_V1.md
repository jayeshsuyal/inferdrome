# Vast bounded bootstrap and broker transfer v1

This is an implementation and local fake rehearsal of one file-driven launch
profile. It supplies no live provider client or independent guard launcher, no
authenticated broker enrollment method, no image build, and no paid launch.
The adapter deliberately refuses missing transfer prerequisites. Real host-key
trust, broker compatibility with UID2000:GID0, and provider behavior must still
be established by the accountable operator before launch.

The profile retains strict `args`, one image, two H100 serving processes, zero
guest public ports, zero persistent volumes, and the existing v4 evidence and
shared-container isolation disclosures. No SSH daemon runs in the guest. The
operator file channel uses SSH to the provider-side rsync broker.

## Provider basis and enforced prerequisites

Vast preserves the image entrypoint in `args` mode, without injected SSH,
Jupyter, or an on-start shell. The final arguments must therefore start our
bootstrap. The documented constrained execute command is not a way to start
Python after creation. [Creation semantics](https://docs.vast.ai/api-reference/creating-instances-with-api),
[execute reference](https://docs.vast.ai/cli/reference/execute).

Vast documents local/instance file transfer, including stopped Docker
instances. This is separate from guest SSH. Its current CLI negotiates a broker
and uses rsync over SSH as `vastai_kaalia`, with an instance-ID module and an
absolute container path. [Data movement](https://docs.vast.ai/guides/instances/storage/data-movement),
[current CLI source](https://github.com/vast-ai/vast-cli/blob/master/vastai/cli/commands/storage.py#L68-L106).

We do not copy the CLI's disabled host-key checks or unchecked subprocess
completion. `BrokerGate` requires an exact instance ID and host/port, a private
operator-owned identity file, a separately retained known-hosts file and its
digest, one matching ED25519 key and fingerprint, and separate evidence for the
broker contract and authenticated key source. Missing, unverified, or unsupported
values fail before a transfer subprocess is constructed. `ssh-keyscan` and
trust-on-first-use are not accepted enrollment sources.

Before create, `LaunchIntent.transfer_prerequisites` must already identify the
reviewed broker contract, authenticated key source/evidence, and verified
UID2000:GID0 compatibility. Unknown prerequisites cannot form a valid intent.
These are explicit operator declarations, not attestations created by this
code. After create, the actual `BrokerGate` must match those approved evidence
bindings and the newly retained exact instance ID. This repository provides no
automatic key enrollment or guessed broker negotiation endpoint. Until an
authenticated source and supported mapping are established, live launch is gated.

The broker runs with `StrictHostKeyChecking=yes`, a private pinned known-hosts
copy, `-F /dev/null`, `IdentitiesOnly=yes`, no agent, no proxy, no forwarding,
no password fallback, and a closed environment. Transfers use fixed filenames,
single-file rsync calls, bounded output/file sizes and wall time, explicit exit
status checking, and owned process-group teardown. Failed receives never publish
a partial destination; publication is atomic and refuses replacement.

## End-to-end sequence

1. Prepare a canonical `LaunchIntent` and a `ControlIntent` whose
   `launch_request_sha256` is the launch intent digest and whose execution and
   cleanup deadlines match. Bind source/image, offer, run nonce, six installed
   module hashes, operator, transfer prerequisites, and finite stage, approval,
   capture and retrieval budgets. Execution may be at most two hours ahead;
   the separate cleanup reserve is 1–600 seconds. Review both exact digests.
2. Supply a reviewed `Provider` adapter and separately hosted `IndependentGuard`
   to `execute_control`. The controller checks the workflow bindings, fsyncs the
   intent, and requires a matching live guard acknowledgement tied to the same
   journal inode/device and a separate process identity before create. The
   adapter must observe the guard with a held worker handle, not trust a PID
   alone. No default same-process guard or live provider adapter is supplied.
3. Create once. The provider adapter must bind its actual request to the approved
   launch digest and use the final image entrypoint and arguments below. Retain
   and fsync `new_contract` as the instance ID, separately from the offer ID.
   If create may have succeeded but no exact ID was durably retained, record
   `CREATE_OUTCOME_UNRESOLVED`: no duplicate create, guessed ID, or broad cleanup.
   The operator must reconcile the account under separate explicit authority.
4. Bootstrap starts as UID2000:GID0 and creates a fresh, owner-controlled
   `/workspace/vast-bootstrap`. It observes `CONTAINER_ID`, installed build
   metadata and two GPU UUIDs, then publishes `outbox/ready.json`. It never reads
   the injected container API key. Readiness is a local observation and does
   not start engines or attest the provider's actual image resolution.
5. `BootstrapWorkflow.readiness` obtains and binds that record. Its transfer
   factory receives only the durably retained instance ID. `stage` sends the
   canonical intent, exactly the frozen model inventory, then `inbox/stage.json`
   last. The guest rejects non-regular, linked, extra, partial, changing,
   incorrectly sized or hash-mismatched files. It copies into fresh UID-owned
   private model files and prepares the complete canonical plan. Foreign file
   ownership is never adopted as prepared private state.
6. `approve` retrieves the full `outbox/plan.json`, reconstructs the expected
   plan and compares every field. Its review callback receives those exact
   bytes and the remaining approval budget. It must return the explicitly
   approved digest. The workflow then sends `inbox/approval.json` last, binding
   nonce, intent, instance, plan, operator, deadline and the cleanup-risk
   acknowledgement. Expired, reused or mismatched approvals do not execute.
7. Bootstrap consumes the approval once, invokes the existing bounded supervisor
   and fixed two-TP1-engine/observer experiment, verifies the sealed package,
   and exports its four records to `outbox/export`. The result record binds
   the exact plan and retained digest. Its status explicitly says the guest
   export has not yet been retrieved.
8. The operator `run` phase waits for the bound result; `retrieve` copies only
   the four export filenames into an empty private destination, seals it
   read-only, re-verifies the retained digest and source/plan identity, then
   durably records successful retrieval. Missing/malformed/tampered evidence
   never counts as success. Read-only readiness/plan/result polling is bounded;
   configuration failures and received malformed records fail immediately.
9. On every outcome or deadline, request destruction of the persisted exact ID.
   The independent `DeadlineGuard.run` can perform this without guest startup
   or a surviving work process. Controller and guard share locked, fsynced
   state. The first cleanup attempt fixes a durable finite cleanup window;
   subsequent attempts or worker restarts cannot renew it. A destroy timeout,
   failed/partial listing, remaining exact ID or unexpected volume stays
   `CLEANUP_UNCONFIRMED`. The positive operator-reported label requires durable,
   complete exact-ID acknowledgement and absence observations. It is not an
   independent provider attestation, nor a billing guarantee.

Creation arguments for the existing image entrypoint are:

```text
bootstrap --run-nonce RUN_NONCE_32_LOWERCASE_HEX --intent-sha256 sha256:APPROVED_LAUNCH_INTENT_DIGEST --deadline YYYY-MM-DDTHH:MM:SSZ
```

The placeholders are deliberately not runnable values. Do not use the old
immediate `execute --directory ...` form on a fresh guest without staged input.
The executable bootstrap dispatch is
`python -I -m inferdrome.deployment.vast_process_runtime bootstrap ...`.
Its paths are fixed by the image code; there is no arbitrary shell or path
argument. It has an overall UTC/monotonic bound and wall alarm. Alarms and
interrupts are deferred only while bounded owned-child cleanup finishes.

## Operator integration interfaces

All modules ship in the wheel. `vast_bootstrap` provides `LaunchIntent`,
`BootstrapWorkflow`, and the guest entrypoint. `vast_transfer` provides
`BrokerGate`, `BrokerTransfer` and distinct configuration/transfer errors.
`vast_control` provides `ControlIntent`, `ControlJournal`, `execute_control`,
the typed create/destroy/absence records and `DeadlineGuard` worker service.
Schemas are checked under `schemas/vast-process/v1`.

The controller takes an explicitly approved control-intent digest, a private
existing journal directory, the provider/guard interfaces, and the workflow.
The workflow takes an already validated launch intent, an instance-bound
transfer factory, a fresh local destination, a locally staged frozen model,
a sanitized launch-readback callback and a full-plan approval callback. A
reviewed deployment integration must supply the actual live provider transport,
authenticated broker/key evidence and independent worker lifecycle. Merely
constructing fake implementations is not a launch-readiness claim.

Control calls run on a POSIX main thread under per-call alarms with late-return
checks. Nested alarms never extend an enclosing deadline. Adapters must not
suppress the alarm, and must clean up their owned subprocesses. Unsupported
thread/platform execution fails before create. The separately hosted guard
remains necessary even with these local timeouts.

The local integration rehearsal starts from an absent guest directory and uses
the real bootstrap, strict broker wrapper with an injected rsync runner, real
controller/journals, fake provider and guard, real supervisor with fake children,
and the real fixed-workload executor, sealer and verifier with fake endpoint IO.
It covers success, never-started guest, failed approval, failed engine,
failed retrieval and destroy timeout. Focused tests additionally cover stale
nonces, exact IDs, partial/tampered model and control files, key/config refusal,
byte/output bounds, process cleanup under alarms, clock rollback, create-response
ambiguity, incomplete/malformed absence and durable receipt failures.

These fakes establish local refusal/state transitions and integrity checks.
They do not establish actual broker enrollment, UID access, GPU serving, image
resolution, provider cleanup, capacity or billing. No live action was performed.
