#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "$0")/.." && pwd)
state_root="$repository_root/.inferdrome-gpu"
capture_root="$repository_root/gpu-proof-output/capture"
gpu_index=0
startup_timeout_seconds=900
managed_capability_profile=""
qwen3_profile_id=managed-vllm-0.26-qwen3-8b-bf16-v1

fail() {
  echo "run-real-gpu-capture: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: ./scripts/run_real_gpu_capture.sh [options]

Options:
  --state-root PATH               New pinned environment destination
  --capture-root PATH             New proof destination named "capture"
  --gpu-index INDEX               Physical NVIDIA GPU index (default: 0)
  --startup-timeout-seconds N     Per-model-load timeout (default: 900)
  --managed-capability-profile ID Explicit Qwen3 capability-spike profile
  -h, --help                      Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-root)
      [[ $# -ge 2 ]] || fail "--state-root requires a path"
      state_root=$2
      shift 2
      ;;
    --capture-root)
      [[ $# -ge 2 ]] || fail "--capture-root requires a path"
      capture_root=$2
      shift 2
      ;;
    --gpu-index)
      [[ $# -ge 2 ]] || fail "--gpu-index requires a value"
      gpu_index=$2
      shift 2
      ;;
    --startup-timeout-seconds)
      [[ $# -ge 2 ]] || fail "--startup-timeout-seconds requires a value"
      startup_timeout_seconds=$2
      shift 2
      ;;
    --managed-capability-profile)
      [[ $# -ge 2 ]] || fail "--managed-capability-profile requires a value"
      [[ -z $managed_capability_profile ]] || \
        fail "managed capability profile cannot be supplied twice"
      managed_capability_profile=$2
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done

if [[ -n $managed_capability_profile && \
      $managed_capability_profile != "$qwen3_profile_id" ]]; then
  fail "managed capability profile is unsupported"
fi

command -v python3.12 >/dev/null || fail "python3.12 is required"
command -v tar >/dev/null || fail "tar is required"
command -v sha256sum >/dev/null || fail "GNU sha256sum is required"
[[ $gpu_index =~ ^[0-9]+$ ]] || fail "GPU index must be a nonnegative integer"
(( gpu_index <= 255 )) || fail "GPU index must be between 0 and 255"
[[ $startup_timeout_seconds =~ ^[0-9]+([.][0-9]+)?$ ]] || \
  fail "startup timeout must be a positive number"
python3.12 - "$startup_timeout_seconds" <<'PY' || \
  fail "startup timeout must be between 1 and 3600 seconds"
import math
import sys

value = float(sys.argv[1])
if not math.isfinite(value) or not 1 <= value <= 3600:
    raise SystemExit(1)
PY

mapfile -t resolved_paths < <(
  python3.12 - "$state_root" "$capture_root" <<'PY'
from pathlib import Path
import sys

state = Path(sys.argv[1]).absolute()
capture = Path(sys.argv[2]).absolute()
if capture.name != "capture":
    raise SystemExit('capture destination must be named "capture"')
if state == capture or state.is_relative_to(capture) or capture.is_relative_to(state):
    raise SystemExit("state and capture destinations must be disjoint")
for value in (state, capture):
    if "\n" in str(value) or "\r" in str(value):
        raise SystemExit("capture paths cannot contain line breaks")
print(state)
print(capture)
PY
)
[[ ${#resolved_paths[@]} -eq 2 ]] || fail "capture paths are invalid"
state_root=${resolved_paths[0]}
capture_root=${resolved_paths[1]}
archive_path="$capture_root.tar.gz"
checksum_path="$archive_path.sha256"

[[ ! -e $state_root && ! -L $state_root ]] || \
  fail "state destination already exists: $state_root"
[[ ! -e $capture_root && ! -L $capture_root ]] || \
  fail "capture destination already exists: $capture_root"
[[ ! -e $archive_path && ! -L $archive_path ]] || \
  fail "capture archive already exists: $archive_path"
[[ ! -e $checksum_path && ! -L $checksum_path ]] || \
  fail "capture checksum already exists: $checksum_path"

if command -v git >/dev/null 2>&1 && \
   git -C "$repository_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  repository_status=$(
    git -C "$repository_root" status --porcelain --untracked-files=normal
  )
  [[ -z $repository_status ]] || fail "the Inferdrome checkout must be clean"
  repository_commit=$(git -C "$repository_root" rev-parse --verify HEAD)
else
  source_export_path="$repository_root/.inferdrome-source-export.json"
  [[ -f "$source_export_path" && ! -L "$source_export_path" ]] || \
    fail "Inferdrome source provenance is unavailable"
  repository_commit=$(
    python3.12 - "$source_export_path" <<'PY'
import json
from pathlib import Path
import re
import sys

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if set(value) != {
    "repository_commit",
    "schema_version",
    "source_archive_sha256",
    "transport",
}:
    raise SystemExit("source export marker has an unexpected shape")
if value["schema_version"] != "inferdrome.source-tree-export.v1":
    raise SystemExit("source export marker version is unsupported")
if value["transport"] != "git-archive-exact-head-tree-v1":
    raise SystemExit("source export transport is unsupported")
if re.fullmatch(r"[0-9a-f]{40}", value["repository_commit"]) is None:
    raise SystemExit("source export commit is invalid")
if re.fullmatch(r"sha256:[0-9a-f]{64}", value["source_archive_sha256"]) is None:
    raise SystemExit("source export digest is invalid")
print(value["repository_commit"])
PY
  )
fi
[[ $repository_commit =~ ^[0-9a-f]{40}$ ]] || \
  fail "the Inferdrome commit cannot be resolved"

mkdir -p "$capture_root/logs"
capture_complete=false
current_step=initializing

finalize_capture() {
  local original_status=$?
  local archive_status=0
  trap - EXIT INT TERM
  set +e
  if [[ $capture_complete != true ]]; then
    if (( original_status == 0 )); then
      original_status=1
    fi
    python3.12 "$repository_root/scripts/real_gpu_capture.py" write-failure \
      --capture-root "$capture_root" \
      --repository-commit "$repository_commit" \
      --failed-step "$current_step" \
      --exit-code "$original_status"
  fi
  tar -C "$(dirname "$capture_root")" \
    -czf "$archive_path" "$(basename "$capture_root")"
  archive_status=$?
  if (( archive_status == 0 )); then
    sha256sum "$archive_path" > "$checksum_path"
    archive_status=$?
  fi
  if (( archive_status == 0 )) && [[ -n $managed_capability_profile ]]; then
    python3.12 - "$archive_path" "$checksum_path" <<'PY'
import json
from pathlib import Path
import re
import sys

archive = Path(sys.argv[1])
checksum = Path(sys.argv[2]).read_text(encoding="ascii").split()
if len(checksum) != 2 or re.fullmatch(r"[0-9a-f]{64}", checksum[0]) is None:
    raise SystemExit("capture checksum is invalid")
metadata = {
    "archive_name": archive.name,
    "archive_sha256": f"sha256:{checksum[0]}",
    "schema_version": "inferdrome.qwen3-transfer-metadata.v1",
    "size_bytes": archive.stat().st_size,
}
archive.with_suffix(archive.suffix + ".metadata.json").write_text(
    json.dumps(metadata, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
    archive_status=$?
  fi
  if (( original_status == 0 && archive_status != 0 )); then
    original_status=$archive_status
  fi
  if (( archive_status == 0 )); then
    echo "capture_archive_path=$archive_path"
    echo "capture_checksum_path=$checksum_path"
  else
    echo "run-real-gpu-capture: capture archive finalization failed" >&2
  fi
  exit "$original_status"
}

trap finalize_capture EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_logged() {
  local name=$1
  shift
  "$@" 2>&1 | tee "$capture_root/logs/$name.log"
}

current_step=prepare-host
if [[ -n $managed_capability_profile ]]; then
  run_logged 01-prepare-host \
    "$repository_root/scripts/prepare_real_gpu_host.sh" \
    --state-root "$state_root" \
    --managed-capability-profile "$managed_capability_profile"
else
  run_logged 01-prepare-host \
    "$repository_root/scripts/prepare_real_gpu_host.sh" "$state_root"
fi

mkdir -p "$capture_root/support"
cp "$state_root/host-preparation.json" "$capture_root/support/"
cp "$state_root/python-packages.txt" "$capture_root/support/"
cp "$state_root/vllm-version.txt" "$capture_root/support/"
cp "$state_root/inferdrome-version.txt" "$capture_root/support/"

prepared_python="$state_root/venv/bin/python"

if [[ -n $managed_capability_profile ]]; then
  cp \
    "$repository_root/campaigns/v1/profiles/$qwen3_profile_id.json" \
    "$capture_root/support/campaign-profile.json"
  cp \
    "$repository_root/campaigns/v1/profiles/qwen3-8b-host-dependencies.json" \
    "$capture_root/support/host-dependencies.json"
  cp \
    "$repository_root/campaigns/v1/profiles/qwen3-8b-model-files.json" \
    "$capture_root/support/model-files.json"
  cp \
    "$repository_root/campaigns/v1/qwen3-8b-concurrency-1.yaml" \
    "$capture_root/support/source.yaml"
  cp \
    "$repository_root/campaigns/v1/workloads/qwen-text-mixed-length-v1.manifest.json" \
    "$capture_root/support/workload-manifest.json"
  chmod a-w "$capture_root"/support/*

  mapfile -t prepared_profile < <(
    "$prepared_python" - "$state_root/host-preparation.json" <<'PY'
import json
from pathlib import Path
import sys

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if value.get("schema_version") != "inferdrome.qwen3-host-preparation.v1":
    raise SystemExit("Qwen3 host preparation receipt is unsupported")
if value.get("profile_id") != "managed-vllm-0.26-qwen3-8b-bf16-v1":
    raise SystemExit("Qwen3 host preparation profile drifted")
model_root = value.get("model_directory")
if not isinstance(model_root, str) or not Path(model_root).is_absolute():
    raise SystemExit("Qwen3 model directory is invalid")
if "\n" in model_root or "\r" in model_root:
    raise SystemExit("Qwen3 model directory is invalid")
print(model_root)
PY
  )
  [[ ${#prepared_profile[@]} -eq 1 ]] || \
    fail "Qwen3 prepared model path could not be read"
  model_root=${prepared_profile[0]}
  mkdir -p "$capture_root/runs"

  current_step=qwen3-a10-capability-spike
  run_logged 02-qwen3-a10-capability-spike \
    "$prepared_python" -m inferdrome run \
    "$repository_root/campaigns/v1/qwen3-8b-concurrency-1.yaml" \
    --runs-root "$capture_root/runs" \
    --tokenizer-path "$model_root" \
    --managed-local-vllm \
    --managed-model-path "$model_root" \
    --managed-gpu-index "$gpu_index" \
    --managed-startup-timeout-seconds "$startup_timeout_seconds" \
    --managed-capability-profile "$managed_capability_profile"

  current_step=write-qwen3-capture-manifest
  run_logged 03-write-qwen3-capture-manifest \
    "$prepared_python" "$repository_root/scripts/qwen3_gpu_capture.py" write \
    --capture-root "$capture_root" \
    --repository-commit "$repository_commit"
else
  mkdir -p "$capture_root/single" "$capture_root/comparison"
  chmod a-w "$capture_root"/support/*

  current_step=single-proof
  run_logged 02-single-proof \
    "$prepared_python" "$repository_root/scripts/run_real_gpu_demo.py" \
    --state-root "$state_root" \
    --output-root "$capture_root/single" \
    --gpu-index "$gpu_index" \
    --startup-timeout-seconds "$startup_timeout_seconds"

  current_step=comparison-proof
  run_logged 03-comparison-proof \
    "$prepared_python" "$repository_root/scripts/run_real_gpu_demo.py" \
    --comparison \
    --state-root "$state_root" \
    --output-root "$capture_root/comparison" \
    --gpu-index "$gpu_index" \
    --startup-timeout-seconds "$startup_timeout_seconds"

  current_step=write-capture-manifest
  run_logged 04-write-capture-manifest \
    "$prepared_python" "$repository_root/scripts/real_gpu_capture.py" write \
    --capture-root "$capture_root" \
    --repository-commit "$repository_commit"
fi

capture_complete=true
current_step=complete
