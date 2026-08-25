#!/usr/bin/env bash
set -euo pipefail

inferdrome_python=${INFERDROME_PYTHON:-python3}
repository_root=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}"

"$inferdrome_python" scripts/generate_schemas.py --check
"$inferdrome_python" scripts/generate_deployment_spec.py --check
"$inferdrome_python" scripts/generate_deployment_receipt.py --check
"$inferdrome_python" scripts/generate_gcp_dry_run_plan.py --check
"$inferdrome_python" scripts/generate_gcp_execution_contracts.py --check
"$inferdrome_python" scripts/generate_kubernetes_contract.py --check
"$inferdrome_python" scripts/runner_smoke.py --check
"$inferdrome_python" scripts/build_runner_image.py --check
"$inferdrome_python" scripts/generate_capability_profiles.py --check
"$inferdrome_python" scripts/generate_gpu_campaign.py --check
"$inferdrome_python" scripts/generate_qwen3_launch_profile.py --check
"$inferdrome_python" scripts/generate_fake_golden.py --check
"$inferdrome_python" scripts/generate_vllm_golden.py --check
"$inferdrome_python" scripts/run_real_gpu_demo.py --check
"$inferdrome_python" scripts/capture_real_gpu_over_ssh.py --check
"$inferdrome_python" scripts/watch_lambda_a100_capacity.py --check
"$inferdrome_python" scripts/watch_lambda_gpu_capacity.py \
  --gpu-tier a100-40gb-pcie \
  --check
"$inferdrome_python" scripts/watch_lambda_gpu_capacity.py \
  --gpu-tier a100-40gb-sxm4 \
  --check
"$inferdrome_python" scripts/watch_lambda_gpu_capacity.py \
  --gpu-tier h100-80gb-pcie \
  --check
"$inferdrome_python" scripts/review_qwen3_gpu_evidence_publication.py --check-records
"$inferdrome_python" scripts/review_qwen3_a100_sxm4_evidence.py --check-records
qwen3_evidence_archive="$repository_root/gpu-proof-retrieved/20260821T203940Z-058482df4737-68efd4f4/capture.tar.gz"
if [[ -f "$qwen3_evidence_archive" ]]; then
  "$inferdrome_python" scripts/review_qwen3_gpu_evidence_publication.py --check
fi
a100_evidence_archive="${INFERDROME_A100_EVIDENCE_ARCHIVE:-$repository_root/gpu-proof-retrieved/20260823T192609Z-a02bfd7c3f8b-dcb7a227/capture.tar.gz}"
a100_capture_record="${INFERDROME_A100_CAPTURE_RECORD:-$(dirname "$a100_evidence_archive")}"
if [[ -f "$a100_evidence_archive" ]]; then
  "$inferdrome_python" scripts/review_qwen3_a100_sxm4_evidence.py \
    "$a100_evidence_archive" \
    --capture-record "$a100_capture_record" \
    --check
fi
bash -n scripts/prepare_real_gpu_host.sh
bash -n scripts/run_real_gpu_capture.sh
bash -n scripts/dashboard_gate.sh
bash -n scripts/run_kubernetes_mock_e2e.sh
"$inferdrome_python" -m py_compile scripts/real_gpu_capture.py
"$inferdrome_python" -m py_compile scripts/generate_deployment_spec.py
"$inferdrome_python" -m py_compile scripts/generate_deployment_receipt.py
"$inferdrome_python" -m py_compile scripts/generate_kubernetes_contract.py
"$inferdrome_python" -m py_compile src/inferdrome/kubernetes.py
"$inferdrome_python" -m py_compile scripts/gcp_dry_run_plan.py
"$inferdrome_python" -m py_compile scripts/generate_gcp_dry_run_plan.py
"$inferdrome_python" -m py_compile scripts/generate_gcp_execution_contracts.py
"$inferdrome_python" -m py_compile scripts/gcp_guarded_lifecycle.py
"$inferdrome_python" -m py_compile scripts/runner_smoke.py
"$inferdrome_python" -m py_compile scripts/build_runner_image.py
"$inferdrome_python" -m py_compile scripts/qwen3_gpu_capture.py
"$inferdrome_python" -m py_compile scripts/generate_capability_profiles.py
"$inferdrome_python" -m py_compile scripts/generate_gpu_campaign.py
"$inferdrome_python" -m py_compile scripts/generate_qwen3_launch_profile.py
"$inferdrome_python" -m py_compile scripts/review_gpu_evidence_publication.py
"$inferdrome_python" -m py_compile scripts/review_qwen3_gpu_evidence_publication.py
"$inferdrome_python" -m py_compile scripts/review_qwen3_a100_sxm4_evidence.py
"$inferdrome_python" -m py_compile scripts/capture_real_gpu_over_ssh.py
"$inferdrome_python" -m py_compile scripts/lambda_gpu_guard.py
"$inferdrome_python" -m py_compile scripts/watch_lambda_a100_capacity.py
"$inferdrome_python" -m py_compile scripts/watch_lambda_gpu_capacity.py
"$inferdrome_python" -m py_compile scripts/materialize_real_gpu_receipt.py
"$inferdrome_python" -m py_compile scripts/run_local_demo.py
"$inferdrome_python" -m py_compile scripts/run_qwen3_evidence_dashboard.py
"$inferdrome_python" -m py_compile scripts/run_qwen3_a100_sxm4_evidence_dashboard.py
"$inferdrome_python" -m py_compile scripts/verify_dashboard_install.py
"$inferdrome_python" -m py_compile scripts/verify_qwen3_workload_tokenization.py
"$inferdrome_python" -m ruff check .
"$inferdrome_python" -m mypy
"$inferdrome_python" -m pytest
