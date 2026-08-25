"""Strict deployment v1 contract, canonicalization, and secret-boundary tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from inferdrome.deployment import (
    DeploymentSpec,
    canonical_deployment_spec_bytes,
    deployment_spec_digest,
    deployment_spec_schema,
    parse_deployment_spec_json,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = REPOSITORY_ROOT / "deployments" / "v1" / "examples"
SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "deployment" / "v1" / "deployment-spec.schema.json"
)


def _payload(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLE_ROOT / name).read_text(encoding="utf-8"))


def _spec(payload: dict[str, Any]) -> DeploymentSpec:
    return parse_deployment_spec_json(json.dumps(payload, allow_nan=False))


def _invalid(payload: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        _spec(payload)


@pytest.mark.parametrize(
    "name",
    [
        "local-mock.json",
        "lambda-dry-run-reference.json",
        "gcp-dry-run-reference.json",
    ],
)
def test_privacy_safe_examples_are_valid_and_non_executing(name: str) -> None:
    payload = _payload(name)
    spec = _spec(payload)
    assert spec.schema_version == "inferdrome.deployment.v1"
    assert spec.execution_intent in {"mock_only", "dry_run_reference"}
    if spec.provider.provider_id != "local":
        assert spec.provider.credential_refs
    assert "api_key" not in json.dumps(payload)
    assert "password" not in json.dumps(payload)
    assert "secret_value" not in json.dumps(payload)


def test_committed_schema_is_closed_and_current() -> None:
    committed = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(committed)
    assert committed == deployment_spec_schema()

    def assert_closed(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for child in value.values():
                assert_closed(child)
        elif isinstance(value, list):
            for child in value:
                assert_closed(child)

    assert_closed(committed)


def test_schema_and_pydantic_accept_the_same_positive_examples() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for path in sorted(EXAMPLE_ROOT.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert not list(validator.iter_errors(payload)), path.name
        _spec(payload)


def test_canonical_bytes_and_digest_ignore_mapping_order_and_whitespace() -> None:
    payload = _payload("local-mock.json")
    reordered = {key: payload[key] for key in reversed(tuple(payload))}
    left = _spec(payload)
    right = parse_deployment_spec_json(
        json.dumps(reordered, indent=4, sort_keys=False).encode("utf-8")
    )
    assert canonical_deployment_spec_bytes(left) == canonical_deployment_spec_bytes(
        right
    )
    assert deployment_spec_digest(left) == deployment_spec_digest(right)
    assert deployment_spec_digest(left) == (
        "sha256:3ecbfe13f3a392d29e958aea0700e990774e90e214231b0a8543262b0cbfbc59"
    )


def test_parser_rejects_duplicate_keys_at_top_level_and_nested_boundaries() -> None:
    raw = (EXAMPLE_ROOT / "local-mock.json").read_text(encoding="utf-8")
    top_level_duplicate = raw.replace(
        '"schema_version": "inferdrome.deployment.v1"',
        '"schema_version": "inferdrome.deployment.v2",\n'
        '  "schema_version": "inferdrome.deployment.v1"',
        1,
    )
    nested_duplicate = raw.replace(
        '"region": null,\n    "credential_refs"',
        '"region": null,\n    "region": null,\n    "credential_refs"',
        1,
    )

    for duplicate in (top_level_duplicate, nested_duplicate):
        with pytest.raises(ValueError, match="keys must be unique"):
            parse_deployment_spec_json(duplicate)
        with pytest.raises(ValueError, match="keys must be unique"):
            DeploymentSpec.model_validate_json(duplicate)


def test_parser_differential_has_one_canonical_object_and_digest() -> None:
    raw = (EXAMPLE_ROOT / "local-mock.json").read_bytes()
    first = DeploymentSpec.model_validate_json(raw)
    second = parse_deployment_spec_json(first.model_dump_json())
    assert canonical_deployment_spec_bytes(first) == canonical_deployment_spec_bytes(
        second
    )
    assert deployment_spec_digest(first) == deployment_spec_digest(second)


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",
        "10.255.255.254",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.0.1",
        "192.168.255.254",
    ],
)
def test_private_endpoint_accepts_only_rfc1918_ipv4_literals(host: str) -> None:
    payload = _payload("lambda-dry-run-reference.json")
    payload["runtime"]["endpoint"]["host"] = host
    assert _spec(payload).runtime.endpoint.host == host


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "8.8.8.8",
        "127.0.0.1",
        "100.64.0.1",
        "169.254.169.254",
        "192.0.2.1",
        "198.51.100.1",
        "203.0.113.1",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
    ],
)
def test_private_endpoint_rejects_non_rfc1918_ipv4_literals(host: str) -> None:
    payload = _payload("lambda-dry-run-reference.json")
    payload["runtime"]["endpoint"]["host"] = host
    _invalid(payload)


@pytest.mark.parametrize(
    "field", ["unexpected", "provider.unexpected", "runtime.endpoint.unexpected"]
)
def test_unknown_fields_are_rejected_at_every_contract_boundary(field: str) -> None:
    payload = _payload("local-mock.json")
    parent: dict[str, Any] = payload
    parts = field.split(".")
    for part in parts[:-1]:
        parent = parent[part]
    parent[parts[-1]] = "must-not-be-accepted"
    _invalid(payload)


def test_future_schema_version_is_not_silently_accepted() -> None:
    payload = _payload("local-mock.json")
    payload["schema_version"] = "inferdrome.deployment.v2"
    _invalid(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("artifacts", "runner_image", "digest"), None),
        (("artifacts", "serving_runtime_image", "digest"), None),
        (("runtime", "model_revision"), "main"),
        (("runtime", "tokenizer_revision"), "main"),
        (("runtime", "endpoint", "host"), "8.8.8.8"),
        (("runtime", "endpoint", "host"), "169.254.169.254"),
        (("runtime", "endpoint", "path"), "/v1/../admin"),
        (("topology", "runner_runtime_colocation"), "separate"),
        (("cleanup_policy", "cleanup_on_every_exit"), False),
        (("cost_ceiling", "max_cost_usd"), "0"),
        (("cost_ceiling", "max_cost_usd"), "0.00"),
    ],
)
def test_proof_mode_requires_immutable_safe_and_bounded_configuration(
    path: tuple[str, ...], value: object
) -> None:
    payload = _payload("lambda-dry-run-reference.json")
    parent: dict[str, Any] = payload
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value
    _invalid(payload)


def test_cloud_provider_cannot_be_changed_to_local_execution() -> None:
    payload = _payload("lambda-dry-run-reference.json")
    payload["execution_intent"] = "local_execute"
    _invalid(payload)


def test_local_provider_cannot_carry_cloud_region_or_reference_intent() -> None:
    payload = _payload("local-mock.json")
    payload["provider"]["region"] = "us-east-1"
    _invalid(payload)

    payload = _payload("local-mock.json")
    payload["execution_intent"] = "dry_run_reference"
    _invalid(payload)


def test_mock_mode_is_explicit_and_cannot_declare_gpu_evidence_resources() -> None:
    payload = _payload("local-mock.json")
    payload["resources"]["gpu_count"] = 1
    payload["resources"]["gpu_model"] = "NVIDIA A100-SXM4-40GB"
    _invalid(payload)

    payload = _payload("local-mock.json")
    payload["mode"] = "proof"
    _invalid(payload)


def test_sglang_is_reserved_for_versioned_development_references() -> None:
    payload = _payload("lambda-dry-run-reference.json")
    payload["deployment_id"] = "sglang-reference"
    payload["mode"] = "development"
    payload["runtime"]["engine"] = "sglang"
    payload["runtime"]["engine_version"] = "0.4.0"
    payload["runtime"]["adapter"] = "sglang_reference_v1"
    assert _spec(payload).runtime.engine == "sglang"

    payload["mode"] = "proof"
    _invalid(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("provider", "provider_id"), "aws"),
        (("runtime", "engine"), "other"),
        (("runtime", "engine_version"), "0.27.0"),
        (("resources", "cpu_cores"), True),
        (("resources", "memory_mib"), 0),
        (("timeouts", "total_seconds"), 2_000_000),
        (("cost_ceiling", "max_cost_usd"), "1000000000000.00"),
    ],
)
def test_unsupported_or_unbounded_values_fail_closed(
    path: tuple[str, ...], value: object
) -> None:
    payload = _payload("local-mock.json")
    parent: dict[str, Any] = payload
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value
    _invalid(payload)


def test_secret_values_have_no_serializable_model_boundary() -> None:
    payload = _payload("lambda-dry-run-reference.json")
    secret_value = "do-not-serialize-this-value"
    payload["provider"]["apiKey"] = secret_value
    with pytest.raises(ValueError) as exc_info:
        _spec(payload)
    assert secret_value not in str(exc_info.value)

    payload = _payload("lambda-dry-run-reference.json")
    payload["provider"]["credential_refs"][0]["value"] = secret_value
    with pytest.raises((ValidationError, ValueError)) as exc_info:
        _spec(payload)
    assert secret_value not in str(exc_info.value)

    spec = _spec(_payload("lambda-dry-run-reference.json"))
    serialized = spec.model_dump(mode="json")
    assert serialized["provider"]["credential_refs"] == [
        {"kind": "environment_variable", "name": "INFERDROME_LAMBDA_API_KEY"}
    ]
    assert "value" not in json.dumps(serialized)
    assert "api_key" not in json.dumps(serialized)


@pytest.mark.parametrize(
    ("source", "field", "credential_shape"),
    [
        ("gcp", "secret_id", "sk-" + "A" * 40),
        ("gcp", "secret_id", "ghp_" + "A" * 40),
        ("gcp", "secret_id", "github_pat_" + "A" * 40),
        ("gcp", "secret_id", "AKIA" + "A" * 16),
        ("gcp", "secret_id", "-----BEGIN RSA PRIVATE KEY-----"),
        ("gcp", "secret_id", "a" * 48),
        ("environment", "name", "A" * 48),
        ("environment", "name", "AKIA" + "A" * 16),
    ],
)
def test_structured_secret_reference_rejects_credential_shaped_components(
    source: str,
    field: str,
    credential_shape: str,
) -> None:
    if source == "gcp":
        payload = _payload("gcp-dry-run-reference.json")
        payload["provider"]["credential_refs"][0][field] = credential_shape
    else:
        payload = _payload("local-mock.json")
        payload["provider"]["credential_refs"] = [
            {"kind": "environment_variable", field: credential_shape}
        ]

    raw = json.dumps(payload, allow_nan=False)
    for parser in (parse_deployment_spec_json, DeploymentSpec.model_validate_json):
        with pytest.raises((ValidationError, ValueError)) as exc_info:
            parser(raw)
        assert credential_shape not in str(exc_info.value)


def test_gcp_secret_reference_serializes_only_structured_resource_identity() -> None:
    spec = _spec(_payload("gcp-dry-run-reference.json"))
    refs = spec.model_dump(mode="json")["provider"]["credential_refs"]
    assert refs == [
        {
            "kind": "gcp_secret_manager",
            "project_id": "inferdrome-example",
            "secret_id": "provider-token",
            "version": "1",
        }
    ]
    assert "value" not in json.dumps(refs)
    assert "sk-" not in json.dumps(refs)


def test_methodology_is_a_reference_not_an_embedded_workload() -> None:
    payload = _payload("local-mock.json")
    methodology = payload["topology"]["methodology"]
    assert set(methodology) == {"source", "methodology_id", "manifest_sha256"}
    assert "records" not in payload["topology"]
    assert "prompts" not in payload["topology"]


def test_spec_models_are_immutable_after_validation() -> None:
    spec = _spec(_payload("local-mock.json"))
    with pytest.raises(ValidationError):
        spec.deployment_id = "changed"  # type: ignore[misc]


def test_direct_model_validation_still_rejects_unknown_secret_fields() -> None:
    payload = _payload("local-mock.json")
    payload["password"] = "not-returned-in-error"
    with pytest.raises((ValidationError, ValueError)) as exc_info:
        DeploymentSpec.model_validate_json(json.dumps(payload))
    assert "not-returned-in-error" not in str(exc_info.value)
