"""Offline and adversarial tests for the PR8 GCP execution boundary."""

from __future__ import annotations

import inspect
import json
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from inferdrome.deployment import (
    ARM_CONFIRMATION,
    GCP_RECOVERY_CONFIRMATION,
    FakeGcpComputeTransport,
    FileExecutionArmStore,
    GcpCapacityInput,
    GcpClock,
    GcpCostQuote,
    GcpExecutionBootImage,
    GcpExecutionEnvironment,
    GcpExecutionError,
    GcpExecutionNetwork,
    GcpGuardedLifecycleController,
    GcpInsertRequest,
    GcpJournalError,
    GcpLeaseJournal,
    GcpPlanError,
    GcpQuoteComponent,
    GcpTransportError,
    InMemoryExecutionArmStore,
    canonical_gcp_execution_arm_bytes,
    canonical_gcp_execution_outcome_bytes,
    gcp_execution_contract_schemas,
    gcp_execution_environment_digest,
    gcp_execution_request_digest,
    gcp_execution_supervisor_contract_schemas,
    gcp_execution_v2_safety_contract_schemas,
    gcp_supervisor_binding,
    issue_gcp_execution_arm,
    parse_deployment_spec_json,
    parse_gcp_execution_arm_json,
    parse_gcp_inventory_json,
    plan_gcp_dry_run,
    system_gcp_clock,
    validate_gcp_a2_profile,
    validate_gcp_execution_preflight,
)
from inferdrome.deployment.gcp_compute_transport import (
    GcpV2LocalTransportFactory,
    _canonical_resource_ref,
    _observation,
    bind_gcp_v2_watchdog_cleanup_transport,
    create_google_compute_transport,
    create_google_compute_transport_for_v2_capability,
)
from inferdrome.deployment.gcp_cost_guard import (
    GcpCostGuardError,
    canonical_gcp_cost_cleanup_guard_bytes,
    canonical_gcp_read_only_quote_basis_bytes,
    gcp_cost_cleanup_guard_digest,
    gcp_cost_guard_contract_schemas,
    gcp_read_only_quote_basis_digest,
    issue_gcp_cost_cleanup_guard,
    issue_gcp_read_only_quote_basis,
    parse_gcp_cost_cleanup_guard_json,
    parse_gcp_read_only_quote_basis_json,
)
from inferdrome.deployment.gcp_supervisor import (
    _MUTATION_ACTIVATION_PROOF_SEAL,
    GCP_KILL_SWITCH_CONFIRMATION,
    GCP_SUPERVISOR_CONFIRMATION,
    FakeGcpWatchdog,
    FileGcpKillSwitch,
    FileGcpWatchdog,
    GcpLifecycleSupervisor,
    GcpSupervisorError,
    GcpSupervisorJournal,
    GcpV2ActivatedMutationProof,
    GcpV2LocalWatchdogExecutorFactory,
    GcpWatchdogActivationReceipt,
    InMemoryGcpKillSwitch,
    _MutationProofUse,
    canonical_gcp_execution_approval_bytes,
    canonical_gcp_kill_switch_bytes,
    gcp_execution_approval_digest,
    gcp_watchdog_activation_receipt_id,
    issue_gcp_execution_approval,
    issue_gcp_kill_switch_record,
    parse_gcp_execution_approval_json,
    validate_gcp_v2_activated_mutation_proof,
)
from inferdrome.deployment.gcp_v2_contracts import (
    GCP_CLEANUP_RECOVERY_CONFIRMATION,
    GcpV2ContractError,
    GcpV2MutationCapability,
    canonical_gcp_v2_startup_projection_bytes,
    gcp_v2_execution_payload_digest,
    gcp_v2_mutation_capability_id,
    issue_gcp_cleanup_recovery_authorization,
    issue_gcp_v2_activation_deadline,
    issue_gcp_v2_mutation_capability,
    issue_gcp_v2_startup_projection,
    parse_gcp_v2_startup_projection_json,
    validate_gcp_cleanup_recovery_authorization,
)
from inferdrome.deployment.gcp_v2_disk_cleanup import (
    GCP_V2_OWNED_BOOT_DISK_SCHEMA_VERSION,
    FileBackedFakeDiskProvider,
    GcpV2DiskCleanupError,
    GcpV2ExactOwnedBootDisk,
    GcpV2ExactOwnedInstance,
    GcpV2LocalDiskCleanupFactory,
    issue_gcp_v2_disk_cleanup_binding,
    issue_gcp_v2_owned_boot_disk_inventory,
)
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "deployments/v1/examples/gcp-dry-run-reference.json"
INVENTORY_PATH = ROOT / "tests/fixtures/gcp-v1/synthetic-inventory.json"
CONTEXT_PATH = ROOT / "tests/fixtures/gcp-v1/synthetic-planning-context.json"
ARM_FIXTURE = ROOT / "tests/fixtures/gcp-v1/synthetic-execution-arm.json"
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _inputs() -> tuple[Any, Any, Any, Any]:
    spec = parse_deployment_spec_json(SPEC_PATH.read_bytes())
    inventory = parse_gcp_inventory_json(INVENTORY_PATH.read_bytes())
    from inferdrome.deployment import GcpPlanningContext

    context = GcpPlanningContext.model_validate_json(CONTEXT_PATH.read_bytes())
    return spec, inventory, context, plan_gcp_dry_run(spec, inventory, context)


def _arm(controller_id: str = "ctl-12345678") -> Any:
    spec, inventory, context, plan = _inputs()
    return issue_gcp_execution_arm(
        plan=plan,
        expected_spec=spec,
        expected_inventory=inventory,
        expected_context=context,
        controller_id=controller_id,
        nonce="0123456789abcdef0123456789abcdef",
        issued_at=NOW,
        max_controller_duration_seconds=600,
        confirmation=ARM_CONFIRMATION,
    )


