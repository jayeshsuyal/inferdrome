# v0.1 prospective capture rehearsal packet

Status: **GPU-free rehearsal only; no production capture or ExitSpec verdict**

This packet is the operator handoff for the PR #37 prospective workflow. It
uses the existing `prospective_handoff.py`,
`prospective_real_gpu_capture.py`, and `capture_real_gpu_over_ssh.py` paths.
It does not add a second transport or fixture format.

The rehearsal must remain local and inert. Do not run `ssh`, `scp`,
`ssh-keyscan`, `nvidia-smi`, a provider API call, a model server, or a live
capture while exercising this packet. A later live run is a separately
authorized operation with fresh external inputs.

## 1. Scope and immutable anchors

The required base was checked before preparing this packet:

```text
required merge: 1fac341c474df7a2ea80b91948daf9febdf2d1eb
observed base:  1fac341c474df7a2ea80b91948daf9febdf2d1eb
```

The prospective cases, in required order, are:

1. `native-p95-under-20ms`;
2. `native-p95-under-10ms`; and
3. `semantic-first-nonempty-under-20ms`.

The source archive and handoff archive are independent pins. The values below
are the observed, deterministic rehearsal anchors for the required base and
the checked-in byte fixture. They are not production authorization and must be
recomputed when the clean reviewed source commit differs:

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| Git exact-HEAD source archive for `1fac341c...` | 6,174,720 | `sha256:1b174631391d75db65684574d49d50b349655f7f8de51b826a822ad84ba46375` |
| 12-file fixture handoff archive | 5,162 | `sha256:3198fedd28973a73dd59e511ea190bc0e36bc4cdf014bb2f2f9f86bb12b83750` |
| fixture `handoff-manifest.json` | 13,516 | `sha256:2dfb5808c2b172f0fd17d034421aa8439f96c54f0a578b7c3f42bdcba2b8231c` |
| fixture `sources/real-gpu/workload.jsonl` | 13,900 | `sha256:22bf3389cc29ee946ae567870d7f8d7b458594224542a796e8990c15b1cfcd63` |

The checked-in fixture is a **NON-PRODUCTION TEST FIXTURE**. Its contract
bytes and linked source YAMLs exercise the parser and transport boundary only;
they are not an ExitSpec handoff, do not authorize a GPU run, and must never
be published as evidence.

## 2. Exact source commit and archive pin procedure

Run this from the Inferdrome checkout. Use a clean checkout for the exact
commit that the operator intends to transport. The base values below reproduce
the anchor table above; for a later reviewed commit, replace both the commit
and the resulting archive digest together.

```bash
set -euo pipefail
REPO_ROOT=/absolute/path/to/Inferdrome
SOURCE_COMMIT=1fac341c474df7a2ea80b91948daf9febdf2d1eb
SOURCE_ARCHIVE=/private/tmp/inferdrome-source-${SOURCE_COMMIT}.tar

test "$(git -C "$REPO_ROOT" rev-parse --verify HEAD^{commit})" = "$SOURCE_COMMIT"
test -z "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all)"
git -C "$REPO_ROOT" merge-base --is-ancestor \
  1fac341c474df7a2ea80b91948daf9febdf2d1eb "$SOURCE_COMMIT"
test ! -e "$SOURCE_ARCHIVE"

git -C "$REPO_ROOT" archive --format=tar \
  --output="$SOURCE_ARCHIVE" "$SOURCE_COMMIT"
SOURCE_ARCHIVE_SHA256="sha256:$(shasum -a 256 "$SOURCE_ARCHIVE" | awk '{print $1}')"
SOURCE_ARCHIVE_BYTES="$(wc -c < "$SOURCE_ARCHIVE" | tr -d ' ')"
printf 'source_commit=%s\nsource_archive_sha256=%s\nsource_archive_bytes=%s\n' \
  "$SOURCE_COMMIT" "$SOURCE_ARCHIVE_SHA256" "$SOURCE_ARCHIVE_BYTES"
archive_members="$(tar -tf "$SOURCE_ARCHIVE")"
rg_status=0
rg -q '(^|/)\.git(/|$)' <<<"$archive_members" || rg_status=$?
if test "$rg_status" -eq 0; then
  printf 'source archive contains a .git path\n' >&2
  exit 1
fi
if test "$rg_status" -ne 1; then
  printf 'source archive member scan failed\n' >&2
  exit 1
fi
```

