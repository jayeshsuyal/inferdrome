"""Independent scalar expectations for reducer-generated UI fixture reports."""

import json
from pathlib import Path

import pytest

from inferdrome.dashboard.evaluation_reports import EvaluationReportsIndex
from inferdrome.evaluation.engine_binding import build_sglang_engine_binding
from inferdrome.evaluation.sglang_results import load_sglang_trial_bytes
from inferdrome.evaluation.study_config import compile_study
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.evaluation_dashboard_support import write_cache_report, write_study_report
from tests.sglang_dashboard_support import (
    write_engine_catalog,
    write_sglang_study_report,
)
from tests.unit.test_evaluation_engine_binding import profiles, study


def test_censored_fixture_does_not_turn_observed_publication_into_dispatch_recovery(
    tmp_path: Path,
):
    _, report = write_study_report(tmp_path / "censored", variant="censored")
    assert report["status"] == "COMPLETED"
    intervals = report["trials"][1]["recovery"]["intervals"]
    assert intervals["publication"]["duration_ns"] == 0
    assert (
        intervals["decision"]
        == intervals["dispatch"]
        == {
            "status": "UNOBSERVED_OR_CENSORED",
            "duration_ns": None,
            "observation_horizon_ns": 60_000_000,
        }
    )


def test_study_fixture_retains_four_policies_failure_and_actual_recovery(
    tmp_path: Path,
):
    path, report = write_study_report(tmp_path / "study")
    assert path.read_bytes() == canonical_json_bytes(report) + b"\n"
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert report["evidence_eligible"] is False
    assert report["coverage"]["returned_trials"] == 4
    assert report["coverage"]["foreground"]["returned_records"] == 24
    assert report["coverage"]["background"]["returned_records"] == 8
    assert [
        row["mean_goodput_rps"]["decimal"] for row in report["strata"][0]["policies"]
    ] == ["33.333333", "33.333333", "33.333333", "27.777778"]
    fail_closed = report["trials"][3]
    assert fail_closed["foreground"]["outcomes"]["REJECTED_ROUTE"]["count"] == 1
    assert fail_closed["foreground"]["slo_success_fraction"]["decimal"] == "0.833333"
    assert (
        fail_closed["foreground"]["successful_latency_ns"]["scheduled_to_terminal"][
            "p99"
        ]
        is None
    )
    assert fail_closed["recovery"]["actual_restore_ns"] == 100_000_000
    assert fail_closed["recovery"]["intervals"]["publication"] == {
        "duration_ns": 0,
        "status": "OBSERVED",
        "observation_horizon_ns": 70_000_000,
    }


@pytest.mark.parametrize(
    "variant", ["partial", "cancelled", "no-measurements", "warmup-failed"]
)
def test_incomplete_study_fixtures_preserve_absent_and_cancelled_populations(
    tmp_path: Path, variant: str
):
    _, report = write_study_report(tmp_path / variant, variant=variant)
    assert report["comparative_headline"] == "SUPPRESSED_INCOMPLETE_STUDY"
    assert all(
        row["mean_goodput_difference_rps"] is None
        for row in report["strata"][0]["paired_contrasts"]
    )
    if variant == "no-measurements":
        assert report["trials"] == []
        assert report["evidence_class"] == "LOCAL_MEASUREMENT_ONLY"
        assert report["coverage"]["foreground"]["returned_records"] == 0
        assert all(
            row["mean_goodput_rps"] is None for row in report["strata"][0]["policies"]
        )
    elif variant == "partial":
        assert report["coverage"]["foreground"] == {
            "planned_offers": 24,
            "returned_records": 6,
            "offers_in_aborted_trials_without_measurements": 6,
            "offers_in_not_run_trials": 12,
        }
    elif variant == "cancelled":
        assert report["status"] == "CANCELLED"
        assert report["coverage"]["cancelled_results"] == 1
        assert report["trials"][0]["foreground"]["outcomes"]["CANCELLED"]["count"] == 6
    else:
        assert report["coverage"]["warmup_failed_results"] == 1


@pytest.mark.parametrize("block_count", [1, 4, 8])
def test_cache_fixtures_preserve_exact_contrasts_and_replication_floor(
    tmp_path: Path, block_count: int
):
    path, report = write_cache_report(tmp_path / "cache", block_count=block_count)
    assert path.read_bytes() == canonical_json_bytes(report) + b"\n"
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert (
        report["cache_treatment_attribution"]
        == report["runtime_verification"]
        == "UNVERIFIED"
    )
    assert report["workload_verification"] == "SYNTHETIC_TOKENIZER"
    assert report["coverage"]["planned_offers"] == 16 * block_count
    assert [
        row["mean_goodput_difference_rps"]["decimal"] for row in report["contrasts"]
    ] == ["33.333333", "11.111111", "22.222222"]
    for row in report["contrasts"]:
        assert (row["interval_rps"] is None) is (block_count < 8)
    by_condition = {row["condition"]: row for row in report["cells"][:4]}
    assert [
        by_condition[key]["population"]["slo_good_count"]
        for key in ("S0", "S1", "U0", "U1")
    ] == [1, 4, 1, 2]


