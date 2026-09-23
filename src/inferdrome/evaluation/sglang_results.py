"""Engine-bound SGLang results using the native client and numerical policies.

The private adaptation below supplies two numerical score coordinates to the
existing verifier. It does not equate SGLang scheduler gauges with vLLM state.
Only explicit SGLang v2 observations and engine-bound envelopes are public;
the temporary v1-shaped replay input must never be retained or published.
Acquisition age does not establish the age of scheduler state at the source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal, cast

from pydantic import Field

from inferdrome.evaluation.contracts import ClosedModel, EvaluationError
from inferdrome.evaluation.engine_binding import (
    SGLANG_TELEMETRY_SEMANTICS,
    EvaluationEngineBinding,
    SglangDirectEngineBinding,
    SglangEngineBinding,
    engine_binding_bytes,
    engine_binding_sha256,
    load_engine_binding_bytes,
    validate_engine_binding_trial,
)
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.faults import RoutingFaultResult, TrialStatus
from inferdrome.evaluation.healthy import HealthyRoutingResult
from inferdrome.evaluation.study_config import CompiledStudy, CompiledTrial, Digest
from inferdrome.evaluation.study_validation import (
    ValidatedTrialResult,
    validate_trial_result,
)
from inferdrome.parsing import StructuredDataLimits, validate_json_structure
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

_LIMITS = StructuredDataLimits(
    max_depth=34, max_tokens=4_002_500, max_integer_digits=16
)
_SEMANTICS = {
    "engine": "sglang",
    "telemetry_semantics": SGLANG_TELEMETRY_SEMANTICS,
    "telemetry_freshness": "ACQUISITION_START_AGE_ONLY",
    "telemetry_source_age": "UNAVAILABLE",
}
_COUNT_NAMES = (
    ("running", "reported_running_requests"),
    ("waiting", "reported_queued_requests"),
)


class _TrialEnvelope(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-study-trial-result.v2"]
    plan_sha256: Digest
    trial_id: Annotated[str, Field(pattern=r"^trial-[0-9]{4}$")]
    block_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
    workload_sha256: Digest
    config_sha256: Digest
    engine_binding: EvaluationEngineBinding
    engine_binding_sha256: Digest
    result: dict[str, object]


class _DirectTrialEnvelope(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-study-trial-result.v3"]
    plan_sha256: Digest
    trial_id: Annotated[str, Field(pattern=r"^trial-[0-9]{4}$")]
    block_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
    workload_sha256: Digest
    config_sha256: Digest
    engine_binding: SglangDirectEngineBinding
    engine_binding_sha256: Digest
    result: dict[str, object]


@dataclass(frozen=True)
class SGLangTrialResult:
    """Validated engine-bound result; statistical replay input remains private."""

    binding: SglangEngineBinding
    engine_binding_sha256: str
    result_sha256: str
    _raw_bytes: bytes = field(repr=False)
    _statistical_input: ValidatedTrialResult = field(repr=False)

    @property
    def status(self) -> TrialStatus:
        return cast(TrialStatus, self._statistical_input.status)

    @property
    def evidence_class(self) -> str:
        return self._statistical_input.evidence_class

    def to_dict(self) -> dict[str, Any]:
        """Return a detached SGLang v2 raw result, never private replay input."""
        result: dict[str, Any] = json.loads(self._raw_bytes)
        return result


def _context(
    binding: SglangEngineBinding, plan: CompiledStudy, trial: CompiledTrial
) -> SglangEngineBinding:
    validate_engine_binding_trial(binding, plan, trial)
    validated = load_engine_binding_bytes(engine_binding_bytes(binding), plan)
    return validated


def _counts(row: Any, *, to_sglang: bool) -> None:
    if type(row) is not dict:
        raise ValueError
    for native, explicit in _COUNT_NAMES:
        source, target = (native, explicit) if to_sglang else (explicit, native)
        if source not in row or target in row:
            raise ValueError
        row[target] = row.pop(source)


def _convert_counts(raw: dict[str, Any], *, to_sglang: bool) -> None:
    """Adapt only known load fields; the verifier closes every nested object."""
    if (
        type(raw.get("observations")) is not list
        or type(raw.get("decisions")) is not list
    ):
        raise ValueError
    for observation in raw["observations"]:
        _counts(observation, to_sglang=to_sglang)
    for decision in raw["decisions"]:
        endpoints = decision["snapshot"]["endpoints"]
        if type(endpoints) is not list or len(endpoints) != 2:
            raise ValueError
        for endpoint in endpoints:
            for name in ("load", "last_load_attempt"):
                if endpoint[name] is not None:
                    _counts(endpoint[name], to_sglang=to_sglang)


def _schema(trial: CompiledTrial, version: int) -> str:
    scenario = "routing" if isinstance(trial.config, RoutingFaultConfig) else "healthy"
    return f"inferdrome.evaluation-{scenario}-result.v{version}"


def _validate_raw(
    raw: dict[str, Any],
    binding: SglangEngineBinding,
    trial: CompiledTrial,
    *,
    result_digest: str | None = None,
) -> SGLangTrialResult:
    binding_digest = engine_binding_sha256(binding)
    if (
        raw.get("schema_version") != _schema(trial, _version(binding))
        or raw.get("engine_binding_sha256") != binding_digest
        or any(raw.get(name) != value for name, value in _semantics(binding).items())
    ):
        raise ValueError
    encoded = canonical_json_bytes(raw) + b"\n"
    # This detached numerical replay shape is private and short lived. The same
    # policy, timestamp, population, failure and cancellation guards run here.
    numerical = json.loads(encoded)
    numerical["schema_version"] = _schema(trial, 1)
    for name in (*_SEMANTICS, "engine_binding_sha256"):
        del numerical[name]
    _convert_counts(numerical, to_sglang=False)
    validated = validate_trial_result(numerical, trial.config)
    digest = result_digest or sha256_digest(encoded)
    return SGLangTrialResult(
        binding,
        binding_digest,
        digest,
        encoded,
        replace(validated, result_sha256=digest),
    )


def wrap_sglang_result(
    native_result: HealthyRoutingResult | RoutingFaultResult,
    binding: SglangEngineBinding,
    plan: CompiledStudy,
    trial: CompiledTrial,
) -> SGLangTrialResult:
    """Bind trusted native execution before exposing explicit SGLang results."""
    try:
        binding = _context(binding, plan, trial)
        expected = (
            RoutingFaultResult
            if isinstance(trial.config, RoutingFaultConfig)
            else HealthyRoutingResult
        )
        if type(native_result) is not expected:
            raise ValueError
        raw = json.loads(canonical_json_bytes(native_result.to_dict()))
        if raw["schema_version"] != _schema(trial, 1):
            raise ValueError
        _convert_counts(raw, to_sglang=True)
        raw.update(_semantics(binding))
        raw["schema_version"] = _schema(trial, _version(binding))
        raw["engine_binding_sha256"] = engine_binding_sha256(binding)
        return _validate_raw(raw, binding, trial)
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise EvaluationError(
            "SGLang result violates its engine-bound contract"
        ) from None


def sglang_trial_bytes(
    plan: CompiledStudy,
    trial: CompiledTrial,
    result: SGLangTrialResult,
    binding: SglangEngineBinding,
) -> bytes:
    """Serialize a revalidated v2 envelope with its full pre-dispatch binding."""
    try:
        binding = _context(binding, plan, trial)
        if type(result) is not SGLangTrialResult or result.binding != binding:
            raise ValueError
        validated = _validate_raw(result.to_dict(), binding, trial)
        if result.engine_binding_sha256 != validated.engine_binding_sha256:
            raise ValueError
        model = (
            _DirectTrialEnvelope
            if isinstance(binding, SglangDirectEngineBinding)
            else _TrialEnvelope
        )
        artifact = model.model_validate(
            dict(
                schema_version=f"inferdrome.evaluation-study-trial-result.v{_version(binding)}",
                plan_sha256=binding.plan_sha256,
                trial_id=trial.trial_id,
                block_id=trial.block_id,
                workload_sha256=trial.workload_sha256,
                config_sha256=trial.config_sha256,
                engine_binding=binding,
                engine_binding_sha256=validated.engine_binding_sha256,
                result=validated.to_dict(),
            )
        )
        content = canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n"
        if not 1 <= len(content) <= plan.config.limits.per_trial_result_bytes:
            raise ValueError
        return content
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise EvaluationError(
            "SGLang trial artifact violates its expected binding"
        ) from None


def load_sglang_trial_bytes(
    content: bytes,
    plan: CompiledStudy,
    trial: CompiledTrial,
    binding: SglangEngineBinding,
) -> SGLangTrialResult:
    """Check bounded closed identities and selected semantics before policy replay."""
    try:
        binding = _context(binding, plan, trial)
        if (
            type(content) is not bytes
            or not 1 <= len(content) <= plan.config.limits.per_trial_result_bytes
        ):
            raise ValueError
        validate_json_structure(content.decode("utf-8"), limits=_LIMITS)
        model = (
            _DirectTrialEnvelope
            if isinstance(binding, SglangDirectEngineBinding)
            else _TrialEnvelope
        )
        artifact = model.model_validate_json(content)
        if (
            canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n" != content
            or artifact.plan_sha256 != binding.plan_sha256
            or artifact.trial_id != trial.trial_id
            or artifact.block_id != trial.block_id
            or artifact.workload_sha256 != trial.workload_sha256
            or artifact.config_sha256 != trial.config_sha256
            or artifact.engine_binding != binding
            or artifact.engine_binding_sha256 != engine_binding_sha256(binding)
        ):
            raise ValueError
        return _validate_raw(
            artifact.result, binding, trial, result_digest=sha256_digest(content)
        )
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        UnicodeError,
        RecursionError,
    ):
        raise EvaluationError(
            "SGLang trial artifact violates its expected binding"
        ) from None


def _version(binding: SglangEngineBinding) -> int:
    return 3 if isinstance(binding, SglangDirectEngineBinding) else 2


def _semantics(binding: SglangEngineBinding) -> dict[str, str]:
    return {**_SEMANTICS, "telemetry_semantics": binding.telemetry_semantics}
