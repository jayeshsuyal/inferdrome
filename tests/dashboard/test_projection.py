"""Behavioral contract for bounded, evidence-derived dashboard projections."""

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import inferdrome.dashboard.projection as projection_module
from inferdrome.bundle import recalculate_bundle as authoritative_recalculate
from inferdrome.dashboard.projection import load_run_detail


def _measurement_map(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], Any]:
    return {(row["metric"], row["aggregation"]): row for row in rows}


def _object_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for nested in value.values() for key in _object_keys(nested)
        }
    if isinstance(value, list):
        return {key for nested in value for key in _object_keys(nested)}
    return set()


def test_summary_and_detail_preserve_authoritative_measurements(
    sealed_fake_bundle: Any,
    as_public_json: Callable[[object], Any],
) -> None:
    analysis = authoritative_recalculate(sealed_fake_bundle.sealed.path)

    projected = load_run_detail(sealed_fake_bundle.sealed.path)
    summary = as_public_json(projected.summary)
    detail = as_public_json(projected)
    expected = _measurement_map(
        [
            item.model_dump(mode="json", by_alias=True, exclude_none=False)
            for item in analysis.reduction.measurements.measurements
        ]
    )
    detail_measurements = _measurement_map(detail["measurements"])

    assert summary["run_id"] == analysis.verification.run_id
    assert summary["bundle_digest"] == analysis.verification.bundle_digest
    assert summary["integrity_status"] == "VALID"
    assert summary["headline_metrics"]
    assert set(detail_measurements) == set(expected)
    for key, expected_measurement in expected.items():
        actual = detail_measurements[key]
        assert actual["metric"] == expected_measurement["metric"]
        assert actual["aggregation"] == expected_measurement["aggregation"]
        assert actual["value"] == str(expected_measurement["value"])
        assert actual["unit"] == expected_measurement["unit"]
        assert actual["sample_count"] == expected_measurement["sample_count"]
        assert actual["population"] == expected_measurement["population"]
        assert actual["definition_id"] == expected_measurement["definition_id"]
        assert actual["quantile_method"] == expected_measurement["quantile_method"]
        assert actual["rounding_policy"] == expected_measurement["rounding_policy"]


def test_detail_contains_artifact_context_and_bounded_distributions(
    sealed_fake_bundle: Any,
    as_public_json: Callable[[object], Any],
) -> None:
    analysis = authoritative_recalculate(sealed_fake_bundle.sealed.path)

    detail = as_public_json(load_run_detail(sealed_fake_bundle.sealed.path))

    artifacts = detail["artifacts"]
    assert len(artifacts) == analysis.verification.artifact_count
    assert {artifact["role"] for artifact in artifacts} == {
        artifact.role.value for artifact in analysis.verification.descriptor.artifacts
    }
    assert all(
        {
            "path",
            "role",
            "media_type",
            "sensitivity",
            "size_bytes",
            "content_exposed",
        }
        <= artifact.keys()
        for artifact in artifacts
    )
    assert all(artifact["content_exposed"] is False for artifact in artifacts)

    context = {item["key"]: item["value"] for item in detail["context"]}
    assert context["producer.name"] == "inferdrome_fake"
    assert context["execution.max_runtime_seconds"] == "60"
    assert context["execution.max_measured_requests"] == "2"
    assert context["target.api"] == "synthetic_fixture"
    assert context["target.endpoint_identity"] is None
    assert detail["execution"]["measurement_window_ns"] == (
        sealed_fake_bundle.fake.execution.measurement_window_ns
    )
    assert detail["summary"]["environment_completeness"] == "COMPLETE"
    assert detail["digests"]["execution_fingerprint"] == (
        sealed_fake_bundle.resolution.execution_fingerprint
    )
    assert detail["comparison_contract"]["execution_fingerprint"] == (
        sealed_fake_bundle.resolution.execution_fingerprint
    )

    distributions = {item["metric"]: item for item in detail["distributions"]}
    assert set(distributions) == {"ttft_ns", "last_choices_event_span_ns"}
    for distribution in distributions.values():
        assert distribution["unit"] == "ns"
        assert distribution["sample_count"] > 0
        assert 1 <= len(distribution["bins"]) <= 64
        assert (
            sum(item["count"] for item in distribution["bins"])
            == (distribution["sample_count"])
        )


def test_projections_never_include_native_or_canonical_response_bodies(
    sealed_fake_bundle: Any,
    as_public_json: Callable[[object], Any],
) -> None:
    detail = load_run_detail(sealed_fake_bundle.sealed.path)
    payload = as_public_json(detail)
    serialized = json.dumps(payload, sort_keys=True)

    native_responses = {
        row.response_content
        for row in sealed_fake_bundle.fake.native_result.rows
        if row.response_content
    }
    assert native_responses
    for response in native_responses:
        assert response not in serialized
    keys = _object_keys(payload)
    assert "canonical_response_content" not in keys
    assert "response_content" not in keys


def test_attached_endpoint_is_exposed_only_as_an_identity_digest(
    sealed_vllm_bundle: Any,
    as_public_json: Callable[[object], Any],
) -> None:
    detail = as_public_json(load_run_detail(sealed_vllm_bundle.sealed.path))
    endpoint = str(sealed_vllm_bundle.resolution.resolved_spec.target.endpoint)
    serialized = json.dumps(detail, sort_keys=True)
    context = {item["key"]: item["value"] for item in detail["context"]}

    assert endpoint not in serialized
    assert context["target.endpoint_identity"].startswith("sha256:")
    assert len(context["target.endpoint_identity"]) == len("sha256:") + 64


def test_detail_projection_always_uses_authoritative_recalculation(
    sealed_fake_bundle: Any,
    monkeypatch: Any,
) -> None:
    observed: list[object] = []

    def recalculate_spy(bundle_path: object, **kwargs: object) -> object:
        observed.append(bundle_path)
        return authoritative_recalculate(bundle_path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(projection_module, "recalculate_bundle", recalculate_spy)

    detail = load_run_detail(sealed_fake_bundle.sealed.path)

    assert observed == [sealed_fake_bundle.sealed.path]
    assert detail.verification.verified_by_recalculation is True
