"""The namespaced routing-campaign CLI uses the same sealed package boundary."""

import json
import runpy
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN_ROOT = REPOSITORY_ROOT / "campaigns" / "routing-campaign-v1"
PLAN_PATH = CAMPAIGN_ROOT / "stale-load-fresh-health.plan.json"
TRACE_PATH = CAMPAIGN_ROOT / "stale-load-fresh-health.trace.jsonl"
FAULT_SCHEDULE_PATH = CAMPAIGN_ROOT / "stale-load-fresh-health.fault-schedule.json"
TRIAL_PLAN_PATH = CAMPAIGN_ROOT / "trial-plan.json"


def _run_module_cli(
    arguments: list[str],
    *,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    expected_exit_code: int = 0,
) -> tuple[str, str]:
    """Exercise the actual ``python -m inferdrome.routing_campaign`` entrypoint."""

    monkeypatch.setattr(
        sys,
        "argv",
        ["python -m inferdrome.routing_campaign", *arguments],
    )
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("inferdrome.routing_campaign", run_name="__main__")
    assert exited.value.code == expected_exit_code
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_module_cli_runs_verifies_and_inspects_sealed_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "campaign-package"
    run_stdout, run_stderr = _run_module_cli(
        [
            "run",
            "--campaign-plan",
            str(PLAN_PATH),
            "--request-trace",
            str(TRACE_PATH),
            "--fault-schedule",
            str(FAULT_SCHEDULE_PATH),
            "--trial-plan",
            str(TRIAL_PLAN_PATH),
            "--output",
            str(output_path),
        ],
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert run_stderr == ""
    created = json.loads(run_stdout)
    package_path = Path(created["package_path"])
    retained_digest = created["retained_digest"]
    assert package_path.is_dir()
    assert retained_digest.startswith("sha256:")

    verify_stdout, verify_stderr = _run_module_cli(
        ["verify", str(package_path), "--expected-digest", retained_digest],
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert verify_stderr == ""
    verified = json.loads(verify_stdout)
    assert verified["valid"] is True
    assert verified["retained_digest"] == retained_digest

    inspect_stdout, inspect_stderr = _run_module_cli(
        ["inspect", str(package_path)],
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert inspect_stderr == ""
    inspected = json.loads(inspect_stdout)
    assert inspected["retained_digest"] == retained_digest
    assert inspected["verified"] is True
    assert set(inspected["trial_ids"]) == {
        "trial-fail-closed-v1",
        "trial-fail-open-v1",
        "trial-typed-v1",
    }


def test_module_cli_rejects_wrong_retained_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "campaign-package"
    run_stdout, run_stderr = _run_module_cli(
        [
            "run",
            "--campaign-plan",
            str(PLAN_PATH),
            "--request-trace",
            str(TRACE_PATH),
            "--fault-schedule",
            str(FAULT_SCHEDULE_PATH),
            "--trial-plan",
            str(TRIAL_PLAN_PATH),
            "--output",
            str(output_path),
        ],
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert run_stderr == ""
    package_path = Path(json.loads(run_stdout)["package_path"])

    verify_stdout, verify_stderr = _run_module_cli(
        [
            "verify",
            str(package_path),
            "--expected-digest",
            f"sha256:{'0' * 64}",
        ],
        monkeypatch=monkeypatch,
        capsys=capsys,
        expected_exit_code=2,
    )
    assert verify_stdout == ""
    assert "digest" in verify_stderr.lower()
