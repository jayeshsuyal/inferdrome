"""Offline GCP inventory, deterministic planning, and publication tests."""

from __future__ import annotations

import json
import re
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from inferdrome.deployment import (
    GCP_INVENTORY_SCHEMA_ID,
    GCP_PLAN_SCHEMA_ID,
    GcpDryRunPlan,
    GcpInventorySnapshot,
    GcpPlanError,
    GcpPlanningContext,
    GcpPlanPublicationError,
    canonical_gcp_inventory_bytes,
    canonical_gcp_plan_bytes,
    gcp_inventory_digest,
    gcp_inventory_schema,
    gcp_plan_id,
    gcp_plan_schema,
    gcp_plan_sha256,
    parse_deployment_spec_json,
    parse_gcp_inventory_json,
    parse_gcp_plan_json,
    plan_gcp_dry_run,
    publish_gcp_dry_run_plan,
    verify_gcp_dry_run_plan,
    verify_gcp_dry_run_plan_bytes,
    verify_published_gcp_dry_run_plan,
)
from scripts.gcp_dry_run_plan import _read_regular

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPOSITORY_ROOT / "deployments/v1/examples/gcp-dry-run-reference.json"
INVENTORY = REPOSITORY_ROOT / "tests/fixtures/gcp-v1/synthetic-inventory.json"
CONTEXT = REPOSITORY_ROOT / "tests/fixtures/gcp-v1/synthetic-planning-context.json"
PLAN = REPOSITORY_ROOT / "plans/gcp/v1/gcp-dry-run-reference.plan.json"
INVENTORY_SCHEMA = REPOSITORY_ROOT / "schemas/deployment/v1/gcp-inventory.schema.json"
PLAN_SCHEMA = REPOSITORY_ROOT / "schemas/deployment/v1/gcp-plan.schema.json"


def _payload(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def _inputs() -> tuple[Any, GcpInventorySnapshot, GcpPlanningContext]:
    return (
        parse_deployment_spec_json(EXAMPLE.read_bytes()),
        parse_gcp_inventory_json(INVENTORY.read_bytes()),
        GcpPlanningContext.model_validate_json(CONTEXT.read_bytes()),
    )


def _plan() -> Any:
    spec, inventory, context = _inputs()
    return plan_gcp_dry_run(spec, inventory, context)


def test_schemas_are_closed_current_and_additive() -> None:
    inventory_schema = json.loads(INVENTORY_SCHEMA.read_bytes())
    plan_schema = json.loads(PLAN_SCHEMA.read_bytes())
    Draft202012Validator.check_schema(inventory_schema)
    Draft202012Validator.check_schema(plan_schema)
    assert inventory_schema == gcp_inventory_schema()
    assert plan_schema == gcp_plan_schema()
    assert inventory_schema["$id"] == GCP_INVENTORY_SCHEMA_ID
    assert plan_schema["$id"] == GCP_PLAN_SCHEMA_ID
    assert "schemas/public/v1" not in str(INVENTORY_SCHEMA)

    def assert_closed(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for child in value.values():
                assert_closed(child)
        elif isinstance(value, list):
            for child in value:
                assert_closed(child)

    assert_closed(inventory_schema)
    assert_closed(plan_schema)


def test_generated_fixture_is_exact_canonical_and_cross_input_verified() -> None:
    spec, inventory, context = _inputs()
    plan = _plan()
    fixture_bytes = PLAN.read_bytes()
    assert fixture_bytes == canonical_gcp_plan_bytes(plan)
    assert not fixture_bytes.endswith(b"\n")
    assert plan.plan_id == gcp_plan_id(plan)
    assert gcp_plan_sha256(plan).startswith("sha256:")
    assert (
        verify_gcp_dry_run_plan_bytes(
            fixture_bytes,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
        )
        == plan
    )
    assert plan.execution_authorized is False
    assert plan.provider_mutation_performed is False
    assert plan.credentials_resolved is False
    assert plan.capacity_proven is False
    assert plan.pricing_proven is False
    assert plan.evidence_eligible is False
    assert plan.planning_scope.compute_project_id == "inferdrome-example"


def test_determinism_and_stable_zone_machine_tie_break() -> None:
    spec, inventory, context = _inputs()
    first = plan_gcp_dry_run(spec, inventory, context)
    payload = _payload(INVENTORY)
    payload["offerings"] = list(reversed(payload["offerings"]))
    reordered = parse_gcp_inventory_json(json.dumps(payload, indent=2))
    second = plan_gcp_dry_run(spec, reordered, context)
    assert canonical_gcp_inventory_bytes(inventory) == canonical_gcp_inventory_bytes(
        reordered
    )
    assert canonical_gcp_plan_bytes(first) == canonical_gcp_plan_bytes(second)
    assert first.selection.selected_provider.zone == "us-central1-b"
    assert first.selection.selected_provider.machine_type == "synthetic-a2-highgpu-1g"


def test_reordered_json_has_one_meaning_and_same_digests() -> None:
    raw = _payload(INVENTORY)
    reordered = {key: raw[key] for key in reversed(tuple(raw))}
    left = parse_gcp_inventory_json(json.dumps(raw))
    right = parse_gcp_inventory_json(json.dumps(reordered, indent=4))
    assert canonical_gcp_inventory_bytes(left) == canonical_gcp_inventory_bytes(right)
    assert gcp_inventory_digest(left) == gcp_inventory_digest(right)
    plan = _plan()
    plan_value = json.loads(canonical_gcp_plan_bytes(plan))
    plan_reordered = {key: plan_value[key] for key in reversed(tuple(plan_value))}
    assert parse_gcp_plan_json(json.dumps(plan_reordered)) == plan


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":"inferdrome.gcp-inventory.v1","schema_version":"inferdrome.gcp-inventory.v1"}',
        '{"schema_version":"inferdrome.gcp-inventory.v1","source":{"kind":"synthetic_fixture","kind":"synthetic_fixture","observed_at":null}}',
    ],
)
def test_inventory_duplicate_keys_reject_before_model_validation(raw: str) -> None:
    with pytest.raises(ValueError, match="keys must be unique"):
        parse_gcp_inventory_json(raw)
    with pytest.raises(ValueError, match="keys must be unique"):
        GcpInventorySnapshot.model_validate_json(raw)


