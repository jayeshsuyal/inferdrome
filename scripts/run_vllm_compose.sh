#!/usr/bin/env bash
# Run the bounded local mock or the explicitly armed Linux/NVIDIA Compose path.

set -u

repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
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

for compose_variable in \
  INFERDROME_COMPOSE_FILE \
  INFERDROME_GPU_COMPOSE_FILE \
  COMPOSE_FILE \
  COMPOSE_ENV_FILES \
  COMPOSE_PROJECT_NAME \
  COMPOSE_PROFILES \
  DOCKER_HOST \
  DOCKER_CONTEXT; do
  [[ -z "${!compose_variable:-}" ]] || \
    fail "ambient $compose_variable override is not supported"
done
export COMPOSE_DISABLE_ENV_FILE=1
compose_file="$repository_root/compose.yaml"
gpu_compose_file="$repository_root/compose.gpu.yaml"

validate_identity_component() {
  value=$1
  label=$2
  case "$value" in
    ''|*[!0-9]*) fail "$label identity is invalid" ;;
  esac
  [[ "${#value}" -le 5 ]] || fail "$label identity is invalid"
  value_number=$((10#$value))
  (( value_number >= 1 && value_number <= 65534 )) || fail "$label identity is invalid"
}

validate_local_docker_context() {
  docker_context_host=$(docker context inspect \
    --format '{{json (index .Endpoints "docker").Host}}' 2>/dev/null) || \
    fail "active Docker context could not be inspected"
  case "$docker_context_host" in
    \"unix://*|\"npipe://*) ;;
    *) fail "active Docker context is not local" ;;
  esac
}

python_candidate_works() {
  candidate=$1
  [[ -x "$candidate" ]] || return 1
  "$candidate" -c \
    'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' \
    >/dev/null 2>&1 || return 1
  PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$candidate" -c \
    'import inferdrome.vllm_compose, jsonschema, pydantic, yaml, rfc8785' \
    >/dev/null 2>&1 || return 1
  return 0
}

select_gpu_python() {
  if [[ -n "${INFERDROME_PYTHON:-}" ]]; then
    python_candidate_works "$INFERDROME_PYTHON" || \
      fail "GPU preflight requires a compatible Inferdrome Python 3.12 interpreter"
    inferdrome_python=$INFERDROME_PYTHON
    return
  fi

  repository_python="$repository_root/.venv/bin/python"
  if python_candidate_works "$repository_python"; then
    inferdrome_python=$repository_python
    return
  fi

  python3_candidate=$(command -v python3 2>/dev/null || true)
  if [[ -n "$python3_candidate" ]] && python_candidate_works "$python3_candidate"; then
    inferdrome_python=$python3_candidate
    return
  fi
  fail "GPU preflight requires a compatible Inferdrome Python 3.12 interpreter"
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
if [[ "$mode" == "gpu" ]]; then
  [[ -f "$gpu_compose_file" && ! -L "$gpu_compose_file" ]] || \
    fail "GPU Compose override is unavailable"
fi
if [[ -e "$evidence_dir" && ( ! -d "$evidence_dir" || -L "$evidence_dir" ) ]]; then
  fail "evidence output must be a real directory"
fi
compose_uid=$(id -u 2>/dev/null) || fail "host uid cannot be determined"
compose_gid=$(id -g 2>/dev/null) || fail "host gid cannot be determined"
validate_identity_component "$compose_uid" "Compose uid"
validate_identity_component "$compose_gid" "Compose gid"
export INFERDROME_COMPOSE_UID="$compose_uid"
export INFERDROME_COMPOSE_GID="$compose_gid"
mkdir -p -- "$evidence_dir" || fail "evidence output cannot be created"
[[ -d "$evidence_dir" && ! -L "$evidence_dir" && -w "$evidence_dir" ]] || fail "evidence output is not writable"

if [[ "$mode" == "gpu" ]]; then
  select_gpu_python
  preflight_args=(
    -m inferdrome.vllm_compose gpu-preflight
    --confirmation "$confirmation"
    --platform "$(uname -s)"
    --nvidia-available
    --runner-image "$INFERDROME_VLLM_RUNNER_IMAGE"
    --runtime-image "$INFERDROME_VLLM_RUNTIME_IMAGE"
    --model-path "$INFERDROME_QWEN3_MODEL_PATH"
    --experiment-dir "$INFERDROME_EXPERIMENT_DIR"
    --evidence-dir "$evidence_dir"
    --profile-id "$INFERDROME_QWEN3_PROFILE_ID"
    --model-id "$INFERDROME_QWEN3_MODEL_ID"
    --model-revision "$INFERDROME_QWEN3_MODEL_REVISION"
    --tokenizer-revision "$INFERDROME_QWEN3_TOKENIZER_REVISION"
    --uid "$compose_uid"
    --gid "$compose_gid"
  )
  PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" "$inferdrome_python" \
    "${preflight_args[@]}" || exit $?
fi

command -v docker >/dev/null 2>&1 || fail "Docker is unavailable"
validate_local_docker_context
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is unavailable"

if [[ "$mode" == "gpu" ]]; then
  PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" "$inferdrome_python" \
    "${preflight_args[@]}" --compose-available || exit $?
fi

compose=(docker compose -f "$compose_file")
if [[ "$mode" == "gpu" ]]; then
  compose+=(
    -f "$gpu_compose_file"
    --profile gpu
  )
  export INFERDROME_GPU_COMPOSE_GATE=1
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
  --remove-orphans \
  "$exit_service"
run_status=$?
exit "$run_status"
