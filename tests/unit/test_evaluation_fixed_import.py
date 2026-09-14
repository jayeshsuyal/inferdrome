"""Public offline import guards for failed fixed-assignment background offers."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

import pytest

from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.study_config import CompiledTrial, compile_study
from inferdrome.evaluation.study_validation import (
    StudyValidationError,
    validate_trial_result,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes
from tests.unit.test_evaluation_study_config import load, study_payload
from tests.unit.test_evaluation_study_report import measure


@pytest.fixture(scope="module")
def fixed_background() -> tuple[CompiledTrial, dict[str, Any]]:
    trial = compile_study(load(study_payload("STALE_LOAD"))).trials[0]
    return trial, measure(trial)


def _undispatched_first_background(original: dict[str, Any]) -> dict[str, Any]:
    raw = deepcopy(original)
    population = raw["background"]
    row = population["records"][0]
    assert row["outcome"] == "CANCELLED" and row["dispatch_ns"] is not None
    row.update(
        attempts=0,
        dispatch_ns=None,
        dispatch_lag_ns=None,
        response_headers_ns=None,
        first_body_byte_ns=None,
        first_content_ns=None,
        protocol_done_ns=None,
        http_status=None,
        finish_reason=None,
        content_event_times_ns=[],
        prompt_tokens=None,
        completion_tokens=None,
        usage_provenance="UNAVAILABLE",
    )
    population["dispatched_count"] = sum(
        record["attempts"] for record in population["records"]
    )
    raw["recovery"]["first_background_dispatch_ns"] = min(
        record["dispatch_ns"]
        for record in population["records"]
        if record["dispatch_ns"] is not None
    )
    # The second real request remains active across restoration, so the other
    # recovery fields and the trial's foreground are unchanged.
    return raw


def test_fixed_import_preserves_real_controller_output(
    fixed_background: tuple[CompiledTrial, dict[str, Any]],
) -> None:
    trial, raw = fixed_background
    result = validate_trial_result(raw, trial.config)
    assert result.background is not None
    assert result.background.routing == "FIXED_ASSIGNMENT"
    assert canonical_json_bytes(result.background.to_dict()) == canonical_json_bytes(
        raw["background"]
    )


@pytest.mark.parametrize("selected", [False, True])
def test_fixed_import_preserves_unselected_and_correctly_selected_cancellation(
    fixed_background: tuple[CompiledTrial, dict[str, Any]], selected: bool
) -> None:
    trial, original = fixed_background
    assert isinstance(trial.config, RoutingFaultConfig)
    raw = _undispatched_first_background(original)
    endpoint = trial.config.background.offers[0].endpoint_id if selected else None
    raw["background"]["records"][0]["endpoint_id"] = endpoint
    result = validate_trial_result(raw, trial.config)
    assert result.background is not None
    record = result.background.records[0]
    assert record.outcome == "CANCELLED"
    assert record.attempts == 0 and record.dispatch_ns is None
    assert record.endpoint_id == endpoint


def test_fixed_import_rejects_wrong_selection_without_a_dispatch(
    fixed_background: tuple[CompiledTrial, dict[str, Any]],
) -> None:
    trial, original = fixed_background
    raw = _undispatched_first_background(original)
    validate_trial_result(raw, trial.config)
    row = raw["background"]["records"][0]
    row["endpoint_id"] = (
        "endpoint-b" if row["endpoint_id"] == "endpoint-a" else "endpoint-a"
    )
    with pytest.raises(StudyValidationError):
        validate_trial_result(raw, trial.config)


def test_fixed_import_rejects_route_rejection_but_allows_capacity_rejection(
    fixed_background: tuple[CompiledTrial, dict[str, Any]],
) -> None:
    trial, original = fixed_background
    raw = _undispatched_first_background(original)
    population = raw["background"]
    row = population["records"][0]
    row.update(
        endpoint_id=None,
        outcome="REJECTED_CAPACITY",
        terminal_ns=row["arrival_observed_ns"],
    )
    population["outcomes"] = dict(
        Counter(record["outcome"] for record in population["records"])
    )
    validate_trial_result(raw, trial.config)
    row["outcome"] = "REJECTED_ROUTE"
    population["outcomes"] = dict(
        Counter(record["outcome"] for record in population["records"])
    )
    with pytest.raises(StudyValidationError):
        validate_trial_result(raw, trial.config)
