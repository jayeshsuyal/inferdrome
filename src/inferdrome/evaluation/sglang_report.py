"""Bound SGLang envelope around the existing validated statistical summaries.

The source population's LOCAL_MEASUREMENT_ONLY or SYNTHETIC_ONLY classification
is retained. Identity digests and DECLARED_COLD do not establish artifact identity,
GPU qualification, a performed reset, or observed cache state. The engine binding
must accompany every statistical projection; never export its nested v1 report.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Literal

from pydantic import ConfigDict, field_validator

from inferdrome.evaluation.contracts import ClosedModel, EvaluationError
from inferdrome.evaluation.engine_binding import (
    MAX_ENGINE_BINDING_BYTES,
    EvaluationEngineBinding,
    SglangDirectEngineBinding,
    SglangEngineBinding,
    engine_binding_bytes,
    engine_binding_sha256,
    load_engine_binding_bytes,
)
from inferdrome.evaluation.report_reader import (
    MAX_REPORT_BYTES,
    StudyReport,
    load_evaluation_report_bytes,
)
from inferdrome.evaluation.study_config import CompiledStudy, Digest
from inferdrome.evaluation.study_report import render_markdown
from inferdrome.parsing import StructuredDataLimits, validate_json_structure
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

MAX_SGLANG_REPORT_BYTES = MAX_REPORT_BYTES + MAX_ENGINE_BINDING_BYTES + 4096
_LIMITS = StructuredDataLimits(
    max_depth=33, max_tokens=1_002_048, max_integer_digits=16
)


class SglangStudyReport(ClosedModel):
    """Closed self-contained report, retaining explicit engine and source claims."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True
    )
    schema_version: Literal["inferdrome.evaluation-study-report.v2"]
    engine: Literal["sglang"]
    engine_binding: EvaluationEngineBinding
    engine_binding_sha256: Digest
    statistical_report: StudyReport
    statistical_report_sha256: Digest
    evidence_class: Literal["LOCAL_MEASUREMENT_ONLY", "SYNTHETIC_ONLY"]
    runtime_verification: Literal["UNVERIFIED"]
    evidence_eligible: Literal[False]
    dashboard_projection: Literal["UNSUPPORTED_ENGINE_BINDING", "ENGINE_BOUND_V2"]

    @field_validator("evidence_eligible", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("eligibility requires a boolean primitive")
        return value


class SglangDirectStudyReport(ClosedModel):
    """Closed self-contained report, retaining explicit engine and source claims."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True
    )
    schema_version: Literal["inferdrome.evaluation-study-report.v3"]
    engine: Literal["sglang"]
    engine_binding: SglangDirectEngineBinding
    engine_binding_sha256: Digest
    statistical_report: StudyReport
    statistical_report_sha256: Digest
    evidence_class: Literal["LOCAL_MEASUREMENT_ONLY", "SYNTHETIC_ONLY"]
    runtime_verification: Literal["UNVERIFIED"]
    evidence_eligible: Literal[False]
    dashboard_projection: Literal["UNSUPPORTED_ENGINE_BINDING"]

    @field_validator("evidence_eligible", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("eligibility requires a boolean primitive")
        return value


def _json_object(content: bytes, maximum: int) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in items:
            if name in result:
                raise ValueError
            result[name] = value
        return result

    def reject_number(_value: str) -> Any:
        raise ValueError

    if type(content) is not bytes or not 1 <= len(content) <= maximum:
        raise ValueError
    text = content.decode("utf-8")
    validate_json_structure(text, limits=_LIMITS)
    value = json.loads(
        text,
        object_pairs_hook=pairs,
        parse_float=reject_number,
        parse_constant=reject_number,
    )
    if type(value) is not dict:
        raise ValueError
    return value


def _checked_statistics(value: dict[str, Any], plan: CompiledStudy) -> StudyReport:
    # Check integer primitives before canonical encoding can normalize floats.
    payload = _json_object(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8"),
        MAX_REPORT_BYTES,
    )
    report = load_evaluation_report_bytes(
        canonical_json_bytes(payload) + b"\n", kind="STUDY"
    )
    if not isinstance(report, StudyReport):
        raise ValueError
    if (
        report.config_sha256 != plan.config_sha256
        or report.plan_sha256
        != sha256_digest(canonical_json_bytes(plan.to_dict()) + b"\n")
        or report.preparation.model_dump(mode="json")
        != plan.config.preparation.model_dump(mode="json")
        or report.reporting.model_dump(mode="json")
        != plan.reporting.model_dump(mode="json")
        or report.coverage.planned_trials != len(plan.trials)
    ):
        raise ValueError
    trials = {trial.trial_id: trial for trial in plan.trials}
    for summary in report.trials:
        expected = trials.get(summary.trial_id)
        if expected is None:
            raise ValueError
        summary_fields = summary.model_dump(mode="json")
        if any(
            summary_fields[name] != wanted
            for name, wanted in expected.to_dict().items()
        ):
            raise ValueError
    for population in ("foreground", "background"):
        count = sum(
            len(config.offers)
            for trial in plan.trials
            if (config := getattr(trial.config, population, None)) is not None
        )
        if getattr(report.coverage, population).planned_offers != count:
            raise ValueError
    expected_strata: dict[tuple[str, str, str | None], set[str]] = defaultdict(set)
    for trial in plan.trials:
        expected_strata[
            (trial.scenario, trial.profile_id, trial.target_endpoint_id)
        ].add(trial.block_id)
    actual_strata = {
        (
            stratum.scenario,
            stratum.profile_id,
            stratum.target_endpoint_id,
        ): stratum.planned_blocks
        for stratum in report.strata
    }
    if actual_strata != {key: len(blocks) for key, blocks in expected_strata.items()}:
        raise ValueError
    return report


def bind_sglang_report(
    statistical_report: dict[str, Any],
    binding: SglangEngineBinding,
    plan: CompiledStudy,
) -> dict[str, Any]:
    """Wrap existing statistics without changing their values or evidence class.

    This verifies report integrity and plan membership, not raw-source execution.
    The bundle owner must validate native populations before computing statistics.
    """
    try:
        checked_binding = load_engine_binding_bytes(engine_binding_bytes(binding), plan)
        checked_report = _checked_statistics(statistical_report, plan)
        direct = isinstance(checked_binding, SglangDirectEngineBinding)
        model = SglangDirectStudyReport if direct else SglangStudyReport
        schema = f"inferdrome.evaluation-study-report.v{3 if direct else 2}"
        report = model.model_validate(
            dict(
                schema_version=schema,
                engine="sglang",
                engine_binding=checked_binding,
                engine_binding_sha256=engine_binding_sha256(checked_binding),
                statistical_report=checked_report,
                statistical_report_sha256=sha256_digest(
                    canonical_json_bytes(checked_report.model_dump(mode="json")) + b"\n"
                ),
                evidence_class=checked_report.evidence_class,
                runtime_verification="UNVERIFIED",
                evidence_eligible=False,
                dashboard_projection="UNSUPPORTED_ENGINE_BINDING"
                if direct
                else "ENGINE_BOUND_V2",
            )
        ).model_dump(mode="json")
        if len(canonical_json_bytes(report)) + 1 > MAX_SGLANG_REPORT_BYTES:
            raise ValueError
        return report
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise EvaluationError(
            "SGLang report violates its bound study contract"
        ) from None


def _read_sglang_report_bytes(
    content: bytes,
) -> SglangStudyReport | SglangDirectStudyReport:
    """Read a self-contained envelope without replaying a private study or sources.

    Validate both exact component digests and their shared study identities. This
    does not attest the declared engine, model artifacts, cache state or runtime.
    Historical unsupported envelopes remain readable, but are not projectable.
    """
    try:
        raw = _json_object(content, MAX_SGLANG_REPORT_BYTES)
        if canonical_json_bytes(raw) + b"\n" != content:
            raise ValueError
        model = (
            SglangDirectStudyReport
            if raw.get("schema_version") == "inferdrome.evaluation-study-report.v3"
            else SglangStudyReport
        )
        report = model.model_validate_json(content)
        if canonical_json_bytes(report.model_dump(mode="json")) + b"\n" != content:
            raise ValueError
        statistics = load_evaluation_report_bytes(
            canonical_json_bytes(raw["statistical_report"]) + b"\n", kind="STUDY"
        )
        if not isinstance(statistics, StudyReport):
            raise ValueError
        binding = report.engine_binding
        expected_prefix = (
            "DECLARED_ENABLED"
            if binding.settings.prefix_cache == "RADIX_ENABLED"
            else "DECLARED_DISABLED"
        )
        if (
            report.engine_binding_sha256 != engine_binding_sha256(binding)
            or report.statistical_report_sha256
            != sha256_digest(canonical_json_bytes(raw["statistical_report"]) + b"\n")
            or statistics.config_sha256 != binding.config_sha256
            or statistics.plan_sha256 != binding.plan_sha256
            or statistics.evidence_class != report.evidence_class
            or statistics.preparation.cache_state != binding.cache_state
            or statistics.preparation.prefix_caching != expected_prefix
            or statistics.preparation.serving_image_reference
            not in (
                (None,)
                if isinstance(binding, SglangDirectEngineBinding)
                else (None, binding.image_reference.split("@", 1)[1])
            )
        ):
            raise ValueError
        return report
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        UnicodeError,
        RecursionError,
    ):
        raise EvaluationError(
            "SGLang report violates its bound study contract"
        ) from None


def load_sglang_report_bytes(
    content: bytes,
    plan: CompiledStudy,
    binding: SglangEngineBinding,
) -> dict[str, Any]:
    """Validate exact canonical bytes, both component digests and bound summaries."""
    try:
        report = _read_sglang_report_bytes(content)
        rebuilt = bind_sglang_report(
            report.statistical_report.model_dump(mode="json"), binding, plan
        )
        # Preserve producer-era capability declarations and historical bytes.
        rebuilt["dashboard_projection"] = report.dashboard_projection
        if canonical_json_bytes(rebuilt) + b"\n" != content:
            raise ValueError
        return rebuilt
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        UnicodeError,
        RecursionError,
    ):
        raise EvaluationError(
            "SGLang report violates its bound study contract"
        ) from None


def render_sglang_markdown(
    report: dict[str, Any],
    plan: CompiledStudy,
    binding: SglangEngineBinding,
) -> str:
    """Render the unchanged statistics with inseparable SGLang claim boundaries."""
    try:
        # Preserve input primitive types until the closed parser checks them.
        raw = _json_object(
            json.dumps(
                report, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8"),
            MAX_SGLANG_REPORT_BYTES,
        )
        checked = load_sglang_report_bytes(
            canonical_json_bytes(raw) + b"\n", plan, binding
        )
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise EvaluationError(
            "SGLang report violates its bound study contract"
        ) from None
    lines = [
        f"# SGLang {binding.producer_version} native study",
        "",
        f"Engine binding: `{checked['engine_binding_sha256']}`. "
        f"Statistical report: `{checked['statistical_report_sha256']}`.",
        "",
        f"Source population evidence class: **{checked['evidence_class']}**. "
        "Runtime: **UNVERIFIED**; evidence eligible: **false**.",
        "",
        f"Telemetry semantics: **{binding.telemetry_semantics}**. "
        "Acquisition freshness uses scrape start time; scheduler-state age is "
        "**UNAVAILABLE**. These gauges do not establish vLLM load equivalence.",
        "",
        "Preparation: **WARMUP_DRAIN_FLUSH**, cache state **DECLARED_COLD**. "
        "This declaration does not establish that a reset occurred or that cache "
        "state was observed. Artifact and GPU compatibility remain unverified.",
        "",
        f"Dashboard projection: **{checked['dashboard_projection']}**. "
        "Keep the engine binding attached to the statistical report. "
        "Historical UNSUPPORTED_ENGINE_BINDING reports remain withheld.",
        "",
    ]
    return "\n".join(lines) + "\n" + render_markdown(checked["statistical_report"])


def read_sglang_report_bytes(content: bytes) -> SglangStudyReport:
    """Preserve the closed 0.5.18 dashboard reader boundary."""
    report = _read_sglang_report_bytes(content)
    if type(report) is not SglangStudyReport:
        raise EvaluationError("SGLang report is unsupported by the v2 reader")
    return report


def read_sglang_direct_report_bytes(content: bytes) -> SglangDirectStudyReport:
    """Read the distinct native 0.5.15 report; dashboard projection is withheld."""
    report = _read_sglang_report_bytes(content)
    if type(report) is not SglangDirectStudyReport:
        raise EvaluationError("SGLang report is unsupported by the direct reader")
    return report
