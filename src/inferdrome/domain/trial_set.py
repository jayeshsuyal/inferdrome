"""Public contract for a descriptive repeated-trial grouping."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.ids import (
    ExperimentId,
    RunId,
    SemanticVersion,
    Sha256Digest,
    TrialSetId,
)


class TrialSetMember(FrozenModel):
    repetition_index: Annotated[int, Field(strict=True, ge=0, le=99)]
    run_id: RunId
    bundle_digest: Sha256Digest


class TrialSet(FrozenModel):
    """Immutable links to same-configuration runs; request populations stay separate."""

    schema_version: Literal["inferdrome.trial-set.v1"]
    trial_set_id: TrialSetId
    experiment_id: ExperimentId
    title: Annotated[str, Field(min_length=1, max_length=256)]
    hypothesis: Annotated[str, Field(min_length=1, max_length=4096)] | None
    created_at: AwareDatetime
    design_status: Literal["RETROSPECTIVE"]
    membership_policy: Literal["same_execution_fingerprint_v1"]
    request_population_policy: Literal["separate_per_run_v1"]
    statistical_unit: Literal["run"]
    weighting: Literal["equal_per_run"]
    execution_fingerprint: Sha256Digest
    metric_definitions_digest: Sha256Digest
    reducer_version: SemanticVersion
    members: Annotated[tuple[TrialSetMember, ...], Field(min_length=2, max_length=100)]

    @model_validator(mode="after")
    def validate_ordered_unique_members(self) -> "TrialSet":
        expected_indices = tuple(range(len(self.members)))
        actual_indices = tuple(member.repetition_index for member in self.members)
        if actual_indices != expected_indices:
            raise ValueError(
                "trial-set repetition indices must be contiguous and ordered"
            )
        run_ids = tuple(member.run_id for member in self.members)
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("trial-set run IDs must be unique")
        bundle_digests = tuple(member.bundle_digest for member in self.members)
        if len(set(bundle_digests)) != len(bundle_digests):
            raise ValueError("trial-set bundle digests must be unique")
        return self
