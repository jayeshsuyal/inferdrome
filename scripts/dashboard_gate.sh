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
"$repository_root/scripts/dashboard_package_gate.sh"
