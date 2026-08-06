"""Deterministic synthetic producer for golden and reducer tests."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.execution import (
    ExecutionRecord,
    PhaseEvent,
    PhaseName,
    PhaseTimingStatus,
)
from inferdrome.domain.experiment import (
    CanonicalResponseContentPolicy,
    ExperimentSpec,
    FakeExecution,
    SyntheticTarget,
)
from inferdrome.domain.ids import (
    ProducerRequestId,
    RequestId,
    RunId,
    Sha256Digest,
    sha256_digest,
)
from inferdrome.domain.request_plan import RequestPlan
from inferdrome.domain.request_record import (
    FakeProducerSemantics,
    NativeSourceLocator,
    RequestContentEvidence,
    RequestOutcome,
    RequestRecord,
    RequestStatus,
    TimingObservations,
    TokenObservations,
)
from inferdrome.errors import AdapterError

FAKE_PRODUCER_VERSION: Literal["1.0.0"] = "1.0.0"
FAKE_ADAPTER_VERSION: Literal["1.0.0"] = "1.0.0"
FAKE_NATIVE_ARTIFACT_PATH = "native/benchmark-result.json"


class FakeObservation(FrozenModel):
    status: RequestStatus
    input_tokens: Annotated[int, Field(strict=True, ge=0)]
    output_tokens: Annotated[int, Field(strict=True, ge=0)]
    start_offset_ns: Annotated[int, Field(strict=True, ge=0)]
    ttft_ns: Annotated[int, Field(strict=True, ge=0)] | None
    itl_ns: tuple[Annotated[int, Field(strict=True, ge=0)], ...]
    response_content: Annotated[str, Field(max_length=4_194_304)] | None
    producer_error: Annotated[str, Field(min_length=1, max_length=65_536)] | None

    @model_validator(mode="after")
    def outcome_is_internally_consistent(self) -> "FakeObservation":
        if self.ttft_ns is None and self.itl_ns:
            raise ValueError("synthetic ITLs require an observed TTFT")
        if self.status is RequestStatus.SUCCESS:
            if self.ttft_ns is None or self.response_content is None:
                raise ValueError("synthetic success requires timing and response")
            if self.producer_error is not None:
                raise ValueError("synthetic success cannot carry an error")
        elif self.status is RequestStatus.FAILED:
            if self.producer_error is None:
                raise ValueError("synthetic failure requires error text")
        else:
            if (
                self.ttft_ns is not None
                or self.itl_ns
                or self.output_tokens != 0
                or self.response_content != ""
                or self.producer_error is not None
            ):
                raise ValueError("synthetic empty-stream anomaly is inconsistent")
        return self


class FakeNativeRow(FrozenModel):
    request_id: RequestId
    producer_request_id: ProducerRequestId
    prompt_sha256: Sha256Digest
    status: RequestStatus
    input_tokens: Annotated[int, Field(strict=True, ge=0)]
    output_tokens: Annotated[int, Field(strict=True, ge=0)]
    start_offset_ns: Annotated[int, Field(strict=True, ge=0)]
    ttft_ns: Annotated[int, Field(strict=True, ge=0)] | None
    itl_ns: tuple[Annotated[int, Field(strict=True, ge=0)], ...]
    response_content: str | None
    producer_error: str | None


class FakeNativeResult(FrozenModel):
    schema_version: Literal["inferdrome.fake-native.v1"]
    run_id: RunId
    execution_mode: Literal["synthetic_fixture"]
    evidence_eligibility: Literal["SYNTHETIC_ONLY"]
    producer_name: Literal["inferdrome_fake"]
    producer_version: Literal["1.0.0"]
    adapter_version: Literal["1.0.0"]
    rows: Annotated[tuple[FakeNativeRow, ...], Field(min_length=1)]


@dataclass(frozen=True)
class FakeRunResult:
    native_result: FakeNativeResult
    native_result_bytes: bytes
    native_schema_fingerprint: str
    request_records: tuple[RequestRecord, ...]
    request_records_bytes: bytes
    execution: ExecutionRecord
    execution_bytes: bytes


def _canonical_model_bytes(model: FrozenModel) -> bytes:
    return canonical_json_bytes(
        model.model_dump(mode="json", by_alias=True, exclude_none=False)
    )


def fake_native_schema_fingerprint() -> str:
    schema = FakeNativeResult.model_json_schema(
        mode="validation", ref_template="#/$defs/{model}"
    )
    return sha256_digest(canonical_json_bytes(schema))


def _default_observations(request_count: int) -> tuple[FakeObservation, ...]:
    return tuple(
        FakeObservation(
            status=RequestStatus.SUCCESS,
            input_tokens=4 + index,
            output_tokens=2,
            start_offset_ns=index * 100_000_000,
            ttft_ns=10_000_000 + index * 1_000_000,
            itl_ns=(20_000_000,),
            response_content=f"fake response {index}",
            producer_error=None,
        )
        for index in range(request_count)
    )


def _validate_adapter_inputs(spec: ExperimentSpec, plan: RequestPlan) -> None:
    if not isinstance(spec.execution, FakeExecution) or not isinstance(
        spec.target, SyntheticTarget
    ):
        raise AdapterError("fake adapter requires a synthetic resolved experiment")
    if plan.experiment_id != spec.experiment.id:
        raise AdapterError("request plan belongs to a different experiment")
    if plan.traffic != spec.traffic:
        raise AdapterError("request plan traffic differs from resolved experiment")


class FakeAdapter:
    """Produce deterministic synthetic native and canonical observations."""

    def execute(
        self,
        spec: ExperimentSpec,
        plan: RequestPlan,
        *,
        observations: tuple[FakeObservation, ...] | None = None,
        started_at: datetime | None = None,
        measurement_window_ns: int | None = None,
    ) -> FakeRunResult:
        _validate_adapter_inputs(spec, plan)
        if measurement_window_ns is not None and (
            isinstance(measurement_window_ns, bool)
            or not isinstance(measurement_window_ns, int)
            or measurement_window_ns <= 0
        ):
            raise AdapterError("fake measurement window must be a positive integer")
        selected_observations = observations or _default_observations(
            len(plan.requests)
        )
        if len(selected_observations) != len(plan.requests):
            raise AdapterError("fake observation count must match the request plan")
        required_window_ns = max(
            observation.start_offset_ns
            + (observation.ttft_ns or 0)
            + sum(observation.itl_ns)
            for observation in selected_observations
        )
        selected_window_ns = measurement_window_ns or max(
            1_000_000_000, required_window_ns
        )

        start_time = started_at or datetime.now(UTC)
        if start_time.tzinfo is None or start_time.utcoffset() is None:
            raise AdapterError("fake run timestamp must include a UTC offset")

        fingerprint = fake_native_schema_fingerprint()
        native_rows = []
        records = []
        include_canonical_response = (
            spec.evidence.canonical_response_content
            is CanonicalResponseContentPolicy.INCLUDE
        )
        for planned, observation in zip(
            plan.requests, selected_observations, strict=True
        ):
            last_choices_offset = observation.start_offset_ns
            if observation.ttft_ns is not None:
                last_choices_offset += observation.ttft_ns + sum(observation.itl_ns)
            if last_choices_offset > selected_window_ns:
                raise AdapterError("synthetic observation exceeds measurement window")

            native_row = FakeNativeRow(
                request_id=planned.request_id,
                producer_request_id=planned.producer_request_id,
                prompt_sha256=planned.prompt.sha256,
                status=observation.status,
                input_tokens=observation.input_tokens,
                output_tokens=observation.output_tokens,
                start_offset_ns=observation.start_offset_ns,
                ttft_ns=observation.ttft_ns,
                itl_ns=observation.itl_ns,
                response_content=observation.response_content,
                producer_error=observation.producer_error,
            )
            native_rows.append(native_row)

            response_digest = (
                sha256_digest(observation.response_content.encode("utf-8"))
                if observation.response_content is not None
                else None
            )
            records.append(
                RequestRecord(
                    schema_version="inferdrome.request-record.v1",
                    run_id=plan.run_id,
                    request_id=planned.request_id,
                    producer_request_id=planned.producer_request_id,
                    sequence_index=planned.sequence_index,
                    native_source=NativeSourceLocator(
                        artifact_path=FAKE_NATIVE_ARTIFACT_PATH,
                        array_index=planned.sequence_index,
                    ),
                    producer=FakeProducerSemantics(
                        producer_name="inferdrome_fake",
                        producer_version=FAKE_PRODUCER_VERSION,
                        adapter_name="fake",
                        adapter_version=FAKE_ADAPTER_VERSION,
                        native_schema_fingerprint=fingerprint,
                        request_id_derivation="frozen_order_and_prefix_v1",
                    ),
                    tokens=TokenObservations(
                        input_tokens=observation.input_tokens,
                        output_tokens=observation.output_tokens,
                        input_tokens_semantics="vllm_output_prompt_len_v0_26",
                        output_tokens_semantics="vllm_output_len_v0_26",
                    ),
                    timing=TimingObservations(
                        clock_domain="producer_monotonic_normalized_v1",
                        start_offset_ns=observation.start_offset_ns,
                        ttft_ns=observation.ttft_ns,
                        ttft_definition="vllm_first_choices_event_v0_26",
                        itl_ns=observation.itl_ns,
                        itl_definition=(
                            "vllm_subsequent_choices_event_interval_v0_26"
                        ),
                    ),
                    outcome=RequestOutcome(
                        status=observation.status,
                        producer_error=observation.producer_error,
                    ),
                    content=RequestContentEvidence(
                        prompt_sha256=planned.prompt.sha256,
                        response_sha256=response_digest,
                        canonical_response_content=(
                            observation.response_content
                            if include_canonical_response
                            else None
                        ),
                        native_response_content_present=True,
                    ),
                )
            )

        native = FakeNativeResult(
            schema_version="inferdrome.fake-native.v1",
            run_id=plan.run_id,
            execution_mode="synthetic_fixture",
            evidence_eligibility="SYNTHETIC_ONLY",
            producer_name="inferdrome_fake",
            producer_version=FAKE_PRODUCER_VERSION,
            adapter_version=FAKE_ADAPTER_VERSION,
            rows=tuple(native_rows),
        )
        native_bytes = _canonical_model_bytes(native)
        record_tuple = tuple(records)
        records_bytes = b"".join(
            _canonical_model_bytes(record) + b"\n" for record in record_tuple
        )

        preflight_start = start_time
        preflight_end = preflight_start + timedelta(milliseconds=1)
        warmup_start = preflight_end
        warmup_end = warmup_start + timedelta(milliseconds=1)
        measuring_start = warmup_end
        measuring_end = measuring_start + timedelta(
            microseconds=(selected_window_ns + 999) // 1_000
        )
        finalizing_start = measuring_end
        finalizing_end = finalizing_start + timedelta(milliseconds=1)
        execution = ExecutionRecord(
            schema_version="inferdrome.execution.v1",
            run_id=plan.run_id,
            terminal_state="COMPLETE",
            started_at=preflight_start,
            ended_at=finalizing_end,
            monotonic_clock_domain_id=f"fake-{plan.run_id}",
            configured_traffic=plan.traffic,
            measurement_window_ns=selected_window_ns,
            measurement_window_definition="fake_measurement_window_v1",
            producer_exit_status=0,
            native_result_sha256=sha256_digest(native_bytes),
            phases=(
                PhaseEvent(
                    phase=PhaseName.PREFLIGHT,
                    timing_status=PhaseTimingStatus.OBSERVED,
                    started_at=preflight_start,
                    ended_at=preflight_end,
                    configured_request_count=1,
                ),
                PhaseEvent(
                    phase=PhaseName.WARMUP,
                    timing_status=PhaseTimingStatus.OBSERVED,
                    started_at=warmup_start,
                    ended_at=warmup_end,
                    configured_request_count=plan.traffic.warmup_requests,
                ),
                PhaseEvent(
                    phase=PhaseName.MEASURING,
                    timing_status=PhaseTimingStatus.OBSERVED,
                    started_at=measuring_start,
                    ended_at=measuring_end,
                    configured_request_count=plan.traffic.measured_requests,
                ),
                PhaseEvent(
                    phase=PhaseName.FINALIZING,
                    timing_status=PhaseTimingStatus.OBSERVED,
                    started_at=finalizing_start,
                    ended_at=finalizing_end,
                    configured_request_count=0,
                ),
            ),
        )
        return FakeRunResult(
            native_result=native,
            native_result_bytes=native_bytes,
            native_schema_fingerprint=fingerprint,
            request_records=record_tuple,
            request_records_bytes=records_bytes,
            execution=execution,
            execution_bytes=_canonical_model_bytes(execution),
        )
