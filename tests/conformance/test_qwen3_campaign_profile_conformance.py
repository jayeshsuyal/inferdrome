"""Cross-validator conformance for the operational Qwen3 campaign profile."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from inferdrome.qwen3_campaign import (
    Qwen3ManagedProfile,
    qwen3_profile_document,
    qwen3_profile_schema,
)

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
    if mutation["operation"] in {"add", "replace"}:
        parent[key] = mutation["value"]
    elif mutation["operation"] == "remove":
        del parent[key]
    else:
        raise AssertionError("unsupported profile mutation")
    return mutated


SCHEMA = _load_json(CAMPAIGN_ROOT / "profiles" / "qwen3-8b-profile.schema.json")
PROFILE = _load_json(
    CAMPAIGN_ROOT / "profiles" / "managed-vllm-0.26-qwen3-8b-bf16-v1.json"
)
CASES = _load_json(FIXTURE_ROOT / "qwen3-profile-cases.json")
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_qwen3_profile_conformance_vectors(case: dict[str, Any]) -> None:
    payload = copy.deepcopy(PROFILE)
    if mutation := case.get("mutation"):
        payload = _apply_mutation(payload, mutation)

    schema_valid = not list(VALIDATOR.iter_errors(payload))
    try:
        Qwen3ManagedProfile.model_validate_json(
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        )
    except ValidationError:
        profile_valid = False
    else:
        profile_valid = True

    assert schema_valid is case["schema_valid"]
    assert profile_valid is case["profile_valid"]


def test_qwen3_profile_schema_and_document_are_current_and_closed() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    assert qwen3_profile_schema() == SCHEMA
    assert qwen3_profile_document() == PROFILE

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
