"""Closed v0.3 development capability and limitations contract.

The contract keeps current implementation facts separate from release, routing,
promotion, cloud, and acceptance authority. It is a product-story sensor, not
an attestation emitted by an external router or provider.
"""

from __future__ import annotations

from typing import Literal

from inferdrome.domain.base import FrozenModel

CapabilityStatus = Literal[
    "PROVEN_LOCAL_SOCKET_LEVEL",
    "PRESERVED_EXTERNAL_ONLY",
    "UNEXECUTED",
    "LOCAL_FAKE_VALIDATED",
    "LOCAL_FIXTURE_VALIDATED",
    "NOT_CLAIMED",
]
CapabilityId = Literal[
    "local_routing_execution",
    "external_router_evidence_adapter",
    "historical_a10_serving_evidence",
    "two_a100_multi_endpoint_campaign",
    "gcp_operation",
    "kubernetes_operation",
]


class V0_3Capability(FrozenModel):
    """One bounded implementation fact and the limit that keeps it honest."""

    capability_id: CapabilityId
    status: CapabilityStatus
    supported_fact: str
    limitation: str


class V0_3CapabilityContract(FrozenModel):
    """The active v0.3 development product claim boundary."""

    schema_version: Literal["inferdrome.v0_3_capabilities.v1"]
    product_role: Literal["MEASUREMENT_EVIDENCE_ONLY"]
    capabilities: tuple[V0_3Capability, ...]
    authorities_not_provided: tuple[
        Literal[
            "PRODUCTION_ROUTING",
            "POLICY_VERDICT",
            "PROMOTION_CONTROL",
            "ACCEPTANCE_VERDICT",
        ],
        ...,
    ]


V0_3_CAPABILITY_CONTRACT = V0_3CapabilityContract(
    schema_version="inferdrome.v0_3_capabilities.v1",
    product_role="MEASUREMENT_EVIDENCE_ONLY",
    capabilities=(
        V0_3Capability(
            capability_id="local_routing_execution",
            status="PROVEN_LOCAL_SOCKET_LEVEL",
            supported_fact=(
                "Two loopback OpenAI-compatible endpoints are exercised through "
                "the bounded routing-execution bridge, then sealed and replayed "
                "offline."
            ),
            limitation=(
                "This local proof does not establish GPU, Docker, cloud, or "
                "production-routing operation."
            ),
        ),
        V0_3Capability(
            capability_id="external_router_evidence_adapter",
            status="LOCAL_FIXTURE_VALIDATED",
            supported_fact=(
                "The llm-d-attached-v1 profile validates supplied local router "
                "facts into canonical, digest-bound evidence without importing "
                "or controlling a router."
            ),
            limitation=(
                "No native llm-d API, live cluster, router decision, or endpoint "
                "telemetry has been observed by this profile."
            ),
        ),
        V0_3Capability(
            capability_id="historical_a10_serving_evidence",
            status="PRESERVED_EXTERNAL_ONLY",
            supported_fact=(
                "The historical Qwen3-8B A10 serving archive remains preserved "
                "with committed privacy-safe handoff metadata."
            ),
            limitation=(
                "The raw archive remains EXTERNAL_ONLY and is not a v0.3 "
                "campaign, hardware comparison, or acceptance decision."
            ),
        ),
        V0_3Capability(
            capability_id="two_a100_multi_endpoint_campaign",
            status="UNEXECUTED",
            supported_fact=(
                "The repository contains a guarded, local/fake-validated "
                "one-host two-engine pre-campaign shape."
            ),
            limitation=(
                "No two-A100 multi-endpoint campaign has produced real evidence; "
                "neither same-host nor distributed execution is claimed."
            ),
        ),
        V0_3Capability(
            capability_id="gcp_operation",
            status="LOCAL_FAKE_VALIDATED",
            supported_fact=(
                "The guarded GCP pre-campaign controller is covered with local "
                "fakes and has no authority to run without separate approval."
            ),
            limitation=(
                "No GCP production operation, provider evidence, billing result, "
                "or GPU campaign is claimed."
            ),
        ),
        V0_3Capability(
            capability_id="kubernetes_operation",
            status="NOT_CLAIMED",
            supported_fact=(
                "Static and local-synthetic Kubernetes boundaries remain available "
                "for inspection."
            ),
            limitation=(
                "No Kubernetes cluster, GPU workload, or production operation is "
                "claimed."
            ),
        ),
    ),
    authorities_not_provided=(
        "PRODUCTION_ROUTING",
        "POLICY_VERDICT",
        "PROMOTION_CONTROL",
        "ACCEPTANCE_VERDICT",
    ),
)
