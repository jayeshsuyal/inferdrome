#!/usr/bin/env bash
set -euo pipefail

inferdrome_python=${INFERDROME_PYTHON:-python3}
repository_root=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}"

"$inferdrome_python" scripts/generate_schemas.py --check
"$inferdrome_python" scripts/generate_capability_profiles.py --check
"$inferdrome_python" scripts/generate_gpu_campaign.py --check
"$inferdrome_python" scripts/generate_qwen3_launch_profile.py --check
"$inferdrome_python" scripts/generate_fake_golden.py --check
"$inferdrome_python" scripts/generate_vllm_golden.py --check
"$inferdrome_python" scripts/run_real_gpu_demo.py --check
"$inferdrome_python" scripts/capture_real_gpu_over_ssh.py --check
bash -n scripts/prepare_real_gpu_host.sh
bash -n scripts/run_real_gpu_capture.sh
bash -n scripts/dashboard_gate.sh
"$inferdrome_python" -m py_compile scripts/real_gpu_capture.py
"$inferdrome_python" -m py_compile scripts/qwen3_gpu_capture.py
"$inferdrome_python" -m py_compile scripts/generate_capability_profiles.py
"$inferdrome_python" -m py_compile scripts/generate_gpu_campaign.py
"$inferdrome_python" -m py_compile scripts/generate_qwen3_launch_profile.py
"$inferdrome_python" -m py_compile scripts/review_gpu_evidence_publication.py
"$inferdrome_python" -m py_compile scripts/capture_real_gpu_over_ssh.py
"$inferdrome_python" -m py_compile scripts/lambda_gpu_guard.py
"$inferdrome_python" -m py_compile scripts/materialize_real_gpu_receipt.py
"$inferdrome_python" -m py_compile scripts/run_local_demo.py
"$inferdrome_python" -m py_compile scripts/verify_dashboard_install.py
"$inferdrome_python" -m py_compile scripts/verify_qwen3_workload_tokenization.py
"$inferdrome_python" -m ruff check .
"$inferdrome_python" -m mypy
"$inferdrome_python" -m pytest
