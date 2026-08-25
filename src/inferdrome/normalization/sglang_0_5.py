"""Additive normalization for the pinned SGLang 0.5.18 detailed output.

SGLang's persisted detailed output has no request IDs or request start
offsets.  This module therefore exposes only native array-index observations
and deliberately cannot produce v0.1 request records, execution records, or
eligible evidence.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from typing import Annotated, Any, Final, Literal

from pydantic import ConfigDict, Field, ValidationError, model_validator

from inferdrome.adapters.sglang import (
    SGLANG_BACKEND,
    SGLANG_BENCHMARK_MODULE,
    SGLANG_FIXED_TAG,
    SGLANG_RELEASE_COMMIT,
    SGLANG_SOURCE_REPOSITORY,
    SGLANG_VERSION,
    SglangInvocation,
    validate_sglang_invocation,
)
from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.ids import Sha256Digest, sha256_digest
from inferdrome.errors import AdapterError, NormalizationError


class _SglangModel(FrozenModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )


SGLANG_NORMALIZATION_SCHEMA_VERSION: Final[
    Literal["inferdrome.sglang-normalization.v1"]
] = "inferdrome.sglang-normalization.v1"
SGLANG_NORMALIZATION_SCHEMA_ID: Final = "urn:inferdrome:sglang-normalization:v1"
SGLANG_NATIVE_SCHEMA_VERSION: Final[Literal["sglang-detailed-output-v0.5.18"]] = (
    "sglang-detailed-output-v0.5.18"
)

_MAX_NATIVE_BYTES = 16_777_216
_MAX_SERVER_INFO_BYTES = 262_144
_MAX_SERVER_INFO_DEPTH = 12
_MAX_SERVER_INFO_NODES = 4_096
_MAX_SERVER_INFO_STRING = 16_384
_MAX_SERVER_INFO_INTEGER = 9_007_199_254_740_991
_MAX_DIAGNOSTIC_STRING = 4_096
_MAX_ITL_COUNT = 32_768
_MAX_CANONICAL_INTEGER = 9_007_199_254_740_991
_MAX_NS = _MAX_CANONICAL_INTEGER
_MAX_ATTEMPTS = 10_000
_MAX_TOTAL_TOKENS = 327_680_000
_MAX_REPORT_BYTES = 2_097_152


NonnegativeSeconds = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
NonnegativeNumber = Annotated[
    Decimal,
    Field(ge=0, le=Decimal("1e18"), allow_inf_nan=False),
]
BoundedError = Annotated[str, Field(min_length=1, max_length=_MAX_DIAGNOSTIC_STRING)]
BoundedResponse = Annotated[str, Field(max_length=4_194_304)]


class SglangNativeDetailedResult(_SglangModel):
    """The exact v0.5.18 result-writer field set consumed by this adapter."""

    tag: Annotated[str, Field(max_length=256)] | None
    backend: Literal["sglang"]
    dataset_name: Literal["random-ids"]
    request_rate: Annotated[Decimal, Field(strict=True, gt=0, le=Decimal("1e5"))]
    max_concurrency: Annotated[int, Field(strict=True, ge=1, le=1_024)] | None
    sharegpt_output_len: Annotated[int, Field(strict=True, ge=0, le=32_768)] | None
    random_input_len: Annotated[int, Field(strict=True, ge=1, le=32_768)]
    random_output_len: Annotated[int, Field(strict=True, ge=1, le=32_768)]
    random_range_ratio: Annotated[Decimal, Field(strict=True, ge=0, le=1)]
    total_input_tokens: Annotated[
        int, Field(strict=True, ge=0, le=_MAX_TOTAL_TOKENS)
    ]
    total_output_tokens: Annotated[
        int, Field(strict=True, ge=0, le=_MAX_TOTAL_TOKENS)
    ]
    total_input_text_tokens: Annotated[
        int, Field(strict=True, ge=0, le=_MAX_TOTAL_TOKENS)
    ]
    total_input_vision_tokens: Annotated[
        int, Field(strict=True, ge=0, le=_MAX_TOTAL_TOKENS)
    ]
    total_output_tokens_retokenized: Annotated[
        int, Field(strict=True, ge=0, le=_MAX_TOTAL_TOKENS)
    ]
    duration: NonnegativeNumber
    request_throughput: NonnegativeNumber
    input_throughput: NonnegativeNumber
    output_throughput: NonnegativeNumber
    total_throughput: NonnegativeNumber
    mean_e2e_latency_ms: NonnegativeNumber
    median_e2e_latency_ms: NonnegativeNumber
    std_e2e_latency_ms: NonnegativeNumber
    p90_e2e_latency_ms: NonnegativeNumber
    p95_e2e_latency_ms: NonnegativeNumber
    p99_e2e_latency_ms: NonnegativeNumber
    mean_ttft_ms: NonnegativeNumber
    median_ttft_ms: NonnegativeNumber
    std_ttft_ms: NonnegativeNumber
    p90_ttft_ms: NonnegativeNumber
    p95_ttft_ms: NonnegativeNumber
    p99_ttft_ms: NonnegativeNumber
    mean_tpot_ms: NonnegativeNumber
    median_tpot_ms: NonnegativeNumber
    std_tpot_ms: NonnegativeNumber
    p90_tpot_ms: NonnegativeNumber
    p95_tpot_ms: NonnegativeNumber
    p99_tpot_ms: NonnegativeNumber
    mean_itl_ms: NonnegativeNumber
    median_itl_ms: NonnegativeNumber
    std_itl_ms: NonnegativeNumber
    p90_itl_ms: NonnegativeNumber
    p95_itl_ms: NonnegativeNumber
    p99_itl_ms: NonnegativeNumber
    completed: Annotated[int, Field(strict=True, ge=0, le=_MAX_ATTEMPTS)]
    concurrency: Annotated[Decimal, Field(strict=True, ge=0, le=1_024)]
    accept_length: Annotated[Decimal, Field(strict=True, ge=0)] | None
    max_output_tokens_per_s: NonnegativeNumber
    max_concurrent_requests: Annotated[int, Field(strict=True, ge=0, le=1_024)]
    input_lens: tuple[
        Annotated[int, Field(strict=True, ge=0, le=32_768)], ...
    ]
    output_lens: tuple[
        Annotated[int, Field(strict=True, ge=0, le=32_768)], ...
    ]
    ttfts: tuple[NonnegativeSeconds, ...]
    itls: tuple[tuple[NonnegativeSeconds, ...], ...]
    generated_texts: tuple[BoundedResponse, ...]
    errors: tuple[Annotated[str, Field(max_length=_MAX_DIAGNOSTIC_STRING)], ...]
    server_info_present: bool
    server_info_sha256: Sha256Digest | None

    @model_validator(mode="after")
    def validate_population_and_timings(self) -> SglangNativeDetailedResult:
        arrays = (
            self.input_lens,
            self.output_lens,
            self.ttfts,
            self.itls,
            self.generated_texts,
            self.errors,
        )
        attempted = len(self.input_lens)
        if (
            attempted < 1
            or attempted > _MAX_ATTEMPTS
            or any(len(values) != attempted for values in arrays)
        ):
            raise ValueError("detailed arrays do not align")
        successful = tuple(
            index for index, error in enumerate(self.errors) if not error
        )
        failed = tuple(index for index, error in enumerate(self.errors) if error)
        if (
            len(successful) != self.completed
            or len(failed) != attempted - self.completed
        ):
            raise ValueError("error rows do not reconcile with completed count")
        if any(self.ttfts[index] <= 0 for index in successful):
            raise ValueError("successful streaming rows require positive TTFT")
        if any(self.ttfts[index] != Decimal(0) or self.itls[index] for index in failed):
            raise ValueError("failed rows must use the native zero-timing sentinel")
        if any(
            self.output_lens[index] != 0
            or self.generated_texts[index] != ""
            for index in failed
        ):
            raise ValueError("failed rows must use the native empty-output sentinel")
        for index, intervals in enumerate(self.itls):
            if len(intervals) > min(self.output_lens[index], _MAX_ITL_COUNT):
                raise ValueError("native ITL count exceeds output-token bound")
        if (
            self.max_concurrency is not None
            and self.max_concurrent_requests > self.max_concurrency
        ):
            raise ValueError(
                "native concurrency aggregates exceed the configured bound"
            )
        if self.random_range_ratio == Decimal(0) and any(
            input_tokens != self.random_input_len for input_tokens in self.input_lens
        ):
            raise ValueError(
                "random-ids input lengths do not match the fixed length"
            )
        expected_input = sum(self.input_lens[index] for index in successful)
        expected_output = sum(self.output_lens[index] for index in successful)
        if (
            self.total_input_text_tokens + self.total_input_vision_tokens
            != self.total_input_tokens
        ):
            raise ValueError("native input token aggregates do not reconcile")
        if self.total_input_tokens != expected_input:
            raise ValueError("successful input-token aggregate does not reconcile")
        if self.total_output_tokens != expected_output:
            raise ValueError("successful output-token aggregate does not reconcile")
        return self


class SglangCapabilityStates(_SglangModel):
    native_detailed_metrics: Literal["SUPPORTED"]
    native_response_content: Literal["OBSERVED_BUT_OMITTED"]
    request_plan_binding: Literal["UNAVAILABLE"]
    request_start_offsets: Literal["UNAVAILABLE"]
    canonical_request_record_v1: Literal["UNSUPPORTED"]
    acceptance_verdict: Literal["NOT_OWNED"]


class SglangNormalizedRow(_SglangModel):
    native_index: Annotated[int, Field(strict=True, ge=0, le=10_000)]
    success: bool
    input_tokens: Annotated[int, Field(strict=True, ge=0, le=32_768)]
    output_tokens: Annotated[int, Field(strict=True, ge=0, le=32_768)]
    ttft_ns: Annotated[int, Field(strict=True, ge=1, le=_MAX_NS)] | None
    itl_ns: tuple[Annotated[int, Field(strict=True, ge=0, le=_MAX_NS)], ...]
    response_sha256: Sha256Digest | None

    @model_validator(mode="after")
    def validate_row(self) -> SglangNormalizedRow:
        if self.success and self.ttft_ns is None:
            raise ValueError("successful normalized rows require TTFT")
        if not self.success and (
            self.ttft_ns is not None or self.response_sha256 is not None
        ):
            raise ValueError(
                "failed normalized rows cannot contain success observations"
            )
        if len(self.itl_ns) > self.output_tokens:
            raise ValueError("normalized ITL count exceeds output-token count")
        if self.ttft_ns is None and self.itl_ns:
            raise ValueError("ITL requires a normalized TTFT")
        return self


class SglangNormalizationReport(_SglangModel):
    """Additive report; it is never a v0.1 evidence or acceptance artifact."""

    schema_version: Literal["inferdrome.sglang-normalization.v1"]
    producer_name: Literal["sglang"]
    producer_version: Literal["0.5.18"]
    release_commit: Literal["71de97b264b04dcd514cf904003028aefe9775c8"]
    source_repository: Literal["https://github.com/sgl-project/sglang"]
    benchmark_module: Literal["sglang.benchmark.serving"]
    backend: Literal["sglang"]
    streaming: Literal[True]
    invocation_sha256: Sha256Digest
    native_result_sha256: Sha256Digest
    native_schema_fingerprint: Sha256Digest
    server_info_present: bool
    server_info_sha256: Sha256Digest | None
    attempted_request_count: Annotated[int, Field(strict=True, ge=1, le=_MAX_ATTEMPTS)]
    completed_request_count: Annotated[
        int, Field(strict=True, ge=0, le=_MAX_ATTEMPTS)
    ]
    failed_request_count: Annotated[int, Field(strict=True, ge=0, le=_MAX_ATTEMPTS)]
    total_input_tokens: Annotated[
        int, Field(strict=True, ge=0, le=_MAX_TOTAL_TOKENS)
    ]
    total_output_tokens: Annotated[
        int, Field(strict=True, ge=0, le=_MAX_TOTAL_TOKENS)
    ]
    synthetic_only: bool
    evidence_eligible: Literal[False]
    request_plan_binding: Literal["UNAVAILABLE"]
    request_start_offsets: Literal["UNAVAILABLE"]
    canonical_request_record_v1: Literal["UNSUPPORTED"]
    acceptance_verdict: Literal["NOT_OWNED"]
    capability_states: SglangCapabilityStates
    rows: tuple[SglangNormalizedRow, ...]

    @model_validator(mode="after")
    def validate_report(self) -> SglangNormalizationReport:
        if len(self.rows) != self.attempted_request_count:
            raise ValueError("normalized row count does not reconcile")
        if self.completed_request_count + self.failed_request_count != len(self.rows):
            raise ValueError("normalized population does not reconcile")
        if sum(row.success for row in self.rows) != self.completed_request_count:
            raise ValueError("normalized success population does not reconcile")
        if (
            sum(row.input_tokens for row in self.rows if row.success)
            != self.total_input_tokens
        ):
            raise ValueError("normalized input aggregate does not reconcile")
        if (
            sum(row.output_tokens for row in self.rows if row.success)
            != self.total_output_tokens
        ):
            raise ValueError("normalized output aggregate does not reconcile")
        if self.server_info_present != (self.server_info_sha256 is not None):
            raise ValueError("server-info presence and digest disagree")
        return self


@dataclass(frozen=True)
class SglangNormalizationResult:
    native_result: SglangNativeDetailedResult
    report: SglangNormalizationReport
    report_bytes: bytes


_NATIVE_REQUIRED_FIELDS = frozenset(
    {
        "accept_length",
        "backend",
        "completed",
        "concurrency",
        "dataset_name",
        "duration",
        "errors",
        "generated_texts",
        "input_lens",
        "itls",
        "input_throughput",
        "max_concurrent_requests",
        "max_concurrency",
        "max_output_tokens_per_s",
        "mean_e2e_latency_ms",
        "mean_itl_ms",
        "mean_tpot_ms",
        "mean_ttft_ms",
        "median_e2e_latency_ms",
        "median_itl_ms",
        "median_tpot_ms",
        "median_ttft_ms",
        "output_lens",
        "output_throughput",
        "p90_e2e_latency_ms",
        "p90_itl_ms",
        "p90_tpot_ms",
        "p90_ttft_ms",
        "p95_e2e_latency_ms",
        "p95_itl_ms",
        "p95_tpot_ms",
        "p95_ttft_ms",
        "p99_e2e_latency_ms",
        "p99_itl_ms",
        "p99_tpot_ms",
        "p99_ttft_ms",
        "random_input_len",
        "random_output_len",
        "random_range_ratio",
        "request_rate",
        "request_throughput",
        "server_info",
        "sharegpt_output_len",
        "std_e2e_latency_ms",
        "std_itl_ms",
        "std_tpot_ms",
        "std_ttft_ms",
        "tag",
        "total_input_text_tokens",
        "total_input_tokens",
        "total_input_vision_tokens",
        "total_output_tokens",
        "total_output_tokens_retokenized",
        "total_throughput",
        "ttfts",
    }
)
_NATIVE_OPTIONAL_FIELDS: Final[frozenset[str]] = frozenset()
_NATIVE_DECIMAL_FIELDS = (
    "request_rate",
    "random_range_ratio",
    "duration",
    "request_throughput",
    "input_throughput",
    "output_throughput",
    "total_throughput",
    "mean_e2e_latency_ms",
    "median_e2e_latency_ms",
    "std_e2e_latency_ms",
    "p90_e2e_latency_ms",
    "p95_e2e_latency_ms",
    "p99_e2e_latency_ms",
    "mean_ttft_ms",
    "median_ttft_ms",
    "std_ttft_ms",
    "p90_ttft_ms",
    "p95_ttft_ms",
    "p99_ttft_ms",
    "mean_tpot_ms",
    "median_tpot_ms",
    "std_tpot_ms",
    "p90_tpot_ms",
    "p95_tpot_ms",
    "p99_tpot_ms",
    "mean_itl_ms",
    "median_itl_ms",
    "std_itl_ms",
    "p90_itl_ms",
    "p95_itl_ms",
    "p99_itl_ms",
    "concurrency",
    "accept_length",
    "max_output_tokens_per_s",
)


def _reject_nonfinite(_: str) -> None:
    raise NormalizationError("SGLang native output contains a non-finite number")


def _strict_native_object(content: bytes) -> dict[str, Any]:
    if len(content) > _MAX_NATIVE_BYTES:
        raise NormalizationError("SGLang native output exceeds its size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise NormalizationError("SGLang native output is not valid UTF-8") from None
    if text.endswith("\n"):
        text = text[:-1]
    if not text or "\n" in text or "\r" in text:
        raise NormalizationError("SGLang native output must contain one JSONL row")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise NormalizationError("SGLang native output has duplicate keys")
            result[key] = value
        return result

    try:
        raw = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_float=Decimal,
            parse_constant=_reject_nonfinite,
        )
    except NormalizationError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise NormalizationError("SGLang native output is not valid JSON") from None
    if not isinstance(raw, dict):
        raise NormalizationError("SGLang native output must be one JSON object")
    if set(raw) - (_NATIVE_REQUIRED_FIELDS | _NATIVE_OPTIONAL_FIELDS):
        raise NormalizationError("SGLang native output has an unknown field")
    if _NATIVE_REQUIRED_FIELDS - set(raw):
        raise NormalizationError("SGLang native output is missing a field")
    if "server_info" in raw:
        if raw["server_info"] is None:
            raw["server_info_present"] = False
            raw["server_info_sha256"] = None
        else:
            _validate_server_info(raw["server_info"])
            raw["server_info_present"] = True
            raw["server_info_sha256"] = _server_info_digest(raw["server_info"])
        raw.pop("server_info")
    else:
        raw["server_info_present"] = False
        raw["server_info_sha256"] = None

    for field_name in (
        "input_lens",
        "output_lens",
        "ttfts",
        "generated_texts",
        "errors",
    ):
        value = raw.get(field_name)
        if not isinstance(value, list):
            raise NormalizationError("SGLang native arrays have an invalid type")
        raw[field_name] = tuple(value)
    raw_itls = raw.get("itls")
    if not isinstance(raw_itls, list):
        raise NormalizationError("SGLang native ITL array has an invalid type")
    converted_itls: list[tuple[Decimal, ...]] = []
    for row in raw_itls:
        if not isinstance(row, list):
            raise NormalizationError("SGLang native ITL row has an invalid type")
        converted: list[Decimal] = []
        for value in row:
            if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
                raise NormalizationError("SGLang native timing has an invalid type")
            converted.append(Decimal(value))
        converted_itls.append(tuple(converted))
    raw["itls"] = tuple(converted_itls)
    for field_name in _NATIVE_DECIMAL_FIELDS:
        value = raw[field_name]
        if value is None and field_name == "accept_length":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise NormalizationError("SGLang native numeric field has an invalid type")
        raw[field_name] = Decimal(value)
    for field_name in ("ttfts",):
        values = raw[field_name]
        converted_timing: list[Decimal] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
                raise NormalizationError("SGLang native timing has an invalid type")
            converted_timing.append(Decimal(value))
        raw[field_name] = tuple(converted_timing)
    try:
        result = SglangNativeDetailedResult.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        raise NormalizationError(
            "SGLang native output violates the pinned contract"
        ) from None
    return result.model_dump(mode="python")


def _validate_server_info(
    value: object, *, depth: int = 0, nodes: list[int] | None = None
) -> None:
    counter = nodes if nodes is not None else [0]
    counter[0] += 1
    if counter[0] > _MAX_SERVER_INFO_NODES or depth > _MAX_SERVER_INFO_DEPTH:
        raise NormalizationError("SGLang server-info is too complex")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > _MAX_SERVER_INFO_INTEGER:
            raise NormalizationError("SGLang server-info integer is outside its bound")
        return
    if isinstance(value, Decimal):
        if (
            not value.is_finite()
            or abs(value) > Decimal("1e18")
            or (
                value == value.to_integral_value()
                and abs(value) > _MAX_SERVER_INFO_INTEGER
            )
        ):
            raise NormalizationError("SGLang server-info number is outside its bound")
        return
    if isinstance(value, str):
        if len(value) > _MAX_SERVER_INFO_STRING or any(
            ord(character) < 0x20 for character in value
        ):
            raise NormalizationError("SGLang server-info text is outside its bound")
        return
    if isinstance(value, list):
        if len(value) > 256:
            raise NormalizationError("SGLang server-info array is too large")
        for child in value:
            _validate_server_info(child, depth=depth + 1, nodes=counter)
        return
    if isinstance(value, dict):
        if len(value) > 256:
            raise NormalizationError("SGLang server-info object is too large")
        for key, child in value.items():
            if (
                not isinstance(key, str)
                or len(key) > 256
                or any(ord(character) < 0x20 for character in key)
            ):
                raise NormalizationError("SGLang server-info key is invalid")
            _validate_server_info(child, depth=depth + 1, nodes=counter)
        try:
            if (
                len(canonical_json_bytes(_canonical_json_value(value)))
                > _MAX_SERVER_INFO_BYTES
            ):
                raise NormalizationError("SGLang server-info exceeds its size limit")
        except (TypeError, ValueError, OverflowError):
            raise NormalizationError(
                "SGLang server-info is not canonicalizable"
            ) from None
        return
    raise NormalizationError("SGLang server-info has an unsupported value")


def _canonical_json_value(value: object) -> object:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("non-finite server-info number")
        return converted
    if isinstance(value, list):
        return [_canonical_json_value(child) for child in value]
    if isinstance(value, dict):
        return {str(key): _canonical_json_value(child) for key, child in value.items()}
    return value


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
            "SGLang timing is outside the canonical domain"
        ) from None
    if not 0 <= result <= _MAX_NS:
        raise NormalizationError("SGLang timing is outside the canonical domain")
    return result


def sglang_native_schema_fingerprint() -> str:
    contract = {
        "schema_version": SGLANG_NATIVE_SCHEMA_VERSION,
        "producer": SGLANG_BACKEND,
        "producer_version": SGLANG_VERSION,
        "required_fields": sorted(_NATIVE_REQUIRED_FIELDS),
        "optional_fields": sorted(_NATIVE_OPTIONAL_FIELDS),
        "ttft_definition": "request_start_to_first_non_empty_cumulative_text",
        "itl_definition": (
            "later_qualifying_chunk_intervals_apportioned_over_new_tokens"
        ),
    }
    return sha256_digest(canonical_json_bytes(contract))


def _server_info_digest(value: object) -> str:
    try:
        encoded = canonical_json_bytes(_canonical_json_value(value))
    except (TypeError, ValueError, OverflowError, ArithmeticError):
        raise NormalizationError(
            "SGLang server-info is not canonicalizable"
        ) from None
    if len(encoded) > _MAX_SERVER_INFO_BYTES:
        raise NormalizationError("SGLang server-info exceeds its size limit")
    return sha256_digest(encoded)


def _canonical_report_bytes(report: SglangNormalizationReport) -> bytes:
    try:
        encoded = canonical_json_bytes(report.model_dump(mode="json", by_alias=True))
    except (TypeError, ValueError, OverflowError, ArithmeticError):
        raise NormalizationError(
            "SGLang normalization report is not canonicalizable"
        ) from None
    if len(encoded) > _MAX_REPORT_BYTES:
        raise NormalizationError("SGLang normalization report exceeds its size limit")
    return encoded


def _parse_native(content: bytes) -> SglangNativeDetailedResult:
    raw = _strict_native_object(content)
    try:
        return SglangNativeDetailedResult.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        raise NormalizationError(
            "SGLang native output violates the pinned contract"
        ) from None


def _validate_native_invocation_binding(
    native: SglangNativeDetailedResult,
    invocation: SglangInvocation,
) -> None:
    config = invocation.config
    if (
        native.backend != SGLANG_BACKEND
        or native.dataset_name != "random-ids"
        or native.request_rate != config.request_rate
        or native.max_concurrency != config.concurrency
        or native.sharegpt_output_len is not None
        or native.random_input_len != config.input_tokens
        or native.random_output_len != config.output_tokens
        or native.random_range_ratio != Decimal(0)
        or native.tag != SGLANG_FIXED_TAG
        or any(
            input_tokens != config.input_tokens
            for input_tokens in native.input_lens
        )
        or native.max_concurrent_requests > config.concurrency
    ):
        raise NormalizationError("SGLang native controls differ from invocation")


def normalize_sglang_native(
    native_bytes: bytes,
    invocation: SglangInvocation,
    *,
    synthetic_only: bool = False,
) -> SglangNormalizationResult:
    """Normalize only observations persisted by native SGLang output."""

    if not isinstance(invocation, SglangInvocation):
        raise NormalizationError("SGLang invocation context is invalid")
    try:
        validated_invocation = validate_sglang_invocation(invocation.evidence_bytes)
    except AdapterError:
        raise NormalizationError("SGLang invocation context is invalid") from None
    if validated_invocation != invocation:
        raise NormalizationError("SGLang invocation context is inconsistent")
    native = _parse_native(native_bytes)
    _validate_native_invocation_binding(native, validated_invocation)
    attempted = len(native.input_lens)
    if attempted != invocation.config.request_count:
        raise NormalizationError("SGLang native row count differs from invocation")
    if any(
        output_tokens > invocation.config.output_tokens
        for output_tokens in native.output_lens
    ):
        raise NormalizationError("SGLang output exceeds invocation token bound")
    fingerprint = sglang_native_schema_fingerprint()
    rows: list[SglangNormalizedRow] = []
    for index in range(attempted):
        error = native.errors[index]
        success = not error
        ttft_ns = _seconds_to_nanoseconds(native.ttfts[index]) if success else None
        itl_ns = tuple(_seconds_to_nanoseconds(value) for value in native.itls[index])
        generated_text = native.generated_texts[index]
        response_sha256 = (
            sha256_digest(generated_text.encode("utf-8")) if success else None
        )
        try:
            rows.append(
                SglangNormalizedRow(
                    native_index=index,
                    success=success,
                    input_tokens=native.input_lens[index],
                    output_tokens=native.output_lens[index],
                    ttft_ns=ttft_ns,
                    itl_ns=itl_ns,
                    response_sha256=response_sha256,
                )
            )
        except (ValidationError, TypeError, ValueError):
            raise NormalizationError("SGLang normalized row is inconsistent") from None
    server_info_present = native.server_info_present
    server_info_sha256 = native.server_info_sha256
    try:
        report = SglangNormalizationReport(
            schema_version=SGLANG_NORMALIZATION_SCHEMA_VERSION,
            producer_name=SGLANG_BACKEND,
            producer_version=SGLANG_VERSION,
            release_commit=SGLANG_RELEASE_COMMIT,
            source_repository=SGLANG_SOURCE_REPOSITORY,
            benchmark_module=SGLANG_BENCHMARK_MODULE,
            backend=SGLANG_BACKEND,
            streaming=True,
            invocation_sha256=validated_invocation.evidence_sha256,
            native_result_sha256=sha256_digest(native_bytes),
            native_schema_fingerprint=fingerprint,
            server_info_present=server_info_present,
            server_info_sha256=server_info_sha256,
            attempted_request_count=attempted,
            completed_request_count=native.completed,
            failed_request_count=attempted - native.completed,
            total_input_tokens=native.total_input_tokens,
            total_output_tokens=native.total_output_tokens,
            synthetic_only=synthetic_only,
            evidence_eligible=False,
            request_plan_binding="UNAVAILABLE",
            request_start_offsets="UNAVAILABLE",
            canonical_request_record_v1="UNSUPPORTED",
            acceptance_verdict="NOT_OWNED",
            capability_states=SglangCapabilityStates(
                native_detailed_metrics="SUPPORTED",
                native_response_content="OBSERVED_BUT_OMITTED",
                request_plan_binding="UNAVAILABLE",
                request_start_offsets="UNAVAILABLE",
                canonical_request_record_v1="UNSUPPORTED",
                acceptance_verdict="NOT_OWNED",
            ),
            rows=tuple(rows),
        )
    except (ValidationError, TypeError, ValueError):
        raise NormalizationError(
            "SGLang normalization report is inconsistent"
        ) from None
    report_bytes = _canonical_report_bytes(report)
    return SglangNormalizationResult(
        native_result=native,
        report=report,
        report_bytes=report_bytes,
    )


def parse_sglang_detailed_jsonl(
    content: bytes,
    *,
    expected_request_count: int,
    max_output_tokens: int,
) -> SglangNativeDetailedResult:
    """Parse native output without normalizing it into frozen v0.1 records."""

    if (
        isinstance(expected_request_count, bool)
        or not isinstance(expected_request_count, int)
        or not 1 <= expected_request_count <= 10_000
        or isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or not 1 <= max_output_tokens <= 32_768
    ):
        raise NormalizationError("SGLang expected bounds are invalid")
    native = _parse_native(content)
    if len(native.input_lens) != expected_request_count:
        raise NormalizationError("SGLang native row count differs from expected count")
    if any(value > max_output_tokens for value in native.output_lens):
        raise NormalizationError("SGLang native output exceeds expected bound")
    return native


def validate_sglang_normalization_report(
    content: bytes,
    invocation: SglangInvocation,
    *,
    expected_native_bytes: bytes | None = None,
) -> SglangNormalizationReport:
    """Verify canonical report bytes and cross-input bindings.

    When native bytes are supplied, the report is replayed through the same
    normalizer and must match byte-for-byte.  Without them this function is a
    deliberately structural check: it can verify the pinned schema/fingerprint
    and invocation binding, but cannot independently verify the native-result
    digest's preimage.
    """

    if len(content) > _MAX_REPORT_BYTES:
        raise NormalizationError("SGLang normalization report exceeds its size limit")
    if not isinstance(invocation, SglangInvocation):
        raise NormalizationError("SGLang invocation context is invalid")
    try:
        validated_invocation = validate_sglang_invocation(invocation.evidence_bytes)
    except AdapterError:
        raise NormalizationError("SGLang invocation context is invalid") from None
    if validated_invocation != invocation:
        raise NormalizationError("SGLang invocation context is inconsistent")

    try:
        text = content.decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=lambda pairs: _unique_report_object(pairs),
            parse_constant=lambda _: (_ for _ in ()).throw(
                NormalizationError("SGLang normalization has a non-finite number")
            ),
        )
    except NormalizationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise NormalizationError(
            "SGLang normalization report is invalid JSON"
        ) from None
    if not isinstance(raw, dict):
        raise NormalizationError("SGLang normalization report must be one object")
    raw_rows = raw.get("rows")
    if isinstance(raw_rows, list):
        prepared_rows: list[dict[str, Any]] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                raise NormalizationError("SGLang normalization row is invalid")
            row = dict(raw_row)
            if isinstance(row.get("itl_ns"), list):
                row["itl_ns"] = tuple(row["itl_ns"])
            prepared_rows.append(row)
        raw["rows"] = tuple(prepared_rows)
    try:
        report = SglangNormalizationReport.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        raise NormalizationError(
            "SGLang normalization report violates its contract"
        ) from None
    if canonical_json_bytes(report.model_dump(mode="json")) != content:
        raise NormalizationError("SGLang normalization report is not canonical")
    if report.invocation_sha256 != validated_invocation.evidence_sha256:
        raise NormalizationError("SGLang normalization invocation binding differs")
    if report.native_schema_fingerprint != sglang_native_schema_fingerprint():
        raise NormalizationError("SGLang normalization schema fingerprint differs")
    if tuple(row.native_index for row in report.rows) != tuple(
        range(report.attempted_request_count)
    ):
        raise NormalizationError("SGLang normalization row order is invalid")
    if expected_native_bytes is not None:
        if report.native_result_sha256 != sha256_digest(expected_native_bytes):
            raise NormalizationError("SGLang normalization native binding differs")
        try:
            replayed = normalize_sglang_native(
                expected_native_bytes,
                validated_invocation,
                synthetic_only=report.synthetic_only,
            )
        except NormalizationError:
            raise NormalizationError(
                "SGLang normalization native replay failed"
            ) from None
        if replayed.report_bytes != content:
            raise NormalizationError("SGLang normalization replay differs")
    return report


def _unique_report_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NormalizationError("SGLang normalization has duplicate keys")
        result[key] = value
    return result


__all__ = [
    "SGLANG_NATIVE_SCHEMA_VERSION",
    "SGLANG_NORMALIZATION_SCHEMA_ID",
    "SGLANG_NORMALIZATION_SCHEMA_VERSION",
    "SglangCapabilityStates",
    "SglangNativeDetailedResult",
    "SglangNormalizationReport",
    "SglangNormalizationResult",
    "SglangNormalizedRow",
    "normalize_sglang_native",
    "parse_sglang_detailed_jsonl",
    "sglang_native_schema_fingerprint",
    "validate_sglang_normalization_report",
]
