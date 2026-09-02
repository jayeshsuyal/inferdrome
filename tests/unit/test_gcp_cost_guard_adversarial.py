"""Adversarial local-only tests for the v2 read-only GCP cost boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from inferdrome.deployment import (
    ARM_CONFIRMATION,
    GcpCapacityInput,
    GcpCostQuote,
    GcpExecutionBootImage,
    GcpExecutionEnvironment,
    GcpExecutionNetwork,
    GcpExecutionPreflight,
    GcpLeaseRecord,
    GcpPlanningContext,
    GcpQuoteComponent,
    build_gcp_insert_request,
    canonical_gcp_execution_arm_bytes,
    gcp_execution_environment_digest,
    gcp_execution_request_digest,
    issue_gcp_execution_arm,
    parse_deployment_spec_json,
    parse_gcp_inventory_json,
    plan_gcp_dry_run,
    validate_gcp_execution_preflight,
)
from inferdrome.deployment.gcp_cost_guard import (
    GcpCostCleanupGuard,
    GcpCostGuardError,
    GcpReadOnlyQuoteBasis,
    canonical_gcp_read_only_quote_basis_bytes,
    issue_gcp_cost_cleanup_guard,
    issue_gcp_read_only_quote_basis,
    parse_gcp_read_only_quote_basis_json,
    validate_gcp_cost_cleanup_guard,
    validate_gcp_read_only_quote_basis,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _preflight() -> GcpExecutionPreflight:
    """Build canonical synthetic observations without a provider boundary."""

    spec = parse_deployment_spec_json(
        (ROOT / "deployments/v1/examples/gcp-dry-run-reference.json").read_bytes()
    )
    inventory = parse_gcp_inventory_json(
        (ROOT / "tests/fixtures/gcp-v1/synthetic-inventory.json").read_bytes()
    )
    context = GcpPlanningContext.model_validate_json(
        (ROOT / "tests/fixtures/gcp-v1/synthetic-planning-context.json").read_bytes()
    )
    plan = plan_gcp_dry_run(spec, inventory, context)
    arm = issue_gcp_execution_arm(
        plan=plan,
        expected_spec=spec,
        expected_inventory=inventory,
        expected_context=context,
        controller_id="ctl-12345678",
        nonce="0123456789abcdef0123456789abcdef",
        issued_at=NOW,
        max_controller_duration_seconds=600,
        confirmation=ARM_CONFIRMATION,
    )
    environment = GcpExecutionEnvironment(
        schema_version="inferdrome.gcp-execution-environment.v1",
        boot_image=GcpExecutionBootImage(
            image_name="projects/inferdrome-example/global/images/inferdrome-base-v1",
            provider_image_id=123456789,
            digest="sha256:" + "1" * 64,
            status="READY",
        ),
        runner_image=plan.runner_image,
        serving_runtime_image=plan.serving_runtime_image,
        network=GcpExecutionNetwork(
            network="projects/inferdrome-example/global/networks/private",
            subnetwork=(
                "projects/inferdrome-example/regions/us-central1/subnetworks/private"
            ),
            external_access_config="absent",
            ip_forwarding=False,
        ),
        service_account="inferdrome-runner@example-project.iam.gserviceaccount.com",
        service_account_scopes=("logging.write",),
        boot_disk_size_gib=100,
        boot_disk_type="pd-balanced",
        architecture="amd64",
        deletion_protection=False,
        automatic_restart=False,
        maintenance_policy="TERMINATE",
        startup_script_digest=None,
    )
    request = build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    components = tuple(
        GcpQuoteComponent(component_id=component, maximum_microusd=1_000)
        for component in ("compute", "gpu", "boot_disk", "network")
    )
    quote = GcpCostQuote(
        schema_version="inferdrome.gcp-execution-quote.v1",
        quote_id="quote-12345678",
        plan_id=arm.plan_id,
        controller_id=arm.controller_id,
        request_digest=gcp_execution_request_digest(request),
        environment_digest=gcp_execution_environment_digest(request),
        project_id=request.project_id,
        region=request.region,
        zone=request.zone,
        machine_type=request.machine_type,
        accelerator_model=request.accelerator_model,
        accelerator_provider_type=request.accelerator_provider_type,
        accelerator_count=request.accelerator_count,
        accelerator_attachment_mode=request.accelerator_attachment_mode,
        network=request.network.network,
        subnetwork=request.network.subnetwork,
        boot_image_name=request.boot_image.image_name,
        boot_image_provider_id=request.boot_image.provider_image_id,
        boot_image_digest=request.boot_image.digest,
        runner_image_digest=request.runner_image.digest,
        serving_runtime_image_digest=request.serving_runtime_image.digest,
        boot_disk_size_gib=request.boot_disk_size_gib,
        boot_disk_type=request.boot_disk_type,
        service_account=request.service_account,
        provider_max_runtime_seconds=request.provider_max_runtime_seconds,
        billable_duration_seconds=(
            request.provider_max_runtime_seconds
            + plan.timeouts.cleanup_seconds
            + plan.timeouts.termination_confirmation_seconds
        ),
        issued_at="2026-08-25T11:00:00Z",
        valid_until="2026-08-25T12:10:00Z",
        freshness_seconds=7_200,
        currency="USD",
        components=components,
        safety_margin_microusd=1_000,
        complete=True,
        pricing_proven=False,
        pricing_status="operator_supplied_estimate_not_invoice_truth",
        invoice_truth="unavailable_external_provider_invoice",
    )
    capacity = GcpCapacityInput(
        schema_version="inferdrome.gcp-execution-capacity.v1",
        plan_id=arm.plan_id,
        request_digest=gcp_execution_request_digest(request),
        source="operator_supplied_read_only_observation",
        observed_at="2026-08-25T11:00:00Z",
        freshness_seconds=7_200,
        project_id=request.project_id,
        region=request.region,
        zone=request.zone,
        machine_type=request.machine_type,
        accelerator_model=request.accelerator_model,
        accelerator_count=request.accelerator_count,
        capacity_status="operator_supplied_eligible",
        matching_active_resources=0,
        capacity_proven=False,
    )
    return validate_gcp_execution_preflight(
        plan=plan,
        expected_spec=spec,
        expected_inventory=inventory,
        expected_context=context,
        arm_bytes=canonical_gcp_execution_arm_bytes(arm),
        environment=environment,
        quote=quote,
        capacity=capacity,
        now=NOW,
    )


def _basis_and_guard() -> tuple[
    GcpExecutionPreflight, GcpReadOnlyQuoteBasis, GcpCostCleanupGuard
]:
    preflight = _preflight()
    basis = issue_gcp_read_only_quote_basis(preflight=preflight)
    guard = issue_gcp_cost_cleanup_guard(
        preflight=preflight,
        quote_basis=basis,
        max_cleanup_attempts=3,
        cleanup_timeout_seconds=120,
        now=NOW,
    )
    return preflight, basis, guard


def test_read_only_quote_basis_parser_rejects_duplicate_and_unknown_json() -> None:
    _preflight_input, basis, _guard = _basis_and_guard()
    raw = canonical_gcp_read_only_quote_basis_bytes(basis)

    duplicate = raw.replace(
        b'"basis_kind":"read_only_rational_rate_basis"',
        (
            b'"basis_kind":"read_only_rational_rate_basis",'
            b'"basis_kind":"read_only_rational_rate_basis"'
        ),
        1,
    )
    with pytest.raises(GcpCostGuardError, match="RATE_BASIS_INVALID"):
        parse_gcp_read_only_quote_basis_json(duplicate)

    unknown = json.loads(raw)
    unknown["unexpected"] = True
    with pytest.raises(GcpCostGuardError, match="RATE_BASIS_INVALID"):
        parse_gcp_read_only_quote_basis_json(json.dumps(unknown))


def test_cost_guard_rejects_quote_rate_basis_and_capacity_substitution() -> None:
    preflight, basis, guard = _basis_and_guard()
    substitute_components = tuple(
        component.model_copy(
            update={"maximum_microusd": component.maximum_microusd + 1}
        )
        if component.component_id == "gpu"
        else component
        for component in preflight.quote.components
    )
    quote_substitution = preflight.quote.model_copy(
        update={"components": substitute_components}
    )
    substituted_quote_preflight = replace(preflight, quote=quote_substitution)

    with pytest.raises(GcpCostGuardError, match="RATE_BASIS_BINDING_MISMATCH"):
        validate_gcp_read_only_quote_basis(
            basis,
            preflight=substituted_quote_preflight,
            now=NOW,
        )

    substituted_basis = issue_gcp_read_only_quote_basis(
        preflight=substituted_quote_preflight
    )
    with pytest.raises(GcpCostGuardError, match="RATE_BASIS_BINDING_MISMATCH"):
        validate_gcp_cost_cleanup_guard(
            guard,
            preflight=preflight,
            quote_basis=substituted_basis,
            now=NOW,
        )

    capacity_substitution = preflight.capacity.model_copy(
        update={"observed_at": "2026-08-25T11:00:01Z"}
    )
    with pytest.raises(GcpCostGuardError, match="COST_GUARD_BINDING_MISMATCH"):
        validate_gcp_cost_cleanup_guard(
            guard,
            preflight=replace(preflight, capacity=capacity_substitution),
            quote_basis=basis,
            now=NOW,
        )


def test_cost_guard_rejects_expiry_and_cleanup_timeout_drift() -> None:
    preflight, basis, guard = _basis_and_guard()
    expired_now = NOW + timedelta(minutes=11)

    with pytest.raises(GcpCostGuardError, match="RATE_BASIS_EXPIRED"):
        validate_gcp_read_only_quote_basis(
            basis,
            preflight=preflight,
            now=expired_now,
        )

    quote_valid_beyond_controller_deadline = preflight.quote.model_copy(
        update={"valid_until": "2026-08-25T12:20:00Z"}
    )
    later_basis_preflight = replace(
        preflight, quote=quote_valid_beyond_controller_deadline
    )
    later_basis = issue_gcp_read_only_quote_basis(preflight=later_basis_preflight)
    later_guard = issue_gcp_cost_cleanup_guard(
        preflight=later_basis_preflight,
        quote_basis=later_basis,
        max_cleanup_attempts=3,
        cleanup_timeout_seconds=120,
        now=NOW,
    )
    with pytest.raises(GcpCostGuardError, match="COST_GUARD_DEADLINE_EXPIRED"):
        validate_gcp_cost_cleanup_guard(
            later_guard,
            preflight=later_basis_preflight,
            quote_basis=later_basis,
            now=expired_now,
        )

    lease_with_cleanup_timeout_drift = SimpleNamespace(
        plan_id=guard.plan_id,
        request_digest=guard.request_digest,
        quote_digest=guard.quote_digest,
        capacity_digest=guard.capacity_digest,
        estimated_max_microusd=guard.estimated_max_microusd,
        max_cleanup_attempts=guard.max_cleanup_attempts,
        cleanup_timeout_seconds=guard.cleanup_timeout_seconds + 1,
    )
    with pytest.raises(GcpCostGuardError, match="COST_GUARD_LEASE_MISMATCH"):
        validate_gcp_cost_cleanup_guard(
            guard,
            preflight=preflight,
            quote_basis=basis,
            now=NOW,
            record=cast(GcpLeaseRecord, lease_with_cleanup_timeout_drift),
        )