The controller independently recreates and validates this exact tree archive
with `git ls-tree` and `git archive`; pass the observed digest as
`--expected-source-archive-sha256`. Do not use a working-tree tarball, include
`.git`, or let a self-authored exported-tree marker become the pin.

If the checkout is exported without Git, the existing controller requires the
retained `.inferdrome-source-archive.tar` and an independent commit/digest pin.
Do not create that marker by hand for this rehearsal.

## 3. Immutable 12-file P1 handoff intake contract

The operator receives one real directory from the external ExitSpec owner. The
directory must contain exactly these 12 regular, single-link files and no
symlinks, hard links, devices, or extra files:

| Relative path | Role |
| --- | --- |
| `.complete` | Completion marker with exact bytes `exitspec.inferdrome-prospective-handoff.complete.v1\n` |
| `handoff-manifest.json` | Canonical allowlist, case order, and cross-digest manifest |
| `contracts/native-p95-under-20ms.frozen.json` | Frozen external contract |
| `contracts/native-p95-under-10ms.frozen.json` | Frozen external contract |
| `contracts/semantic-first-nonempty-under-20ms.frozen.json` | Frozen external contract |
| `confirmations/native-p95-under-20ms.confirmation.json` | Owner confirmation artifact |
| `confirmations/native-p95-under-10ms.confirmation.json` | Owner confirmation artifact |
| `confirmations/semantic-first-nonempty-under-20ms.confirmation.json` | Owner confirmation artifact |
| `sources/native-p95-under-20ms.yaml` | Exact linked Inferdrome source YAML |
| `sources/native-p95-under-10ms.yaml` | Exact linked Inferdrome source YAML |
| `sources/semantic-first-nonempty-under-20ms.yaml` | Exact linked Inferdrome source YAML |
| `sources/real-gpu/workload.jsonl` | Exact workload bytes referenced by all three cases |

The manifest has exactly these top-level keys:

```text
acceptance_verdict
authority_boundary
canonicalization_scheme_id
cases
completion_marker
confirmation_identity_assurance
hash_algorithm_id
link_derivation_policy_id
schema_version
workload_artifact_path
workload_artifact_sha256
```

Each case entry has exactly these keys, in the canonical case order above:

```text
case_id
confirmation_artifact_path
confirmation_id
confirmation_record_sha256
contract_artifact_path
contract_artifact_sha256
contract_canonical_hash
contract_confirmation_fingerprint
contract_id
contract_version
methodology
producer_contract_link
source_yaml_artifact_path
source_yaml_artifact_sha256
```

The validator also requires canonical RFC 8785 JSON bytes, strict duplicate-
free JSON/YAML, lowercase tagged `sha256:` digests, no acceptance verdict,
the authority boundary `EXIT_SPEC_CUSTOMER_CONFIRMED_HANDOFF_ONLY`, and the
confirmation assurance
`PROCESS_LOCAL_DECLARED_IDENTITY_NOT_AUTHENTICATED`. Each contract must be
`FROZEN`, version `1.0.0`, linked to its matching `cnf_...` confirmation, and
dated before measurement. Each source must use the exact pinned Qwen2.5
0.5B/vLLM 0.26.0 methodology: loopback `127.0.0.1:18080`, the pinned model
and tokenizer revision, concurrency 4, 10 warmups, 100 measured requests,
32 requested output tokens, temperature 0, seed 42, and the shared workload
digest. Inferdrome preserves and checks these identities; it does not evaluate
the ExitSpec criterion or emit a verdict.

### Rehearsal-only fixture expansion

To exercise the exact intake path without external files, expand the checked-in
byte fixture into a new disposable directory. This is a one-time test-input
materialization, not production machinery:

