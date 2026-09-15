"""Pinned-report projection, private-file, auth and bounded-work contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inferdrome.dashboard.api import create_app
from inferdrome.dashboard.auth import DashboardKeyringStore
from inferdrome.dashboard.evaluation_reports import (
    CATALOG_LIMIT,
    EvaluationReportNotFound,
    EvaluationReportsIndex,
)
from inferdrome.errors import DashboardError
from inferdrome.limits import WorkLimits
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.evaluation_dashboard_support import write_cache_report, write_study_report


def _catalog(root: Path, paths: list[tuple[str, Path]]) -> Path:
    root.chmod(0o700)
    catalog = root / "catalog.json"
    catalog.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.dashboard-evaluation-reports-catalog.v1",
                "entries": [
                    {
                        "kind": kind,
                        "report_path": str(path),
                        "expected_sha256": sha256_digest(path.read_bytes()),
                    }
                    for kind, path in paths
                ],
            }
        )
        + b"\n"
    )
    catalog.chmod(0o600)
    return catalog


def _client(tmp_path: Path, catalog: Path | None) -> TestClient:
    return TestClient(
        create_app(runs_root=tmp_path / "runs", evaluation_reports_catalog=catalog)
    )


def _value(metrics: list[dict[str, Any]], key: str) -> str | None:
    return next(item["value"] for item in metrics if item["key"] == key)


def test_study_projection_exact_values_populations_and_provenance(
    tmp_path: Path,
) -> None:
    path, source = write_study_report(tmp_path / "private-sentinel")
    client = _client(tmp_path, _catalog(tmp_path, [("STUDY", path)]))
    listed = client.get("/api/v1/evaluation-reports")
    assert listed.status_code == 200
    summary = listed.json()["reports"][0]
    response = client.get("/api/v1/evaluation-reports/" + summary["report_id"])
    assert response.status_code == 200
    view = response.json()
    assert view["summary"]["report_sha256"] == sha256_digest(path.read_bytes())
    assert summary["source_replay"] == "NOT_PERFORMED"
    assert summary["report_integrity"] == "EXPECTED_DIGEST_MATCH"
    assert summary["report_contract"] == "VALIDATED"
    assert summary["evidence_class"] == "SYNTHETIC_ONLY"
    assert summary["runtime_verification"] == "UNVERIFIED"
    assert summary["evidence_eligible"] is False
    assert summary["tokenizer_reverified_here"] is False
    trial = next(
        row for row in view["trials"] if row["policy_id"] == "evaluation_fail_closed_v1"
    )
    original = next(
        row
        for row in source["trials"]
        if row["policy_id"] == "evaluation_fail_closed_v1"
    )
    assert _value(trial["foreground"]["metrics"], "slo_goodput_rps") == "27.777778"
    assert _value(trial["foreground"]["metrics"], "slo_success_fraction") == "0.833333"
    assert original["foreground"]["slo_goodput_rps"]["decimal"] == "27.777778"
    for projected, actual in zip(view["trials"], source["trials"], strict=True):
        for key in (
            "slo_goodput_rps",
            "slo_success_fraction",
            "offered_rate_rps",
            "dispatch_rate_rps",
        ):
            assert (
                _value(projected["foreground"]["metrics"], key)
                == actual["foreground"][key]["decimal"]
            )
        assert (
            projected["foreground"]["latency"][0]["population"]
            == "ALL_SUCCESS_INCLUDING_SLO_MISSES"
        )
        assert projected["foreground"]["latency"][0]["p99_ns"] is None
        assert (
            projected["foreground"]["latency"][0]["p99_status"]
            == "BELOW_REPORTING_FLOOR"
        )
    publication = next(
        row
        for row in view["trials"][0]["recovery"]["intervals"]
        if row["metric"] == "publication"
    )
    assert publication["duration_ns"] == "0"
    assert publication["status"] == "OBSERVED"
    for forbidden in (
        str(tmp_path),
        "private-sentinel",
        "report_path",
        "preparation",
        "workload_seed",
        "attempt_id",
        "http://",
        "prompt",
        "token_ids",
    ):
        # Aggregate prompt token counts are allowed; raw prompt fields are not.
        if forbidden == "prompt":
            assert '"prompt":' not in response.text
        else:
            assert forbidden not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("blocks", [1, 4, 8])
def test_cache_projection_copies_matched_values_and_interval_gate(
    tmp_path: Path, blocks: int
) -> None:
    path, source = write_cache_report(tmp_path / "cache", block_count=blocks)
    index = EvaluationReportsIndex(_catalog(tmp_path, [("PREFIX_CACHE", path)]))
    view = index.get_report(index.refresh().reports[0].report_id).model_dump(
        mode="json"
    )
    assert view["summary"]["comparison_status"] == "AVAILABLE"
    assert view["cache_treatment_attribution"] == "UNVERIFIED"
    assert view["workload_verification"] == "SYNTHETIC_TOKENIZER"
    assert [row["mean_rps"] for row in view["contrasts"]] == [
        "33.333333",
        "11.111111",
        "22.222222",
    ]
    assert len(view["blocks"]) == blocks
    assert view["low_replication"] is (blocks < 8)
    for result, actual in zip(view["contrasts"], source["contrasts"], strict=True):
        assert result["mean_rps"] == actual["mean_goodput_difference_rps"]["decimal"]
        assert (result["lower_rps"] is None) is (blocks < 8)
        assert result["interval_status"] == actual["interval_status"]
    cells = view["blocks"][0]["cells"]
    assert [cell["condition"] for cell in cells] == ["S0", "S1", "U0", "U1"]
    assert cells[0]["config_sha256"] == cells[1]["config_sha256"]
    assert cells[0]["index"] != cells[1]["index"]
    assert _value(cells[1]["population"]["metrics"], "slo_goodput_rps") == "44.444444"


@pytest.mark.parametrize(
    "kind,variant",
    [
        ("STUDY", "cancelled"),
        ("STUDY", "no-measurements"),
        ("PREFIX_CACHE", "invalid"),
        ("PREFIX_CACHE", "missing"),
        ("PREFIX_CACHE", "cancelled"),
        ("PREFIX_CACHE", "all-failure"),
    ],
)
def test_partial_invalid_cells_and_zero_are_not_promoted(
    tmp_path: Path, kind: str, variant: str
) -> None:
    writer = write_study_report if kind == "STUDY" else write_cache_report
    path, source = writer(tmp_path / "report", variant=variant)
    index = EvaluationReportsIndex(_catalog(tmp_path, [(kind, path)]))
    listed = index.refresh()
    assert not listed.rejected
    view = index.get_report(listed.reports[0].report_id).model_dump(mode="json")
    assert view["summary"]["status"] == source["status"]
    if variant == "no-measurements":
        assert view["summary"]["returned_records"] == 0
        assert view["summary"]["evidence_class"] == "LOCAL_MEASUREMENT_ONLY"
        assert view["trials"] == []
    elif variant == "all-failure":
        assert view["summary"]["comparison_status"] == "AVAILABLE"
        assert (
            _value(
                view["blocks"][0]["cells"][0]["population"]["metrics"],
                "slo_goodput_rps",
            )
            == "0.000000"
        )
    elif kind == "PREFIX_CACHE":
        assert view["summary"]["comparison_status"] == "SUPPRESSED_INCOMPLETE"
        assert all(row["mean_rps"] is None for row in view["contrasts"])


def test_deleted_changed_and_replaced_sources_invalidate_detail(tmp_path: Path) -> None:
    path, _ = write_cache_report(tmp_path / "source")
    client = _client(tmp_path, _catalog(tmp_path, [("PREFIX_CACHE", path)]))
    report_id = client.get("/api/v1/evaluation-reports").json()["reports"][0][
        "report_id"
    ]
    url = "/api/v1/evaluation-reports/" + report_id
    saved = path.read_bytes()
    assert client.get(url).status_code == 200
    path.unlink()
    assert client.get(url).status_code == 404
    path.write_bytes(saved + b" ")
    path.chmod(0o600)
    assert client.get(url).status_code == 404
    assert (
        client.get("/api/v1/evaluation-reports").json()["rejected"][0]["code"]
        == "DIGEST_MISMATCH"
    )
    path.write_bytes(saved)
    assert client.get(url).status_code == 200


@pytest.mark.parametrize(
    "unsafe",
    ["symlink", "hardlink", "fifo", "directory", "world-readable", "parent-symlink"],
)
def test_unsafe_report_files_are_withheld(tmp_path: Path, unsafe: str) -> None:
    path, _ = write_cache_report(tmp_path / "source")
    catalog = _catalog(tmp_path, [("PREFIX_CACHE", path)])
    if unsafe == "world-readable":
        path.chmod(0o644)
    elif unsafe == "parent-symlink":
        path.parent.rename(tmp_path / "moved")
        path.parent.symlink_to(tmp_path / "moved", target_is_directory=True)
    else:
        target = tmp_path / "target"
        path.rename(target)
        if unsafe == "symlink":
            path.symlink_to(target)
        elif unsafe == "hardlink":
            os.link(target, path)
        elif unsafe == "fifo":
            os.mkfifo(path, 0o600)
        else:
            path.mkdir(mode=0o700)
    result = EvaluationReportsIndex(catalog).refresh()
    assert result.reports == ()
    assert result.rejected[0].code == "REPORT_UNAVAILABLE"


@pytest.mark.parametrize(
    "mutation", ["duplicate", "relative", "extra", "bad-digest", "wrong-kind", "nine"]
)
def test_catalog_entry_contracts_fail_closed(tmp_path: Path, mutation: str) -> None:
    path, _ = write_cache_report(tmp_path / "source")
    catalog = _catalog(tmp_path, [("PREFIX_CACHE", path)])
    data = json.loads(catalog.read_bytes())
    row = data["entries"][0]
    if mutation == "duplicate":
        data["entries"].append(dict(row))
    elif mutation == "relative":
        row["report_path"] = "source/report.json"
    elif mutation == "extra":
        row["secret"] = "private-sentinel"
    elif mutation == "bad-digest":
        row["expected_sha256"] = "invalid"
    elif mutation == "wrong-kind":
        row["kind"] = "STUDY"
    else:
        data["entries"] *= 9
    catalog.write_bytes(canonical_json_bytes(data))
    client = _client(tmp_path, catalog)
    response = client.get("/api/v1/evaluation-reports")
    if mutation == "nine":
        assert response.status_code == 503
    else:
        assert response.status_code == 200
        assert response.json()["reports"] == []
        assert response.json()["rejected"]
    assert "private-sentinel" not in response.text
    assert str(tmp_path) not in response.text


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":"x","schema_version":"y","entries":[]}',
        b'{"schema_version":"x","entries":[NaN]}',
        b"[" * 40,
        b" " * (CATALOG_LIMIT + 1),
    ],
)
def test_invalid_outer_catalog_is_generic_unavailable(
    tmp_path: Path, raw: bytes
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_bytes(raw)
    catalog.chmod(0o600)
    response = _client(tmp_path, catalog).get("/api/v1/evaluation-reports")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Evaluation reports are temporarily unavailable."
    }


def test_empty_disabled_catalog_and_bad_id(tmp_path: Path) -> None:
    for catalog in (None, _catalog(tmp_path, [])):
        index = EvaluationReportsIndex(catalog)
        assert index.refresh().reports == ()
        with pytest.raises(EvaluationReportNotFound):
            index.get_report("../../private")


def test_auth_precedes_work_and_revocation_relocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = write_cache_report(tmp_path / "source")
    index = EvaluationReportsIndex(_catalog(tmp_path, [("PREFIX_CACHE", path)]))
    keyring = DashboardKeyringStore(tmp_path / "keyring.json")
    token, record = keyring.create("evaluation-test")
    client = TestClient(
        create_app(
            runs_root=tmp_path / "runs",
            evaluation_report_index=index,
            keyring_path=keyring.path,
        )
    )
    original = index._scan

    def fail() -> Any:
        raise AssertionError("work before authentication")

    monkeypatch.setattr(index, "_scan", fail)
    for url in (
        "/api/v1/evaluation-reports",
        "/api/v1/evaluation-reports/ev-" + "0" * 64,
    ):
        assert client.get(url).status_code == 401
    monkeypatch.setattr(index, "_scan", original)
    headers = {"Authorization": "Bearer " + token}
    assert client.get("/api/v1/evaluation-reports", headers=headers).status_code == 200
    keyring.revoke(record.key_id)
    monkeypatch.setattr(index, "_scan", fail)
    assert client.get("/api/v1/evaluation-reports", headers=headers).status_code == 401


def test_work_exhaustion_and_concurrent_admission_are_bounded(tmp_path: Path) -> None:
    path, _ = write_cache_report(tmp_path / "source")
    catalog = _catalog(tmp_path, [("PREFIX_CACHE", path)])
    limits = WorkLimits(max_units=1, max_bytes=CATALOG_LIMIT, max_seconds=1)
    index = EvaluationReportsIndex(catalog, work_limits=limits)
    with pytest.raises(DashboardError):
        index.refresh()
    index = EvaluationReportsIndex(catalog)
    with index._controller.session(index.work_limits), pytest.raises(DashboardError):
        index.refresh()
    now = [0.0]
    index = EvaluationReportsIndex(catalog, clock=lambda: now.pop(0) if now else 100.0)
    with pytest.raises(DashboardError):
        index.refresh()


def test_viewer_does_not_execute_replay_tokenize_write_or_open_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = write_cache_report(tmp_path / "source")
    index = EvaluationReportsIndex(_catalog(tmp_path, [("PREFIX_CACHE", path)]))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("forbidden viewer work")

    for name in (
        "inferdrome.evaluation.cache.run_cache_cell",
        "inferdrome.evaluation.runner.run_evaluation",
        "inferdrome.evaluation.study.report_study",
        "inferdrome.evaluation.cache_report.report_cache_experiment",
        "inferdrome.evaluation.cache_workload.verify_cache_workloads",
        "socket.create_connection",
        "zipfile.ZipFile",
        "tarfile.open",
        "pathlib.Path.write_bytes",
        "pathlib.Path.write_text",
        "os.write",
    ):
        monkeypatch.setattr(name, forbidden)
    assert len(index.refresh().reports) == 1


def test_api_is_read_only_and_schema_has_no_private_configuration(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, None)
    assert (
        client.post(
            "/api/v1/evaluation-reports", json={"report_path": "/private"}
        ).status_code
        == 405
    )
    assert (
        client.get(
            "/api/v1/evaluation-reports", headers={"Host": "evil.invalid"}
        ).status_code
        == 400
    )
    schema = client.get("/api/v1/openapi.json").json()
    models = schema["components"]["schemas"]
    assert models["EvaluationSummary"]["additionalProperties"] is False
    assert models["EvaluationReportIndex"]["properties"]["reports"]["maxItems"] == 8
    for name, contract in models.items():
        if name.startswith("Evaluation"):
            assert "report_path" not in json.dumps(contract)
            assert "expected_sha256" not in json.dumps(contract)


@pytest.mark.parametrize("replacement", ["file", "directory", "same-file-edit"])
def test_report_replacement_during_held_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    path, _ = write_cache_report(tmp_path / "source")
    catalog = _catalog(tmp_path, [("PREFIX_CACHE", path)])
    saved = path.read_bytes()
    inode = path.stat().st_ino
    original = os.read
    changed = False

    def replace_during_read(descriptor: int, amount: int) -> bytes:
        nonlocal changed
        chunk = original(descriptor, amount)
        if not changed and os.fstat(descriptor).st_ino == inode:
            changed = True
            if replacement == "directory":
                path.parent.rename(tmp_path / "old-source")
                path.parent.mkdir(mode=0o700)
            elif replacement == "file":
                path.rename(path.with_name("old-report.json"))
            path.write_bytes(saved if replacement != "same-file-edit" else saved + b" ")
            path.chmod(0o600)
        return chunk

    monkeypatch.setattr(os, "read", replace_during_read)
    result = EvaluationReportsIndex(catalog).refresh()
    assert changed
    assert result.reports == ()
    assert result.rejected[0].code == "REPORT_UNAVAILABLE"


def test_repinning_inconsistent_report_does_not_bypass_contract(
    tmp_path: Path,
) -> None:
    path, source = write_cache_report(tmp_path / "source", variant="missing")
    source["comparison_status"] = "AVAILABLE"
    path.chmod(0o600)
    path.write_bytes(canonical_json_bytes(source) + b"\n")
    index = EvaluationReportsIndex(_catalog(tmp_path, [("PREFIX_CACHE", path)]))
    result = index.refresh()
    assert result.reports == ()
    assert result.rejected[0].code == "REPORT_INVALID"


def test_cli_and_server_wire_catalog_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inferdrome.cli import main

    captured: list[dict[str, Any]] = []

    def capture(*args: Any, **kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr("inferdrome.dashboard.server.run_dashboard", capture)
    path = tmp_path / "catalog.json"
    assert main(["dashboard", "--evaluation-reports-catalog", str(path)]) == 0
    assert captured[0]["evaluation_reports_catalog"] == path
