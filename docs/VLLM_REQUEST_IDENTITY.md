# Request identity for live routing studies

**Breakpoint, step 1:** connect each scheduled client offer to its router ledger
row. This extends Inferdrome's existing scheduled-arrival client and two-replica
router. It is the foundation for controlled workload comparisons; search,
counterexample reduction, and confirmation experiments are future work.

## What is recorded

With correlation enabled, the client allocates a random, opaque request ID for
every offer before scheduling tasks. Even an offer cancelled before dispatch
keeps its ID. A dispatched offer sends `X-Inferdrome-Request-ID` to the router.
The router uses that ID in its existing ledger and echoes it in the response.
The header is not forwarded to the serving engine and does not influence policy
selection. Only one lowercase 32-hex value is accepted. Invalid or duplicate
headers produce HTTP 400 and a generated safe ID; raw invalid values are not
recorded. Requests without a header retain the router's generated-ID behavior.

The client writes `inferdrome.vllm-router-correlated-result.v1`:

- `measurement`: the unchanged, hashed study-result v1 or capacity-result v2.
- `request_links`: one indexed client ID and observed response ID per offer.
- `result_sha256`: the canonical JSON digest of the wrapper without this field.

A missing, malformed, duplicated, or mismatching response ID cannot pass
verification when the client observed response headers. If no headers arrived,
the response ID remains null. The client never saves malformed header text.

## Use

For a future, separately authorized study, add `--correlate-requests` to the
existing `python -m inferdrome.vllm_router_study run` command. All existing plan,
token certificate, router, model, policy, and output arguments still apply.
Use a fresh router and ledger for each trial. This flag is opt-in; the existing
GPU campaign orchestration continues to emit its historical result formats.
The Python `run_trial(..., correlate_requests=True)` API also accepts capacity
plans with their existing token certificate checks.

After the trial settles, verify the saved artifacts offline:

```bash
PYTHONPATH=src python -m inferdrome.vllm_request_identity \
  --result /private/study/correlated-result.json \
  --ledger /private/study/router.jsonl \
  --output /private/study/request-links.json
```

The verifier joins by ID, independently of ledger order or timestamps. It checks
both result digests, complete indexed link coverage, unique IDs, matching policy,
response echoes, and receipt outcome/replica fields. A completed router request
must name replica 0 or 1. Other outcomes may have an explicit null replica if no
endpoint was selected. Duplicate IDs, orphan receipts, and a receipt for a client
offer marked undispatched are rejected.

| Per-offer state | Meaning |
| --- | --- |
| `MATCHED` | Client dispatch and a router ledger row share the ID. |
| `NOT_DISPATCHED` | The client recorded no dispatch; no router row exists. |
| `DISPATCHED_WITHOUT_ROUTER_RECORD` | The client attempted dispatch, but the supplied ledger contains no matching row. Delivery and router disposition remain unresolved. |

The report preserves client and router outcomes separately: a disconnect or
cancellation race can give them different terminal observations. It binds the
measurement digest, wrapper digest, serialized ledger rows, and exact ledger
file bytes. The row digest uses Python JSON with sorted keys, compact separators,
UTF-8 (`ensure_ascii=False`), and `allow_nan=False`, identified by
`PYTHON_JSON_SORTED_COMPACT_UTF8_V1`. This preserves the ledger's absolute
nanosecond integers even on long-running hosts. Other canonical digests use
RFC 8785. Output files are created exclusively and never overwritten.
Incomplete dispatch coverage writes an `INCOMPLETE` report and exits 2;
malformed evidence fails without a report. The study CLI also retains its result
and exits 2 when identity observations fail validation or the measurement is
ineligible for comparison. A successful study command still requires the
separate ledger verification above.

## Boundaries

- `VERIFIED` and `correlation_valid` apply only to the request join. The original
  `comparison_valid` value is carried separately as
  `measurement_comparison_valid`; identity verification does not recalculate
  scientific metrics or establish a valid performance comparison.
- IDs and hashes provide correlation and consistency checks, not authentication,
  execution attestation, customer acceptance, or a causal explanation. The router
  does not maintain a global registry of IDs; duplicate IDs in a supplied trial
  ledger are rejected offline.
- This change is tested with local fake streaming replicas. It adds no GPU
  result, routing-performance claim, or confirmed Breakpoint counterexample.
- Archived runs lack shared client/router IDs. They remain readable by their
  original verifiers and cannot be retroactively joined by this verifier. The
  [published baseline and capacity results](VLLM_ROUTER_RESULTS.md), including
  the separate 7.2% capacity observation, retain their original scope.
- Keep result wrappers, ledgers, and reports private until reviewed for
  publication. The embedded legacy measurement includes its router origin;
  hashes and opaque IDs do not make an entire artifact safe to publish.

Implementation: [`vllm_request_identity.py`](../src/inferdrome/vllm_request_identity.py).
End-to-end local checks: [`test_vllm_correlated_study.py`](../tests/integration/test_vllm_correlated_study.py).