```bash
set -euo pipefail
umask 077
REPO_ROOT=/absolute/path/to/Inferdrome
FIXTURE="$REPO_ROOT/tests/fixtures/prospective_p1_v1_bytes.json"
HANDOFF_ROOT=/private/tmp/inferdrome-p1-non-production
test ! -e "$HANDOFF_ROOT"

python3.12 - "$FIXTURE" "$HANDOFF_ROOT" <<'PY'
import json
import sys
from pathlib import Path

fixture = Path(sys.argv[1])
root = Path(sys.argv[2])
files = json.loads(fixture.read_text(encoding="utf-8"))["files"]
if set(files) != {
    ".complete",
    "handoff-manifest.json",
    "sources/real-gpu/workload.jsonl",
    "sources/native-p95-under-20ms.yaml",
    "sources/native-p95-under-10ms.yaml",
    "sources/semantic-first-nonempty-under-20ms.yaml",
    "contracts/native-p95-under-20ms.frozen.json",
    "contracts/native-p95-under-10ms.frozen.json",
    "contracts/semantic-first-nonempty-under-20ms.frozen.json",
    "confirmations/native-p95-under-20ms.confirmation.json",
    "confirmations/native-p95-under-10ms.confirmation.json",
    "confirmations/semantic-first-nonempty-under-20ms.confirmation.json",
}:
    raise SystemExit("fixture is not the bounded 12-file P1 set")
root.mkdir(mode=0o700, parents=True)
for relative, text in files.items():
    path = root / relative
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
PY
```

The fixture's three external contract links are, in required order:

```text
native-p95-under-20ms=sha256:c73f3fe1127575443bc30baa1cac4a610dfebfcd721ac72a2c998a6bf1c21580
native-p95-under-10ms=sha256:6a499cfc2e15245e905ecec8282910536e1a594ca3a4d9117e50394ee4f0d855
semantic-first-nonempty-under-20ms=sha256:fe776bccedbd5a935480be5808bfaf73b60e17c3f275c2c9b1ba46c2ba9eb248
```

These are fixture-only values. A real handoff must carry fresh owner-supplied
contract files and independently retained digests.

## 4. Manifest and workload pin verification

Before any transport, calculate the two operator-observed pins from the exact
directory and compare them with the handoff manifest and the received record:

```bash
HANDOFF_MANIFEST_SHA256="sha256:$(shasum -a 256 \
  "$HANDOFF_ROOT/handoff-manifest.json" | awk '{print $1}')"
WORKLOAD_SHA256="sha256:$(shasum -a 256 \
  "$HANDOFF_ROOT/sources/real-gpu/workload.jsonl" | awk '{print $1}')"
printf 'handoff_manifest_sha256=%s\nworkload_sha256=%s\n' \
  "$HANDOFF_MANIFEST_SHA256" "$WORKLOAD_SHA256"
```

For the non-production fixture, these must be:

```text
handoff_manifest_sha256=sha256:2dfb5808c2b172f0fd17d034421aa8439f96c54f0a578b7c3f42bdcba2b8231c
workload_sha256=sha256:22bf3389cc29ee946ae567870d7f8d7b458594224542a796e8990c15b1cfcd63
```

The controller's dry run then performs the one-read inventory, exact 12-file
allowlist check, canonical JSON check, contract/confirmation/source
cross-digest checks, and deterministic handoff archive construction. A pin
mismatch is a no-go before source archive creation, SSH, Lambda, or GPU work.

## 5. SSH identity and host-key prerequisites, without connecting

For a later live run, the operator must have both of these independent local
inputs before starting the controller:

- a capture-only private-key file that is a regular, non-symlink file; and
- an operator-pinned `known_hosts` file whose raw bytes have an independently
  retained digest.

The host-key digest here is a digest of the complete `known_hosts` bytes. It is
not an SSH public-key fingerprint and is not obtained by this rehearsal. Check
the files locally and compute the raw-byte pin without invoking `ssh-keyscan`:

```bash
IDENTITY_FILE=/absolute/path/to/capture-only-id_ed25519
HOST_KEY_FILE=/absolute/path/to/pinned-known_hosts
test -f "$IDENTITY_FILE" && test ! -L "$IDENTITY_FILE"
test -f "$HOST_KEY_FILE" && test ! -L "$HOST_KEY_FILE"
HOST_KEY_SHA256="$(shasum -a 256 "$HOST_KEY_FILE" | awk '{print $1}')"
printf 'host_key_sha256=%s\n' "$HOST_KEY_SHA256"
```

The live controller copies those bytes into its private staging directory and
uses `StrictHostKeyChecking=yes`, `UserKnownHostsFile=<staged-pin>`,
`IdentitiesOnly=yes`, `ForwardAgent=no`, `ProxyCommand=none`, `ProxyJump=none`,
and `-F /dev/null`. Supplying `--host-key-file` avoids the controller's
`ssh-keyscan` branch. Do not substitute the rehearsal fixture's fake key line
for a live host key.

