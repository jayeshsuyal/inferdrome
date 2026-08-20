"""Conformance tests for the standalone managed real-GPU capability profile."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from inferdrome.capability_profiles import (
    MANAGED_VLLM_GPU_PROFILE_ID,
    canonical_document_sha256,
    profile_documents,
)
from inferdrome.gpu_proof import LocalGpuProof

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE_ROOT = REPOSITORY_ROOT / "profiles" / "v1"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "profiles" / "v1"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _apply_mutation(payload: Any, mutation: dict[str, Any]) -> Any:
    mutated = copy.deepcopy(payload)
    parts = [part for part in mutation["path"].split("/") if part]
    parent = mutated
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    final = parts[-1]
    key: int | str = int(final) if isinstance(parent, list) else final
    operation = mutation["operation"]
    if operation in {"add", "replace"}:
        parent[key] = mutation["value"]
    elif operation == "remove":
        del parent[key]
    else:
        raise AssertionError(f"unsupported fixture mutation: {operation}")
    return mutated


LOCAL_SCHEMA = _load_json(PROFILE_ROOT / "local-gpu-proof.schema.json")
LOCAL_VALIDATOR = Draft202012Validator(
    LOCAL_SCHEMA,
    format_checker=FormatChecker(),
)
PROFILE = _load_json(
    PROFILE_ROOT / "managed-vllm-0.26-evidence-profile.json"
)
CASES = _load_json(FIXTURE_ROOT / "cases.json")


def _local_schema_valid(payload: Any) -> bool:
    return not list(LOCAL_VALIDATOR.iter_errors(payload))


def _local_profile_valid(payload: Any) -> bool:
    try:
        LocalGpuProof.model_validate_json(json.dumps(payload, allow_nan=False))
    except (TypeError, ValueError, ValidationError):
        return False
    return True


def _invocation_schema_valid(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    contract = PROFILE["producer_invocation"]
    if set(payload) != set(contract["required_root_fields"]):
        return False
    if payload.get("schema_version") != contract["schema_version"]:
        return False
    argv = payload.get("argv")
    limits = contract["argv_contract"]
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > limits["max_arguments"]
        or any(
            not isinstance(argument, str)
            or not argument
            or len(argument) > limits["max_argument_characters"]
            for argument in argv
        )
    ):
        return False
    metadata = payload.get("metadata")
    if (
        not isinstance(metadata, dict)
        or set(metadata) != set(contract["metadata"]["required_fields"])
        or any(not isinstance(value, str) for value in metadata.values())
    ):
        return False
    preflight = payload.get("endpoint_preflight")
    if not isinstance(preflight, dict) or set(preflight) != {
        "response_base64",
        "result",
    }:
        return False
    result = preflight.get("result")
    if not isinstance(result, dict) or set(result) != {
        "api",
        "response_sha256",
        "schema_version",
        "server_reported_models",
        "status",
        "target_model",
    }:
        return False
    encoded = preflight.get("response_base64")
    try:
        if not isinstance(encoded, str):
            return False
        response = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return False
    if len(response) > contract["endpoint_preflight"]["response_limit_bytes"]:
        return False
    return _local_schema_valid(payload.get("local_gpu_proof"))


def _one_option(argv: list[str], option: str) -> str:
    positions = [index for index, value in enumerate(argv) if value == option]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise ValueError("option missing or duplicated")
    return argv[positions[0] + 1]


def _expected_server_argv(
    proof: LocalGpuProof,
    *,
    model: str,
    port: int,
    seed: str,
) -> list[str]:
    return [
        proof.producer_distribution.executable_path,
        "serve",
        proof.model_snapshot.root,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--served-model-name",
        model,
        "--tokenizer",
        proof.tokenizer_snapshot.root,
        "--tokenizer-mode",
        "auto",
        "--dtype",
        "auto",
        "--seed",
        seed,
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
        str(proof.selected_gpu_indices[0]),
        "--no-enable-log-requests",
        "--disable-uvicorn-access-log",
        "--uvicorn-log-level",
        "warning",
    ]


def _invocation_profile_valid(payload: Any) -> bool:
    if not _invocation_schema_valid(payload):
        return False
    assert isinstance(payload, dict)
    try:
        proof = LocalGpuProof.model_validate_json(
            json.dumps(payload["local_gpu_proof"], allow_nan=False)
        )
        argv = payload["argv"]
        metadata = payload["metadata"]
        preflight = payload["endpoint_preflight"]
        result = preflight["result"]
        response = base64.b64decode(preflight["response_base64"], validate=True)
        endpoint = urlsplit(proof.server.endpoint)
        if endpoint.port is None:
            return False
        seed = _one_option(argv, "--seed")
    except (ValidationError, ValueError, binascii.Error):
        return False

    if (
        metadata["inferdrome_adapter_version"] != "1.0.0"
        or metadata["inferdrome_producer_version"] != "0.26.0"
        or metadata["inferdrome_run_id"] != proof.run_id
        or not Draft202012Validator(
            {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
        ).is_valid(metadata["inferdrome_execution_fingerprint"])
        or not Draft202012Validator(
            {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
        ).is_valid(metadata["inferdrome_workload_sha256"])
        or argv[0] != proof.producer_distribution.executable_path
        or argv[:3] != [argv[0], "bench", "serve"]
        or _one_option(argv, "--base-url") != proof.server.endpoint
        or _one_option(argv, "--tokenizer") != proof.tokenizer_snapshot.root
        or _one_option(argv, "--request-id-prefix") != f"{proof.run_id}-"
        or _one_option(argv, "--model") != result["target_model"]
        or result["api"] != "openai_chat_completions"
        or result["schema_version"] != "inferdrome.endpoint-preflight.v1"
        or result["status"] != 200
        or result["target_model"] not in result["server_reported_models"]
        or result["response_sha256"]
        != "sha256:" + hashlib.sha256(response).hexdigest()
        or list(proof.server.argv)
        != _expected_server_argv(
            proof,
            model=result["target_model"],
            port=endpoint.port,
            seed=seed,
        )
    ):
        return False

    marker = argv.index("--metadata")
    expected_metadata_tail = [f"{key}={value}" for key, value in metadata.items()]
    return argv[marker + 1 :] == expected_metadata_tail


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_profile_conformance_vectors(case: dict[str, Any]) -> None:
    payload = _load_json(FIXTURE_ROOT / case["fixture"])
    if mutation := case.get("mutation"):
        payload = _apply_mutation(payload, mutation)

    if case["kind"] == "local_gpu_proof":
        schema_valid = _local_schema_valid(payload)
        profile_valid = _local_profile_valid(payload)
    elif case["kind"] == "producer_invocation":
        schema_valid = _invocation_schema_valid(payload)
        profile_valid = _invocation_profile_valid(payload)
    else:
        raise AssertionError(f"unknown conformance kind: {case['kind']}")

    assert schema_valid is case["schema_valid"]
    assert profile_valid is case["profile_valid"]


def test_committed_profile_documents_are_valid_and_current() -> None:
    expected = profile_documents()
    for relative, value in expected.items():
        assert _load_json(REPOSITORY_ROOT / relative) == value

    Draft202012Validator.check_schema(LOCAL_SCHEMA)
    assert PROFILE["profile_id"] == MANAGED_VLLM_GPU_PROFILE_ID
    assert canonical_document_sha256(PROFILE).startswith("sha256:")


def test_local_gpu_schema_closes_every_object_shape() -> None:
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(LOCAL_SCHEMA)


def test_profile_does_not_invent_repository_commit_inside_local_proof() -> None:
    local_schema_text = json.dumps(LOCAL_SCHEMA, sort_keys=True)
    assert "repository_commit" not in local_schema_text
    binding = next(
        item
        for item in PROFILE["cross_artifact_bindings"]
        if item["id"] == "producer-commit-outer-binding"
    )
    assert "not a local_gpu_proof field" in binding["rule"]
