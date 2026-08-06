# Pinned vLLM 0.26.0 adapter

Status: **Implemented for PR 5**

Implementation date: **2026-08-05**

Inferdrome supports exactly `vllm bench serve` version `0.26.0` in v0.1. The
adapter is intentionally narrower than the full vLLM CLI. It accepts an
attached OpenAI-compatible endpoint, executes one measured benchmark command,
preserves producer artifacts, and normalizes only the detailed native shape
proven by the committed capability capture.

The source pin, local spike patch, exact captured artifacts, and field-level
findings remain under [`spikes/vllm-0.26.0`](../spikes/vllm-0.26.0/README.md).

## Attached endpoint preflight

Preflight performs one redirect-free `GET` against `<base-url>/v1/models` with
a bounded timeout and a 1 MiB response limit. It requires HTTP 200, strict
UTF-8 JSON with no duplicate keys or non-finite values, a non-empty model list,
and the exact configured model ID. The returned capture contains the untouched
response bytes and their tagged SHA-256 digest. Both the response bytes and the
strictly parsed result are embedded in canonical invocation evidence, so the
offline verifier can replay the model-list parse and cross-bind the
`server.model_id` environment claim without network access.

Endpoint URLs are already constrained by the public experiment contract: HTTP
or HTTPS only, with no user information, query, or fragment. v0.1 does not
support secret-bearing endpoint arguments or authorization headers.

## Exact invocation contract

The adapter builds an argument tuple and never invokes a shell. Its fixed
controls include:

- `openai-chat` backend and `/v1/chat/completions` endpoint;
- explicit base URL, model, local tokenizer, and custom JSONL dataset;
- requested output length and measured request count;
- disabled shuffle and skipped chat-template transformation;
- explicit request rate, burstiness, and optional concurrency limit;
- explicit warmup count, temperature, seed, and request-ID prefix;
- detailed result output and fixed result filename; and
- diagnostic E2E percentiles, which remain native diagnostics rather than
  canonical verdict metrics.

Every native result must echo exactly these five metadata entries:

```text
inferdrome_adapter_version
inferdrome_execution_fingerprint
inferdrome_producer_version
inferdrome_run_id
inferdrome_workload_sha256
```

Stored invocation JSON is canonical and is independently regenerated during
bundle verification. Only the absolute dataset, tokenizer, and result
directory paths come from the captured invocation; every other token must
match the frozen experiment and request plan exactly. Before invocation and
again immediately before execution, the dataset is read without following
links, checked for stable file identity, hashed against the resolved workload,
strictly parsed, and compared prompt-by-prompt with the request plan. Its path
identity must remain unchanged through producer exit.

vLLM 0.26.0 does not expose a per-request timeout option for this command.
Inferdrome therefore does not claim one. A bounded supervisor enforces the
resolved total runtime, drains stdout and stderr without shell interpolation,
caps each stream, and distinguishes normal exit, user cancellation, deadline,
output overflow, and pipe-holding orphan descendants. Termination applies to
the isolated producer process group. It performs no automatic retry.

## Producer and native preservation

`vllm --version` runs as a separate bounded probe with merged raw output. One
standalone `0.26.0` version line is required; a local-version suffix such as
the spike's `0.26.0+empty` is retained while the supported producer contract
remains `0.26.0`.

Before execution, the expected native result path must not exist. Afterward,
the adapter reads it once with no-follow behavior, file identity checks, a
single-link requirement, and an explicit size limit. The resulting bytes are
not canonicalized or repaired. Exit status, stdout, stderr, invocation, version
output, overall UTC boundaries, and native JSON remain separate evidence.

## Normalization contract

The normalizer accepts exactly the detailed field set established by the real
four-request fixture. Additional keys are accepted only when they are the exact
`inferdrome_*` metadata entries present in the invocation. Missing fields,
unknown structural fields, changed cardinality, duplicate keys, non-finite
numbers, incoherent aggregates, unexpected metadata, a changed tokenizer ID,
or ambiguous success semantics fail closed.

The measured arrays are mapped by their frozen sequence index because vLLM's
native JSON does not carry request IDs. Canonical and producer request IDs come
from the pre-execution request plan; they are not represented as native
observations. Every canonical row retains its native array index.

The supported row semantics are:

- non-empty native error text means `FAILED`, including the stored fixture's
  HTTP 503 row;
- no error plus a positive producer TTFT means `SUCCESS`; and
- no error, zero TTFT, no ITLs, zero output tokens, and empty generated text
  means `ANOMALOUS_EMPTY_STREAM`.

The normalizer never parses HTTP status from error text. HTTP status, finish
reason, retry history, scheduled-send offset, per-request terminal latency, and
warmup rows remain unavailable.

Native seconds are parsed as decimal values and converted to integer
nanoseconds with decimal half-even rounding. Start times are normalized against
the minimum measured start in the same producer monotonic clock domain. TTFT is
named `vllm_first_choices_event_v0_26`, matching the producer's first
non-empty-`choices` event behavior rather than claiming first generated text.

The producer's aggregate E2E, throughput, RTF, and coarse maximum-concurrency
fields remain diagnostic. Canonical counts, latency distributions, and
throughput are reduced from canonical request records plus the native benchmark
duration.

## Bundle verification

For vLLM bundles the offline verifier:

1. validates the pinned resolved execution and attached target;
2. replays endpoint preflight and regenerates the exact invocation from frozen
   inputs;
3. validates raw producer-version output and exit status;
4. checks the native-result exact-byte digest;
5. renormalizes native output with invocation metadata and tokenizer identity;
6. requires byte-identical canonical request JSONL;
7. rebuilds the execution record without inventing phase boundaries; and
8. independently recalculates all measurements.

Environment evidence paths must name declared bundle artifacts. Server model,
producer version, and configured target identity claims are cross-bound to the
preflight, version artifact, and resolved target rather than trusted as free
text.

Detailed vLLM output always receives `RESPONSE_CONTENT` sensitivity because it
contains generated text even when canonical response content is omitted.

## Non-GPU conformance path

Normal pull-request tests require no GPU. The committed real capture includes
three successful measured rows and one failed row. A generator freezes its
native bytes, canonical records, execution record, metric definitions, derived
measurements, and exact-byte manifest. The engineering gate regenerates all six
files in check mode.

The sealed attached-endpoint integration fixture is explicitly
`EvidenceEligibility.INELIGIBLE`: it adapts the real captured measurements with
the production metadata/path echoes needed to exercise sealing and offline
verification. It is test evidence, not a customer benchmark claim. The raw
golden native artifact remains byte-identical to the actual vLLM capture.
