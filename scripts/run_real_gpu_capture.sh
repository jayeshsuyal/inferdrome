#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "$0")/.." && pwd)
state_root="$repository_root/.inferdrome-gpu"
capture_root="$repository_root/gpu-proof-output/capture"
gpu_index=0
startup_timeout_seconds=900

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
    -h | --help)
      usage
      exit 0
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done

command -v python3.12 >/dev/null || fail "python3.12 is required"
command -v git >/dev/null || fail "git is required"
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

repository_status=$(git -C "$repository_root" status --porcelain --untracked-files=normal)
[[ -z $repository_status ]] || fail "the Inferdrome checkout must be clean"
repository_commit=$(git -C "$repository_root" rev-parse --verify HEAD)
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
run_logged 01-prepare-host \
  "$repository_root/scripts/prepare_real_gpu_host.sh" "$state_root"

mkdir -p "$capture_root/support" "$capture_root/single" \
  "$capture_root/comparison"
cp "$state_root/host-preparation.json" "$capture_root/support/"
cp "$state_root/python-packages.txt" "$capture_root/support/"
cp "$state_root/vllm-version.txt" "$capture_root/support/"
cp "$state_root/inferdrome-version.txt" "$capture_root/support/"
chmod a-w "$capture_root"/support/*

prepared_python="$state_root/venv/bin/python"

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

capture_complete=true
current_step=complete
