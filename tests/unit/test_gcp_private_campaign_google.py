"""Fake-SDK tests for the lazy two-engine GCE projection; no provider access."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

import inferdrome.deployment.gcp_private_campaign_google as campaign_google
from inferdrome.deployment.gcp_private_campaign_google import (
    GoogleGcpPrivateCampaignTransport,
    _ComputeCredentialBinding,
    _IapTunnelEndpoints,
    _metric_is_present,
    render_gcp_private_campaign_engine_argv,
    render_gcp_private_campaign_runner_argv,
    render_gcp_private_campaign_startup_script,
)
from inferdrome.deployment.gcp_private_campaign_v2 import (
    GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION,
    GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION,
    GCP_PRIVATE_CAMPAIGN_CLEANUP_AUTHORIZATION_SCHEMA_VERSION,
    GCP_PRIVATE_CAMPAIGN_CLEANUP_CONFIRMATION,
    GCP_PRIVATE_CAMPAIGN_RUNNER_ATTESTATION_SCHEMA_VERSION,
    GcpPrivateCampaignApproval,
    GcpPrivateCampaignCleanupAuthorization,
    GcpPrivateCampaignCreateRequest,
    GcpPrivateCampaignError,
    GcpPrivateCampaignEvidenceReceipt,
    GcpPrivateCampaignExactResourceBinding,
    GcpPrivateCampaignHandoffReceipt,
    GcpPrivateCampaignJournal,
    GcpPrivateCampaignProposalPayload,
    GcpPrivateCampaignRunnerAttestation,
    build_gcp_private_campaign_create_request,
    issue_gcp_private_campaign_proposal,
)
from inferdrome.deployment.gcp_private_campaign_watchdog_v3 import (
    GcpPrivateCampaignWatchdog,
    GcpPrivateCampaignWatchdogCapability,
)
from inferdrome.routing_execution.canonical import sha256_digest
from tests.unit.test_gcp_private_campaign_v2 import _proposal


@dataclass
class _Record:
    value: dict[str, Any]

    def __init__(self, **kwargs: Any) -> None:
        self.value = kwargs
        for key, child in kwargs.items():
            setattr(self, key, child)


class _Sdk:
    Instance = _Record
    AttachedDisk = _Record
    AttachedDiskInitializeParams = _Record
    NetworkInterface = _Record
    ServiceAccount = _Record
    Tags = _Record
    Scheduling = _Record
    Metadata = _Record
    Items = _Record
    InsertInstanceRequest = _Record
    DeleteInstanceRequest = _Record
    DeleteDiskRequest = _Record
    GetDiskRequest = _Record
    ListDisksRequest = _Record
    ListInstancesRequest = _Record


class _NoCalls:
    def insert(self, *, request: object, timeout: int) -> object:
        del request, timeout
        raise AssertionError("provider insert was not expected")

    def get(self, *, project: str, zone: str, instance: str, timeout: int) -> object:
        del project, zone, instance, timeout
        raise AssertionError("provider get was not expected")

    def delete(self, *, request: object, timeout: int) -> object:
        del request, timeout
        raise AssertionError("provider delete was not expected")

    def list(self, *, request: object, timeout: int) -> list[object]:
        del request, timeout
        raise AssertionError("provider list was not expected")


class _NoImages:
    def get(self, *, project: str, image: str, timeout: int) -> object:
        del project, image, timeout
        raise AssertionError("provider image read was not expected")


class _NoFirewalls:
    def get(self, *, project: str, firewall: str, timeout: int) -> object:
        del project, firewall, timeout
        raise AssertionError("provider firewall read was not expected")


class _Rows(list[object]):
    next_page_token: str | None = None


class _ReadyImage:
    def __init__(self, row: object) -> None:
        self.row = row
        self.calls = 0

    def get(self, *, project: str, image: str, timeout: int) -> object:
        del project, image, timeout
        self.calls += 1
        return self.row


class _ReadyFirewall:
    def __init__(self, row: object) -> None:
        self.row = row
        self.calls = 0

    def get(self, *, project: str, firewall: str, timeout: int) -> object:
        del project, firewall, timeout
        self.calls += 1
        return self.row


class _Runner:
    def __init__(self) -> None:
        self.runner_origin: str | None = None
        self.closed = False

    def bind_evidence_root(self, root: object) -> None:
        del root

    def bind_iap_runner_origin(self, origin: str) -> None:
        self.runner_origin = origin

    def readiness_attestation(
        self, request: Any, *, timeout_seconds: int
    ) -> GcpPrivateCampaignRunnerAttestation:
        del timeout_seconds
        runner = request.startup_payload.runner
        return GcpPrivateCampaignRunnerAttestation(
            schema_version=GCP_PRIVATE_CAMPAIGN_RUNNER_ATTESTATION_SCHEMA_VERSION,
            container_name=runner.container_name,
            private_port=runner.private_port,
            runner_image=runner.runner_image,
            container_command_module=runner.container_command_module,
            adapter_source_sha256=runner.adapter_source_sha256,
            runner_command_sha256=runner.runner_command_sha256,
            startup_payload_digest=request.proposal.startup_payload_digest,
            docker_network=runner.docker_network,
            gpu_access=runner.gpu_access,
            cloud_credentials=runner.cloud_credentials,
            docker_socket=runner.docker_socket,
            serving_role=runner.serving_role,
            provider_mutation_authority=runner.provider_mutation_authority,
        )

    def close(self) -> None:
        self.closed = True


class _RecordingRunner(_Runner):
    def __init__(self) -> None:
        super().__init__()
        self.handoff_calls: list[tuple[bytes, bytes, int]] = []
        self.retrieve_calls: list[tuple[GcpPrivateCampaignHandoffReceipt, int]] = []

    def handoff(
        self,
        request: Any,
        *,
        routing_config: bytes,
        workload: bytes,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignHandoffReceipt:
        self.handoff_calls.append((routing_config, workload, timeout_seconds))
        return GcpPrivateCampaignHandoffReceipt(
            proposal_id=request.proposal.proposal_id,
            routing_config_sha256=sha256_digest(routing_config),
            selected_workload_sha256=request.proposal.routing.selected_workload_sha256,
            endpoint_a_origin_sha256="sha256:" + ("a" * 64),
            endpoint_b_origin_sha256="sha256:" + ("b" * 64),
            routed_after_verified_readiness=True,
            co_located_freshness_admitted=True,
            runner_command_sha256=request.startup_payload.runner.runner_command_sha256,
        )

    def retrieve(
        self,
        request: Any,
        *,
        admitted_handoff: GcpPrivateCampaignHandoffReceipt,
        timeout_seconds: int,
    ) -> GcpPrivateCampaignEvidenceReceipt:
        self.retrieve_calls.append((admitted_handoff, timeout_seconds))
        return GcpPrivateCampaignEvidenceReceipt(
            proposal_id=request.proposal.proposal_id,
            retained_digest="sha256:" + ("c" * 64),
            collection_mode="CREATE_NO_REPLACE_RETRIEVAL",
            raw_prompt_or_output_retained=False,
        )


class _IapTunnels:
    def __init__(self) -> None:
        self.open_calls = 0
        self.closed = False
        self.preflight_calls = 0
        self.principal_error: Exception | None = None

    def preflight_principal(self, request: object) -> None:
        del request
        self.preflight_calls += 1
        if self.principal_error is not None:
            raise self.principal_error

    def open(self, request: object, *, timeout_seconds: int) -> _IapTunnelEndpoints:
        del request, timeout_seconds
        self.open_calls += 1
        return _IapTunnelEndpoints(
            readiness_origins=("http://127.0.0.1:18000", "http://127.0.0.1:18001"),
            runner_control_origin="http://127.0.0.1:18002",
        )

    def close(self) -> None:
        self.closed = True


class _CredentialResolver:
    def __init__(self, *, effective_principal: str) -> None:
        self.effective_principal = effective_principal
        self.calls: list[str] = []
        self.credential = object()

    def resolve(self, controller_principal: str) -> _ComputeCredentialBinding:
        self.calls.append(controller_principal)
        return _ComputeCredentialBinding(
            credentials=self.credential,
            effective_principal=self.effective_principal,
        )


class _InsertCounter:
    def __init__(
        self, current: object | None = None, rows: _Rows | None = None
    ) -> None:
        self.current = current
        self.rows = rows or _Rows()
        self.insert_calls = 0
        self.delete_calls = 0

    def insert(self, *, request: object, timeout: int) -> object:
        del request, timeout
        self.insert_calls += 1
        return _Record(name="create-operation")

    def get(self, *, project: str, zone: str, instance: str, timeout: int) -> object:
        del project, zone, instance, timeout
        if self.current is None:
            raise _NotFound()
        return self.current

    def delete(self, *, request: object, timeout: int) -> object:
        del request, timeout
        self.delete_calls += 1
        return _Record(name="delete-operation")

    def list(self, *, request: object, timeout: int) -> _Rows:
        del request, timeout
        return self.rows


class _DiskRows:
    def __init__(self, rows: _Rows) -> None:
        self.rows = rows
        self.delete_calls = 0

    def list(self, *, request: object, timeout: int) -> _Rows:
        del request, timeout
        return self.rows

    def get(self, *, request: object, timeout: int) -> object:
        del request, timeout
        if not self.rows:
            raise _NotFound()
        return self.rows[0]

    def delete(self, *, request: object, timeout: int) -> object:
        del request, timeout
        self.delete_calls += 1
        return _Record(name="disk-delete-operation")


class _NotFound(Exception):
    def code(self) -> int:
        return 404


def _full_instance(
    proposal: object,
    *,
    provider_instance_id: str = "123456789",
    request: Any | None = None,
) -> _Record:
    topology = proposal.topology
    labels = proposal.ownership_labels.model_dump(mode="json")
    metadata_items: list[_Record] = []
    if request is not None:
        script = render_gcp_private_campaign_startup_script(request)
        metadata_items = [
            _Record(key="startup-script", value=script),
            _Record(
                key="inferdrome-startup-script-sha256",
                value=sha256_digest(script.encode("utf-8")),
            ),
            _Record(
                key="inferdrome-startup-payload-digest",
                value=proposal.startup_payload_digest,
            ),
            _Record(key="inferdrome-proposal-digest", value=proposal.proposal_id),
            _Record(
                key="inferdrome-create-request-digest",
                value=request.create_request_digest,
            ),
            _Record(key="block-project-ssh-keys", value="TRUE"),
            _Record(key="enable-oslogin", value="FALSE"),
        ]
    return _Record(
        name=proposal.instance_name,
        id=provider_instance_id,
        labels=labels,
        machine_type=(
            "https://www.googleapis.com/compute/v1/projects/"
            f"{topology.project_id}/zones/{topology.zone}/machineTypes/a2-highgpu-2g"
        ),
        network_interfaces=[
            _Record(
                network=topology.private_network,
                subnetwork=topology.private_subnetwork,
                stack_type="IPV4_ONLY",
                access_configs=[],
                ipv6_access_configs=[],
                network_i_p="10.23.0.17",
            )
        ],
        service_accounts=[
            _Record(
                email=proposal.guest_service_account.service_account_email,
                scopes=[],
            )
        ],
        tags=_Record(items=[proposal.iap_connectivity.instance_network_tag]),
        guest_accelerators=[],
        can_ip_forward=False,
        status="RUNNING",
        disks=[
            _Record(
                boot=True,
                auto_delete=True,
                device_name=proposal.boot_disk_name,
            ),
            _Record(boot=False, auto_delete=True, interface="NVME", type="SCRATCH"),
            _Record(boot=False, auto_delete=True, interface="NVME", type="SCRATCH"),
        ],
        deletion_protection=False,
        scheduling=_Record(
            automatic_restart=False,
            on_host_maintenance="TERMINATE",
            instance_termination_action="DELETE",
            max_run_duration=_Record(seconds=proposal.max_runtime_seconds),
        ),
        metadata=_Record(items=metadata_items),
    )


def _live_boot_disk(
    proposal: object,
    *,
    provider_disk_id: str = "246813579",
    attached: bool = True,
) -> _Record:
    return _Record(
        name=proposal.boot_disk_name,
        id=provider_disk_id,
        labels=proposal.ownership_labels.model_dump(mode="json"),
        self_link=(
            "https://www.googleapis.com/compute/v1/projects/"
            f"{proposal.topology.project_id}/zones/{proposal.topology.zone}/disks/"
            f"{proposal.boot_disk_name}"
        ),
        source_image=proposal.boot_image.image_ref,
        source_image_id=proposal.boot_image.provider_image_id,
        users=(
            [
                "https://www.googleapis.com/compute/v1/projects/"
                f"{proposal.topology.project_id}/zones/{proposal.topology.zone}/instances/"
                f"{proposal.instance_name}"
            ]
            if attached
            else []
        ),
    )


def _live_firewall(proposal: object) -> _Record:
    return _Record(
        id=proposal.iap_connectivity.firewall_provider_id,
        self_link=(
            "https://www.googleapis.com/compute/v1/"
            f"{proposal.iap_connectivity.firewall_rule_ref}"
        ),
        direction="INGRESS",
        disabled=False,
        source_ranges=[proposal.iap_connectivity.iap_source_cidr],
        target_tags=[proposal.iap_connectivity.instance_network_tag],
        allowed=[_Record(i_p_protocol="tcp", ports=["8000", "8001", "8002"])],
        denied=[],
    )


def _adapter(
    *,
    instances: _InsertCounter | None = None,
    disks: _DiskRows | None = None,
    images: object | None = None,
    firewalls: object | None = None,
    runner: _Runner | None = None,
    iap_tunnels: _IapTunnels | None = None,
    monotonic: object | None = None,
    sleeper: object | None = None,
    pre_insert_guard: object | None = None,
    evidence_root: Path | None = None,
    watchdog_capability: GcpPrivateCampaignWatchdogCapability | None = None,
) -> GoogleGcpPrivateCampaignTransport:
    adapter = GoogleGcpPrivateCampaignTransport(
        sdk=_Sdk,
        instances_client=instances or _InsertCounter(),
        disks_client=disks or _DiskRows(_Rows()),
        images_client=images or _NoImages(),  # type: ignore[arg-type]
        firewalls_client=firewalls or _NoFirewalls(),  # type: ignore[arg-type]
        iap_tunnels=iap_tunnels or _IapTunnels(),  # type: ignore[arg-type]
        monotonic=monotonic,  # type: ignore[arg-type]
        sleeper=sleeper,  # type: ignore[arg-type]
        runner=runner or _Runner(),
        watchdog_capability=watchdog_capability,
    )
    adapter.bind_pre_insert_guard(
        pre_insert_guard if callable(pre_insert_guard) else lambda: None
    )
    proposal, _ = _proposal()
    adapter.bind_controller_principal(proposal.iap_connectivity.controller_principal)
    return adapter


@pytest.fixture
def live_google_capability(
    tmp_path: Path,
) -> Iterator[
    Callable[
        [],
        tuple[GcpPrivateCampaignCreateRequest, GcpPrivateCampaignWatchdogCapability],
    ]
]:
    """Issue genuine opaque capabilities for fake-SDK create tests.

    The production Google transport accepts only the exact watchdog capability
    type.  These tests use a real local worker and journal rather than a
    structurally compatible authority, while keeping all Compute clients fake.
    """

    watchdogs: list[GcpPrivateCampaignWatchdog] = []

    def issue() -> tuple[
        GcpPrivateCampaignCreateRequest, GcpPrivateCampaignWatchdogCapability
    ]:
        proposal, startup = _proposal()
        now = datetime.now(UTC).replace(microsecond=0)
        payload = proposal.model_dump(mode="python")
        payload.pop("proposal_id")
        payload["quote"] = proposal.quote.model_copy(
            update={
                "quoted_at": (now - timedelta(minutes=2))
                .isoformat()
                .replace("+00:00", "Z"),
                "expires_at": (now + timedelta(minutes=15))
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        fresh = issue_gcp_private_campaign_proposal(
            GcpPrivateCampaignProposalPayload.model_validate(payload)
        )
        approval = GcpPrivateCampaignApproval(
            schema_version=GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION,
            approval_kind="exact_human_campaign_approval",
            confirmation=GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION,
            human_approval_record_id="google-watchdog-test",
            approved_at=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            expires_at=(now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            proposal=fresh,
            proposal_digest=fresh.proposal_id,
        )
        cleanup = GcpPrivateCampaignCleanupAuthorization(
            schema_version=GCP_PRIVATE_CAMPAIGN_CLEANUP_AUTHORIZATION_SCHEMA_VERSION,
            authorization_kind="exact_cleanup_recovery",
            confirmation=GCP_PRIVATE_CAMPAIGN_CLEANUP_CONFIRMATION,
            cleanup_record_id="google-watchdog-cleanup-test",
            authorized_at=(now - timedelta(minutes=1))
            .isoformat()
            .replace("+00:00", "Z"),
            proposal=fresh,
            proposal_digest=fresh.proposal_id,
            cleanup_request_id=fresh.request_ids.delete_request_id,
        )
        suffix = str(len(watchdogs))
        journal_root = tmp_path / f"journal-{suffix}"
        watchdog_root = tmp_path / f"watchdog-{suffix}"
        journal_root.mkdir(mode=0o700)
        watchdog_root.mkdir(mode=0o700)
        journal = GcpPrivateCampaignJournal(journal_root)
        request = build_gcp_private_campaign_create_request(
            proposal=fresh, startup_payload=startup
        )
        journal.append(
            fresh,
            state="CREATE_INTENT_DURABLE",
            occurred_at=now,
            detail_digest=request.create_request_digest,
        )
        journal.append(fresh, state="CREATE_INTENT", occurred_at=now)
        watchdog = GcpPrivateCampaignWatchdog(
            watchdog_root=watchdog_root,
            journal_root=journal_root,
            worker_backend="google_cleanup_only_v1",
        )
        watchdog.activate(
            proposal=fresh,
            approval=approval,
            cleanup_authorization=cleanup,
            request=request,
            journal_identity=journal.identity(fresh),
            execution_deadline=journal.execution_deadline(fresh),
            now=now,
        )
        watchdogs.append(watchdog)
        capability = watchdog.take_create_capability()
        capability.consume_for_factory()
        return request, capability

    yield issue

    for watchdog in watchdogs:
        if watchdog._handle is not None:
            watchdog._kill_process(watchdog._handle.process)


def test_compute_clients_are_constructed_only_with_the_approved_identity(
    live_google_capability: Callable[
        [],
        tuple[GcpPrivateCampaignCreateRequest, GcpPrivateCampaignWatchdogCapability],
    ],
) -> None:
    request, watchdog_capability = live_google_capability()
    proposal = request.proposal
    constructors: list[tuple[str, object]] = []

    class _LazySdk:
        @staticmethod
        def InstancesClient(*, credentials: object) -> _InsertCounter:
            constructors.append(("instances", credentials))
            return _InsertCounter()

        @staticmethod
        def DisksClient(*, credentials: object) -> _DiskRows:
            constructors.append(("disks", credentials))
            return _DiskRows(_Rows())

        @staticmethod
        def ImagesClient(*, credentials: object) -> _NoImages:
            constructors.append(("images", credentials))
            return _NoImages()

        @staticmethod
        def FirewallsClient(*, credentials: object) -> _NoFirewalls:
            constructors.append(("firewalls", credentials))
            return _NoFirewalls()

    resolver = _CredentialResolver(
        effective_principal=proposal.iap_connectivity.controller_principal
    )
    adapter = GoogleGcpPrivateCampaignTransport(
        sdk=_LazySdk,
        credential_resolver=resolver,
        runner=_Runner(),
        iap_tunnels=_IapTunnels(),  # type: ignore[arg-type]
        watchdog_capability=watchdog_capability,
    )
    adapter.bind_pre_insert_guard(lambda: None)

    with pytest.raises(GcpPrivateCampaignError, match="CONTROLLER_PRINCIPAL_UNBOUND"):
        adapter.create_instance(
            request,
            request_id=proposal.request_ids.create_request_id,
            timeout_seconds=1,
        )
    assert resolver.calls == []
    assert constructors == []

    adapter.bind_controller_principal(proposal.iap_connectivity.controller_principal)

    assert resolver.calls == [proposal.iap_connectivity.controller_principal]
    assert [name for name, _ in constructors] == [
        "instances",
        "disks",
        "images",
        "firewalls",
    ]
    assert all(credential is resolver.credential for _, credential in constructors)


def test_create_requires_watchdog_authority_before_any_provider_preflight() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    adapter = GoogleGcpPrivateCampaignTransport(
        sdk=_Sdk,
        instances_client=_NoCalls(),
        disks_client=_DiskRows(_Rows()),
        images_client=_NoImages(),
        firewalls_client=_NoFirewalls(),
        iap_tunnels=_IapTunnels(),  # type: ignore[arg-type]
        runner=_Runner(),
    )
    adapter.bind_pre_insert_guard(lambda: None)
    adapter.bind_controller_principal(proposal.iap_connectivity.controller_principal)

    with pytest.raises(
        GcpPrivateCampaignError, match="WATCHDOG_CREATE_AUTHORITY_REQUIRED"
    ):
        adapter.create_instance(
            request,
            request_id=proposal.request_ids.create_request_id,
            timeout_seconds=1,
        )


def test_live_transport_rejects_structural_create_authority_before_client_use() -> None:
    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_CAPABILITY_REQUIRED"):
        GoogleGcpPrivateCampaignTransport(
            sdk=_Sdk,
            instances_client=_NoCalls(),
            disks_client=_DiskRows(_Rows()),
            images_client=_NoImages(),
            firewalls_client=_NoFirewalls(),
            runner=_Runner(),
            watchdog_capability=object(),  # type: ignore[arg-type]
        )


def test_live_resolver_surface_requires_exact_capability_before_client_use() -> None:
    resolver = _CredentialResolver(
        effective_principal="precampaign-controller@inferdrome-lab.iam.gserviceaccount.com"
    )
    with pytest.raises(
        GcpPrivateCampaignError, match="WATCHDOG_CREATE_AUTHORITY_REQUIRED"
    ):
        GoogleGcpPrivateCampaignTransport(
            sdk=object(),
            credential_resolver=resolver,
            runner=_Runner(),
            iap_tunnels=_IapTunnels(),  # type: ignore[arg-type]
        )
    assert resolver.calls == []


def test_capability_principal_mismatch_blocks_resolver_before_client_construction(
    live_google_capability: Callable[
        [],
        tuple[GcpPrivateCampaignCreateRequest, GcpPrivateCampaignWatchdogCapability],
    ],
) -> None:
    _, capability = live_google_capability()
    resolver = _CredentialResolver(
        effective_principal="precampaign-controller@inferdrome-lab.iam.gserviceaccount.com"
    )
    adapter = GoogleGcpPrivateCampaignTransport(
        sdk=object(),
        credential_resolver=resolver,
        runner=_Runner(),
        iap_tunnels=_IapTunnels(),  # type: ignore[arg-type]
        watchdog_capability=capability,
    )

    with pytest.raises(
        GcpPrivateCampaignError, match="WATCHDOG_CONTROLLER_PRINCIPAL_MISMATCH"
    ):
        adapter.bind_controller_principal(
            "wrong-controller@inferdrome-lab.iam.gserviceaccount.com"
        )
    assert resolver.calls == []


def test_controller_principal_mismatch_never_constructs_or_calls_compute_clients() -> (
    None
):
    proposal, _ = _proposal()
    resolver = _CredentialResolver(
        effective_principal="other-controller@inferdrome-lab.iam.gserviceaccount.com"
    )
    adapter = GoogleGcpPrivateCampaignTransport(
        sdk=object(),
        credential_resolver=resolver,
        runner=_Runner(),
        iap_tunnels=_IapTunnels(),  # type: ignore[arg-type]
        cleanup_only=True,
    )

    with pytest.raises(GcpPrivateCampaignError, match="CONTROLLER_PRINCIPAL_MISMATCH"):
        adapter.bind_controller_principal(
            proposal.iap_connectivity.controller_principal
        )

    assert resolver.calls == [proposal.iap_connectivity.controller_principal]


def test_projection_contains_exact_two_gpu_private_profile_and_semantic_startup() -> (
    None
):
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    adapter = GoogleGcpPrivateCampaignTransport(
        sdk=_Sdk,
        instances_client=_NoCalls(),
        disks_client=_NoCalls(),
        images_client=_NoImages(),
        runner=object(),  # type: ignore[arg-type]  # projection does not use runner
    )

    projected = adapter.project_create_request(
        request, request_id=proposal.request_ids.create_request_id
    )

    instance = projected.instance_resource
    assert instance.machine_type.endswith("/machineTypes/a2-highgpu-2g")
    assert len(instance.disks) == 1
    assert instance.disks[0].boot is True
    assert instance.disks[0].auto_delete is True
    assert instance.disks[0].initialize_params.disk_name == proposal.boot_disk_name
    assert len(instance.network_interfaces) == 1
    assert not hasattr(instance.network_interfaces[0], "access_configs")
    assert instance.scheduling.instance_termination_action == "DELETE"
    assert instance.scheduling.max_run_duration == {"seconds": "300"}
    assert instance.service_accounts[0].email == (
        proposal.guest_service_account.service_account_email
    )
    assert instance.service_accounts[0].scopes == []
    assert instance.tags.items == [proposal.iap_connectivity.instance_network_tag]
    metadata = {row.key: row.value for row in instance.metadata.items}
    assert metadata["inferdrome-startup-payload-digest"] == startup.startup_payload_id
    script = metadata["startup-script"]
    assert proposal.serving_image.reference in script
    assert proposal.runner_image.reference in script
    assert "--gpus device=0" in script
    assert "--gpus device=1" in script
    assert script.count("docker run") == 3
    assert "inferdrome-private-campaign" in script
    assert "inferdrome-runner-observer" in script
    assert metadata["block-project-ssh-keys"] == "TRUE"
    assert metadata["enable-oslogin"] == "FALSE"
    assert "docker pull" not in script
    assert "vllm serve" not in script
    assert "api_key" not in script
    assert "ssh" not in script


def test_engine_argv_overrides_default_entrypoint_and_starts_adapter_only() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    argv = render_gcp_private_campaign_engine_argv(request, startup.engines[0])

    assert argv[:5] == ("docker", "run", "--pull", "never", "--detach")
    assert argv.count("vllm") == 0
    entrypoint_index = argv.index("--entrypoint")
    assert argv[entrypoint_index + 1] == "/opt/inferdrome-runtime/bin/python"
    image_index = argv.index(proposal.serving_image.reference)
    assert argv[image_index + 1 : image_index + 4] == (
        "-m",
        "inferdrome.deployment.gcp_private_engine_adapter",
        "--endpoint-id",
    )
    assert "--disable-log-requests" not in argv


def test_runner_argv_is_cpu_only_and_has_no_engine_or_host_authority() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )

    argv = render_gcp_private_campaign_runner_argv(request)

    assert proposal.runner_image.reference in argv
    assert proposal.serving_image.reference not in argv
    assert "--gpus" not in argv
    assert "docker.sock" not in "\0".join(argv)
    assert startup.preloaded_artifacts.model_snapshot_path not in "\0".join(argv)
    assert argv[argv.index("--network") + 1] == "inferdrome-private-campaign"
    assert argv[argv.index("--user") + 1] == "2000:0"
    assert "/home/vllm:rw,nosuid,nodev,size=1g" in argv
    assert argv[argv.index("--entrypoint") + 1] == "/opt/inferdrome-runtime/bin/python"
    image_index = argv.index(proposal.runner_image.reference)
    assert argv[image_index + 1 : image_index + 4] == (
        "-m",
        "inferdrome.deployment.gcp_private_runner_adapter",
        "--container-name",
    )


def test_runner_image_uid_home_and_startup_tmpfs_contract_are_coherent() -> None:
    dockerfile = Path("Dockerfile.vllm-benchmark-runner").read_text(encoding="utf-8")

    assert "USER 2000:0" in dockerfile
    assert 'ENV HOME="/home/vllm"' in dockerfile
    assert (
        "install -d --owner=2000 --group=0 --mode=0700 /home/vllm /workspace"
        in dockerfile
    )
    assert "ARG INFERDROME_RUNTIME_ROLE" in dockerfile
    assert "private-engine|cpu-runner-observer" in dockerfile
    assert 'com.inferdrome.runtime-role="${INFERDROME_RUNTIME_ROLE}"' in dockerfile


def test_controller_handoffs_and_retrieves_only_through_the_bound_runner() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    runner = _RecordingRunner()
    adapter = _adapter(runner=runner)
    adapter._iap_endpoints = _IapTunnelEndpoints(
        readiness_origins=("http://127.0.0.1:18000", "http://127.0.0.1:18001"),
        runner_control_origin="http://127.0.0.1:18002",
    )
    runner.bind_iap_runner_origin(adapter._iap_endpoints.runner_control_origin)

    handoff = adapter.handoff_campaign(
        request,
        routing_config=b'{"config":"bounded"}',
        workload=b'{"prompt":"redacted"}\n',
        timeout_seconds=7,
    )
    evidence = adapter.retrieve_evidence(
        request, admitted_handoff=handoff, timeout_seconds=11
    )
    adapter.close()

    assert handoff.co_located_freshness_admitted is True
    assert evidence.collection_mode == "CREATE_NO_REPLACE_RETRIEVAL"
    assert runner.handoff_calls == [
        (b'{"config":"bounded"}', b'{"prompt":"redacted"}\n', 7)
    ]
    assert runner.retrieve_calls == [(handoff, 11)]
    assert runner.runner_origin == "http://127.0.0.1:18002"
    assert runner.closed is True


def test_a2_readback_requires_empty_guest_accelerators_and_two_scratch_disks() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    instance = _full_instance(proposal, request=request)
    adapter = _adapter(instances=_InsertCounter(instance))
    assert adapter._instance(request, timeout_seconds=1) is instance

    contradictory = _full_instance(proposal, request=request)
    contradictory.guest_accelerators = [_Record(accelerator_count=2)]
    with pytest.raises(GcpPrivateCampaignError, match="INSTANCE_TOPOLOGY_MISMATCH"):
        _adapter(instances=_InsertCounter(contradictory))._instance(
            request, timeout_seconds=1
        )

    missing_scratch = _full_instance(proposal, request=request)
    missing_scratch.disks = missing_scratch.disks[:2]
    with pytest.raises(GcpPrivateCampaignError, match="INSTANCE_SAFETY_MISMATCH"):
        _adapter(
            instances=_InsertCounter(missing_scratch)
        )._observe_exact_instance_value(request, missing_scratch, timeout_seconds=1)


def test_target_metric_accepts_unrelated_histogram_duplicates() -> None:
    _metric_is_present(
        b"\n".join(
            (
                b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 0',
                b'vllm:time_to_first_token_seconds_bucket{le="0.1"} 1',
                b'vllm:time_to_first_token_seconds_bucket{le="1"} 2',
            )
        )
        + b"\n"
    )
    with pytest.raises(GcpPrivateCampaignError, match="METRICS_TARGET_AMBIGUOUS"):
        _metric_is_present(
            b"\n".join(
                (
                    b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 0',
                    b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 1',
                )
            )
            + b"\n"
        )
    with pytest.raises(GcpPrivateCampaignError, match="METRICS_TARGET_MODEL_MISMATCH"):
        _metric_is_present(b'vllm:num_requests_running{model_name="Qwen/Qwen3-4B"} 0\n')


@pytest.mark.parametrize("value", (b"0.0", b"1e0", b"+2.000"))
def test_target_metric_accepts_prometheus_integral_decimal_forms(value: bytes) -> None:
    _metric_is_present(
        b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} ' + value + b"\n"
    )


@pytest.mark.parametrize("value", (b"-1", b"0.5", b"NaN", b"+Inf", b"-Inf"))
def test_target_metric_rejects_non_integral_or_nonfinite_values(value: bytes) -> None:
    with pytest.raises(GcpPrivateCampaignError, match="METRICS_TARGET_INVALID"):
        _metric_is_present(
            b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} ' + value + b"\n"
        )


def test_rendered_startup_script_is_deterministic_and_contains_no_prompt_surface() -> (
    None
):
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )

    script = render_gcp_private_campaign_startup_script(request)

    assert script == render_gcp_private_campaign_startup_script(request)
    assert "Qwen/Qwen3-8B" in script
    assert "/v1/chat/completions" not in script
    assert "prompt" not in script


def test_create_checks_exact_boot_image_identity_before_insert(
    live_google_capability: Callable[
        [],
        tuple[GcpPrivateCampaignCreateRequest, GcpPrivateCampaignWatchdogCapability],
    ],
) -> None:
    request, capability = live_google_capability()
    proposal = request.proposal
    instances = _InsertCounter()
    image = _ReadyImage(
        _Record(
            id=999,
            status="READY",
            self_link=(
                f"https://www.googleapis.com/compute/v1/{proposal.boot_image.image_ref}"
            ),
        )
    )
    adapter = _adapter(
        instances=instances, images=image, watchdog_capability=capability
    )

    with pytest.raises(GcpPrivateCampaignError, match="BOOT_IMAGE_IDENTITY_MISMATCH"):
        adapter.create_instance(
            request,
            request_id=proposal.request_ids.create_request_id,
            timeout_seconds=1,
        )

    assert image.calls == 1
    assert instances.insert_calls == 0


def test_create_revalidates_after_slow_boot_read_and_verifies_iap_firewall(
    live_google_capability: Callable[
        [],
        tuple[GcpPrivateCampaignCreateRequest, GcpPrivateCampaignWatchdogCapability],
    ],
) -> None:
    request, capability = live_google_capability()
    proposal = request.proposal
    image = _ReadyImage(
        _Record(
            id=proposal.boot_image.provider_image_id,
            status="READY",
            self_link=(
                f"https://www.googleapis.com/compute/v1/{proposal.boot_image.image_ref}"
            ),
        )
    )
    firewall = _ReadyFirewall(_live_firewall(proposal))
    instances = _InsertCounter()
    checks = 0

    def expires_after_image_read() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise GcpPrivateCampaignError("APPROVAL_EXPIRED")

    adapter = _adapter(
        instances=instances,
        images=image,
        firewalls=firewall,
        pre_insert_guard=expires_after_image_read,
        watchdog_capability=capability,
    )
    with pytest.raises(GcpPrivateCampaignError, match="APPROVAL_EXPIRED"):
        adapter.create_instance(
            request,
            request_id=proposal.request_ids.create_request_id,
            timeout_seconds=1,
        )
    assert image.calls == 1
    assert firewall.calls == 0
    assert instances.insert_calls == 0

    instances = _InsertCounter()
    request, capability = live_google_capability()
    proposal = request.proposal
    wrong_firewall = _live_firewall(proposal)
    wrong_firewall.source_ranges = ["10.0.0.0/8"]
    adapter = _adapter(
        instances=instances,
        images=_ReadyImage(image.row),
        firewalls=_ReadyFirewall(wrong_firewall),
        watchdog_capability=capability,
    )
    with pytest.raises(GcpPrivateCampaignError, match="IAP_FIREWALL_POLICY_MISMATCH"):
        adapter.create_instance(
            request,
            request_id=proposal.request_ids.create_request_id,
            timeout_seconds=1,
        )
    assert instances.insert_calls == 0


def test_create_rejects_known_wrong_iap_principal_before_insert(
    live_google_capability: Callable[
        [],
        tuple[GcpPrivateCampaignCreateRequest, GcpPrivateCampaignWatchdogCapability],
    ],
) -> None:
    request, capability = live_google_capability()
    proposal = request.proposal
    instances = _InsertCounter()
    iap = _IapTunnels()
    iap.principal_error = GcpPrivateCampaignError("IAP_PRINCIPAL_MISMATCH")
    adapter = _adapter(
        instances=instances,
        images=_ReadyImage(
            _Record(
                id=proposal.boot_image.provider_image_id,
                status="READY",
                self_link=(
                    "https://www.googleapis.com/compute/v1/"
                    f"{proposal.boot_image.image_ref}"
                ),
            )
        ),
        firewalls=_ReadyFirewall(_live_firewall(proposal)),
        iap_tunnels=iap,
        watchdog_capability=capability,
    )

    with pytest.raises(GcpPrivateCampaignError, match="IAP_PRINCIPAL_MISMATCH"):
        adapter.create_instance(
            request,
            request_id=proposal.request_ids.create_request_id,
            timeout_seconds=1,
        )

    assert iap.preflight_calls == 1
    assert instances.insert_calls == 0


@pytest.mark.parametrize(
    "field,value,code",
    (
        ("service_accounts", [], "INSTANCE_SERVICE_ACCOUNT_MISMATCH"),
        ("tags", _Record(items=["wrong-tag"]), "INSTANCE_IAP_TAG_MISMATCH"),
    ),
)
def test_instance_readback_rejects_guest_identity_and_iap_tag_drift(
    field: str, value: object, code: str
) -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    instance = _full_instance(proposal, request=request)
    setattr(instance, field, value)
    with pytest.raises(GcpPrivateCampaignError, match=code):
        _adapter(instances=_InsertCounter(instance))._instance(
            request, timeout_seconds=1
        )

    ssh_drift = _full_instance(proposal, request=request)
    ssh_drift.metadata.items[-1].value = "TRUE"
    with pytest.raises(GcpPrivateCampaignError, match="INSTANCE_STARTUP_BINDING"):
        _adapter(
            instances=_InsertCounter(ssh_drift),
            disks=_DiskRows(_Rows([_live_boot_disk(proposal)])),
        ).observe_exact_instance(request, timeout_seconds=1)


def test_exact_delete_rechecks_bound_provider_instance_identity() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    instances = _InsertCounter(
        _full_instance(proposal, provider_instance_id="987654321")
    )
    binding = GcpPrivateCampaignExactResourceBinding(
        proposal_id=proposal.proposal_id,
        project_id=proposal.topology.project_id,
        zone=proposal.topology.zone,
        instance_name=proposal.instance_name,
        ownership_labels=proposal.ownership_labels,
        provider_instance_id_sha256=sha256_digest(b"123456789"),
        boot_disk_provider_id_sha256=sha256_digest(b"246813579"),
    )

    with pytest.raises(GcpPrivateCampaignError, match="INSTANCE_IDENTITY_MISMATCH"):
        _adapter(instances=instances).delete_exact_instance(
            request,
            exact_resource_binding=binding,
            request_id=proposal.request_ids.delete_request_id,
            timeout_seconds=1,
        )

    assert instances.delete_calls == 0


def test_exact_delete_never_deletes_a_same_name_replacement_between_reads() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    first = _full_instance(proposal, provider_instance_id="123456789")
    replacement = _full_instance(proposal, provider_instance_id="987654321")

    class ReplacingInstances(_InsertCounter):
        def __init__(self) -> None:
            super().__init__(current=first)
            self.reads = 0

        def get(
            self, *, project: str, zone: str, instance: str, timeout: int
        ) -> object:
            del project, zone, instance, timeout
            self.reads += 1
            return first if self.reads == 1 else replacement

    instances = ReplacingInstances()
    disks = _DiskRows(_Rows([_live_boot_disk(proposal, attached=True)]))
    binding = GcpPrivateCampaignExactResourceBinding(
        proposal_id=proposal.proposal_id,
        project_id=proposal.topology.project_id,
        zone=proposal.topology.zone,
        instance_name=proposal.instance_name,
        ownership_labels=proposal.ownership_labels,
        provider_instance_id_sha256=sha256_digest(b"123456789"),
        boot_disk_provider_id_sha256=sha256_digest(b"246813579"),
    )

    with pytest.raises(GcpPrivateCampaignError, match="INSTANCE_IDENTITY_MISMATCH"):
        _adapter(instances=instances, disks=disks).delete_exact_instance(
            request,
            exact_resource_binding=binding,
            request_id=proposal.request_ids.delete_request_id,
            timeout_seconds=1,
        )

    assert instances.reads == 2
    assert instances.delete_calls == 0


def test_attached_boot_disk_is_bound_before_normal_instance_delete() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    instance = _full_instance(proposal, request=request)
    instances = _InsertCounter(current=instance, rows=_Rows([instance]))
    disks = _DiskRows(_Rows([_live_boot_disk(proposal, attached=True)]))
    adapter = _adapter(instances=instances, disks=disks)
    inventory = adapter.list_exact_owned_residuals(request, timeout_seconds=1)
    assert inventory.instance_state == "PRESENT"
    assert inventory.disks[0].attachment_state == "ATTACHED"
    assert inventory.disks[0].attached_provider_instance_id == "123456789"
    binding = GcpPrivateCampaignExactResourceBinding(
        proposal_id=proposal.proposal_id,
        project_id=proposal.topology.project_id,
        zone=proposal.topology.zone,
        instance_name=proposal.instance_name,
        ownership_labels=proposal.ownership_labels,
        provider_instance_id_sha256=sha256_digest(b"123456789"),
        boot_disk_provider_id_sha256=inventory.disks[0].provider_disk_id_sha256,
    )
    adapter.delete_exact_instance(
        request,
        exact_resource_binding=binding,
        request_id=proposal.request_ids.delete_request_id,
        timeout_seconds=1,
    )
    assert instances.delete_calls == 1


def test_attached_boot_disk_replacement_blocks_instance_auto_delete() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    instance = _full_instance(proposal, request=request)
    instances = _InsertCounter(current=instance, rows=_Rows([instance]))
    disks = _DiskRows(
        _Rows([_live_boot_disk(proposal, provider_disk_id="987654321", attached=True)])
    )
    binding = GcpPrivateCampaignExactResourceBinding(
        proposal_id=proposal.proposal_id,
        project_id=proposal.topology.project_id,
        zone=proposal.topology.zone,
        instance_name=proposal.instance_name,
        ownership_labels=proposal.ownership_labels,
        provider_instance_id_sha256=sha256_digest(b"123456789"),
        boot_disk_provider_id_sha256=sha256_digest(b"246813579"),
    )
    with pytest.raises(GcpPrivateCampaignError, match="BOOT_DISK_IDENTITY_MISMATCH"):
        _adapter(instances=instances, disks=disks).delete_exact_instance(
            request,
            exact_resource_binding=binding,
            request_id=proposal.request_ids.delete_request_id,
            timeout_seconds=1,
        )
    assert instances.delete_calls == 0


def test_final_absence_refuses_reappeared_exact_owned_instance() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    instances = _InsertCounter(rows=_Rows([_full_instance(proposal)]))

    with pytest.raises(GcpPrivateCampaignError, match="INSTANCE_RESIDUAL"):
        _adapter(instances=instances).confirm_exact_absence(request, timeout_seconds=1)


def test_final_absence_refuses_exact_resources_that_lost_ownership_labels() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    label_lost_instance = _full_instance(proposal, request=request)
    label_lost_instance.labels = {}
    with pytest.raises(GcpPrivateCampaignError, match="INSTANCE_OWNERSHIP_MISMATCH"):
        _adapter(
            instances=_InsertCounter(current=label_lost_instance, rows=_Rows()),
            disks=_DiskRows(_Rows()),
        ).confirm_exact_absence(request, timeout_seconds=1)

    label_lost_disk = _live_boot_disk(proposal, attached=False)
    label_lost_disk.labels = {}
    with pytest.raises(GcpPrivateCampaignError, match="BOOT_DISK_OWNERSHIP_MISMATCH"):
        _adapter(
            instances=_InsertCounter(rows=_Rows()),
            disks=_DiskRows(_Rows([label_lost_disk])),
        ).confirm_exact_absence(request, timeout_seconds=1)


def test_readback_rejects_network_and_disk_provenance_drift() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    wrong_network = _full_instance(proposal)
    wrong_network.network_interfaces[
        0
    ].subnetwork = "projects/inferdrome-lab/regions/us-central1/subnetworks/other"
    with pytest.raises(GcpPrivateCampaignError, match="INSTANCE_NETWORK_MISMATCH"):
        _adapter(instances=_InsertCounter(wrong_network))._instance(
            request, timeout_seconds=1
        )

    bad_disk = _Record(
        name=proposal.boot_disk_name,
        labels=proposal.ownership_labels.model_dump(mode="json"),
        self_link=(
            "https://www.googleapis.com/compute/v1/projects/"
            f"{proposal.topology.project_id}/zones/{proposal.topology.zone}/disks/"
            f"{proposal.boot_disk_name}"
        ),
        source_image=proposal.boot_image.image_ref,
        source_image_id=999,
        users=[],
    )
    with pytest.raises(GcpPrivateCampaignError, match="BOOT_DISK_PROVENANCE_MISMATCH"):
        _adapter(
            instances=_InsertCounter(rows=_Rows()), disks=_DiskRows(_Rows([bad_disk]))
        ).list_exact_owned_residuals(request, timeout_seconds=1)


def test_readback_rejects_tampered_or_extra_startup_metadata() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    instance = _full_instance(proposal, request=request)
    instance.metadata.items[0].value = "#!/bin/sh\nexit 0\n"
    with pytest.raises(GcpPrivateCampaignError, match="INSTANCE_STARTUP_BINDING"):
        _adapter(
            instances=_InsertCounter(instance),
            disks=_DiskRows(_Rows([_live_boot_disk(proposal)])),
        ).observe_exact_instance(request, timeout_seconds=1)

    extra = _full_instance(proposal, request=request)
    extra.metadata.items.append(_Record(key="startup-script-url", value="ignored"))
    with pytest.raises(GcpPrivateCampaignError, match="INSTANCE_STARTUP_BINDING"):
        _adapter(
            instances=_InsertCounter(extra),
            disks=_DiskRows(_Rows([_live_boot_disk(proposal)])),
        ).observe_exact_instance(request, timeout_seconds=1)


def test_boot_disk_delete_rechecks_durable_disk_identity() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    disks = _DiskRows(_Rows([_live_boot_disk(proposal, attached=False)]))
    adapter = _adapter(disks=disks)
    observed = adapter.list_exact_owned_residuals(request, timeout_seconds=1).disks[0]
    binding = GcpPrivateCampaignExactResourceBinding(
        proposal_id=proposal.proposal_id,
        project_id=proposal.topology.project_id,
        zone=proposal.topology.zone,
        instance_name=proposal.instance_name,
        ownership_labels=proposal.ownership_labels,
        provider_instance_id_sha256=sha256_digest(b"123456789"),
        boot_disk_provider_id_sha256=observed.provider_disk_id_sha256,
    )
    disks.rows = _Rows(
        [_live_boot_disk(proposal, provider_disk_id="987654321", attached=False)]
    )

    with pytest.raises(GcpPrivateCampaignError, match="BOOT_DISK_DELETE_SCOPE"):
        adapter.delete_exact_owned_boot_disk(
            request,
            observed,
            exact_resource_binding=binding,
            request_id=proposal.request_ids.boot_disk_delete_request_id,
            timeout_seconds=1,
        )

    assert disks.delete_calls == 0


def test_readiness_requires_real_generation_shape_and_fresh_local_monotonic_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    calls: list[tuple[str, str]] = []

    def attestation_body(origin: str) -> bytes:
        engine = startup.engines[0 if origin.endswith(":18000") else 1]
        return json.dumps(
            {
                "schema_version": "inferdrome.gcp-private-engine-attestation.v2",
                "endpoint_id": engine.endpoint_id,
                "container_name": engine.container_name,
                "gpu_ordinal": engine.gpu_ordinal,
                "private_port": engine.private_port,
                "serving_image": engine.serving_image.model_dump(mode="json"),
                "model": engine.model.model_dump(mode="json"),
                "runtime": engine.runtime.model_dump(mode="json"),
                "startup_payload_digest": startup.startup_payload_id,
                "adapter_source_sha256": engine.adapter_source_sha256,
                "model_snapshot_sha256": (
                    startup.preloaded_artifacts.model_snapshot_sha256
                ),
                "listener_scope": engine.listener_scope,
            }
        ).encode("utf-8")

    def fake_http(
        origin: str,
        path: str,
        *,
        method: str,
        timeout_seconds: int,
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        del timeout_seconds
        calls.append((method, path))
        if path == "/health":
            return 200, b""
        if path == "/v1/models":
            return 200, b'{"data":[{"id":"Qwen/Qwen3-8B"}]}'
        if path == "/v1/chat/completions":
            assert body is not None
            return 200, (
                b'{"choices":[{"finish_reason":"stop","index":0,'
                b'"message":{"content":"ok","role":"assistant"}}]}'
            )
        if path == "/metrics":
            return 200, b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 0\n'
        if path == "/inferdrome/v2/engine-attestation":
            return 200, attestation_body(origin)
        raise AssertionError(path)

    monkeypatch.setattr(
        GoogleGcpPrivateCampaignTransport, "_http", staticmethod(fake_http)
    )
    readiness = _adapter(
        instances=_InsertCounter(_full_instance(proposal, request=request)),
        disks=_DiskRows(_Rows([_live_boot_disk(proposal)])),
    ).observe_readiness(request, timeout_seconds=1)

    assert calls.count(("POST", "/v1/chat/completions")) == 2
    assert all(endpoint.observed_monotonic_ns > 0 for endpoint in readiness.endpoints)

    def empty_choice_http(
        origin: str,
        path: str,
        *,
        method: str,
        timeout_seconds: int,
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        del origin, method, timeout_seconds, body
        if path == "/health":
            return 200, b""
        if path == "/v1/models":
            return 200, b'{"data":[{"id":"Qwen/Qwen3-8B"}]}'
        if path == "/v1/chat/completions":
            return 200, b'{"choices":[]}'
        return 200, b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 0\n'

    monkeypatch.setattr(
        GoogleGcpPrivateCampaignTransport, "_http", staticmethod(empty_choice_http)
    )
    with pytest.raises(GcpPrivateCampaignError, match="PRIVATE_GENERATION_INVALID"):
        _adapter(
            instances=_InsertCounter(_full_instance(proposal, request=request)),
            disks=_DiskRows(_Rows([_live_boot_disk(proposal)])),
        ).observe_readiness(request, timeout_seconds=1)


def test_readiness_rechecks_running_state_before_any_private_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    stopped = _full_instance(proposal, request=request)
    stopped.status = "TERMINATED"
    probe_calls = 0

    def unexpected_http(*args: object, **kwargs: object) -> tuple[int, bytes]:
        nonlocal probe_calls
        del args, kwargs
        probe_calls += 1
        raise AssertionError("readiness must not probe a stopped instance")

    monkeypatch.setattr(
        GoogleGcpPrivateCampaignTransport, "_http", staticmethod(unexpected_http)
    )
    with pytest.raises(GcpPrivateCampaignError, match="PRIVATE_READINESS_TIMEOUT"):
        _adapter(
            instances=_InsertCounter(stopped),
            disks=_DiskRows(_Rows([_live_boot_disk(proposal)])),
        ).observe_readiness(request, timeout_seconds=1)
    assert probe_calls == 0


def test_readiness_polls_iap_only_until_delayed_engines_become_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    now = 0.0
    attempts = 0
    origins: list[str] = []

    def monotonic() -> float:
        return now

    def sleep(delay: float) -> None:
        nonlocal now
        now += delay

    def fake_http(
        origin: str,
        path: str,
        *,
        method: str,
        timeout_seconds: int,
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        nonlocal attempts
        del method, timeout_seconds, body
        origins.append(origin)
        if path == "/health" and origin.endswith(":18000"):
            attempts += 1
            if attempts == 1:
                return 503, b""
        if path == "/health":
            return 200, b""
        if path == "/v1/models":
            return 200, b'{"data":[{"id":"Qwen/Qwen3-8B"}]}'
        if path == "/v1/chat/completions":
            return 200, (
                b'{"choices":[{"finish_reason":"stop","index":0,'
                b'"message":{"content":"ok","role":"assistant"}}]}'
            )
        if path == "/metrics":
            return 200, b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 0\n'
        if path == "/inferdrome/v2/engine-attestation":
            engine = startup.engines[0 if origin.endswith(":18000") else 1]
            return 200, json.dumps(
                {
                    "schema_version": "inferdrome.gcp-private-engine-attestation.v2",
                    "endpoint_id": engine.endpoint_id,
                    "container_name": engine.container_name,
                    "gpu_ordinal": engine.gpu_ordinal,
                    "private_port": engine.private_port,
                    "serving_image": engine.serving_image.model_dump(mode="json"),
                    "model": engine.model.model_dump(mode="json"),
                    "runtime": engine.runtime.model_dump(mode="json"),
                    "startup_payload_digest": startup.startup_payload_id,
                    "adapter_source_sha256": engine.adapter_source_sha256,
                    "model_snapshot_sha256": (
                        startup.preloaded_artifacts.model_snapshot_sha256
                    ),
                    "listener_scope": engine.listener_scope,
                }
            ).encode("utf-8")
        raise AssertionError(path)

    monkeypatch.setattr(
        GoogleGcpPrivateCampaignTransport, "_http", staticmethod(fake_http)
    )
    readiness = _adapter(
        instances=_InsertCounter(_full_instance(proposal, request=request)),
        disks=_DiskRows(_Rows([_live_boot_disk(proposal)])),
        monotonic=monotonic,
        sleeper=sleep,
    ).observe_readiness(request, timeout_seconds=1)

    assert attempts == 2
    assert readiness.endpoints[0].endpoint_id == "endpoint-a"
    assert origins and all(origin.startswith("http://127.0.0.1:") for origin in origins)


def test_iap_path_loss_fails_before_any_endpoint_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    probes = 0

    class LostIap:
        def open(self, request: object, *, timeout_seconds: int) -> _IapTunnelEndpoints:
            del request, timeout_seconds
            raise GcpPrivateCampaignError("IAP_TUNNEL_UNAVAILABLE")

        def close(self) -> None:
            return None

    def unexpected_http(*args: object, **kwargs: object) -> tuple[int, bytes]:
        nonlocal probes
        del args, kwargs
        probes += 1
        raise AssertionError("endpoint probe must not follow IAP path loss")

    monkeypatch.setattr(
        GoogleGcpPrivateCampaignTransport, "_http", staticmethod(unexpected_http)
    )
    with pytest.raises(GcpPrivateCampaignError, match="IAP_TUNNEL_UNAVAILABLE"):
        _adapter(
            instances=_InsertCounter(_full_instance(proposal, request=request)),
            disks=_DiskRows(_Rows([_live_boot_disk(proposal)])),
            iap_tunnels=LostIap(),  # type: ignore[arg-type]
        ).observe_readiness(request, timeout_seconds=1)
    assert probes == 0


def test_readiness_rejects_repeated_gpu_or_wrong_container_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )

    def fake_http(
        origin: str,
        path: str,
        *,
        method: str,
        timeout_seconds: int,
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        del method, timeout_seconds, body
        if path == "/health":
            return 200, b""
        if path == "/v1/models":
            return 200, b'{"data":[{"id":"Qwen/Qwen3-8B"}]}'
        if path == "/v1/chat/completions":
            return 200, (
                b'{"choices":[{"finish_reason":"stop","index":0,'
                b'"message":{"content":"ok","role":"assistant"}}]}'
            )
        if path == "/metrics":
            return 200, b'vllm:num_requests_running{model_name="Qwen/Qwen3-8B"} 0\n'
        if path == "/inferdrome/v2/engine-attestation":
            engine = startup.engines[0 if origin.endswith(":18000") else 1]
            return 200, json.dumps(
                {
                    "schema_version": "inferdrome.gcp-private-engine-attestation.v2",
                    "endpoint_id": engine.endpoint_id,
                    "container_name": engine.container_name,
                    # Both endpoint responses assert GPU 0; endpoint B must
                    # fail rather than let two ports claim one engine/GPU.
                    "gpu_ordinal": 0,
                    "private_port": engine.private_port,
                    "serving_image": engine.serving_image.model_dump(mode="json"),
                    "model": engine.model.model_dump(mode="json"),
                    "runtime": engine.runtime.model_dump(mode="json"),
                    "startup_payload_digest": startup.startup_payload_id,
                    "adapter_source_sha256": engine.adapter_source_sha256,
                    "model_snapshot_sha256": (
                        startup.preloaded_artifacts.model_snapshot_sha256
                    ),
                    "listener_scope": engine.listener_scope,
                }
            ).encode("utf-8")
        raise AssertionError(path)

    monkeypatch.setattr(
        GoogleGcpPrivateCampaignTransport, "_http", staticmethod(fake_http)
    )
    with pytest.raises(GcpPrivateCampaignError, match="PRIVATE_ENGINE_ATTESTATION"):
        _adapter(
            instances=_InsertCounter(_full_instance(proposal, request=request)),
            disks=_DiskRows(_Rows([_live_boot_disk(proposal)])),
        ).observe_readiness(request, timeout_seconds=1)


def test_direct_private_http_refuses_redirects_without_proxy_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: tuple[object, ...] = ()

    class RedirectingOpener:
        def open(self, request: object, *, timeout: int) -> object:
            del timeout
            raise HTTPError(request, 302, "redirect", {}, None)

    def fake_build_opener(*provided: object) -> RedirectingOpener:
        nonlocal handlers
        handlers = provided
        return RedirectingOpener()

    monkeypatch.setattr(campaign_google, "build_opener", fake_build_opener)
    status, content = GoogleGcpPrivateCampaignTransport._http(
        "http://10.23.0.17:8000", "/health", method="GET", timeout_seconds=1
    )

    assert (status, content) == (302, b"")
    assert any(
        handler.__class__.__name__ == "ProxyHandler"
        and getattr(handler, "proxies", None) == {}
        for handler in handlers
    )
    assert (
        campaign_google._NoRedirect().redirect_request(
            object(), object(), 302, "redirect", object(), "http://example.test"
        )
        is None
    )
