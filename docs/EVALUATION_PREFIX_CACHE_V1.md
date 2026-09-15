# Bounded prefix-cache experiment v1

This extension compares shared-document and unique-document traffic with prefix
caching declared disabled/enabled on the existing two vLLM replicas. It compiles
a finite plan, runs **one externally prepared cell per invocation**, and imports
the resulting private directories into an offline report. It has no serving
management, cache reset, provider, shell-hook, retry, or resume capability.

The experiment reuses the PR1 streaming request, fixed-arrival scheduler,
per-offer endpoint assignment, and bounded cleanup. PR3 supplies the private
file primitives, strict population importer, and scheduled-origin SLO summary.
PR2 policies and faults are unchanged. PR3's four-policy study schema and
serialized report meaning are unchanged; cache conditions are separate types.

All runtime and cache-treatment attribution remains **UNVERIFIED** and
`evidence_eligible` remains **false**. No paid experiment is authorized by this
guide or by a valid plan. Calibration, cache hits, cost, hardware attestation,
and causal prefill/decode timing are unavailable. The new local tests establish
code behavior with fake tokenizers/transports and localhost HTTP servers; they
do not establish vLLM cache behavior.

## Four cells and fixed assignment

| Condition | Document workload | External prefix-cache declaration |
|---|---|---|
| S0 | One shared document followed by distinct question suffixes | Disabled |
| S1 | Exactly the same S0 prompts, settings, and arrival schedule | Enabled |
| U0 | Distinct documents paired with the same question suffixes | Disabled |
| U1 | Exactly the same U0 prompts, settings, and arrival schedule | Enabled |

One block contains each condition once. Offer index zero goes to endpoint A,
index one to B, and so on, in every cell. Earlier capacity rejection or timeout
does not shift the assignment of a later offer. A rejected request does not
establish that a prefix reached the server. Actual dispatch counts remain
visible beside planned assignments.

The compiler accepts **one, four, or eight blocks**. One complete block is a
bounded rehearsal/pilot with low replication and no interval or generalized
performance claim. Four/eight blocks use each of these Williams orders once or
twice, respectively; their block order is explicit in the input:

```text
S0 S1 U1 U0
S1 U0 S0 U1
U0 U1 S1 S0
U1 S0 U0 S1
```

Every condition occupies each position equally, and directed predecessor pairs
are balanced across four blocks. Every block needs a distinct ID, declared
workload seed, and shared document. Seeds label an explicit finite workload;
the compiler does not use them as model-sampling or prompt-generation seeds.
Eight complete valid blocks are required for the descriptive interval.
Repetition and counterbalancing do not establish independent hardware state.

Each block supplies one `shared_document`, at least four ordered `cases`,
`cell_order`, and four globally unique `attempt_ids` in that order. Each case
supplies `unique_document`, `suffix`, and `scheduled_ns`. Unique documents and
suffixes must each be distinct within a block. The fixed user-message renderer
is:

```text
document + "\n\nQuestion:\n" + suffix
```

Documents precede questions. Reserved chat-control marker syntax is rejected.
There is no arbitrary template evaluator or automatic document generator. Use
explicit inert synthetic text and inspect the compiled token checks before any
run. Shared/unique raw text lengths need not match, but corresponding **complete
rendered token lengths must match exactly**.

## Pinned local tokenization

The model is `Qwen/Qwen3-8B`, with model/tokenizer revision
`b968826d9c46dd6066d109eabc6255188de91218`. Local verification uses the existing
Qwen file reader, frozen non-thinking one-user renderer, and exactly
`tokenizers==0.22.1`. It requires independently verified regular local files:

| File | SHA-256 |
|---|---|
| `tokenizer.json` | `aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4` |
| `tokenizer_config.json` | `d5d09f07b48c3086c508b30d1c9114bd1189145b74e982a265350c923acd8101` |

No command downloads files or installs dependencies. A missing package or
snapshot produces `verification_status: UNAVAILABLE` and `executable: false`.
Wrong hashes, package versions, links, malformed input, or failed workload
matching reject verification. The repository's other tokenizer fixtures are
not substitutes. The historical verifier's 96-prompt counts do not verify a new
cache workload.

