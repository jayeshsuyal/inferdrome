"""Bound synthetic SGLang envelopes; no runtime, cache or artifact attestation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from inferdrome.dashboard.evaluation_projection import project_report
from inferdrome.dashboard.evaluation_reports import (
    CATALOG_LIMIT,
    EvaluationReportNotFound,
    EvaluationReportsIndex,
    _CatalogEntry,
    _report_bytes,
)
from inferdrome.errors import DashboardError, InferdromeError
from inferdrome.evaluation.engine_binding import (
    EvaluationEngineBinding,
    engine_binding_sha256,
    engine_choice_sha256,
)
from inferdrome.evaluation.sglang_report import (
    MAX_SGLANG_REPORT_BYTES,
    bind_sglang_report,
    read_sglang_report_bytes,
)
from inferdrome.evaluation.study_config import CompiledStudy
from inferdrome.evaluation.study_files import MAX_METADATA_BYTES
from inferdrome.limits import WorkLimits
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.dashboard.test_evaluation_reports import _catalog, _client
from tests.evaluation_dashboard_support import write_study_report
from tests.unit.test_evaluation_sglang_report import fixture as fixture


def _write(root: Path, report: dict[str, Any]) -> Path:
    root.mkdir(mode=0o700)
    path = root / "report.json"
    path.write_bytes(canonical_json_bytes(report) + b"\n")
    path.chmod(0o600)
    return path


def test_bound_projection_preserves_statistics_and_engine_identity(
    tmp_path: Path,
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
) -> None:
    plan, binding, source = fixture
    envelope = bind_sglang_report(source, binding, plan)
    path = _write(tmp_path / "private-sentinel", envelope)
    client = _client(tmp_path, _catalog(tmp_path, [("SGLANG_STUDY", path)]))
    response = client.get("/api/v1/evaluation-reports")
    assert response.status_code == 200
    index = response.json()
    assert index["projection_version"] == "inferdrome.evaluation-dashboard.v2"
    summary = index["reports"][0]
    assert summary["label"] == "SGLang study report 1"
    assert summary["source_schema"] == "inferdrome.evaluation-study-report.v2"
    assert summary["report_sha256"] == sha256_digest(path.read_bytes())
    assert summary["report_sha256"] != envelope["statistical_report_sha256"]
    assert summary["config_sha256"] == plan.config_sha256
    assert summary["evidence_class"] == "SYNTHETIC_ONLY"
    assert summary["source_replay"] == "NOT_PERFORMED"
    assert summary["runtime_verification"] == "UNVERIFIED"
    assert summary["evidence_eligible"] is False
    assert summary["tokenizer_reverified_here"] is False
    assert summary["engine_identity"] == {
        "engine": "sglang",
        "producer_version": "0.5.18",
        "profile": "SGLANG_0_5_18_SINGLE_DEVICE_BF16",
        "engine_binding_sha256": engine_binding_sha256(binding),
        "engine_choice_sha256": engine_choice_sha256(binding),
        "telemetry_semantics": "SGLANG_0_5_18_SCHEDULER_GAUGES",
        "telemetry_freshness": "ACQUISITION_START_AGE_ONLY",
        "telemetry_source_age": "UNAVAILABLE",
        "cache_preparation": "WARMUP_DRAIN_FLUSH",
        "cache_state": "DECLARED_COLD",
    }
    response = client.get("/api/v1/evaluation-reports/" + summary["report_id"])
    assert response.status_code == 200
    view = response.json()
    assert view["projection_version"] == "inferdrome.evaluation-dashboard.v2"
    assert view["summary"] == summary
    expected = project_report(
        read_sglang_report_bytes(path.read_bytes()).statistical_report.model_dump(
            mode="json"
        ),
        kind="STUDY",
        digest=summary["report_sha256"],
        entry=1,
    ).model_dump(mode="json")
    for key in ("coverage", "reporting", "limitations", "trials", "strata"):
        assert view[key] == expected[key]
    for private in (
        str(tmp_path),
        "private-sentinel",
        "127.0.0.1",
        "/opt/",
        "private-model",
        '"engine_binding":',
        '"prompt":',
    ):
        assert private not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_mixed_index_preserves_legacy_summary_and_detail_bytes(
    tmp_path: Path,
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
) -> None:
    legacy, _ = write_study_report(tmp_path / "legacy")
    catalog = _catalog(tmp_path, [("STUDY", legacy)])
    client = _client(tmp_path, catalog)
    old_index = client.get("/api/v1/evaluation-reports").json()
    old_summary = old_index["reports"][0]
    route = "/api/v1/evaluation-reports/" + old_summary["report_id"]
    old_detail = client.get(route).content
    assert old_index["projection_version"] == "inferdrome.evaluation-dashboard.v1"
    assert "engine_identity" not in old_summary
    plan, binding, source = fixture
    bound = _write(tmp_path / "bound", bind_sglang_report(source, binding, plan))
    _catalog(tmp_path, [("STUDY", legacy), ("SGLANG_STUDY", bound)])
    mixed = client.get("/api/v1/evaluation-reports").json()
    assert mixed["projection_version"] == "inferdrome.evaluation-dashboard.v2"
    assert mixed["reports"][0] == old_summary
    assert client.get(route).content == old_detail
    _catalog(tmp_path, [("STUDY", legacy)])
    assert client.get("/api/v1/evaluation-reports").json() == old_index


@pytest.mark.parametrize(
    "mutation",
    [
        "legacy-kind",
        "nested-legacy",
        "historical-unsupported",
        "missing-binding",
        "component-digest",
        "cross-plan",
        "semantics",
        "evidence",
        "missing-default",
        "preparation-warm",
        "preparation-prefix",
        "preparation-image",
    ],
)
def test_unsupported_or_invalid_envelopes_are_withheld(
    tmp_path: Path,
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
    mutation: str,
) -> None:
    plan, binding, source = fixture
    report = bind_sglang_report(source, binding, plan)
    kind = "SGLANG_STUDY"
    if mutation == "legacy-kind":
        kind = "STUDY"
    elif mutation == "nested-legacy":
        report = deepcopy(source)
    elif mutation == "historical-unsupported":
        report["dashboard_projection"] = "UNSUPPORTED_ENGINE_BINDING"
    elif mutation == "missing-binding":
        del report["engine_binding"]
    elif mutation == "component-digest":
        report["statistical_report_sha256"] = "sha256:" + "0" * 64
    elif mutation == "evidence":
        report["evidence_eligible"] = True
    elif mutation.startswith("preparation-"):
        field, value = {
            "preparation-warm": ("cache_state", "DECLARED_WARM"),
            "preparation-prefix": ("prefix_caching", "DECLARED_DISABLED"),
            "preparation-image": ("serving_image_reference", "sha256:" + "f" * 64),
        }[mutation]
        report["statistical_report"]["preparation"][field] = value
        report["statistical_report_sha256"] = sha256_digest(
            canonical_json_bytes(report["statistical_report"]) + b"\n"
        )
    else:
        if mutation == "cross-plan":
            report["engine_binding"]["plan_sha256"] = "sha256:" + "f" * 64
        elif mutation == "semantics":
            report["engine_binding"]["telemetry_semantics"] = "VLLM_LOAD"
        else:
            del report["engine_binding"]["evidence_eligible"]
        report["engine_binding_sha256"] = sha256_digest(
            canonical_json_bytes(report["engine_binding"]) + b"\n"
        )
    path = _write(tmp_path / "report", report)
    response = _client(tmp_path, _catalog(tmp_path, [(kind, path)])).get(
        "/api/v1/evaluation-reports"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reports"] == []
    assert body["projection_version"] == "inferdrome.evaluation-dashboard.v1"
    assert body["rejected"] == [
        {
            "entry": 1,
            "code": "REPORT_INVALID",
            "message": "Configured report was withheld.",
        }
    ]
    assert str(tmp_path) not in response.text


def test_digest_change_removes_previously_valid_sglang_report(
    tmp_path: Path,
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
) -> None:
    plan, binding, source = fixture
    path = _write(tmp_path / "report", bind_sglang_report(source, binding, plan))
    index = EvaluationReportsIndex(_catalog(tmp_path, [("SGLANG_STUDY", path)]))
    report_id = index.refresh().reports[0].report_id
    path.write_bytes(path.read_bytes() + b"\n")
    assert index.refresh().rejected[0].code == "DIGEST_MISMATCH"
    with pytest.raises(EvaluationReportNotFound):
        index.get_report(report_id)


@pytest.mark.parametrize(
    "kind,limit",
    [("STUDY", MAX_METADATA_BYTES), ("SGLANG_STUDY", MAX_SGLANG_REPORT_BYTES)],
)
def test_catalog_kind_selects_exact_private_file_read_bound(
    tmp_path: Path,
    kind: str,
    limit: int,
) -> None:
    root = tmp_path / "report"
    root.mkdir(mode=0o700)
    path = root / "report.json"
    path.write_bytes(b" " * limit)
    path.chmod(0o600)
    entry = _CatalogEntry.model_validate(
        {
            "kind": kind,
            "report_path": str(path),
            "expected_sha256": "sha256:" + "0" * 64,
        }
    )
    assert len(_report_bytes(entry)) == limit
    path.write_bytes(b" " * (limit + 1))
    with pytest.raises((OSError, ValueError, InferdromeError)):
        _report_bytes(entry)


def test_sglang_reserves_its_full_work_allowance_before_reading(
    tmp_path: Path,
    fixture: tuple[CompiledStudy, EvaluationEngineBinding, dict[str, Any]],
) -> None:
    plan, binding, source = fixture
    path = _write(tmp_path / "report", bind_sglang_report(source, binding, plan))
    catalog = _catalog(tmp_path, [("SGLANG_STUDY", path)])
    legacy_budget = WorkLimits(
        max_units=1, max_bytes=CATALOG_LIMIT + MAX_METADATA_BYTES, max_seconds=30
    )
    with pytest.raises(DashboardError):
        EvaluationReportsIndex(catalog, work_limits=legacy_budget).refresh()
    bound_budget = WorkLimits(
        max_units=1, max_bytes=CATALOG_LIMIT + MAX_SGLANG_REPORT_BYTES, max_seconds=30
    )
    assert (
        len(EvaluationReportsIndex(catalog, work_limits=bound_budget).refresh().reports)
        == 1
    )
