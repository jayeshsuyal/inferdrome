"""Cross-field invariants omitted from JSON Schema remain normative."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from inferdrome.domain.controlled_comparison import (
    ControlledComparisonPlan,
    ControlledComparisonResult,
)

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "conformance" / "valid"
)


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def test_plan_rejects_a_run_assigned_to_both_arms() -> None:
    payload = _fixture("controlled-comparison-plan.json")
    candidate_arm = copy.deepcopy(payload["candidate_arm"])
    assert isinstance(candidate_arm, dict)
    candidate_runs = candidate_arm["run_ids"]
    assert isinstance(candidate_runs, list)
    candidate_runs[0] = "run-11111111111111111111111111111111"
    payload["candidate_arm"] = candidate_arm

    with pytest.raises(ValidationError, match="arms must be disjoint"):
        ControlledComparisonPlan.model_validate_json(json.dumps(payload))


def test_plan_rejects_nonmatching_exitspec_contract_identities() -> None:
    payload = _fixture("controlled-comparison-plan.json")
    baseline_arm = payload["baseline_arm"]
    candidate_arm = payload["candidate_arm"]
    assert isinstance(baseline_arm, dict)
    assert isinstance(candidate_arm, dict)
    baseline_spec = baseline_arm["resolved_experiment"]
    candidate_spec = candidate_arm["resolved_experiment"]
    assert isinstance(baseline_spec, dict)
    assert isinstance(candidate_spec, dict)
    baseline_spec["links"] = {"exitspec_contract_digest": f"sha256:{'a' * 64}"}
    candidate_spec["links"] = {"exitspec_contract_digest": f"sha256:{'b' * 64}"}

    with pytest.raises(ValidationError, match="differ only at the treatment path"):
        ControlledComparisonPlan.model_validate_json(json.dumps(payload))


def test_plan_rejects_an_extra_cross_arm_control_change() -> None:
    payload = _fixture("controlled-comparison-plan.json")
    candidate_arm = copy.deepcopy(payload["candidate_arm"])
    assert isinstance(candidate_arm, dict)
    candidate_spec = candidate_arm["resolved_experiment"]
    assert isinstance(candidate_spec, dict)
    candidate_workload = candidate_spec["workload"]
    assert isinstance(candidate_workload, dict)
    candidate_workload["seed"] = 43
    payload["candidate_arm"] = candidate_arm

    with pytest.raises(
        ValidationError,
        match=r"fingerprint pin is incorrect|differ only at the treatment path",
    ):
        ControlledComparisonPlan.model_validate_json(json.dumps(payload))


def test_plan_rejects_an_unrecognized_metric_definition_digest() -> None:
    payload = _fixture("controlled-comparison-plan.json")
    payload["metric_definitions_digest"] = f"sha256:{'0' * 64}"

    with pytest.raises(ValidationError, match="metric-definition digest"):
        ControlledComparisonPlan.model_validate_json(json.dumps(payload))


def test_incomparable_result_cannot_expose_an_estimate() -> None:
    payload = _fixture("controlled-comparison-result.json")
    payload["status"] = "INCOMPARABLE"

    with pytest.raises(ValidationError, match="must omit point estimates"):
        ControlledComparisonResult.model_validate_json(json.dumps(payload))


def test_result_rejects_negative_zero() -> None:
    payload = _fixture("controlled-comparison-result.json")
    outcomes = payload["outcomes"]
    assert isinstance(outcomes, list)
    outcome = outcomes[0]
    assert isinstance(outcome, dict)
    outcome["estimate"] = "-0.000000"

    with pytest.raises(ValidationError, match="negative zero"):
        ControlledComparisonResult.model_validate_json(json.dumps(payload))
