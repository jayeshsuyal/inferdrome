"""Pure PR-B handoff from verified two-engine GCP readiness to routing inputs.

The handoff is intentionally not a provider client and does not dispatch any
request.  It materializes the already-reviewed ``routing-execution-v1`` source
configuration only after the pre-campaign controller has validated exact
two-GPU private readiness.  The returned byte strings are ephemeral invocation
inputs; published PR-B evidence retains only endpoint-origin hashes.
"""

from __future__ import annotations

from dataclasses import dataclass

from inferdrome.deployment.gcp_private_campaign_v2 import (
    GcpPrivateCampaignError,
    GcpPrivateCampaignProposal,
    GcpPrivateCampaignReadiness,
)
from inferdrome.qwen3_campaign import qwen3_workload_sha256
from inferdrome.routing_execution.canonical import (
    canonical_json_bytes,
    sha256_digest,
)
from inferdrome.routing_execution.contracts import (
    RoutingExecutionConfig,
    fixed_policy_ids,
    fixed_selected_workload_bytes,
)
from inferdrome.routing_execution.executor import declared_input_transfer_digest
from inferdrome.routing_execution.topology import TopologyAdmissionError, admit_topology


@dataclass(frozen=True)
class GcpPrivateRoutingExecutionInputs:
    """In-memory PR-B runner inputs with no publication/serialization method."""

    routing_config: RoutingExecutionConfig
    routing_config_bytes: bytes
    workload_bytes: bytes
    routing_config_sha256: str


def _policy_set_sha256() -> str:
    return sha256_digest(canonical_json_bytes(list(fixed_policy_ids())))


def _assert_verified_binding(
    proposal: GcpPrivateCampaignProposal,
    readiness: GcpPrivateCampaignReadiness,
) -> None:
    if (
        readiness.proposal_id != proposal.proposal_id
        or readiness.project_id != proposal.topology.project_id
        or readiness.zone != proposal.topology.zone
        or readiness.instance_name != proposal.instance_name
        or readiness.ownership_labels != proposal.ownership_labels
        or readiness.machine_type != "a2-highgpu-2g"
        or readiness.accelerator_model != "NVIDIA A100-SXM4-40GB"
        or readiness.accelerator_count != 2
        or readiness.startup_payload_digest != proposal.startup_payload_digest
    ):
        raise GcpPrivateCampaignError("ROUTING_HANDOFF_READINESS_MISMATCH")
    if (
        proposal.routing.workload_sha256 != qwen3_workload_sha256()
        or proposal.routing.selected_workload_sha256
        != sha256_digest(fixed_selected_workload_bytes())
        or proposal.routing.policy_set_sha256 != _policy_set_sha256()
    ):
        raise GcpPrivateCampaignError("ROUTING_HANDOFF_INPUT_MISMATCH")


