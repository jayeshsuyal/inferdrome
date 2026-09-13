# Owned stock SFTP guest profile

Status: **source implementation and fake verification; image wiring remains
blocked by automatic approval review**. No image has been built or published,
no credentials or provider connection have been used, and no rental has been
created. The pending `Dockerfile.vast-process` patch installs OpenSSH and the
separate hash-locked downloader, provisions the two accounts, emits a v2 build
marker, and switches the final user/entrypoint to the owned root supervisor.
The current Dockerfile still describes the legacy v1 runtime.

**An unknown create ID or loss of the operator host can leave a billable
instance.** The detached cleanup guard survives its controller, not its host.
Unknown outcomes require separately authorized account reconciliation; the
controller never guesses an ID, repeats create, or destroys account-wide.
Guest shutdown is not provider termination. These limitations must accompany
any future price, disk and time proposal.

## Closed launch and versioned records

`SftpLaunchIntent` uses `inferdrome.vast-launch-intent.v2`. Its compiler emits
exactly these create fields:

- the reviewed immutable image reference and explicit measured disk request;
- `runtype=args`, `target_state=running`, `user=0:0`;
- `env={"-p 2222:2222":"1"}` as the only management mapping;
- fixed `serve` arguments carrying the fresh nonce, canonical intent digest,
  original execution and cleanup deadlines, and the operator's public key.

The intended entrypoint is:

```text
/opt/inferdrome-runtime/bin/python -I -m inferdrome.deployment.vast_guest_ssh
```

There is no template, onstart command, provider SSH daemon, Jupyter, broker,
inference port mapping, persistent volume or container credential discovery.
The private operator identity remains local. `VastSftpLaunchReadback` records
root management identity, transfer UID2001:GID0, and exactly one TCP2222 mapping
with a canonical globally routable IPv4 address. The readback is explicitly
operator-supplied, not provider attestation; no resolved image digest is
invented from the requested image. Operators must review the exact-ID mapping
from the authenticated provider readback before supplying it.

The v2 input, launch, ready, stage, approval and result schemas live under
`schemas/vast-process/v2`. Their source inventory includes the original six
modules and the provider, guard, SSH trust, guest supervisor, SFTP transfer and
model stager. The original v1 schema snapshots and sealed experiment evidence
contracts remain unchanged. A v1 readback cannot claim the new root/mapping
facts.

## Startup and key trust

`vast_guest_ssh` requires Linux x86_64, root real/effective/saved IDs, PID1 and
a canonical positive `CONTAINER_ID`. It validates original deadlines before
creating any state. The accounts must be `inferdrome-transfer` UID2001:GID0
and `inferdrome-experiment` UID2000:GID0 with executable nologin shells. The
transfer account must be nonlocked for key authentication; the dedicated
sshd configuration disables password and keyboard-interactive authentication.

The supervisor generates one fresh Ed25519 private host key in a root0700
directory and derives the public key from that exact private file. A separate
root0755 public directory contains the root0644 authorized-key file, so sshd
can read it as the transfer user. Existing keys, repeated startup, a global
`/etc/ssh/sshrc`, or an occupied exchange directory fail closed.

The dedicated daemon listens on internal2222. It admits only the transfer
account, forces stock `internal-sftp`, and disables root login, shells, PTYs,
forwarding, user rc and user environment. Configuration has no `Include` or
custom wire protocol/dispatcher. Private key/configuration files are root-owned
and not group writable. The chroot and its ancestors are root-owned.

After daemon readiness, container stdout receives only the bounded public
host-key announcement. `sftp_workflow` obtains authenticated exact-ID logs,
requires the current nonce, and enrolls an immutable `SshPinJournal` entry.
Only a missing startup marker is polled within the original budget. Stale,
conflicting or malformed evidence fails immediately. Stock SFTP uses a private
snapshot of this pin with strict host checking and the explicit local identity.
The SSH handshake proves possession of the announced key. This trusts Vast's
control plane/log delivery and is not hardware attestation.

## Two identities and file admission

