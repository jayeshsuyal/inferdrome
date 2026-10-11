# Pilot cleanup guardian

This is the external cleanup backstop for a separately approved Breakpoint
pilot. **Preparing the pilot does not authorize this command, a rental or any
spending.** The checked-in preparation remains undecided.

The guardian runs on an operator-controlled machine outside the rented GPU
host. It owns only one explicit Vast instance ID. It cannot rent, create,
start or select an instance, and it does not delete volumes or other resources.
Use an allocation without separately rented persistent volumes; those need
their own cleanup procedure and are outside this pilot.

## Approval and budget

`inferdrome.breakpoint-pilot-approval.v1` accepts exactly these fields:

| Field | Required value |
| --- | --- |
| `schema` | `inferdrome.breakpoint-pilot-approval.v1` |
| `execution_authorized` | Literal `true`, after separate human approval. |
| `provider`, `instance_id` | `vast`, and the exact observed positive numeric ID as a string. |
| `billing_start_utc` | Actual observed billing origin, UTC with whole seconds and `Z`. |
| `max_session_s` | At most 12,600 seconds, measured from billing origin. |
| `cleanup_reserve_s` | 600–1,800 seconds, smaller than the whole session. |
| `hourly_rate_usd` | Current quoted **total pair** compute rate, decimal string. |
| `cap_usd` | Separately approved total USD ceiling, decimal string. |
| `storage_allowance_usd`, `transfer_allowance_usd` | Bounded allowances from the actual offer and planned transfers, decimal strings. |
| `reserve_usd` | Additional USD reserve for uncertainty, decimal string. |
| `plan_sha256` | Exact prepared pilot plan digest, with `sha256:` prefix. |
| `source_revision` | Reviewed pilot controller revision, 40 hexadecimal characters. |
| `image_reference` | The exact pinned stock Vast vLLM image used by the pilot. |
| `guardian_host_kind` | Operator declaration `EXTERNAL_OPERATOR_HOST`. |

The cap must cover `hourly_rate_usd × max_session_s / 3600`, plus storage,
transfer and the additional USD reserve. Cleanup compute time is already inside
the session duration. Unknown fields, null spending, floats, nonfinite values,
ambiguous JSON, and an undecided planning packet are rejected.

These are quoted bounds, not a provider billing guarantee. A change in rates,
unbounded transfer or storage, or provider termination failure can defeat the
estimate. No successful guardian result verifies an invoice.
Retain a separate private quote worksheet with allocated disk, storage rate and
duration, transfer rates and maximum planned bytes, quote time, account credit,
and allowance arithmetic. Decimal dollar allowances alone do not establish
those underlying terms or enforce transfer usage.

## Future authorized operation

Use the external machine's already installed, authenticated Vast CLI. Do not
put credentials in the approval, command arguments or study artifacts. Check
the installed CLI's supported grammar before the rental. The current official
implementation supports exact-ID `destroy instance ID -y --raw` and an unfiltered
`show instances --raw` returning the complete list across pages.
[Official Vast CLI source](https://github.com/vast-ai/vast-cli/blob/master/vastai/cli/commands/instances.py)

After the future rental has an observed identity and billing origin, start the
guardian under a supervisor on an awake external machine, before workload
startup. Its parent directory must exist and its output directory must be new.
Do not tie this process to the GPU host or its SSH connection.

```bash
python -m inferdrome.breakpoint_pilot_guard \
  --approval /private/pilot/approved-instance.json \
  --vast-executable /absolute/path/to/vastai \
  --output-dir /private/pilot/guardian \
  --confirm DESTROY_EXACT_INSTANCE_AT_DEADLINE
```

This command stays running. It first reads the complete provider inventory and
requires the exact target to be present before writing `ready.json`. That
heartbeat is refreshed every 15 seconds. Copy a fresh receipt to the collector
immediately before its required launch checks; receipts older than 60 seconds
or bound to another approval are rejected. A copied heartbeat proves only that
the guardian wrote that observation. It does not attest future uptime or prove
that the declared external machine is independent.

The guardian starts destruction at
`billing_start_utc + max_session_s - cleanup_reserve_s`. The collector reserves
another fixed 600 seconds before that point for local cleanup and export. At
the maximum session and a 600-second guardian reserve, new collection stops
by 3h10 and provider cleanup starts by 3h20, both measured from billing origin.
Finish retrieval before provider cleanup; cleanup does not wait for export.
The guardian also requests immediate destruction on SIGINT, SIGTERM or SIGHUP;
use that path after verified early retrieval to stop paying before the ceiling.
Do not kill it with SIGKILL or power down its machine. Loss of that machine,
network, credentials or provider availability still requires the operator's
manual exact-ID cleanup fallback.

## Receipts and failure handling

Every provider call has a timeout and output-size bound. The implementation
passes argument arrays directly, uses only exact-ID destruction, and separately
reads the unfiltered inventory. A destroy response or zero exit status alone
never establishes absence. Ambiguous, partial, duplicate or malformed inventory
is not accepted as an empty inventory.

Private command receipts retain timestamps, status, output hashes and whether
the target is present. Raw provider payloads and stderr are not retained.
The result records a live CLI observation with hashes; the retained projection
cannot independently replay the raw provider inventory or authenticate the
provider's response. A recorded absence is an operator-observed readback,
not a cryptographically attested provider receipt.
The final `result.json` distinguishes `ABSENCE_CONFIRMED` from
`CLEANUP_UNCONFIRMED`, and reports whether confirmation arrived before the
deadline. It explicitly leaves invoice verification and other resources false.
Failure to write receipts cannot justify continuing paid collection.

An expired approval still permits an immediate bounded emergency cleanup
attempt, recorded as late. An unavailable provider or exhausted cleanup window
leaves `CLEANUP_UNCONFIRMED`; inspect the provider console and perform the
approved exact-ID fallback immediately. Do not report the rental as closed
without independent absence readback.

Before renting, fake-provider tests exercise the command sequence, conservative
clock handling, signals, failure/timeout, unrelated IDs, malformed inventory,
approval rejection and cleanup uncertainty. They establish local behavior;
actual provider authentication, deletion and readback remain host-time checks.
