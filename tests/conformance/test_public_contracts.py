"""Cross-validator conformance for every committed public v1 schema."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from inferdrome.schema_registry import SCHEMA_MODELS, public_schema

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = REPOSITORY_ROOT / "schemas" / "public" / "v1"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "conformance"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def apply_mutation(payload: Any, mutation: dict[str, Any]) -> Any:
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


CASES = load_json(FIXTURE_ROOT / "cases.json")


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_json_schema_and_pydantic_agree(case: dict[str, Any]) -> None:
    payload = load_json(FIXTURE_ROOT / case["fixture"])
    if mutation := case.get("mutation"):
        payload = apply_mutation(payload, mutation)

    schema = load_json(SCHEMA_ROOT / case["schema"])
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    json_schema_valid = not list(validator.iter_errors(payload))

    model = SCHEMA_MODELS[case["schema"]]
    try:
        model.model_validate_json(json.dumps(payload, allow_nan=False))
    except ValidationError:
        pydantic_valid = False
    else:
        pydantic_valid = True

    assert json_schema_valid is case["valid"]
    assert pydantic_valid is case["valid"]


def test_committed_schemas_are_valid_and_current() -> None:
    for filename, model in SCHEMA_MODELS.items():
        committed = load_json(SCHEMA_ROOT / filename)
        Draft202012Validator.check_schema(committed)
        assert committed == public_schema(filename, model)


def test_every_object_shape_fails_closed() -> None:
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    for filename in SCHEMA_MODELS:
        walk(load_json(SCHEMA_ROOT / filename))


def test_request_record_does_not_promise_unavailable_observations() -> None:
    schema_text = (SCHEMA_ROOT / "request-record.schema.json").read_text(
        encoding="utf-8"
    )
    unavailable_names = {
        "attempt_id",
        "completed_offset_ns",
        "e2e_latency_ns",
        "finish_reason",
        "first_nonempty_content_ttft_ns",
        "http_status",
        "scheduled_offset_ns",
        "tpot_ns",
    }
    for unavailable_name in unavailable_names:
        assert f'"{unavailable_name}"' not in schema_text
