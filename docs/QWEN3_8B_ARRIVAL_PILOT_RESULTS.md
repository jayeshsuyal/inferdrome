# Qwen3-8B arrival-pattern pilot — 2026-10-12

**Completed and independently replayed; reversal screen: INCONCLUSIVE.**

The run completed **23,040/23,040 inference requests** across 32 windows on two independent TP1 vLLM 0.26.0+cu129 replicas serving Qwen3-8B BF16, one per A100 PCIe 40 GB GPU. The offline audit verified all request accounting, 32 resets, 32 transport sidecars/statuses/progress records and 97 safety receipts. No transport faults or diagnostic sink failures were recorded.

## Method

Eight matched blocks each contain two policies (`cache_only`, `least_busy`) × two arrival schedules (baseline, candidate), in a frozen balanced order. Each window offers 720 requests over 120 seconds; paired policies retain the same request population and token budget. The `control.v1` workload uses 48 document prefixes near 4,096 tokens, 128 output tokens, and an 8,192-token context. Traffic is offered at 6 requests/s. Candidate timing groups eight offers, retains at least 25% of their spacing, and advances an offer by at most one second within its original workload epoch. No search, new calibration or adaptive recipe changes occurred.

**64 separate eight-token warmups (512 output tokens)** precede the measurement windows. They are reset traffic and are excluded from inference counts and goodput. SLO attainment uses all offered requests as its denominator, including requests that miss an SLO. Native SLO goodput divides SLO-good requests by the fixed 120-second traffic window; drain time is not substituted. SLO-good requires first content within 500 ms and completion within 5 s of scheduled offer time. Client scheduling lag and queue delay each have a native p95 limit of 10 ms.

## Observed SLO goodput

Each cell contains eight windows and 5,760 offers. Aggregate goodput below is SLO-good count / `(8 × 120 seconds)`, equivalent to the mean of the eight fixed-window rates.

| Arrival schedule | Routing policy | SLO-good / offered | SLO-good requests/s |
| --- | --- | ---: | ---: |
| Baseline | Cache only | 5,179 / 5,760 | 5.39479 |
| Baseline | Least busy | 3,008 / 5,760 | 3.13333 |
| Candidate | Cache only | 5,139 / 5,760 | 5.35313 |
| Candidate | Least busy | 2,545 / 5,760 | 2.65104 |

Cache-only had higher SLO goodput under both schedules in every matched block. The declared screen looked for cache-only's baseline advantage to reverse toward least-busy under candidate timing; it did **not** flag that reversal. `INCONCLUSIVE` describes this reversal search, not an unfinished run. It does not establish equivalence or prove that a reversal is impossible.

Inspect the [aggregate counts, all eight block contrasts, exact screening statistics and fingerprints](results/qwen3-arrival-pilot-2026-10-12.json).

## Revisions and artifact identities

- Executed source: [`c4a2ad0725a2a971e59006fb3354cefda2217ead`](https://github.com/jayeshsuyal/inferdrome/commit/c4a2ad0725a2a971e59006fb3354cefda2217ead); public merge: [`ea0d4d15b941c8caa40d0c1913d2b51318c26000`](https://github.com/jayeshsuyal/inferdrome/commit/ea0d4d15b941c8caa40d0c1913d2b51318c26000).
- Native [trial/reset implementation](https://github.com/jayeshsuyal/inferdrome/blob/c4a2ad0725a2a971e59006fb3354cefda2217ead/src/inferdrome/breakpoint_pilot_run.py), [paired protocol](https://github.com/jayeshsuyal/inferdrome/blob/c4a2ad0725a2a971e59006fb3354cefda2217ead/src/inferdrome/vllm_paired_protocol.py), and [offline comparison](https://github.com/jayeshsuyal/inferdrome/blob/c4a2ad0725a2a971e59006fb3354cefda2217ead/src/inferdrome/vllm_paired_comparison.py) are public. A private operator wrapper added owned-process/guardian boundaries and environment checks around these native APIs; its hash is retained below.
- Full plan SHA256: `83c5acaf74bbc5bd8224344df2961a3cf0c3a6ee13c813fd3de32d232442976c`.
- Native semantic protocol SHA256: `0e7540ec4b85c0975763d07b0ee888fa2e7030d48765920fa185535c1fee9a5f`.
- Private runner SHA256: `63fb98317c550f8cf267cfb7c97daad07fcb31809584e0df668071d46621d510`.
- Private archive SHA256: `589e00ba212c621e7c3ccf8450504573bd5598e8f2a67f51c3c88bb5615209fa`.
- Independent audit SHA256: `332b5f8535759117a12216d7a93d8496d56f8a910700e018a4851f2e733758dc`; replay file SHA256: `d103836305c2864e36b21ba041f8e57940807e1db4acf3eb6cfa69c3a594667d`.

A local reader correction accepted canonical JSON `1` and `1.0` as the same one-second client setting, excluding booleans. Original readers were preserved; inference data, sealed inputs and operator code were unchanged, and retrieval was finalized offline without another transfer.

## Limits

This is a local exploratory result: `evidence_eligible=false`, held-out confirmation not implemented. It is a later research execution outside the frozen v0.2/v0.3 release claims and is not an executed GCP campaign or production deployment. Blocks are the replication unit; their independence is unverified, adaptive-search multiplicity is uncontrolled, and no confidence interval or production winner is established. Estimated cache affinity is routing history, not verified per-request KV residency.

Client reuse/one-second idle retention and the server's 30-second setting were configured. Two owned API-process environment observations selected `VLLM_HTTP_TIMEOUT_KEEP_ALIVE=30`; that does not attest the effective server timeout or establish the cause of earlier disconnects. One completed fault-free run does not prove a universal transport fix.

This PCIe 40 GB profile differs from the older A100 SXM4 40 GB studies. No hardware comparison or pooling of earlier failed/diagnostic windows is made. The earlier 21,300-request routing baseline and separate **7.2% capacity-study result** remain separate; this pilot supplies no replacement percentage headline.

Raw prompts, archives, ledgers, logs and operator records remain private. Public counts support the table arithmetic; authorized raw-data inspection is required to replay the underlying receipts. Customer acceptance remains outside Inferdrome.
