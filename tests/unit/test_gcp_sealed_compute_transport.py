"""Local fake-SDK coverage for the v2 sealed executable GCE boundary.

These tests use plain in-process fakes only.  They do not import Google,
discover credentials, open a network connection, or create a provider resource.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from inferdrome.deployment.gcp import GcpPlanImage
from inferdrome.deployment.gcp_compute_transport import (
    _WATCHDOG_CLEANUP_BOUND_TRANSPORT_SEAL,
    GcpV2SealedComputeTransportFactory,
    GcpV2WatchdogCleanupBoundTransport,
    _create_google_compute_transport_for_v2_activation_guard,
    create_google_compute_transport,
    create_google_compute_transport_for_v2_capability,
)
from inferdrome.deployment.gcp_lifecycle import (
    GcpExecutionBootImage,
    GcpExecutionLabels,
    GcpExecutionNetwork,
    GcpInsertRequest,
    GcpTransportError,
    gcp_execution_request_digest,
)
from inferdrome.deployment.gcp_supervisor import (
    _FUTURE_MUTATION_ACTIVATION_GUARD_SEAL,
    _MUTATION_ACTIVATION_PROOF_SEAL,
    _WATCHDOG_CLEANUP_HANDLE_SEAL,
    GcpV2ActivatedMutationProof,
    GcpWatchdogActivationReceipt,
    _CleanupHandleUse,
    _GcpV2FutureMutationActivationGuard,
    _GcpV2WatchdogCleanupHandle,
    _MutationProofUse,
)
from inferdrome.deployment.gcp_v2_contracts import (
    GcpV2MutationCapability,
    gcp_v2_startup_projection_digest,
    issue_gcp_v2_startup_projection,
)
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class _Value:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _Sdk:
    Instance = _Value
    AttachedDisk = _Value
    AttachedDiskInitializeParams = _Value
    AcceleratorConfig = _Value
    NetworkInterface = _Value
    Scheduling = _Value
    ServiceAccount = _Value
    InsertInstanceRequest = _Value
    DeleteInstanceRequest = _Value
    DeleteDiskRequest = _Value
    ListDisksRequest = _Value
    Metadata = _Value
    Items = _Value

    class Operation:
        class Status:
            PENDING = "PENDING"
            RUNNING = "RUNNING"
            DONE = "DONE"


class _MetadataTamperingSdk(_Sdk):
    class InsertInstanceRequest(_Value):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            cast(Any, self).instance_resource.metadata.items[
                0
            ].value = "tampered-binding"


class _NotFound(Exception):
    code = 404


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _compact(prefix: str, value: str) -> str:
    return prefix + "_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:48]


def _request() -> GcpInsertRequest:
    controller_id = "ctl-12345678"
    plan_id = _digest("a")
    arm_id = _digest("b")
    labels = GcpExecutionLabels(
        inferdrome="inferdrome",
        controller_id=_compact("c", controller_id),
        plan_id=_compact("p", plan_id),
        arm_id=_compact("a", arm_id),
        managed_by="inferdrome_gcp_execution_v1",
        role="provider-envelope",
    )
    return GcpInsertRequest(
        schema_version="inferdrome.gcp-execution-request.v1",
        project_id="inferdrome-example",
        region="us-central1",
        zone="us-central1-a",
        instance_name="inferdrome-ctl-12345678",
        machine_type="a2-highgpu-1g",
        architecture="amd64",
        accelerator_model="NVIDIA A100-SXM4-40GB",
        accelerator_provider_type="nvidia-tesla-a100",
        accelerator_count=1,
        boot_image=GcpExecutionBootImage(
            image_name="projects/inferdrome-example/global/images/inferdrome-base-v1",
            provider_image_id=123456789,
            digest=_digest("c"),
            status="READY",
        ),
        runner_image=GcpPlanImage(
            repository="registry.example/inferdrome-runner", digest=_digest("d")
        ),
        serving_runtime_image=GcpPlanImage(
            repository="registry.example/inferdrome-runtime", digest=_digest("e")
        ),
        boot_disk_size_gib=100,
        boot_disk_type="pd-balanced",
        boot_disk_auto_delete=True,
        network=GcpExecutionNetwork(
            network="projects/inferdrome-example/global/networks/private",
            subnetwork=(
                "projects/inferdrome-example/regions/us-central1/subnetworks/private"
            ),
            external_access_config="absent",
            ip_forwarding=False,
        ),
        service_account=(
            "inferdrome-runner@inferdrome-example.iam.gserviceaccount.com"
        ),
        service_account_scopes=("logging.write",),
        deletion_protection=False,
        automatic_restart=False,
        maintenance_policy="TERMINATE",
        provider_max_runtime_seconds=600,
        plan_id=plan_id,
        arm_id=arm_id,
        controller_id=controller_id,
        insert_request_id="123e4567-e89b-12d3-a456-426614174000",
        delete_request_id="123e4567-e89b-12d3-a456-426614174001",
        labels=labels,
        model_id="opaque-model-id",
        model_revision="1" * 40,
        tokenizer_revision="2" * 40,
        runtime_engine="vllm",
        runtime_version="0.26.0",
        endpoint_scope="private",
        runner_runtime_colocation="colocated",
        accelerator_attachment_mode="a2_fixed_gpu",
        startup_script_digest=None,
    )


def _capability_and_proof(
    request: GcpInsertRequest,
    *,
    payload: bytes = b"opaque-v2-test-payload",
) -> tuple[Any, GcpV2ActivatedMutationProof]:
    projection = issue_gcp_v2_startup_projection(
        request=request, execution_payload=payload
    )
    capability_payload: dict[str, Any] = {
        "schema_version": "inferdrome.gcp-mutation-capability.v2",
        "capability_kind": "one_exact_future_create",
        "activation_contract_digest": _digest("f"),
        "approval_digest": _digest("0"),
        "controller_id": request.controller_id,
        "request_digest": gcp_execution_request_digest(request),
        "startup_projection_digest": gcp_v2_startup_projection_digest(projection),
        "execution_payload_digest": projection.execution_payload_digest,
        "project_id": request.project_id,
        "region": request.region,
        "zone": request.zone,
        "instance_name": request.instance_name,
        "labels": request.labels.model_dump(mode="json"),
        "activated_at": "2026-09-01T12:00:00Z",
        "provider_runtime_deadline_at": "2026-09-01T12:10:00Z",
        "watchdog_cleanup_deadline_at": "2026-09-01T12:20:00Z",
    }
    capability = GcpV2MutationCapability(
        **capability_payload,
        capability_id=digest_bytes(
            DigestDomain.GCP_EXECUTION_APPROVAL,
            canonical_json_bytes(capability_payload),
        ),
    )
    receipt_payload: dict[str, Any] = {
        "schema_version": "inferdrome.gcp-watchdog-activation-receipt.v2",
        "watchdog_id": "watchdog-local",
        "approval_digest": capability.approval_digest,
        "activation_deadline_digest": capability.activation_contract_digest,
        "request_digest": capability.request_digest,
        "startup_projection_digest": capability.startup_projection_digest,
        "execution_payload_digest": capability.execution_payload_digest,
        "controller_id": capability.controller_id,
        "capability_id": capability.capability_id,
        "activated_at": capability.activated_at,
        "provider_runtime_deadline_at": capability.provider_runtime_deadline_at,
        "watchdog_cleanup_deadline_at": capability.watchdog_cleanup_deadline_at,
        "ready": True,
        "independently_durable": True,
    }
    receipt = GcpWatchdogActivationReceipt(
        **receipt_payload,
        receipt_id=digest_bytes(
            DigestDomain.GCP_EXECUTION_WATCHDOG,
            canonical_json_bytes(receipt_payload),
        ),
    )
    return projection, GcpV2ActivatedMutationProof(
        capability=capability,
        receipt=receipt,
        _seal=_MUTATION_ACTIVATION_PROOF_SEAL,
        _use=_MutationProofUse(),
    )


def _test_only_activation_guard(
    proof: GcpV2ActivatedMutationProof,
) -> object:
    """Exercise the private fake-SDK route with a sealed test handoff.

    The real construction guard is minted only by ``GcpLifecycleSupervisor``;
    integration coverage exercises that path.  This lightweight fake keeps
    provider-boundary tests independent of a full on-disk watchdog setup while
    retaining the exact capability/binding checks at the private factory.
    """

    capability = proof.capability
    return _GcpV2FutureMutationActivationGuard(
        proof=proof,
        binding=_Value(
            approval_digest=capability.approval_digest,
            activation_deadline_digest=capability.activation_contract_digest,
            request_digest=capability.request_digest,
            startup_projection_digest=capability.startup_projection_digest,
            execution_payload_digest=capability.execution_payload_digest,
            controller_id=capability.controller_id,
            project_id=capability.project_id,
            region=capability.region,
            zone=capability.zone,
            instance_name=capability.instance_name,
            labels=capability.labels,
        ),
        rate_basis_digest=_digest("1"),
        cost_guard_digest=_digest("2"),
        _seal=_FUTURE_MUTATION_ACTIVATION_GUARD_SEAL,
    )


class _FakeInstancesClient:
    def __init__(self, request: GcpInsertRequest) -> None:
        self.request = request
        self.insert_calls = 0
        self.delete_calls = 0
        self.insert_request: Any | None = None
        self.delete_request: Any | None = None

    def _instance(self) -> _Value:
        request = self.request
        instance_link = (
            "https://www.googleapis.com/compute/v1/projects/"
            f"{request.project_id}/zones/{request.zone}/instances/{request.instance_name}"
        )
        disk_link = (
            "https://www.googleapis.com/compute/v1/projects/"
            f"{request.project_id}/zones/{request.zone}/disks/"
            "inferdrome-ctl-87654321"
        )
        return _Value(
            name=request.instance_name,
            id=123456790,
            self_link=instance_link,
            status="RUNNING",
            labels=request.labels.model_dump(mode="json"),
            zone=(
                "https://www.googleapis.com/compute/v1/projects/"
                f"{request.project_id}/zones/{request.zone}"
            ),
            machine_type=(
                "https://www.googleapis.com/compute/v1/projects/"
                f"{request.project_id}/zones/{request.zone}/machineTypes/"
                f"{request.machine_type}"
            ),
            guest_accelerators=[],
            can_ip_forward=False,
            network_interfaces=[
                _Value(
                    network=request.network.network,
                    subnetwork=request.network.subnetwork,
                    stack_type="IPV4_ONLY",
                    access_configs=[],
                    ipv6_access_configs=[],
                    external_ipv6=None,
                    ipv6_address=None,
                    ipv6_access_type=None,
                    network_i_p="10.0.0.2",
                )
            ],
            disks=[
                _Value(
                    boot=True,
                    auto_delete=True,
                    source=disk_link,
                    device_name="boot",
                )
            ],
        )

    def insert(self, *, request: Any, timeout: int) -> _Value:
        del timeout
        self.insert_calls += 1
        self.insert_request = request
        return _Value(
            name="insert-op-1",
            operation_type="insert",
            target_link=(
                "https://www.googleapis.com/compute/v1/projects/"
                f"{self.request.project_id}/zones/{self.request.zone}/instances/"
                f"{self.request.instance_name}"
            ),
            status="DONE",
            error=None,
        )

    def delete(self, *, request: Any, timeout: int) -> _Value:
        del timeout
        self.delete_calls += 1
        self.delete_request = request
        return _Value(
            name="delete-instance-op-1",
            operation_type="delete",
            target_link=(
                "https://www.googleapis.com/compute/v1/projects/"
                f"{self.request.project_id}/zones/{self.request.zone}/instances/"
                f"{self.request.instance_name}"
            ),
            status="DONE",
            error=None,
        )

    def get(self, *, project: str, zone: str, instance: str, timeout: int) -> _Value:
        del timeout
        assert (project, zone, instance) == (
            self.request.project_id,
            self.request.zone,
            self.request.instance_name,
        )
        return self._instance()

    def list(self, *, request: Any, timeout: int) -> list[_Value]:
        del request, timeout
        return [self._instance()]


class _FakeDisksClient:
    def __init__(self, request: GcpInsertRequest) -> None:
        self.request = request
        self.present = True
        self.delete_request: Any | None = None

    def _disk(self) -> _Value:
        request = self.request
        root = "https://www.googleapis.com/compute/v1/projects/"
        disk_link = (
            f"{root}{request.project_id}/zones/{request.zone}/disks/"
            "inferdrome-ctl-87654321"
        )
        return _Value(
            name="inferdrome-ctl-87654321",
            id=987654321,
            self_link=disk_link,
            source_image=(
                f"{root}{request.project_id}/global/images/inferdrome-base-v1"
            ),
            source_image_id=request.boot_image.provider_image_id,
            type=(
                f"{root}{request.project_id}/zones/{request.zone}/diskTypes/"
                f"{request.boot_disk_type}"
            ),
            labels=request.labels.model_dump(mode="json"),
            users=[
                f"{root}{request.project_id}/zones/{request.zone}/instances/"
                f"{request.instance_name}"
            ],
            size_gb=request.boot_disk_size_gib,
        )

    def get(self, *, project: str, zone: str, disk: str, timeout: int) -> _Value:
        del timeout
        assert (project, zone, disk) == (
            self.request.project_id,
            self.request.zone,
            "inferdrome-ctl-87654321",
        )
        if not self.present:
            raise _NotFound()
        return self._disk()

    def list(self, *, request: Any, timeout: int) -> list[_Value]:
        del timeout
        assert request.max_results == 2
        assert "labels.controller_id" in request.filter
        return [self._disk()]

    def delete(self, *, request: Any, timeout: int) -> _Value:
        del timeout
        self.delete_request = request
        return _Value(
            name="delete-disk-op-1",
            operation_type="delete",
            target_link=(
                "https://www.googleapis.com/compute/v1/projects/"
                f"{self.request.project_id}/zones/{self.request.zone}/disks/"
                "inferdrome-ctl-87654321"
            ),
            status="DONE",
            error=None,
        )


class _FakeOperationsClient:
    def get(self, **_: Any) -> _Value:
        raise AssertionError("cached exact fake operation should be reconciled first")


class _FakeImagesClient:
    def get(self, **_: Any) -> _Value:
        raise AssertionError("sealed fake image read was not needed by this test")


def _sealed_transport(
    request: GcpInsertRequest,
) -> tuple[Any, _FakeInstancesClient, _FakeDisksClient, Any]:
    projection, proof = _capability_and_proof(request)
    instances = _FakeInstancesClient(request)
    disks = _FakeDisksClient(request)
    transport = _create_google_compute_transport_for_v2_activation_guard(
        activation_guard=_test_only_activation_guard(proof),
        startup_projection=projection,
        now=NOW,
        now_fn=lambda: NOW,
        sdk_module=_Sdk,
        client=instances,
        operations_client=_FakeOperationsClient(),
        images_client=_FakeImagesClient(),
        disks_client=disks,
    )
    return transport, instances, disks, projection


def test_generic_injected_transport_remains_permanently_mutation_disabled() -> None:
    request = _request()
    instances = _FakeInstancesClient(request)
    generic = create_google_compute_transport(
        sdk_module=_Sdk,
        client=instances,
        operations_client=_FakeOperationsClient(),
        images_client=_FakeImagesClient(),
        disks_client=_FakeDisksClient(request),
    )

    with pytest.raises(GcpTransportError, match="LIVE_MUTATION_DISABLED"):
        generic.insert(request, timeout_seconds=5, request_id=request.insert_request_id)

    assert instances.insert_calls == 0


def test_sealed_proof_route_projects_opaque_metadata_without_raw_escape() -> None:
    request = _request()
    transport, instances, disks, projection = _sealed_transport(request)

    insert = transport.insert(
        request, timeout_seconds=5, request_id=request.insert_request_id
    )
    assert insert.operation_kind == "insert"
    assert instances.insert_calls == 1
    assert instances.insert_request is not None
    metadata = {
        item.key: item.value
        for item in instances.insert_request.instance_resource.metadata.items
    }
    assert metadata == {
        "inferdrome-v2-startup-projection-id": projection.projection_id,
        "inferdrome-v2-request-digest": gcp_execution_request_digest(request),
        "inferdrome-v2-execution-payload-digest": projection.execution_payload_digest,
    }
    assert all("opaque-v2-test-payload" not in value for value in metadata.values())
    assert request.startup_script_digest is None

    # The capability wrapper never exposes its broader sealed implementation.
    # Exact disk cleanup must instead arrive through a separate opaque cleanup
    # handle/factory edge.
    assert not hasattr(transport, "_transport_after_authority")
    with pytest.raises(AttributeError):
        transport._transport_after_authority()  # type: ignore[attr-defined]
    assert disks.delete_request is None


def test_sealed_route_revalidates_projected_metadata_before_sdk_dispatch() -> None:
    request = _request()
    projection, proof = _capability_and_proof(request)
    instances = _FakeInstancesClient(request)
    transport = _create_google_compute_transport_for_v2_activation_guard(
        activation_guard=_test_only_activation_guard(proof),
        startup_projection=projection,
        now=NOW,
        now_fn=lambda: NOW,
        sdk_module=_MetadataTamperingSdk,
        client=instances,
        operations_client=_FakeOperationsClient(),
        images_client=_FakeImagesClient(),
        disks_client=_FakeDisksClient(request),
    )

    with pytest.raises(GcpTransportError, match="SEALED_GCE_METADATA_MISMATCH"):
        transport.insert(
            request, timeout_seconds=5, request_id=request.insert_request_id
        )

    assert instances.insert_calls == 0


def test_sealed_component_supplier_is_lazy_until_exact_insert() -> None:
    request = _request()
    projection, proof = _capability_and_proof(request)
    instances = _FakeInstancesClient(request)
    disks = _FakeDisksClient(request)
    calls = 0

    def supplier() -> tuple[Any, Any, Any, Any, Any]:
        nonlocal calls
        calls += 1
        return (
            _Sdk,
            instances,
            _FakeOperationsClient(),
            _FakeImagesClient(),
            disks,
        )

    transport = _create_google_compute_transport_for_v2_activation_guard(
        activation_guard=_test_only_activation_guard(proof),
        startup_projection=projection,
        now=NOW,
        now_fn=lambda: NOW,
        component_supplier=supplier,
    )
    assert calls == 0

    transport.insert(request, timeout_seconds=5, request_id=request.insert_request_id)

    assert calls == 1
    assert instances.insert_calls == 1


def test_watchdog_cleanup_adapter_cannot_escape_to_create_or_raw_transport() -> None:
    request = _request()
    projection, proof = _capability_and_proof(request)
    supplier_calls = 0

    def supplier() -> Any:
        nonlocal supplier_calls
        supplier_calls += 1
        raise AssertionError("forbidden cleanup operation reached supplier")

    transport = GcpV2WatchdogCleanupBoundTransport(
        transport_supplier=supplier,
        capability=proof.capability,
        startup_projection=projection,
        now_fn=lambda: NOW,
        _seal=_WATCHDOG_CLEANUP_BOUND_TRANSPORT_SEAL,
    )

    with pytest.raises(GcpTransportError, match="WATCHDOG_CLEANUP_CREATE_FORBIDDEN"):
        transport.insert(
            request, timeout_seconds=5, request_id=request.insert_request_id
        )
    with pytest.raises(GcpTransportError, match="SEALED_TRANSPORT_OPERATION_FORBIDDEN"):
        transport._call_transport_after_authority(
            "insert", request, timeout_seconds=5, request_id=request.insert_request_id
        )

    assert not hasattr(transport, "_transport_after_authority")
    assert supplier_calls == 0


def test_nominal_sealed_factory_uses_private_guard_before_lazy_components() -> None:
    request = _request()
    projection, proof = _capability_and_proof(request)
    instances = _FakeInstancesClient(request)
    disks = _FakeDisksClient(request)
    calls = 0

    def supplier() -> tuple[Any, Any, Any, Any, Any]:
        nonlocal calls
        calls += 1
        return (
            _Sdk,
            instances,
            _FakeOperationsClient(),
            _FakeImagesClient(),
            disks,
        )

    factory = GcpV2SealedComputeTransportFactory(
        component_supplier=supplier,
        now_fn=lambda: NOW,
        startup_projection=projection,
    )

    with pytest.raises(GcpTransportError, match="MUTATION_ACTIVATION_GUARD_INVALID"):
        factory.bind_mutation(proof)
    assert calls == 0

    transport = factory.bind_mutation(_test_only_activation_guard(proof))
    assert calls == 0
    transport.insert(request, timeout_seconds=5, request_id=request.insert_request_id)

    assert calls == 1
    assert instances.insert_calls == 1


def test_nominal_sealed_factory_cleanup_handle_is_no_create_and_lazy() -> None:
    """A watchdog handoff reaches only the sealed no-create GCE subtype."""

    request = _request()
    projection, proof = _capability_and_proof(request)
    instances = _FakeInstancesClient(request)
    disks = _FakeDisksClient(request)
    calls = 0

    def supplier() -> tuple[Any, Any, Any, Any, Any]:
        nonlocal calls
        calls += 1
        return (
            _Sdk,
            instances,
            _FakeOperationsClient(),
            _FakeImagesClient(),
            disks,
        )

    factory = GcpV2SealedComputeTransportFactory(
        component_supplier=supplier,
        now_fn=lambda: NOW,
        startup_projection=projection,
    )
    # This is a test-only equivalent of the private watchdog mint: production
    # code can produce the handle only after reopening a durable ACTIVE event.
    handle = _GcpV2WatchdogCleanupHandle(
        binding=_test_only_activation_guard(proof).binding,
        capability=proof.capability,
        activation_receipt_id=proof.receipt.receipt_id,
        _seal=_WATCHDOG_CLEANUP_HANDLE_SEAL,
        _use=_CleanupHandleUse(),
    )

    cleanup = factory.bind_watchdog_cleanup(handle)
    assert calls == 0
    with pytest.raises(GcpTransportError, match="WATCHDOG_CLEANUP_CREATE_FORBIDDEN"):
        cleanup.insert(
            request, timeout_seconds=5, request_id=request.insert_request_id
        )
    with pytest.raises(GcpTransportError, match="SEALED_TRANSPORT_OPERATION_FORBIDDEN"):
        cleanup._call_transport_after_authority(
            "insert",
            request,
            timeout_seconds=5,
            request_id=request.insert_request_id,
        )
    assert calls == 0

    cleanup.delete(
        request, timeout_seconds=5, request_id=request.delete_request_id
    )

    assert calls == 1
    assert instances.insert_calls == 0
    assert instances.delete_calls == 1


def test_payload_substitution_or_direct_proof_never_constructs_components() -> None:
    request = _request()
    projection, proof = _capability_and_proof(request)
    substituted = issue_gcp_v2_startup_projection(
        request=request, execution_payload=b"substituted-opaque-payload"
    )
    calls = 0

    def supplier() -> tuple[Any, Any, Any, Any, Any]:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid authority reached the component supplier")

    with pytest.raises(GcpTransportError, match="MUTATION_CAPABILITY_PAYLOAD_MISMATCH"):
        _create_google_compute_transport_for_v2_activation_guard(
            activation_guard=_test_only_activation_guard(proof),
            startup_projection=substituted,
            now=NOW,
            now_fn=lambda: NOW,
            component_supplier=supplier,
        )
    assert calls == 0

    with pytest.raises(GcpTransportError, match="MUTATION_ACTIVATION_GUARD_REQUIRED"):
        create_google_compute_transport_for_v2_capability(
            capability=proof,
            startup_projection=projection,
            now=NOW,
            now_fn=lambda: NOW,
            component_supplier=supplier,
        )
    assert calls == 0
