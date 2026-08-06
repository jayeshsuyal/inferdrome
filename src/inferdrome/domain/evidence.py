"""Portable evidence-bundle descriptor contract."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.ids import (
    ExperimentId,
    RelativeArtifactPath,
    RunId,
    SemanticVersion,
    Sha256Digest,
)
from inferdrome.domain.states import (
    EnvironmentCompleteness,
    EvidenceEligibility,
    Replayability,
)


class EvidenceExecutionMode(StrEnum):
    ATTACHED_ENDPOINT = "attached_endpoint"
    SYNTHETIC_FIXTURE = "synthetic_fixture"


class ArtifactRole(StrEnum):
    BUNDLE_DESCRIPTOR = "bundle_descriptor"
    ORIGINAL_SPEC = "original_spec"
    RESOLVED_SPEC = "resolved_spec"
    REQUEST_PLAN = "request_plan"
    ENVIRONMENT = "environment"
    EXECUTION = "execution"
    PRODUCER_INVOCATION = "producer_invocation"
    PRODUCER_VERSION = "producer_version"
    PRODUCER_EXIT_STATUS = "producer_exit_status"
    NATIVE_RESULT = "native_result"
    NATIVE_STDOUT = "native_stdout"
    NATIVE_STDERR = "native_stderr"
    REQUEST_RECORDS = "request_records"
    METRIC_DEFINITIONS = "metric_definitions"
    MEASUREMENTS = "measurements"
    INTEGRITY_MANIFEST = "integrity_manifest"


class ArtifactSensitivity(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL_DIAGNOSTIC = "INTERNAL_DIAGNOSTIC"
    PROMPT_CONTENT = "PROMPT_CONTENT"
    RESPONSE_CONTENT = "RESPONSE_CONTENT"


class ArtifactMediaType(StrEnum):
    JSON = "application/json"
    JSONL = "application/x-ndjson"
    YAML = "application/yaml"
    TEXT = "text/plain"


class ArtifactDescriptor(FrozenModel):
    path: RelativeArtifactPath
    role: ArtifactRole
    media_type: ArtifactMediaType
    sensitivity: ArtifactSensitivity
    required: Literal[True]


class VllmProducerDescriptor(FrozenModel):
    name: Literal["vllm"]
    version: Literal["0.26.0"]
    adapter: Literal["vllm_bench_serve"]
    adapter_version: SemanticVersion
    native_schema_fingerprint: Sha256Digest


class FakeProducerDescriptor(FrozenModel):
    name: Literal["inferdrome_fake"]
    version: SemanticVersion
    adapter: Literal["fake"]
    adapter_version: SemanticVersion
    native_schema_fingerprint: Sha256Digest


ProducerDescriptor = Annotated[
    VllmProducerDescriptor | FakeProducerDescriptor,
    Field(discriminator="name"),
]


class DigestDomains(FrozenModel):
    source_spec_digest: Sha256Digest
    execution_fingerprint: Sha256Digest
    request_plan_digest: Sha256Digest
    metric_definitions_digest: Sha256Digest
    exitspec_contract_digest: Sha256Digest | None


class SensitivityDeclaration(FrozenModel):
    prompt_content_in_request_plan: bool
    canonical_response_content_included: bool
    native_response_content_present: bool
    secrets_permitted: Literal[False]


_REQUIRED_ARTIFACT_ROLES = frozenset(ArtifactRole)
_MEDIA_TYPE_BY_ROLE: dict[ArtifactRole, ArtifactMediaType] = {
    ArtifactRole.BUNDLE_DESCRIPTOR: ArtifactMediaType.JSON,
    ArtifactRole.ORIGINAL_SPEC: ArtifactMediaType.YAML,
    ArtifactRole.RESOLVED_SPEC: ArtifactMediaType.JSON,
    ArtifactRole.REQUEST_PLAN: ArtifactMediaType.JSON,
    ArtifactRole.ENVIRONMENT: ArtifactMediaType.JSON,
    ArtifactRole.EXECUTION: ArtifactMediaType.JSON,
    ArtifactRole.PRODUCER_INVOCATION: ArtifactMediaType.JSON,
    ArtifactRole.PRODUCER_VERSION: ArtifactMediaType.TEXT,
    ArtifactRole.PRODUCER_EXIT_STATUS: ArtifactMediaType.TEXT,
    ArtifactRole.NATIVE_RESULT: ArtifactMediaType.JSON,
    ArtifactRole.NATIVE_STDOUT: ArtifactMediaType.TEXT,
    ArtifactRole.NATIVE_STDERR: ArtifactMediaType.TEXT,
    ArtifactRole.REQUEST_RECORDS: ArtifactMediaType.JSONL,
    ArtifactRole.METRIC_DEFINITIONS: ArtifactMediaType.JSON,
    ArtifactRole.MEASUREMENTS: ArtifactMediaType.JSON,
    ArtifactRole.INTEGRITY_MANIFEST: ArtifactMediaType.JSON,
}


class EvidenceBundle(FrozenModel):
    schema_version: Literal["inferdrome.evidence.v1"]
    run_id: RunId
    experiment_id: ExperimentId
    created_at: AwareDatetime
    execution_mode: EvidenceExecutionMode
    run_state: Literal["COMPLETE"]
    integrity_status: Literal["VALID"]
    environment_completeness: EnvironmentCompleteness
    evidence_eligibility: EvidenceEligibility
    replayability: Replayability
    producer: ProducerDescriptor
    digests: DigestDomains
    sensitivity: SensitivityDeclaration
    integrity_manifest_path: RelativeArtifactPath
    artifacts: Annotated[tuple[ArtifactDescriptor, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_bundle_contract(self) -> "EvidenceBundle":
        paths = [artifact.path for artifact in self.artifacts]
        roles = [artifact.role for artifact in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        if len(roles) != len(set(roles)):
            raise ValueError("v1 artifact roles must be unique")
        if set(roles) != _REQUIRED_ARTIFACT_ROLES:
            raise ValueError("bundle must declare every normative v1 artifact role")

        artifacts_by_role = {artifact.role: artifact for artifact in self.artifacts}
        for role, expected_media_type in _MEDIA_TYPE_BY_ROLE.items():
            if artifacts_by_role[role].media_type is not expected_media_type:
                raise ValueError(f"artifact media type does not match role: {role}")

        manifest_entries = [
            artifact
            for artifact in self.artifacts
            if artifact.role is ArtifactRole.INTEGRITY_MANIFEST
        ]
        if manifest_entries[0].path != self.integrity_manifest_path:
            raise ValueError(
                "integrity manifest path does not match artifact inventory"
            )

        if isinstance(self.producer, VllmProducerDescriptor):
            if self.execution_mode is not EvidenceExecutionMode.ATTACHED_ENDPOINT:
                raise ValueError("vLLM evidence requires attached-endpoint mode")
            if self.evidence_eligibility is EvidenceEligibility.SYNTHETIC_ONLY:
                raise ValueError("vLLM evidence cannot be marked synthetic-only")
            if not self.sensitivity.native_response_content_present:
                raise ValueError(
                    "detailed vLLM native output contains response content"
                )
        else:
            if self.execution_mode is not EvidenceExecutionMode.SYNTHETIC_FIXTURE:
                raise ValueError("fake producer requires synthetic-fixture mode")
            if self.evidence_eligibility is not EvidenceEligibility.SYNTHETIC_ONLY:
                raise ValueError("fake producer must remain synthetic-only")

        if (
            self.sensitivity.native_response_content_present
            and artifacts_by_role[ArtifactRole.NATIVE_RESULT].sensitivity
            is not ArtifactSensitivity.RESPONSE_CONTENT
        ):
            raise ValueError(
                "response-content-bearing native output must be classified"
            )
        if (
            self.sensitivity.prompt_content_in_request_plan
            and artifacts_by_role[ArtifactRole.REQUEST_PLAN].sensitivity
            is not ArtifactSensitivity.PROMPT_CONTENT
        ):
            raise ValueError("prompt-bearing request plan must be classified")
        if (
            self.sensitivity.canonical_response_content_included
            and artifacts_by_role[ArtifactRole.REQUEST_RECORDS].sensitivity
            is not ArtifactSensitivity.RESPONSE_CONTENT
        ):
            raise ValueError("response-bearing request records must be classified")
        return self
