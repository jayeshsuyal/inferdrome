#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
inferdrome_python=${INFERDROME_PYTHON:-python3}
inferdrome_uv=${INFERDROME_UV:-}

fail() {
  printf 'Dashboard package gate failed: %s\n' "$1" >&2
  exit 1
}

if [[ "$inferdrome_python" == */* ]]; then
  inferdrome_python=$(cd -- "$(dirname -- "$inferdrome_python")" && pwd -P)/$(
    basename -- "$inferdrome_python"
  )
else
  inferdrome_python=$(command -v "$inferdrome_python" 2>/dev/null || true)
fi
[[ -n "$inferdrome_python" && -x "$inferdrome_python" ]] ||
  fail "the locked Python interpreter is unavailable"

if [[ -z "$inferdrome_uv" ]]; then
  sibling_uv=$(dirname -- "$inferdrome_python")/uv
  if [[ -x "$sibling_uv" ]]; then
    inferdrome_uv=$sibling_uv
  else
    inferdrome_uv=$(command -v uv 2>/dev/null || true)
  fi
elif [[ "$inferdrome_uv" == */* ]]; then
  inferdrome_uv=$(cd -- "$(dirname -- "$inferdrome_uv")" && pwd -P)/$(
    basename -- "$inferdrome_uv"
  )
else
  inferdrome_uv=$(command -v "$inferdrome_uv" 2>/dev/null || true)
fi
[[ -n "$inferdrome_uv" && -x "$inferdrome_uv" ]] ||
  fail "the pinned uv executable is unavailable"
uv_reported_version=$("$inferdrome_uv" --version)
case "$uv_reported_version" in
  "uv 0.8.17" | "uv 0.8.17 ("*")")
    ;;
  *)
    fail "uv 0.8.17 is required"
    ;;
esac
[[ -f "$repository_root/pyproject.toml" &&
  ! -L "$repository_root/pyproject.toml" ]] ||
  fail "pyproject.toml is unavailable"
[[ -f "$repository_root/uv.lock" && ! -L "$repository_root/uv.lock" ]] ||
  fail "uv.lock is unavailable"

wheel_root=$(mktemp -d "${TMPDIR:-/tmp}/inferdrome-dashboard-wheel.XXXXXX") ||
  fail "could not create an isolated package workspace"
cleanup() {
  rm -rf -- "$wheel_root"
}
trap cleanup EXIT

uv_cache_root="$wheel_root/uv-cache"
source_dist_root="$wheel_root/source-dist"
wheel_dist_root="$wheel_root/wheel-dist"
"$inferdrome_uv" build \
  --offline \
  --no-python-downloads \
  --cache-dir "$uv_cache_root" \
  --sdist \
  --no-build-isolation \
  --python "$inferdrome_python" \
  --out-dir "$source_dist_root" \
  "$repository_root"
source_archives=("$source_dist_root"/inferdrome-*.tar.gz)
if [[ ${#source_archives[@]} -ne 1 || ! -f "${source_archives[0]}" ]]; then
  fail "expected exactly one Inferdrome source distribution"
fi

"$inferdrome_uv" build \
  --offline \
  --no-python-downloads \
  --cache-dir "$uv_cache_root" \
  --wheel \
  --no-build-isolation \
  --python "$inferdrome_python" \
  --out-dir "$wheel_dist_root" \
  "${source_archives[0]}"
wheel_files=("$wheel_dist_root"/inferdrome-*.whl)
if [[ ${#wheel_files[@]} -ne 1 || ! -f "${wheel_files[0]}" ]]; then
  fail "expected exactly one Inferdrome wheel"
fi

install_root="$wheel_root/install"
"$inferdrome_uv" pip install \
  --offline \
  --no-python-downloads \
  --cache-dir "$uv_cache_root" \
  --no-index \
  --no-deps \
  --no-build \
  --link-mode copy \
  --python "$inferdrome_python" \
  --target "$install_root" \
  "${wheel_files[0]}"
(
  cd -- "$wheel_root"
  PYTHONPATH="$install_root${PYTHONPATH:+:$PYTHONPATH}" \
    "$inferdrome_python" "$repository_root/scripts/verify_dashboard_install.py" \
    --expected-package-root "$install_root"
)