def test_all_failure_cache_is_measured_zero_while_missing_is_unavailable(
    tmp_path: Path,
):
    _, zero = write_cache_report(tmp_path / "zero", variant="all-failure")
    _, missing = write_cache_report(tmp_path / "missing", variant="no-measurements")
    assert zero["status"] == "COMPLETED" and zero["comparison_status"] == "AVAILABLE"
    assert zero["coverage"]["returned_records"] == 16
    assert all(
        row["population"]["outcomes"]["HTTP_ERROR"]["count"] == 4
        for row in zero["cells"]
    )
    assert all(
        row["population"]["slo_goodput_rps"]["decimal"] == "0.000000"
        for row in zero["cells"]
    )
    assert missing["coverage"]["returned_records"] == 0
    assert missing["comparison_status"] == "SUPPRESSED_INCOMPLETE"
    assert all(row["population"] is None for row in missing["cells"])
    assert all(
        row["mean_goodput_difference_rps"] is None for row in missing["contrasts"]
    )


def test_sglang_fixture_reduces_native_typed_fake_sessions_with_all_offered_records(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sglang"
    path, report = write_sglang_study_report(root)
    plan = compile_study(study("STALE_LOAD"))
    binding = build_sglang_engine_binding(plan, profiles())
    manifest = json.loads((root / "study" / "manifest.json").read_bytes())
    assert path.read_bytes() == canonical_json_bytes(report) + b"\n"
    assert report["dashboard_projection"] == "ENGINE_BOUND_V2"
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert report["runtime_verification"] == "UNVERIFIED"
    assert report["evidence_eligible"] is False
    statistics = report["statistical_report"]
    assert report["statistical_report_sha256"] == sha256_digest(
        canonical_json_bytes(statistics) + b"\n"
    )
    assert statistics["coverage"]["foreground"]["returned_records"] == 24
    assert statistics["coverage"]["background"]["returned_records"] == 8
    assert [
        row["mean_goodput_rps"]["decimal"]
        for row in statistics["strata"][0]["policies"]
    ] == ["33.333333", "33.333333", "33.333333", "27.777778"]
    for trial, entry, summary in zip(
        plan.trials, manifest["trials"], statistics["trials"], strict=True
    ):
        content = (root / "study" / entry["result_filename"]).read_bytes()
        result = load_sglang_trial_bytes(content, plan, trial, binding)
        assert (
            result.result_sha256 == entry["result_sha256"] == summary["result_sha256"]
        )
        raw = result.to_dict()
        assert raw["engine"] == "sglang"
        assert raw["telemetry_source_age"] == "UNAVAILABLE"
        for population in ("foreground", "background"):
            source = getattr(trial.config, population)
            assert raw[population]["evidence_class"] == "SYNTHETIC_ONLY"
            assert [row["request_index"] for row in raw[population]["records"]] == list(
                range(len(source.offers))
            )
            assert [row["scheduled_ns"] for row in raw[population]["records"]] == [
                offer.scheduled_ns for offer in source.offers
            ]
        assert all(
            "reported_running_requests" in row and "running" not in row
            for row in raw["observations"]
        )
    assert (
        statistics["trials"][3]["foreground"]["outcomes"]["REJECTED_ROUTE"]["count"]
        == 1
    )
    for forbidden in (b"private-model", b"private foreground", b"127.0.0.1", b"/opt/"):
        assert forbidden not in path.read_bytes()


def test_sglang_browser_fixture_is_the_authoritative_mixed_catalog_projection(
    tmp_path: Path,
) -> None:
    fixture = write_engine_catalog(tmp_path)
    index = EvaluationReportsIndex(Path(fixture["catalog"]))
    snapshot = index.refresh()
    assert not snapshot.rejected
    assert len(snapshot.reports) == 2
    assert (
        snapshot.model_dump(mode="json")["projection_version"]
        == "inferdrome.evaluation-dashboard.v2"
    )
    legacy, sglang = snapshot.reports
    assert legacy.source_schema == "inferdrome.evaluation-study-report.v1"
    assert "engine_identity" not in legacy.model_dump(mode="json")
    assert sglang.source_schema == "inferdrome.evaluation-study-report.v2"
    projected = index.get_report(sglang.report_id).model_dump(mode="json")
    committed = (
        Path(__file__).resolve().parents[2]
        / "frontend/src/test/evaluations/sglang.json"
    )
    assert json.loads(committed.read_bytes()) == projected
    assert projected["summary"]["engine_identity"]["engine"] == "sglang"
    assert (
        projected["summary"]["engine_identity"]["telemetry_source_age"] == "UNAVAILABLE"
    )
    assert projected["summary"]["runtime_verification"] == "UNVERIFIED"