def _environment(plan: Any) -> GcpExecutionEnvironment:
    return GcpExecutionEnvironment(
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
            subnetwork="projects/inferdrome-example/regions/us-central1/subnetworks/private",
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


def _quote_and_capacity(
    plan: Any, arm: Any, request: GcpInsertRequest
) -> tuple[Any, Any]:
    components = tuple(
        GcpQuoteComponent(component_id=name, maximum_microusd=1_000)
        for name in ("compute", "gpu", "boot_disk", "network")
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
    return quote, capacity


def _controller(
    tmp_path: Path,
    transport: Any,
    arm_store: Any | None = None,
    *,
    safety_supervisor: Any | None = None,
) -> Any:
    return GcpGuardedLifecycleController(
        transport=transport,
        arm_store=arm_store or InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(tmp_path),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=safety_supervisor,
    )


def _run(
    tmp_path: Path,
    transport: Any,
    *,
    work: Any = None,
    return_controller: bool = False,
    safety_supervisor: Any | None = None,
) -> Any:
    spec, inventory, context, plan = _inputs()
    arm = _arm()
    environment = _environment(plan)
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    quote, capacity = _quote_and_capacity(plan, arm, request)
    controller = _controller(
        tmp_path, transport, safety_supervisor=safety_supervisor
    )
    outcome = controller.execute(
        plan=plan,
        expected_spec=spec,
        expected_inventory=inventory,
        expected_context=context,
        arm_bytes=canonical_gcp_execution_arm_bytes(arm),
        environment=environment,
        quote=quote,
        capacity=capacity,
        work=work,
    )
    return (outcome, controller) if return_controller else outcome


def _v2_inputs(controller_id: str = "ctl-12345678") -> tuple[Any, ...]:
    spec, inventory, context, plan = _inputs()
    arm = _arm(controller_id)
    environment = _environment(plan)
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    quote, capacity = _quote_and_capacity(plan, arm, request)
    preflight = validate_gcp_execution_preflight(
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
    return spec, inventory, context, plan, arm, environment, quote, capacity, preflight


def _v2_inputs_with_observations(
    inputs: tuple[Any, ...], *, quote: Any | None = None, capacity: Any | None = None
) -> tuple[Any, ...]:
    """Rebuild canonical preflight after a local fake observation change."""

    (
        spec,
        inventory,
        context,
        plan,
        arm,
        environment,
        original_quote,
        original_capacity,
        _preflight,
    ) = inputs
    quote = original_quote if quote is None else quote
    capacity = original_capacity if capacity is None else capacity
    preflight = validate_gcp_execution_preflight(
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
    return spec, inventory, context, plan, arm, environment, quote, capacity, preflight


def _v2_startup_projection(preflight: Any) -> Any:
    """Bind only fixed opaque test bytes; no execution semantics are implied."""

    return issue_gcp_v2_startup_projection(
        request=preflight.request,
        execution_payload=b"inferdrome-v2-opaque-test-payload",
    )


def _execute_v2(controller: Any, inputs: tuple[Any, ...], *, work: Any = None) -> Any:
    (
        spec,
        inventory,
        context,
        plan,
        arm,
        environment,
        quote,
        capacity,
        _preflight,
    ) = inputs
    return controller.execute(
        plan=plan,
        expected_spec=spec,
        expected_inventory=inventory,
        expected_context=context,
        arm_bytes=canonical_gcp_execution_arm_bytes(arm),
        environment=environment,
        quote=quote,
        capacity=capacity,
        work=work,
    )


def _v2_supervisor(
    root: Path,
    preflight: Any,
    *,
    watchdog: Any | None = None,
    kill_switch: Any | None = None,
) -> tuple[GcpLifecycleSupervisor, Any]:
    root.mkdir()
    startup_projection = _v2_startup_projection(preflight)
    quote_basis = issue_gcp_read_only_quote_basis(preflight=preflight)
    cost_guard = issue_gcp_cost_cleanup_guard(
        preflight=preflight,
        quote_basis=quote_basis,
        max_cleanup_attempts=3,
        cleanup_timeout_seconds=120,
        now=NOW,
    )
    activation_deadline = issue_gcp_v2_activation_deadline(
        preflight=preflight,
        startup_projection=startup_projection,
        rate_basis_digest=gcp_read_only_quote_basis_digest(quote_basis),
        cost_guard_digest=gcp_cost_cleanup_guard_digest(cost_guard),
        issued_at=NOW,
        authorization_expires_at=NOW + timedelta(minutes=5),
        setup_margin_seconds=60,
    )
    approval = issue_gcp_execution_approval(
        preflight=preflight,
        quote_basis=quote_basis,
        cost_guard=cost_guard,
        activation_deadline=activation_deadline,
        startup_projection=startup_projection,
        operator_identity="operator-alice",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        confirmation=GCP_SUPERVISOR_CONFIRMATION,
    )
    return (
        GcpLifecycleSupervisor(
            approval=approval,
            quote_basis=quote_basis,
            cost_guard=cost_guard,
            activation_deadline=activation_deadline,
            startup_projection=startup_projection,
            journal=GcpSupervisorJournal(root),
            watchdog=watchdog or FakeGcpWatchdog(),
            kill_switch=kill_switch or InMemoryGcpKillSwitch(),
        ),
        approval,
    )


def _v2_disk_binding_for_record(record: Any) -> Any:
    """Return a test-only externally observed disk binding.

    The disk/attachment values model one complete provider observation.  The
    name happens to equal this fake's observed boot disk; it is supplied as a
    fact and is separately checked against the post-create transport readback.
    Production callers must obtain the equivalent binding from the dedicated
    exact inventory boundary.
    """

    request = record.request
    disk = GcpV2ExactOwnedBootDisk(
        schema_version=GCP_V2_OWNED_BOOT_DISK_SCHEMA_VERSION,
        request_digest=record.request_digest,
        project_id=record.project_id,
        zone=record.zone,
        instance_name=record.instance_name,
        disk_name=record.instance_name,
        provider_disk_id=987654321,
        self_link=(
            f"projects/{record.project_id}/zones/{record.zone}/disks/"
            f"{record.instance_name}"
        ),
        attached_instance_provider_id=123456790,
        attached_instance_self_link=(
            f"projects/{record.project_id}/zones/{record.zone}/instances/"
            f"{record.instance_name}"
        ),
        boot_device_name="boot",
        boot_source_disk_self_link=(
            f"projects/{record.project_id}/zones/{record.zone}/disks/"
            f"{record.instance_name}"
        ),
        boot_attachment=True,
        labels=record.labels,
        source_image_name=request.boot_image.image_name,
        source_image_provider_id=request.boot_image.provider_image_id,
        source_image_digest=request.boot_image.digest,
        disk_type=request.boot_disk_type,
        size_gib=request.boot_disk_size_gib,
        state="PRESENT",
    )
    inventory = issue_gcp_v2_owned_boot_disk_inventory(disk=disk, observed_at=NOW)
    instance = GcpV2ExactOwnedInstance(
        schema_version="inferdrome.gcp-owned-instance.v2",
        request_digest=record.request_digest,
        project_id=record.project_id,
        region=record.region,
        zone=record.zone,
        instance_name=record.instance_name,
        provider_instance_id=123456790,
        self_link=(
            f"projects/{record.project_id}/zones/{record.zone}/instances/"
            f"{record.instance_name}"
        ),
        labels=record.labels,
        state="RUNNING",
    )
    return issue_gcp_v2_disk_cleanup_binding(
        inventory,
        controller_id=record.controller_id,
        attached_instance=instance,
    )


class _UnexpectedFileWatchdogExecutor:
    """Sentinel executor for activation-only tests.

    A successful core cleanup must settle the durable watchdog locally; this
    executor therefore makes an accidental sidecar cleanup visible.
    """

    def cleanup_exact(self, event: Any, *, timeout_seconds: int) -> Any:
        del event, timeout_seconds
        raise AssertionError("file watchdog executor must not run in this test")

    def cleanup_prebind(self, event: Any, *, timeout_seconds: int) -> Any:
        del event, timeout_seconds
        raise AssertionError("prebind watchdog executor must not run in this test")


class _NoopFactorySafetySupervisor:
    """Private test double for legacy fake-only factory-boundary tests."""

    def reserve(self, record: Any, *, preflight: Any, now: datetime) -> None:
        del record, preflight, now

    def arm_watchdog(self, record: Any, *, preflight: Any, now: datetime) -> None:
        del record, preflight, now

    def assert_create_permitted(
        self, record: Any, *, preflight: Any, now: datetime
    ) -> None:
        del record, preflight, now

    def create_intent(self, record: Any, *, preflight: Any, now: datetime) -> None:
        del record, preflight, now

    def before_work(self, record: Any, *, preflight: Any, now: datetime) -> None:
        del record, preflight, now

    def cleanup_pending(self, record: Any, *, now: datetime) -> None:
        del record, now

    def cleanup_terminal(
        self,
        record: Any,
        *,
        confirmed: bool,
        error_code: str | None,
        now: datetime,
    ) -> None:
        del record, confirmed, error_code, now

    def block(self, record: Any, *, error_code: str, now: datetime) -> None:
        del record, error_code, now

    def resume_cleanup(self, record: Any, *, now: datetime) -> None:
        del record, now


def test_generated_schema_is_closed_and_fixture_is_exact() -> None:
    schemas = gcp_execution_contract_schemas()
    assert len(schemas) == 11
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
        assert schema["$id"].startswith("urn:inferdrome:gcp-execution-")

    supervisor_schemas = gcp_execution_supervisor_contract_schemas()
    assert len(supervisor_schemas) == 9
    for schema in supervisor_schemas.values():
        Draft202012Validator.check_schema(schema)
        assert schema["$id"].startswith("urn:inferdrome:gcp-")
        assert schema["$id"].endswith(":v2")

    cost_guard_schemas = gcp_cost_guard_contract_schemas()
    assert len(cost_guard_schemas) == 2
    for schema in cost_guard_schemas.values():
        Draft202012Validator.check_schema(schema)
        assert schema["$id"].startswith("urn:inferdrome:gcp-")
        assert schema["$id"].endswith(":v2")

    v2_safety_schemas = gcp_execution_v2_safety_contract_schemas()
    assert len(v2_safety_schemas) == 3
    for schema in v2_safety_schemas.values():
        Draft202012Validator.check_schema(schema)
        assert schema["$id"].startswith("urn:inferdrome:gcp-")
        assert schema["$id"].endswith(":v2")

    spec, inventory, context, plan = _inputs()
    expected = issue_gcp_execution_arm(
        plan=plan,
        expected_spec=spec,
        expected_inventory=inventory,
        expected_context=context,
        controller_id="ctl-a1b2c3d4",
        nonce="0123456789abcdef0123456789abcdef",
        issued_at=NOW,
        max_controller_duration_seconds=600,
        confirmation=ARM_CONFIRMATION,
    )
    assert ARM_FIXTURE.read_bytes() == canonical_gcp_execution_arm_bytes(expected)
    assert parse_gcp_execution_arm_json(ARM_FIXTURE.read_bytes()) == expected


def test_arm_is_deterministic_and_reordered_json_has_one_meaning() -> None:
    arm = _arm()
    raw = canonical_gcp_execution_arm_bytes(arm)
    value = json.loads(raw)
    reordered = {key: value[key] for key in reversed(tuple(value))}
    assert parse_gcp_execution_arm_json(json.dumps(reordered)) == arm
    assert (
        canonical_gcp_execution_arm_bytes(
            parse_gcp_execution_arm_json(json.dumps(reordered))
        )
        == raw
    )


def test_duplicate_unknown_and_nonfinite_arm_inputs_reject_without_echo() -> None:
    raw = canonical_gcp_execution_arm_bytes(_arm()).decode()
    duplicate = raw.replace(
        '"arm_kind":"one_shot_operator_capability"',
        '"arm_kind":"one_shot_operator_capability","arm_kind":"one_shot_operator_capability"',
        1,
    )
    with pytest.raises(ValueError, match="keys must be unique"):
        parse_gcp_execution_arm_json(duplicate)
    value = json.loads(raw)
    value["unknown"] = True
    with pytest.raises(ValidationError):
        parse_gcp_execution_arm_json(json.dumps(value))
    with pytest.raises(ValueError, match="non-finite"):
        parse_gcp_execution_arm_json(raw.replace("600", "NaN", 1))


def test_arm_rejects_replay_substitution_expiry_and_invalid_copy_before_transport(
    tmp_path: Path,
) -> None:
    spec, inventory, context, plan = _inputs()
    arm = _arm()
    other = _arm("ctl-87654321")
    with pytest.raises((ValueError, GcpPlanError)):
        __import__(
            "inferdrome.deployment", fromlist=["verify_gcp_execution_arm_bytes"]
        ).verify_gcp_execution_arm_bytes(
            canonical_gcp_execution_arm_bytes(other),
            expected_plan=plan,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
            now=NOW,
            expected_controller_id=arm.controller_id,
        )
    mutated = arm.model_copy(update={"max_controller_duration_seconds": 601})
    with pytest.raises(ValueError):
        parse_gcp_execution_arm_json(canonical_gcp_execution_arm_bytes(mutated))
    transport = FakeGcpComputeTransport()
    environment = _environment(plan)
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    quote, capacity = _quote_and_capacity(plan, arm, request)
    with pytest.raises(ValueError):
        _controller(tmp_path, transport).execute(
            plan=plan,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
            arm_bytes=canonical_gcp_execution_arm_bytes(mutated),
            environment=environment,
            quote=quote,
            capacity=capacity,
        )
    assert transport.insert_calls == 0


def test_request_is_private_and_tied_to_immutable_images() -> None:
    _spec, _inventory, _context, plan = _inputs()
    arm = _arm()
    environment = _environment(plan)
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    assert request.network.external_access_config == "absent"
    assert request.network.ip_forwarding is False
    assert request.boot_disk_auto_delete is True
    assert request.deletion_protection is False
    assert request.automatic_restart is False
    assert request.boot_image.digest.startswith("sha256:")
    assert request.plan_id == arm.plan_id
    assert len(request.labels.plan_id) <= 63
    assert ":" not in request.labels.plan_id
    assert gcp_execution_request_digest(request).startswith("sha256:")
    with pytest.raises(ValidationError):
        GcpExecutionNetwork(
            network="https://public.invalid",
            subnetwork="regions/us-central1/subnetworks/private",
            external_access_config="absent",
            ip_forwarding=False,
        )


def test_quote_fixed_point_boundary_and_incomplete_capacity_fail_closed() -> None:
    _spec, _inventory, _context, plan = _inputs()
    arm = _arm()
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=_environment(plan))
    quote, capacity = _quote_and_capacity(plan, arm, request)
    from inferdrome.deployment import validate_cost_and_capacity

    validate_cost_and_capacity(
        plan=plan, arm=arm, request=request, quote=quote, capacity=capacity, now=NOW
    )
    with pytest.raises(ValueError, match="exceeds"):
        validate_cost_and_capacity(
            plan=plan,
            arm=arm,
            request=request,
            quote=quote.model_copy(update={"safety_margin_microusd": 10**12}),
            capacity=capacity,
            now=NOW,
        )
    with pytest.raises(ValueError, match="capacity"):
        validate_cost_and_capacity(
            plan=plan,
            arm=arm,
            request=request,
            quote=quote,
            capacity=capacity.model_copy(update={"capacity_status": "unknown"}),
            now=NOW,
        )


def test_v2_supervisor_orders_approval_watchdog_and_intent_before_fake_create(
    tmp_path: Path,
) -> None:
    inputs = _v2_inputs()
    safety_root = tmp_path / "safety"
    watchdog = FakeGcpWatchdog()
    supervisor, approval = _v2_supervisor(
        safety_root, inputs[-1], watchdog=watchdog
    )

    # A direct exact fake remains an explicitly offline-only test route.  A
    # real v2 supervisor rejects generic lazy factories; those must instead
    # receive an opaque one-shot capability proof and return a sealed adapter.
    transport = FakeGcpComputeTransport()

    core_root = tmp_path / "core"
    core_root.mkdir()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
    )

    outcome = _execute_v2(controller, inputs)

    assert outcome.status == "SUCCEEDED", outcome.model_dump_json()
    assert approval.operator_confirmation == GCP_SUPERVISOR_CONFIRMATION
    assert watchdog.arm_calls == 1
    assert transport.insert_calls == 1
    assert supervisor.journal.load("ctl-12345678").state == "CLEANUP_CONFIRMED"


def test_v2_concrete_watchdog_factory_is_one_shot_and_request_bound(
    tmp_path: Path,
) -> None:
    """Exercise the only activation-capable route with local injected fakes.

    The seed lease supplies an externally observed test disk binding.  It is
    not used to infer a target in production code; all three persistent
    journals remain separate and the transport factory receives only the
    opaque post-watchdog proof.
    """

    inputs = _v2_inputs()
    startup_projection = _v2_startup_projection(inputs[-1])
    seed_root = tmp_path / "seed-core"
    seed_root.mkdir()
    _seed_outcome, seeded = _run(
        seed_root, FakeGcpComputeTransport(), return_controller=True
    )
    seed_record = seeded.journal.load("ctl-12345678")
    disk_binding = _v2_disk_binding_for_record(seed_record)

    watchdog_root = tmp_path / "watchdog"
    watchdog_root.mkdir()
    core_root = tmp_path / "core"
    core_root.mkdir()
    disk_journal_root = tmp_path / "disk-journal"
    disk_journal_root.mkdir()
    disk_provider = FileBackedFakeDiskProvider.initialize(
        tmp_path / "disk-provider", observation=disk_binding.disk
    )
    disk_cleanup_factory = GcpV2LocalDiskCleanupFactory(
        provider_supplier=lambda: disk_provider,
        journal_root=disk_journal_root,
        now_fn=lambda: NOW,
        startup_projection=startup_projection,
    )
    raw_transport = FakeGcpComputeTransport(
        exact_boot_disk_observation=disk_binding.disk,
        exact_boot_disk_inventory=disk_binding.owned_inventory,
        exact_owned_instance_observation=disk_binding.attached_instance,
    )
    activation_factory = GcpV2LocalTransportFactory(
        transport_supplier=lambda: raw_transport,
        now_fn=lambda: NOW,
        startup_projection=startup_projection,
    )
    watchdog = FileGcpWatchdog(
        watchdog_root,
        executor=GcpV2LocalWatchdogExecutorFactory(
            transport_factory=activation_factory,
            disk_cleanup_factory=disk_cleanup_factory,
            core_journal=GcpLeaseJournal(core_root),
            now_fn=lambda: NOW,
        ),
        runner_enabled=True,
        runner_poll_seconds=0.01,
        runner_now_fn=lambda: NOW,
    )
    supervisor, _approval = _v2_supervisor(
        tmp_path / "safety", inputs[-1], watchdog=watchdog
    )

    controller = GcpGuardedLifecycleController(
        activation_transport_factory=activation_factory,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
        exact_disk_cleanup_factory=disk_cleanup_factory,
    )

    outcome = _execute_v2(controller, inputs)

    assert outcome.status == "SUCCEEDED"
    assert raw_transport.insert_calls == 1
    assert (
        watchdog.run_due(
            controller_id="ctl-12345678", now=NOW + timedelta(minutes=10)
        ).state
        == "CLEANUP_NOT_REQUIRED"
    )

    assert raw_transport.insert_calls == 1


def test_v2_rejects_marker_generic_factory_fake_watchdog_and_fake_subclass(
    tmp_path: Path,
) -> None:
    inputs = _v2_inputs()
    supervisor, _approval = _v2_supervisor(tmp_path / "safety", inputs[-1])

    def factory() -> FakeGcpComputeTransport:
        raise AssertionError("generic factory must never be invoked")

    with pytest.raises(GcpExecutionError, match="GCP_V2_GENERIC_FACTORY_FORBIDDEN"):
        GcpGuardedLifecycleController(
            transport_factory=factory,
            arm_store=InMemoryExecutionArmStore(),
            journal=GcpLeaseJournal(tmp_path / "generic-core"),
            clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
            safety_supervisor=supervisor,
        )

    with pytest.raises(GcpExecutionError, match="GCP_V2_DURABLE_WATCHDOG_REQUIRED"):
        GcpGuardedLifecycleController(
            activation_transport_factory=lambda proof: proof,
            arm_store=InMemoryExecutionArmStore(),
            journal=GcpLeaseJournal(tmp_path / "fake-watchdog-core"),
            clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
            safety_supervisor=supervisor,
        )

    class FakeSubclass(FakeGcpComputeTransport):
        pass

    with pytest.raises(GcpExecutionError, match="GCP_V2_CAPABILITY_FACTORY_REQUIRED"):
        GcpGuardedLifecycleController(
            transport=FakeSubclass(),
            arm_store=InMemoryExecutionArmStore(),
            journal=GcpLeaseJournal(tmp_path / "subclass-core"),
            clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
            safety_supervisor=supervisor,
        )

    class ForgedMarker:
        requires_v2_capability_factories = True

    with pytest.raises(
        GcpExecutionError, match="GCP_V2_FACTORIES_REQUIRE_REAL_SUPERVISOR"
    ):
        GcpGuardedLifecycleController(
            activation_transport_factory=lambda proof: proof,
            arm_store=InMemoryExecutionArmStore(),
            journal=GcpLeaseJournal(tmp_path / "marker-core"),
            clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
            safety_supervisor=ForgedMarker(),
        )


def test_v2_approval_substitution_blocks_before_future_factory(tmp_path: Path) -> None:
    inputs = _v2_inputs()
    other_inputs = _v2_inputs("ctl-87654321")
    safety_root = tmp_path / "safety"
    supervisor, _approval = _v2_supervisor(safety_root, other_inputs[-1])
    transport = FakeGcpComputeTransport()

    core_root = tmp_path / "core"
    core_root.mkdir()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
    )

    outcome = _execute_v2(controller, inputs)

    assert outcome.status == "FAILED"
    # The opaque startup projection binds the exact request before the later
    # approval/deadline checks, so a substituted controller identity fails at
    # the earliest exact binding boundary.
    assert outcome.primary_error_code == "STARTUP_PROJECTION_BINDING_MISMATCH"
    assert outcome.arm_consumed is False
    assert outcome.cleanup_confirmed is True
    assert transport.insert_calls == 0


def test_v2_opaque_payload_projection_is_domain_separated_and_closed() -> None:
    """The v2 overlay binds opaque bytes without retaining their semantics."""

    preflight = _v2_inputs()[-1]
    payload = b"opaque-local-payload-bytes"
    projection = issue_gcp_v2_startup_projection(
        request=preflight.request, execution_payload=payload
    )

    assert gcp_v2_execution_payload_digest(payload) == digest_bytes(
        DigestDomain.GCP_EXECUTION_PAYLOAD, payload
    )
    assert gcp_v2_execution_payload_digest(payload) != digest_bytes(
        DigestDomain.GCP_EXECUTION_APPROVAL, payload
    )
    assert projection.startup_script_digest == preflight.request.startup_script_digest
    projection_bytes = canonical_gcp_v2_startup_projection_bytes(projection)
    assert payload not in projection_bytes
    assert b"execution_payload_digest" in projection_bytes

    missing_field = json.loads(projection_bytes)
    missing_field.pop("execution_payload_digest")
    with pytest.raises(GcpV2ContractError, match="STARTUP_PROJECTION_INVALID"):
        parse_gcp_v2_startup_projection_json(canonical_json_bytes(missing_field))


def test_v2_activation_deadline_keeps_runtime_distinct_from_setup_margin(
    tmp_path: Path,
) -> None:
    """Activation needs setup/auth headroom, not a pre-expired full runtime."""

    inputs = _v2_inputs()
    supervisor, approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    contract = supervisor.activation_deadline
    activation_at = NOW + timedelta(seconds=contract.setup_margin_seconds - 1)

    capability = issue_gcp_v2_mutation_capability(
        contract=contract,
        approval_digest=gcp_execution_approval_digest(approval),
        now=activation_at,
    )

    provider_deadline = datetime.fromisoformat(
        str(capability.provider_runtime_deadline_at).replace("Z", "+00:00")
    )
    setup_deadline = datetime.fromisoformat(
        str(contract.setup_deadline_at).replace("Z", "+00:00")
    )
    assert provider_deadline == activation_at + timedelta(
        seconds=contract.provider_runtime_seconds
    )
    assert provider_deadline > setup_deadline
    with pytest.raises(GcpV2ContractError, match="ACTIVATION_SETUP_HORIZON_EXPIRED"):
        issue_gcp_v2_mutation_capability(
            contract=contract,
            approval_digest=gcp_execution_approval_digest(approval),
            now=setup_deadline,
        )


def test_v2_payload_substitution_blocks_supplier_before_factory(tmp_path: Path) -> None:
    """A changed opaque digest cannot even initialize a future supplier."""

    inputs = _v2_inputs()
    supervisor, approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    substituted_projection = issue_gcp_v2_startup_projection(
        request=inputs[-1].request,
        execution_payload=b"different-opaque-local-payload",
    )
    capability = issue_gcp_v2_mutation_capability(
        contract=supervisor.activation_deadline,
        approval_digest=gcp_execution_approval_digest(approval),
        now=NOW,
    )
    supplier_calls = 0

    def supplier() -> FakeGcpComputeTransport:
        nonlocal supplier_calls
        supplier_calls += 1
        return FakeGcpComputeTransport()

    with pytest.raises(
        GcpTransportError, match="WATCHDOG_CLEANUP_PAYLOAD_MISMATCH"
    ):
        bind_gcp_v2_watchdog_cleanup_transport(
            transport_supplier=supplier,
            capability=capability,
            startup_projection=substituted_projection,
            now=NOW,
            now_fn=lambda: NOW,
        )

    assert supplier_calls == 0


def test_v2_public_capability_factory_checks_payload_before_sdk_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exported future seam cannot reach SDK/client construction on mismatch."""

    inputs = _v2_inputs()
    supervisor, approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    _outcome, seeded = _run(
        seed_root, FakeGcpComputeTransport(), return_controller=True
    )
    record = seeded.journal.load("ctl-12345678")
    binding = gcp_supervisor_binding(approval, record)
    capability = issue_gcp_v2_mutation_capability(
        contract=supervisor.activation_deadline,
        approval_digest=gcp_execution_approval_digest(approval),
        now=NOW,
    )
    proof = GcpV2ActivatedMutationProof(
        capability=capability,
        receipt=FakeGcpWatchdog().activate(
            binding, approval=approval, capability=capability, now=NOW
        ),
        _seal=_MUTATION_ACTIVATION_PROOF_SEAL,
        _use=_MutationProofUse(),
    )
    substituted_projection = issue_gcp_v2_startup_projection(
        request=inputs[-1].request,
        execution_payload=b"substituted-public-factory-payload",
    )
    boundary_calls = 0

    def unexpected_sdk_boundary(**_: Any) -> Any:
        nonlocal boundary_calls
        boundary_calls += 1
        raise AssertionError("payload mismatch reached SDK/client boundary")

    import inferdrome.deployment.gcp_compute_transport as transport_module

    monkeypatch.setattr(
        transport_module, "create_google_compute_transport", unexpected_sdk_boundary
    )
    with pytest.raises(GcpTransportError, match="MUTATION_CAPABILITY_PAYLOAD_MISMATCH"):
        create_google_compute_transport_for_v2_capability(
            capability=proof,
            startup_projection=substituted_projection,
            now=NOW,
        )

    assert boundary_calls == 0


def test_v2_disk_adapter_rechecks_payload_before_lazy_provider_supplier(
    tmp_path: Path,
) -> None:
    """A post-bind payload substitution cannot wake a deferred disk provider."""

    inputs = _v2_inputs()
    supervisor, approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    core_root = tmp_path / "core"
    core_root.mkdir()
    _outcome, seeded = _run(
        core_root, FakeGcpComputeTransport(), return_controller=True
    )
    record = seeded.journal.load("ctl-12345678")
    binding = _v2_disk_binding_for_record(record)
    capability = issue_gcp_v2_mutation_capability(
        contract=supervisor.activation_deadline,
        approval_digest=gcp_execution_approval_digest(approval),
        now=NOW,
    )
    supplier_calls = 0

    def supplier() -> FileBackedFakeDiskProvider:
        nonlocal supplier_calls
        supplier_calls += 1
        return FileBackedFakeDiskProvider.initialize(
            tmp_path / "provider", observation=binding.disk
        )

    disk_root = tmp_path / "disk-journal"
    disk_root.mkdir()
    coordinator = GcpV2LocalDiskCleanupFactory(
        provider_supplier=supplier,
        journal_root=disk_root,
        now_fn=lambda: NOW,
        startup_projection=supervisor.startup_projection,
    ).bind(record=record, binding=binding, authority=capability, now=NOW)
    tampered = capability.model_dump(mode="json")
    tampered["execution_payload_digest"] = gcp_v2_execution_payload_digest(
        b"tampered-deferred-disk-payload"
    )
    tampered["capability_id"] = "sha256:" + "0" * 64
    provisional = GcpV2MutationCapability.model_construct(**tampered)
    tampered["capability_id"] = gcp_v2_mutation_capability_id(provisional)
    coordinator.provider._authority = GcpV2MutationCapability.model_validate_json(
        canonical_json_bytes(tampered)
    )

    with pytest.raises(GcpV2DiskCleanupError, match="DISK_CAPABILITY_PAYLOAD_MISMATCH"):
        coordinator.provider.read_exact_owned_boot_disk(binding, timeout_seconds=1)

    assert supplier_calls == 0


def test_v2_tampered_watchdog_receipt_payload_is_rejected(tmp_path: Path) -> None:
    """The sealed proof checks the receipt's opaque payload binding too."""

    inputs = _v2_inputs()
    supervisor, approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    _outcome, seeded = _run(
        seed_root, FakeGcpComputeTransport(), return_controller=True
    )
    record = seeded.journal.load("ctl-12345678")
    binding = gcp_supervisor_binding(approval, record)
    capability = issue_gcp_v2_mutation_capability(
        contract=supervisor.activation_deadline,
        approval_digest=gcp_execution_approval_digest(approval),
        now=NOW,
    )
    receipt = FakeGcpWatchdog().activate(
        binding, approval=approval, capability=capability, now=NOW
    )
    proof = GcpV2ActivatedMutationProof(
        capability=capability,
        receipt=receipt,
        _seal=_MUTATION_ACTIVATION_PROOF_SEAL,
        _use=_MutationProofUse(),
    )
    assert validate_gcp_v2_activated_mutation_proof(proof, now=NOW) == proof

    tampered = receipt.model_dump(mode="json")
    tampered["execution_payload_digest"] = gcp_v2_execution_payload_digest(
        b"substituted-receipt-payload"
    )
    tampered["receipt_id"] = "sha256:" + "0" * 64
    provisional = GcpWatchdogActivationReceipt.model_construct(**tampered)
    tampered["receipt_id"] = gcp_watchdog_activation_receipt_id(provisional)
    tampered_receipt = GcpWatchdogActivationReceipt.model_validate_json(
        canonical_json_bytes(tampered)
    )
    tampered_proof = GcpV2ActivatedMutationProof(
        capability=capability,
        receipt=tampered_receipt,
        _seal=_MUTATION_ACTIVATION_PROOF_SEAL,
        _use=_MutationProofUse(),
    )

    with pytest.raises(GcpSupervisorError, match="MUTATION_PROOF_BINDING_MISMATCH"):
        validate_gcp_v2_activated_mutation_proof(tampered_proof, now=NOW)

def test_v2_read_only_cost_guard_binds_rate_cap_and_blocks_substitution(
    tmp_path: Path,
) -> None:
    inputs = _v2_inputs()
    supervisor, approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    quote_basis = supervisor.quote_basis
    guard = supervisor.cost_guard

    assert quote_basis.currency == "USD"
    assert quote_basis.quote_authority == "operator_supplied_read_only_quote"
    assert quote_basis.invoice_truth == "unavailable_external_provider_invoice"
    assert (
        sum(component.maximum_microusd for component in quote_basis.component_rates)
        + quote_basis.safety_margin_microusd
        == quote_basis.worst_case_microusd
    )
    assert guard.quote_authority == "read_only_quote_only"
    assert guard.capacity_authority == "read_only_capacity_only"
    assert guard.provider_billing_enforcement is False
    assert guard.estimated_max_microusd <= guard.hard_ceiling_microusd

    invalid_guard = json.loads(canonical_gcp_cost_cleanup_guard_bytes(guard))
    invalid_guard["estimated_max_microusd"] = (
        invalid_guard["hard_ceiling_microusd"] + 1
    )
    with pytest.raises(GcpCostGuardError, match="COST_GUARD_INVALID"):
        parse_gcp_cost_cleanup_guard_json(json.dumps(invalid_guard))

    tampered_guard = guard.model_copy(update={"max_cleanup_attempts": 2})
    safety_root = tmp_path / "tampered-safety"
    safety_root.mkdir()
    tampered_supervisor = GcpLifecycleSupervisor(
        approval=approval,
        quote_basis=quote_basis,
        cost_guard=tampered_guard,
        activation_deadline=supervisor.activation_deadline,
        startup_projection=supervisor.startup_projection,
        journal=GcpSupervisorJournal(safety_root),
        watchdog=FakeGcpWatchdog(),
        kill_switch=InMemoryGcpKillSwitch(),
    )
    transport = FakeGcpComputeTransport()

    core_root = tmp_path / "core"
    core_root.mkdir()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=tampered_supervisor,
    )

    outcome = _execute_v2(controller, inputs)

    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "ACTIVATION_CONTRACT_BINDING_MISMATCH"
    assert outcome.arm_consumed is False
    assert outcome.provider_mutation_attempted is False
    assert transport.insert_calls == 0


