"""SYNTHETIC_ONLY reducer-envelope fixtures; no SGLang runtime/cache validation.

The existing deterministic native population fixtures supply statistical shapes.
These tests do not claim their fake metrics came from SGLang or a live engine.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.engine_binding import (
    EvaluationEngineBinding,
    build_sglang_engine_binding,
    engine_binding_bytes,
    engine_binding_sha256,
)
from inferdrome.evaluation.report_reader import load_evaluation_report_bytes
from inferdrome.evaluation.sglang_report import (
    MAX_SGLANG_REPORT_BYTES,
    bind_sglang_report,
    load_sglang_report_bytes,
    read_sglang_report_bytes,
    render_sglang_markdown,
)
from inferdrome.evaluation.study_config import CompiledStudy, compile_study
from inferdrome.evaluation.study_report import (
    render_markdown,
    summarize_study,
    summarize_trial,
)
from inferdrome.evaluation.study_validation import validate_trial_result
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_evaluation_engine_binding import profiles, study
from tests.unit.test_evaluation_study_config import load
from tests.unit.test_evaluation_study_report import _manifest, measure


def encoded(value: dict[str, Any]) -> bytes:
    return canonical_json_bytes(value) + b"\n"


@pytest.fixture(scope="module", params=["HEALTHY", "STALE_LOAD"])
def fixture(
    request: pytest.FixtureRequest,
) -> tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]]:
    plan = compile_study(study(request.param))
    summaries = [
        summarize_trial(
            trial,
            replace(
                validate_trial_result(measure(trial), trial.config),
                evidence_class="SYNTHETIC_ONLY",
            ),
        )
        for trial in plan.trials
    ]
    statistical = summarize_study(plan, summaries, _manifest(plan, summaries))
    return plan, build_sglang_engine_binding(plan, profiles()), statistical


def test_bound_report_preserves_statistics_claims_and_exact_component_digests(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
) -> None:
    plan, binding, source = fixture
    before = deepcopy(source)
    report = bind_sglang_report(source, binding, plan)
    assert source == before
    assert report["schema_version"] == "inferdrome.evaluation-study-report.v2"
    assert report["engine"] == "sglang"
    assert report["statistical_report"] == source
    assert report["statistical_report_sha256"] == sha256_digest(encoded(source))
    assert report["engine_binding"] == json.loads(engine_binding_bytes(binding))
    assert report["engine_binding_sha256"] == engine_binding_sha256(binding)
    assert report["runtime_verification"] == "UNVERIFIED"
    assert report["evidence_eligible"] is False
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert report["dashboard_projection"] == "ENGINE_BOUND_V2"
    assert load_sglang_report_bytes(encoded(report), plan, binding) == report
    assert read_sglang_report_bytes(encoded(report)).model_dump(mode="json") == report
    for forbidden in (b"private-model", b"private foreground", b"127.0.0.1", b"/opt/"):
        assert forbidden not in encoded(report)


def test_frozen_dashboard_reader_rejects_bound_envelope(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
) -> None:
    plan, binding, source = fixture
    report = bind_sglang_report(source, binding, plan)
    with pytest.raises(EvaluationError):
        load_evaluation_report_bytes(encoded(report), kind="STUDY")
    assert (
        load_evaluation_report_bytes(encoded(source), kind="STUDY").model_dump(
            mode="json"
        )
        == source
    )


def test_markdown_prepends_binding_and_limitations_without_changing_statistics(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
) -> None:
    plan, binding, source = fixture
    report = bind_sglang_report(source, binding, plan)
    markdown = render_sglang_markdown(report, plan, binding)
    assert markdown.startswith("# SGLang 0.5.18 native study\n")
    # Canonical loading orders mapping keys; the existing renderer iterates some
    # outcome maps. Compare the exact reader-validated source representation.
    checked_source = load_sglang_report_bytes(encoded(report), plan, binding)[
        "statistical_report"
    ]
    assert checked_source == source
    assert markdown.endswith(render_markdown(checked_source))
    for required in (
        engine_binding_sha256(binding),
        report["statistical_report_sha256"],
        "SYNTHETIC_ONLY",
        "UNVERIFIED",
        "**false**",
        "SGLANG_0_5_18_SCHEDULER_GAUGES",
        "UNAVAILABLE",
        "WARMUP_DRAIN_FLUSH",
        "DECLARED_COLD",
        "does not establish that a reset occurred",
        "ENGINE_BOUND_V2",
    ):
        assert required in markdown


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", "inferdrome.evaluation-study-report.v1"),
        ("engine", "vllm"),
        ("runtime_verification", "VERIFIED"),
        ("evidence_eligible", True),
        ("evidence_eligible", 0),
        ("evidence_class", "LOCAL_MEASUREMENT_ONLY"),
        ("dashboard_projection", "SUPPORTED"),
        ("engine_binding_sha256", "sha256:" + "0" * 64),
        ("statistical_report_sha256", "sha256:" + "0" * 64),
        ("private_source_path", "/private/secret"),
    ],
)
def test_parser_rejects_outer_claim_digest_and_extra_field_tampering(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
    field: str,
    replacement: object,
) -> None:
    plan, binding, source = fixture
    report = bind_sglang_report(source, binding, plan)
    report[field] = replacement
    with pytest.raises(EvaluationError) as error:
        load_sglang_report_bytes(encoded(report), plan, binding)
    assert "secret" not in str(error.value)
    with pytest.raises(EvaluationError):
        read_sglang_report_bytes(encoded(report))


@pytest.mark.parametrize("mutation", ["binding", "statistics", "trial", "digest"])
def test_recomputed_component_digests_do_not_bypass_plan_or_identity_validation(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
    mutation: str,
) -> None:
    plan, binding, source = fixture
    report = bind_sglang_report(source, binding, plan)
    if mutation == "binding":
        report["engine_binding"]["settings"]["random_seed"] += 1
    elif mutation == "statistics":
        report["statistical_report"]["coverage"]["foreground"]["planned_offers"] += 1
    elif mutation == "trial":
        report["statistical_report"]["trials"][0]["config_sha256"] = (
            "sha256:" + "f" * 64
        )
    else:
        report["statistical_report"]["plan_sha256"] = "sha256:" + "f" * 64
    report["engine_binding_sha256"] = sha256_digest(encoded(report["engine_binding"]))
    report["statistical_report_sha256"] = sha256_digest(
        encoded(report["statistical_report"])
    )
    with pytest.raises(EvaluationError):
        load_sglang_report_bytes(encoded(report), plan, binding)


def test_binding_against_another_study_fails_even_when_native_ids_are_reused(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
) -> None:
    plan, binding, source = fixture
    value = plan.config.model_dump(mode="json")
    value["blocks"][0]["foreground"]["offers"][0]["prompt"] += "new prompt"
    changed_plan = compile_study(load(value))
    changed_binding = build_sglang_engine_binding(changed_plan, profiles())
    report = bind_sglang_report(source, binding, plan)
    with pytest.raises(EvaluationError):
        bind_sglang_report(source, changed_binding, changed_plan)
    with pytest.raises(EvaluationError):
        load_sglang_report_bytes(encoded(report), changed_plan, changed_binding)


@pytest.mark.parametrize("field", ["reporting", "preparation"])
def test_report_declarations_must_match_bound_native_plan(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
    field: str,
) -> None:
    plan, binding, original = fixture
    source = deepcopy(original)
    if field == "reporting":
        source["reporting"]["bootstrap_seed"] += 1
        for stratum in source["strata"]:
            for contrast in stratum["paired_contrasts"]:
                contrast["bootstrap_seed"] += 1
    else:
        source["preparation"]["cache_state"] = "UNKNOWN"
    load_evaluation_report_bytes(encoded(source), kind="STUDY")
    with pytest.raises(EvaluationError):
        bind_sglang_report(source, binding, plan)


@pytest.mark.parametrize(
    "variant", ["newline", "pretty", "duplicate", "omitted", "float"]
)
def test_report_loader_requires_complete_canonical_json_and_exact_primitives(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
    variant: str,
) -> None:
    plan, binding, source = fixture
    report = bind_sglang_report(source, binding, plan)
    raw = encoded(report)
    if variant == "newline":
        raw += b"\n"
    elif variant == "pretty":
        raw = json.dumps(report, indent=2).encode() + b"\n"
    elif variant == "duplicate":
        raw = raw.replace(b"{", b'{"engine":"sglang",', 1)
    elif variant == "omitted":
        del report["engine_binding"]["evidence_eligible"]
        raw = encoded(report)
    else:
        report["statistical_report"]["study_elapsed_ns"] = float(
            source["study_elapsed_ns"]
        )
        raw = json.dumps(report).encode() + b"\n"
    with pytest.raises(EvaluationError):
        load_sglang_report_bytes(raw, plan, binding)
    with pytest.raises(EvaluationError):
        read_sglang_report_bytes(raw)


@pytest.mark.parametrize(
    "value", [b"", b"\xff", b"null", b"NaN", b"{}{}", b"[" * 34 + b"]" * 34]
)
def test_report_loader_rejects_malformed_json(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]], value: bytes
) -> None:
    plan, binding, _ = fixture
    with pytest.raises(EvaluationError):
        load_sglang_report_bytes(value, plan, binding)


def test_report_byte_limit_precedes_parsing(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
) -> None:
    plan, binding, _ = fixture
    with pytest.raises(EvaluationError):
        load_sglang_report_bytes(b" " * (MAX_SGLANG_REPORT_BYTES + 1), plan, binding)


def test_binding_and_rendering_reject_float_coercion_in_memory(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
) -> None:
    plan, binding, original = fixture
    source = deepcopy(original)
    source["study_elapsed_ns"] = float(source["study_elapsed_ns"])
    with pytest.raises(EvaluationError):
        bind_sglang_report(source, binding, plan)
    report = bind_sglang_report(original, binding, plan)
    report["statistical_report"] = source
    with pytest.raises(EvaluationError):
        render_sglang_markdown(report, plan, binding)


def test_incomplete_and_unavailable_populations_remain_explicit(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
) -> None:
    plan, binding, original = fixture
    summaries = deepcopy(original["trials"][:1])
    partial = summarize_study(plan, summaries, _manifest(plan, summaries, "ABORTED"))
    report = bind_sglang_report(partial, binding, plan)
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert report["statistical_report"]["coverage"]["not_run_trials"] == 3
    assert (
        report["statistical_report"]["comparative_headline"]
        == "SUPPRESSED_INCOMPLETE_STUDY"
    )
    assert load_sglang_report_bytes(encoded(report), plan, binding) == report
    empty = summarize_study(plan, [], _manifest(plan, [], "ABORTED"))
    # No fake provenance is manufactured when the source has no measured rows.
    empty_report = bind_sglang_report(empty, binding, plan)
    assert empty_report["evidence_class"] == empty["evidence_class"]
    assert empty_report["statistical_report"]["coverage"]["returned_trials"] == 0


def test_historical_unsupported_envelope_keeps_exact_bytes(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
) -> None:
    plan, binding, source = fixture
    report = bind_sglang_report(source, binding, plan)
    report["dashboard_projection"] = "UNSUPPORTED_ENGINE_BINDING"
    content = encoded(report)
    assert encoded(read_sglang_report_bytes(content).model_dump(mode="json")) == content
    assert encoded(load_sglang_report_bytes(content, plan, binding)) == content


@pytest.mark.parametrize("field", ["config_sha256", "plan_sha256"])
def test_standalone_reader_rejects_cross_study_binding_even_with_recomputed_digest(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
    field: str,
) -> None:
    plan, binding, source = fixture
    report = bind_sglang_report(source, binding, plan)
    report["engine_binding"][field] = "sha256:" + "f" * 64
    report["engine_binding_sha256"] = sha256_digest(encoded(report["engine_binding"]))
    with pytest.raises(EvaluationError):
        read_sglang_report_bytes(encoded(report))


@pytest.mark.parametrize(
    "field,value",
    [
        ("telemetry_semantics", "VLLM_LOAD"),
        ("telemetry_source_age", "OBSERVED"),
        ("cache_state", "OBSERVED_COLD"),
        ("runtime_verification", "VERIFIED"),
        ("evidence_eligible", 0),
        ("private_path", "/private/secret"),
    ],
)
def test_standalone_reader_rejects_unsupported_binding_claims(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
    field: str,
    value: object,
) -> None:
    plan, binding, source = fixture
    report = bind_sglang_report(source, binding, plan)
    report["engine_binding"][field] = value
    report["engine_binding_sha256"] = sha256_digest(encoded(report["engine_binding"]))
    with pytest.raises(EvaluationError):
        read_sglang_report_bytes(encoded(report))


def test_standalone_reader_exact_byte_boundary(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inferdrome.evaluation.sglang_report as module

    plan, binding, source = fixture
    content = encoded(bind_sglang_report(source, binding, plan))
    monkeypatch.setattr(module, "MAX_SGLANG_REPORT_BYTES", len(content))
    assert read_sglang_report_bytes(content).engine == "sglang"
    monkeypatch.setattr(module, "MAX_SGLANG_REPORT_BYTES", len(content) - 1)
    with pytest.raises(EvaluationError):
        read_sglang_report_bytes(content)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cache_state", "DECLARED_WARM"),
        ("prefix_caching", "DECLARED_DISABLED"),
        ("serving_image_reference", "sha256:" + "f" * 64),
    ],
)
def test_standalone_reader_rejects_preparation_contradicting_binding(
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
    field: str,
    value: str,
) -> None:
    plan, binding, source = fixture
    report = bind_sglang_report(source, binding, plan)
    report["statistical_report"]["preparation"][field] = value
    report["statistical_report_sha256"] = sha256_digest(
        encoded(report["statistical_report"])
    )
    with pytest.raises(EvaluationError):
        read_sglang_report_bytes(encoded(report))


def test_sglang_writer_uses_its_explicit_output_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import inferdrome.evaluation.sglang_report as report_module
    from inferdrome.evaluation.study import report_study
    from inferdrome.evaluation.study_files import MAX_METADATA_BYTES
    from tests.sglang_dashboard_support import write_sglang_study_report

    # Produce native fake trial files first; only the Markdown rendering size is
    # controlled here. The JSON envelope still passes real binding validation.
    root = tmp_path / "source"
    write_sglang_study_report(root, scenario="HEALTHY")
    config = study("HEALTHY")
    binding = build_sglang_engine_binding(config, profiles())
    assert MAX_SGLANG_REPORT_BYTES > MAX_METADATA_BYTES
    monkeypatch.setattr(
        report_module,
        "render_sglang_markdown",
        lambda *_args: "x" * MAX_SGLANG_REPORT_BYTES,
    )
    destination = tmp_path / "boundary"
    report_study(config, root / "study", destination, engine_binding=binding)
    assert (destination / "report.md").stat().st_size == MAX_SGLANG_REPORT_BYTES
    read_sglang_report_bytes((destination / "report.json").read_bytes())
    monkeypatch.setattr(
        report_module,
        "render_sglang_markdown",
        lambda *_args: "x" * (MAX_SGLANG_REPORT_BYTES + 1),
    )
    rejected = tmp_path / "over-boundary"
    with pytest.raises(EvaluationError):
        report_study(config, root / "study", rejected, engine_binding=binding)
    assert not rejected.exists()
