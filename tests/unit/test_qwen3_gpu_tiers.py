"""Fail-closed coverage for implemented Qwen3 GPU execution tiers."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from inferdrome.gpu_campaign import canonical_qwen_gpu_campaign
from inferdrome.qwen3_gpu_tiers import (
    QWEN3_A100_GPU_TIER_ID,
    QWEN3_A100_SXM4_GPU_TIER_ID,
    QWEN3_H100_GPU_TIER_ID,
    QWEN3_PHASE_BUDGET_SECONDS,
    A100ExecutionPack,
    H100ExecutionPack,
    qwen3_a100_execution_pack,
    qwen3_a100_execution_pack_conformance_cases,
    qwen3_a100_execution_pack_schema,
    qwen3_gpu_execution_documents,
    qwen3_gpu_tier_policy,
    qwen3_h100_execution_pack,
    qwen3_h100_execution_pack_conformance_cases,
    qwen3_h100_execution_pack_schema,
)

PACK_PATH = "campaigns/v1/execution-packs/qwen3-8b-a100.json"
SCHEMA_PATH = (
    "campaigns/v1/execution-packs/qwen3-a100-execution-pack.schema.json"
)
CASES_PATH = (
    "tests/fixtures/campaigns/v1/qwen3-a100-execution-pack-cases.json"
)
H100_PACK_PATH = "campaigns/v1/execution-packs/qwen3-8b-h100.json"
H100_SCHEMA_PATH = (
    "campaigns/v1/execution-packs/qwen3-h100-execution-pack.schema.json"
)
H100_CASES_PATH = (
    "tests/fixtures/campaigns/v1/qwen3-h100-execution-pack-cases.json"
)


def _generated_json(path: str) -> Any:
    return json.loads(qwen3_gpu_execution_documents()[path])


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
        raise AssertionError(f"unsupported execution-pack mutation: {operation}")
    return mutated


@pytest.mark.parametrize(
    (
        "tier_id",
        "campaign_gpu_model",
        "nvidia_smi_name",
        "vram_gib",
        "hourly_rate",
        "cost_cap",
        "allowed_seconds",
    ),
    [
        (
            "a10-24gb-pcie",
            "NVIDIA A10",
            "NVIDIA A10",
            24,
            Decimal("1.29"),
            Decimal("0.75"),
            2093,
        ),
        (
            "a100-40gb-pcie",
            "NVIDIA A100",
            "NVIDIA A100-PCIE-40GB",
            40,
            Decimal("1.99"),
            Decimal("1.25"),
            2261,
        ),
        (
            "h100-80gb-pcie",
            "NVIDIA H100",
            "NVIDIA H100 PCIe",
            80,
            Decimal("3.29"),
            Decimal("2.25"),
            2462,
        ),
    ],
)
def test_implemented_tier_policies_cross_bind_the_frozen_campaign(
    tier_id: str,
    campaign_gpu_model: str,
    nvidia_smi_name: str,
    vram_gib: int,
    hourly_rate: Decimal,
    cost_cap: Decimal,
    allowed_seconds: int,
) -> None:
    campaign = canonical_qwen_gpu_campaign()
    target = next(item for item in campaign.gpu_targets if item.gpu_tier_id == tier_id)
    budget = next(
        item for item in campaign.budget.sessions if item.gpu_tier_id == tier_id
    )
    policy = qwen3_gpu_tier_policy(tier_id)

    assert target.model_dump(mode="json") == {
        "gpu_count": 1,
        "gpu_model": campaign_gpu_model,
        "gpu_tier_id": tier_id,
        "interconnect": "PCIE",
        "provider": "lambda_cloud",
        "provider_instance_type_policy": "api_resolved_exact_gpu_tier_v1",
        "vram_gib": vram_gib,
    }
    assert budget.model_dump(mode="json") == {
        "gpu_tier_id": tier_id,
        "hourly_rate_snapshot_usd": str(hourly_rate),
        "max_session_cost_usd": str(cost_cap),
    }
    assert policy.public_target() == {
        "campaign_gpu_model": campaign_gpu_model,
        "expected_nvidia_smi_name": nvidia_smi_name,
        "gpu_count": 1,
        "gpu_tier_id": tier_id,
        "interconnect": "PCIE",
        "provider": "lambda_cloud",
        "provider_instance_type_policy": "api_resolved_exact_gpu_tier_v1",
        "vram_gib": vram_gib,
    }
    assert policy.hourly_rate_usd == hourly_rate
    assert policy.max_session_cost_usd == cost_cap
    assert policy.allowed_seconds == allowed_seconds
    assert sum(QWEN3_PHASE_BUDGET_SECONDS.values()) == 2078
    assert sum(QWEN3_PHASE_BUDGET_SECONDS.values()) <= policy.allowed_seconds


def test_a100_sxm4_policy_is_a_distinct_capacity_extension() -> None:
    policy = qwen3_gpu_tier_policy(QWEN3_A100_SXM4_GPU_TIER_ID)

    assert policy.public_target() == {
        "campaign_gpu_model": "NVIDIA A100",
        "expected_nvidia_smi_name": "NVIDIA A100-SXM4-40GB",
        "gpu_count": 1,
        "gpu_tier_id": "a100-40gb-sxm4",
        "interconnect": "SXM4",
        "provider": "lambda_cloud",
        "provider_instance_type_policy": "api_resolved_exact_gpu_tier_v1",
        "vram_gib": 40,
    }
    assert policy.hourly_rate_usd == Decimal("1.99")
    assert policy.max_session_cost_usd == Decimal("1.25")
    assert policy.allowed_seconds == 2261
    assert all(
        target.gpu_tier_id != QWEN3_A100_SXM4_GPU_TIER_ID
        for target in canonical_qwen_gpu_campaign().gpu_targets
    )


def test_a100_execution_pack_freezes_the_unproven_qwen3_8b_control() -> None:
    assert qwen3_a100_execution_pack().model_dump(mode="json") == {
        "acceptance_verdict": None,
        "campaign_id": "qwen-gpu-capability-campaign-v1",
        "capability_state": "LOCALLY_CONFORMANT_RUNTIME_UNPROVEN",
        "cost_boundary": {
            "allowed_seconds": 2261,
            "api_rate_match_required": True,
            "hourly_rate_snapshot_usd": "1.99",
            "max_session_cost_usd": "1.25",
            "rate_snapshot_date": "2026-08-20",
            "termination_safety_margin_seconds": 300,
        },
        "gpu_target": {
            "campaign_gpu_model": "NVIDIA A100",
            "expected_nvidia_smi_name": "NVIDIA A100-PCIE-40GB",
            "gpu_count": 1,
            "gpu_tier_id": QWEN3_A100_GPU_TIER_ID,
            "interconnect": "PCIE",
            "provider": "lambda_cloud",
            "provider_instance_type_policy": "api_resolved_exact_gpu_tier_v1",
            "vram_gib": 40,
        },
        "hardware_attestation": False,
        "launch_authorization": "EXPLICIT_OPERATOR_CONFIRMATION_REQUIRED",
        "model_binding": {
            "model_id": "Qwen/Qwen3-8B",
            "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
            "tokenizer_revision": "b968826d9c46dd6066d109eabc6255188de91218",
        },
        "phase_budget_seconds": {
            "archive_transfer": 180,
            "controller_handoff": 23,
            "metadata_transfer": 30,
            "remote_capture": 1300,
            "remote_kill_grace": 60,
            "remote_preflight": 90,
            "source_upload": 90,
            "ssh_close_grace": 5,
            "termination_confirmation": 300,
        },
        "profile_binding": {
            "profile_id": "managed-vllm-0.26-qwen3-8b-bf16-v1",
            "profile_sha256": (
                "sha256:858382b5ea2e86253f55ed914d11e4ab"
                "7e8b13aa6331e8699fb4d364a9ee9369"
            ),
            "producer_name": "vllm",
            "producer_version": "0.26.0",
        },
        "provider_binding": {
            "exact_instance_type_name": "RUNTIME_ARGUMENT_REQUIRED",
            "one_active_instance_required": True,
            "remote_gpu_observation_required": True,
        },
        "schema_version": "inferdrome.qwen3-a100-execution-pack.v1",
        "track_id": "qwen3-8b-hardware-control",
        "workload_binding": {
            "concurrency": 1,
            "measured_requests": 96,
            "warmup_requests": 12,
            "workload_id": "inferdrome.qwen-text-mixed-length.v1",
            "workload_sha256": (
                "sha256:72db7f3a4e8e70c9fb721fe5544d1d96"
                "aac37ec6baedbce99e05ad423fdb105f"
            ),
        },
    }


def test_generated_schema_validates_the_canonical_a100_pack() -> None:
    schema = _generated_json(SCHEMA_PATH)
    payload = _generated_json(PACK_PATH)

    Draft202012Validator.check_schema(schema)
    assert schema == qwen3_a100_execution_pack_schema()
    assert payload == qwen3_a100_execution_pack().model_dump(mode="json")
    assert not list(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(payload)
    )


@pytest.mark.parametrize(
    "case",
    _generated_json(CASES_PATH),
    ids=[case["name"] for case in _generated_json(CASES_PATH)],
)
def test_generated_conformance_mutations_match_both_validators(
    case: dict[str, Any],
) -> None:
    schema = _generated_json(SCHEMA_PATH)
    payload = _generated_json(PACK_PATH)
    if mutation := case.get("mutation"):
        payload = _apply_mutation(payload, mutation)

    schema_valid = not list(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(payload)
    )
    try:
        A100ExecutionPack.model_validate_json(
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        )
    except ValidationError:
        pack_valid = False
    else:
        pack_valid = True

    assert _generated_json(CASES_PATH) == (
        qwen3_a100_execution_pack_conformance_cases()
    )
    assert schema_valid is case["schema_valid"]
    assert pack_valid is case["pack_valid"]


def test_h100_execution_pack_freezes_the_unproven_qwen3_8b_control() -> None:
    payload = qwen3_h100_execution_pack().model_dump(mode="json")

    assert payload["acceptance_verdict"] is None
    assert payload["capability_state"] == "LOCALLY_CONFORMANT_RUNTIME_UNPROVEN"
    assert payload["cost_boundary"] == {
        "allowed_seconds": 2462,
        "api_rate_match_required": True,
        "hourly_rate_snapshot_usd": "3.29",
        "max_session_cost_usd": "2.25",
        "rate_snapshot_date": "2026-08-20",
        "termination_safety_margin_seconds": 300,
    }
    assert payload["gpu_target"] == {
        "campaign_gpu_model": "NVIDIA H100",
        "expected_nvidia_smi_name": "NVIDIA H100 PCIe",
        "gpu_count": 1,
        "gpu_tier_id": QWEN3_H100_GPU_TIER_ID,
        "interconnect": "PCIE",
        "provider": "lambda_cloud",
        "provider_instance_type_policy": "api_resolved_exact_gpu_tier_v1",
        "vram_gib": 80,
    }
    assert payload["hardware_attestation"] is False
    assert payload["launch_authorization"] == (
        "EXPLICIT_OPERATOR_CONFIRMATION_REQUIRED"
    )
    assert payload["model_binding"]["model_id"] == "Qwen/Qwen3-8B"
    assert payload["profile_binding"]["profile_id"] == (
        "managed-vllm-0.26-qwen3-8b-bf16-v1"
    )
    assert payload["schema_version"] == (
        "inferdrome.qwen3-h100-execution-pack.v1"
    )
    assert payload["track_id"] == "qwen3-8b-hardware-control"
    assert sum(payload["phase_budget_seconds"].values()) == 2078


def test_generated_schema_validates_the_canonical_h100_pack() -> None:
    schema = _generated_json(H100_SCHEMA_PATH)
    payload = _generated_json(H100_PACK_PATH)

    Draft202012Validator.check_schema(schema)
    assert schema == qwen3_h100_execution_pack_schema()
    assert payload == qwen3_h100_execution_pack().model_dump(mode="json")
    assert not list(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(payload)
    )


@pytest.mark.parametrize(
    "case",
    _generated_json(H100_CASES_PATH),
    ids=[case["name"] for case in _generated_json(H100_CASES_PATH)],
)
def test_h100_conformance_mutations_match_both_validators(
    case: dict[str, Any],
) -> None:
    schema = _generated_json(H100_SCHEMA_PATH)
    payload = _generated_json(H100_PACK_PATH)
    if mutation := case.get("mutation"):
        payload = _apply_mutation(payload, mutation)

    schema_valid = not list(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(payload)
    )
    try:
        H100ExecutionPack.model_validate_json(
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        )
    except ValidationError:
        pack_valid = False
    else:
        pack_valid = True

    assert _generated_json(H100_CASES_PATH) == (
        qwen3_h100_execution_pack_conformance_cases()
    )
    assert schema_valid is case["schema_valid"]
    assert pack_valid is case["pack_valid"]


def test_unimplemented_b200_tier_rejects() -> None:
    with pytest.raises(
        ValueError,
        match=r"Qwen3 GPU tier is not implemented: b200-180gb-sxm6",
    ):
        qwen3_gpu_tier_policy("b200-180gb-sxm6")


@pytest.mark.parametrize(
    ("path", "value", "schema_valid"),
    [
        (
            ("profile_binding", "profile_sha256"),
            "sha256:" + "0" * 64,
            False,
        ),
        (
            ("workload_binding", "workload_sha256"),
            "sha256:" + "0" * 64,
            False,
        ),
        (("phase_budget_seconds", "remote_capture"), 1299, False),
    ],
)
def test_a100_pack_rejects_digest_and_phase_budget_drift(
    path: tuple[str, str],
    value: str | int,
    schema_valid: bool,
) -> None:
    payload = copy.deepcopy(_generated_json(PACK_PATH))
    payload[path[0]][path[1]] = value

    with pytest.raises(ValidationError):
        A100ExecutionPack.model_validate_json(
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        )

    validator = Draft202012Validator(
        _generated_json(SCHEMA_PATH),
        format_checker=FormatChecker(),
    )
    assert (not list(validator.iter_errors(payload))) is schema_valid


def test_h100_pack_rejects_runtime_claim_and_cost_drift() -> None:
    payload = copy.deepcopy(_generated_json(H100_PACK_PATH))
    payload["capability_state"] = "RUNTIME_PROVEN"
    payload["cost_boundary"]["max_session_cost_usd"] = "2.26"

    with pytest.raises(ValidationError):
        H100ExecutionPack.model_validate_json(
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        )

    validator = Draft202012Validator(
        _generated_json(H100_SCHEMA_PATH),
        format_checker=FormatChecker(),
    )
    assert list(validator.iter_errors(payload))
