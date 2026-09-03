"""Ephemeral IAP mapping tests; sealed GCP_PRIVATE identities are unchanged."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inferdrome.deployment.gcp_private_campaign_v2 import (
    FakeGcpPrivateCampaignTransport,
    build_gcp_private_campaign_create_request,
)
from inferdrome.deployment.gcp_private_routing_handoff import (
    build_gcp_private_routing_execution_inputs,
)
from inferdrome.routing_execution.contracts import RoutingExecutionConfig
from inferdrome.routing_execution.transport import TransportResponse
from inferdrome.routing_execution.tunnel import (
    IapTunnelMappedTransport,
    IapTunnelTransportMap,
    TunnelMapError,
)
from tests.unit.test_gcp_private_campaign_v2 import _proposal


class _Transport:
    def __init__(self) -> None:
        self.origins: list[str] = []

    def get(self, origin: str, path: str, *, timeout_ms: int) -> TransportResponse:
        del path, timeout_ms
        self.origins.append(origin)
        return TransportResponse(status=200, body=b"")

    def post_json(
        self, origin: str, path: str, body: bytes, *, timeout_ms: int
    ) -> TransportResponse:
        del path, body, timeout_ms
        self.origins.append(origin)
        return TransportResponse(status=200, body=b"{}")

    def close(self) -> None:
        return None


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


def _write_map(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_iap_map_maps_only_the_two_admitted_private_origins(tmp_path: Path) -> None:
    config = _config()
    path = tmp_path / "iap-map.json"
    _write_map(
        path,
        {
            "schema_version": "inferdrome.routing-execution-iap-transport-map.v1",
            "endpoint_a_origin": "http://host.docker.internal:18000",
            "endpoint_b_origin": "http://host.docker.internal:18001",
        },
    )
    mapping = IapTunnelTransportMap.load(path, config=config)
    inner = _Transport()
    transport = IapTunnelMappedTransport(
        inner=inner, origins=mapping.logical_to_tunnel(config)
    )
    logical_a = config.endpoints[0].origin
    transport.get(logical_a, "/health", timeout_ms=100)

    assert inner.origins == ["http://host.docker.internal:18000"]
    with pytest.raises(TunnelMapError, match="rejected an endpoint"):
        transport.get("http://10.0.0.9:8000", "/health", timeout_ms=100)


@pytest.mark.parametrize(
    "a_origin,b_origin",
    (
        (None, "http://host.docker.internal:18001"),
        (123, "http://host.docker.internal:18001"),
        ("http://host.docker.internal:18001", "http://host.docker.internal:18001"),
        ("http://host.docker.internal:8000", "http://host.docker.internal:18001"),
    ),
)
def test_iap_map_rejects_malformed_or_ambiguous_local_origins(
    tmp_path: Path, a_origin: object, b_origin: object
) -> None:
    config = _config()
    path = tmp_path / "iap-map.json"
    _write_map(
        path,
        {
            "schema_version": "inferdrome.routing-execution-iap-transport-map.v1",
            "endpoint_a_origin": a_origin,
            "endpoint_b_origin": b_origin,
        },
    )
    with pytest.raises(TunnelMapError):
        IapTunnelTransportMap.load(path, config=config)