def test_plan_duplicate_and_unknown_fields_reject_at_every_level() -> None:
    raw = PLAN.read_text(encoding="utf-8")
    # Insert a second key without exposing a submitted value in the assertion.
    duplicate = raw.replace(
        '"plan_kind":"gcp_dry_run_reference"',
        '"plan_kind":"gcp_dry_run_reference","plan_kind":"gcp_dry_run_reference"',
        1,
    )
    with pytest.raises(ValueError, match="keys must be unique"):
        parse_gcp_plan_json(duplicate)
    value = json.loads(raw)
    value["unknown"] = True
    with pytest.raises(ValidationError):
        parse_gcp_plan_json(json.dumps(value))
    value = json.loads(raw)
    value["runtime"]["unknown"] = True
    with pytest.raises(ValidationError):
        parse_gcp_plan_json(json.dumps(value))


def test_nonfinite_oversized_public_endpoint_and_secret_values_fail_closed() -> None:
    inventory = _payload(INVENTORY)
    inventory["offerings"][0]["cpu_cores"] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        parse_gcp_inventory_json(json.dumps(inventory, allow_nan=True))
    with pytest.raises(ValueError, match="exceeds"):
        parse_gcp_inventory_json("{" + "a" * 524_289 + "}")
    inventory = _payload(INVENTORY)
    inventory["offerings"][0]["machine_type"] = "https://public.invalid"
    with pytest.raises(ValueError) as public_error:
        parse_gcp_inventory_json(json.dumps(inventory))
    assert "public.invalid" not in str(public_error.value)
    inventory = _payload(INVENTORY)
    inventory["offerings"][0]["api_key"] = "sk-" + "Z" * 32
    with pytest.raises(ValueError) as secret_error:
        parse_gcp_inventory_json(json.dumps(inventory))
    assert "ZZZZ" not in str(secret_error.value)
    inventory = _payload(INVENTORY)
    inventory["offerings"][0]["machine_type"] = "A" * 48
    with pytest.raises(ValueError) as generic_error:
        parse_gcp_inventory_json(json.dumps(inventory))
    assert "AAAA" not in str(generic_error.value)


def test_public_url_in_structurally_accepted_plan_field_is_rejected_in_preflight() -> (
    None
):
    value = json.loads(PLAN.read_bytes())
    value["runtime"]["model_id"] = "https://public.invalid"
    with pytest.raises(ValueError) as error:
        parse_gcp_plan_json(json.dumps(value))
    assert "public.invalid" not in str(error.value)


