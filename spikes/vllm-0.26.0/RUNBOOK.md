# vLLM 0.26.0 executable spike runbook

This runbook captures an honest native fixture from the exact pinned benchmark
producer against the deterministic mock endpoint.

## Preferred Linux requirements

- Linux `x86_64` or `aarch64` compatible with `manylinux_2_28`;
- Python 3.10 through 3.14;
- vLLM exactly `0.26.0`;
- Bash and curl; and
- no GPU requirement for this mock-endpoint client spike.

The later real-vLLM serving demonstration still requires a compatible GPU.

## Verify the producer artifact

The expected wheel hashes are recorded in `producer-pin.json`. Before
installation, download the wheel without dependencies and verify the matching
platform hash. Then install vLLM 0.26.0 in an isolated environment and confirm:

```bash
vllm --version
```

The output must identify `0.26.0`. The runner refuses other versions.

Install the optional benchmark dependencies needed by the custom JSONL loader,
including Pandas.

## macOS import-only reproduction

vLLM documents `VLLM_TARGET_DEVICE=empty` for imports on unsupported operating
systems. In 0.26.0, `setup.py` overwrites that selection with `cpu` on macOS and
attempts to compile native extensions. The checked-in compatibility patch only
allows the already-supported `empty` value to pass through.

Starting from the verified source distribution:

```bash
git apply /path/to/inferdrome/spikes/vllm-0.26.0/macos-empty-device.patch
python3.12 -m venv /private/tmp/inferdrome-vllm-empty-0.26.0
VLLM_TARGET_DEVICE=empty \
  /private/tmp/inferdrome-vllm-empty-0.26.0/bin/pip install .
/private/tmp/inferdrome-vllm-empty-0.26.0/bin/pip install pandas
```

The installed package reports `0.26.0+empty`. Before accepting a fixture,
compare the installed source hashes in `execution-environment.json` with
`producer-pin.json`. Do not use this environment to claim engine, model-serving,
CUDA, or GPU behavior.

## Execute

From the repository root:

```bash
./spikes/vllm-0.26.0/run_spike.sh \
  ./spikes/vllm-0.26.0/output/client-fixture
```

For the macOS import-only environment:

```bash
VLLM_TARGET_DEVICE=empty \
INFERDROME_SPIKE_COMPATIBILITY_PATCH_ID=macos-empty-device-v1 \
PATH=/private/tmp/inferdrome-vllm-empty-0.26.0/bin:$PATH \
./spikes/vllm-0.26.0/run_spike.sh \
  ./spikes/vllm-0.26.0/output/client-fixture
```

The destination must not already contain files. The runner starts the local
mock endpoint, waits for readiness, invokes the pinned producer, records its
exit status, and validates the native result.

## Expected artifacts

```text
output/client-fixture/
├── MANIFEST.sha256
├── execution-environment.json
├── invocation.json
├── producer-version.txt
├── exit-status.txt
├── native/
│   └── benchmark-result.json
├── stdout.log
├── stderr.log
├── mock-server.stdout.log
├── mock-server.stderr.log
├── mock-server.trace.jsonl
└── verification.json
```

The native result must remain untouched. Diagnostic mock traces are spike-only
artifacts and are not part of the future Inferdrome evidence contract.

## Review

After `verify_fixture.py` passes:

1. compare every native key and array invariant with `CAPABILITY_MATRIX.md`;
2. confirm one measured HTTP 503 appears only as producer error text;
3. compare the mock trace's role-only event with native TTFT semantics;
4. confirm warmup and preflight events appear in the mock trace but not the
   native detailed arrays;
5. confirm `generated_texts` contains response content;
6. confirm native output omits request IDs, HTTP status, finish reason, and
   per-request terminal latency; and
7. promote reviewed artifacts into a committed fixture in a separate change.

The first reviewed fixture is committed under
`fixtures/client-macos-empty/`.

The matrix becomes execution-verified only after this review is recorded.
