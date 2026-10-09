# Qwen3-8B two-replica vLLM results

Two separately completed, descriptive experiments ran on one manually rented
host with two NVIDIA A100-SXM4 40 GB GPUs. Each GPU served an independent
Qwen3-8B BF16 vLLM 0.26.0+cu129 replica (tensor parallelism 1); a loopback
router assigned requests to the two replicas. Both used the pinned model
revision `b968826d9c46dd6066d109eabc6255188de91218` and prefix caching.
These are later research results, outside the frozen v0.2/v0.3 release claims.
They are not evidence that the separate GCP guarded campaign or a production
deployment ran.

## Routing baseline — 28 September 2026

The [frozen baseline method](VLLM_ROUTER_GPU_PR3.md) used a synthetic changing
hotspot over two short documents, 286 input and 128 output tokens per request.
Three five-minute **calibration** windows offered 300, 600, and 1,200 requests
(1, 2, and 4 req/s), totaling **2,100**. All qualified; the highest tested
rate, 4 req/s, was selected without finding a failure boundary. The separate
**evaluation** used four balanced policy-order blocks. Each policy saw the same
1,200 scheduled offers per block, for **19,200** evaluation requests. The
combined total was **21,300/21,300 completed**. Both engines stayed loaded;
each condition had a fresh router and verified prefix-cache reset on each
replica.

| Evaluation policy | Completed/offered | SLO-goodput at 4 req/s |
| --- | ---: | ---: |
| `round_robin` | 4,800/4,800 | 4.000 req/s |
| `least_busy` | 4,800/4,800 | 4.000 req/s |
| `cache_only` | 4,800/4,800 | 4.000 req/s |
| `cache_plus_load` | 4,800/4,800 | 4.000 req/s |

SLO-goodput divides requests meeting both first-content ≤500 ms and completion
≤5 s by the fixed scheduled window, counting **all offered requests** in the
denominator. The baseline policies tied at the tested rate; this study did not
show a cache-aware goodput advantage or establish maximum capacity. The raw
session reports 19 valid conditions and a valid saved evaluation schedule.
The effective measured source was
[`7f3d7ee`](https://github.com/jayeshsuyal/inferdrome/commit/7f3d7ee207b1eb9754c096ef2bdd9b699c95fc58),
the one-line vLLM 0.26 CLI correction to the prepared
[`e32ded7`](https://github.com/jayeshsuyal/inferdrome/commit/e32ded7c598cae018f0f6389df8eb86e6918ee6a)
runner ([PR #110](https://github.com/jayeshsuyal/inferdrome/pull/110)).
Locally verified aggregate `report.json` SHA-256:
`22668f38d0a73470303045c7559961c0a6e4b67d9c02151ca94c6e1ef5204d77`.

## Separate capacity sweep

The [capacity method](VLLM_ROUTER_CAPACITY_V2.md) changed the workload to 48
distinct approximately 4,096-token document prefixes and 128 output tokens,
with a shifting hot group. It kept the same two-replica model topology but used
8,192-token context. A `least_busy` pilot offered 2, 4, then 6 req/s in three
120-second windows (1,440 requests); 2 and 4 passed the frozen 95% SLO
attainment rule, while 6 failed. The saved schedule then evaluated all four
policies at all three rates in four balanced blocks: **48 evaluation windows**
and **23,040 offers**, distinct from the baseline's 21,300. Each policy received
matched traces within a rate and block. All 24,480 pilot and evaluation offers
completed, although completing did not imply meeting the latency SLO.

| Policy at 6 offered req/s | Mean SLO-goodput across four blocks | SLO attainment |
| --- | ---: | ---: |
| `round_robin` | 5.4729 req/s | 91.2% |
| `least_busy` | 5.2542 req/s | 87.6% |
| `cache_only` | 5.6333 req/s | 93.9% |
| `cache_plus_load` | 5.5104 req/s | 91.8% |

At 6 req/s, `cache_only` exceeded `least_busy` by **7.2% relative SLO-goodput**:
`(5.6333333333 / 5.2541666667 - 1) × 100 = 7.2165%`. It was higher in each
of the four matched blocks. All four policies fell short of the 95% pass rule
at 6 req/s. This comparison is from the **capacity sweep**, not the routing
baseline. The measured source was
[`aa25f31`](https://github.com/jayeshsuyal/inferdrome/commit/aa25f31e2f8027315803d36f0f1dc453d4866803).
Locally verified aggregate `report.json` SHA-256:
`e47a6d46f9ed016bf0b97ccb8b50309dc3c8aa88f10db0e95ba7065c76ff27ca`.

## Evidence boundary

The tables above expose the evaluated populations, denominator, comparison,
and exact source pins. Raw request traces, prompts, logs, session records, and
the full reports are retained in private local archives; their hashes identify
the reports used here but the archives are not publicly downloadable. These
single-host synthetic studies support descriptive comparisons only. Four
matched blocks are the repetitions; individual request rows are correlated,
not independent replications. The 7.2% difference is not a significance or
production-wide gain claim. Routing affinity and cache counters do not prove
KV-cache residency or isolate a causal cache benefit. The passing 4 req/s and
failing 6 req/s capacity pilot bracket the tested SLO boundary; they do not
measure the exact maximum rate between them.
