"""Generated, standalone contracts for the managed real-GPU evidence path."""

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from typing import Any, Final

from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.gpu_proof import (
    MANAGED_PROCESS_ENVIRONMENT_OVERRIDES,
    MANAGED_PROCESS_ENVIRONMENT_POLICY,
    LocalGpuProof,
    expected_vllm_source_wheel,
)
from inferdrome.normalization.vllm_0_26 import (
    VLLM_ADAPTER_VERSION,
    VLLM_VERSION,
    vllm_native_schema_fingerprint,
)

LOCAL_GPU_PROOF_SCHEMA_ID: Final = "urn:inferdrome:local-gpu-proof:v1"
MANAGED_VLLM_GPU_PROFILE_ID: Final = (
    "inferdrome.managed-vllm-0.26-evidence-profile.v1"
)
PRODUCER_INVOCATION_SCHEMA_VERSION: Final = "inferdrome.producer-invocation.v1"

_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_GPU_UUID_PATTERN = r"^GPU-[0-9A-Za-z-]{8,120}$"
_VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$"
_ABSOLUTE_LINUX_PATH_PATTERN = r"^/(?:[^\u0000-\u001f/]*/)*[^\u0000-\u001f/]*$"
_LOOPBACK_ENDPOINT_PATTERN = r"^http://127\.0\.0\.1:[0-9]{1,5}$"


