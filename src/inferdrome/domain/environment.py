"""Allowlist-based environment and provenance contract."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.ids import RelativeArtifactPath, RunId
from inferdrome.domain.states import EnvironmentCompleteness


class ProvenanceKind(StrEnum):
    DECLARED = "DECLARED"
    CONFIGURED = "CONFIGURED"
    CLIENT_OBSERVED = "CLIENT_OBSERVED"
    SERVER_REPORTED = "SERVER_REPORTED"
    LOCALLY_VERIFIED = "LOCALLY_VERIFIED"
    DERIVED = "DERIVED"
    UNKNOWN = "UNKNOWN"


class EnvironmentFieldName(StrEnum):
    CLIENT_OS = "client.os"
    CLIENT_ARCH = "client.arch"
    CLIENT_PYTHON_VERSION = "client.python_version"
    PRODUCER_VERSION = "producer.version"
    PRODUCER_DISTRIBUTION_SHA256 = "producer.distribution_sha256"
    TARGET_ENGINE_VERSION = "target.engine_version"
    TARGET_MODEL_REVISION = "target.model_revision"
    TARGET_TOKENIZER_REVISION = "target.tokenizer_revision"
    SERVER_MODEL_ID = "server.model_id"
    GPU_MODEL = "gpu.model"
    GPU_COUNT = "gpu.count"
    CUDA_VERSION = "cuda.version"
    DRIVER_VERSION = "driver.version"


EnvironmentScalar = (
    Annotated[str, Field(min_length=1, max_length=4096)]
    | Annotated[int, Field(strict=True)]
    | bool
)


class EnvironmentField(FrozenModel):
    name: EnvironmentFieldName
    value: EnvironmentScalar | None
    provenance: ProvenanceKind
    evidence_path: RelativeArtifactPath | None

    @model_validator(mode="after")
    def unknown_state_must_match_value(self) -> "EnvironmentField":
        if self.provenance is ProvenanceKind.UNKNOWN:
            if self.value is not None or self.evidence_path is not None:
                raise ValueError(
                    "unknown environment field cannot claim value or evidence"
                )
        elif self.value is None:
            raise ValueError("known environment provenance requires a value")
        return self


class EnvironmentManifest(FrozenModel):
    schema_version: Literal["inferdrome.environment.v1"]
    run_id: RunId
    field_set_version: Literal["inferdrome.environment-fields.v1"]
    captured_at: AwareDatetime
    completeness: EnvironmentCompleteness
    fields: Annotated[tuple[EnvironmentField, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_field_set_and_completeness(self) -> "EnvironmentManifest":
        names = [field.name for field in self.fields]
        expected_names = set(EnvironmentFieldName)
        if len(names) != len(set(names)):
            raise ValueError("environment field names must be unique")
        if set(names) != expected_names:
            raise ValueError("environment manifest must enumerate the v1 allowlist")

        unknown_count = sum(
            field.provenance is ProvenanceKind.UNKNOWN for field in self.fields
        )
        if unknown_count == 0:
            expected_completeness = EnvironmentCompleteness.COMPLETE
        elif unknown_count == len(self.fields):
            expected_completeness = EnvironmentCompleteness.UNKNOWN
        else:
            expected_completeness = EnvironmentCompleteness.PARTIAL
        if self.completeness is not expected_completeness:
            raise ValueError("environment completeness does not match field provenance")
        return self
