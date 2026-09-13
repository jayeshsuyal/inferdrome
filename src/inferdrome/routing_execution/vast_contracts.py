"""Closed v4 declarations for separate processes in one Vast container.

These pure contracts do not import the supervisor, inspect a process, or attest
the provider-selected OCI image. Artifact hashes bind separately retained local
records; they do not turn operator declarations into provider observations.
The v1 receipt, sealing and replay contracts remain unchanged.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from inferdrome.qwen3_campaign import (
    qwen3_expected_snapshot_sha256,
    qwen3_model_manifest_sha256,
)
from inferdrome.routing_execution.contracts import (
    Commit,
    Digest,
    EndpointCapabilities,
    EndpointId,
    EvidenceDestination,
    ExecutionId,
    ExecutionModel,
    FaultPlan,
    ImageIdentity,
    ModelIdentity,
    PublishedEndpointIdentity,
    RoutingInputIdentity,
    RuntimeIdentity,
    TelemetryPlan,
    WorkloadIdentity,
)


class VastArtifactProvenance(ExecutionModel):
    """Declared image/source and hashes of separately retained local observations."""

    container_image_assertion: Literal["OPERATOR_DECLARED_NOT_OBSERVED"]
    source_commit: Commit
    observer_artifact_sha256: Digest
    supervisor_artifact_sha256: Digest
    model_manifest_sha256: Digest
    model_snapshot_sha256: Digest
    runtime_observation_sha256: Digest
    runtime_assertion: Literal["LOCAL_PROCESS_OBSERVATIONS_NOT_PROVIDER_ATTESTATION"]

    @model_validator(mode="after")
    def _frozen_model(self) -> VastArtifactProvenance:
        if (
            self.model_manifest_sha256 != qwen3_model_manifest_sha256()
            or self.model_snapshot_sha256 != qwen3_expected_snapshot_sha256()
        ):
            raise ValueError("Vast model artifact identities disagree with frozen pins")
        return self


class VastProcessTopology(ExecutionModel):
    """One shared container; process separation does not exclude GPU device access."""

    profile_id: Literal["vast-container-two-h100-sxm5-80gb-v1"]
    provider: Literal["VAST_AI"]
    provisioning: Literal["OPERATOR_SUPPLIED_CONTAINER"]
    identity_assertion: Literal["OPERATOR_DECLARED_NOT_OBSERVED"]
    lifecycle_protection: Literal["UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY"]
    declaration_sha256: Digest
    instance_identity_sha256: Digest
    gpu_uuid_sha256: tuple[Digest, Digest]
    container_count: Literal[1]
    serving_engine_count: Literal[2]
    one_engine_per_endpoint: Literal[True]
    tensor_parallel_size: Literal[1]
    accelerator_model: Literal["NVIDIA H100-SXM5-80GB"]
    accelerator_count: Literal[2]
    isolation_boundary: Literal["SEPARATE_PROCESSES_SHARED_CONTAINER"]
    observer_gpu_isolation: Literal["ENVIRONMENT_ONLY_NOT_HARDWARE_ENFORCED"]

    @model_validator(mode="after")
    def _distinct_gpus(self) -> VastProcessTopology:
        if len(set(self.gpu_uuid_sha256)) != 2:
            raise ValueError("Vast engines require distinct declared GPU UUIDs")
        return self


class VastEndpointDeclaration(ExecutionModel):
    """A runtime-only loopback endpoint in the same declared container image."""

    endpoint_id: EndpointId
    origin: Annotated[str, Field(min_length=1, max_length=2048)]
    model: ModelIdentity
    runtime: RuntimeIdentity
    container_image: ImageIdentity
    workload_sha256: Digest
    capabilities: EndpointCapabilities


class VastRoutingConfig(ExecutionModel):
    schema_version: Literal["inferdrome.routing-execution-config.v4"]
    execution_id: ExecutionId
    mode: Literal["VAST_MANUAL_CONTAINER"]
    source_commit: Commit
    container_image: ImageIdentity
    artifact_provenance: VastArtifactProvenance
    model: ModelIdentity
    runtime: RuntimeIdentity
    routing_inputs: RoutingInputIdentity
    workload: WorkloadIdentity
    endpoints: tuple[VastEndpointDeclaration, VastEndpointDeclaration]
    telemetry: TelemetryPlan
    fault: FaultPlan
    topology: VastProcessTopology
    evidence_destination: EvidenceDestination
    request_timeout_ms: Annotated[int, Field(ge=1, le=60_000)]
    no_retry: Literal[True]

    @model_validator(mode="after")
    def _bindings(self) -> VastRoutingConfig:
        if self.artifact_provenance.source_commit != self.source_commit:
            raise ValueError("Vast source provenance disagrees")
        if tuple(e.endpoint_id for e in self.endpoints) != ("endpoint-a", "endpoint-b"):
            raise ValueError("Vast endpoint order is invalid")
        for endpoint in self.endpoints:
            if (
                endpoint.model != self.model
                or endpoint.runtime != self.runtime
                or endpoint.container_image != self.container_image
                or endpoint.workload_sha256 != self.workload.workload_sha256
            ):
                raise ValueError("Vast endpoint artifact identities disagree")
        return self


class VastExecutedManifest(ExecutionModel):
    """Redacted v4 facts; raw provider, process, GPU and origin values stay private."""

    schema_version: Literal["inferdrome.routing-executed-manifest.v4"]
    execution_id: ExecutionId
    mode: Literal["VAST_MANUAL_CONTAINER"]
    source_commit: Commit
    config_sha256: Digest
    container_image: ImageIdentity
    artifact_provenance: VastArtifactProvenance
    model: ModelIdentity
    runtime: RuntimeIdentity
    routing_inputs: RoutingInputIdentity
    workload: WorkloadIdentity
    endpoints: tuple[PublishedEndpointIdentity, PublishedEndpointIdentity]
    telemetry: TelemetryPlan
    fault: FaultPlan
    topology: VastProcessTopology
    evidence_destination_sha256: Digest
    input_transfer_receipt_sha256: Digest
    planned_requests_per_trial: Literal[6]
    planned_terminal_denominator: Literal[18]
    no_retry: Literal[True]

    @model_validator(mode="after")
    def _bindings(self) -> VastExecutedManifest:
        if self.artifact_provenance.source_commit != self.source_commit:
            raise ValueError("Vast manifest source provenance disagrees")
        if (
            tuple(e.endpoint_id for e in self.endpoints) != ("endpoint-a", "endpoint-b")
            or self.endpoints[0].origin_sha256 == self.endpoints[1].origin_sha256
        ):
            raise ValueError("Vast manifest endpoints are invalid")
        return self