For every pair, the verifier encodes the whole exact rendered prompt with
`add_special_tokens=False`, including chat markers and generation prefix. It
checks context length plus the common `max_tokens` ceiling against 2,048. No
truncation is performed. It stores sanitized prompt/rendered-text/token-ID
digests and counts, without storing raw text or token IDs in the plan.

The certificate reports common wrapper overlap, full shared longest common
prefix, a conservative prefix ending within the shared document, and the
largest pairwise prefix in the unique arm. The unique-arm calculation uses
sorted neighboring token sequences, rather than an unbounded quadratic scan.
The declared cache block size is required; there is no guessed default. The
shared **document** must supply at least one additional complete matching
prefix block beyond the unique/wrapper overlap. A long common question cannot
satisfy that requirement. Both full and document prefix token sequences have
digests over canonical JSON integer-array bytes.

Complete matching blocks are only **potentially reusable**. Template boilerplate
can be shared in the unique arm. Exact token equality does not demonstrate cache
residency, hits, evictions, or saved work. This distinction follows vLLM's
token-prefix block design. [vLLM 0.26.0 prefix-cache design](https://github.com/vllm-project/vllm/blob/v0.26.0/docs/design/prefix_caching.md)

The Python-only injected verifier is a test seam. Its certificate is forcibly
`SYNTHETIC_TOKENIZER` / `SYNTHETIC_ONLY`, with no observed pinned-tokenizer
identity and `executable: false`. Running such a plan requires an explicitly
injected transport and two loopback origins; default client construction is
disabled. The CLI exposes no such injection seam.

## Request and serving boundaries

PR1 still sends one user message, temperature zero, non-thinking chat-template
selection, `n: 1`, streamed usage requested, and the common `max_tokens` ceiling.
No new seed, `ignore_eos`, cache salt, prompt-token API, or arbitrary extra
request parameters are added. The historical campaign's temperature-0.7 and
ignored-EOS sampling recipe is not the current replay contract. Equal
`max_tokens` does not imply equal generated lengths.

Preparation declares the existing official image:

```text
vllm/vllm-openai@sha256:ffb2d59b1c059a5bd8d781320c9f5189de8293693b7d95da54befddaa54abf52
```

The repository pins vLLM 0.26.0 and the Qwen identity above. Those expected values
do not identify a newly contacted server. Endpoint origins remain canonical
private IPv4 HTTP addresses under PR1's existing restrictions; no credentials,
DNS names, redirects, ambient proxies, or new networking authority are added.

Before each cell, an operator separately prepares both engines with the same
declared cache mode, the existing approved serving recipe, and a common
model-warmup protocol. The intended initial state is absence of the measured
document prefix; reuse then develops during the measured stream. Initial
document accesses remain in N. The preferred declared method is fresh process
generations with disjoint model warmup. A separately reviewed clearing procedure
may be referenced, but this code neither performs nor verifies it.

Closing a client, waiting through a cooldown, or observing zero running requests
does not clear prefix state. Warmup must not prime the measured document. All
external warmup/reset traffic, time, tokens, human gaps, and spend remain
unmeasured. They are never represented as zero or included in a replay-only
resource budget.

## Immutable identities and preparation input

S0/S1 have the same PR1 client-config digest, as do U0/U1, because cache mode is
external. The distinct cache-cell identity binds condition, block, attempt,
workload, preparation protocol, and replay config. Exactly one attempt ID per
cell is frozen in the plan. A new attempt needs a new experiment plan; an omitted
failed attempt cannot be replaced by a different successful attempt ID.
Undisclosed duplicate execution under the same declared ID remains outside
local integrity guarantees.

The preparation schema is `inferdrome.evaluation-cache-preparation.v1`. It is a
closed JSON declaration, supplied explicitly to one invocation. It contains:

| Fields | Meaning |
|---|---|
| `plan_sha256`, `cell_id`, `cell_sha256`, `attempt_id`, `config_sha256` | Exact expected immutable slot and replay |
| `preparation_id`, `preparation_protocol_sha256` | Unique preparation record and common external procedure |
| `order_position`, `previous_attempt_id`, `chronology_reference` | Declared execution order and an external chronology reference |
| `runtime_recipe_sha256` | Common effective runtime recipe, excluding the deliberately varied cache mode and per-cell process identity |
| `serving_image_reference`, `model_revision`, `tokenizer_revision`, `max_model_len` | Pinned expected values; defaults remain declarations |
| `cache_block_size` | Same declared block granularity as the compiled plan |
| `endpoints` | Exactly ordered A/B entries, each with `process_generation_sha256` and `prefix_caching` |
| `initial_prefix_state`, `method`, `method_reference` | Intended measured-prefix absence and referenced preparation method |
| `model_warmup_reference`, `model_warmup_overlap` | External model-warmup record and disjointness declaration |
| `exclusive_traffic` | Whether unrelated traffic is declared absent |
| `runtime_verification`, `evidence_eligible` | Fixed `UNVERIFIED` and `false` |

The common runtime recipe reference should bind effective generation defaults,
KV dtype/memory/scheduler/hash settings, model/template identity, and both
replicas' intended working configuration. The preparation protocol reference
binds the initial-state and warmup method. References are content digests, not
URLs, paths, shell commands, credential containers, or executable hooks. The
reader does not fetch or attest their contents.

Known mode disagreement, wrong cell/attempt/config/protocol/order, or a different
block size rejects the preparation before replay. `UNKNOWN` declarations or
nullable missing references may accompany descriptive measurement, but make its
comparison ineligible. Reports check common runtime/method declarations, unique
preparation IDs, and distinct process generations for the fresh-process method.
The declared predecessor chain and chronology reference do not establish a
trusted distributed clock or prove that an external procedure occurred.

Config/model digests use canonical JSON without a trailing newline. Plan and
result-file digests cover exact canonical JSON plus LF. Prefix/token digests use
canonical integer arrays. Generated cell IDs are `cell-0000` onward; each cell
directory contains `plan.json`, `manifest.json`, and at most one generated
`trial-NNNN.json`. Directories are fresh/private and files are exclusive and
immutable. Existing descriptor-anchored no-follow, hard-link, mutation,
permission, and byte checks remain in force. Input artifact contents never
choose filesystem paths. No directory is resumed or overwritten.

## Commands

All examples name explicit local input/output paths. Endpoint or preparation
operations are not part of these commands. Start with an offline plan:

```bash
PYTHONPATH=src .venv/bin/python -m inferdrome.evaluation cache-plan \
  --config /private/tmp/cache-config.json \
  --output /private/tmp/cache-plan.json
```

Without verified tokenizer files this still writes a valid, explicitly
unavailable plan; it does not authorize execution. To verify with an already
available pinned local tokenizer, supply:

```text
--tokenizer-root /absolute/path/to/verified/Qwen3-8B-snapshot
```

After separate authorization and external preparation, a single-cell invocation
would be:

```bash
PYTHONPATH=src .venv/bin/python -m inferdrome.evaluation cache-run-cell \
  --config /private/tmp/cache-config.json \
  --tokenizer-root /absolute/path/to/verified/Qwen3-8B-snapshot \
  --cell-id cell-0000 \
  --preparation /private/tmp/cache-preparation-0000.json \
  --output-dir /private/tmp/cache-cell-0000
```

The runner returns after closing its one owned client. It never starts a second
cell. Unknown tokenizer support rejects before client construction. A stopped
returned replay retains all offers; an execution, cleanup, duration, or result
budget failure has an aborted manifest and no completed result. Reason, cleanup,
and elapsed fields must remain causally consistent on import. A failed output
publication does not become a completed cell merely because requests ran.

After obtaining separately prepared cell directories, report explicit mappings:

```bash
PYTHONPATH=src .venv/bin/python -m inferdrome.evaluation cache-report \
  --config /private/tmp/cache-config.json \
  --tokenizer-root /absolute/path/to/verified/Qwen3-8B-snapshot \
  --cell-input cell-0000=/private/tmp/cache-cell-0000 \
  --cell-input cell-0001=/private/tmp/cache-cell-0001 \
  --cell-input cell-0002=/private/tmp/cache-cell-0002 \
  --cell-input cell-0003=/private/tmp/cache-cell-0003 \
  --output-dir /private/tmp/cache-report
```

Reporting is offline and reconstitutes the expected plan from the same config
and local tokenizer. Missing/invalid directories remain visible as unavailable
cells. It writes `report.json` and `report.md` into a fresh private directory.

## Metric and uncertainty contract

Every offered request in fixed window W remains in N. G counts only SUCCESS
records satisfying both predeclared scheduled-arrival-to-first-content and
scheduled-arrival-to-terminal SLOs. Report G/W and G/N with rejection, error,
timeout, and cancellation outcomes. Successes during drain can qualify without
extending W. Missing or aborted cell measurements are unknown, not zero.

For each complete block:

```text
shared delta = goodput(S1) - goodput(S0)
unique delta = goodput(U1) - goodput(U0)
interaction  = shared delta - unique delta
```

The report retains all four cell values and uses equally weighted block
contrasts. One/four complete blocks allow descriptive contrasts with no interval.
Only eight complete, consistently declared blocks permit 2,000 whole-block
bootstrap resamples using the predeclared seed and nearest-rank 5th/95th bounds
for a descriptive 90% interval. The four paired cell measurements remain
together in each draw. There is no request bootstrap, averaged percentile,
post-hoc favorable-block selection, or division by a zero baseline ratio.

Any missing, invalid, cancelled, aborted, or unresolved-preparation cell
suppresses the experiment's comparative headline and interval. Valid completed
cells with ordinary request failures remain part of G/N/W and are not excluded.
Synthetic evidence stays visible, and incompatible evidence classes cannot
form a comparison. Success latency summaries include SLO misses; p99 stays
unavailable below 1,000 successes in the named cell population. Output-token
diagnostics use only valid server-reported usage with explicit missing coverage.

First-content timing is an application/SSE observation, not exact first-token
time or a server prefill timer. APC can avoid processing a matching prompt
prefix; it does not directly skip generation of new output tokens. End-to-end
effects can include queueing, output-length differences, batching, eviction,
and the initial accesses included in this experiment. The interaction is not
a cache-hit or prefill-only measurement. [vLLM APC limitations](https://docs.vllm.ai/en/v0.26.0/features/automatic_prefix_caching/)

## Resource and compatibility limits

| Resource | Ceiling |
|---|---|
| Experiment | 1, 4, or 8 blocks; at most 32 cells; one REHEARSAL profile |
| Owned offers | 4–4,000 per cell; 100,000 overall |
| Replay | Existing PR1 timeout/stream/drain caps; concurrency 64; queue 1,024; 500,000 reserved content timings per cell |
| Private config | 16 MiB input; existing strict JSON checks |
| Prompt text | 32 KiB per rendered user message; 8 MiB expanded shared+unique text across distinct workload families |
| Verification | 8 million processed token IDs; no generator search; existing bounded tokenizer-file reads |
| Context | 2,048 total input plus requested output tokens; no truncation |
| Plan | 1 MiB including detailed sanitized token proof; oversized plans reject rather than truncate |
| Cell artifacts | Declared result cap up to 64 MiB; 2 MiB metadata reserve per cell |
| Whole output | At most 1 GiB reserved, including every cell, 1 MiB plan allowance and 8 MiB reports |
| Reports | At most 4 MiB each for JSON and Markdown; read one cell at a time |
| Owned elapsed | Declared aggregate cap up to 24 hours; conservative duration + drain + 7 cleanup allowances per cell |

Bounds intersect: a workload below the offer ceiling can still exceed its text,
token, plan, or result budget. External preparation and time between separate
invocations are outside the owned elapsed limit; the reporter does not enforce
a paid-session clock. No capacity claim follows from a chosen arrival rate.

The first PR4 commit also closes two narrow PR3 offline importer gaps: fixed
assignment cannot report `REJECTED_ROUTE`, and any non-null selected endpoint
must match its offer even before dispatch. Public-importer regressions preserve
valid cancellation with no selection or the correct selection. This changes
acceptance of inconsistent imported failure records, not runtime routing or
normal PR3 output. Canonical regression checks cover the unchanged PR3 summary
shape after the shared SLO extraction.

Frozen campaigns, evidence/history, deployment/provider/image frameworks,
dashboard, dependency manifests, and the EXTERNAL_ONLY raw archive policy are
unchanged. Cache-affinity routing, another serving engine, and real experiment
execution remain outside this implementation. PR5 awaits separately approved
experiments and verified teardown.
