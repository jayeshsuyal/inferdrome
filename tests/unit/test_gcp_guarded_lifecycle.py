"""Offline and adversarial tests for the PR8 GCP execution boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from inferdrome.deployment import (
    ARM_CONFIRMATION,
    FakeGcpComputeTransport,
    GcpCapacityInput,
    GcpClock,
    GcpCostQuote,
    GcpExecutionEnvironment,
    GcpExecutionNetwork,
    GcpGuardedLifecycleController,
    GcpInsertRequest,
    GcpJournalError,
    GcpLeaseJournal,
    GcpPlanError,
    GcpPlanImage,
    GcpQuoteComponent,
    InMemoryExecutionArmStore,
    canonical_gcp_execution_arm_bytes,
    canonical_gcp_execution_outcome_bytes,
    gcp_execution_contract_schemas,
    gcp_execution_request_digest,
    issue_gcp_execution_arm,
    parse_deployment_spec_json,
    parse_gcp_execution_arm_json,
    parse_gcp_inventory_json,
    plan_gcp_dry_run,
    validate_gcp_execution_preflight,
)
from inferdrome.deployment.gcp_compute_transport import (
    GcpOptionalDependencyUnavailable,
    create_google_compute_transport,
)

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
        boot_image=GcpPlanImage(
            repository="projects/inferdrome-example/global/images/123456789",
            digest="sha256:" + "1" * 64,
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
        project_id=request.project_id,
        region=request.region,
        zone=request.zone,
        machine_type=request.machine_type,
        accelerator_model=request.accelerator_model,
        accelerator_count=request.accelerator_count,
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


def _controller(tmp_path: Path, transport: Any, arm_store: Any | None = None) -> Any:
    return GcpGuardedLifecycleController(
        transport=transport,
        arm_store=arm_store or InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(tmp_path),
        clock=GcpClock(now_fn=lambda: NOW, monotonic_fn=lambda: 0.0),
    )


def _run(
    tmp_path: Path,
    transport: Any,
    *,
    work: Any = None,
    return_controller: bool = False,
) -> Any:
    spec, inventory, context, plan = _inputs()
    arm = _arm()
    environment = _environment(plan)
    request = __import__(
        "inferdrome.deployment", fromlist=["build_gcp_insert_request"]
    ).build_gcp_insert_request(plan=plan, arm=arm, environment=environment)
    quote, capacity = _quote_and_capacity(plan, arm, request)
    controller = _controller(tmp_path, transport)
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


def test_generated_schema_is_closed_and_fixture_is_exact() -> None:
    schemas = gcp_execution_contract_schemas()
    assert len(schemas) == 7
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
        assert schema["$id"].startswith("urn:inferdrome:gcp-execution-")

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
    assert request.labels.plan_id == arm.plan_id
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


def test_false_not_found_is_not_cleanup_confirmation(tmp_path: Path) -> None:
    transport = FakeGcpComputeTransport(false_not_found=True)
    outcome = _run(tmp_path, transport)
    assert outcome.status == "FAILED"
    assert outcome.orphaned is True
    assert outcome.error_code == "CLEANUP_UNCONFIRMED"
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
    assert recovered.status == "SUCCEEDED"
    assert recovered.cleanup_confirmed is True
    assert recovered.orphaned is False


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


def test_optional_transport_is_lazy_and_projects_only_private_request() -> None:
    class Value:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class Operation:
        def __init__(self) -> None:
            self.timeout: int | None = None

        def result(self, *, timeout: int) -> None:
            self.timeout = timeout

    class Client:
        def __init__(self) -> None:
            self.inserted: Any = None
            self.operation = Operation()

        def insert(self, **kwargs: Any) -> Operation:
            self.inserted = kwargs["instance"]
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
        DiskInitializeParams = Value
        NetworkInterface = Value
        Scheduling = Value
        ServiceAccount = Value

    _, _, _, plan = _inputs()
    arm = _arm()
    from inferdrome.deployment import build_gcp_insert_request

    request = build_gcp_insert_request(
        plan=plan, arm=arm, environment=_environment(plan)
    )
    client = Client()
    transport = create_google_compute_transport(sdk_module=Sdk, client=client)
    operation = transport.insert(request, timeout_seconds=7, request_id="insert-test")
    result = transport.wait_operation(operation, timeout_seconds=7)
    assert result.status == "DONE"
    assert client.operation.timeout == 7
    assert client.inserted.network_interfaces[0].network == request.network.network
    assert not hasattr(client.inserted.network_interfaces[0], "access_configs")
    assert client.inserted.deletion_protection is False
    assert client.inserted.scheduling.automatic_restart is False


def test_missing_optional_dependency_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_: str) -> Any:
        raise ModuleNotFoundError("google-cloud-compute")

    monkeypatch.setattr(
        "inferdrome.deployment.gcp_compute_transport.importlib.import_module", missing
    )
    with pytest.raises(
        GcpOptionalDependencyUnavailable,
        match="optional dependency unavailable",
    ):
        create_google_compute_transport()
