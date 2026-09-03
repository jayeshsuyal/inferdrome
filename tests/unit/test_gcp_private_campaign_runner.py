"""Local fake-IAP tests for the CPU-only co-located campaign runner."""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from inferdrome.deployment.gcp_private_campaign_google import (
    IapGcpPrivateCampaignRunner,
    _RunnerIapResponse,
)
from inferdrome.deployment.gcp_private_campaign_v2 import (
    GCP_PRIVATE_CAMPAIGN_RUNNER_ATTESTATION_SCHEMA_VERSION,
    GcpPrivateCampaignHandoffReceipt,
    GcpPrivateCampaignRunnerAttestation,
    GcpPrivateCampaignTransportError,
    build_gcp_private_campaign_create_request,
)
from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.routing_execution.canonical import sha256_digest
from inferdrome.routing_execution.executor import ManualMonotonicClock, run_execution
from inferdrome.routing_execution.stdin_bundle import RuntimeInputBundle
from inferdrome.routing_execution.verifier import verify_execution_package
from tests.routing_execution_support import StaticEndpointTransport, write_inputs
from tests.unit.test_gcp_private_campaign_v2 import _proposal


@dataclass
class _Call:
    origin: str
    path: str
    method: str
    body: bytes | None
    timeout_seconds: int
    maximum_body_bytes: int


class _Http:
    def __init__(self, responses: list[_RunnerIapResponse]) -> None:
        self.responses = responses
        self.calls: list[_Call] = []

    def request(
        self,
        origin: str,
        path: str,
        *,
        method: str,
        body: bytes | None,
        timeout_seconds: int,
        maximum_body_bytes: int,
    ) -> _RunnerIapResponse:
        self.calls.append(
            _Call(origin, path, method, body, timeout_seconds, maximum_body_bytes)
        )
        return self.responses.pop(0)


def _request():
    proposal, startup = _proposal()
    return build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )


def _attestation(request) -> GcpPrivateCampaignRunnerAttestation:
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
        gpu_access="NONE",
        cloud_credentials="NONE",
        docker_socket="ABSENT",
        serving_role="OBSERVER_ONLY",
        provider_mutation_authority="NONE",
    )


def _handoff(request) -> GcpPrivateCampaignHandoffReceipt:
    return GcpPrivateCampaignHandoffReceipt(
        proposal_id=request.proposal.proposal_id,
        routing_config_sha256="sha256:" + ("a" * 64),
        selected_workload_sha256=request.proposal.routing.selected_workload_sha256,
        endpoint_a_origin_sha256="sha256:" + ("b" * 64),
        endpoint_b_origin_sha256="sha256:" + ("c" * 64),
        routed_after_verified_readiness=True,
        co_located_freshness_admitted=True,
        runner_command_sha256=request.startup_payload.runner.runner_command_sha256,
    )


def _json_response(value: object) -> _RunnerIapResponse:
    return _RunnerIapResponse(
        status=200,
        content=canonical_json_bytes(value.model_dump(mode="json")),
        headers={"content-type": "application/json"},
    )


def test_runner_binds_only_the_loopback_iap_control_tunnel() -> None:
    runner = IapGcpPrivateCampaignRunner(Path("/tmp/evidence"), http=_Http([]))

    with pytest.raises(
        GcpPrivateCampaignTransportError, match="IAP_TUNNEL_BINDING_MISMATCH"
    ):
        runner.bind_iap_runner_origin("http://host.docker.internal:18002")

    runner.bind_iap_runner_origin("http://127.0.0.1:18002")
    runner.bind_iap_runner_origin("http://127.0.0.1:18002")
    with pytest.raises(
        GcpPrivateCampaignTransportError, match="IAP_TUNNEL_BINDING_MISMATCH"
    ):
        runner.bind_iap_runner_origin("http://127.0.0.1:18003")