def build_gcp_private_routing_execution_inputs(
    proposal: GcpPrivateCampaignProposal,
    readiness: GcpPrivateCampaignReadiness,
) -> GcpPrivateRoutingExecutionInputs:
    """Build exact PR-B config/workload bytes only from verified readiness.

    ``admit_topology`` runs before the result is returned.  Consequently a
    caller cannot use this helper to materialize an ambiguous, public, or
    duplicate two-endpoint configuration and then create an HTTP transport.
    """

    _assert_verified_binding(proposal, readiness)
    model = proposal.model.model_dump(mode="json")
    runtime = proposal.runtime.model_dump(mode="json")
    serving_image = proposal.serving_image.model_dump(mode="json")
    runner_image = proposal.runner_image.model_dump(mode="json")
    capabilities = {
        "health": "HTTP_HEALTH_V1",
        "load": "VLLM_PROMETHEUS_V1",
        "gpu_dcgm": "UNAVAILABLE",
        "kv_cache": "UNAVAILABLE",
    }
    origins = (
        f"http://{readiness.private_ipv4}:8000",
        f"http://{readiness.private_ipv4}:8001",
    )
    config_value: dict[str, object] = {
        "schema_version": "inferdrome.routing-execution-config.v1",
        "execution_id": "routing-execution-v1",
        "mode": "GCP_PRIVATE",
        "source_commit": proposal.source_commit,
        "runner_image": runner_image,
        "serving_image": serving_image,
        "model": model,
        "runtime": runtime,
        "routing_inputs": {
            "campaign_id": proposal.routing.campaign_id,
            "plan_sha256": proposal.routing.plan_sha256,
            "trace_sha256": proposal.routing.trace_sha256,
            "fault_schedule_sha256": proposal.routing.fault_schedule_sha256,
            "trial_plan_sha256": proposal.routing.trial_plan_sha256,
            "policies": list(fixed_policy_ids()),
        },
        "workload": {
            "workload_id": "inferdrome.qwen-text-mixed-length.v1",
            "workload_sha256": proposal.routing.workload_sha256,
            "selected_workload_sha256": proposal.routing.selected_workload_sha256,
            "selected_request_ids": [f"request-{index:03d}" for index in range(6)],
            "request_denominator": 6,
        },
        "endpoints": [
            {
                "endpoint_id": endpoint_id,
                "origin": origin,
                "model": model,
                "runtime": runtime,
                "serving_image": serving_image,
                "workload_sha256": proposal.routing.workload_sha256,
                "capabilities": capabilities,
            }
            for endpoint_id, origin in zip(
                ("endpoint-a", "endpoint-b"), origins, strict=True
            )
        ],
        "telemetry": {
            "clock_domain": "RUNNER_MONOTONIC_NS",
            "health_freshness_ms": 5,
            "load_freshness_ms": 5,
            "gpu_freshness_ms": 5,
            "load_metric_name": "vllm:num_requests_running",
        },
        "fault": {
            "fault_id": "stale-load-fresh-health-v1",
            "load_collection_pause_after_sequence_index": 1,
            "health_collection_continues": True,
            "inter_request_interval_ms": 10,
        },
        "topology": {
            "runner_separate_from_serving": True,
            "serving_engine_count": 2,
            "one_engine_per_endpoint": True,
            "accelerator_model": "NVIDIA A100-SXM4-40GB",
            "accelerator_count": 2,
        },
        "evidence_destination": {
            "destination_sha256": proposal.routing.evidence_destination_sha256,
            "declared_input_transfer_sha256": "sha256:" + ("0" * 64),
            "publication_mode": "LOCAL_CREATE_NO_REPLACE_V1",
        },
        "request_timeout_ms": 1000,
        "no_retry": True,
    }
    try:
        parsed = RoutingExecutionConfig.model_validate_json(
            canonical_json_bytes(config_value)
        )
        projection = parsed.model_dump(mode="json")
        destination = dict(projection["evidence_destination"])
        destination["declared_input_transfer_sha256"] = declared_input_transfer_digest(
            parsed
        )
        projection["evidence_destination"] = destination
        parsed = RoutingExecutionConfig.model_validate_json(
            canonical_json_bytes(projection)
        )
        admitted = admit_topology(parsed)
    except (TopologyAdmissionError, ValueError):
        raise GcpPrivateCampaignError("ROUTING_HANDOFF_ADMISSION_FAILED") from None
    if tuple(endpoint.endpoint_id for endpoint in admitted.endpoints) != (
        "endpoint-a",
        "endpoint-b",
    ):
        raise GcpPrivateCampaignError("ROUTING_HANDOFF_ADMISSION_FAILED")
    config_bytes = canonical_json_bytes(parsed.model_dump(mode="json"))
    workload = fixed_selected_workload_bytes()
    return GcpPrivateRoutingExecutionInputs(
        routing_config=parsed,
        routing_config_bytes=config_bytes,
        workload_bytes=workload,
        routing_config_sha256=sha256_digest(config_bytes),
    )
