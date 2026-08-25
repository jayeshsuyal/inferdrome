#!/usr/bin/env bash
# Run the bounded local mock or the explicitly armed Linux/NVIDIA Compose path.

set -u

repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
compose_file=${INFERDROME_COMPOSE_FILE:-"$repository_root/compose.yaml"}
evidence_dir=${INFERDROME_COMPOSE_EVIDENCE_DIR:-"$repository_root/.inferdrome-compose/evidence"}
mode=${1:-mock}
shift || true
confirmation=${INFERDROME_ALLOW_GPU_COMPOSE:-}

usage() {
  printf '%s\n' \
    "usage: scripts/run_vllm_compose.sh mock" \
    "       scripts/run_vllm_compose.sh gpu --confirm-gpu"
}

fail() {
  printf 'vLLM Compose preflight failed: %s\n' "$1" >&2
  exit 2
}

while (($# > 0)); do
  case "$1" in
    --confirm-gpu)
      confirmation=1
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      fail "unsupported option"
      ;;
  esac
  shift
done

case "$mode" in
  mock)
    exit_service=synthetic-smoke
    ;;
  gpu)
    exit_service=vllm-benchmark-runner
    [[ "$confirmation" == "1" ]] || fail "GPU mode requires --confirm-gpu or INFERDROME_ALLOW_GPU_COMPOSE=1"
    [[ "$(uname -s)" == "Linux" ]] || fail "GPU mode requires Linux"
    command -v nvidia-smi >/dev/null 2>&1 || fail "GPU mode requires nvidia-smi"
    nvidia-smi -L >/dev/null 2>&1 || fail "GPU mode requires accessible NVIDIA GPU"
    [[ -n "${INFERDROME_VLLM_RUNNER_IMAGE:-}" ]] || fail "GPU mode requires an immutable runner image reference"
    [[ -n "${INFERDROME_VLLM_RUNTIME_IMAGE:-}" ]] || fail "GPU mode requires an immutable runtime image reference"
    [[ -n "${INFERDROME_QWEN3_PROFILE_ID:-}" ]] || fail "GPU mode requires the Qwen3 capability profile"
    [[ -n "${INFERDROME_QWEN3_MODEL_ID:-}" ]] || fail "GPU mode requires the Qwen3 model identity"
    [[ -n "${INFERDROME_QWEN3_MODEL_REVISION:-}" ]] || fail "GPU mode requires the Qwen3 model revision"
    [[ -n "${INFERDROME_QWEN3_TOKENIZER_REVISION:-}" ]] || fail "GPU mode requires the Qwen3 tokenizer revision"
    [[ -n "${INFERDROME_QWEN3_MODEL_PATH:-}" ]] || fail "GPU mode requires a prepared model path"
    [[ -n "${INFERDROME_EXPERIMENT_DIR:-}" ]] || fail "GPU mode requires an experiment input directory"
    [[ -n "${INFERDROME_COMPOSE_EVIDENCE_DIR:-}" ]] || fail "GPU mode requires an evidence output directory"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

[[ -f "$compose_file" && ! -L "$compose_file" ]] || fail "Compose file is unavailable"
if [[ -e "$evidence_dir" && ( ! -d "$evidence_dir" || -L "$evidence_dir" ) ]]; then
  fail "evidence output must be a real directory"
fi
mkdir -p -- "$evidence_dir" || fail "evidence output cannot be created"
[[ -d "$evidence_dir" && ! -L "$evidence_dir" && -w "$evidence_dir" ]] || fail "evidence output is not writable"

if [[ "$mode" == "gpu" ]]; then
  inferdrome_python=${INFERDROME_PYTHON:-python3}
  PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" "$inferdrome_python" -m inferdrome.vllm_compose gpu-preflight \
    --confirmation "$confirmation" \
    --platform "$(uname -s)" \
    --nvidia-available \
    --compose-available \
    --runner-image "$INFERDROME_VLLM_RUNNER_IMAGE" \
    --runtime-image "$INFERDROME_VLLM_RUNTIME_IMAGE" \
    --model-path "$INFERDROME_QWEN3_MODEL_PATH" \
    --experiment-dir "$INFERDROME_EXPERIMENT_DIR" \
    --evidence-dir "$evidence_dir" \
    --profile-id "$INFERDROME_QWEN3_PROFILE_ID" \
    --model-id "$INFERDROME_QWEN3_MODEL_ID" \
    --model-revision "$INFERDROME_QWEN3_MODEL_REVISION" \
    --tokenizer-revision "$INFERDROME_QWEN3_TOKENIZER_REVISION" || exit $?
fi

command -v docker >/dev/null 2>&1 || fail "Docker is unavailable"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is unavailable"

compose=(docker compose -f "$compose_file")
if [[ "$mode" == "gpu" ]]; then
  compose+=(--profile gpu)
fi
cleanup_status=0
cleanup() {
  set +e
  "${compose[@]}" down --remove-orphans --volumes >/dev/null 2>&1
  cleanup_status=$?
}
on_exit() {
  run_status=$?
  trap - EXIT
  cleanup
  if ((cleanup_status != 0)); then
    printf '%s\n' "vLLM Compose cleanup was not confirmed" >&2
    exit 70
  fi
  exit "$run_status"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

"${compose[@]}" up \
  --abort-on-container-exit \
  --exit-code-from "$exit_service" \
  --remove-orphans
run_status=$?
exit "$run_status"
