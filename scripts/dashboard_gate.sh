#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "$0")/.." && pwd)
inferdrome_python=${INFERDROME_PYTHON:-python3}
if [[ "$inferdrome_python" == */* ]]; then
    inferdrome_python=$(cd "$(dirname "$inferdrome_python")" && pwd)/$(basename "$inferdrome_python")
else
    inferdrome_python=$(command -v "$inferdrome_python")
fi

if ! command -v npm >/dev/null 2>&1; then
    echo "dashboard gate: npm is required" >&2
    exit 1
fi
if [[ ! -d "$repository_root/frontend/node_modules" ]]; then
    echo "dashboard gate: run npm ci --prefix frontend first" >&2
    exit 1
fi

npm --prefix "$repository_root/frontend" run typecheck
npm --prefix "$repository_root/frontend" run test
npm --prefix "$repository_root/frontend" run test:e2e
if [[ ! -f "$repository_root/src/inferdrome/dashboard/static/index.html" ]]; then
    echo "dashboard gate: packaged frontend entry point was not built" >&2
    exit 1
fi

export PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}"
"$inferdrome_python" -m pytest "$repository_root/tests/dashboard"

wheel_root=$(mktemp -d "${TMPDIR:-/tmp}/inferdrome-dashboard-wheel.XXXXXX")
cleanup() {
    rm -rf -- "$wheel_root"
}
trap cleanup EXIT

source_dist_root="$wheel_root/source-dist"
wheel_dist_root="$wheel_root/wheel-dist"
"$inferdrome_python" -m build \
    --sdist \
    --no-isolation \
    --outdir "$source_dist_root" \
    "$repository_root"
source_archives=("$source_dist_root"/inferdrome-*.tar.gz)
if [[ ${#source_archives[@]} -ne 1 || ! -f "${source_archives[0]}" ]]; then
    echo "dashboard gate: expected exactly one Inferdrome source distribution" >&2
    exit 1
fi
"$inferdrome_python" -m pip wheel \
    --no-deps \
    --no-build-isolation \
    --wheel-dir "$wheel_dist_root" \
    "${source_archives[0]}"
wheel_files=("$wheel_dist_root"/inferdrome-*.whl)
if [[ ${#wheel_files[@]} -ne 1 || ! -f "${wheel_files[0]}" ]]; then
    echo "dashboard gate: expected exactly one Inferdrome wheel" >&2
    exit 1
fi
install_root="$wheel_root/install"
"$inferdrome_python" -m pip install \
    --no-deps \
    --target "$install_root" \
    "${wheel_files[0]}"
(
    cd "$wheel_root"
    PYTHONPATH="$install_root" "$inferdrome_python" \
        "$repository_root/scripts/verify_dashboard_install.py" \
        --expected-package-root "$install_root"
)