def test_lowercase_hex_is_only_allowed_in_plan_revision_identity_fields() -> None:
    plan = parse_gcp_plan_json(PLAN.read_bytes())
    assert re.fullmatch(r"[0-9a-f]{40,64}", plan.runtime.model_revision)
    inventory = _payload(INVENTORY)
    inventory["offerings"][0]["machine_type"] = "a" * 40
    with pytest.raises(ValueError) as error:
        parse_gcp_inventory_json(json.dumps(inventory))
    assert "aaaa" not in str(error.value)


@pytest.mark.parametrize(
    "timestamp",
    ["2026-99-99T12:00:00Z", "2026-08-25T25:00:00Z", "2026-08-25T12:00:00.1Z"],
)
def test_inventory_timestamp_requires_real_canonical_utc(timestamp: str) -> None:
    inventory = _payload(INVENTORY)
    inventory["source"] = {"kind": "offline_snapshot", "observed_at": timestamp}
    with pytest.raises((ValueError, ValidationError)):
        parse_gcp_inventory_json(json.dumps(inventory))
    inventory["source"]["observed_at"] = "2026-08-25T12:00:00Z"
    parsed = parse_gcp_inventory_json(json.dumps(inventory))
    assert parsed.source.observed_at == "2026-08-25T12:00:00Z"


def test_region_and_zone_grammar_rejects_pseudo_regions() -> None:
    inventory = _payload(INVENTORY)
    inventory["region"] = "us"
    inventory["offerings"][0]["zone"] = "us-a"
    with pytest.raises((ValueError, ValidationError)):
        parse_gcp_inventory_json(json.dumps(inventory))
    with pytest.raises(ValidationError):
        GcpPlanningContext(compute_project_id="inferdrome-example", region="us")


def test_zone_scoped_catalog_never_cross_products_zone_and_gpu() -> None:
    spec, inventory, context = _inputs()
    assert inventory.offerings[0].zone == "us-central1-a"
    assert inventory.offerings[0].accelerator_model != "NVIDIA A100-SXM4-40GB"
    plan = plan_gcp_dry_run(spec, inventory, context)
    assert plan.selection.selected_provider.zone == "us-central1-b"
    assert plan.selection.selected_provider.catalog_eligibility == "catalog_eligible"


def test_duplicate_zone_scoped_offerings_reject() -> None:
    inventory = _payload(INVENTORY)
    inventory["offerings"].append(dict(inventory["offerings"][1]))
    with pytest.raises(ValidationError):
        parse_gcp_inventory_json(json.dumps(inventory))


def test_compute_scope_is_explicit_and_secret_project_is_not_used() -> None:
    spec_payload = _payload(EXAMPLE)
    spec_payload["provider"]["credential_refs"][0]["project_id"] = "security-project"
    spec = parse_deployment_spec_json(json.dumps(spec_payload))
    inventory = parse_gcp_inventory_json(INVENTORY.read_bytes())
    context = GcpPlanningContext(
        compute_project_id="inferdrome-example", region="us-central1"
    )
    plan = plan_gcp_dry_run(spec, inventory, context)
    assert plan.planning_scope.compute_project_id == "inferdrome-example"
    assert plan.selection.selected_provider.project_id == "inferdrome-example"
    assert plan.credentials_resolved is False


def test_context_region_or_inventory_scope_drift_rejects() -> None:
    spec, inventory, context = _inputs()
    with pytest.raises(GcpPlanError, match="region"):
        plan_gcp_dry_run(
            spec,
            inventory,
            GcpPlanningContext(
                compute_project_id="inferdrome-example", region="europe-west1"
            ),
        )
    with pytest.raises(GcpPlanError, match="project"):
        plan_gcp_dry_run(
            spec,
            inventory,
            GcpPlanningContext(
                compute_project_id="another-project", region=context.region
            ),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: [
            offering.update(catalog_eligibility="catalog_unavailable")
            for offering in value["offerings"]
        ],
        lambda value: value["offerings"][1].update(
            accelerator_model="NVIDIA H100 PCIe"
        ),
        lambda value: [offering.update(cpu_cores=1) for offering in value["offerings"]],
        lambda value: value["offerings"][1].update(catalog_eligibility="unknown"),
        lambda value: [
            offering.update(architecture="arm64") for offering in value["offerings"]
        ],
    ],
)
def test_no_match_and_unsupported_inventory_fail_closed(mutation: Any) -> None:
    spec, _, context = _inputs()
    value = _payload(INVENTORY)
    mutation(value)
    inventory = parse_gcp_inventory_json(json.dumps(value))
    with pytest.raises(GcpPlanError, match="eligible resource match"):
        plan_gcp_dry_run(spec, inventory, context)