def test_v2_rate_basis_is_component_order_canonical_and_parser_closed() -> None:
    inputs = _v2_inputs()
    preflight = inputs[-1]
    reordered_preflight = preflight.__class__(
        arm=preflight.arm,
        request=preflight.request,
        quote=preflight.quote.model_copy(
            update={"components": tuple(reversed(preflight.quote.components))}
        ),
        capacity=preflight.capacity,
        arm_sha256=preflight.arm_sha256,
    )
    basis = issue_gcp_read_only_quote_basis(preflight=preflight)

    assert issue_gcp_read_only_quote_basis(preflight=reordered_preflight) == basis
    raw = canonical_gcp_read_only_quote_basis_bytes(basis)
    assert parse_gcp_read_only_quote_basis_json(raw) == basis
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


def test_v2_watchdog_receipt_must_cover_quoted_cleanup_tail(
    tmp_path: Path,
) -> None:
    class RuntimeOnlyWatchdog(FakeGcpWatchdog):
        def arm(self, binding: Any, *, approval: Any, now: datetime) -> Any:
            receipt = super().arm(binding, approval=approval, now=now)
            return receipt.model_copy(
                update={"expires_at": approval.controller_deadline_at}
            )

    inputs = _v2_inputs()
    watchdog = RuntimeOnlyWatchdog()
    supervisor, approval = _v2_supervisor(
        tmp_path / "safety", inputs[-1], watchdog=watchdog
    )
    expected_watchdog_deadline = NOW + timedelta(
        seconds=inputs[-1].quote.billable_duration_seconds + 60
    )
    assert approval.watchdog_deadline_at == expected_watchdog_deadline.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    transport = FakeGcpComputeTransport()

    core_root = tmp_path / "core"
    core_root.mkdir()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
    )

    outcome = _execute_v2(controller, inputs)

    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "WATCHDOG_BINDING_MISMATCH"
    assert outcome.arm_consumed is True
    assert outcome.provider_mutation_attempted is False
    assert transport.insert_calls == 0
    assert watchdog.arm_calls == 1


def test_v2_elapsed_setup_beyond_explicit_margin_blocks_factory(
    tmp_path: Path,
) -> None:
    inputs = _v2_inputs()
    supervisor, _approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    clock_values = iter((NOW, NOW, NOW + timedelta(seconds=61)))
    transport = FakeGcpComputeTransport()

    core_root = tmp_path / "core"
    core_root.mkdir()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(
            now_fn=lambda: next(clock_values, NOW + timedelta(seconds=61)),
            monotonic_fn=lambda: 0.0,
        ),
        safety_supervisor=supervisor,
    )

    outcome = _execute_v2(controller, inputs)

    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "ACTIVATION_SETUP_HORIZON_EXPIRED"
    assert outcome.arm_consumed is True
    assert outcome.provider_mutation_attempted is False
    assert transport.insert_calls == 0


def test_v2_deadline_is_rechecked_before_work_and_triggers_cleanup(
    tmp_path: Path,
) -> None:
    inputs = _v2_inputs()
    supervisor, _approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    current_time = [NOW]
    transport = FakeGcpComputeTransport()
    original_boot_disk = transport.get_boot_disk

    def advance_past_runtime(_: Any, *, timeout_seconds: int) -> Any:
        current_time[0] = NOW + timedelta(minutes=11)
        return original_boot_disk(_, timeout_seconds=timeout_seconds)

    # Keep the exact fake type (rather than a subclass) while advancing the
    # local clock immediately before the supervisor's post-create work gate.
    transport.get_boot_disk = advance_past_runtime  # type: ignore[method-assign]
    core_root = tmp_path / "core"
    core_root.mkdir()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(
            now_fn=lambda: current_time[0],
            monotonic_fn=lambda: 0.0,
        ),
        safety_supervisor=supervisor,
    )
    work_called = False

    def work(_: Any) -> None:
        nonlocal work_called
        work_called = True

    outcome = _execute_v2(controller, inputs, work=work)

    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "PROVIDER_RUNTIME_DEADLINE_EXPIRED"
    assert outcome.cleanup_confirmed is True
    assert work_called is False
    assert transport.insert_calls == 1
    assert transport.delete_calls == 1


def test_v2_final_create_rechecks_stale_capacity_without_insert(
    tmp_path: Path,
) -> None:
    base_inputs = _v2_inputs()
    fresh_capacity = base_inputs[7].model_copy(
        update={"observed_at": "2026-08-25T12:00:00Z", "freshness_seconds": 1}
    )
    inputs = _v2_inputs_with_observations(base_inputs, capacity=fresh_capacity)
    supervisor, _approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    clock_values = iter(
        (NOW, NOW, NOW, NOW + timedelta(seconds=2))
    )
    transport = FakeGcpComputeTransport()

    core_root = tmp_path / "core"
    core_root.mkdir()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(
            now_fn=lambda: next(clock_values, NOW + timedelta(seconds=2)),
            monotonic_fn=lambda: 0.0,
        ),
        safety_supervisor=supervisor,
    )

    outcome = _execute_v2(controller, inputs)

    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "PREFLIGHT_CAPACITY_STALE"
    assert outcome.arm_consumed is True
    assert transport.insert_calls == 0


@pytest.mark.parametrize(
    ("clock_offset", "expected_error"),
    [
        (timedelta(seconds=2), "PREFLIGHT_QUOTE_WINDOW_INVALID"),
        (timedelta(minutes=5), "APPROVAL_EXPIRED"),
    ],
)
def test_v2_elapsed_read_only_or_approval_input_blocks_factory(
    tmp_path: Path, clock_offset: timedelta, expected_error: str
) -> None:
    base_inputs = _v2_inputs()
    quote = base_inputs[6]
    if expected_error == "PREFLIGHT_QUOTE_WINDOW_INVALID":
        quote = quote.model_copy(
            update={
                "issued_at": "2026-08-25T12:00:00Z",
                "valid_until": "2026-08-25T12:00:01Z",
                "freshness_seconds": 1,
            }
        )
    inputs = _v2_inputs_with_observations(base_inputs, quote=quote)
    supervisor, _approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    clock_values = iter((NOW, NOW, NOW + clock_offset))
    transport = FakeGcpComputeTransport()

    core_root = tmp_path / "core"
    core_root.mkdir()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(
            now_fn=lambda: next(clock_values, NOW + clock_offset),
            monotonic_fn=lambda: 0.0,
        ),
        safety_supervisor=supervisor,
    )

    outcome = _execute_v2(controller, inputs)

    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == expected_error
    assert outcome.arm_consumed is True
    assert outcome.provider_mutation_attempted is False
    assert transport.insert_calls == 0


def test_future_factory_and_nonfake_transport_require_safety_supervisor(
    tmp_path: Path,
) -> None:
    factory_called = False

    def factory() -> FakeGcpComputeTransport:
        nonlocal factory_called
        factory_called = True
        return FakeGcpComputeTransport()

    with pytest.raises(GcpExecutionError, match="GCP_LAZY_FACTORY_REQUIRES_SUPERVISOR"):
        GcpGuardedLifecycleController(
            transport_factory=factory,
            arm_store=InMemoryExecutionArmStore(),
            journal=GcpLeaseJournal(tmp_path),
            clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        )
    assert factory_called is False

    non_fake_transport: Any = object()
    with pytest.raises(GcpExecutionError, match="GCP_SUPERVISOR_REQUIRED"):
        GcpGuardedLifecycleController(
            transport=non_fake_transport,
            arm_store=InMemoryExecutionArmStore(),
            journal=GcpLeaseJournal(tmp_path),
            clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        )


def test_v2_watchdog_failure_consumes_arm_but_blocks_future_factory(
    tmp_path: Path,
) -> None:
    inputs = _v2_inputs()
    watchdog = FakeGcpWatchdog(arm_error="WATCHDOG_ARM_FAILED")
    supervisor, _approval = _v2_supervisor(
        tmp_path / "safety", inputs[-1], watchdog=watchdog
    )
    transport = FakeGcpComputeTransport()

    core_root = tmp_path / "core"
    core_root.mkdir()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
    )

    outcome = _execute_v2(controller, inputs)

    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "WATCHDOG_ARM_FAILED"
    assert outcome.arm_consumed is True
    assert outcome.provider_mutation_attempted is False
    assert outcome.cleanup_confirmed is True
    assert transport.insert_calls == 0
    assert watchdog.arm_calls == 1
    assert supervisor.journal.load("ctl-12345678").state == "CLEANUP_CONFIRMED"


@pytest.mark.parametrize(
    ("fail_on_update", "primary_error"),
    [
        (1, "ARM_JOURNAL_UPDATE_FAILED"),
        (2, "CREATE_INTENT_JOURNAL_FAILED"),
    ],
)
def test_v2_core_journal_boundary_failure_blocks_without_insert(
    tmp_path: Path,
    fail_on_update: int,
    primary_error: str,
) -> None:
    class BrokenJournal(GcpLeaseJournal):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.update_calls = 0

        def update(self, record: Any) -> None:
            self.update_calls += 1
            if self.update_calls >= fail_on_update:
                raise GcpJournalError("simulated journal failure")
            super().update(record)

    inputs = _v2_inputs()
    supervisor, _approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    core_root = tmp_path / "core"
    core_root.mkdir()
    transport = FakeGcpComputeTransport()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=BrokenJournal(core_root),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
    )

    outcome = _execute_v2(controller, inputs)

    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == primary_error
    assert outcome.error_code == "JOURNAL_UPDATE_FAILED"
    assert outcome.cleanup_confirmed is False
    assert transport.insert_calls == 0
    assert supervisor.journal.load("ctl-12345678").state == "BLOCKED"


def test_v2_kill_switch_blocks_after_arm_before_future_factory(tmp_path: Path) -> None:
    inputs = _v2_inputs()
    kill_switch = InMemoryGcpKillSwitch(killed=True)
    supervisor, _approval = _v2_supervisor(
        tmp_path / "safety", inputs[-1], kill_switch=kill_switch
    )
    transport = FakeGcpComputeTransport()

    core_root = tmp_path / "core"
    core_root.mkdir()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
    )

    outcome = _execute_v2(controller, inputs)

    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "KILL_SWITCH_ACTIVE"
    assert outcome.arm_consumed is True
    assert outcome.provider_mutation_attempted is False
    assert outcome.cleanup_confirmed is True
    assert transport.insert_calls == 0
    assert kill_switch.checks == 1
    assert supervisor.journal.load("ctl-12345678").state == "CLEANUP_CONFIRMED"


def test_v2_kill_switch_is_rechecked_before_work_and_cleans_exact_lease(
    tmp_path: Path,
) -> None:
    class FlipKillSwitch:
        def __init__(self) -> None:
            self.checks = 0

        def is_killed(self, binding: Any) -> bool:
            del binding
            self.checks += 1
            # Arming, capability issuance, and pre-work each independently
            # consult the local switch; the third check is the post-create
            # gate in the direct fake-only path.
            return self.checks >= 3

    inputs = _v2_inputs()
    kill_switch = FlipKillSwitch()
    supervisor, _approval = _v2_supervisor(
        tmp_path / "safety", inputs[-1], kill_switch=kill_switch
    )
    core_root = tmp_path / "core"
    core_root.mkdir()
    transport = FakeGcpComputeTransport()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
    )
    work_called = False

    def work(_: Any) -> None:
        nonlocal work_called
        work_called = True

    outcome = _execute_v2(controller, inputs, work=work)

    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "KILL_SWITCH_ACTIVE"
    assert outcome.cleanup_confirmed is True
    assert work_called is False
    assert kill_switch.checks == 3
    assert transport.insert_calls == 1
    assert transport.delete_calls == 1
    assert supervisor.journal.load("ctl-12345678").state == "CLEANUP_CONFIRMED"


def test_v2_file_kill_switch_is_exact_bound_and_fails_closed(
    tmp_path: Path,
) -> None:
    inputs = _v2_inputs()
    _supervisor, approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    core_root = tmp_path / "core"
    core_root.mkdir()
    outcome, controller = _run(
        core_root, FakeGcpComputeTransport(), return_controller=True
    )
    assert outcome.status == "SUCCEEDED"
    binding = gcp_supervisor_binding(
        approval, controller.journal.load("ctl-12345678")
    )
    kill_root = tmp_path / "kill"
    kill_root.mkdir()
    marker = issue_gcp_kill_switch_record(
        binding,
        operator_identity="operator-alice",
        now=NOW,
        confirmation=GCP_KILL_SWITCH_CONFIRMATION,
    )
    marker_path = kill_root / "ctl-12345678.kill-v2.json"
    marker_path.write_bytes(canonical_gcp_kill_switch_bytes(marker))
    switch = FileGcpKillSwitch(kill_root)

    assert switch.is_killed(binding) is True
    with pytest.raises(GcpSupervisorError, match="KILL_SWITCH_BINDING_MISMATCH"):
        switch.is_killed(
            binding.model_copy(update={"request_digest": "sha256:" + "f" * 64})
        )

    symlink_root = tmp_path / "symlink-kill"
    symlink_root.mkdir()
    os.symlink(marker_path, symlink_root / "ctl-12345678.kill-v2.json")
    with pytest.raises(GcpSupervisorError, match="KILL_SWITCH"):
        FileGcpKillSwitch(symlink_root).is_killed(binding)


def test_v2_approval_parser_and_sidecar_journal_repair_fail_closed(
    tmp_path: Path,
) -> None:
    inputs = _v2_inputs()
    safety_root = tmp_path / "safety"
    supervisor, approval = _v2_supervisor(safety_root, inputs[-1])
    approval_raw = canonical_gcp_execution_approval_bytes(approval)
    assert parse_gcp_execution_approval_json(approval_raw) == approval
    duplicate = approval_raw.replace(
        b'"provider":"gcp-compute-engine"',
        b'"provider":"gcp-compute-engine","provider":"gcp-compute-engine"',
        1,
    )
    with pytest.raises(GcpSupervisorError, match="APPROVAL_INVALID"):
        parse_gcp_execution_approval_json(duplicate)
    unknown = json.loads(approval_raw)
    unknown["unexpected"] = True
    with pytest.raises(GcpSupervisorError, match="APPROVAL_INVALID"):
        parse_gcp_execution_approval_json(json.dumps(unknown))

    core_root = tmp_path / "core"
    core_root.mkdir()
    outcome, controller = _run(
        core_root, FakeGcpComputeTransport(), return_controller=True
    )
    assert outcome.status == "SUCCEEDED"
    binding = gcp_supervisor_binding(
        approval, controller.journal.load("ctl-12345678")
    )
    supervisor.journal.reserve(binding, now=NOW)
    path = safety_root / "ctl-12345678.safety-v2.events.jsonl"
    path.write_bytes(path.read_bytes() + b'{"partial"')
    assert supervisor.journal.load("ctl-12345678").state == "PREPARED"
    with pytest.raises(GcpSupervisorError, match="SUPERVISOR_UNRESOLVED_LEASE"):
        supervisor.journal.reserve(binding, now=NOW)