| Path | Owner and mode | Role |
| --- | --- | --- |
| `/srv/inferdrome-sftp` | root:0,0755 | Stock OpenSSH chroot |
| `uploads` inside chroot | 2001:0,0750 | Untrusted transfer controls,0640 |
| `downloads` and `downloads/export` | 2000:0,0750 | Guest-published copies,0640 |
| `/workspace` and private bootstrap roots | 2000:0,0700 | Admission, markers, model, execution state |

`SftpTransfer` permits exactly three control uploads: intent, stage and
approval. It uploads to an allowlisted pending name and uses stock rename for
visibility. Its receipt describes verified local bytes; it does not assert
remote no-replace durability. Guest admission is the commit boundary.

The guest opens each foreign upload without following links, requires the
exact owner/group/mode, one regular-file link and a bounded stable snapshot,
then validates canonical bytes and nonce/digest bindings. It copies the record
into a new private owner-created file using fsync and no-replace publication.
Later transfer edits cannot replace private admission or consumed markers.
Partial, replaced, malformed, stale and duplicate records fail closed.

Downloads are separate copies of ready, plan, result and the existing four
sealed export files. Export directory mode is set explicitly even under
umask077. The transfer user has read access but no write access to these
copies or the private originals. Control records retain the131072-byte cap;
each export retains the8388608-byte cap. Local retrieval checks bytes before
no-replace publication and re-verifies the complete sealed package and plan
binding before reporting success.

## Guest-only model staging and disk

The operator sends no model files. The guest uses a separate interpreter at
`/opt/inferdrome-downloader/bin/python` with all transitive requirements and
wheel hashes locked in `requirements-vast-downloader.txt`. The downloader uses
`huggingface-hub==1.3.2`, a fixed public endpoint, no token, no ambient HTTP
credentials/proxies, and no XET helper. Importing or constructing the stager
performs no download.

The only model is Qwen/Qwen3-8B at full revision
`b968826d9c46dd6066d109eabc6255188de91218`. The frozen15-file inventory totals
16,397,461,266 bytes. Each file is fetched into a fresh private scratch
directory, checked for exact size/hash and safe file identity, and copied into
a new private file. Scratch is removed before the next file. The existing
snapshot validator then creates the execution copy. Download and snapshot
validation share one stage budget within the original execution deadline.
The operator receives the completed plan within its staging budget before
starting the separate approval window.
Partial staging is never silently retried or adopted.

There is **no v2 default80GB disk claim**. `DiskFootprint` requires the measured
unpacked image footprint, a digest of its reviewed measurement, explicit
runtime headroom of at least1GiB, and the requested decimal GB. Validation
requires space for the image, two model copies, one largest-file scratch
reserve and runtime headroom. The guest checks free space again before
staging. Image/package installation footprint remains unmeasured until a
separately authorized build; a live disk request is therefore unfinalized.

## Execution, deadlines and verification scope

The supervisor starts bootstrap as UID2000:GID0 with supplementary groups
cleared and a closed environment. Before v2 initialization the guest verifies
its permanent identity, enables `no_new_privs`, and refuses effective,
permitted, inheritable or ambient capabilities. Model staging and experiments
inherit these restrictions. Inference remains in the existing separate
processes sharing one container; it is not container or host isolation.

Plan approval still binds the complete canonical plan. The existing fixed
18-terminal workload, supervisor, sealer, exports and retrieval verification
are reused. Private stage-attempt and approval-consumed markers prevent replay.
Management remains available after guest success/failure for bounded retrieval;
owned children are stopped with waits clamped to the original cleanup deadline.
PID1 exit tears down remaining SSH sessions. The external exact-ID provider
destroy and absence readback remain authoritative for cleanup evidence.

Fake tests exercise startup commands and deadlines, pinned stock SFTP batches,
foreign upload admission, guest model staging, versioned contracts, approval,
execution/export/retrieval and failure paths. They do not validate an installed
OpenSSH daemon, account behavior, actual image footprint, GPU runtime, provider
mapping, network transfer, model download or billing. Those require their
separately authorized image and live qualification steps.
