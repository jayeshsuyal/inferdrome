"""The namespaced qualification CLI composes only local R1 machinery."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

from inferdrome.routing_campaign import run_campaign

_ROOT = Path(__file__).resolve().parents[2]
_INPUTS = _ROOT / "campaigns" / "routing-campaign-v1"


def _run_module_cli(
    arguments: list[str],
    *,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    expected_exit_code: int = 0,
) -> tuple[str, str]:
    monkeypatch.setattr(
        sys,
        "argv",
        ["python -m inferdrome.routing_qualification", *arguments],
    )
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("inferdrome.routing_qualification", run_name="__main__")
    assert exited.value.code == expected_exit_code
    captured = capsys.readouterr()
    return captured.out, captured.err


def _run_source(output: Path):
    return run_campaign(
        _INPUTS / "stale-load-fresh-health.plan.json",
        _INPUTS / "stale-load-fresh-health.trace.jsonl",
        _INPUTS / "stale-load-fresh-health.fault-schedule.json",
        _INPUTS / "trial-plan.json",
        output,
    )


def test_module_cli_runs_verifies_and_inspects_the_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    campaign_output = tmp_path / "campaign"
    qualification_root = tmp_path / "qualification"
    run_stdout, run_stderr = _run_module_cli(
        [
            "run",
            "--campaign-plan",
            str(_INPUTS / "stale-load-fresh-health.plan.json"),
            "--request-trace",
            str(_INPUTS / "stale-load-fresh-health.trace.jsonl"),
            "--fault-schedule",
            str(_INPUTS / "stale-load-fresh-health.fault-schedule.json"),
            "--trial-plan",
            str(_INPUTS / "trial-plan.json"),
            "--campaign-output",
            str(campaign_output),
            "--qualification-output-root",
            str(qualification_root),
        ],
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert run_stderr == ""
    created = json.loads(run_stdout)
    assert Path(created["campaign_package"]).is_dir()
    assert Path(created["qualification_directory"]).is_dir()
    assert created["population_accounting"] == "SEPARATE_PER_TRIAL_NO_POOLING"
    assert len(created["trials"]) == 3

    verify_stdout, verify_stderr = _run_module_cli(
        [
            "verify",
            "--campaign-package",
            created["campaign_package"],
            "--qualification-root",
            str(qualification_root),
            "--expected-qualification-digest",
            created["qualification_retained_digest"],
        ],
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert verify_stderr == ""
    verified = json.loads(verify_stdout)
    assert verified["valid"] is True
    assert verified["qualification_retained_digest"] == (
        created["qualification_retained_digest"]
    )

    inspect_stdout, inspect_stderr = _run_module_cli(
        [
            "inspect",
            "--campaign-package",
            created["campaign_package"],
            "--qualification-root",
            str(qualification_root),
            "--expected-qualification-digest",
            created["qualification_retained_digest"],
        ],
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert inspect_stderr == ""
    inspected = json.loads(inspect_stdout)
    assert "valid" not in inspected
    assert inspected["fault_timeline"]["load_observer_pause_at_ms"] == 15
    assert [trial["trial_id"] for trial in inspected["trials"]] == [
        "trial-fail-closed-v1",
        "trial-fail-open-v1",
        "trial-typed-v1",
    ]


def test_module_cli_capture_requires_source_digest_and_rejects_wrong_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _run_source(tmp_path / "campaign")
    qualification_root = tmp_path / "qualification"
    capture_stdout, capture_stderr = _run_module_cli(
        [
            "capture",
            "--campaign-package",
            str(source.path),
            "--expected-source-digest",
            source.retained_digest,
            "--qualification-output-root",
            str(qualification_root),
        ],
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert capture_stderr == ""
    captured = json.loads(capture_stdout)
    assert captured["source_package_retained_digest"] == source.retained_digest

    verify_stdout, verify_stderr = _run_module_cli(
        [
            "verify",
            "--campaign-package",
            str(source.path),
            "--qualification-root",
            str(qualification_root),
            "--expected-qualification-digest",
            f"sha256:{'0' * 64}",
        ],
        monkeypatch=monkeypatch,
        capsys=capsys,
        expected_exit_code=2,
    )
    assert verify_stdout == ""
    assert "digest" in verify_stderr.lower()
