# SGLang 0.5.18 producer boundary

PR11 adds an additive, evidence-ineligible capability boundary for SGLang
`v0.5.18`. The source pin is the
[upstream release commit](https://github.com/sgl-project/sglang/tree/71de97b264b04dcd514cf904003028aefe9775c8):

```text
repository: https://github.com/sgl-project/sglang
release:    0.5.18
commit:     71de97b264b04dcd514cf904003028aefe9775c8
module:     python -m sglang.benchmark.serving
backend:    sglang
endpoint:   native /generate semantics
```

Inferdrome does not install or execute SGLang in this slice. The adapter only
builds a strict argv vector, probes an injected process runner for the exact
version, and normalizes one bounded detailed JSONL result. The argv vector is
never passed through a shell and has no credential or ambient-auth options.
`build_sglang_invocation` is a pure contract builder: it does not require the
tokenizer snapshot or output parent to exist. A future executor must call
`preflight_sglang_invocation` immediately before execution; that separate
check requires a real non-symlink tokenizer directory and an absent output
destination with safe ancestors. The endpoint must be loopback or an
explicitly private RFC1918/private-DNS endpoint.

The checked-in synthetic invocation uses stable deployment paths
`/opt/inferdrome/models/sglang-tokenizer` and
`/opt/inferdrome/evidence/sglang-detailed.jsonl`; artifact generation never
inspects those paths or a checkout-specific absolute path.

The native detailed result contract is deliberately narrow: one JSONL object
with the exact v0.5.18 result-writer field set. The invocation uses the native
streaming default; it deliberately does not pass the unsupported `--stream`
flag (and does not pass `--disable-stream`). It uses the offline `random-ids`
dataset with `--tokenize-prompt`, an explicit tokenizer snapshot, finite
request rate, and `--random-range-ratio 0`. The native row has no persisted
`streaming`, `num_prompts`, or derived `failed` field: attempted count is the
aligned array length and failed count is derived from non-empty string entries
in `errors`.

The persisted observation arrays are `input_lens`, `output_lens`, `ttfts`,
`itls`, `generated_texts`, and `errors`. Official defaults are retained:
`tag` is `null` when no tag is supplied, `server_info` is required but may be
`null`, `concurrency` is a finite numeric observation, and `accept_length` is
nullable numeric state. `server_info` is accepted as bounded endpoint-controlled
opaque JSON only to record its presence and canonical SHA-256; its contents are
never copied to a normalization report. Duplicate keys, non-finite numbers,
extra lines, unknown fields, type coercion, bounds violations, and
inconsistent populations fail closed.

The normalizer preserves the upstream timing definitions: TTFT is from request
start to the first cumulative response chunk with non-empty text, and ITL is
the interval between later qualifying chunks, apportioned over newly reported
completion tokens. Seconds are converted to nanoseconds with Decimal
round-half-even conversion. Missing request IDs and start offsets remain
unavailable; an ITL is never invented.

The report identity is `inferdrome.sglang-normalization.v1`, outside
`schemas/public/v1` and outside evidence bundles. It explicitly carries:

```text
evidence_eligible:             false
request_plan_binding:          UNAVAILABLE
request_start_offsets:         UNAVAILABLE
canonical_request_record_v1:   UNSUPPORTED
acceptance_verdict:            NOT_OWNED
```

The checked-in `tests/fixtures/sglang/v0_5/` files are synthetic and are not a
GPU capture, benchmark result, receipt, or customer evidence. The generator
`scripts/generate_sglang_normalization.py --check` verifies the schema, profile,
native fixture, canonical report, and fixture manifest.

SGLang's persisted output does not retain request IDs or request start times.
Its custom dataset loader can also skip malformed rows and shuffle accepted
rows. Consequently this adapter cannot bind the frozen Inferdrome request
plan or clock-domain start offsets, and it does not emit v0.1 request records.
Closing that boundary requires an upstream/native capture with request identity
and start offsets plus a separately versioned evidence v2 contract. Until then,
the existing lifecycle rejects SGLang before acquisition and the existing v0.1
vLLM schemas and semantics remain unchanged.
