"""Integration coverage for the pure pre-campaign → PR-B routing bridge."""

from __future__ import annotations

from pathlib import Path

import pytest

from inferdrome.deployment.gcp_private_campaign_v2 import (
    FakeGcpPrivateCampaignTransport,
    GcpPrivateCampaignError,
    build_gcp_private_campaign_create_request,
)
from inferdrome.deployment.gcp_private_routing_handoff import (
    build_gcp_private_routing_execution_inputs,
)
from inferdrome.routing_execution.executor import ManualMonotonicClock, run_execution
from inferdrome.routing_execution.verifier import verify_execution_package
from tests.routing_execution_support import StaticEndpointTransport
from tests.unit.test_gcp_private_campaign_v2 import _proposal


def test_verified_private_readiness_materializes_and_seals_pr_b_execution(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    readiness = FakeGcpPrivateCampaignTransport().observe_readiness(
        request, timeout_seconds=1
    )

    handoff = build_gcp_private_routing_execution_inputs(proposal, readiness)
    config_path = tmp_path / "deployment-config.json"
    workload_path = tmp_path / "workload.jsonl"
    config_path.write_bytes(handoff.routing_config_bytes)
    workload_path.write_bytes(handoff.workload_bytes)
    transport = StaticEndpointTransport()
    sealed = run_execution(
        config_path,
        workload_path,
        tmp_path / "package",
        transport_factory=lambda: transport,
        clock=ManualMonotonicClock(),
    )

    verified = verify_execution_package(sealed.path)
    assert verified.executed_manifest.mode == "GCP_PRIVATE"
    assert verified.executed_manifest.topology.accelerator_count == 2
    assert transport.closed
    assert len(verified.producer_receipt.route_decisions) == 18
    assert len(verified.producer_receipt.terminal_outcomes) == 18
    assert all(
        b"10.23.0.17" not in child.read_bytes()
        for child in sealed.path.iterdir()
        if child.is_file()
    )


def test_public_or_unverified_readiness_never_materializes_a_routing_config() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    readiness = (
        FakeGcpPrivateCampaignTransport()
        .observe_readiness(request, timeout_seconds=1)
        .model_copy(update={"private_ipv4": "8.8.8.8"})
    )

    with pytest.raises(GcpPrivateCampaignError, match="ROUTING_HANDOFF_ADMISSION"):
        build_gcp_private_routing_execution_inputs(proposal, readiness)