## 6. Guarded and non-guarded dry runs

Both commands below are inert previews. They require a clean checkout and an
existing regular identity file because the CLI validates operator inputs, but
they do not open SSH, run a GPU command, create a provider instance, call a
provider API, or create the output root. The dry run builds the source archive
and handoff archive only in disposable temporary storage.

### Non-guarded operator-provided host preview

This path intentionally makes no cost-termination claim. Replace the variables
with the values calculated above; do not copy fixture contract values to a real
handoff.

```bash
PYTHONPATH="$REPO_ROOT/src" "$REPO_ROOT/.venv/bin/python" \
  "$REPO_ROOT/scripts/capture_real_gpu_over_ssh.py" \
  operator@prepared.example.test \
  --prospective \
  --dry-run \
  --handoff-root "$HANDOFF_ROOT" \
  --expected-handoff-manifest-sha256 "$HANDOFF_MANIFEST_SHA256" \
  --expected-workload-sha256 "$WORKLOAD_SHA256" \
  --expected-source-archive-sha256 "$SOURCE_ARCHIVE_SHA256" \
  --expected-commit "$SOURCE_COMMIT" \
  --identity-file "$IDENTITY_FILE" \
  --host-key-file "$HOST_KEY_FILE" \
  --host-key-sha256 "$HOST_KEY_SHA256" \
  --remote-state-root /tmp/inferdrome-p1-rehearsal-state \
  --output-root /private/tmp/inferdrome-p1-rehearsal-output \
  --startup-timeout-seconds 900 \
  --remote-timeout-seconds 300
```

### Guarded Lambda preview

This preview validates the complete guard shape but still does not instantiate
the Lambda client. The identity, rate, cap, billing timestamp, instance ID,
and instance type below are **NON-PRODUCTION PLACEHOLDERS** for CLI rehearsal.
No `LAMBDA_CLOUD_API_KEY` is needed or wanted for this dry run.

```bash
PYTHONPATH="$REPO_ROOT/src" "$REPO_ROOT/.venv/bin/python" \
  "$REPO_ROOT/scripts/capture_real_gpu_over_ssh.py" \
  operator@prepared.example.test \
  --prospective \
  --dry-run \
  --handoff-root "$HANDOFF_ROOT" \
  --expected-handoff-manifest-sha256 "$HANDOFF_MANIFEST_SHA256" \
  --expected-workload-sha256 "$WORKLOAD_SHA256" \
  --expected-source-archive-sha256 "$SOURCE_ARCHIVE_SHA256" \
  --expected-commit "$SOURCE_COMMIT" \
  --identity-file "$IDENTITY_FILE" \
  --host-key-file "$HOST_KEY_FILE" \
  --host-key-sha256 "$HOST_KEY_SHA256" \
  --remote-state-root /tmp/inferdrome-p1-rehearsal-state \
  --output-root /private/tmp/inferdrome-p1-rehearsal-output-guarded \
  --startup-timeout-seconds 900 \
  --remote-timeout-seconds 300 \
  --lambda-instance-id 0123456789abcdef0123456789abcdef \
  --lambda-instance-type-name gpu_1x_a10 \
  --lambda-hourly-rate-usd 1.00 \
  --max-cost-usd 1.00 \
  --lambda-billing-started-at 2026-08-29T00:00:00Z \
  --lambda-guard-state-root /private/tmp/inferdrome-p1-rehearsal-guards
```

Expected dry-run JSON includes the exact `repository_commit`,
`source_archive_sha256`, source byte count, handoff archive digest and byte
count, handoff manifest digest, workload digest, and these boundaries:

```text
non-guarded: billing_boundary=OPERATOR_PROVIDED_HOST; NO COST-TERMINATION CLAIM
guarded:     billing_boundary=LAMBDA API TERMINATION WATCHDOG PLUS CONTROLLER FINALLY
```

The `remote_root` contains `<random>` in a dry-run plan. No path under either
`--output-root` is created by the dry run.

## 7. Lambda cost-cap and termination checklist

For a real guarded run, the operator must complete every item before removing
`--dry-run`:

