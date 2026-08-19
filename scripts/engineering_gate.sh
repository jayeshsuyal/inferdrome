#!/usr/bin/env bash
set -euo pipefail

inferdrome_python=${INFERDROME_PYTHON:-python3}
repository_root=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}"

"$inferdrome_python" scripts/generate_schemas.py --check
"$inferdrome_python" scripts/generate_fake_golden.py --check
"$inferdrome_python" scripts/generate_vllm_golden.py --check
"$inferdrome_python" scripts/run_real_gpu_demo.py --check
"$inferdrome_python" scripts/capture_real_gpu_over_ssh.py --check
bash -n scripts/prepare_real_gpu_host.sh
bash -n scripts/run_real_gpu_capture.sh
bash -n scripts/dashboard_gate.sh
"$inferdrome_python" -m py_compile scripts/real_gpu_capture.py
"$inferdrome_python" -m py_compile scripts/capture_real_gpu_over_ssh.py
"$inferdrome_python" -m py_compile scripts/lambda_gpu_guard.py
"$inferdrome_python" -m py_compile scripts/materialize_real_gpu_receipt.py
"$inferdrome_python" -m py_compile scripts/run_local_demo.py
"$inferdrome_python" -m py_compile scripts/verify_dashboard_install.py
"$inferdrome_python" -m ruff check .
"$inferdrome_python" -m mypy
"$inferdrome_python" -m pytest
