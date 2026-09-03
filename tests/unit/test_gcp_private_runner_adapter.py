"""CPU-only runner adapter tests; no Docker, GCP, or network execution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from inferdrome.deployment import gcp_private_runner_adapter as adapter
from inferdrome.deployment.gcp_private_campaign_v2 import (
    FakeGcpPrivateCampaignTransport,
    build_gcp_private_campaign_create_request,
)
from inferdrome.deployment.gcp_private_routing_handoff import (
    build_gcp_private_routing_execution_inputs,
)
from inferdrome.routing_execution.contracts import RoutingExecutionConfig
from inferdrome.routing_execution.transport import TransportResponse
from tests.unit.test_gcp_private_campaign_v2 import _proposal


def _config() -> RoutingExecutionConfig:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    readiness = FakeGcpPrivateCampaignTransport().observe_readiness(
        request, timeout_seconds=1
    )
    return RoutingExecutionConfig.model_validate_json(
        build_gcp_private_routing_execution_inputs(
            proposal, readiness
        ).routing_config_bytes
    )


def _arguments() -> adapter.RunnerAdapterArguments:
    proposal, startup = _proposal()
    runner = startup.runner
    return adapter.RunnerAdapterArguments(
        container_name=runner.container_name,
        private_port=runner.private_port,
        runner_image_reference=proposal.runner_image.reference,
        runner_command_sha256=runner.runner_command_sha256,
        adapter_source_sha256=runner.adapter_source_sha256,
        startup_payload_digest=startup.startup_payload_id,
        proposal_id=proposal.proposal_id,
        evidence_root=Path("/evidence"),
        endpoint_a_local_origin=runner.endpoint_a_local_origin,
        endpoint_b_local_origin=runner.endpoint_b_local_origin,
    )


class _FreshTransport:
    def __init__(self, **_: object) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def get(self, origin: str, path: str, *, timeout_ms: int) -> TransportResponse:
        assert timeout_ms == 5
        self.calls.append((origin, path))
        return TransportResponse(status=200, body=b"ok")

    def close(self) -> None:
        self.closed = True


def test_pre_campaign_freshness_admits_only_observed_five_ms_local_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_FreshTransport] = []

    def transport_factory(**kwargs: object) -> _FreshTransport:
        transport = _FreshTransport(**kwargs)
        created.append(transport)
        return transport

    ticks = iter((100, 200, 300, 400, 5_000_100))
    monkeypatch.setattr(adapter, "_CoLocatedTransport", transport_factory)

    adapter._pre_campaign_freshness_admission(
        _config(), arguments=_arguments(), monotonic_ns=lambda: next(ticks)
    )

    assert created[0].closed is True
    assert len(created[0].calls) == 4


def test_pre_campaign_freshness_fails_closed_when_observation_is_over_five_ms_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_CoLocatedTransport", _FreshTransport)
    ticks = iter((0, 1, 2, 3, 5_000_001))

    with pytest.raises(adapter.RunnerAdapterError, match="RUNNER_FRESHNESS_STALE"):
        adapter._pre_campaign_freshness_admission(
            _config(), arguments=_arguments(), monotonic_ns=lambda: next(ticks)
        )


def test_runner_attestation_is_cpu_only_and_does_not_claim_serving_authority() -> None:
    attestation = adapter._attestation(_arguments()).decode("utf-8")

    assert '"gpu_access":"NONE"' in attestation
    assert '"cloud_credentials":"NONE"' in attestation
    assert '"docker_socket":"ABSENT"' in attestation
    assert '"serving_role":"OBSERVER_ONLY"' in attestation
    assert '"provider_mutation_authority":"NONE"' in attestation


def test_runner_recovers_an_already_sealed_package_without_reexecuting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = object.__new__(adapter._RunnerServer)
    server.arguments = _arguments()
    server.sealed = None
    expected_path = server._package_path()

    def fake_verify(path: Path):
        assert path == expected_path
        return SimpleNamespace(
            report=SimpleNamespace(retained_digest="sha256:" + "3" * 64)
        )

    monkeypatch.setattr(adapter, "verify_execution_package", fake_verify)
    first = server._recover_sealed()
    second = server._recover_sealed()

    assert first is second
    assert first.path == expected_path
    assert first.retained_digest == "sha256:" + "3" * 64
