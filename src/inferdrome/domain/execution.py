"""Run execution and aggregate phase evidence."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.experiment import TrafficConfig
from inferdrome.domain.ids import OpaqueName, RunId, Sha256Digest


class PhaseName(StrEnum):
    PREFLIGHT = "PREFLIGHT"
    WARMUP = "WARMUP"
    MEASURING = "MEASURING"
    FINALIZING = "FINALIZING"


class PhaseTimingStatus(StrEnum):
    OBSERVED = "OBSERVED"
    UNAVAILABLE = "UNAVAILABLE"


class PhaseEvent(FrozenModel):
    phase: PhaseName
    timing_status: PhaseTimingStatus
    started_at: AwareDatetime | None
    ended_at: AwareDatetime | None
    configured_request_count: Annotated[int, Field(strict=True, ge=0)] | None

    @model_validator(mode="after")
    def timing_fields_must_match_status(self) -> "PhaseEvent":
        if self.timing_status is PhaseTimingStatus.OBSERVED:
            if self.started_at is None or self.ended_at is None:
                raise ValueError("observed phase timing requires both boundaries")
            if self.ended_at < self.started_at:
                raise ValueError("phase end precedes phase start")
        elif self.started_at is not None or self.ended_at is not None:
            raise ValueError("unavailable phase timing cannot carry boundaries")
        return self


class ExecutionRecord(FrozenModel):
    schema_version: Literal["inferdrome.execution.v1"]
    run_id: RunId
    terminal_state: Literal["COMPLETE"]
    started_at: AwareDatetime
    ended_at: AwareDatetime
    monotonic_clock_domain_id: OpaqueName
    configured_traffic: TrafficConfig
    measurement_window_ns: Annotated[int, Field(strict=True, gt=0)]
    measurement_window_definition: Literal[
        "vllm_benchmark_duration_v0_26", "fake_measurement_window_v1"
    ]
    producer_exit_status: Annotated[int, Field(strict=True, ge=0, le=255)]
    native_result_sha256: Sha256Digest
    phases: Annotated[tuple[PhaseEvent, ...], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def validate_execution_lifecycle(self) -> "ExecutionRecord":
        if self.ended_at < self.started_at:
            raise ValueError("execution end precedes start")

        expected_phases = [
            PhaseName.PREFLIGHT,
            PhaseName.WARMUP,
            PhaseName.MEASURING,
            PhaseName.FINALIZING,
        ]
        if [phase.phase for phase in self.phases] != expected_phases:
            raise ValueError("phase events must be complete and ordered")
        for phase in self.phases:
            if phase.started_at is not None and phase.ended_at is not None and (
                phase.started_at < self.started_at or phase.ended_at > self.ended_at
            ):
                raise ValueError("observed phase lies outside execution boundaries")
        for previous, current in zip(self.phases, self.phases[1:], strict=False):
            if (
                current.started_at is not None
                and previous.ended_at is not None
                and current.started_at < previous.ended_at
            ):
                raise ValueError("phase events must not overlap")

        if self.producer_exit_status != 0:
            raise ValueError("complete execution requires producer exit status zero")
        return self