def canonical_document_sha256(value: Any) -> str:
    """Return an ordinary SHA-256 over RFC 8785 canonical JSON bytes."""

    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def local_gpu_proof_schema() -> dict[str, Any]:
    """Render the closed structural schema consumed outside Inferdrome."""

    schema = deepcopy(
        LocalGpuProof.model_json_schema(
            mode="validation",
            ref_template="#/$defs/{model}",
        )
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = LOCAL_GPU_PROOF_SCHEMA_ID
    schema["title"] = "Inferdrome local GPU proof v1"

    properties = schema["properties"]
    definitions = schema["$defs"]
    for name in (
        "nvidia_smi_path",
    ):
        properties[name]["pattern"] = _ABSOLUTE_LINUX_PATH_PATTERN
    for name in (
        "client_python_version",
        "torch_version",
        "cuda_runtime_version",
    ):
        properties[name]["pattern"] = _VERSION_PATTERN

    gpu = definitions["GpuDeviceEvidence"]["properties"]
    gpu["uuid"]["pattern"] = _GPU_UUID_PATTERN
    gpu["driver_version"]["pattern"] = _VERSION_PATTERN

    process = definitions["GpuComputeProcessEvidence"]["properties"]
    process["gpu_uuid"]["pattern"] = _GPU_UUID_PATTERN
    process["pid"]["maximum"] = 2_147_483_647
    process["process_group_id"]["maximum"] = 2_147_483_647

    snapshot = definitions["SnapshotIdentity"]["properties"]
    snapshot["root"]["pattern"] = _ABSOLUTE_LINUX_PATH_PATTERN

    distribution = definitions["VllmDistributionIdentity"]["properties"]
    distribution["executable_path"]["pattern"] = _ABSOLUTE_LINUX_PATH_PATTERN
    distribution["source_wheel_path"]["pattern"] = _ABSOLUTE_LINUX_PATH_PATTERN

    server = definitions["ManagedServerEvidence"]["properties"]
    server["endpoint"]["pattern"] = _LOOPBACK_ENDPOINT_PATTERN
    server["pid"]["maximum"] = 2_147_483_647
    server["process_group_id"]["maximum"] = 2_147_483_647
    return schema


def _constraint(identifier: str, rule: str, *paths: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "paths": list(paths),
        "rule": rule,
    }


def managed_vllm_gpu_evidence_profile() -> dict[str, Any]:
    """Render the independently consumable composite capability profile."""

    x86_wheel = expected_vllm_source_wheel("x86_64")
    arm_wheel = expected_vllm_source_wheel("aarch64")
    return {
        "canonicalization": {
            "document_digest": "sha256_of_rfc8785_canonical_json_bytes",
            "producer_invocation": "rfc8785",
        },
        "claims_boundary": {
            "acceptance_verdict": "NONE",
            "hardware_attestation": False,
            "statement": (
                "The profile supports integrity, provenance, and locally observed "
                "execution evidence. It does not prove trusted execution or assign "
                "PASS, FAIL, or NOT_PROVEN."
            ),
        },
        "cross_artifact_bindings": [
            _constraint(
                "run-id-equality",
                "Every listed run identifier MUST be byte-identical.",
                "invocation.metadata.inferdrome_run_id",
                "invocation.local_gpu_proof.run_id",
                "request_plan.run_id",
                "request_records[*].run_id",
                "measurements.run_id",
                "execution.run_id",
                "bundle.run_id",
            ),
            _constraint(
                "workload-digest-equality",
                "Every listed workload digest MUST be byte-identical.",
                "invocation.metadata.inferdrome_workload_sha256",
                "resolved_experiment.workload.sha256",
                "sha256(exact workload.source.jsonl bytes)",
            ),
            _constraint(
                "execution-fingerprint-equality",
                "Invocation metadata MUST equal the bundle execution fingerprint.",
                "invocation.metadata.inferdrome_execution_fingerprint",
                "bundle.digests.execution_fingerprint",
            ),
            _constraint(
                "source-spec-digest-equality",
                "The request plan and bundle MUST bind the same resolved source spec.",
                "request_plan.source_spec_digest",
                "bundle.digests.source_spec_digest",
            ),
            _constraint(
                "model-tokenizer-revisions",
                "Snapshot revisions MUST equal the exact resolved target revisions.",
                "invocation.local_gpu_proof.model_snapshot.revision",
                "invocation.local_gpu_proof.tokenizer_snapshot.revision",
                "resolved_experiment.target.model_revision",
                "resolved_experiment.target.tokenizer_revision",
            ),
            _constraint(
                "native-result-binding",
                "Native bytes, canonical records, execution, definitions, and "
                "measurements MUST be revalidated through the bundle hash inventory.",
                "bundle.artifacts",
                "integrity/artifact-hashes.json",
            ),
            _constraint(
                "producer-commit-outer-binding",
                "The repository commit is not a local_gpu_proof field. It MUST be "
                "equal across the outer capture manifest, capture receipts, host "
                "preparation receipt, and handoff manifest.",
                "capture_manifest.repository_commit",
                "single_receipt.repository_commit",
                "comparison_receipt.repository_commit",
                "host_preparation.repository_commit",
                "handoff_manifest.provenance.capture_producer_commit",
            ),
        ],
        "local_gpu_proof": {
            "schema_id": LOCAL_GPU_PROOF_SCHEMA_ID,
            "schema_path": "local-gpu-proof.schema.json",
            "semantic_constraints": [
                _constraint(
                    "one-selected-gpu",
                    "Exactly one ordered, unique index MUST be selected and it MUST "
                    "equal the sole parsed inventory index.",
                    "selected_gpu_indices",
                    "gpus[*].index",
                ),
                _constraint(
                    "cuda-index-visible",
                    "torch_cuda_device_count MUST be greater than the selected index.",
                    "torch_cuda_device_count",
                    "selected_gpu_indices[0]",
                ),
                _constraint(
                    "inventory-replay",
                    "gpu_query_stdout MUST parse as strict four-column CSV and equal "
                    "gpus exactly, including index, model, UUID, and driver.",
                    "gpu_query_argv",
                    "gpu_query_stdout",
                    "gpus",
                ),
                _constraint(
                    "compute-process-replay",
                    "compute_query_stdout MUST parse as strict two-column CSV "
                    "and equal "
                    "server.gpu_processes by PID and GPU UUID.",
                    "server.compute_query_argv",
                    "server.compute_query_stdout",
                    "server.gpu_processes",
                ),
                _constraint(
                    "gpu-process-coverage",
                    "Observed compute-process GPU UUIDs MUST equal selected inventory "
                    "GPU UUIDs; every process MUST belong to the server process group.",
                    "gpus[*].uuid",
                    "server.gpu_processes[*].gpu_uuid",
                    "server.gpu_processes[*].process_group_id",
                    "server.process_group_id",
                ),
                _constraint(
                    "server-process-group",
                    "server.pid MUST equal server.process_group_id and process "
                    "identities "
                    "MUST be unique.",
                    "server.pid",
                    "server.process_group_id",
                    "server.gpu_processes",
                ),
                _constraint(
                    "capture-order",
                    "started_at <= ready_at <= captured_at.",
                    "server.started_at",
                    "server.ready_at",
                    "captured_at",
                ),
                _constraint(
                    "snapshot-kinds",
                    "model_snapshot.kind MUST be model and tokenizer_snapshot.kind "
                    "MUST "
                    "be tokenizer.",
                    "model_snapshot.kind",
                    "tokenizer_snapshot.kind",
                ),
                _constraint(
                    "source-wheel-pin",
                    "The architecture MUST select exactly the declared wheel filename "
                    "and exact-byte SHA-256.",
                    "client_arch",
                    "producer_distribution.source_wheel_filename",
                    "producer_distribution.source_wheel_sha256",
                ),
                _constraint(
                    "server-invocation-replay",
                    "server.argv MUST equal server_argv_template after substitution "
                    "from "
                    "the resolved experiment and local proof; no shell is involved.",
                    "server.argv",
                    "server_argv_template",
                ),
            ],
        },
        "native_ttft": {
            "aggregation": "p95",
            "definition_id": "vllm_first_choices_event_v0_26",
            "metric": "ttft_ns",
            "population": "successful_measured_requests_with_observed_ttft",
            "quantile_method": "nearest_rank_v1",
            "required_observation": "request.timing.ttft_ns",
            "source_semantics": "first non-empty choices event reported by vLLM 0.26.0",
            "unit": "ns",
        },
        "nvidia_queries": {
            "compute_processes": {
                "argv_template": [
                    "{nvidia_smi_path}",
                    "--query-compute-apps=pid,gpu_uuid",
                    "--format=csv,noheader,nounits",
                    "--id={selected_gpu_index}",
                ],
                "csv_columns": ["pid", "gpu_uuid"],
                "empty_rows_ignored": True,
            },
            "inventory": {
                "argv_template": [
                    "{nvidia_smi_path}",
                    "--query-gpu=index,name,uuid,driver_version",
                    "--format=csv,noheader,nounits",
                    "--id={selected_gpu_index}",
                ],
                "csv_columns": ["index", "name", "uuid", "driver_version"],
                "empty_rows_ignored": True,
            },
        },
        "producer": {
            "adapter_name": "vllm_bench_serve",
            "adapter_version": VLLM_ADAPTER_VERSION,
            "native_schema_fingerprint": vllm_native_schema_fingerprint(),
            "producer_name": "vllm",
            "producer_version": VLLM_VERSION,
        },
        "producer_invocation": {
            "additional_root_fields": False,
            "argv_contract": {
                "execution_form": "ordered_argv_without_shell",
                "max_arguments": 2_048,
                "max_argument_characters": 8_192,
                "profile": "vllm_bench_serve_0_26_exact_replay_v1",
                "traffic_variants": [
                    "bounded_concurrent_traffic_v1",
                    "bounded_request_rate_traffic_v1",
                ],
            },
            "canonical_json": "rfc8785",
            "endpoint_preflight": {
                "additional_fields": False,
                "fields": ["response_base64", "result"],
                "method": "GET",
                "redirects": "forbidden",
                "response_limit_bytes": 1_048_576,
                "result_constraints": {
                    "api": "openai_chat_completions",
                    "schema_version": "inferdrome.endpoint-preflight.v1",
                    "status": 200,
                    "target_model_must_be_listed": True,
                },
                "url_suffix": "/v1/models",
            },
            "metadata": {
                "additional_fields": False,
                "required_fields": [
                    "inferdrome_adapter_version",
                    "inferdrome_execution_fingerprint",
                    "inferdrome_producer_version",
                    "inferdrome_run_id",
                    "inferdrome_workload_sha256",
                ],
            },
            "required_root_fields": [
                "argv",
                "endpoint_preflight",
                "local_gpu_proof",
                "metadata",
                "schema_version",
            ],
            "schema_version": PRODUCER_INVOCATION_SCHEMA_VERSION,
        },
        "profile_id": MANAGED_VLLM_GPU_PROFILE_ID,
        "schema_version": MANAGED_VLLM_GPU_PROFILE_ID,
        "server_argv_template": [
            "{producer_distribution.executable_path}",
            "serve",
            "{model_snapshot.root}",
            "--host",
            "127.0.0.1",
            "--port",
            "{resolved_target_port}",
            "--served-model-name",
            "{resolved_target_model}",
            "--tokenizer",
            "{tokenizer_snapshot.root}",
            "--tokenizer-mode",
            "auto",
            "--dtype",
            "auto",
            "--seed",
            "{resolved_workload_seed}",
            "--load-format",
            "safetensors",
            "--generation-config",
            "vllm",
            "--model-impl",
            "vllm",
            "--max-model-len",
            "1024",
            "--gpu-memory-utilization",
            "0.80",
            "--tensor-parallel-size",
            "1",
            "--device-ids",
            "{selected_gpu_index}",
            "--no-enable-log-requests",
            "--disable-uvicorn-access-log",
            "--uvicorn-log-level",
            "warning",
        ],
        "server_boundary": {
            "endpoint": "http://127.0.0.1:{port}",
            "environment_overrides": list(MANAGED_PROCESS_ENVIRONMENT_OVERRIDES),
            "environment_policy": MANAGED_PROCESS_ENVIRONMENT_POLICY,
            "inherited_vllm_environment": "removed",
            "network_scope": "loopback_only",
            "one_selected_gpu": True,
        },
        "snapshot_and_distribution_policies": {
            "executable": {
                "path": "safe_absolute_regular_file",
                "sha256": "exact_bytes",
                "must_equal_server_and_benchmark_argv_0": True,
            },
            "installed_distribution": {
                "hash_policy": "installed-wheel-files-v1",
                "max_files": 100_000,
                "max_total_bytes": 17_179_869_184,
                "name": "vllm",
                "version": VLLM_VERSION,
            },
            "snapshots": {
                "hash_policy": "regular-files-excluding-dot-cache-v1",
                "max_files": 100_000,
                "max_total_bytes": 137_438_953_472,
                "stable_no_symlink_walk": True,
            },
            "source_wheels": {
                "aarch64": {"filename": arm_wheel[0], "sha256": arm_wheel[1]},
                "x86_64": {"filename": x86_wheel[0], "sha256": x86_wheel[1]},
            },
        },
    }


def valid_local_gpu_proof_fixture() -> dict[str, Any]:
    """Return a non-capture conformance vector for the standalone schema."""

    revision = "0123456789abcdef0123456789abcdef01234567"
    run_id = "run-0123456789abcdef0123456789abcdef"
    gpu_uuid = "GPU-01234567-89ab-cdef-0123-456789abcdef"
    executable = "/opt/inferdrome/venv/bin/vllm"
    snapshot = f"/opt/inferdrome/models/Qwen2.5-0.5B-Instruct-{revision}"
    endpoint = "http://127.0.0.1:18080"
    source_wheel = expected_vllm_source_wheel("x86_64")
    return {
        "capture_mode": "managed_local_vllm",
        "captured_at": "2026-08-20T00:02:00Z",
        "client_arch": "x86_64",
        "client_os": "Linux",
        "client_python_version": "3.12.3",
        "cuda_runtime_version": "13.0",
        "gpu_query_argv": [
            "/usr/bin/nvidia-smi",
            "--query-gpu=index,name,uuid,driver_version",
            "--format=csv,noheader,nounits",
            "--id=0",
        ],
        "gpu_query_stdout": f"0, NVIDIA A10, {gpu_uuid}, 580.105.08\n",
        "gpus": [
            {
                "driver_version": "580.105.08",
                "index": 0,
                "model": "NVIDIA A10",
                "uuid": gpu_uuid,
            }
        ],
        "model_snapshot": {
            "file_count": 10,
            "hash_policy": "regular-files-excluding-dot-cache-v1",
            "kind": "model",
            "revision": revision,
            "root": snapshot,
            "sha256": f"sha256:{'1' * 64}",
            "total_bytes": 999_604_126,
        },
        "nvidia_smi_path": "/usr/bin/nvidia-smi",
        "nvidia_smi_sha256": f"sha256:{'2' * 64}",
        "producer_distribution": {
            "executable_path": executable,
            "executable_sha256": f"sha256:{'3' * 64}",
            "file_count": 6_863,
            "hash_policy": "installed-wheel-files-v1",
            "name": "vllm",
            "sha256": f"sha256:{'4' * 64}",
            "source_wheel_filename": source_wheel[0],
            "source_wheel_path": f"/opt/inferdrome/downloads/{source_wheel[0]}",
            "source_wheel_sha256": source_wheel[1],
            "total_bytes": 767_641_627,
            "version": "0.26.0",
        },
        "run_id": run_id,
        "schema_version": "inferdrome.local-gpu-proof.v1",
        "selected_gpu_indices": [0],
        "server": {
            "argv": [
                executable,
                "serve",
                snapshot,
                "--host",
                "127.0.0.1",
                "--port",
                "18080",
                "--served-model-name",
                "Qwen/Qwen2.5-0.5B-Instruct",
                "--tokenizer",
                snapshot,
                "--tokenizer-mode",
                "auto",
                "--dtype",
                "auto",
                "--seed",
                "42",
                "--load-format",
                "safetensors",
                "--generation-config",
                "vllm",
                "--model-impl",
                "vllm",
                "--max-model-len",
                "1024",
                "--gpu-memory-utilization",
                "0.80",
                "--tensor-parallel-size",
                "1",
                "--device-ids",
                "0",
                "--no-enable-log-requests",
                "--disable-uvicorn-access-log",
                "--uvicorn-log-level",
                "warning",
            ],
            "compute_query_argv": [
                "/usr/bin/nvidia-smi",
                "--query-compute-apps=pid,gpu_uuid",
                "--format=csv,noheader,nounits",
                "--id=0",
            ],
            "compute_query_stdout": f"1210, {gpu_uuid}\n",
            "endpoint": endpoint,
            "environment_overrides": list(MANAGED_PROCESS_ENVIRONMENT_OVERRIDES),
            "environment_policy": MANAGED_PROCESS_ENVIRONMENT_POLICY,
            "gpu_processes": [
                {
                    "gpu_uuid": gpu_uuid,
                    "pid": 1210,
                    "process_group_id": 1200,
                }
            ],
            "pid": 1200,
            "process_group_id": 1200,
            "ready_at": "2026-08-20T00:01:00Z",
            "started_at": "2026-08-20T00:00:00Z",
        },
        "tokenizer_snapshot": {
            "file_count": 10,
            "hash_policy": "regular-files-excluding-dot-cache-v1",
            "kind": "tokenizer",
            "revision": revision,
            "root": snapshot,
            "sha256": f"sha256:{'1' * 64}",
            "total_bytes": 999_604_126,
        },
        "torch_cuda_device_count": 1,
        "torch_version": "2.11.0+cu130",
    }


def valid_managed_vllm_invocation_fixture() -> dict[str, Any]:
    """Return a closed invocation vector embedding the valid GPU proof."""

    proof = valid_local_gpu_proof_fixture()
    run_id = str(proof["run_id"])
    execution_fingerprint = f"sha256:{'5' * 64}"
    workload_sha256 = f"sha256:{'6' * 64}"
    metadata = {
        "inferdrome_adapter_version": "1.0.0",
        "inferdrome_execution_fingerprint": execution_fingerprint,
        "inferdrome_producer_version": "0.26.0",
        "inferdrome_run_id": run_id,
        "inferdrome_workload_sha256": workload_sha256,
    }
    response = json.dumps(
        {
            "data": [{"id": "Qwen/Qwen2.5-0.5B-Instruct"}],
            "object": "list",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    argv = [
        proof["producer_distribution"]["executable_path"],
        "bench",
        "serve",
        "--backend",
        "openai-chat",
        "--base-url",
        proof["server"]["endpoint"],
        "--endpoint",
        "/v1/chat/completions",
        "--model",
        "Qwen/Qwen2.5-0.5B-Instruct",
        "--tokenizer",
        proof["tokenizer_snapshot"]["root"],
        "--dataset-name",
        "custom",
        "--dataset-path",
        "/opt/inferdrome/runs/example/inputs/workload.source.jsonl",
        "--custom-output-len",
        "32",
        "--num-prompts",
        "100",
        "--disable-shuffle",
        "--skip-chat-template",
        "--request-rate",
        "inf",
        "--burstiness",
        "1",
        "--max-concurrency",
        "4",
        "--num-warmups",
        "10",
        "--ready-check-timeout-sec",
        "5",
        "--temperature",
        "0",
        "--seed",
        "42",
        "--request-id-prefix",
        f"{run_id}-",
        "--percentile-metrics",
        "e2el",
        "--metric-percentiles",
        "50,95,99",
        "--save-result",
        "--save-detailed",
        "--result-dir",
        "/opt/inferdrome/runs/example/native-capture",
        "--result-filename",
        "benchmark-result.json",
        "--metadata",
        *(f"{key}={value}" for key, value in metadata.items()),
    ]
    return {
        "argv": argv,
        "endpoint_preflight": {
            "response_base64": base64.b64encode(response).decode("ascii"),
            "result": {
                "api": "openai_chat_completions",
                "response_sha256": "sha256:" + hashlib.sha256(response).hexdigest(),
                "schema_version": "inferdrome.endpoint-preflight.v1",
                "server_reported_models": ["Qwen/Qwen2.5-0.5B-Instruct"],
                "status": 200,
                "target_model": "Qwen/Qwen2.5-0.5B-Instruct",
            },
        },
        "local_gpu_proof": proof,
        "metadata": metadata,
        "schema_version": PRODUCER_INVOCATION_SCHEMA_VERSION,
    }


def conformance_cases() -> list[dict[str, Any]]:
    """Return positive and mutation vectors for independent implementations."""

    return [
        {
            "fixture": "valid/local-gpu-proof.json",
            "kind": "local_gpu_proof",
            "name": "valid-local-gpu-proof",
            "profile_valid": True,
            "schema_valid": True,
        },
        {
            "fixture": "valid/local-gpu-proof.json",
            "kind": "local_gpu_proof",
            "mutation": {
                "operation": "add",
                "path": "/unexpected",
                "value": True,
            },
            "name": "local-proof-rejects-unknown-root-field",
            "profile_valid": False,
            "schema_valid": False,
        },
        {
            "fixture": "valid/local-gpu-proof.json",
            "kind": "local_gpu_proof",
            "mutation": {
                "operation": "replace",
                "path": "/gpus/0/uuid",
                "value": "not-a-gpu-uuid",
            },
            "name": "local-proof-rejects-malformed-gpu-uuid",
            "profile_valid": False,
            "schema_valid": False,
        },
        {
            "fixture": "valid/local-gpu-proof.json",
            "kind": "local_gpu_proof",
            "mutation": {
                "operation": "replace",
                "path": "/selected_gpu_indices/0",
                "value": 1,
            },
            "name": "local-proof-rejects-selected-index-drift",
            "profile_valid": False,
            "schema_valid": True,
        },
        {
            "fixture": "valid/local-gpu-proof.json",
            "kind": "local_gpu_proof",
            "mutation": {
                "operation": "replace",
                "path": "/producer_distribution/source_wheel_sha256",
                "value": f"sha256:{'0' * 64}",
            },
            "name": "local-proof-rejects-wheel-pin-drift",
            "profile_valid": False,
            "schema_valid": True,
        },
        {
            "fixture": "valid/local-gpu-proof.json",
            "kind": "local_gpu_proof",
            "mutation": {
                "operation": "replace",
                "path": "/server/compute_query_stdout",
                "value": (
                    "1211, GPU-01234567-89ab-cdef-0123-456789abcdef\n"
                ),
            },
            "name": "local-proof-rejects-process-query-drift",
            "profile_valid": False,
            "schema_valid": True,
        },
        {
            "fixture": "valid/managed-vllm-invocation.json",
            "kind": "producer_invocation",
            "name": "valid-managed-vllm-invocation",
            "profile_valid": True,
            "schema_valid": True,
        },
        {
            "fixture": "valid/managed-vllm-invocation.json",
            "kind": "producer_invocation",
            "mutation": {
                "operation": "remove",
                "path": "/local_gpu_proof",
            },
            "name": "managed-invocation-requires-local-proof",
            "profile_valid": False,
            "schema_valid": False,
        },
        {
            "fixture": "valid/managed-vllm-invocation.json",
            "kind": "producer_invocation",
            "mutation": {
                "operation": "add",
                "path": "/unexpected",
                "value": True,
            },
            "name": "managed-invocation-rejects-unknown-root-field",
            "profile_valid": False,
            "schema_valid": False,
        },
        {
            "fixture": "valid/managed-vllm-invocation.json",
            "kind": "producer_invocation",
            "mutation": {
                "operation": "replace",
                "path": "/metadata/inferdrome_run_id",
                "value": "run-fedcba9876543210fedcba9876543210",
            },
            "name": "managed-invocation-rejects-run-id-drift",
            "profile_valid": False,
            "schema_valid": True,
        },
        {
            "fixture": "valid/managed-vllm-invocation.json",
            "kind": "producer_invocation",
            "mutation": {
                "operation": "replace",
                "path": "/metadata/inferdrome_producer_version",
                "value": "0.27.0",
            },
            "name": "managed-invocation-rejects-producer-version-drift",
            "profile_valid": False,
            "schema_valid": True,
        },
        {
            "fixture": "valid/managed-vllm-invocation.json",
            "kind": "producer_invocation",
            "mutation": {
                "operation": "replace",
                "path": "/argv/0",
                "value": "/opt/inferdrome/venv/bin/other-vllm",
            },
            "name": "managed-invocation-rejects-executable-drift",
            "profile_valid": False,
            "schema_valid": True,
        },
    ]


def profile_documents() -> dict[str, Any]:
    """Return every generated profile and conformance document."""

    return {
        "profiles/v1/local-gpu-proof.schema.json": local_gpu_proof_schema(),
        "profiles/v1/managed-vllm-0.26-evidence-profile.json": (
            managed_vllm_gpu_evidence_profile()
        ),
        "tests/fixtures/profiles/v1/cases.json": conformance_cases(),
        "tests/fixtures/profiles/v1/valid/local-gpu-proof.json": (
            valid_local_gpu_proof_fixture()
        ),
        "tests/fixtures/profiles/v1/valid/managed-vllm-invocation.json": (
            valid_managed_vllm_invocation_fixture()
        ),
    }