- [ ] The exact active Lambda instance ID resolves to the exact SSH endpoint.
- [ ] The API-reported `instance_type_name` is copied exactly; it is not guessed
      from a marketing name.
- [ ] The displayed hourly rate is recorded and matches
      `--lambda-hourly-rate-usd` exactly.
- [ ] The actual provider billing start timestamp is recorded and passed as
      `--lambda-billing-started-at`; never substitute launch-request time.
- [ ] `max_cost_usd` is a positive operator budget and is passed exactly.
- [ ] The budget is understood as a client-side stop boundary, not a provider
      billing guarantee.
- [ ] The API key exists only in `LAMBDA_CLOUD_API_KEY`, loaded without shell
      history, and is absent from SSH arguments and the remote environment.
- [ ] The guard state root is local, private, and on durable storage for the run.
- [ ] The watchdog reports readiness before SSH/provider work continues.
- [ ] Exactly one non-terminal target instance exists at guard readiness.
- [ ] The live termination deadline leaves at least the controller's 300-second
      post-remote budget for prospective retrieval/finalization.
- [ ] After capture or any failure, provider termination is confirmed before
      local extraction, semantic verification, or publication review.
- [ ] The final guard receipt says the target is `terminated` or `preempted` and
      the local guard state is retained.

The guard computes the cost window as
`floor(max_cost_usd / hourly_rate_usd * 3600)` seconds from the billing start,
then subtracts the prospective safety margin. Elapsed billing time is already
consumed before controller startup. If the window cannot leave the required
remote and post-remote budgets, stop; do not extend the cap informally.

Without the guard, the controller reports
`OPERATOR_PROVIDED_HOST_NO_COST_TERMINATION_CLAIM`. The operator owns provider
cleanup and must not describe a successful non-guarded run as cost-bounded.

## 8. Failure and recovery matrix

| Condition | Expected boundary | Recovery |
| --- | --- | --- |
| Dirty checkout, wrong `HEAD`, or missing required merge ancestry | Stop before source archive/SSH | Return to the exact clean reviewed commit and recompute the source archive pin |
| Missing, extra, linked, or changing P1 file | Stop before source archive/provider work | Obtain a new owner handoff; never delete or edit the received bytes to make the count fit |
| Manifest/workload digest mismatch | Stop before source archive/provider work | Recalculate from the retained directory and obtain corrected independent pins |
| Contract, confirmation, source link, or methodology drift | Stop before host/provider work | Ask the external owner for a newly frozen matching handoff; Inferdrome does not repair it |
| Identity or pinned host-key input unavailable | No SSH attempted | Supply regular local files and independently record the raw-byte host-key digest |
| Dry run rejects the source archive pin | No SSH/provider work | Recreate the archive from the exact clean commit; do not weaken the pin |
| Lambda guard cannot arm, rate/endpoint/type differs, or multiple active targets exist | Fail closed; guarded path attempts termination when its safety logic is engaged | Keep the watchdog/guard receipt, confirm provider state independently, and do not continue |
| Remote host preflight or prospective wrapper fails | No eligible capture claim | Retain diagnostics; guarded path must confirm termination; non-guarded operator terminates the host |
| Retrieved archive size or digest differs | No extraction or semantic verification | Retain the bounded diagnostics and restart from a fresh clean transport |
| Provider termination is not confirmed | Do not extract, verify, or publish | Leave the watchdog armed, escalate through the provider's normal control plane, and wait for confirmation |
| Offline verification rejects a retrieved session | No `EXTERNAL_ONLY` promotion | Preserve the archive and receipts as failed diagnostics; do not hand-edit metadata |
| Controller process dies after retrieval | Evidence state is unknown | Preserve the complete local staging directory; do not infer completion or invent a prospective resume path; after termination is independently confirmed, rerun from a fresh reviewed input if needed |

No recovery row authorizes editing a frozen contract, replacing a source YAML,
reusing a different workload, bypassing the guard, or claiming an ExitSpec
outcome.

## 9. Expected outputs and claim boundaries

The repository engineering gate's inert wrapper check must emit JSON with:

```json
{
  "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
  "provider_or_gpu_mutation": "NONE",
  "status": "INERT_NO_CONTRACTS",
  "valid": true
}
```

