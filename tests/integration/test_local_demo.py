"""One-command local product demo contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "run_local_demo.py"


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


def _prepare(workspace: Path) -> dict[str, object]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(REPOSITORY_ROOT / "src"), existing) if value
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--workspace",
            str(workspace),
            "--prepare-only",
            "--json",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def test_local_demo_prepares_then_reuses_the_exact_verified_schedule(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "local-demo"
    try:
        first = _prepare(workspace)
        first_comparison = first["comparison"]
        assert isinstance(first_comparison, dict)
        assert first["claim_boundary"] == "SYNTHETIC_ONLY"
        assert first["schema_version"] == "inferdrome.local-demo-summary.v1"
        assert first_comparison["status"] == "INCOMPARABLE"
        assert first_comparison["unsatisfied_controls"] == [
            "COMPLETE_EQUAL_OBSERVED_ENVIRONMENT"
        ]
        assert first_comparison["planned_run_count"] == 4
        assert len(first_comparison["executed_run_ids"]) == 4
        assert first_comparison["reused_run_ids"] == []

        second = _prepare(workspace)
        second_comparison = second["comparison"]
        assert isinstance(second_comparison, dict)
        assert second_comparison["executed_run_ids"] == []
        assert len(second_comparison["reused_run_ids"]) == 4
        assert second_comparison["comparison_plan_digest"] == (
            first_comparison["comparison_plan_digest"]
        )
        assert second_comparison["comparison_result_digest"] == (
            first_comparison["comparison_result_digest"]
        )
        assert second["recording_routes"] == first["recording_routes"]

        state_text = (workspace / "demo-state.json").read_text(encoding="utf-8")
        state = json.loads(state_text)
        assert state["claim_boundary"] == "SYNTHETIC_ONLY"
        assert "CUSTOMER_ELIGIBLE" not in state_text
    finally:
        _make_tree_writable(workspace)


def test_local_demo_refuses_to_guess_an_unretained_plan_digest(tmp_path: Path) -> None:
    workspace = tmp_path / "unsafe-demo"
    plan = (
        workspace
        / "comparison-plans"
        / "comparison-plan-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    )
    plan.mkdir(parents=True)
    try:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--workspace",
                str(workspace),
                "--prepare-only",
                "--json",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 1
        assert "will not guess its digest" in completed.stderr
        assert not (workspace / "demo-state.json").exists()
    finally:
        _make_tree_writable(workspace)
