# LLM restart recovery: 100 ms of admission headroom

## Question

When one serving worker restarts under load, does stopping request forwarding
at 400 ms improve a 500 ms first-token SLO compared with forwarding until 500 ms?

The treatment changes the forwarding cutoff. Both arms retain the same
first-token deadline and immediate return on first successful health check.

## Result

| Primary recovery outcome | 500 ms baseline | 400 ms guard |
| --- | ---: | ---: |
| Offered requests | 8,640 | 8,640 |
| SLO-good | 5,220 | 5,642 |
| Misses | 3,420 | 2,998 |
| SLO-good fraction | 60.4167% | 65.3009% |

The guard produced **422 fewer misses: 12.3392% relative miss reduction**,
or **4.8843 percentage points** more SLO-good requests. Every primary pair
favored 400 ms. This is an improvement in SLO attainment, not a claim of 12.34%
lower latency or higher production throughput.

The mean reduction was 35.1667 misses per 720-offer arm. The prespecified paired
Student t calculation gives a descriptive 95% interval of 27.7480–42.5854
misses per pair. The unit is the pair, not each request; independence of repeated
pairs on one host has not been established.

## Method and controls

- **Runtime:** vLLM 0.26.0+cu129; Qwen/Qwen3-8B, revision
  `b968826d9c46dd6066d109eabc6255188de91218`; one independent replica per
  A100 PCIe 40 GB GPU on one host.
- **Traffic:** the same frozen workload in both arms, 6 offered requests/s,
  4,096-token document prefixes and 128 output tokens. Each trial contains
  60 s of history and 120 s of recovery.
- **Failure:** one worker is killed and restarted; its peer continues serving.
  The replacement returns on first health, with no priming or gradual ramp.
- **Admission:** both arms use the same per-replica cap 16 after trusted worker
  loss, retained across return; the cap is disabled before the loss. Only the
  scheduled forwarding budget differs: 500 ms versus 400 ms.
- **Scoring:** first content within 500 ms and completion within 5 s, both from
  scheduled arrival. Every offer stays in the denominator, including rejections,
  timeouts, missing responses, and other errors.
- **Schedule:** 12 fixed pairs, six per execution order, frozen with seed 1729.
  No pair was replaced, retried, or discarded for its outcome.
- **Population:** 25,920 total offers = 8,640 history + 17,280 primary recovery.
  Each arm has 8,640 primary offers. Healthy and first 30-second windows are
  subsets, not additional requests.
- **Readiness:** GPU/native-runtime and bound collector API readiness passed
  before model download. The model snapshot passed its pinned checksums before traffic.

The actual p95 scheduling-lag and client-queue gates were 10 ms per trial.
The worst observed per-trial p95 values were 0.177088 ms and 0.068059 ms,
respectively. These are generator checks, not model latency measurements or
bounds on every individual scheduling outlier. All 552 independent audit checks
passed. The healthy-reference windows were 2,880/2,880 SLO-good, with zero
pre-restart admission rejections across all history windows.

## All 12 pairs

Each row retains 720 primary offers per arm. Positive differences favor 400 ms.

| Pair | Execution order (ms) | 500 ms misses | 400 ms misses | Fewer misses |
| --- | --- | ---: | ---: | ---: |
| 1 | 500 → 400 | 291 | 260 | 31 |
| 2 | 500 → 400 | 266 | 251 | 15 |
| 3 | 500 → 400 | 278 | 251 | 27 |
| 4 | 400 → 500 | 287 | 268 | 19 |
| 5 | 400 → 500 | 289 | 239 | 50 |
| 6 | 500 → 400 | 291 | 251 | 40 |
| 7 | 500 → 400 | 294 | 240 | 54 |
| 8 | 400 → 500 | 285 | 242 | 43 |
| 9 | 400 → 500 | 295 | 254 | 41 |
| 10 | 400 → 500 | 271 | 242 | 29 |
| 11 | 500 → 400 | 285 | 251 | 34 |
| 12 | 400 → 500 | 288 | 249 | 39 |

