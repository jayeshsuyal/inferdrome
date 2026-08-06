"""Fail-closed normalizer for vLLM bench serve 0.26.0 detailed JSON."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from typing import Annotated, Any, Final, Literal

from pydantic import Field, StringConstraints, ValidationError, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.execution import (
    ExecutionRecord,
    PhaseEvent,
    PhaseName,
    PhaseTimingStatus,
)
from inferdrome.domain.experiment import (
    AttachedVllmTarget,
    CanonicalResponseContentPolicy,
    ConcurrentTraffic,
    ExperimentSpec,
    RequestRateTraffic,
    VllmExecution,
)
from inferdrome.domain.ids import sha256_digest
from inferdrome.domain.request_plan import RequestPlan
from inferdrome.domain.request_record import (
    NativeSourceLocator,
    RequestContentEvidence,
    RequestOutcome,
    RequestRecord,
    RequestStatus,
    TimingObservations,
    TokenObservations,
    VllmProducerSemantics,
)
from inferdrome.errors import NormalizationError

VLLM_VERSION: Final[Literal["0.26.0"]] = "0.26.0"
VLLM_ADAPTER_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
VLLM_NATIVE_ARTIFACT_PATH = "native/benchmark-result.json"

NonnegativeDecimal = Annotated[
    Decimal,
    Field(ge=0, allow_inf_nan=False),
]
PositiveDecimal = Annotated[
    Decimal,
    Field(gt=0, allow_inf_nan=False),
]
BoundedNativeText = Annotated[str, StringConstraints(max_length=4_194_304)]
BoundedDiagnosticText = Annotated[str, StringConstraints(max_length=65_536)]
BoundedIdentifierText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4_096),
]


class VllmMetadataEntry(FrozenModel):
    key: Annotated[
        str,
        StringConstraints(pattern=r"^inferdrome_[a-z0-9_]+$", max_length=128),
    ]
    value: Annotated[str, StringConstraints(max_length=4_096)]


class VllmNativeResult(FrozenModel):
    date: Annotated[str, StringConstraints(pattern=r"^[0-9]{8}-[0-9]{6}$")]
    endpoint_type: Literal["openai-chat"]
    backend: Literal["openai-chat"]
    label: Annotated[str, StringConstraints(max_length=256)] | None
    model_id: BoundedIdentifierText
    tokenizer_id: BoundedIdentifierText
    num_prompts: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
    request_rate: Literal["inf"] | PositiveDecimal
    burstiness: PositiveDecimal
    max_concurrency: Annotated[int, Field(strict=True, ge=1, le=100_000)] | None
    duration: PositiveDecimal
    completed: Annotated[int, Field(strict=True, ge=0)]
    failed: Annotated[int, Field(strict=True, ge=0)]
    total_input_tokens: Annotated[int, Field(strict=True, ge=0)]
    total_output_tokens: Annotated[int, Field(strict=True, ge=0)]
    request_throughput: NonnegativeDecimal
    request_goodput: NonnegativeDecimal | None
    output_throughput: NonnegativeDecimal
    total_token_throughput: NonnegativeDecimal
    input_lens: tuple[Annotated[int, Field(strict=True, ge=0)], ...]
    output_lens: tuple[Annotated[int, Field(strict=True, ge=0)], ...]
    ttfts: tuple[NonnegativeDecimal, ...]
    itls: tuple[tuple[NonnegativeDecimal, ...], ...]
    start_times: tuple[PositiveDecimal, ...]
    generated_texts: tuple[BoundedNativeText, ...]
    errors: tuple[BoundedDiagnosticText, ...]
    max_output_tokens_per_s: NonnegativeDecimal
    max_concurrent_requests: Annotated[int, Field(strict=True, ge=0)]
    rtfx: NonnegativeDecimal
    mean_e2el_ms: NonnegativeDecimal
    median_e2el_ms: NonnegativeDecimal
    std_e2el_ms: NonnegativeDecimal
    p50_e2el_ms: NonnegativeDecimal
    p95_e2el_ms: NonnegativeDecimal
    p99_e2el_ms: NonnegativeDecimal
    metadata: tuple[VllmMetadataEntry, ...]

    @model_validator(mode="after")
    def detailed_arrays_and_populations_must_align(self) -> "VllmNativeResult":
        arrays = (
            self.input_lens,
            self.output_lens,
            self.ttfts,
            self.itls,
            self.start_times,
            self.generated_texts,
            self.errors,
        )
        if any(len(values) != self.num_prompts for values in arrays):
            raise ValueError("native detailed arrays must align with num_prompts")
        failed_indexes = tuple(
            index for index, error in enumerate(self.errors) if error
        )
        if len(failed_indexes) != self.failed:
            raise ValueError("native error rows disagree with failed count")
        if self.completed + self.failed != self.num_prompts:
            raise ValueError("native completed and failed counts do not cover rows")
        successful_indexes = tuple(
            index for index, error in enumerate(self.errors) if not error
        )
        expected_input_total = sum(
            self.input_lens[index] for index in successful_indexes
        )
        expected_output_total = sum(
            self.output_lens[index] for index in successful_indexes
        )
        if self.total_input_tokens != expected_input_total:
            raise ValueError("native successful input-token aggregate disagrees")
        if self.total_output_tokens != expected_output_total:
            raise ValueError("native successful output-token aggregate disagrees")
        origin = min(self.start_times)
        if any(start_time - origin > self.duration for start_time in self.start_times):
            raise ValueError("native request start lies outside benchmark duration")
        metadata_keys = tuple(entry.key for entry in self.metadata)
        if metadata_keys != tuple(sorted(metadata_keys)):
            raise ValueError("native metadata must use stable key order")
        if len(metadata_keys) != len(set(metadata_keys)):
            raise ValueError("native metadata keys must be unique")
        return self


_NATIVE_BASE_KEYS = frozenset(VllmNativeResult.model_fields) - {"metadata"}


@dataclass(frozen=True)
class VllmNormalizationResult:
    native_result: VllmNativeResult
    native_schema_fingerprint: str
    measurement_window_ns: int
    request_records: tuple[RequestRecord, ...]
    request_records_bytes: bytes


def _canonical_model_bytes(model: FrozenModel) -> bytes:
    return canonical_json_bytes(
        model.model_dump(mode="json", by_alias=True, exclude_none=False)
    )


def vllm_native_schema_fingerprint() -> str:
    contract = {
        "metadata_policy": "inferdrome_prefixed_string_values_v1",
        "producer": "vllm",
        "producer_version": VLLM_VERSION,
        "schema": VllmNativeResult.model_json_schema(
            mode="validation", ref_template="#/$defs/{model}"
        ),
    }
    return sha256_digest(canonical_json_bytes(contract))


def _load_native_json(content: bytes) -> VllmNativeResult:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise NormalizationError("vLLM native result is not valid UTF-8") from None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise NormalizationError("vLLM native result has duplicate keys")
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        raise NormalizationError("vLLM native result has a non-finite number")

    try:
        raw = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_float=Decimal,
            parse_constant=reject_constant,
        )
    except NormalizationError:
        raise
    except (json.JSONDecodeError, RecursionError):
        raise NormalizationError("vLLM native result is not valid JSON") from None
    if not isinstance(raw, dict):
        raise NormalizationError("vLLM native result must be one JSON object")

    raw_keys = set(raw)
    missing = _NATIVE_BASE_KEYS - raw_keys
    metadata_keys = raw_keys - _NATIVE_BASE_KEYS
    if missing:
        raise NormalizationError("vLLM native result is missing required fields")
    if any(not key.startswith("inferdrome_") for key in metadata_keys):
        raise NormalizationError("vLLM native result has an unknown structural field")
    metadata = []
    for key in sorted(metadata_keys):
        value = raw.pop(key)
        if not isinstance(value, str):
            raise NormalizationError("vLLM native metadata values must be strings")
        try:
            metadata.append(VllmMetadataEntry(key=key, value=value))
        except ValidationError:
            raise NormalizationError(
                "vLLM native metadata violates the pinned contract"
            ) from None

    for field_name in (
        "input_lens",
        "output_lens",
        "ttfts",
        "start_times",
        "generated_texts",
        "errors",
    ):
        value = raw.get(field_name)
        if isinstance(value, list):
            raw[field_name] = tuple(value)
    raw_itls = raw.get("itls")
    if isinstance(raw_itls, list):
        raw["itls"] = tuple(
            tuple(value) if isinstance(value, list) else value for value in raw_itls
        )
    raw["metadata"] = tuple(metadata)
    try:
        return VllmNativeResult.model_validate(raw)
    except ValidationError:
        raise NormalizationError(
            "vLLM native result violates the pinned structural contract"
        ) from None


def _seconds_to_nanoseconds(value: Decimal) -> int:
    try:
        with localcontext() as context:
            context.prec = max(50, len(value.as_tuple().digits) + 20)
            converted = (value * Decimal(1_000_000_000)).quantize(
                Decimal(1), rounding=ROUND_HALF_EVEN
            )
        result = int(converted)
    except (InvalidOperation, OverflowError, ValueError):
        raise NormalizationError(
            "vLLM timing is outside the canonical domain"
        ) from None
    if not 0 <= result <= 9_223_372_036_854_775_807:
        raise NormalizationError("vLLM timing is outside the canonical domain")
    return result


def _validate_expected_metadata(
    native: VllmNativeResult,
    expected_metadata: Mapping[str, str],
) -> None:
    actual = {entry.key: entry.value for entry in native.metadata}
    if actual != dict(expected_metadata):
        raise NormalizationError("vLLM metadata differs from invocation")


def _validate_configured_echoes(
    native: VllmNativeResult,
    spec: ExperimentSpec,
) -> None:
    if not isinstance(spec.target, AttachedVllmTarget):
        raise NormalizationError("vLLM normalization requires attached target")
    if native.model_id != spec.target.model:
        raise NormalizationError("vLLM native model ID differs from resolved target")
    traffic = spec.traffic
    if isinstance(traffic, ConcurrentTraffic):
        if (
            native.request_rate != "inf"
            or native.max_concurrency != traffic.concurrency
        ):
            raise NormalizationError("vLLM native concurrency controls disagree")
        if native.burstiness != Decimal(1):
            raise NormalizationError("vLLM native burstiness differs from invocation")
    elif isinstance(traffic, RequestRateTraffic):
        if native.request_rate == "inf" or native.request_rate != Decimal(
            traffic.requests_per_second
        ):
            raise NormalizationError("vLLM native request rate differs")
        if native.burstiness != Decimal(traffic.burstiness):
            raise NormalizationError("vLLM native burstiness differs")
        if native.max_concurrency != traffic.max_concurrency:
            raise NormalizationError("vLLM native concurrency limit differs")


def normalize_vllm_native(
    native_bytes: bytes,
    spec: ExperimentSpec,
    plan: RequestPlan,
    *,
    expected_metadata: Mapping[str, str],
    expected_tokenizer_id: str,
) -> VllmNormalizationResult:
    """Normalize only the exact detailed shape established for vLLM 0.26.0."""

    if not isinstance(spec.execution, VllmExecution):
        raise NormalizationError("vLLM normalization requires vLLM execution")
    if plan.experiment_id != spec.experiment.id or plan.traffic != spec.traffic:
        raise NormalizationError("vLLM request plan differs from resolved experiment")

    native = _load_native_json(native_bytes)
    _validate_expected_metadata(native, expected_metadata)
    _validate_configured_echoes(native, spec)
    if native.tokenizer_id != expected_tokenizer_id:
        raise NormalizationError("vLLM native tokenizer ID differs from invocation")
    if native.label is not None:
        raise NormalizationError("vLLM native label was not configured by Inferdrome")
    if native.num_prompts != len(plan.requests):
        raise NormalizationError("vLLM native row count differs from request plan")

    origin = min(native.start_times)
    fingerprint = vllm_native_schema_fingerprint()
    records = []
    include_content = (
        spec.evidence.canonical_response_content
        is CanonicalResponseContentPolicy.INCLUDE
    )
    for index, planned in enumerate(plan.requests):
        error = native.errors[index]
        ttft_seconds = native.ttfts[index]
        itl_seconds = native.itls[index]
        generated_text = native.generated_texts[index]
        output_tokens = native.output_lens[index]

        if error:
            status = RequestStatus.FAILED
            ttft_ns = (
                _seconds_to_nanoseconds(ttft_seconds)
                if ttft_seconds > 0
                else None
            )
        elif ttft_seconds > 0:
            status = RequestStatus.SUCCESS
            ttft_ns = _seconds_to_nanoseconds(ttft_seconds)
        elif not itl_seconds and output_tokens == 0 and generated_text == "":
            status = RequestStatus.ANOMALOUS_EMPTY_STREAM
            ttft_ns = None
        else:
            raise NormalizationError("vLLM row has ambiguous success semantics")

        itl_ns = tuple(_seconds_to_nanoseconds(value) for value in itl_seconds)
        if ttft_ns is None and itl_ns:
            raise NormalizationError("vLLM row has ITLs without a first choices event")
        response_digest = sha256_digest(generated_text.encode("utf-8"))
        records.append(
            RequestRecord(
                schema_version="inferdrome.request-record.v1",
                run_id=plan.run_id,
                request_id=planned.request_id,
                producer_request_id=planned.producer_request_id,
                sequence_index=index,
                native_source=NativeSourceLocator(
                    artifact_path=VLLM_NATIVE_ARTIFACT_PATH,
                    array_index=index,
                ),
                producer=VllmProducerSemantics(
                    producer_name="vllm",
                    producer_version=VLLM_VERSION,
                    adapter_name="vllm_bench_serve",
                    adapter_version=VLLM_ADAPTER_VERSION,
                    native_schema_fingerprint=fingerprint,
                    request_id_derivation="frozen_order_and_prefix_v1",
                ),
                tokens=TokenObservations(
                    input_tokens=native.input_lens[index],
                    output_tokens=output_tokens,
                    input_tokens_semantics="vllm_output_prompt_len_v0_26",
                    output_tokens_semantics="vllm_output_len_v0_26",
                ),
                timing=TimingObservations(
                    clock_domain="producer_monotonic_normalized_v1",
                    start_offset_ns=_seconds_to_nanoseconds(
                        native.start_times[index] - origin
                    ),
                    ttft_ns=ttft_ns,
                    ttft_definition="vllm_first_choices_event_v0_26",
                    itl_ns=itl_ns,
                    itl_definition=(
                        "vllm_subsequent_choices_event_interval_v0_26"
                    ),
                ),
                outcome=RequestOutcome(
                    status=status,
                    producer_error=error or None,
                ),
                content=RequestContentEvidence(
                    prompt_sha256=planned.prompt.sha256,
                    response_sha256=response_digest,
                    canonical_response_content=(
                        generated_text if include_content else None
                    ),
                    native_response_content_present=True,
                ),
            )
        )

    record_tuple = tuple(records)
    records_bytes = b"".join(
        _canonical_model_bytes(record) + b"\n" for record in record_tuple
    )
    return VllmNormalizationResult(
        native_result=native,
        native_schema_fingerprint=fingerprint,
        measurement_window_ns=_seconds_to_nanoseconds(native.duration),
        request_records=record_tuple,
        request_records_bytes=records_bytes,
    )


def build_vllm_execution_record(
    normalization: VllmNormalizationResult,
    plan: RequestPlan,
    native_bytes: bytes,
    *,
    started_at: datetime,
    ended_at: datetime,
    producer_exit_status: int,
) -> ExecutionRecord:
    """Build aggregate execution evidence without inventing phase boundaries."""

    return ExecutionRecord(
        schema_version="inferdrome.execution.v1",
        run_id=plan.run_id,
        terminal_state="COMPLETE",
        started_at=started_at,
        ended_at=ended_at,
        monotonic_clock_domain_id=f"vllm-bench-serve-{plan.run_id}",
        configured_traffic=plan.traffic,
        measurement_window_ns=normalization.measurement_window_ns,
        measurement_window_definition="vllm_benchmark_duration_v0_26",
        producer_exit_status=producer_exit_status,
        native_result_sha256=sha256_digest(native_bytes),
        phases=(
            PhaseEvent(
                phase=PhaseName.PREFLIGHT,
                timing_status=PhaseTimingStatus.UNAVAILABLE,
                started_at=None,
                ended_at=None,
                configured_request_count=1,
            ),
            PhaseEvent(
                phase=PhaseName.WARMUP,
                timing_status=PhaseTimingStatus.UNAVAILABLE,
                started_at=None,
                ended_at=None,
                configured_request_count=plan.traffic.warmup_requests,
            ),
            PhaseEvent(
                phase=PhaseName.MEASURING,
                timing_status=PhaseTimingStatus.UNAVAILABLE,
                started_at=None,
                ended_at=None,
                configured_request_count=plan.traffic.measured_requests,
            ),
            PhaseEvent(
                phase=PhaseName.FINALIZING,
                timing_status=PhaseTimingStatus.UNAVAILABLE,
                started_at=None,
                ended_at=None,
                configured_request_count=None,
            ),
        ),
    )
