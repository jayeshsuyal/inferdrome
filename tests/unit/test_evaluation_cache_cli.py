"""Offline cache CLI paths and explicit missing-tokenizer behavior."""

import json
from pathlib import Path

import pytest

import inferdrome.evaluation.cache as cache_module
from inferdrome.evaluation.cli import main
from tests.unit.test_evaluation_cache import cache_payload


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def forbidden(_config: object) -> None:
        pytest.fail("an offline CLI path attempted to construct a network client")

    monkeypatch.setattr(cache_module, "AiohttpTransport", forbidden)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cache_payload()))
    return path


def test_plan_is_explicitly_unavailable_without_local_tokenizer(
    config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "plan.json"
    assert (
        main(["cache-plan", "--config", str(config_path), "--output", str(destination)])
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    plan = json.loads(destination.read_bytes())
    assert output["verification_status"] == plan["verification_status"] == "UNAVAILABLE"
    assert not output["executable"] and not plan["executable"]
    assert plan["verification"] is None and len(plan["cells"]) == 4
    assert plan["evidence_eligible"] is False


def test_plan_does_not_overwrite_an_existing_output(
    config_path: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "plan.json"
    destination.write_bytes(b"retained")
    assert (
        main(["cache-plan", "--config", str(config_path), "--output", str(destination)])
        == 2
    )
    assert destination.read_bytes() == b"retained"


def test_run_rejects_unavailable_tokenizer_before_preparation_or_network(
    config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "cell"
    assert (
        main(
            [
                "cache-run-cell",
                "--config",
                str(config_path),
                "--cell-id",
                "cell-0000",
                "--preparation",
                str(tmp_path / "not-read.json"),
                "--output-dir",
                str(destination),
            ]
        )
        == 2
    )
    assert "tokenization is unavailable" in capsys.readouterr().err
    assert not destination.exists()


def test_offline_report_preserves_missing_cell_coverage(
    config_path: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "report"
    assert (
        main(
            [
                "cache-report",
                "--config",
                str(config_path),
                "--output-dir",
                str(destination),
            ]
        )
        == 0
    )
    report = json.loads((destination / "report.json").read_bytes())
    assert report["comparison_status"] == "SUPPRESSED_INCOMPLETE"
    assert report["workload_verification"] == "UNAVAILABLE"
    assert report["coverage"]["missing_cells"] == 4
    assert report["coverage"]["returned_records"] == 0
    assert report["coverage"]["offers_without_measurements"] == 16
    assert all(contrast["interval_rps"] is None for contrast in report["contrasts"])


@pytest.mark.parametrize(
    "mappings",
    [
        ["cell-0000=/not-read", "cell-0000=/also-not-read"],
        ["cell-9999=/not-read"],
        ["cell-0000="],
        ["cell-0000"],
    ],
)
def test_report_rejects_ambiguous_input_mapping(
    config_path: Path, tmp_path: Path, mappings: list[str]
) -> None:
    args = [
        "cache-report",
        "--config",
        str(config_path),
        "--output-dir",
        str(tmp_path / "report"),
    ]
    for mapping in mappings:
        args.extend(("--cell-input", mapping))
    assert main(args) == 2
    assert not (tmp_path / "report").exists()