@pytest.mark.parametrize(
    ("crash_point", "expected_state"),
    [
        ("reserve_after_event_fsync", "PREPARED"),
        ("reserve_after_directory_fsync", "PREPARED"),
        ("advance_after_event_fsync", "ARM_CONSUMED"),
        ("advance_after_directory_fsync", "ARM_CONSUMED"),
    ],
)
def test_v2_sidecar_crash_prefix_is_durable_and_restartable(
    tmp_path: Path, crash_point: str, expected_state: str
) -> None:
    inputs = _v2_inputs()
    core_root = tmp_path / "core"
    core_root.mkdir()
    outcome, controller = _run(
        core_root, FakeGcpComputeTransport(), return_controller=True
    )
    assert outcome.status == "SUCCEEDED"
    supervisor, approval = _v2_supervisor(tmp_path / "safety", inputs[-1])
    binding = gcp_supervisor_binding(
        approval, controller.journal.load("ctl-12345678")
    )
    if crash_point.startswith("advance"):
        supervisor.journal.reserve(binding, now=NOW)

    pid = os.fork()
    if pid == 0:
        journal = GcpSupervisorJournal(
            tmp_path / "safety",
            crash_hook=lambda point: os._exit(77)
            if point == crash_point
            else None,
        )
        if crash_point.startswith("reserve"):
            journal.reserve(binding, now=NOW)
        else:
            journal.advance(binding, state="ARM_CONSUMED", now=NOW)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 77
    assert (
        GcpSupervisorJournal(tmp_path / "safety").load("ctl-12345678").state
        == expected_state
    )


@pytest.mark.parametrize("state", ["PREPARED", "ARM_CONSUMED"])
def test_v2_no_provider_recovery_converges_each_pre_mutation_core_state(
    tmp_path: Path, state: str
) -> None:
    """A restart settles both journals before any factory/provider boundary."""

    inputs = _v2_inputs()
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    _seed_outcome, seed_controller = _run(
        seed_root, FakeGcpComputeTransport(), return_controller=True
    )
    seeded = seed_controller.journal.load("ctl-12345678")
    values = seeded.model_dump(mode="python")
    values.update(
        {
            "state": "PREPARED",
            "arm_consumed": False,
            "provider_mutation_attempted": False,
            "provider_mutation_ambiguous": False,
            "provider_operation_id": None,
            "provider_operation_name": None,
            "provider_operation_kind": None,
            "provider_operation_status": None,
            "provider_operation_terminal": False,
            "cleanup_confirmed": False,
            "orphaned": False,
            "delete_attempts": 0,
            "last_error_code": None,
        }
    )
    prepared = type(seeded).model_validate(values)
    core_root = tmp_path / "core"
    core_root.mkdir()
    core_journal = GcpLeaseJournal(core_root)
    core_journal.reserve(prepared)
    if state == "ARM_CONSUMED":
        armed_values = prepared.model_dump(mode="python")
        armed_values.update({"state": "ARM_CONSUMED", "arm_consumed": True})
        core_journal.update(type(prepared).model_validate(armed_values))

    watchdog = FakeGcpWatchdog()
    supervisor, _approval = _v2_supervisor(
        tmp_path / "safety", inputs[-1], watchdog=watchdog
    )
    transport = FakeGcpComputeTransport()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=core_journal,
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
    )

    recovered = controller.recover_cleanup(controller_id="ctl-12345678")

    assert recovered.status == "CANCELLED"
    assert recovered.primary_error_code == "NO_PROVIDER_MUTATION_RECOVERED"
    assert recovered.provider_mutation_attempted is False
    assert recovered.cleanup_confirmed is True
    assert transport.insert_calls == 0
    assert transport.delete_calls == 0
    assert watchdog.activation_calls == 0
    assert core_journal.load("ctl-12345678").state == "CLEANUP_CONFIRMED"
    assert supervisor.journal.load("ctl-12345678").state == "CLEANUP_CONFIRMED"

    repeated = controller.recover_cleanup(controller_id="ctl-12345678")
    assert repeated.primary_error_code == "CLEANUP_ALREADY_CONFIRMED"
    assert transport.insert_calls == 0
    assert transport.delete_calls == 0


def test_v2_file_watchdog_core_fence_survives_crash_and_settles_without_provider(
    tmp_path: Path,
) -> None:
    """A crashed intent cannot outlive authoritative core cleanup indefinitely."""

    inputs = _v2_inputs()
    startup_projection = _v2_startup_projection(inputs[-1])
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    _seed_outcome, seed_controller = _run(
        seed_root, FakeGcpComputeTransport(), return_controller=True
    )
    record = seed_controller.journal.load("ctl-12345678")

    watchdog_root = tmp_path / "watchdog"
    watchdog_root.mkdir()
    disk_journal_root = tmp_path / "disk-journal"
    disk_journal_root.mkdir()
    provider_calls = 0

    def transport_supplier() -> FakeGcpComputeTransport:
        nonlocal provider_calls
        provider_calls += 1
        return FakeGcpComputeTransport()

    def disk_provider_supplier() -> Any:
        raise AssertionError("disk supplier must not run behind a terminal fence")

    transport_factory = GcpV2LocalTransportFactory(
        transport_supplier=transport_supplier,
        now_fn=lambda: NOW,
        startup_projection=startup_projection,
    )
    disk_factory = GcpV2LocalDiskCleanupFactory(
        provider_supplier=disk_provider_supplier,
        journal_root=disk_journal_root,
        now_fn=lambda: NOW,
        startup_projection=startup_projection,
    )
    executor = GcpV2LocalWatchdogExecutorFactory(
        transport_factory=transport_factory,
        disk_cleanup_factory=disk_factory,
        core_journal=GcpLeaseJournal(seed_root),
        now_fn=lambda: NOW,
    )
    watchdog = FileGcpWatchdog(
        watchdog_root,
        executor=executor,
        runner_enabled=True,
        runner_poll_seconds=0.01,
        runner_now_fn=lambda: NOW,
    )
    supervisor, approval = _v2_supervisor(
        tmp_path / "safety", inputs[-1], watchdog=watchdog
    )
    binding = gcp_supervisor_binding(approval, record)
    watchdog.configure_lease(record)
    watchdog.arm(binding, approval=approval, now=NOW)
    capability = issue_gcp_v2_mutation_capability(
        contract=supervisor.activation_deadline,
        approval_digest=gcp_execution_approval_digest(approval),
        now=NOW,
    )
    watchdog.activate(binding, approval=approval, capability=capability, now=NOW)
    due = datetime.fromisoformat(
        str(capability.provider_runtime_deadline_at).replace("Z", "+00:00")
    )
    runner_pid: int | None = None
    try:
        with watchdog._exclusive() as root:
            runner_pid = watchdog._read_locked(root, binding.controller_id)[
                -1
            ].runner_process_id
        crash_pid = os.fork()
        if crash_pid == 0:
            crashed = FileGcpWatchdog(
                watchdog_root,
                executor=executor,
                crash_hook=lambda point: os._exit(77)
                if point == "file_watchdog_after_cleanup_intent"
                else None,
                runner_enabled=True,
                runner_poll_seconds=0.01,
                runner_now_fn=lambda: NOW,
            )
            crashed.run_due(controller_id=binding.controller_id, now=due)
            os._exit(1)
        _, status = os.waitpid(crash_pid, 0)
        assert os.waitstatus_to_exitcode(status) == 77

        with pytest.raises(
            GcpSupervisorError, match="FILE_WATCHDOG_CORE_TERMINAL_PENDING"
        ):
            supervisor.reconcile_core_terminal_sidecar(record, now=due)
        assert provider_calls == 0

        restarted = FileGcpWatchdog(
            watchdog_root,
            executor=executor,
            runner_enabled=True,
            runner_poll_seconds=0.01,
            runner_now_fn=lambda: NOW,
        )
        fenced = restarted.run_due(
            controller_id=binding.controller_id, now=due + timedelta(seconds=1)
        )
        assert fenced.state == "PREBIND_CLEANUP_INTENT"
        assert fenced.core_terminal_fenced_at is not None
        assert provider_calls == 0

        settled_at = due + timedelta(seconds=record.cleanup_timeout_seconds + 1)
        settled = restarted.run_due(
            controller_id=binding.controller_id, now=settled_at
        )
        assert settled.state == "CLEANUP_NOT_REQUIRED"
        assert provider_calls == 0

        resumed_supervisor = GcpLifecycleSupervisor(
            approval=approval,
            quote_basis=supervisor.quote_basis,
            cost_guard=supervisor.cost_guard,
            activation_deadline=supervisor.activation_deadline,
            startup_projection=supervisor.startup_projection,
            journal=GcpSupervisorJournal(tmp_path / "safety"),
            watchdog=restarted,
            kill_switch=InMemoryGcpKillSwitch(),
        )
        resumed_supervisor.reconcile_core_terminal_sidecar(record, now=settled_at)
        assert provider_calls == 0
        assert (
            resumed_supervisor.journal.load(binding.controller_id).state
            == "CLEANUP_CONFIRMED"
        )
    finally:
        if runner_pid is not None:
            watchdog._kill_runner(runner_pid)


def test_v2_file_watchdog_hung_cleanup_is_bounded_and_restart_fences_locally(
    tmp_path: Path,
) -> None:
    """A hung local cleanup child is killed and cannot permanently block restart."""

    inputs = _v2_inputs()
    record = _prepared_record(tmp_path / "prepared").model_copy(
        update={"cleanup_timeout_seconds": 1}
    )
    core_root = tmp_path / "core"
    core_root.mkdir()
    core_journal = GcpLeaseJournal(core_root)
    core_journal.reserve(record)
    startup_projection = _v2_startup_projection(inputs[-1])

    watchdog_root = tmp_path / "watchdog"
    watchdog_root.mkdir()
    disk_journal_root = tmp_path / "disk-journal"
    disk_journal_root.mkdir()
    supplier_marker = tmp_path / "unexpected-transport-supplier"
    disk_supplier_marker = tmp_path / "unexpected-disk-supplier"

    def transport_supplier() -> FakeGcpComputeTransport:
        supplier_marker.write_text("called", encoding="ascii")
        return FakeGcpComputeTransport()

    def disk_provider_supplier() -> Any:
        disk_supplier_marker.write_text("called", encoding="ascii")
        raise AssertionError("a hung prebind worker must not reach disk cleanup")

    transport_factory = GcpV2LocalTransportFactory(
        transport_supplier=transport_supplier,
        now_fn=lambda: NOW,
        startup_projection=startup_projection,
    )
    disk_factory = GcpV2LocalDiskCleanupFactory(
        provider_supplier=disk_provider_supplier,
        journal_root=disk_journal_root,
        now_fn=lambda: NOW,
        startup_projection=startup_projection,
    )
    executor_factory = GcpV2LocalWatchdogExecutorFactory(
        transport_factory=transport_factory,
        disk_cleanup_factory=disk_factory,
        core_journal=core_journal,
        now_fn=lambda: NOW,
    )
    watchdog = FileGcpWatchdog(
        watchdog_root,
        executor=executor_factory,
        runner_enabled=True,
        runner_poll_seconds=0.01,
        runner_now_fn=lambda: NOW,
    )
    supervisor, approval = _v2_supervisor(
        tmp_path / "safety", inputs[-1], watchdog=watchdog
    )
    binding = gcp_supervisor_binding(approval, record)
    watchdog.configure_lease(record)
    watchdog.arm(binding, approval=approval, now=NOW)
    capability = issue_gcp_v2_mutation_capability(
        contract=supervisor.activation_deadline,
        approval_digest=gcp_execution_approval_digest(approval),
        now=NOW,
    )
    watchdog.activate(binding, approval=approval, capability=capability, now=NOW)
    due = datetime.fromisoformat(
        str(capability.provider_runtime_deadline_at).replace("Z", "+00:00")
    )
    runner_pid: int | None = None
    try:
        with watchdog._exclusive() as root:
            runner_pid = watchdog._read_locked(root, binding.controller_id)[
                -1
            ].runner_process_id

        original_bind_for_event = executor_factory.bind_for_event

        def bind_hanging_prebind(event: Any) -> Any:
            executor = original_bind_for_event(event)

            def hang(*_: Any, **__: Any) -> Any:
                time.sleep(10)
                raise AssertionError("hung worker should have been killed")

            executor.cleanup_prebind = hang  # type: ignore[method-assign]
            return executor

        executor_factory.bind_for_event = bind_hanging_prebind  # type: ignore[method-assign]
        started = time.monotonic()
        retry = watchdog.run_due(controller_id=binding.controller_id, now=due)
        elapsed = time.monotonic() - started

        assert retry.state == "PREBIND_RETRY_PENDING"
        assert retry.error_code == "WATCHDOG_PREBIND_CLEANUP_UNCONFIRMED"
        assert elapsed < 4.0
        assert not supplier_marker.exists()
        assert not disk_supplier_marker.exists()

        terminal = record.model_copy(
            update={
                "state": "CLEANUP_CONFIRMED",
                "cleanup_confirmed": True,
                "orphaned": False,
                "last_error_code": "NO_PROVIDER_MUTATION",
            }
        )
        core_journal.update(terminal)
        restarted = FileGcpWatchdog(
            watchdog_root,
            executor=GcpV2LocalWatchdogExecutorFactory(
                transport_factory=transport_factory,
                disk_cleanup_factory=disk_factory,
                core_journal=GcpLeaseJournal(core_root),
                now_fn=lambda: NOW,
            ),
            runner_enabled=True,
            runner_poll_seconds=0.01,
            runner_now_fn=lambda: NOW,
        )
        settled = restarted.reconcile_core_terminal(
            binding,
            now=due + timedelta(seconds=1),
            reason_code="CORE_CLEANUP_CONFIRMED",
        )
        assert settled is not None
        assert settled.state == "CLEANUP_NOT_REQUIRED"
        assert (
            restarted.run_due(
                controller_id=binding.controller_id,
                now=due + timedelta(seconds=2),
            ).state
            == "CLEANUP_NOT_REQUIRED"
        )
        assert not supplier_marker.exists()
        assert not disk_supplier_marker.exists()
    finally:
        if runner_pid is not None:
            watchdog._kill_runner(runner_pid)


@pytest.mark.parametrize(
    ("crash_point", "phase", "expected_state"),
    [
        ("file_watchdog_after_event_fsync", "arm", "READY"),
        ("file_watchdog_after_directory_fsync", "arm", "READY"),
        ("file_watchdog_after_runner_ready", "activate", "RUNNER_READY"),
        (
            "file_watchdog_child_after_post_gate_validation",
            "activate",
            "RUNNER_READY",
        ),
    ],
)
def test_v2_file_watchdog_crash_prefixes_restart_without_transport_activation(
    tmp_path: Path, crash_point: str, phase: str, expected_state: str
) -> None:
    """Every watchdog-journal publication edge has a locally restartable prefix."""

    inputs = _v2_inputs()
    record = _prepared_record(tmp_path / "prepared")
    core_root = tmp_path / "core"
    core_root.mkdir()
    core_journal = GcpLeaseJournal(core_root)
    core_journal.reserve(record)
    startup_projection = _v2_startup_projection(inputs[-1])
    watchdog_root = tmp_path / "watchdog"
    watchdog_root.mkdir()
    disk_journal_root = tmp_path / "disk-journal"
    disk_journal_root.mkdir()
    transport_supplier_marker = tmp_path / "unexpected-transport-supplier"
    disk_supplier_marker = tmp_path / "unexpected-disk-supplier"

    def transport_supplier() -> FakeGcpComputeTransport:
        transport_supplier_marker.write_text("called", encoding="ascii")
        return FakeGcpComputeTransport()

    def disk_provider_supplier() -> Any:
        disk_supplier_marker.write_text("called", encoding="ascii")
        raise AssertionError("watchdog recovery must stay before provider activation")

    transport_factory = GcpV2LocalTransportFactory(
        transport_supplier=transport_supplier,
        now_fn=lambda: NOW,
        startup_projection=startup_projection,
    )
    disk_factory = GcpV2LocalDiskCleanupFactory(
        provider_supplier=disk_provider_supplier,
        journal_root=disk_journal_root,
        now_fn=lambda: NOW,
        startup_projection=startup_projection,
    )

    def make_watchdog(crash_hook: Any = None) -> FileGcpWatchdog:
        return FileGcpWatchdog(
            watchdog_root,
            executor=GcpV2LocalWatchdogExecutorFactory(
                transport_factory=transport_factory,
                disk_cleanup_factory=disk_factory,
                core_journal=GcpLeaseJournal(core_root),
                now_fn=lambda: NOW,
            ),
            crash_hook=crash_hook,
            runner_enabled=True,
            runner_poll_seconds=0.01,
            runner_now_fn=lambda: NOW,
        )

    watchdog = make_watchdog()
    supervisor, approval = _v2_supervisor(
        tmp_path / "safety", inputs[-1], watchdog=watchdog
    )
    binding = gcp_supervisor_binding(approval, record)
    capability = issue_gcp_v2_mutation_capability(
        contract=supervisor.activation_deadline,
        approval_digest=gcp_execution_approval_digest(approval),
        now=NOW,
    )
    if phase == "activate":
        watchdog.configure_lease(record)
        watchdog.arm(binding, approval=approval, now=NOW)

    crash_marker = tmp_path / "crash-marker"

    def crash_hook(point: str) -> None:
        if point == crash_point:
            crash_marker.write_text(point, encoding="ascii")
            os._exit(77)

    pid = os.fork()
    if pid == 0:
        crashed = make_watchdog(crash_hook=crash_hook)
        crashed.configure_lease(record)
        try:
            if phase == "arm":
                crashed.arm(binding, approval=approval, now=NOW)
            else:
                crashed.activate(
                    binding, approval=approval, capability=capability, now=NOW
                )
        except GcpSupervisorError:
            # The post-gate runner crash is observed by its parent as an
            # unavailable runner; the durable RUNNER_READY event remains.
            pass
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) in {0, 77}
    assert crash_marker.read_text(encoding="ascii") == crash_point

    restarted = make_watchdog()
    restarted.configure_lease(record)
    runner_pid: int | None = None
    try:
        if phase == "arm":
            restarted.arm(binding, approval=approval, now=NOW)
        else:
            restarted.activate(
                binding, approval=approval, capability=capability, now=NOW
            )
        with restarted._exclusive() as root:
            latest = restarted._read_locked(root, binding.controller_id)[-1]
            assert latest.state == expected_state
            runner_pid = latest.runner_process_id
        assert not transport_supplier_marker.exists()
        assert not disk_supplier_marker.exists()
    finally:
        if runner_pid is not None:
            restarted._kill_runner(runner_pid)


