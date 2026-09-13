"""Pure, fail-closed topology admission for routing-execution-v1.

This module intentionally has no HTTP, DNS, provider, or deployment imports.
Calling :func:`admit_topology` is the mandatory boundary before a caller is
allowed to create a transport, resolve a name, sample telemetry, or dispatch a
request.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.contracts import (
    EndpointDeclaration,
    EndpointId,
    PublishedEndpointIdentity,
)
from inferdrome.routing_execution.manual_host_contracts import ExecutionConfig
from inferdrome.routing_execution.vast_contracts import VastEndpointDeclaration


class TopologyAdmissionError(ValueError):
    """An execution topology is unsafe, ambiguous, or unsupported."""


@dataclass(frozen=True)
class AdmittedEndpoint:
    """A private runtime-only endpoint, with no serialization method."""

    endpoint_id: EndpointId
    canonical_origin: str
    published_identity: PublishedEndpointIdentity


@dataclass(frozen=True)
class AdmittedTopology:
    """The only endpoint material passed to the execution transport layer."""

    config: ExecutionConfig
    endpoints: tuple[AdmittedEndpoint, AdmittedEndpoint]


def _rfc1918(value: str) -> bool:
    """Accept only RFC1918, never broad library-specific 'private' ranges."""

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.version != 4:
        return False
    return any(
        address in network
        for network in (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
    )


def _canonical_origin(value: str) -> tuple[str, str, int]:
    """Normalize a base origin and reject every ambiguous URL component."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise TopologyAdmissionError("endpoint origin is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or not parsed.hostname
        or not parsed.hostname.isascii()
    ):
        raise TopologyAdmissionError("endpoint origin is invalid")
    host = parsed.hostname.lower().rstrip(".")
    if not host or host in {"0.0.0.0", "::"}:
        raise TopologyAdmissionError("endpoint origin is invalid")
    default_port = 443 if parsed.scheme == "https" else 80
    selected_port = default_port if port is None else port
    if not 1 <= selected_port <= 65_535:
        raise TopologyAdmissionError("endpoint origin is invalid")
    return f"{parsed.scheme}://{host}:{selected_port}", host, selected_port


def _admit_origin(
    config: ExecutionConfig, endpoint: EndpointDeclaration | VastEndpointDeclaration
) -> str:
    canonical, host, port = _canonical_origin(endpoint.origin)
    if config.mode == "VAST_MANUAL_CONTAINER":
        parsed = urlsplit(endpoint.origin)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or port < 1024
        ):
            raise TopologyAdmissionError(
                "Vast processes require explicit unprivileged HTTP loopback ports"
            )
        return canonical
    if config.mode == "LOCAL_LOOPBACK":
        if host != "127.0.0.1" or port in {80, 443}:
            raise TopologyAdmissionError(
                "local topology requires explicit loopback ports"
            )
        return canonical

    # A literal RFC1918 target is self-evidently private. This PR deliberately
    # does not admit an internal DNS name: urllib would resolve it later and a
    # config-supplied result could not pin that later answer. A future bounded
    # resolver/connection binding may add internal names without weakening the
    # admission-before-transport rule.
    if _rfc1918(host):
        return canonical
    raise TopologyAdmissionError("deployment requires a literal private target")


def _published_identity(
    endpoint: EndpointDeclaration | VastEndpointDeclaration, canonical_origin: str
) -> PublishedEndpointIdentity:
    capabilities = endpoint.capabilities.model_dump(mode="json")
    return PublishedEndpointIdentity(
        endpoint_id=endpoint.endpoint_id,
        origin_sha256=sha256_digest(canonical_origin.encode("utf-8")),
        capability_identity_sha256=sha256_digest(canonical_json_bytes(capabilities)),
    )


def admit_topology(config: ExecutionConfig) -> AdmittedTopology:
    """Return an admitted exactly-two-endpoint topology without external I/O.

    The GCP branch deliberately rejects PR A's current single-A100 profile:
    the explicit one-engine-per-endpoint contract requires two accelerators for
    two distinct serving engines.  It does not select a replacement shape.
    """

    if (
        config.topology.serving_engine_count != 2
        or not config.topology.one_engine_per_endpoint
    ):
        raise TopologyAdmissionError(
            "topology does not represent two independent engines"
        )
    if config.mode == "GCP_PRIVATE" and (
        config.topology.accelerator_model != "NVIDIA A100-SXM4-40GB"
        or config.topology.accelerator_count < 2
    ):
        raise TopologyAdmissionError(
            "two endpoint engines require two declared A100-SXM4-40GB accelerators"
        )
    declarations: tuple[EndpointDeclaration | VastEndpointDeclaration, ...] = (
        config.endpoints
    )
    origins = tuple(_admit_origin(config, endpoint) for endpoint in declarations)
    if len(set(origins)) != 2:
        raise TopologyAdmissionError("endpoint origins are not distinct")
    admitted = tuple(
        AdmittedEndpoint(
            endpoint_id=endpoint.endpoint_id,
            canonical_origin=origin,
            published_identity=_published_identity(endpoint, origin),
        )
        for endpoint, origin in zip(declarations, origins, strict=True)
    )
    first, second = admitted
    return AdmittedTopology(config=config, endpoints=(first, second))
