#!/usr/bin/env bash
set -euo pipefail

inferdrome_python=${INFERDROME_PYTHON:-python3}
repository_root=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}"

"$inferdrome_python" scripts/generate_schemas.py --check
"$inferdrome_python" scripts/generate_fake_golden.py --check
"$inferdrome_python" scripts/generate_vllm_golden.py --check
"$inferdrome_python" scripts/run_real_gpu_demo.py --check
bash -n scripts/prepare_real_gpu_host.sh
"$inferdrome_python" -m ruff check .
"$inferdrome_python" -m mypy
"$inferdrome_python" -m pytest