def test_runner_attestation_and_handoff_are_iap_only_and_transport_map_free() -> None:
    request = _request()
    routing_config = b'{"schema_version":"routing-execution-config.v1"}'
    workload = b'{"prompt":"redacted"}\n'
    receipt = GcpPrivateCampaignHandoffReceipt(
        proposal_id=request.proposal.proposal_id,
        routing_config_sha256=sha256_digest(routing_config),
        selected_workload_sha256=request.proposal.routing.selected_workload_sha256,
        endpoint_a_origin_sha256="sha256:" + ("a" * 64),
        endpoint_b_origin_sha256="sha256:" + ("b" * 64),
        routed_after_verified_readiness=True,
        co_located_freshness_admitted=True,
        runner_command_sha256=request.startup_payload.runner.runner_command_sha256,
    )
    http = _Http([_json_response(_attestation(request)), _json_response(receipt)])
    runner = IapGcpPrivateCampaignRunner(Path("/tmp/evidence"), http=http)
    runner.bind_iap_runner_origin("http://127.0.0.1:18002")

    assert runner.readiness_attestation(request, timeout_seconds=5) == _attestation(
        request
    )
    assert (
        runner.handoff(
            request,
            routing_config=routing_config,
            workload=workload,
            timeout_seconds=5,
        )
        == receipt
    )

    assert [(call.origin, call.path, call.method) for call in http.calls] == [
        ("http://127.0.0.1:18002", "/inferdrome/v2/runner/attestation", "GET"),
        ("http://127.0.0.1:18002", "/inferdrome/v2/runner/handoff", "POST"),
    ]
    bundle = RuntimeInputBundle.decode(http.calls[1].body or b"")
    assert bundle.config_bytes == routing_config
    assert bundle.workload_bytes == workload
    assert bundle.iap_transport_map_bytes is None


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            _RunnerIapResponse(status=302, content=b"", headers={}),
            "EVIDENCE_RETRIEVAL_FAILED",
        ),
        (
            _RunnerIapResponse(
                status=200,
                content=b"x" * (33_554_433),
                headers={
                    "content-type": "application/x-tar",
                    "x-inferdrome-retained-digest": "sha256:" + ("1" * 64),
                },
            ),
            "RUNNER_RETRIEVAL_TOO_LARGE",
        ),
        (
            _RunnerIapResponse(
                status=200,
                content=b"not-a-tar",
                headers={
                    "content-type": "application/x-tar",
                    "x-inferdrome-retained-digest": "sha256:" + ("1" * 64),
                },
            ),
            "RUNNER_RETRIEVAL_INVALID",
        ),
    ],
)
def test_runner_retrieval_rejects_redirect_oversize_and_tamper(
    response: _RunnerIapResponse, code: str
) -> None:
    request = _request()
    runner = IapGcpPrivateCampaignRunner(Path("/tmp/evidence"), http=_Http([response]))
    runner.bind_iap_runner_origin("http://127.0.0.1:18002")

    with pytest.raises(GcpPrivateCampaignTransportError, match=code):
        runner.retrieve(request, admitted_handoff=_handoff(request), timeout_seconds=5)


def test_runner_retrieval_rejects_a_valid_package_from_a_different_handoff(
    tmp_path: Path,
) -> None:
    request = _request()
    package_root = tmp_path / "source"
    package_root.mkdir(mode=0o700)
    config_path, workload_path, _, _ = write_inputs(package_root)
    sealed = run_execution(
        config_path,
        workload_path,
        package_root / "package",
        transport_factory=StaticEndpointTransport,
        clock=ManualMonotonicClock(),
    )
    verified = verify_execution_package(sealed.path)
    archive = io.BytesIO()
    names = (
        "executed-manifest.json",
        "input-transfer-receipt.json",
        "integrity-manifest.json",
        "producer-receipt.json",
    )
    with tarfile.open(fileobj=archive, mode="w:") as bundle:
        for name in names:
            payload = (sealed.path / name).read_bytes()
            member = tarfile.TarInfo(name)
            member.mode = 0o400
            member.uid = 0
            member.gid = 0
            member.mtime = 0
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))

    origins = {
        endpoint.endpoint_id: endpoint.origin_sha256
        for endpoint in verified.executed_manifest.endpoints
    }
    admitted_handoff = GcpPrivateCampaignHandoffReceipt(
        proposal_id=request.proposal.proposal_id,
        routing_config_sha256=verified.executed_manifest.config_sha256,
        selected_workload_sha256=(
            verified.executed_manifest.workload.selected_workload_sha256
        ),
        endpoint_a_origin_sha256="sha256:" + ("f" * 64),
        endpoint_b_origin_sha256=origins["endpoint-b"],
        routed_after_verified_readiness=True,
        co_located_freshness_admitted=True,
        runner_command_sha256=request.startup_payload.runner.runner_command_sha256,
    )
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    runner = IapGcpPrivateCampaignRunner(evidence_root, http=_Http([]))
    root = SafeDirFD.open(evidence_root)
    try:
        runner.bind_evidence_root(root)
    finally:
        root.close()

    with pytest.raises(
        GcpPrivateCampaignTransportError, match="EVIDENCE_HANDOFF_BINDING_MISMATCH"
    ):
        runner._import_archive(
            request,
            archive=archive.getvalue(),
            retained_digest=verified.report.retained_digest,
            admitted_handoff=admitted_handoff,
        )

    assert not list(evidence_root.iterdir())