def test_v2_recovery_requires_exact_cleanup_authorization_and_sidecar(
    tmp_path: Path, request: Any
) -> None:
    inputs = _v2_inputs()
    safety_root = tmp_path / "safety"
    supervisor, approval = _v2_supervisor(safety_root, inputs[-1])
    core_root = tmp_path / "core"
    core_root.mkdir()
    transport = FakeGcpComputeTransport(delete_error="DELETE_DENIED")
    initial = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=supervisor,
    )
    initial_outcome = _execute_v2(initial, inputs)
    assert initial_outcome.orphaned is True
    orphaned_event = supervisor.journal.load("ctl-12345678")
    assert orphaned_event.state == "ORPHANED"
    assert orphaned_event.orphan_report is not None
    assert orphaned_event.orphan_report.request_digest == initial_outcome.request_digest
    assert orphaned_event.orphan_report.instance_name == "inferdrome-ctl-12345678"
    assert orphaned_event.orphan_report.reason_code == "DELETE_DENIED"

    recovery_record = initial.journal.load("ctl-12345678")
    disk_binding = _v2_disk_binding_for_record(recovery_record)
    disk_root = tmp_path / "disk-journal"
    disk_root.mkdir()
    disk_provider = FileBackedFakeDiskProvider.initialize(
        tmp_path / "disk-provider", observation=disk_binding.disk
    )
    disk_cleanup_factory = GcpV2LocalDiskCleanupFactory(
        provider_supplier=lambda: disk_provider,
        journal_root=disk_root,
        now_fn=lambda: NOW,
        startup_projection=supervisor.startup_projection,
    )
    cleanup_transport_factory = GcpV2LocalTransportFactory(
        transport_supplier=lambda: transport,
        now_fn=lambda: NOW,
        startup_projection=supervisor.startup_projection,
    )
    watchdog_root = tmp_path / "recovery-watchdog"
    watchdog_root.mkdir()
    watchdogs_to_stop: list[FileGcpWatchdog] = []

    def stop_watchdog_runners() -> None:
        """Prevent a local fake sidecar from surviving this test process."""

        runner_pids: set[int] = set()
        for candidate in watchdogs_to_stop:
            try:
                with candidate._exclusive() as locked_root:
                    runner_pids.update(
                        event.runner_process_id
                        for event in candidate._read_locked(
                            locked_root, "ctl-12345678"
                        )
                        if event.runner_process_id is not None
                    )
            except (GcpSupervisorError, OSError):
                continue
        if not watchdogs_to_stop:
            return
        for runner_pid in runner_pids:
            try:
                watchdogs_to_stop[0]._kill_runner(runner_pid)
            except OSError:
                continue

    request.addfinalizer(stop_watchdog_runners)

    def watchdog_for_restart() -> FileGcpWatchdog:
        return FileGcpWatchdog(
            watchdog_root,
            executor=GcpV2LocalWatchdogExecutorFactory(
                transport_factory=cleanup_transport_factory,
                disk_cleanup_factory=disk_cleanup_factory,
                core_journal=GcpLeaseJournal(core_root),
                now_fn=lambda: NOW,
            ),
            runner_enabled=True,
            runner_poll_seconds=0.01,
            runner_now_fn=lambda: NOW,
        )

    # Seed a separately durable sidecar exactly as a future activation would,
    # then reopen it through a fresh watchdog object.  This keeps the original
    # failed fake lifecycle offline while exercising recovery's concrete,
    # restartable factory boundary.
    armed_watchdog = watchdog_for_restart()
    watchdogs_to_stop.append(armed_watchdog)
    durable_binding = gcp_supervisor_binding(approval, recovery_record)
    armed_watchdog.configure_lease(recovery_record)
    armed_watchdog.arm(durable_binding, approval=approval, now=NOW)
    capability = issue_gcp_v2_mutation_capability(
        contract=supervisor.activation_deadline,
        approval_digest=gcp_execution_approval_digest(approval),
        now=NOW,
    )
    armed_watchdog.activate(
        durable_binding,
        approval=approval,
        capability=capability,
        now=NOW,
    )
    armed_watchdog.bind_disk_cleanup(
        durable_binding, disk_cleanup_binding=disk_binding, now=NOW
    )
    resumed_watchdog = watchdog_for_restart()
    watchdogs_to_stop.append(resumed_watchdog)
    resumed_supervisor = GcpLifecycleSupervisor(
        approval=approval,
        quote_basis=supervisor.quote_basis,
        cost_guard=supervisor.cost_guard,
        activation_deadline=supervisor.activation_deadline,
        startup_projection=supervisor.startup_projection,
        journal=GcpSupervisorJournal(safety_root),
        watchdog=resumed_watchdog,
        kill_switch=InMemoryGcpKillSwitch(),
    )

    recovery = GcpGuardedLifecycleController(
        # Recovery receives only cleanup-scoped factories; it never receives a
        # create-capability route.
        cleanup_transport_factory=cleanup_transport_factory,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(core_root),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=resumed_supervisor,
        exact_disk_cleanup_factory=disk_cleanup_factory,
    )

    cleanup_authorization = issue_gcp_cleanup_recovery_authorization(
        record=recovery_record,
        approval_digest=gcp_execution_approval_digest(approval),
        startup_projection_digest=approval.startup_projection_digest,
        execution_payload_digest=approval.execution_payload_digest,
        disk_cleanup_binding=disk_binding,
        operator_identity="operator-alice",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        watchdog_cleanup_deadline_at=datetime.fromisoformat(
            str(approval.watchdog_deadline_at).replace("Z", "+00:00")
        ),
        confirmation=GCP_CLEANUP_RECOVERY_CONFIRMATION,
    )
    # Create authorization expires at five minutes, while this distinct,
    # exact cleanup-only authorization remains valid only through the durable
    # watchdog horizon.  The later validation must not reopen create authority
    # or derive a target from the original hostname.
    cleanup_after_create_expiry = NOW + timedelta(minutes=6)
    assert cleanup_after_create_expiry > datetime.fromisoformat(
        str(approval.expires_at).replace("Z", "+00:00")
    )
    assert validate_gcp_cleanup_recovery_authorization(
        cleanup_authorization,
        record=recovery_record,
        approval_digest=gcp_execution_approval_digest(approval),
        startup_projection_digest=approval.startup_projection_digest,
        execution_payload_digest=approval.execution_payload_digest,
        now=cleanup_after_create_expiry,
        watchdog_cleanup_deadline_at=datetime.fromisoformat(
            str(approval.watchdog_deadline_at).replace("Z", "+00:00")
        ),
    ) == cleanup_authorization
    recovered = recovery.recover_cleanup(
        controller_id="ctl-12345678", cleanup_authorization=cleanup_authorization
    )

    assert recovered.orphaned is True
    assert recovered.cleanup_confirmed is False
    assert resumed_supervisor.journal.load("ctl-12345678").state == "ORPHANED"


def test_successful_fake_lifecycle_has_one_delete_and_no_evidence_claim(
    tmp_path: Path,
) -> None:
    transport = FakeGcpComputeTransport()
    outcome = _run(tmp_path, transport)
    assert outcome.status == "SUCCEEDED"
    assert outcome.cleanup_confirmed is True
    assert outcome.orphaned is False
    assert outcome.evidence_eligible is False
    assert outcome.invoice_truth == "unavailable_external_provider_invoice"
    assert transport.insert_calls == 1
    assert transport.delete_calls == 1
    assert canonical_gcp_execution_outcome_bytes(
        outcome
    ) == canonical_gcp_execution_outcome_bytes(outcome)


def test_incomplete_owned_inventory_blocks_before_create(tmp_path: Path) -> None:
    transport = FakeGcpComputeTransport(owned_inventory_incomplete=True)

    outcome = _run(tmp_path, transport)

    assert outcome.status == "FAILED"
    assert outcome.error_code == "LIST_OWNED_INCOMPLETE"
    assert outcome.cleanup_confirmed is True
    assert transport.insert_calls == 0
    assert transport.delete_calls == 0


def test_post_create_safety_mismatch_cleans_up_before_work(tmp_path: Path) -> None:
    transport = FakeGcpComputeTransport(
        instance_safety_error="INSTANCE_TTL_MISMATCH"
    )

    outcome = _run(tmp_path, transport)

    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "INSTANCE_TTL_MISMATCH"
    assert outcome.cleanup_confirmed is True
    assert outcome.orphaned is False
    assert transport.insert_calls == 1
    assert transport.delete_calls == 1


def test_residual_exact_boot_disk_never_confirms_cleanup(tmp_path: Path) -> None:
    transport = FakeGcpComputeTransport(residual_boot_disk=True)

    outcome = _run(tmp_path, transport)

    assert outcome.status == "FAILED"
    assert outcome.cleanup_confirmed is False
    assert outcome.orphaned is True
    assert outcome.cleanup_error_code == "BOOT_DISK_RESIDUAL"
    assert transport.insert_calls == 1
    assert transport.delete_calls == 1


@pytest.mark.parametrize("failure", ["benchmark", "keyboard"])
def test_failure_paths_cleanup_and_preserve_primary_cause(
    tmp_path: Path, failure: str
) -> None:
    def work(_: Any) -> None:
        if failure == "keyboard":
            raise KeyboardInterrupt
        raise RuntimeError("unbounded secret-like details must not serialize")

    transport = FakeGcpComputeTransport()
    outcome = _run(tmp_path, transport, work=work)
    assert outcome.status in {"FAILED", "INTERRUPTED"}
    assert outcome.cleanup_confirmed is True
    assert outcome.error_code in {"BENCHMARK_FAILURE", "KEYBOARD_INTERRUPT"}
    assert "secret-like" not in canonical_gcp_execution_outcome_bytes(outcome).decode()
    assert transport.delete_calls == 1


def test_cleanup_failure_dominates_and_orphans(tmp_path: Path) -> None:
    transport = FakeGcpComputeTransport(delete_error="DELETE_DENIED")
    outcome = _run(tmp_path, transport)
    assert outcome.status == "FAILED"
    assert outcome.cleanup_confirmed is False
    assert outcome.orphaned is True
    assert outcome.error_code == "DELETE_DENIED"
    assert outcome.cleanup_error_code == "DELETE_DENIED"
    assert outcome.primary_error_code is None
    assert outcome.journal_state == "ORPHANED"


def test_ambiguous_delete_never_confirms_absence(tmp_path: Path) -> None:
    transport = FakeGcpComputeTransport(
        delete_error="DELETE_AMBIGUOUS", delete_error_ambiguous=True
    )
    outcome = _run(tmp_path, transport)
    assert outcome.status == "FAILED"
    assert outcome.cleanup_confirmed is False
    assert outcome.orphaned is True
    assert outcome.error_code == "DELETE_AMBIGUOUS"
    assert transport.delete_calls == 3


def test_false_not_found_is_not_cleanup_confirmation(tmp_path: Path) -> None:
    transport = FakeGcpComputeTransport(false_not_found=True)
    outcome = _run(tmp_path, transport)
    assert outcome.status == "FAILED"
    assert outcome.orphaned is True
    assert outcome.error_code in {"CLEANUP_UNCONFIRMED", "CLEANUP_ATTEMPTS_EXHAUSTED"}
    assert transport.delete_calls >= 1


def test_recovery_reconstructs_exact_request_from_orphaned_lease(
    tmp_path: Path,
) -> None:
    transport = FakeGcpComputeTransport(delete_error="DELETE_DENIED")
    outcome, controller = _run(tmp_path, transport, return_controller=True)
    assert outcome.orphaned is True
    transport.delete_error = None
    recovered = controller.recover_cleanup(
        controller_id="ctl-12345678", confirmation="RECOVER_GCP_LEASE"
    )
    assert recovered.status == "FAILED"
    assert recovered.cleanup_confirmed is False
    assert recovered.orphaned is True
    assert transport.delete_calls == 3


def test_malformed_plan_is_rejected_before_any_transport_call(tmp_path: Path) -> None:
    spec, inventory, context, plan = _inputs()
    arm = _arm()
    environment = _environment(plan)
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    quote, capacity = _quote_and_capacity(plan, arm, request)
    transport = FakeGcpComputeTransport()
    with pytest.raises((ValueError, GcpPlanError)):
        validate_gcp_execution_preflight(
            plan=plan.model_copy(update={"execution_authorized": True}),
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
            arm_bytes=canonical_gcp_execution_arm_bytes(arm),
            environment=environment,
            quote=quote,
            capacity=capacity,
            now=NOW,
        )
    assert transport.insert_calls == 0


def test_journal_is_atomic_no_symlink_and_tamper_safe(tmp_path: Path) -> None:
    journal = GcpLeaseJournal(tmp_path)
    record = _run(tmp_path / "run", FakeGcpComputeTransport()) if False else None
    del record
    (tmp_path / "link.lease.json").symlink_to(tmp_path / "missing")
    with pytest.raises(GcpJournalError):
        journal.unresolved_for_plan("sha256:" + "1" * 64)


def test_journal_intent_anchor_rejects_retargeted_lease(tmp_path: Path) -> None:
    outcome, controller = _run(
        tmp_path, FakeGcpComputeTransport(), return_controller=True
    )
    assert outcome.status == "SUCCEEDED"
    anchor = tmp_path / "ctl-12345678.intent.json"
    raw = anchor.read_bytes()
    anchor.write_bytes(raw.replace(b"inferdrome-example", b"other-project"))
    with pytest.raises(GcpJournalError):
        controller.journal.load("ctl-12345678")


def test_journal_event_chain_rejects_operation_retargeting(tmp_path: Path) -> None:
    outcome, controller = _run(
        tmp_path, FakeGcpComputeTransport(), return_controller=True
    )
    assert outcome.status == "SUCCEEDED"
    path = tmp_path / "ctl-12345678.lease.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["provider_operation_id"] = "op-99999999"
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    with pytest.raises(GcpJournalError):
        controller.journal.load("ctl-12345678")


def test_journal_rejects_terminal_state_regression(tmp_path: Path) -> None:
    outcome, controller = _run(
        tmp_path, FakeGcpComputeTransport(), return_controller=True
    )
    assert outcome.status == "SUCCEEDED"
    record = controller.journal.load("ctl-12345678")
    tampered = record.model_copy(
        update={"state": "ORPHANED", "cleanup_confirmed": False, "orphaned": True}
    )
    with pytest.raises(GcpJournalError):
        controller.journal.update(tampered)


def test_file_arm_store_is_one_shot_across_controller_objects(tmp_path: Path) -> None:
    from inferdrome.deployment import FileExecutionArmStore

    arm = _arm()
    arm_sha = __import__(
        "inferdrome.deployment", fromlist=["gcp_execution_arm_sha256"]
    ).gcp_execution_arm_sha256(arm)
    stores = [FileExecutionArmStore(tmp_path), FileExecutionArmStore(tmp_path)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda store: store.consume(arm.arm_id, arm_sha), stores)
        )
    assert sorted(results) == [False, True]


def test_provider_observation_rejects_identity_and_network_drift() -> None:
    _spec, _inventory, _context, plan = _inputs()
    arm = _arm()
    from inferdrome.deployment import build_gcp_insert_request

    request = build_gcp_insert_request(
        plan=plan, arm=arm, environment=_environment(plan)
    )

    class Value:
        pass

    value = Value()
    value.name = request.instance_name
    value.self_link = (
        f"https://compute.googleapis.com/compute/v1/projects/{request.project_id}"
        f"/zones/{request.zone}/instances/{request.instance_name}"
    )
    value.status = "RUNNING"
    value.zone = (
        f"https://compute.googleapis.com/compute/v1/projects/{request.project_id}"
        f"/zones/{request.zone}"
    )
    value.machine_type = (
        f"https://compute.googleapis.com/compute/v1/projects/{request.project_id}"
        f"/zones/{request.zone}/machineTypes/{request.machine_type}"
    )
    value.guest_accelerators = [
        type(
            "Accelerator",
            (),
            {
                "accelerator_type": (
                    f"https://compute.googleapis.com/compute/v1/projects/{request.project_id}"
                    f"/zones/{request.zone}/acceleratorTypes/{request.accelerator_provider_type}"
                ),
                "accelerator_count": request.accelerator_count,
            },
        )()
    ]
    value.can_ip_forward = False
    value.labels = request.labels.model_dump(mode="json")
    value.network_interfaces = [
        type(
            "Interface",
            (),
            {
                "network": request.network.network,
                "subnetwork": request.network.subnetwork,
                "stack_type": "IPV4_ONLY",
                "access_configs": [],
                "ipv6_access_configs": [],
                "network_i_p": "10.0.0.2",
            },
        )()
    ]
    observation = _observation(value, request)
    assert observation.project_id == request.project_id
    value.machine_type = (
        f"https://compute.googleapis.com/compute/v1/projects/{request.project_id}"
        f"/zones/{request.zone}/machineTypes/other-machine"
    )
    with pytest.raises(ValueError, match="MACHINE_TYPE"):
        _observation(value, request)


