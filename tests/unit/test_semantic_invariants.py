"""Semantic invariants that extend structural JSON Schema checks."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from inferdrome.domain.environment import EnvironmentManifest
from inferdrome.domain.evidence import EvidenceBundle
from inferdrome.domain.execution import ExecutionRecord
from inferdrome.domain.experiment import ExperimentSpec
from inferdrome.domain.metrics import Measurements, MetricDefinitions
from inferdrome.domain.request_plan import RequestPlan
from inferdrome.domain.request_record import RequestRecord

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "conformance" / "valid"
)


def payload(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def assert_rejected(model: type[Any], value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(value))


def test_secret_bearing_endpoint_is_rejected() -> None:
    value = payload("experiment.json")
    value["target"]["endpoint"] = "http://127.0.0.1:8000?api_key=secret"
    assert_rejected(ExperimentSpec, value)


def test_request_plan_requires_contiguous_derived_identity() -> None:
    value = payload("request-plan.json")
    value["requests"][0]["sequence_index"] = 1
    assert_rejected(RequestPlan, value)


def test_inline_prompt_digest_is_verified() -> None:
    value = payload("request-plan.json")
    value["requests"][0]["prompt"]["text"] = "Changed prompt"
    assert_rejected(RequestPlan, value)


def test_success_requires_observed_ttft() -> None:
    value = payload("request-record.json")
    value["timing"]["ttft_ns"] = None
    value["timing"]["itl_ns"] = []
    assert_rejected(RequestRecord, value)


def test_environment_completeness_is_derived_from_provenance() -> None:
    value = payload("environment.json")
    value["completeness"] = "COMPLETE"
    assert_rejected(EnvironmentManifest, value)


def test_execution_phases_cannot_overlap() -> None:
    value = payload("execution.json")
    value["phases"][1]["started_at"] = "2026-08-05T22:12:09Z"
    assert_rejected(ExecutionRecord, value)


def test_metric_definition_order_is_frozen() -> None:
    value = payload("metric-definitions.json")
    value["definitions"][0], value["definitions"][1] = (
        value["definitions"][1],
        value["definitions"][0],
    )
    assert_rejected(MetricDefinitions, value)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("definitions", 4, "unit"), "count"),
        (("definitions", 4, "population"), "all_measured_requests"),
        (("definitions", 4, "allowed_aggregations"), ["mean"]),
        (
            ("definitions", 4, "required_observations"),
            ["request.timing.itl_ns"],
        ),
        (("definitions", 4, "quantile_method"), None),
        (("definitions", 4, "rounding_policy"), "none"),
    ],
)
def test_metric_definition_semantics_are_frozen(
    path: tuple[str | int, ...], replacement: object
) -> None:
    value = payload("metric-definitions.json")
    target: Any = value
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    assert_rejected(MetricDefinitions, value)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("definition_id", "measured_request_count_v1"),
        ("unit", "count"),
        ("population", "all_measured_requests"),
        ("aggregation", "count"),
        ("value", "10000000"),
        ("quantile_method", None),
        ("rounding_policy", "none"),
    ],
)
def test_measurement_semantics_are_frozen(field: str, replacement: object) -> None:
    value = payload("measurements.json")
    value["measurements"][4][field] = replacement
    assert_rejected(Measurements, value)


def test_count_measurement_matches_population_size() -> None:
    value = payload("measurements.json")
    value["measurements"][2]["sample_count"] = 1
    assert_rejected(Measurements, value)


def test_error_rate_cannot_exceed_one() -> None:
    value = payload("measurements.json")
    value["measurements"][3]["value"] = "1.000001"
    assert_rejected(Measurements, value)


def test_vllm_bundle_cannot_be_marked_synthetic() -> None:
    value = payload("evidence-bundle.json")
    value["evidence_eligibility"] = "SYNTHETIC_ONLY"
    assert_rejected(EvidenceBundle, value)


def test_artifact_media_type_must_match_role() -> None:
    value = payload("evidence-bundle.json")
    value["artifacts"][0]["media_type"] = "text/plain"
    assert_rejected(EvidenceBundle, value)


def test_native_response_content_must_be_classified() -> None:
    value = payload("evidence-bundle.json")
    native_result = next(
        artifact
        for artifact in value["artifacts"]
        if artifact["role"] == "native_result"
    )
    native_result["sensitivity"] = "PUBLIC"
    assert_rejected(EvidenceBundle, value)


def test_models_are_frozen() -> None:
    model = RequestRecord.model_validate_json(
        json.dumps(payload("request-record.json"))
    )
    with pytest.raises(ValidationError):
        model.sequence_index = 1


def test_fixture_mutation_does_not_modify_source_payload() -> None:
    original = payload("request-record.json")
    mutated = copy.deepcopy(original)
    mutated["outcome"]["status"] = "FAILED"
    assert original["outcome"]["status"] == "SUCCESS"
