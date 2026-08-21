"""Semantic invariants for the design-only GPU campaign contract."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from inferdrome.gpu_campaign import GpuCampaignPlan, canonical_qwen_gpu_campaign


def _payload() -> dict[str, Any]:
    value = canonical_qwen_gpu_campaign().model_dump(
        mode="json",
        by_alias=True,
        exclude_none=False,
    )
    return copy.deepcopy(value)


def _validate(value: dict[str, Any]) -> GpuCampaignPlan:
    return GpuCampaignPlan.model_validate_json(json.dumps(value, allow_nan=False))


def test_canonical_campaign_separates_control_ladder_and_canary() -> None:
    plan = canonical_qwen_gpu_campaign()

    control_models = {
        assignment.model_key for assignment in plan.hardware_control.assignments
    }
    assert control_models == {"qwen3-8b"}
    assert tuple(
        assignment.model_key for assignment in plan.capability_ladder.assignments
    ) == (
        "qwen3-8b",
        "qwen3-14b",
        "qwen3-32b",
        "qwen3.5-122b-a10b-fp8",
    )
    assert plan.hardware_control.cross_hardware_ratio_publication == (
        "WITHHELD_PENDING_REVIEWED_HARDWARE_CONTRACT"
    )
    assert plan.capability_ladder.cross_profile_speed_ranking == "PROHIBITED"
    assert plan.historical_canary.campaign_execution is False
    assert plan.historical_canary.bundle_digest == (
        "sha256:bae216f2165eb06ae2e0f14d3cd852f8e0ebb381bf1f68c71072769b3c0c1675"
    )


def test_campaign_rejects_reordered_gpu_targets() -> None:
    value = _payload()
    value["gpu_targets"][0], value["gpu_targets"][1] = (
        value["gpu_targets"][1],
        value["gpu_targets"][0],
    )

    with pytest.raises(ValidationError, match="frozen ordered four-tier matrix"):
        _validate(value)


def test_campaign_rejects_a_non_increasing_capacity_ladder() -> None:
    value = _payload()
    value["model_pins"][2]["total_parameters"] = 1

    with pytest.raises(ValidationError, match="parameter counts must increase"):
        _validate(value)


def test_campaign_rejects_a_control_model_change() -> None:
    value = _payload()
    value["hardware_control"]["assignments"][2]["model_key"] = "qwen3-32b"

    with pytest.raises(ValidationError, match="must use only Qwen3-8B"):
        _validate(value)


def test_campaign_rejects_a_cost_cap_not_equal_to_session_caps() -> None:
    value = _payload()
    value["budget"]["sessions"][0]["max_session_cost_usd"] = "0.74"

    with pytest.raises(ValidationError, match="must equal the campaign compute cap"):
        _validate(value)


def test_campaign_rejects_fp8_without_language_model_only_mode() -> None:
    value = _payload()
    value["model_pins"][3]["server_model_mode"] = "TEXT_GENERATION"

    with pytest.raises(ValidationError, match="must disable vision"):
        _validate(value)


def test_campaign_rejects_unknown_authoritative_fields() -> None:
    value = _payload()
    value["winner"] = "B200"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _validate(value)
