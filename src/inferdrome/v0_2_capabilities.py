"""Closed v0.2 product capability and limitations contract.

This is a claim boundary, not a runtime attestation or release decision.  Its
small fixed surface makes the local demonstration and historical evidence
status inspectable without turning Inferdrome into a verdict authority.
"""

from __future__ import annotations

from typing import Literal

from inferdrome.domain.base import FrozenModel

CapabilityStatus = Literal[
    "PROVEN_LOCAL_SOCKET_LEVEL",
    "PRESERVED_EXTERNAL_ONLY",
    "UNEXECUTED",
    "LOCAL_FAKE_VALIDATED",
    "NOT_CLAIMED",
]
CapabilityId = Literal[
    "local_routing_execution",
    "historical_a10_serving_evidence",
    "two_a100_multi_endpoint_campaign",
    "gcp_operation",
    "kubernetes_operation",
]


class V0_2Capability(FrozenModel):
    """One closed claim and the boundary that keeps it honest."""

    capability_id: CapabilityId
    status: CapabilityStatus
    supported_fact: str
    limitation: str


class V0_2CapabilityContract(FrozenModel):
    """The active, non-frozen v0.2 product claim boundary."""

    schema_version: Literal["inferdrome.v0_2_capabilities.v1"]
    product_role: Literal["MEASUREMENT_EVIDENCE_ONLY"]
    capabilities: tuple[V0_2Capability, ...]
    authorities_not_provided: tuple[
        Literal[
            "PRODUCTION_ROUTING",
            "POLICY_VERDICT",
            "PROMOTION_CONTROL",
            "ACCEPTANCE_VERDICT",
        ],
        ...,
    ]


V0_2_CAPABILITY_CONTRACT = V0_2CapabilityContract(
    schema_version="inferdrome.v0_2_capabilities.v1",
    product_role="MEASUREMENT_EVIDENCE_ONLY",
    capabilities=(
        V0_2Capability(
            capability_id="local_routing_execution",
            status="PROVEN_LOCAL_SOCKET_LEVEL",
            supported_fact=(
                "Two loopback OpenAI-compatible endpoints are exercised through "
                "the bounded routing-execution bridge, then sealed and replayed "
                "offline."
            ),
            limitation=(
                "This local proof does not establish a GPU, Docker, cloud, or "
                "production-routing operation."
            ),
        ),
        V0_2Capability(
            capability_id="historical_a10_serving_evidence",
            status="PRESERVED_EXTERNAL_ONLY",
            supported_fact=(
                "The historical Qwen3-8B A10 serving archive remains preserved "
                "with its committed privacy-safe handoff metadata."
            ),
            limitation=(
                "The raw archive remains EXTERNAL_ONLY and is not a new v0.2 "
                "campaign, hardware comparison, or acceptance decision."
            ),
        ),
        V0_2Capability(
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
        V0_2Capability(
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
        V0_2Capability(
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