def test_offline_planning_never_uses_socket_subprocess_or_writes_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec, inventory, context = _inputs()
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("socket")),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("subprocess")),
    )
    plan = plan_gcp_dry_run(spec, inventory, context)
    assert plan.plan_id
    assert list(tmp_path.iterdir()) == []


def test_plan_cross_input_substitution_and_model_copy_are_rejected(
    tmp_path: Path,
) -> None:
    spec, inventory, context = _inputs()
    plan = _plan()
    altered_inventory_value = _payload(INVENTORY)
    altered_inventory_value["offerings"][1]["catalog_eligibility"] = (
        "catalog_unavailable"
    )
    altered_inventory = parse_gcp_inventory_json(json.dumps(altered_inventory_value))
    with pytest.raises(GcpPlanError):
        verify_gcp_dry_run_plan(
            plan,
            expected_spec=spec,
            expected_inventory=altered_inventory,
            expected_context=context,
        )
    forged = plan.model_copy(update={"plan_id": "sha256:" + "0" * 64})
    with pytest.raises(GcpPlanPublicationError):
        publish_gcp_dry_run_plan(
            root=tmp_path / "forged",
            plan=forged,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
        )
    constructed_values = dict(plan.__dict__)
    constructed_values["plan_id"] = "sha256:" + "1" * 64
    constructed = GcpDryRunPlan.model_construct(**constructed_values)
    with pytest.raises(GcpPlanPublicationError):
        publish_gcp_dry_run_plan(
            root=tmp_path / "constructed",
            plan=constructed,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
        )


def test_publish_is_no_replace_read_back_verified_and_tamper_detected(
    tmp_path: Path,
) -> None:
    spec, inventory, context = _inputs()
    plan = _plan()
    published = publish_gcp_dry_run_plan(
        root=tmp_path,
        plan=plan,
        expected_spec=spec,
        expected_inventory=inventory,
        expected_context=context,
    )
    assert published.path.is_dir()
    assert (published.path / "plan.json").read_bytes() == canonical_gcp_plan_bytes(plan)
    assert (
        verify_published_gcp_dry_run_plan(
            published.path,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
        )
        == plan
    )
    with pytest.raises(GcpPlanPublicationError):
        publish_gcp_dry_run_plan(
            root=tmp_path,
            plan=plan,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
        )
    (published.path / "plan.json").chmod(0o600)
    (published.path / "plan.json").write_bytes(
        canonical_gcp_plan_bytes(plan).replace(b"false", b"true", 1)
    )
    with pytest.raises(GcpPlanError):
        verify_published_gcp_dry_run_plan(
            published.path,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
        )


def test_rejected_publication_does_not_create_root_or_follow_symlink(
    tmp_path: Path,
) -> None:
    spec, inventory, context = _inputs()
    plan = _plan()
    invalid = plan.model_copy(update={"evidence_eligible": True})
    rejected_root = tmp_path / "rejected"
    with pytest.raises(GcpPlanPublicationError):
        publish_gcp_dry_run_plan(
            root=rejected_root,
            plan=invalid,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
        )
    assert not rejected_root.exists()
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(GcpPlanPublicationError):
        publish_gcp_dry_run_plan(
            root=link,
            plan=plan,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
        )
    assert list(target.iterdir()) == []


def test_cli_reader_rejects_symlink_non_regular_and_oversized_inputs(
    tmp_path: Path,
) -> None:
    regular = tmp_path / "input.json"
    regular.write_bytes(b"{}")
    assert _read_regular(regular) == b"{}"
    link = tmp_path / "link.json"
    link.symlink_to(regular)
    with pytest.raises(ValueError, match="unavailable"):
        _read_regular(link)
    with pytest.raises(ValueError, match="unavailable"):
        _read_regular(tmp_path)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (524_288 + 1))
    with pytest.raises(ValueError, match="unavailable"):
        _read_regular(oversized)