def test_provider_observation_accepts_documented_partial_network_references() -> None:
    _spec, _inventory, _context, plan = _inputs()
    arm = _arm()
    from inferdrome.deployment import build_gcp_insert_request

    request = build_gcp_insert_request(
        plan=plan, arm=arm, environment=_environment(plan)
    )
    value = type(
        "Instance",
        (),
        {
            "name": request.instance_name,
            "self_link": (
                f"https://compute.googleapis.com/compute/v1/projects/{request.project_id}"
                f"/zones/{request.zone}/instances/{request.instance_name}"
            ),
            "status": "RUNNING",
            "zone": f"https://compute.googleapis.com/compute/v1/projects/{request.project_id}/zones/{request.zone}",
            "machine_type": f"zones/{request.zone}/machineTypes/{request.machine_type}",
            "guest_accelerators": [],
            "can_ip_forward": False,
            "labels": request.labels.model_dump(mode="json"),
            "network_interfaces": [
                type(
                    "Nic",
                    (),
                    {
                        "network": "global/networks/private",
                        "subnetwork": f"regions/{request.region}/subnetworks/private",
                        "stack_type": "IPV4_ONLY",
                        "access_configs": [],
                        "ipv6_access_configs": [],
                        "network_i_p": "10.0.0.2",
                    },
                )()
            ],
        },
    )()
    observation = _observation(value, request)
    assert observation.network == request.network.network
    assert observation.subnetwork == request.network.subnetwork


def test_real_sdk_image_and_boot_disk_observations_bind_provider_identity() -> None:
    sdk = pytest.importorskip("google.cloud.compute_v1")
    _spec, _inventory, _context, plan = _inputs()
    arm = _arm()
    from inferdrome.deployment import build_gcp_insert_request

    request = build_gcp_insert_request(
        plan=plan, arm=arm, environment=_environment(plan)
    ).model_copy(
        update={
            "machine_type": "a2-highgpu-1g",
            "accelerator_provider_type": "nvidia-tesla-a100",
        }
    )
    image_name = request.boot_image.image_name.rsplit("/", 1)[-1]
    image_link = (
        f"https://www.googleapis.com/compute/v1/projects/{request.project_id}"
        f"/global/images/{image_name}"
    )

    class Images:
        def get(self, *, project: str, image: str, timeout: int) -> Any:
            assert (project, image, timeout) == (request.project_id, image_name, 5)
            return sdk.Image(
                name=image_name,
                id=request.boot_image.provider_image_id,
                status="READY",
                self_link=image_link,
            )

    class Disks:
        def get(self, *, project: str, zone: str, disk: str, timeout: int) -> Any:
            assert (project, zone, disk, timeout) == (
                request.project_id,
                request.zone,
                request.instance_name,
                5,
            )
            return sdk.Disk(
                name=request.instance_name,
                source_image_id=str(request.boot_image.provider_image_id),
            )

    transport = create_google_compute_transport(
        sdk_module=sdk,
        client=object(),
        operations_client=object(),
        images_client=Images(),
        disks_client=Disks(),
    )
    assert (
        transport.get_image(request, timeout_seconds=5).provider_image_id == 123456789
    )
    assert (
        transport.get_boot_disk(request, timeout_seconds=5).source_image_id == 123456789
    )


def test_real_sdk_operation_states_keep_empty_done_errors_nonterminal() -> None:
    sdk = pytest.importorskip("google.cloud.compute_v1")
    _spec, _inventory, _context, plan = _inputs()
    arm = _arm()
    from inferdrome.deployment import build_gcp_insert_request

    request = build_gcp_insert_request(
        plan=plan, arm=arm, environment=_environment(plan)
    ).model_copy(
        update={
            "machine_type": "a2-highgpu-1g",
            "accelerator_provider_type": "nvidia-tesla-a100",
        }
    )

    class Client:
        def __init__(self) -> None:
            self.operation: Any = None

        def insert(self, *, request: Any, timeout: int) -> Any:
            del request, timeout
            return self.operation

    client = Client()
    transport = create_google_compute_transport(
        sdk_module=sdk,
        client=client,
        operations_client=object(),
        images_client=object(),
        disks_client=object(),
    )
    target_link = (
        "https://www.googleapis.com/compute/v1/projects/inferdrome-example/"
        "zones/us-central1-b/instances/inferdrome-ctl-12345678"
    )
    for status in (sdk.Operation.Status.PENDING, sdk.Operation.Status.RUNNING):
        client.operation = sdk.Operation(
            name="operation-1",
            status=status,
            operation_type="insert",
            target_link=target_link,
        )
        handle = transport._remember(client.operation, "insert", request)
        assert transport.wait_operation(handle, timeout_seconds=5).status == "TIMEOUT"
    client.operation = sdk.Operation(
        name="operation-1",
        status=sdk.Operation.Status.DONE,
        operation_type="insert",
        target_link=target_link,
    )
    handle = transport._remember(client.operation, "insert", request)
    done_result = transport.wait_operation(handle, timeout_seconds=5)
    assert done_result.status == "DONE"
    assert done_result.instance_name == request.instance_name
    client.operation = sdk.Operation(
        name="operation-1",
        status=sdk.Operation.Status.DONE,
        operation_type="insert",
        target_link=target_link,
        error=sdk.Error(errors=[{"code": "FAILED", "message": "provider"}]),
    )
    handle = transport._remember(client.operation, "insert", request)
    assert transport.wait_operation(handle, timeout_seconds=5).status == "ERROR"

    class PollFailure:
        name = "operation-1"
        status = sdk.Operation.Status.DONE
        operation_type = "insert"
        target_link = (
            "https://www.googleapis.com/compute/v1/projects/inferdrome-example/"
            "zones/us-central1-b/instances/inferdrome-ctl-12345678"
        )
        error = sdk.Error()

        @staticmethod
        def result(*, timeout: int) -> None:
            del timeout
            raise RuntimeError("transient provider polling failure")

    client.operation = PollFailure()
    handle = transport._remember(client.operation, "insert", request)
    assert transport.wait_operation(handle, timeout_seconds=5).status == "TIMEOUT"

    class Operations:
        def get(self, **_: Any) -> Any:
            return sdk.Operation(
                name="different-operation",
                status=sdk.Operation.Status.DONE,
                operation_type="insert",
                target_link=target_link,
            )

    client.operation = sdk.Operation(
        name="operation-1",
        status=sdk.Operation.Status.DONE,
        operation_type="insert",
        target_link=target_link,
    )
    handle = transport._remember(client.operation, "insert", request)
    class FreshOperations:
        def get(self, **_: Any) -> Any:
            return sdk.Operation(
                name="operation-1",
                status=sdk.Operation.Status.DONE,
                operation_type="insert",
                target_link=target_link,
            )

    fresh = create_google_compute_transport(
        sdk_module=sdk,
        client=object(),
        operations_client=FreshOperations(),
        images_client=object(),
        disks_client=object(),
    )
    assert fresh.wait_operation(handle, timeout_seconds=5).status == "DONE"
    transport._operations.clear()
    transport._operations_client = Operations()
    with pytest.raises(GcpTransportError, match="OPERATION_RESPONSE_MISMATCH"):
        transport.wait_operation(handle, timeout_seconds=5)


def test_real_sdk_operation_binds_kind_and_full_target() -> None:
    sdk = pytest.importorskip("google.cloud.compute_v1")
    _spec, _inventory, _context, plan = _inputs()
    arm = _arm()
    from inferdrome.deployment import build_gcp_insert_request

    request = build_gcp_insert_request(
        plan=plan, arm=arm, environment=_environment(plan)
    ).model_copy(
        update={
            "machine_type": "a2-highgpu-1g",
            "accelerator_provider_type": "nvidia-tesla-a100",
        }
    )
    expected_target = (
        "https://www.googleapis.com/compute/v1/projects/inferdrome-example/"
        "zones/us-central1-b/instances/inferdrome-ctl-12345678"
    )

    class Client:
        operation: Any

        def insert(self, *, request: Any, timeout: int) -> Any:
            del request, timeout
            return self.operation

    client = Client()
    transport = create_google_compute_transport(
        sdk_module=sdk,
        client=client,
        operations_client=object(),
        images_client=object(),
        disks_client=object(),
    )
    vectors = [
        {"operation_type": "delete", "target_link": expected_target},
        {
            "operation_type": "insert",
            "target_link": expected_target.replace(
                "inferdrome-example", "evil-project"
            ),
        },
        {
            "operation_type": "insert",
            "target_link": expected_target.replace("us-central1-b", "us-east1-b"),
        },
        {
            "operation_type": "insert",
            "target_link": expected_target.replace("inferdrome-ctl-12345678", "other"),
        },
        {"operation_type": "insert", "target_link": None},
        {"operation_type": "insert", "target_link": expected_target, "status": 999},
    ]
    for vector in vectors:
        client.operation = sdk.Operation(
            name="operation-1",
            status=vector.get("status", sdk.Operation.Status.DONE),
            operation_type=vector["operation_type"],
            target_link=vector["target_link"],
        )
        handle = transport._remember(client.operation, "insert", request)
        with pytest.raises(
            GcpTransportError, match=r"OPERATION_RESPONSE_(?:MISMATCH|INVALID)"
        ):
            transport.wait_operation(handle, timeout_seconds=5)


def test_optional_transport_is_lazy_and_projects_only_private_request() -> None:
    class Value:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class Operation:
        def __init__(self) -> None:
            self.timeout: int | None = None
            self.name = "operation-1"
            self.operation_type = "insert"
            self.target_link = (
                "https://www.googleapis.com/compute/v1/projects/inferdrome-example/"
                "zones/us-central1-b/instances/inferdrome-ctl-12345678"
            )
            self.status = "DONE"
            self.error = None

        def result(self, *, timeout: int) -> None:
            self.timeout = timeout

    class Client:
        def __init__(self) -> None:
            self.inserted: Any = None
            self.operation = Operation()

        def insert(self, **kwargs: Any) -> Operation:
            self.inserted = kwargs["request"].instance_resource
            return self.operation

        def get(self, **_: Any) -> Any:
            return Value(name="inferdrome-ctl-12345678", status="RUNNING", labels={})

        def list(self, **_: Any) -> list[Any]:
            return []

        def delete(self, **_: Any) -> Operation:
            return self.operation

    class Sdk:
        Instance = Value
        AttachedDisk = Value
        AcceleratorConfig = Value
        AttachedDiskInitializeParams = Value
        NetworkInterface = Value
        Scheduling = Value
        ServiceAccount = Value
        InsertInstanceRequest = Value
        DeleteInstanceRequest = Value
        ListInstancesRequest = Value

        class Operation:
            class Status:
                DONE = "DONE"
                PENDING = "PENDING"
                RUNNING = "RUNNING"

        @staticmethod
        def ZoneOperationsClient() -> object:
            return object()

        class ImagesClient:
            pass

        class DisksClient:
            pass

    _, _, _, plan = _inputs()
    arm = _arm()
    from inferdrome.deployment import build_gcp_insert_request

    request = build_gcp_insert_request(
        plan=plan, arm=arm, environment=_environment(plan)
    )
    request = request.model_copy(
        update={
            "machine_type": "a2-highgpu-1g",
            "accelerator_provider_type": "nvidia-tesla-a100",
        }
    )
    client = Client()
    transport = create_google_compute_transport(
        sdk_module=Sdk,
        client=client,
        operations_client=object(),
        images_client=object(),
        disks_client=object(),
    )
    projected = transport.project_create_request(
        request, request_id=request.insert_request_id
    )
    with pytest.raises(GcpTransportError, match="LIVE_MUTATION_DISABLED"):
        transport.insert(
            request, timeout_seconds=7, request_id=request.insert_request_id
        )
    assert client.inserted is None
    assert (
        projected.instance_resource.network_interfaces[0].network
        == request.network.network
    )
    assert not hasattr(
        projected.instance_resource.network_interfaces[0], "access_configs"
    )
    assert projected.instance_resource.deletion_protection is False
    assert projected.instance_resource.scheduling.automatic_restart is False


def test_live_transport_factory_is_disabled_before_sdk_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def missing(_: str) -> Any:
        nonlocal called
        called = True
        raise ModuleNotFoundError("google-cloud-compute")

    monkeypatch.setattr(
        "inferdrome.deployment.gcp_compute_transport.importlib.import_module", missing
    )
    with pytest.raises(GcpTransportError, match="LIVE_TRANSPORT_DISABLED"):
        create_google_compute_transport()
    assert called is False


def test_default_clock_is_canonical_second_precision() -> None:
    value = system_gcp_clock().now()
    assert value.tzinfo == UTC
    assert value.microsecond == 0


@pytest.mark.parametrize(
    "value",
    [
        "http://evil.example/compute/v1/projects/inferdrome-example/global/networks/private",
        "https://evil.example/anything/projects/inferdrome-example/global/networks/private",
        "https://www.googleapis.com/compute/v1/projects/inferdrome-example/global/networks/private?x=1",
        "https://user:password@www.googleapis.com/compute/v1/projects/inferdrome-example/global/networks/private",
        "https://www.googleapis.com:443/compute/v1/projects/inferdrome-example/global/networks/private",
        "https://www.googleapis.com/compute/v1/projects/inferdrome-example/projects/other/global/networks/private",
    ],
)
def test_resource_reference_rejects_insecure_host_prefix_and_ambiguity(
    value: str,
) -> None:
    with pytest.raises(GcpTransportError, match="NETWORK"):
        _canonical_resource_ref(
            value,
            kind="network",
            project="inferdrome-example",
            region="us-central1",
        )


def test_resource_reference_accepts_only_exact_supported_compute_urls() -> None:
    assert (
        _canonical_resource_ref(
            "https://compute.googleapis.com/compute/v1/projects/inferdrome-example/global/networks/private",
            kind="network",
            project="inferdrome-example",
            region="us-central1",
        )
        == "projects/inferdrome-example/global/networks/private"
    )


def test_timeout_reconciles_late_instance_before_delete(tmp_path: Path) -> None:
    transport = FakeGcpComputeTransport(
        insert_status="TIMEOUT",
        insert_timeout_reconcile_status="DONE",
        insert_timeout_late_instance=True,
    )
    outcome = _run(tmp_path, transport)
    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "CREATE_OPERATION_TIMEOUT_AMBIGUOUS"
    assert outcome.cleanup_confirmed is True
    assert transport.delete_calls == 1


def test_ambiguous_timeout_never_confirms_absence_or_deletes(tmp_path: Path) -> None:
    transport = FakeGcpComputeTransport(insert_status="TIMEOUT")
    outcome = _run(tmp_path, transport)
    assert outcome.status == "FAILED"
    assert outcome.orphaned is True
    assert outcome.journal_state == "ORPHANED"
    assert outcome.error_code == "OPERATION_RECONCILIATION_TIMEOUT"
    assert transport.delete_calls == 0
    assert transport.get_calls == 0


def test_network_after_send_is_ambiguous_and_never_confirms_absence(
    tmp_path: Path,
) -> None:
    transport = FakeGcpComputeTransport(
        insert_error="INSERT_AMBIGUOUS", insert_error_ambiguous=True
    )
    outcome = _run(tmp_path, transport)
    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "INSERT_AMBIGUOUS"
    assert outcome.cleanup_confirmed is False
    assert outcome.orphaned is True
    assert outcome.error_code == "AMBIGUOUS_MUTATION_UNRESOLVED"
    assert transport.get_calls > 0
    assert transport.delete_calls == 0


def test_pre_insert_intent_is_durable_before_provider_call(tmp_path: Path) -> None:
    transport = FakeGcpComputeTransport(
        insert_error="INSERT_AMBIGUOUS", insert_error_ambiguous=True
    )
    outcome, controller = _run(tmp_path, transport, return_controller=True)
    assert outcome.orphaned is True
    assert transport.insert_calls == 1
    events = (tmp_path / "ctl-12345678.events.jsonl").read_text(encoding="utf-8")
    assert '"state":"CREATE_SUBMITTED"' in events
    assert '"provider_mutation_ambiguous":true' in events
    assert '"provider_operation_status":"UNKNOWN"' in events
    assert '"provider_mutation_attempted":true' in events
    assert controller.journal.load("ctl-12345678").orphaned is True


def test_pre_insert_journal_failure_blocks_provider_call(tmp_path: Path) -> None:
    class BrokenJournal(GcpLeaseJournal):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.calls = 0

        def update(self, record: Any) -> None:
            del record
            self.calls += 1
            if self.calls >= 2:
                raise GcpJournalError("simulated journal failure")

    spec, inventory, context, plan = _inputs()
    arm = _arm()
    environment = _environment(plan)
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    quote, capacity = _quote_and_capacity(plan, arm, request)
    transport = FakeGcpComputeTransport()
    controller = GcpGuardedLifecycleController(
        transport=transport,
        arm_store=InMemoryExecutionArmStore(),
        journal=BrokenJournal(tmp_path),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
    )
    with pytest.raises(GcpExecutionError, match="provider mutation was not reached"):
        controller.execute(
            plan=plan,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
            arm_bytes=canonical_gcp_execution_arm_bytes(arm),
            environment=environment,
            quote=quote,
            capacity=capacity,
        )
    assert transport.insert_calls == 0


def test_event_chain_recovers_when_snapshot_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, controller = _run(
        tmp_path, FakeGcpComputeTransport(), return_controller=True
    )
    assert outcome.status == "SUCCEEDED"
    record = controller.journal.load("ctl-12345678")
    changed = record.model_copy(update={"last_error_code": "RECOVERABLE_EVENT"})
    original_replace = os.replace

    def fail_replace(*_: Any, **__: Any) -> None:
        raise OSError("simulated snapshot publication crash")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(GcpJournalError):
        controller.journal.update(changed)
    monkeypatch.setattr(os, "replace", original_replace)
    assert (
        controller.journal.load("ctl-12345678").last_error_code == "RECOVERABLE_EVENT"
    )


def test_partial_event_tail_is_truncated_before_chain_advances(tmp_path: Path) -> None:
    outcome, controller = _run(
        tmp_path, FakeGcpComputeTransport(), return_controller=True
    )
    assert outcome.status == "SUCCEEDED"
    event_path = tmp_path / "ctl-12345678.events.jsonl"
    with event_path.open("ab") as stream:
        stream.write(b'{"partial_event":')
    record = controller.journal.load("ctl-12345678")
    controller.journal.update(
        record.model_copy(update={"last_error_code": "TAIL_REPAIRED"})
    )
    assert controller.journal.load("ctl-12345678").last_error_code == "TAIL_REPAIRED"
    assert event_path.read_bytes().endswith(b"\n")


