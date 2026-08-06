#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 OUTPUT_DIR" >&2
  exit 2
fi

spike_script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
spike_output_dir=$1
spike_port=${INFERDROME_SPIKE_PORT:-18080}
spike_server_pid=""

if [[ -d "$spike_output_dir" ]] && find "$spike_output_dir" -mindepth 1 -print -quit | grep -q .; then
  echo "refusing to overwrite non-empty output directory: $spike_output_dir" >&2
  exit 2
fi

mkdir -p "$spike_output_dir/native"

for required_command in python3 curl vllm; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    echo "required command not found: $required_command" >&2
    exit 2
  fi
done

python3 - "$spike_output_dir/execution-environment.json" <<'PY'
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import sys
from pathlib import Path

destination = Path(sys.argv[1])
package_names = ("vllm", "torch", "pandas", "transformers", "tokenizers", "aiohttp")
packages = {}
for package_name in package_names:
    try:
        packages[package_name] = importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        packages[package_name] = None

vllm_spec = importlib.util.find_spec("vllm")
if vllm_spec is None or not vllm_spec.submodule_search_locations:
    raise SystemExit("cannot locate installed vllm package")
vllm_root = Path(next(iter(vllm_spec.submodule_search_locations)))
source_paths = (
    "benchmarks/serve.py",
    "benchmarks/lib/endpoint_request_func.py",
    "benchmarks/datasets/datasets.py",
    "entrypoints/cli/benchmark/serve.py",
)
source_hashes = {}
for relative_path in source_paths:
    source_path = vllm_root / relative_path
    source_hashes[f"vllm/{relative_path}"] = hashlib.sha256(
        source_path.read_bytes()
    ).hexdigest()

record = {
    "schema_version": "inferdrome.spike-execution-environment.v1",
    "python": {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
    },
    "platform": {
        "machine": platform.machine(),
        "release": platform.release(),
        "system": platform.system(),
    },
    "packages": packages,
    "selected_environment": {
        "VLLM_TARGET_DEVICE": os.environ.get("VLLM_TARGET_DEVICE"),
        "compatibility_patch_id": os.environ.get(
            "INFERDROME_SPIKE_COMPATIBILITY_PATCH_ID"
        ),
    },
    "installed_vllm_source_sha256": source_hashes,
}
destination.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

python3 -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 15) else 1)' || {
  echo "Python 3.10 through 3.14 is required" >&2
  exit 2
}

vllm --version >"$spike_output_dir/producer-version.txt"
spike_producer_version=$(<"$spike_output_dir/producer-version.txt")
if [[ ! "$spike_producer_version" =~ (^|[^0-9])0\.26\.0([^0-9]|$) ]]; then
  echo "expected vLLM 0.26.0, got: $spike_producer_version" >&2
  exit 2
fi

cleanup_spike_server() {
  if [[ -n "$spike_server_pid" ]] && kill -0 "$spike_server_pid" 2>/dev/null; then
    kill "$spike_server_pid" 2>/dev/null || true
    wait "$spike_server_pid" 2>/dev/null || true
  fi
}
trap cleanup_spike_server EXIT INT TERM

python3 "$spike_script_dir/mock_openai_server.py" \
  --port "$spike_port" \
  --trace-path "$spike_output_dir/mock-server.trace.jsonl" \
  --fail-request-id "inferdrome-spike-2" \
  >"$spike_output_dir/mock-server.stdout.log" \
  2>"$spike_output_dir/mock-server.stderr.log" &
spike_server_pid=$!

spike_ready=0
for _ in {1..100}; do
  if curl -fsS "http://127.0.0.1:${spike_port}/health" >/dev/null 2>&1; then
    spike_ready=1
    break
  fi
  sleep 0.1
done

if [[ $spike_ready -ne 1 ]]; then
  echo "mock endpoint did not become ready" >&2
  exit 1
fi

spike_command=(
  vllm bench serve
  --backend openai-chat
  --base-url "http://127.0.0.1:${spike_port}"
  --endpoint /v1/chat/completions
  --model inferdrome/mock-model
  --tokenizer "$spike_script_dir/tokenizer"
  --dataset-name custom
  --dataset-path "$spike_script_dir/workload.jsonl"
  --custom-output-len 2
  --num-prompts 4
  --disable-shuffle
  --skip-chat-template
  --request-rate inf
  --max-concurrency 2
  --num-warmups 2
  --ready-check-timeout-sec 5
  --temperature 0
  --seed 42
  --request-id-prefix inferdrome-spike-
  --percentile-metrics e2el
  --metric-percentiles 50,95,99
  --save-result
  --save-detailed
  --result-dir "$spike_output_dir/native"
  --result-filename benchmark-result.json
  --metadata
  inferdrome_spike_id=vllm-0.26.0-client-capability
  inferdrome_producer_version=0.26.0
)

python3 - "$spike_output_dir/invocation.json" "${spike_command[@]}" <<'PY'
import json
import sys

destination, *arguments = sys.argv[1:]
with open(destination, "w", encoding="utf-8") as stream:
    json.dump({"argv": arguments}, stream, indent=2)
    stream.write("\n")
PY

set +e
"${spike_command[@]}" >"$spike_output_dir/stdout.log" 2>"$spike_output_dir/stderr.log"
spike_exit_status=$?
set -e
printf '%s\n' "$spike_exit_status" >"$spike_output_dir/exit-status.txt"

cleanup_spike_server
spike_server_pid=""

if [[ $spike_exit_status -ne 0 ]]; then
  echo "vLLM benchmark failed with exit status $spike_exit_status" >&2
  exit "$spike_exit_status"
fi

python3 "$spike_script_dir/verify_fixture.py" \
  "$spike_output_dir/native/benchmark-result.json" \
  --mock-trace "$spike_output_dir/mock-server.trace.jsonl" \
  >"$spike_output_dir/verification.json"
cat "$spike_output_dir/verification.json"

python3 - "$spike_output_dir" <<'PY'
import hashlib
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest_path = root / "MANIFEST.sha256"
lines = []
for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
    if path == manifest_path:
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

echo "capability spike succeeded: $spike_output_dir"
