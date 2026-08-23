"""A100 SXM4 capacity evidence stays distinct from the frozen PCIe plan."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from inferdrome.gpu_campaign import canonical_qwen_gpu_campaign
from inferdrome.qwen3_gpu_capacity_extensions import (
    A100Sxm4CapacityExtensionPack,
    qwen3_a100_sxm4_capacity_extension_cases,
    qwen3_a100_sxm4_capacity_extension_pack,
    qwen3_a100_sxm4_capacity_extension_schema,
    qwen3_capacity_extension_documents,
)

PACK_PATH = (
    "campaigns/v1/execution-packs/"
    "qwen3-8b-a100-sxm4-capacity-extension.json"
)
SCHEMA_PATH = (
    "campaigns/v1/execution-packs/"
    "qwen3-a100-sxm4-capacity-extension.schema.json"
)
CASES_PATH = (
    "tests/fixtures/campaigns/v1/"
    "qwen3-a100-sxm4-capacity-extension-cases.json"
)


def _generated_json(path: str) -> Any:
    return json.loads(qwen3_capacity_extension_documents()[path])


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
        raise AssertionError(f"unsupported capacity-extension mutation: {operation}")
    return mutated


def test_a100_sxm4_pack_freezes_a_distinct_pre_measurement_extension() -> None:
    payload = qwen3_a100_sxm4_capacity_extension_pack().model_dump(mode="json")

    assert payload["base_campaign_id"] == "qwen-gpu-capability-campaign-v1"
    assert payload["campaign_extension_id"] == (
        "qwen3-8b-a100-sxm4-capacity-extension-2026-08-23"
    )
    assert payload["campaign_relationship"] == (
        "DISTINCT_HARDWARE_TIER_DOES_NOT_REPLACE_A100_PCIE"
    )
    assert payload["extension_frozen_on"] == "2026-08-23"
    assert payload["gpu_target"] == {
        "campaign_gpu_model": "NVIDIA A100",
        "expected_nvidia_smi_name": "NVIDIA A100-SXM4-40GB",
        "gpu_count": 1,
        "gpu_tier_id": "a100-40gb-sxm4",
        "interconnect": "SXM4",
        "provider": "lambda_cloud",
        "provider_instance_type_policy": "api_resolved_exact_gpu_tier_v1",
        "vram_gib": 40,
    }
    assert payload["provider_binding"] == {
        "architecture": "x86_64",
        "exact_instance_type_name": "gpu_1x_a100_sxm4",
        "memory_gib": 200,
        "one_active_instance_required": True,
        "provider_description": "1x A100 (40 GB SXM4)",
        "provider_gpu_description": "A100 (40 GB SXM4)",
        "region_policy": "API_RESOLVED_CAPACITY_REGION",
        "remote_gpu_observation_required": True,
        "storage_gib": 512,
        "vcpus": 30,
    }
    assert payload["cost_boundary"] == {
        "allowed_seconds": 2261,
        "api_rate_match_required": True,
        "hourly_rate_snapshot_usd": "1.99",
        "max_session_cost_usd": "1.25",
        "rate_snapshot_date": "2026-08-23",
        "termination_safety_margin_seconds": 300,
    }
    assert payload["capability_state"] == "LOCALLY_CONFORMANT_RUNTIME_UNPROVEN"
    assert payload["hardware_attestation"] is False
    assert payload["acceptance_verdict"] is None


def test_capacity_extension_does_not_mutate_the_frozen_campaign() -> None:
    campaign = canonical_qwen_gpu_campaign()

    assert tuple(target.gpu_tier_id for target in campaign.gpu_targets) == (
        "a10-24gb-pcie",
        "a100-40gb-pcie",
        "h100-80gb-pcie",
        "b200-180gb-sxm6",
    )


def test_generated_schema_validates_the_capacity_extension() -> None:
    schema = _generated_json(SCHEMA_PATH)
    payload = _generated_json(PACK_PATH)

    Draft202012Validator.check_schema(schema)
    assert schema == qwen3_a100_sxm4_capacity_extension_schema()
    assert payload == qwen3_a100_sxm4_capacity_extension_pack().model_dump(
        mode="json"
    )
    assert not list(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(payload)
    )


@pytest.mark.parametrize(
    "case",
    qwen3_a100_sxm4_capacity_extension_cases(),
    ids=[case["name"] for case in qwen3_a100_sxm4_capacity_extension_cases()],
)
def test_capacity_extension_mutations_match_both_validators(
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
        A100Sxm4CapacityExtensionPack.model_validate_json(
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        )
    except ValidationError:
        pack_valid = False
    else:
        pack_valid = True

    assert _generated_json(CASES_PATH) == (
        qwen3_a100_sxm4_capacity_extension_cases()
    )
    assert schema_valid is case["schema_valid"]
    assert pack_valid is case["pack_valid"]
