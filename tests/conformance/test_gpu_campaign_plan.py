"""Cross-validator conformance for the operational GPU campaign plan."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from inferdrome.gpu_campaign import (
    GpuCampaignPlan,
    campaign_documents,
    gpu_campaign_plan_schema,
)
from inferdrome.schema_registry import SCHEMA_MODELS

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN_ROOT = REPOSITORY_ROOT / "campaigns" / "v1"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "campaigns" / "v1"


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


SCHEMA = _load_json(CAMPAIGN_ROOT / "gpu-campaign-plan.schema.json")
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
CASES = _load_json(FIXTURE_ROOT / "cases.json")


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_json_schema_and_pydantic_campaign_conformance(case: dict[str, Any]) -> None:
    payload = _load_json(FIXTURE_ROOT / case["fixture"])
    if mutation := case.get("mutation"):
        payload = _apply_mutation(payload, mutation)

    schema_valid = not list(VALIDATOR.iter_errors(payload))
    try:
        GpuCampaignPlan.model_validate_json(json.dumps(payload, allow_nan=False))
    except ValidationError:
        plan_valid = False
    else:
        plan_valid = True

    assert schema_valid is case["schema_valid"]
    assert plan_valid is case["plan_valid"]


def test_committed_campaign_documents_are_valid_and_current() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    assert gpu_campaign_plan_schema() == SCHEMA
    rendered = campaign_documents()
    for relative, expected in rendered.items():
        assert _load_json(REPOSITORY_ROOT / relative) == expected


def test_campaign_schema_fails_closed_and_is_not_a_public_bundle_schema() -> None:
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(SCHEMA)
    assert "gpu-campaign-plan.schema.json" not in SCHEMA_MODELS


def test_canonical_plan_and_positive_fixture_are_byte_equivalent_values() -> None:
    assert _load_json(
        CAMPAIGN_ROOT / "qwen-gpu-capability-campaign.json"
    ) == _load_json(FIXTURE_ROOT / "valid" / "qwen-gpu-capability-campaign.json")
