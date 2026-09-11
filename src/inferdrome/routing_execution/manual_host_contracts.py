"""Additive v2 manual-host declarations; v1 contracts remain closed and unchanged.

These are operator declarations, not provider/driver attestations. The existing
v1 receipt, sealing, digest and replay semantics are deliberately reused.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from inferdrome.routing_execution.contracts import (
    Commit,
    Digest,
    EndpointDeclaration,
    EvidenceDestination,
    ExecutedManifest,
    ExecutionId,
    ExecutionModel,
    FaultPlan,
    ImageIdentity,
    ModelIdentity,
    PublishedEndpointIdentity,
    RoutingExecutionConfig,
    RoutingInputIdentity,
    RuntimeIdentity,
    TelemetryPlan,
    WorkloadIdentity,
    oci_content_digest,
)


class ManualHostTopology(ExecutionModel):
    profile_id: Literal["lambda-manual-two-a100-pcie-40gb-v1"]
    provider: Literal["LAMBDA"]
    provisioning: Literal["OPERATOR_SUPPLIED_VM"]
    identity_assertion: Literal["OPERATOR_DECLARED_NOT_OBSERVED"]
    lifecycle_protection: Literal["UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY"]
    declaration_sha256: Digest
    host_identity_sha256: Digest
    gpu_uuid_sha256: tuple[Digest, Digest]
    runner_separate_from_serving: Literal[True]
    serving_engine_count: Literal[2]
    one_engine_per_endpoint: Literal[True]
    tensor_parallel_size: Literal[1]
    accelerator_model: Literal["NVIDIA A100-PCIE-40GB"]
    accelerator_count: Literal[2]

    @model_validator(mode="after")
    def _distinct_gpus(self) -> ManualHostTopology:
        if len(set(self.gpu_uuid_sha256)) != 2:
            raise ValueError("manual host needs distinct GPU UUID declarations")
        return self


class H100ManualHostTopology(ManualHostTopology):
    """Additive H100 SXM5 topology; v2 A100 literals remain untouched."""

    profile_id: Literal["lambda-manual-two-h100-sxm5-80gb-v1"]
    accelerator_model: Literal["NVIDIA H100-SXM5-80GB"]


class ManualHostRoutingConfig(ExecutionModel):
    schema_version: Literal["inferdrome.routing-execution-config.v2"]
    execution_id: ExecutionId
    mode: Literal["LAMBDA_MANUAL_HOST"]
    source_commit: Commit
    runner_image: ImageIdentity
    serving_image: ImageIdentity
    model: ModelIdentity
    runtime: RuntimeIdentity
    routing_inputs: RoutingInputIdentity
    workload: WorkloadIdentity
    endpoints: tuple[EndpointDeclaration, EndpointDeclaration]
    telemetry: TelemetryPlan
    fault: FaultPlan
    topology: ManualHostTopology
    evidence_destination: EvidenceDestination
    request_timeout_ms: Annotated[int, Field(ge=1, le=60_000)]
    no_retry: Literal[True]

    @model_validator(mode="after")
    def _bindings(self) -> ManualHostRoutingConfig:
        if tuple(e.endpoint_id for e in self.endpoints) != ("endpoint-a", "endpoint-b"):
            raise ValueError("manual host endpoint order is invalid")
        for endpoint in self.endpoints:
            if (
                endpoint.model != self.model
                or endpoint.runtime != self.runtime
                or endpoint.serving_image != self.serving_image
                or endpoint.workload_sha256 != self.workload.workload_sha256
            ):
                raise ValueError("manual host endpoint identities disagree")
        if oci_content_digest(self.runner_image) == oci_content_digest(
            self.serving_image
        ):
            raise ValueError("runner and serving image content must be distinct")
        return self


class H100ManualHostRoutingConfig(ManualHostRoutingConfig):
    """Versioned H100 config with unchanged routing/evidence semantics."""

    schema_version: Literal["inferdrome.routing-execution-config.v3"]
    topology: H100ManualHostTopology


class ManualHostExecutedManifest(ExecutionModel):
    schema_version: Literal["inferdrome.routing-executed-manifest.v2"]
    execution_id: ExecutionId
    mode: Literal["LAMBDA_MANUAL_HOST"]
    source_commit: Commit
    config_sha256: Digest
    runner_image: ImageIdentity
    serving_image: ImageIdentity
    model: ModelIdentity
    runtime: RuntimeIdentity
    routing_inputs: RoutingInputIdentity
    workload: WorkloadIdentity
    endpoints: tuple[PublishedEndpointIdentity, PublishedEndpointIdentity]
    telemetry: TelemetryPlan
    fault: FaultPlan
    topology: ManualHostTopology
    evidence_destination_sha256: Digest
    input_transfer_receipt_sha256: Digest
    planned_requests_per_trial: Literal[6]
    planned_terminal_denominator: Literal[18]
    no_retry: Literal[True]

    @model_validator(mode="after")
    def _bindings(self) -> ManualHostExecutedManifest:
        if (
            tuple(e.endpoint_id for e in self.endpoints) != ("endpoint-a", "endpoint-b")
            or self.endpoints[0].origin_sha256 == self.endpoints[1].origin_sha256
        ):
            raise ValueError("manual host manifest endpoints are invalid")
        if oci_content_digest(self.runner_image) == oci_content_digest(
            self.serving_image
        ):
            raise ValueError("manual host manifest image roles are not distinct")
        return self


class H100ManualHostExecutedManifest(ManualHostExecutedManifest):
    """Versioned H100 manifest paired only with the v3 manual-host config."""

    schema_version: Literal["inferdrome.routing-executed-manifest.v3"]
    topology: H100ManualHostTopology


ExecutionConfig = (
    RoutingExecutionConfig | ManualHostRoutingConfig | H100ManualHostRoutingConfig
)
ExecutionManifest = (
    ExecutedManifest | ManualHostExecutedManifest | H100ManualHostExecutedManifest
)
CONFIG_ADAPTER: TypeAdapter[ExecutionConfig] = TypeAdapter(
    Annotated[ExecutionConfig, Field(discriminator="schema_version")]
)
MANIFEST_ADAPTER: TypeAdapter[ExecutionManifest] = TypeAdapter(
    Annotated[ExecutionManifest, Field(discriminator="schema_version")]
)
