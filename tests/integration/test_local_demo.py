"""One-command local product demo contract."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def _make_tree_read_only(root: Path) -> None:
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o400)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o500)
        current.chmod(0o500)


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


def _local_demo_module():
    spec = importlib.util.spec_from_file_location("inferdrome_local_demo", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
            "COMPLETE_EQUAL_OBSERVED_ENVIRONMENT",
            "OUTCOME_COVERAGE_AND_SEMANTICS",
        ]
        assert first_comparison["planned_run_count"] == 4
        assert len(first_comparison["executed_run_ids"]) == 4
        assert first_comparison["reused_run_ids"] == []
        first_routing_campaign = first["routing_campaign"]
        assert isinstance(first_routing_campaign, dict)
        assert first_routing_campaign == {
            "campaign_id": "routing-campaign-v1",
            "execution_mode": "SYNTHETIC_CPU_ONLY",
            "retained_digest": first_routing_campaign["retained_digest"],
            "verified_by_replay": True,
        }
        assert isinstance(first_routing_campaign["retained_digest"], str)
        assert first["recording_routes"]["routing_campaigns"] == "/routing-campaigns"
        assert first["recording_routes"]["routing_campaign"] == (
            "/routing-campaigns/routing-campaign-v1"
        )
        assert first["recording_routes"]["routing_qualifications"] == (
            "/routing-qualifications"
        )
        assert first["recording_routes"]["routing_qualification"] == (
            "/routing-qualifications/stale-telemetry-qualification-v1"
        )
        first_routing_qualification = first["routing_qualification"]
        assert isinstance(first_routing_qualification, dict)
        assert first_routing_qualification == {
            "qualification_id": "stale-telemetry-qualification-v1",
            "retained_digest": first_routing_qualification["retained_digest"],
            "source_package_retained_digest": first_routing_campaign["retained_digest"],
            "verified_by_source_replay": True,
            "verified_descriptor_binding": True,
        }
        first_roots = first["roots"]
        assert isinstance(first_roots, dict)
        routing_campaign_root = Path(first_roots["routing_campaign"])
        assert routing_campaign_root.is_dir()
        routing_qualification_root = Path(first_roots["routing_qualification"])
        assert routing_qualification_root.is_dir()

        second = _prepare(workspace)
        second_comparison = second["comparison"]
        assert isinstance(second_comparison, dict)
        assert second_comparison["executed_run_ids"] == []
        assert len(second_comparison["reused_run_ids"]) == 4
        assert (
            second_comparison["comparison_plan_digest"]
            == (first_comparison["comparison_plan_digest"])
        )
        assert (
            second_comparison["comparison_result_digest"]
            == (first_comparison["comparison_result_digest"])
        )
        assert second["routing_campaign"] == first_routing_campaign
        assert second["routing_qualification"] == first_routing_qualification
        assert second["recording_routes"] == first["recording_routes"]

        state_text = (workspace / "demo-state.json").read_text(encoding="utf-8")
        state = json.loads(state_text)
        assert state["claim_boundary"] == "SYNTHETIC_ONLY"
        assert state["routing_campaign"] == {
            "campaign_id": "routing-campaign-v1",
            "package_root": "routing-campaign-v1",
            "retained_digest": first_routing_campaign["retained_digest"],
        }
        assert state["routing_qualification"] == {
            "qualification_id": "stale-telemetry-qualification-v1",
            "source_package_root": "routing-campaign-v1",
            "retained_digest": first_routing_qualification["retained_digest"],
        }
        assert "CUSTOMER_ELIGIBLE" not in state_text
    finally:
        _make_tree_writable(workspace)


def test_local_demo_refuses_a_tampered_retained_routing_campaign(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "tampered-routing-demo"
    try:
        summary = _prepare(workspace)
        roots = summary["roots"]
        assert isinstance(roots, dict)
        package_root = Path(roots["routing_campaign"])
        _make_tree_writable(package_root)
        plan = package_root / "campaign-plan.json"
        plan.write_text('{"tampered":true}\n', encoding="utf-8")
        _make_tree_read_only(package_root)

        with pytest.raises(
            AssertionError,
            match="routing plan violates its strict contract",
        ):
            _prepare(workspace)
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


def test_local_demo_launch_binds_the_verified_qualification_to_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demo = _local_demo_module()
    invoked: dict[str, object] = {}

    def capture_exec(
        executable: str,
        command: list[str],
        environment: dict[str, str],
    ) -> None:
        invoked["executable"] = executable
        invoked["command"] = command
        invoked["environment"] = environment
        raise RuntimeError("stop after command capture")

    monkeypatch.setattr(demo.os, "execvpe", capture_exec)
    summary = {
        "roots": {
            "runs": "/safe/runs",
            "trial_sets": "/safe/trial-sets",
            "comparison_plans": "/safe/comparison-plans",
            "comparison_results": "/safe/comparison-results",
            "routing_campaign": "/safe/routing-campaign",
            "routing_qualification": "/safe/routing-qualification",
        },
        "routing_qualification": {
            "retained_digest": f"sha256:{'a' * 64}",
        },
    }

    with pytest.raises(RuntimeError, match="command capture"):
        demo._launch_dashboard(
            python="python",
            summary=summary,
            port=8787,
            open_browser=False,
        )

    assert invoked["executable"] == "python"
    command = invoked["command"]
    assert isinstance(command, list)
    assert command == [
        "python",
        "-m",
        "inferdrome",
        "dashboard",
        "--runs-root",
        "/safe/runs",
        "--trial-sets-root",
        "/safe/trial-sets",
        "--comparison-plans-root",
        "/safe/comparison-plans",
        "--comparison-results-root",
        "/safe/comparison-results",
        "--routing-campaigns-root",
        "/safe/routing-campaign",
        "--routing-qualification-root",
        "/safe/routing-qualification",
        "--routing-qualification-digest",
        f"sha256:{'a' * 64}",
        "--port",
        "8787",
    ]
