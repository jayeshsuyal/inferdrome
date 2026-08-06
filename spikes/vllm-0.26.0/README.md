# vLLM 0.26.0 capability spike

Status: **Complete — source and execution verified**

Schema gate: **Passed; PR 1 is unblocked**

This spike determines what the exact pinned `vllm bench serve` producer can
honestly contribute to Inferdrome v0.1 canonical evidence.

## Producer selection

vLLM `0.26.0` was the current stable PyPI release when the spike began on
2026-08-05. The release tag resolves to Git commit
`568afb3a13806beb53bb2e6bd518269357b237c0`.

Exact distribution and analyzed-source hashes are recorded in
[producer-pin.json](producer-pin.json).

## Work completed

- The official source distribution was downloaded and its SHA-256 verified.
- Producer result serialization was inspected at the pinned tag.
- Chat streaming, timing, request-ID, warmup, traffic, and error behavior were
  traced through the pinned source.
- A field-level [capability matrix](CAPABILITY_MATRIX.md) was produced.
- A deterministic OpenAI-compatible mock endpoint and a fixture verifier were
  prepared for the executable client spike.
- A [runbook](RUNBOOK.md) defines the exact fixture-capture procedure.
- The exact benchmark client ran against the mock endpoint and produced a
  verifier-passing [native fixture](fixtures/README.md).
- Installed producer source hashes were checked against the selected source
  distribution.

## Execution environment and compatibility boundary

The fixture ran on Apple Silicon macOS under CPython 3.12.13 using vLLM's
documented import-only `VLLM_TARGET_DEVICE=empty` mode. vLLM 0.26.0 contains a
packaging-order bug that overwrites `empty` with `cpu` on macOS, so the build
used [macos-empty-device.patch](macos-empty-device.patch). That one-line patch
changes `setup.py` platform selection only. It does not alter benchmark,
endpoint-client, dataset-loader, or result-serialization code.

The installed distribution reports `0.26.0+empty`. Its relevant Python source
hashes match [producer-pin.json](producer-pin.json), and the exact environment
is captured with the fixture.

This proves the client and native serializer behavior under test. It does not
claim that the vLLM engine can serve models on macOS, validate CUDA behavior,
or replace the later real-GPU release proof.

## Spike design

The deterministic mock endpoint intentionally:

- returns a role-only streaming event before generated content;
- returns two content events and a terminal `finish_reason` event;
- emits a separate usage event;
- fails exactly one measured request with HTTP 503;
- allows preflight and warmup requests to succeed; and
- records a server-side diagnostic event trace.

This makes upstream TTFT, completion, error, response-content, and warmup
behavior visible without requiring a model or GPU. The later real-GPU proof is a
separate release gate.

## Files

- `producer-pin.json`: exact producer and source identity;
- `CAPABILITY_MATRIX.md`: source-backed field classification;
- `RUNBOOK.md`: compatible-host execution instructions;
- `workload.jsonl`: non-sensitive deterministic input;
- `tokenizer/`: deterministic local tokenizer used only to load the workload;
- `mock_openai_server.py`: deterministic test endpoint;
- `run_spike.sh`: exact producer invocation and artifact capture; and
- `verify_fixture.py`: structural fixture gate;
- `macos-empty-device.patch`: recorded packaging-only compatibility patch; and
- `fixtures/client-macos-empty/`: committed raw fixture and integrity manifest.

## Completion condition

The spike completion conditions were:

1. the exact pinned benchmark and serialization sources execute;
2. untouched native JSON, stdout, stderr, invocation, version, and mock trace
   are captured;
3. the fixture verifier passes;
4. source-derived claims are reconciled with observed output; and
5. the capability matrix changes from execution-pending to execution-verified.

All five conditions are satisfied. A Linux client run remains a useful
cross-platform confirmation, while the real-GPU proof remains a separate v0.1
release gate.
