"""Fake-SDK tests for the lazy two-engine GCE projection; no provider access."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

import inferdrome.deployment.gcp_private_campaign_google as campaign_google
from inferdrome.deployment.gcp_private_campaign_google import (
    GoogleGcpPrivateCampaignTransport,
    LocalGcpPrivateCampaignRunner,
    _metric_is_present,
    render_gcp_private_campaign_startup_script,
)
from inferdrome.deployment.gcp_private_campaign_v2 import (
    GcpPrivateCampaignError,
    GcpPrivateCampaignExactResourceBinding,
    GcpPrivateCampaignProposalPayload,
    build_gcp_private_campaign_create_request,
    issue_gcp_private_campaign_proposal,
    verify_gcp_private_campaign_evidence_destination,
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


def _adapter(
    *,
    instances: _InsertCounter | None = None,
    disks: _DiskRows | None = None,
    images: object | None = None,
    evidence_root: Path | None = None,
) -> GoogleGcpPrivateCampaignTransport:
    return GoogleGcpPrivateCampaignTransport(
        sdk=_Sdk,
        instances_client=instances or _InsertCounter(),
        disks_client=disks or _DiskRows(_Rows()),
        images_client=images or _NoImages(),  # type: ignore[arg-type]
        runner=LocalGcpPrivateCampaignRunner(evidence_root or Path("/private/tmp")),
    )


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
    metadata = {row.key: row.value for row in instance.metadata.items}
    assert metadata["inferdrome-startup-payload-digest"] == startup.startup_payload_id
    script = metadata["startup-script"]
    assert proposal.serving_image.reference in script
    assert "--gpus device=0" in script
    assert "--gpus device=1" in script
    assert "--disable-log-requests" in script
    assert "api_key" not in script
    assert "ssh" not in script


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


def test_create_checks_exact_boot_image_identity_before_insert() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
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
    adapter = _adapter(instances=instances, images=image)

    with pytest.raises(GcpPrivateCampaignError, match="BOOT_IMAGE_IDENTITY_MISMATCH"):
        adapter.create_instance(
            request,
            request_id=proposal.request_ids.create_request_id,
            timeout_seconds=1,
        )

    assert image.calls == 1
    assert instances.insert_calls == 0


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
        engine = startup.engines[0 if origin.endswith(":8000") else 1]
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
    with pytest.raises(GcpPrivateCampaignError, match="PRIVATE_READINESS_UNAVAILABLE"):
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
    with pytest.raises(
        GcpPrivateCampaignError, match="PRIVATE_READINESS_INSTANCE_NOT_RUNNING"
    ):
        _adapter(
            instances=_InsertCounter(stopped),
            disks=_DiskRows(_Rows([_live_boot_disk(proposal)])),
        ).observe_readiness(request, timeout_seconds=1)
    assert probe_calls == 0


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
            engine = startup.engines[0 if origin.endswith(":8000") else 1]
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


def test_runner_refuses_evidence_root_replacement_without_staging_prompt_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    proposal, startup = _proposal()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    payload = proposal.model_dump(mode="python")
    payload.pop("proposal_id")
    payload["routing"] = proposal.routing.model_copy(
        update={
            "evidence_destination_sha256": sha256_digest(
                str(evidence_root.absolute()).encode("utf-8")
            )
        }
    )
    exact_proposal = issue_gcp_private_campaign_proposal(
        GcpPrivateCampaignProposalPayload(**payload)
    )
    request = build_gcp_private_campaign_create_request(
        proposal=exact_proposal, startup_payload=startup
    )
    displaced = tmp_path / "displaced"

    def replace_root(*args: object, **kwargs: object) -> object:
        del args, kwargs
        evidence_root.rename(displaced)
        evidence_root.mkdir(mode=0o700)
        return object()

    monkeypatch.setattr(campaign_google, "run_execution", replace_root)
    runner = LocalGcpPrivateCampaignRunner(evidence_root)
    held_root = verify_gcp_private_campaign_evidence_destination(
        exact_proposal, evidence_root=evidence_root
    )
    try:
        runner.bind_evidence_root(held_root)
        with pytest.raises(GcpPrivateCampaignError, match="EVIDENCE_ROOT_CHANGED"):
            runner.handoff(
                request,
                routing_config=b"{}",
                workload=b"{}\n",
                timeout_seconds=1,
            )
    finally:
        held_root.close()

    assert not list(displaced.rglob("deployment-config.json"))
    assert not list(displaced.rglob("workload.jsonl"))


def test_runner_refuses_root_replacement_after_preflight_before_handoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    proposal, startup = _proposal()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    payload = proposal.model_dump(mode="python")
    payload.pop("proposal_id")
    payload["routing"] = proposal.routing.model_copy(
        update={
            "evidence_destination_sha256": sha256_digest(
                str(evidence_root.absolute()).encode("utf-8")
            )
        }
    )
    exact_proposal = issue_gcp_private_campaign_proposal(
        GcpPrivateCampaignProposalPayload(**payload)
    )
    request = build_gcp_private_campaign_create_request(
        proposal=exact_proposal, startup_payload=startup
    )
    held_root = verify_gcp_private_campaign_evidence_destination(
        exact_proposal, evidence_root=evidence_root
    )
    runner = LocalGcpPrivateCampaignRunner(evidence_root)
    displaced = tmp_path / "displaced"
    calls = 0

    def unexpected_run_execution(*args: object, **kwargs: object) -> object:
        nonlocal calls
        del args, kwargs
        calls += 1
        raise AssertionError("replacement must fail before routing execution")

    monkeypatch.setattr(campaign_google, "run_execution", unexpected_run_execution)
    try:
        runner.bind_evidence_root(held_root)
        evidence_root.rename(displaced)
        evidence_root.mkdir(mode=0o700)
        with pytest.raises(GcpPrivateCampaignError, match="EVIDENCE_ROOT_CHANGED"):
            runner.handoff(
                request,
                routing_config=b"{}",
                workload=b"{}\n",
                timeout_seconds=1,
            )
    finally:
        held_root.close()

    assert calls == 0
    assert not list(evidence_root.iterdir())
