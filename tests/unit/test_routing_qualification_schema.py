"""Generated schema checks for the additive qualification-only contract."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from inferdrome.routing_qualification.contracts import StaleTelemetryQualification
from inferdrome.routing_qualification.schema import (
    check_schemas,
    schema_bytes,
    schema_root,
)

_DIGEST = f"sha256:{'a' * 64}"


def _trial(
    policy_id: str,
    trial_id: str,
    terminal_population: dict[str, int],
) -> dict[str, object]:
    return {
        "policy_id": policy_id,
        "repetition_index": 0,
        "trial_id": trial_id,
        "request_denominator": 6,
        "reset_receipt_sha256": _DIGEST,
        "state_observations_sha256": _DIGEST,
        "state_observation_count": 36,
        "route_decisions_sha256": _DIGEST,
        "route_decision_count": 6,
        "terminal_outcomes_sha256": _DIGEST,
        "terminal_outcome_count": 6,
        "terminal_population": {"values": terminal_population},
    }


def _valid_descriptor() -> dict[str, object]:
    return {
        "schema_version": "inferdrome.stale-telemetry-qualification.v1",
        "qualification_id": "stale-telemetry-qualification-v1",
        "source_campaign_id": "routing-campaign-v1",
        "source_execution_mode": "SYNTHETIC_CPU_ONLY",
        "source_package_retained_digest": _DIGEST,
        "source_inputs": {
            "campaign_plan_sha256": _DIGEST,
            "request_trace_sha256": _DIGEST,
            "fault_schedule_sha256": _DIGEST,
            "trial_plan_sha256": _DIGEST,
        },
        "fault_timeline": {
            "load_observer_pause_at_ms": 15,
            "health_continues": True,
            "first_stale_load_fresh_health_decision_time_ms": 20,
            "health_age_ms": 0,
            "load_age_ms": 10,
            "freshness_bound_ms": 5,
        },
        "repetitions_per_mode": 1,
        "population_accounting": "SEPARATE_PER_TRIAL_NO_POOLING",
        "trials": [
            _trial(
                "fail_closed_required_load_v1",
                "trial-fail-closed-v1",
                {
                    "SUCCEEDED": 2,
                    "TIMED_OUT": 0,
                    "FAILED": 0,
                    "CANCELLED": 0,
                    "NO_SAFE_ROUTE": 4,
                },
            ),
            _trial(
                "explicit_fail_open_stale_load_v1",
                "trial-fail-open-v1",
                {
                    "SUCCEEDED": 2,
                    "TIMED_OUT": 4,
                    "FAILED": 0,
                    "CANCELLED": 0,
                    "NO_SAFE_ROUTE": 0,
                },
            ),
            _trial(
                "typed_admissible_state_only_v1",
                "trial-typed-v1",
                {
                    "SUCCEEDED": 6,
                    "TIMED_OUT": 0,
                    "FAILED": 0,
                    "CANCELLED": 0,
                    "NO_SAFE_ROUTE": 0,
                },
            ),
        ],
    }


def _validator() -> Draft202012Validator:
    schema = json.loads(
        schema_bytes()["stale-telemetry-qualification.schema.json"]
    )
    return Draft202012Validator(schema)


def test_routing_qualification_schema_snapshot_is_current_and_closed() -> None:
    check_schemas()
    root = schema_root()
    assert {path.name for path in root.glob("*.json")} == set(schema_bytes())
    assert all(
        (root / filename).read_bytes() == content
        for filename, content in schema_bytes().items()
    )


def test_generated_schema_closes_terminal_statuses_and_ordered_trials() -> None:
    validator = _validator()
    descriptor = _valid_descriptor()

    assert not tuple(validator.iter_errors(descriptor))
    schema = json.loads(
        schema_bytes()["stale-telemetry-qualification.schema.json"]
    )
    values_schema = schema["$defs"]["TerminalPopulation"]["properties"][
        "values"
    ]
    assert values_schema["required"] == [
        "SUCCEEDED",
        "TIMED_OUT",
        "FAILED",
        "CANCELLED",
        "NO_SAFE_ROUTE",
    ]
    assert values_schema["additionalProperties"] is False
    assert "STRUCTURAL-ONLY LIMIT" in values_schema["$comment"]

    missing_terminal = deepcopy(descriptor)
    del missing_terminal["trials"][0]["terminal_population"]["values"][
        "NO_SAFE_ROUTE"
    ]
    assert tuple(validator.iter_errors(missing_terminal))

    extra_terminal = deepcopy(descriptor)
    extra_terminal["trials"][0]["terminal_population"]["values"]["UNKNOWN"] = 0
    assert tuple(validator.iter_errors(extra_terminal))

    out_of_range_terminal = deepcopy(descriptor)
    out_of_range_terminal["trials"][0]["terminal_population"]["values"][
        "SUCCEEDED"
    ] = 7
    assert tuple(validator.iter_errors(out_of_range_terminal))

    wrong_pair = deepcopy(descriptor)
    wrong_pair["trials"][0]["trial_id"] = "trial-fail-open-v1"
    assert tuple(validator.iter_errors(wrong_pair))

    swapped_pairs = deepcopy(descriptor)
    swapped_pairs["trials"][0], swapped_pairs["trials"][1] = (
        swapped_pairs["trials"][1],
        swapped_pairs["trials"][0],
    )
    assert tuple(validator.iter_errors(swapped_pairs))


def test_terminal_population_arithmetic_remains_explicit_runtime_verification() -> None:
    descriptor = _valid_descriptor()
    descriptor["trials"][0]["terminal_population"]["values"]["SUCCEEDED"] = 3

    assert not tuple(_validator().iter_errors(descriptor))
    with pytest.raises(ValidationError, match="does not close the six-request trial"):
        StaleTelemetryQualification.model_validate_json(json.dumps(descriptor))