@pytest.mark.parametrize(
    "field, value",
    [
        ("arm_consumed", False),
        ("provider_mutation_attempted", False),
        ("cleanup_confirmed", False),
        ("provider_operation_terminal", False),
        ("provider_operation_status", "PENDING"),
    ],
)
def test_journal_rejects_each_lifecycle_regression(
    tmp_path: Path, field: str, value: Any
) -> None:
    outcome, controller = _run(
        tmp_path, FakeGcpComputeTransport(), return_controller=True
    )
    assert outcome.status == "SUCCEEDED"
    record = controller.journal.load("ctl-12345678")
    with pytest.raises(GcpJournalError):
        controller.journal.update(record.model_copy(update={field: value}))


def _prepared_record(tmp_path: Path) -> Any:
    (tmp_path / "seed").mkdir(parents=True, exist_ok=True)
    outcome, controller = _run(
        tmp_path / "seed", FakeGcpComputeTransport(), return_controller=True
    )
    assert outcome.status == "SUCCEEDED"
    record = controller.journal.load("ctl-12345678")
    return record.model_copy(
        update={
            "state": "PREPARED",
            "updated_at": record.created_at,
            "provider_operation_id": None,
            "provider_operation_name": None,
            "provider_operation_kind": None,
            "provider_operation_status": None,
            "provider_operation_terminal": False,
            "provider_mutation_ambiguous": False,
            "arm_consumed": False,
            "provider_mutation_attempted": False,
            "cleanup_confirmed": False,
            "orphaned": False,
            "delete_attempts": 0,
            "last_error_code": None,
        }
    )


@pytest.mark.parametrize(
    "label",
    [
        "reserve_after_anchor_fsync",
        "reserve_after_event_fsync",
        "reserve_after_snapshot_fsync",
    ],
)
def test_subprocess_reserve_death_leaves_conservative_authority(
    tmp_path: Path, label: str
) -> None:
    record = _prepared_record(tmp_path)
    root = tmp_path / label
    root.mkdir()
    pid = os.fork()
    if pid == 0:
        journal = GcpLeaseJournal(
            root,
            crash_hook=lambda point: os._exit(77) if point == label else None,
        )
        journal.reserve(record)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 77
    journal = GcpLeaseJournal(root)
    if label == "reserve_after_anchor_fsync":
        with pytest.raises(GcpJournalError):
            journal.load(record.controller_id)
        journal.reserve(record)
        assert journal.load(record.controller_id) == record
    else:
        loaded = journal.load(record.controller_id)
        assert loaded == record
        advanced = loaded.model_copy(update={"last_error_code": "RESERVE_ADVANCED"})
        journal.update(advanced)
        assert journal.load(record.controller_id).last_error_code == "RESERVE_ADVANCED"
        with pytest.raises(GcpJournalError):
            journal.reserve(record)


def test_two_processes_cannot_both_reserve_the_same_plan(tmp_path: Path) -> None:
    record = _prepared_record(tmp_path)
    root = tmp_path / "race"
    root.mkdir()
    result_paths = [root / "child-one", root / "child-two"]
    pids: list[int] = []
    for result_path in result_paths:
        pid = os.fork()
        if pid == 0:
            try:
                GcpLeaseJournal(root).reserve(record)
                result_path.write_text("reserved", encoding="ascii")
            except GcpJournalError:
                result_path.write_text("rejected", encoding="ascii")
            os._exit(0)
        pids.append(pid)
    for pid in pids:
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    results = [path.read_text(encoding="ascii") for path in result_paths]
    assert sorted(results) == ["rejected", "reserved"]


@pytest.mark.parametrize(
    "label",
    ["update_after_stage_fsync", "update_after_event_fsync", "update_after_replace"],
)
def test_subprocess_update_death_leaves_readable_prefix(
    tmp_path: Path, label: str
) -> None:
    seed_root = tmp_path / "update-seed"
    record = _prepared_record(tmp_path)
    seed_root.mkdir()
    journal = GcpLeaseJournal(seed_root)
    journal.reserve(record)
    changed = record.model_copy(update={"last_error_code": "CRASH_BOUNDARY"})
    pid = os.fork()
    if pid == 0:
        child = GcpLeaseJournal(
            seed_root,
            crash_hook=lambda point: os._exit(77) if point == label else None,
        )
        child.update(changed)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 77
    loaded = journal.load(record.controller_id)
    if label == "update_after_stage_fsync":
        assert loaded == record
    else:
        assert loaded.last_error_code == "CRASH_BOUNDARY"
    advanced = loaded.model_copy(update={"last_error_code": "ADVANCED"})
    journal.update(advanced)
    assert journal.load(record.controller_id).last_error_code == "ADVANCED"


def test_journal_cannot_remove_pending_insert_operation_identity(
    tmp_path: Path,
) -> None:
    record = _prepared_record(tmp_path)
    root = tmp_path / "pending-insert"
    root.mkdir()
    journal = GcpLeaseJournal(root)
    armed = record.model_copy(
        update={"state": "ARM_CONSUMED", "arm_consumed": True}
    )
    journal.reserve(record)
    journal.update(armed)
    pending = armed.model_copy(
        update={
            "state": "CREATE_SUBMITTED",
            "provider_mutation_attempted": True,
            "provider_mutation_ambiguous": True,
            "provider_operation_id": "op-00000001",
            "provider_operation_name": (
                f"projects/{armed.project_id}/zones/{armed.zone}/operations/op-00000001"
            ),
            "provider_operation_kind": "insert",
            "provider_operation_status": "PENDING",
            "last_error_code": "CREATE_MUTATION_PENDING",
        }
    )
    journal.update(pending)
    removed = pending.model_copy(
        update={
            "provider_operation_id": None,
            "provider_operation_name": None,
            "provider_operation_kind": None,
            "provider_operation_status": "UNKNOWN",
            "provider_operation_terminal": False,
        }
    )
    with pytest.raises(GcpJournalError, match="operation identity"):
        journal.update(removed)


def test_journal_cannot_remove_pending_delete_operation_identity(
    tmp_path: Path,
) -> None:
    record = _prepared_record(tmp_path)
    root = tmp_path / "pending-delete"
    root.mkdir()
    journal = GcpLeaseJournal(root)
    armed = record.model_copy(
        update={"state": "ARM_CONSUMED", "arm_consumed": True}
    )
    journal.reserve(record)
    journal.update(armed)
    insert_pending = armed.model_copy(
        update={
            "state": "CREATE_SUBMITTED",
            "provider_mutation_attempted": True,
            "provider_mutation_ambiguous": True,
            "provider_operation_id": "op-00000001",
            "provider_operation_name": (
                f"projects/{armed.project_id}/zones/{armed.zone}/operations/op-00000001"
            ),
            "provider_operation_kind": "insert",
            "provider_operation_status": "PENDING",
            "last_error_code": "CREATE_MUTATION_PENDING",
        }
    )
    journal.update(insert_pending)
    insert_done = insert_pending.model_copy(
        update={
            "provider_mutation_ambiguous": False,
            "provider_operation_status": "DONE",
            "provider_operation_terminal": True,
            "last_error_code": None,
        }
    )
    journal.update(insert_done)
    cleanup_pending = insert_done.model_copy(update={"state": "CLEANUP_PENDING"})
    journal.update(cleanup_pending)
    delete_intent = cleanup_pending.model_copy(
        update={
            "state": "CLEANUP_PENDING",
            "delete_attempts": 1,
            "provider_mutation_ambiguous": True,
            "last_error_code": "DELETE_MUTATION_PENDING",
        }
    )
    journal.update(delete_intent)
    delete_pending = delete_intent.model_copy(
        update={
            "provider_operation_id": "op-00000002",
            "provider_operation_name": (
                f"projects/{armed.project_id}/zones/{armed.zone}/operations/op-00000002"
            ),
            "provider_operation_kind": "delete",
            "provider_operation_status": "PENDING",
            "provider_operation_terminal": False,
            "last_error_code": "DELETE_MUTATION_PENDING",
        }
    )
    journal.update(delete_pending)
    removed = delete_pending.model_copy(
        update={
            "provider_operation_id": None,
            "provider_operation_name": None,
            "provider_operation_kind": None,
            "provider_operation_status": "UNKNOWN",
            "provider_operation_terminal": False,
        }
    )
    with pytest.raises(GcpJournalError, match="operation identity"):
        journal.update(removed)


@pytest.mark.parametrize(
    "state",
    [
        "PREPARED",
        "CREATE_SUBMITTED_AMBIGUOUS",
        "CREATE_SUBMITTED_PENDING",
        "OWNED",
        "CLEANUP_PENDING",
        "ORPHANED",
        "CLEANUP_CONFIRMED",
    ],
)
def test_missing_event_authority_never_erases_remaining_lease(
    tmp_path: Path, state: str
) -> None:
    if state == "CLEANUP_CONFIRMED":
        completed_root = tmp_path / "completed"
        completed_root.mkdir()
        outcome, controller = _run(
            completed_root, FakeGcpComputeTransport(), return_controller=True
        )
        assert outcome.status == "SUCCEEDED"
        record = controller.journal.load("ctl-12345678")
    else:
        record = _prepared_record(tmp_path / "seed")
        root = tmp_path / state
        root.mkdir()
        journal = GcpLeaseJournal(root)
        journal.reserve(record)
        if state == "PREPARED":
            pass
        else:
            record = record.model_copy(
                update={
                    "state": "ARM_CONSUMED",
                    "arm_consumed": True,
                }
            )
            journal.update(record)
            if state == "CREATE_SUBMITTED_AMBIGUOUS":
                updates: dict[str, Any] = {
                    "state": "CREATE_SUBMITTED",
                    "provider_mutation_attempted": True,
                    "provider_mutation_ambiguous": True,
                    "provider_operation_status": "UNKNOWN",
                    "last_error_code": "CREATE_MUTATION_PENDING",
                }
            elif state in {
                "CREATE_SUBMITTED_PENDING",
                "OWNED",
                "CLEANUP_PENDING",
                "ORPHANED",
            }:
                updates = {
                    "state": "CREATE_SUBMITTED",
                    "provider_mutation_attempted": True,
                    "provider_mutation_ambiguous": True,
                    "provider_operation_id": "op-00000001",
                    "provider_operation_name": (
                        f"projects/{record.project_id}/zones/{record.zone}/operations/op-00000001"
                    ),
                    "provider_operation_kind": "insert",
                    "provider_operation_status": "PENDING",
                    "last_error_code": "CREATE_MUTATION_PENDING",
                }
            record = record.model_copy(update=updates)
            journal.update(record)
            if state in {"OWNED", "CLEANUP_PENDING"}:
                record = record.model_copy(
                    update={
                        "state": "OWNED" if state == "OWNED" else "CLEANUP_PENDING",
                        "provider_mutation_ambiguous": False,
                        "provider_operation_status": "DONE",
                        "provider_operation_terminal": True,
                    }
                )
                journal.update(record)
            elif state == "ORPHANED":
                record = record.model_copy(
                    update={
                        "state": "ORPHANED",
                        "orphaned": True,
                        "last_error_code": "AMBIGUOUS_MUTATION_UNRESOLVED",
                    }
                )
                journal.update(record)
        controller = None
    root = controller.journal.root if controller is not None else root
    event_path = root / "ctl-12345678.events.jsonl"
    anchor_path = root / "ctl-12345678.intent.json"
    snapshot_path = root / "ctl-12345678.lease.json"
    event_path.unlink()
    with pytest.raises(GcpJournalError, match="event authority"):
        GcpLeaseJournal(root).unresolved_for_plan(record.plan_id)
    assert anchor_path.exists()
    assert snapshot_path.exists()


def test_anchor_only_reserve_prefix_can_be_discarded_and_retried(
    tmp_path: Path,
) -> None:
    record = _prepared_record(tmp_path)
    root = tmp_path / "anchor-only"
    root.mkdir()
    child = GcpLeaseJournal(
        root,
        crash_hook=lambda point: os._exit(77)
        if point == "reserve_after_anchor_fsync"
        else None,
    )
    pid = os.fork()
    if pid == 0:
        child.reserve(record)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 77
    journal = GcpLeaseJournal(root)
    journal.reserve(record)
    assert journal.load(record.controller_id) == record


def test_delete_handle_requires_prepared_intent_and_cannot_be_replayed(
    tmp_path: Path,
) -> None:
    completed_root = tmp_path / "completed"
    completed_root.mkdir()
    outcome, controller = _run(
        completed_root, FakeGcpComputeTransport(), return_controller=True
    )
    assert outcome.status == "SUCCEEDED"
    record = controller.journal.load("ctl-12345678")
    terminal_to_pending = record.model_copy(
        update={
            "state": "CLEANUP_PENDING",
            "cleanup_confirmed": False,
            "provider_mutation_ambiguous": True,
            "provider_operation_id": "op-00000003",
            "provider_operation_name": (
                f"projects/{record.project_id}/zones/{record.zone}/operations/op-00000003"
            ),
            "provider_operation_kind": "delete",
            "provider_operation_status": "PENDING",
            "provider_operation_terminal": False,
        }
    )
    with pytest.raises(GcpJournalError):
        controller.journal.update(terminal_to_pending)
    with pytest.raises(GcpJournalError):
        controller.journal.update(
            record.model_copy(
                update={
                    "state": "CLEANUP_PENDING",
                    "delete_attempts": record.delete_attempts + 1,
                    "provider_mutation_ambiguous": True,
                    "provider_operation_id": "op-00000003",
                    "provider_operation_name": (
                        f"projects/{record.project_id}/zones/{record.zone}/operations/op-00000003"
                    ),
                    "provider_operation_kind": "delete",
                    "provider_operation_status": "PENDING",
                    "provider_operation_terminal": False,
                }
            )
        )
    with pytest.raises(GcpJournalError):
        controller.journal.update(
            record.model_copy(
                update={
                    "provider_mutation_ambiguous": True,
                    "provider_operation_id": "op-00000003",
                    "provider_operation_name": (
                        f"projects/{record.project_id}/zones/{record.zone}/operations/op-00000003"
                    ),
                    "provider_operation_kind": "delete",
                    "provider_operation_status": "PENDING",
                    "provider_operation_terminal": False,
                }
            )
        )
    with pytest.raises(GcpJournalError, match="GCP journal record is invalid"):
        controller.journal.update(
            record.model_copy(
                update={
                    "provider_mutation_ambiguous": True,
                    "provider_operation_id": "op-00000003",
                    "provider_operation_name": (
                        f"projects/{record.project_id}/zones/{record.zone}/operations/op-00000003"
                    ),
                    "provider_operation_kind": "delete",
                    "provider_operation_status": "PENDING",
                    "provider_operation_terminal": False,
                }
            )
        )


def test_delete_attempt_sequence_rejects_replay_retarget_and_overflow(
    tmp_path: Path,
) -> None:
    record = _prepared_record(tmp_path / "seed")
    root = tmp_path / "journal"
    root.mkdir()
    journal = GcpLeaseJournal(root)
    armed = record.model_copy(
        update={"state": "ARM_CONSUMED", "arm_consumed": True}
    )
    journal.reserve(record)
    journal.update(armed)
    insert_pending = armed.model_copy(
        update={
            "state": "CREATE_SUBMITTED",
            "provider_mutation_attempted": True,
            "provider_mutation_ambiguous": True,
            "provider_operation_id": "op-00000001",
            "provider_operation_name": (
                f"projects/{record.project_id}/zones/{record.zone}/operations/op-00000001"
            ),
            "provider_operation_kind": "insert",
            "provider_operation_status": "PENDING",
        }
    )
    journal.update(insert_pending)
    insert_done = insert_pending.model_copy(
        update={
            "provider_mutation_ambiguous": False,
            "provider_operation_status": "DONE",
            "provider_operation_terminal": True,
        }
    )
    journal.update(insert_done)
    cleanup_pending = insert_done.model_copy(update={"state": "CLEANUP_PENDING"})
    journal.update(cleanup_pending)
    delete_intent = cleanup_pending.model_copy(
        update={
            "state": "CLEANUP_PENDING",
            "delete_attempts": 1,
            "provider_mutation_ambiguous": True,
        }
    )
    journal.update(delete_intent)
    delete_pending = delete_intent.model_copy(
        update={
            "provider_operation_id": "op-00000002",
            "provider_operation_name": (
                f"projects/{record.project_id}/zones/{record.zone}/operations/op-00000002"
            ),
            "provider_operation_kind": "delete",
            "provider_operation_status": "PENDING",
            "provider_operation_terminal": False,
        }
    )
    journal.update(delete_pending)
    with pytest.raises(GcpJournalError, match="operation identity"):
        journal.update(
            delete_pending.model_copy(
                update={
                    "provider_operation_id": "op-00000003",
                    "provider_operation_name": (
                        f"projects/{record.project_id}/zones/{record.zone}/operations/op-00000003"
                    ),
                }
            )
        )
    delete_done = delete_pending.model_copy(
        update={
            "provider_mutation_ambiguous": False,
            "provider_operation_status": "DONE",
            "provider_operation_terminal": True,
        }
    )
    journal.update(delete_done)
    with pytest.raises(GcpJournalError, match="terminal operation"):
        journal.update(delete_pending)
    forged_intent = delete_done.model_copy(
        update={"provider_mutation_ambiguous": True}
    )
    with pytest.raises(GcpJournalError, match="did not consume an attempt"):
        journal.update(forged_intent)
    forged_handle = delete_done.model_copy(
        update={
            "provider_operation_id": "op-00000003",
            "provider_operation_name": (
                f"projects/{record.project_id}/zones/{record.zone}/operations/op-00000003"
            ),
            "provider_operation_kind": "delete",
            "provider_operation_status": "PENDING",
            "provider_operation_terminal": False,
            "provider_mutation_ambiguous": True,
        }
    )
    with pytest.raises(
        GcpJournalError, match=r"terminal operation|operation identity"
    ):
        journal.update(forged_handle)
    second_intent = delete_done.model_copy(
        update={
            "delete_attempts": 2,
            "provider_mutation_ambiguous": True,
        }
    )
    journal.update(second_intent)
    second_pending = second_intent.model_copy(
        update={
            "provider_operation_id": "op-00000003",
            "provider_operation_name": (
                f"projects/{record.project_id}/zones/{record.zone}/operations/op-00000003"
            ),
            "provider_operation_kind": "delete",
            "provider_operation_status": "PENDING",
            "provider_operation_terminal": False,
        }
    )
    journal.update(second_pending)
    with pytest.raises(GcpJournalError, match="record is invalid"):
        journal.update(record.model_copy(update={"delete_attempts": 4}))