## Secondary observations

The fixed first 30-second recovery window was eligible in all 12 pairs and
contained 2,160 offers per arm. SLO-good responses increased 617 → 771; 10 pairs
favored 400 ms, while pairs 3 and 4 favored 500 ms in this secondary window.
The primary 120-second outcome remains the main comparison.

Mean restart-to-first-health time was 56.3994 s at 500 ms and 56.7111 s at 400 ms.
The guard's primary benefit therefore accompanied a slightly later mean peer
return. Survivor-only first 30-second windows provide a separate view before
replacement return.

During those fixed windows, 1 Hz samples showed survivor running-request maxima
of 16, waiting maxima of 0–2, and KV-cache sampled maxima between 43.39% and
46.25%, with zero observed preemption-counter increases. Sampled maxima are not
exact peaks; these observations do not establish unused compute capacity.

A **posthoc descriptive** join found 821 baseline local body-prewrite events
with under 100 ms left until the 500 ms first-token deadline; five were SLO-good.
The guard had zero such events. These are local prewrite timestamps, not
wire-transmission or engine-acceptance guarantees. Total primary body handoffs
were 7,154 → 7,104 and rejections 1,486 → 1,536. The 821 count is not a count of
counterfactually avoided requests: the two arms follow different queue
trajectories. Rejections still count as misses in the primary result.

Worker logs bracket early startup receipt gaps but do not provide function-level
attribution for the historical unexplained 38 s. That question remains open.

## Verification and retained evidence

Completed pairs were archived only after their workers and collectors closed.
Each archive was imported, hashed, and acknowledged locally before the next
pair began. All 12 local raw pairs and the terminal result were independently
recounted and agreed with the driver scores.

One local metadata-change check raised during raw-file verification. The
controller recovered the same detached job and completed delivery of all 12
pairs without rerunning traffic. Subsequent complete content-hash checks passed.
The original error was rethrown at CLI exit after verified delivery and cleanup;
the exact offending raw filename was not recorded.

The [curated aggregate JSON](../../evidence/research/2026-10-19-headroom/summary.json)
contains the full pair table, populations, controls, and source/plan/receipt
digests. It excludes request text, identifiers, credentials, provider endpoints,
billing, and private paths. Digests identify retained artifacts; they do not
make the private raw archive publicly replayable. The sealed research runner
and raw captures remain private, separate from the synthetic CLI demo.

## Earlier baseline

On 28 September 2026, a separate study on two A100-SXM4 40 GB GPUs used two short
286-input-token documents with 128 output tokens and both workers loaded.
It completed 21,300 requests: 2,100 calibration offers and 19,200 evaluation
offers. Four balanced blocks compared `round_robin`, `least_busy`, `cache_only`,
and `cache_plus_load`, with 4,800 evaluation offers per policy.

All four achieved 4.000 SLO-good requests/s at the tested 4 requests/s.
This was a valid tie; it demonstrated no routing-policy advantage or maximum
capacity boundary. The baseline report digest is included in the aggregate JSON.

The recovery study changes prompt length, offered load, GPU variant, and failure
condition. Its 65.30% attainment and the older baseline's result are not a direct
performance comparison. A separate later capacity sweep used 24,480 offers;
its 7.2% relative SLO-goodput result is not part of the 21,300-request baseline
or this recovery result.

## Limits

This is one host and one synthetic workload, with repeated observations rather
than independent production incidents. The 400 ms arm's 34.70% primary miss
rate remains above the 5% allowance of a 95% attainment target. The study
supports a bounded recovery improvement, not production SLA compliance,
a universal cutoff recommendation, or a general cache or routing advantage.

These later research observations do not change Inferdrome's frozen release
capability contracts or constitute a customer acceptance verdict.