With the non-production fixture and all three fixture contract pins, the
wrapper check changes only `status` to `PROSPECTIVE_INPUTS_VALID` and lists
the three case IDs plus each source-spec digest. It still reports
`provider_or_gpu_mutation: NONE`.

A completed live prospective session is expected to retain, per case, the
source-spec digest, execution fingerprint, request-plan digest, bundle digest,
run ID, original-spec digest, and external contract digest. Its metadata must
remain `PENDING_EXTERNAL_EXITSPEC` and `EXTERNAL_ONLY`; Inferdrome acceptance
verdict fields remain null.

| Rehearsal or capture fact | It supports | It does not support |
| --- | --- | --- |
| Exact 12-file snapshot and cross-digests | Intake identity and bounded transport preparation | Contract authorship, authenticated confirmer identity, or ExitSpec acceptance |
| Exact source archive pin | Transported source-tree identity | GitHub authorship, provider state, GPU identity, or runtime success |
| Dry-run JSON | Local command/configuration conformance and no mutation on that path | SSH reachability, host readiness, Lambda capacity, price, billing, or model execution |
| Local fixture tests | Parser, archive, sequencing, and rejection behavior | Production contract validity or genuine GPU performance |
| Guard receipt after a real run | Provider termination boundary for that target and time | A final invoice guarantee or performance acceptance |
| A verified live bundle | Inferdrome producer-side evidence identities | ExitSpec `PASS`, `FAIL`, or `NOT_PROVEN` without external evaluation |

## 10. Final go/no-go checklist

Go only when every applicable box is checked:

- [ ] Work is in Inferdrome only; no ExitSpec, Cascade Router, or other
      repository was inspected or modified.
- [ ] The intended clean source commit is recorded and contains merge
      `1fac341c474df7a2ea80b91948daf9febdf2d1eb` in its ancestry.
- [ ] `git status --porcelain --untracked-files=all` is empty at source-archive
      creation time.
- [ ] The exact Git tree archive was created from that commit and its independent
      digest/byte count were retained.
- [ ] The external handoff has exactly the 12 files above, with no links or
      extra paths.
- [ ] The handoff manifest and workload bytes match their independent pins.
- [ ] All three case IDs, contract links, confirmation IDs, source digests, and
      methodology fields cross-check successfully.
- [ ] The SSH identity and raw-byte host-key pin are present locally; live mode
      will pass `--host-key-file` and will not use `ssh-keyscan`.
- [ ] Both non-guarded and guarded dry runs pass locally, with no output root,
      SSH process, GPU process, provider API call, or provider/GPU mutation.
- [ ] For live guarded mode, rate, cap, billing origin, endpoint, instance ID,
      exact type, API key placement, watchdog readiness, and termination plan
      were freshly reviewed.
- [ ] For live non-guarded mode, the operator explicitly accepts the absence of
      a cost-termination claim and owns cleanup.
- [ ] Any failed or timed-out run is retained only as diagnostics until
      termination and offline verification requirements are satisfied.
- [ ] No acceptance verdict, public publication, license choice, version bump,
      tag, push, PR, or merge is performed by this packet.

## 11. Rehearsal evidence record

Fill this locally for review; do not publish it as evidence:

```text
rehearsal_status: GPU_FREE_LOCAL_ONLY
source_commit:
source_archive_sha256:
source_archive_bytes:
handoff_manifest_sha256:
workload_sha256:
handoff_archive_sha256:
handoff_archive_bytes:
fixture_status: NON_PRODUCTION_ONLY
provider_api_called: false
ssh_connection_attempted: false
ssh_keyscan_attempted: false
gpu_command_attempted: false
provider_or_gpu_mutation: NONE
acceptance_verdict: null
publication_status: EXTERNAL_ONLY
residual_external_inputs: frozen owner handoff, exact host key, SSH identity,
  provider billing/rate/instance facts, and separately authorized live execution
```

The focused existing tests for this packet are:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_prospective_handoff.py \
  tests/unit/test_prospective_controller.py \
  tests/unit/test_prospective_real_gpu_capture.py -q
```

They include mutation/replacement rejection, exact 12-file validation, pin
ordering, dry-run inertness, SSH option hardening, and provider-boundary
sequencing. If repository code changes, run the complete existing
`scripts/engineering_gate.sh` in a provisioned Python 3.12 environment and
retain its result. This documentation packet itself does not authorize any
external action.
