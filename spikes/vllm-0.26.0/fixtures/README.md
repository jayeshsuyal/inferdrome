# Committed capability fixtures

`client-macos-empty` is the first native golden fixture for the pinned vLLM
0.26.0 benchmark client.

It was generated with the exact source hashes in `producer-pin.json`, the
packaging-only `macos-empty-device.patch`, a deterministic local tokenizer, and
the repository mock endpoint. It verifies client-side request, timing, failure,
and serialization behavior only; it is not a model-serving or GPU result.

The native JSON is untouched. The mock trace is diagnostic fixture evidence and
is not part of Inferdrome's future production evidence contract.

Verify the captured files from the fixture directory:

```bash
shasum -a 256 -c MANIFEST.sha256
python3 ../../verify_fixture.py native/benchmark-result.json \
  --mock-trace mock-server.trace.jsonl
```

Expected high-level outcome:

- four measured rows in stable order;
- three successes and one intentional HTTP 503 failure;
- response text retained for successful rows;
- first-`choices` TTFT and event-based ITLs; and
- no native request ID, HTTP status, finish reason, retry ledger, warmup rows,
  scheduled offsets, or per-request terminal latency.
