#!/usr/bin/env bash
set -Eeuo pipefail

repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
inferdrome_python=${INFERDROME_PYTHON:-python3}
export PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}"

"$inferdrome_python" scripts/generate_deployment_qualification.py --check
PYTHONPYCACHEPREFIX=${PYTHONPYCACHEPREFIX:-${TMPDIR:-/tmp}/inferdrome-qualification-pycache} \
  "$inferdrome_python" -m py_compile scripts/run_deployment_qualification.py
"$inferdrome_python" -m pytest -p no:cacheprovider tests/unit/test_deployment_qualification.py

docker_bin=$(command -v docker 2>/dev/null || true)
if [[ -z "$docker_bin" ]] || ! "$docker_bin" compose version --short >/dev/null 2>&1; then
  if [[ "${CI:-}" == "true" ]]; then
    printf '%s\n' 'Docker Compose qualification is required in CI but is unavailable.' >&2
    exit 1
  fi
  printf '%s\n' 'Docker Compose qualification unavailable locally; unit gate passed.'
  exit 0
fi

output_root=${INFERDROME_QUALIFICATION_OUTPUT_ROOT:-"$repository_root/.inferdrome-qualification"}
[[ "$output_root" = /* ]] || {
  printf '%s\n' 'qualification output root must be absolute' >&2
  exit 2
}
if [[ -e "$output_root" && ( ! -d "$output_root" || -L "$output_root" ) ]]; then
  printf '%s\n' 'qualification output root must be a real directory' >&2
  exit 2
fi
mkdir -p -- "$output_root"

"$inferdrome_python" scripts/run_deployment_qualification.py \
  --confirm-synthetic-compose \
  --output-root "$output_root"

set +e
"$inferdrome_python" scripts/run_deployment_qualification.py \
  --confirm-synthetic-compose \
  --output-root "$output_root" \
  --expect-output-sha256 "sha256:0000000000000000000000000000000000000000000000000000000000000000"
induced_status=$?
set -e
if [[ "$induced_status" -eq 0 ]]; then
  printf '%s\n' 'induced qualification failure unexpectedly succeeded' >&2
  exit 1
fi
printf '%s\n' 'Docker Compose qualification: success and induced-failure cleanup passed.'
