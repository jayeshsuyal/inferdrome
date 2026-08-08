"""Semantic invariants beyond the generated trial-set JSON Schema."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from inferdrome.domain.trial_set import TrialSet

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    REPOSITORY_ROOT
    / "tests"
    / "fixtures"
    / "conformance"
    / "valid"
    / "trial-set.json"
)


def _fixture() -> dict[str, Any]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _validate(value: dict[str, Any]) -> TrialSet:
    return TrialSet.model_validate_json(json.dumps(value, allow_nan=False))


def test_trial_set_fixture_satisfies_semantic_contract() -> None:
    descriptor = _validate(_fixture())
    assert len(descriptor.members) == 2


@pytest.mark.parametrize("field", ["run_id", "bundle_digest"])
def test_trial_set_rejects_duplicate_member_identity(field: str) -> None:
    value = _fixture()
    value["members"][1][field] = value["members"][0][field]

    with pytest.raises(ValidationError, match="must be unique"):
        _validate(value)


def test_trial_set_rejects_reordered_or_gapped_repetitions() -> None:
    value = _fixture()
    value["members"][1]["repetition_index"] = 2

    with pytest.raises(ValidationError, match="contiguous and ordered"):
        _validate(value)


def test_trial_set_requires_at_least_two_runs() -> None:
    value = _fixture()
    value["members"] = value["members"][:1]

    with pytest.raises(ValidationError):
        _validate(value)
