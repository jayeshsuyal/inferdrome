#!/usr/bin/env bash
# Build the two fixed role images and run their final CPU-only smokes locally.
# This script has no registry login, push, provider, or cleanup operation.

set -euo pipefail

source_commit=""
work_root=""
while (($#)); do
  case "$1" in
    --source-commit)
      source_commit="${2:-}"
      shift 2
      ;;
    --work-root)
      work_root="${2:-}"
      shift 2
      ;;
    *)
      echo "unexpected build-and-smoke argument" >&2
      exit 2
      ;;
  esac
done

[[ "$source_commit" =~ ^[0-9a-f]{40}$ ]]
[[ "$work_root" == /* ]]
[[ "$(git rev-parse HEAD)" == "$source_commit" ]]
[[ -z "$(git status --porcelain)" ]]
mkdir -p -- "$work_root"

version="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"

normalize_labels() {
  local plan_path="$1"
  local observed_path="$2"
  local normalized_path="$3"
  python - "$plan_path" "$observed_path" "$normalized_path" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
observed = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
required = plan["required_labels"]
if not isinstance(observed, dict) or not all(
    isinstance(key, str) and isinstance(value, str)
    for key, value in observed.items()
):
    raise SystemExit("image labels are not a string mapping")
normalized = {key: observed.get(key) for key in required}
if normalized != required:
    raise SystemExit("image labels do not contain the fixed role identity")
Path(sys.argv[3]).write_text(
    json.dumps(normalized, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
}

run_cpu_smoke() {
  local tag="$1"
  local observed_version="$2"
  local adapter_module="$3"
  local -a run_args=(
    run --rm --network none --read-only --user 2000:0
    --cap-drop=ALL --security-opt=no-new-privileges
    --tmpfs /tmp:rw,noexec,nosuid,size=64m
    --tmpfs /home/vllm:rw,noexec,nosuid,size=64m
  )

  [[ "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$tag")" == "linux/amd64" ]]
  docker "${run_args[@]}" --entrypoint /opt/inferdrome-runtime/bin/python "$tag" \
    -c "from inferdrome import __version__; assert __version__ == '$observed_version'"
  [[ "$(docker "${run_args[@]}" --entrypoint /opt/inferdrome-runtime/bin/inferdrome "$tag" --version)" == "$observed_version" ]]
  docker "${run_args[@]}" --entrypoint /opt/inferdrome-runtime/bin/python "$tag" \
    -m "$adapter_module" --help >/dev/null
  docker "${run_args[@]}" --entrypoint /opt/inferdrome-runtime/bin/python "$tag" \
    -c "from inferdrome.deployment.gcp_private_engine_adapter import observed_vllm_version; observed_vllm_version()"
}

for role in private-engine cpu-runner-observer; do
  case "$role" in
    private-engine)
      repository="ghcr.io/jayeshsuyal/inferdrome-private-engine"
      adapter_module="inferdrome.deployment.gcp_private_engine_adapter"
      ;;
    cpu-runner-observer)
      repository="ghcr.io/jayeshsuyal/inferdrome-cpu-runner-observer"
      adapter_module="inferdrome.deployment.gcp_private_runner_adapter"
      ;;
    *)
      exit 1
      ;;
  esac
  tag="$repository:publication-${source_commit:0:12}-${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
  plan="$work_root/$role.plan.json"
  full_labels="$work_root/$role.full-labels.json"
  labels="$work_root/$role.labels.json"
  printf '%s\n' "$repository" > "$work_root/$role.repository"
  printf '%s\n' "$tag" > "$work_root/$role.tag"
  python scripts/role_image_publish.py plan \
    --role "$role" \
    --source-commit "$source_commit" \
    --version "$version" \
    --workflow-run-id "$GITHUB_RUN_ID" \
    --output "$plan"
  python scripts/build_runner_image.py \
    --flavor release \
    --platform linux/amd64 \
    --image-kind vllm-benchmark-runner \
    --runtime-role "$role" \
    --tag "$tag"
  docker image inspect --format '{{json .Config.Labels}}' "$tag" > "$full_labels"
  normalize_labels "$plan" "$full_labels" "$labels"
  python scripts/role_image_publish.py verify-local-inspection \
    --plan "$plan" \
    --labels-json "$labels" \
    --platform "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$tag")" \
    --user "$(docker image inspect --format '{{.Config.User}}' "$tag")"
  run_cpu_smoke "$tag" "$version" "$adapter_module"
done