def test_delete_attempt_jump_cannot_skip_the_prepared_intent(
    tmp_path: Path,
) -> None:
    record = _prepared_record(tmp_path / "seed")
    root = tmp_path / "journal"
    root.mkdir()
    journal = GcpLeaseJournal(root)
    armed = record.model_copy(
        update={"state": "ARM_CONSUMED", "arm_consumed": True}
    )
    journal.reserve(record)
    journal.update(armed)
    insert_pending = armed.model_copy(
        update={
            "state": "CREATE_SUBMITTED",
            "provider_mutation_attempted": True,
            "provider_mutation_ambiguous": True,
            "provider_operation_id": "op-00000001",
            "provider_operation_name": (
                f"projects/{record.project_id}/zones/{record.zone}/operations/op-00000001"
            ),
            "provider_operation_kind": "insert",
            "provider_operation_status": "PENDING",
        }
    )
    journal.update(insert_pending)
    insert_done = insert_pending.model_copy(
        update={
            "provider_mutation_ambiguous": False,
            "provider_operation_status": "DONE",
            "provider_operation_terminal": True,
        }
    )
    journal.update(insert_done)
    jumped = insert_done.model_copy(
        update={
            "state": "CLEANUP_PENDING",
            "delete_attempts": 2,
            "provider_mutation_ambiguous": True,
        }
    )
    with pytest.raises(GcpJournalError, match="changed invalidly"):
        journal.update(jumped)


def test_sigkill_after_durable_insert_intent_recovers_as_orphan(tmp_path: Path) -> None:
    spec, inventory, context, plan = _inputs()
    arm = _arm()
    environment = _environment(plan)
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    quote, capacity = _quote_and_capacity(plan, arm, request)
    arms_root = tmp_path / "arms"
    journal_root = tmp_path / "journal"
    arms_root.mkdir()
    journal_root.mkdir()
    ready = tmp_path / "insert-called"

    class CrashAfterCall(FakeGcpComputeTransport):
        def insert(
            self, request: GcpInsertRequest, *, timeout_seconds: int, request_id: str
        ) -> Any:
            del request, timeout_seconds, request_id
            ready.write_text("1", encoding="ascii")
            while True:
                time.sleep(0.01)

    pid = os.fork()
    if pid == 0:
        child = GcpGuardedLifecycleController(
            transport=CrashAfterCall(),
            arm_store=FileExecutionArmStore(arms_root),
            journal=GcpLeaseJournal(journal_root),
            clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        )
        child.execute(
            plan=plan,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
            arm_bytes=canonical_gcp_execution_arm_bytes(arm),
            environment=environment,
            quote=quote,
            capacity=capacity,
        )
        os._exit(0)
    deadline = time.monotonic() + 3.0
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    os.kill(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL

    recovery = GcpGuardedLifecycleController(
        transport=FakeGcpComputeTransport(),
        arm_store=FileExecutionArmStore(arms_root),
        journal=GcpLeaseJournal(journal_root),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
    )
    outcome = recovery.recover_cleanup(
        controller_id=arm.controller_id, confirmation=GCP_RECOVERY_CONFIRMATION
    )
    assert outcome.status == "FAILED"
    assert outcome.orphaned is True
    assert outcome.cleanup_confirmed is False
    assert outcome.error_code == "AMBIGUOUS_MUTATION_UNRESOLVED"


@pytest.mark.parametrize(
    "field",
    [
        "quote_digest",
        "environment_digest",
        "estimated_max_microusd",
        "max_cleanup_attempts",
    ],
)
def test_journal_freezes_non_lifecycle_intent_fields(
    tmp_path: Path, field: str
) -> None:
    outcome, controller = _run(
        tmp_path, FakeGcpComputeTransport(), return_controller=True
    )
    assert outcome.status == "SUCCEEDED"
    record = controller.journal.load("ctl-12345678")
    replacement = {
        "quote_digest": "sha256:" + "f" * 64,
        "environment_digest": "sha256:" + "e" * 64,
        "estimated_max_microusd": record.estimated_max_microusd + 1,
        "max_cleanup_attempts": record.max_cleanup_attempts - 1,
    }[field]
    with pytest.raises(GcpJournalError):
        controller.journal.update(record.model_copy(update={field: replacement}))


def test_ambiguous_insert_finds_late_instance_and_deletes_exact_owner(
    tmp_path: Path,
) -> None:
    class LateInstanceTransport(FakeGcpComputeTransport):
        def __init__(self) -> None:
            super().__init__(
                insert_error="INSERT_AMBIGUOUS", insert_error_ambiguous=True
            )
            self.late_instance_revealed = False

        def get_instance(
            self, request: GcpInsertRequest, *, timeout_seconds: int
        ) -> Any:
            observation = super().get_instance(request, timeout_seconds=timeout_seconds)
            if (
                observation.state == "NOT_FOUND"
                and self.requests
                and not self.late_instance_revealed
            ):
                self.late_instance_revealed = True
                self._owned = self._running(request)
                return self._owned
            return observation

    transport = LateInstanceTransport()
    outcome = _run(tmp_path, transport)
    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "INSERT_AMBIGUOUS"
    assert outcome.cleanup_confirmed is True
    assert outcome.orphaned is False
    assert transport.delete_calls == 1


def test_known_pre_provider_rejection_is_not_marked_ambiguous(
    tmp_path: Path,
) -> None:
    transport = FakeGcpComputeTransport(insert_error="INSERT_REJECTED")
    outcome = _run(tmp_path, transport)
    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "INSERT_REJECTED"
    assert outcome.cleanup_confirmed is True
    assert outcome.orphaned is False
    assert transport.delete_calls == 0


def test_cleanup_attempt_budget_is_total_across_recovery(tmp_path: Path) -> None:
    transport = FakeGcpComputeTransport(delete_error="DELETE_DENIED")
    outcome, controller = _run(tmp_path, transport, return_controller=True)
    assert outcome.orphaned is True
    assert transport.delete_calls == 3
    recovered = controller.recover_cleanup(
        controller_id="ctl-12345678", confirmation="RECOVER_GCP_LEASE"
    )
    assert recovered.orphaned is True
    assert transport.delete_calls == 3


def test_request_binds_quote_and_capacity_exact_digests() -> None:
    _spec, _inventory, _context, plan = _inputs()
    arm = _arm()
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=_environment(plan))
    quote, capacity = _quote_and_capacity(plan, arm, request)
    from inferdrome.deployment import validate_cost_and_capacity

    with pytest.raises(ValueError, match="request binding"):
        validate_cost_and_capacity(
            plan=plan,
            arm=arm,
            request=request.model_copy(update={"boot_disk_size_gib": 1_000}),
            quote=quote,
            capacity=capacity,
            now=NOW,
        )


def test_provider_labels_and_request_ids_are_provider_safe() -> None:
    _spec, _inventory, _context, plan = _inputs()
    arm = _arm()
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=_environment(plan))
    for value in request.labels.model_dump(mode="json").values():
        assert len(value) <= 63
        assert all(
            character.islower() or character.isdigit() or character in "_-"
            for character in value
        )
    assert request.insert_request_id != "0" * 36
    assert len(request.insert_request_id) == 36
    assert len(request.delete_request_id) == 36


@pytest.mark.parametrize(
    "machine_type",
    [
        "a2-highgpu-2g",
        "a2-highgpu-4g",
        "a2-highgpu-8g",
        "a2-ultragpu-1g",
        "a2-highgpu-invented",
    ],
)
def test_a2_mapping_rejects_every_non_closed_machine(machine_type: str) -> None:
    with pytest.raises(GcpExecutionError, match="closed A2 profile"):
        validate_gcp_a2_profile(
            machine_type, "NVIDIA A100-SXM4-40GB", "nvidia-tesla-a100", 1
        )
    assert validate_gcp_a2_profile(
        "a2-highgpu-1g", "NVIDIA A100-SXM4-40GB", "nvidia-tesla-a100", 1
    ) == "provider"
    assert validate_gcp_a2_profile(
        "synthetic-a2-highgpu-1g",
        "NVIDIA A100-SXM4-40GB",
        "synthetic-a100-sxm4-40gb",
        1,
    ) == "synthetic"
    with pytest.raises(GcpExecutionError, match="closed A2 profile"):
        validate_gcp_a2_profile(
            "a2-highgpu-1g", "NVIDIA A100-SXM4-40GB", "nvidia-h100-80gb", 1
        )


def test_boot_image_requires_name_and_separate_provider_identity() -> None:
    with pytest.raises(ValidationError):
        GcpExecutionBootImage(
            image_name="projects/inferdrome-example/global/images/123456789",
            provider_image_id=123456789,
            digest="sha256:" + "1" * 64,
            status="READY",
        )


def test_image_observation_mismatch_blocks_insert_before_provider_mutation(
    tmp_path: Path,
) -> None:
    class WrongImage(FakeGcpComputeTransport):
        def get_image(
            self, request: GcpInsertRequest, *, timeout_seconds: int
        ) -> Any:
            return super().get_image(
                request, timeout_seconds=timeout_seconds
            ).model_copy(
                update={"provider_image_id": 999999999}
            )

    transport = WrongImage()
    outcome = _run(tmp_path, transport)
    assert outcome.status == "FAILED"
    assert outcome.error_code == "BOOT_IMAGE_OBSERVATION_MISMATCH"
    assert transport.insert_calls == 0

def test_boot_disk_source_mismatch_triggers_cleanup_before_work(
    tmp_path: Path,
) -> None:
    class WrongDisk(FakeGcpComputeTransport):
        def get_boot_disk(
            self, request: GcpInsertRequest, *, timeout_seconds: int
        ) -> Any:
            return super().get_boot_disk(
                request, timeout_seconds=timeout_seconds
            ).model_copy(
                update={"source_image_id": 999999999}
            )

    transport = WrongDisk()
    outcome = _run(tmp_path, transport)
    assert outcome.status == "FAILED"
    assert outcome.primary_error_code == "BOOT_DISK_IMAGE_MISMATCH"
    assert outcome.cleanup_confirmed is True
    assert transport.delete_calls == 1


def test_lazy_transport_factory_is_not_activated_before_arm_validation(
    tmp_path: Path,
) -> None:
    called = False

    def factory() -> Any:
        nonlocal called
        called = True
        return FakeGcpComputeTransport()

    spec, inventory, context, plan = _inputs()
    arm = _arm()
    environment = _environment(plan)
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    quote, capacity = _quote_and_capacity(plan, arm, request)
    controller = GcpGuardedLifecycleController(
        transport_factory=factory,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(tmp_path),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=_NoopFactorySafetySupervisor(),
    )
    with pytest.raises((GcpPlanError, ValueError)):
        controller.execute(
            plan=plan,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
            arm_bytes=b"{}",
            environment=environment,
            quote=quote,
            capacity=capacity,
        )
    assert called is False


@pytest.mark.parametrize("stale_kind", ["quote", "capacity"])
def test_lazy_factory_is_not_activated_for_stale_execution_inputs(
    tmp_path: Path, stale_kind: str
) -> None:
    called = False

    def factory() -> Any:
        nonlocal called
        called = True
        return FakeGcpComputeTransport()

    spec, inventory, context, plan = _inputs()
    arm = _arm()
    environment = _environment(plan)
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    quote, capacity = _quote_and_capacity(plan, arm, request)
    if stale_kind == "quote":
        quote = quote.model_copy(
            update={
                "issued_at": "2026-08-20T10:00:00Z",
                "valid_until": "2026-08-20T11:00:00Z",
            }
        )
    else:
        capacity = capacity.model_copy(
            update={"observed_at": "2026-08-20T10:00:00Z", "freshness_seconds": 1}
        )
    controller = GcpGuardedLifecycleController(
        transport_factory=factory,
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(tmp_path),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=_NoopFactorySafetySupervisor(),
    )
    with pytest.raises(GcpExecutionError):
        controller.execute(
            plan=plan,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
            arm_bytes=canonical_gcp_execution_arm_bytes(arm),
            environment=environment,
            quote=quote,
            capacity=capacity,
        )
    assert called is False


def test_lazy_factory_is_not_activated_when_journal_reservation_fails(
    tmp_path: Path,
) -> None:
    called = False

    class BrokenJournal(GcpLeaseJournal):
        def reserve(self, record: Any) -> None:
            del record
            raise GcpJournalError("journal reserve failed")

    def factory() -> Any:
        nonlocal called
        called = True
        return FakeGcpComputeTransport()

    spec, inventory, context, plan = _inputs()
    arm = _arm()
    environment = _environment(plan)
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    quote, capacity = _quote_and_capacity(plan, arm, request)
    controller = GcpGuardedLifecycleController(
        transport_factory=factory,
        arm_store=InMemoryExecutionArmStore(),
        journal=BrokenJournal(tmp_path),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=_NoopFactorySafetySupervisor(),
    )
    with pytest.raises(GcpJournalError):
        controller.execute(
            plan=plan,
            expected_spec=spec,
            expected_inventory=inventory,
            expected_context=context,
            arm_bytes=canonical_gcp_execution_arm_bytes(arm),
            environment=environment,
            quote=quote,
            capacity=capacity,
        )
    assert called is False


def test_lazy_factory_is_not_activated_when_arm_replay_is_rejected(
    tmp_path: Path,
) -> None:
    called = False

    class ReplayedArm:
        def consume(self, arm_id: str, arm_sha256: str) -> bool:
            del arm_id, arm_sha256
            return False

    def factory() -> Any:
        nonlocal called
        called = True
        return FakeGcpComputeTransport()

    spec, inventory, context, plan = _inputs()
    arm = _arm()
    environment = _environment(plan)
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    quote, capacity = _quote_and_capacity(plan, arm, request)
    controller = GcpGuardedLifecycleController(
        transport_factory=factory,
        arm_store=ReplayedArm(),
        journal=GcpLeaseJournal(tmp_path),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
        safety_supervisor=_NoopFactorySafetySupervisor(),
    )
    outcome = controller.execute(
        plan=plan,
        expected_spec=spec,
        expected_inventory=inventory,
        expected_context=context,
        arm_bytes=canonical_gcp_execution_arm_bytes(arm),
        environment=environment,
        quote=quote,
        capacity=capacity,
    )
    assert outcome.error_code == "ARM_ALREADY_CONSUMED"
    assert called is False


def test_optional_sdk_surface_matches_compute_1_50_when_extra_is_installed() -> None:
    sdk = pytest.importorskip("google.cloud.compute_v1")
    assert hasattr(sdk, "AttachedDiskInitializeParams")
    signature = inspect.signature(sdk.InstancesClient.insert)
    assert "instance_resource" in signature.parameters
    assert "instance" not in signature.parameters
    _spec, _inventory, _context, plan = _inputs()
    arm = _arm()
    from inferdrome.deployment import build_gcp_insert_request

    request = build_gcp_insert_request(
        plan=plan, arm=arm, environment=_environment(plan)
    )
    request = request.model_copy(
        update={
            "machine_type": "a2-highgpu-1g",
            "accelerator_provider_type": "nvidia-tesla-a100",
        }
    )

    operation = sdk.Operation(
        name="operation-1",
        status=sdk.Operation.Status.DONE,
        operation_type="insert",
        target_link=(
            "https://www.googleapis.com/compute/v1/projects/inferdrome-example/"
            "zones/us-central1-b/instances/inferdrome-ctl-12345678"
        ),
    )

    class Client:
        def insert(self, *, request: Any, timeout: int) -> Any:
            del timeout
            self.request = request
            return operation

        def delete(self, *, request: Any, timeout: int) -> Any:
            del timeout
            self.delete_request = request
            return operation

        def list(self, *, request: Any, timeout: int) -> list[Any]:
            del timeout
            self.list_request = request
            return []

    client = Client()
    transport = create_google_compute_transport(
        sdk_module=sdk,
        client=client,
        operations_client=object(),
        images_client=object(),
        disks_client=object(),
    )
    create_request = transport.project_create_request(
        request, request_id=request.insert_request_id
    )
    assert isinstance(create_request, sdk.InsertInstanceRequest)
    assert create_request.request_id == request.insert_request_id
    assert isinstance(
        create_request.instance_resource.disks[0].initialize_params,
        sdk.AttachedDiskInitializeParams,
    )
    assert (
        create_request.instance_resource.disks[0].initialize_params.source_image
        == request.boot_image.image_name
    )
    assert create_request.instance_resource.guest_accelerators == []
    assert (
        create_request.instance_resource.network_interfaces[0].stack_type == "IPV4_ONLY"
    )
    assert create_request.instance_resource.scheduling.max_run_duration.seconds == 600
    assert (
        create_request.instance_resource.scheduling.instance_termination_action
        == "DELETE"
    )
    with pytest.raises(GcpTransportError, match="LIVE_MUTATION_DISABLED"):
        transport.insert(
            request, timeout_seconds=7, request_id=request.insert_request_id
        )
    assert not hasattr(client, "request")
    transport.list_owned(request, timeout_seconds=7)
    assert isinstance(client.list_request, sdk.ListInstancesRequest)
    assert client.list_request.max_results == 256
    delete_request = transport.project_terminate_request(
        request, request_id=request.delete_request_id
    )
    assert isinstance(delete_request, sdk.DeleteInstanceRequest)
    assert delete_request.request_id == request.delete_request_id
    with pytest.raises(GcpTransportError, match="LIVE_MUTATION_DISABLED"):
        transport.delete(
            request, timeout_seconds=7, request_id=request.delete_request_id
        )
    assert not hasattr(client, "delete_request")
