"""Bounded runtime-only IAP tunnel mapping for admitted private endpoints.

The sealed routing config retains its exact RFC1918 endpoint identities.  This
module maps those already-admitted origins only at transport time to the two
loopback-facing endpoints of a supervised controller tunnel.  The map is an
ephemeral input and is never serialized into the evidence package.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from inferdrome.routing_execution.contracts import RoutingExecutionConfig
from inferdrome.routing_execution.topology import admit_topology
from inferdrome.routing_execution.transport import (
    EndpointTransport,
    TransportResponse,
)

_SCHEMA_VERSION: Final = "inferdrome.routing-execution-iap-transport-map.v1"
_MAX_BYTES: Final = 16_384


class TunnelMapError(ValueError):
    """The local-only tunnel mapping is malformed or would broaden transport."""


@dataclass(frozen=True)
class IapTunnelTransportMap:
    endpoint_a_origin: str
    endpoint_b_origin: str

    @classmethod
    def load(
        cls, path: Path, *, config: RoutingExecutionConfig
    ) -> IapTunnelTransportMap:
        try:
            content = path.read_bytes()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise TunnelMapError("IAP tunnel map is unavailable") from None
        return cls.load_bytes(content, config=config)

    @classmethod
    def load_bytes(
        cls, content: bytes, *, config: RoutingExecutionConfig
    ) -> IapTunnelTransportMap:
        """Validate an ephemeral map already held in process memory."""

        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            value: dict[str, object] = {}
            for key, item in items:
                if key in value:
                    raise ValueError
                value[key] = item
            return value

        def reject_constant(_: str) -> None:
            raise ValueError

        try:
            if not 1 <= len(content) <= _MAX_BYTES:
                raise ValueError
            value = json.loads(
                content,
                object_pairs_hook=pairs,
                parse_constant=reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise TunnelMapError("IAP tunnel map is unavailable") from None
        if not isinstance(value, dict) or set(value) != {
            "endpoint_a_origin",
            "endpoint_b_origin",
            "schema_version",
        }:
            raise TunnelMapError("IAP tunnel map has an invalid shape")
        if value.get("schema_version") != _SCHEMA_VERSION:
            raise TunnelMapError("IAP tunnel map version is unsupported")
        endpoint_a_origin = value.get("endpoint_a_origin")
        endpoint_b_origin = value.get("endpoint_b_origin")
        if not isinstance(endpoint_a_origin, str) or not isinstance(
            endpoint_b_origin, str
        ):
            raise TunnelMapError("IAP tunnel map has an invalid shape")
        result = cls(
            endpoint_a_origin=endpoint_a_origin,
            endpoint_b_origin=endpoint_b_origin,
        )
        result._validate(config)
        return result

    def _validate(self, config: RoutingExecutionConfig) -> None:
        if config.mode != "GCP_PRIVATE":
            raise TunnelMapError("IAP tunnel mapping requires GCP_PRIVATE mode")
        expected = (18000, 18001)
        values = (self.endpoint_a_origin, self.endpoint_b_origin)
        if len(set(values)) != 2:
            raise TunnelMapError("IAP tunnel origins must be distinct")
        for origin, port in zip(values, expected, strict=True):
            try:
                parsed = urlsplit(origin)
                parsed_port = parsed.port
            except ValueError:
                raise TunnelMapError("IAP tunnel origin is invalid") from None
            if (
                parsed.scheme != "http"
                or parsed.hostname != "host.docker.internal"
                or parsed_port != port
                or parsed.path not in {"", "/"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise TunnelMapError("IAP tunnel origin is invalid")
        admitted = admit_topology(config)
        if tuple(endpoint.endpoint_id for endpoint in admitted.endpoints) != (
            "endpoint-a",
            "endpoint-b",
        ):
            raise TunnelMapError("IAP tunnel topology is invalid")

    def logical_to_tunnel(self, config: RoutingExecutionConfig) -> dict[str, str]:
        self._validate(config)
        admitted = admit_topology(config)
        return {
            admitted.endpoints[0].canonical_origin: self.endpoint_a_origin,
            admitted.endpoints[1].canonical_origin: self.endpoint_b_origin,
        }


class IapTunnelMappedTransport:
    """Map only the two admitted logical origins to local IAP tunnel ports."""

    def __init__(self, *, inner: EndpointTransport, origins: dict[str, str]) -> None:
        if len(origins) != 2 or len(set(origins.values())) != 2:
            raise TunnelMapError("IAP tunnel mapping is not exactly two endpoints")
        self._inner = inner
        self._origins = dict(origins)

    def _mapped(self, origin: str) -> str:
        try:
            return self._origins[origin]
        except KeyError:
            raise TunnelMapError("IAP tunnel mapping rejected an endpoint") from None

    def get(self, origin: str, path: str, *, timeout_ms: int) -> TransportResponse:
        return self._inner.get(self._mapped(origin), path, timeout_ms=timeout_ms)

    def post_json(
        self, origin: str, path: str, body: bytes, *, timeout_ms: int
    ) -> TransportResponse:
        return self._inner.post_json(
            self._mapped(origin), path, body, timeout_ms=timeout_ms
        )

    def close(self) -> None:
        self._inner.close()
