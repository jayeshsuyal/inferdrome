"""The real-GPU demo rejects package drift before reserving proof output."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.run_real_gpu_demo as demo
from inferdrome.cli import build_parser as build_cli_parser

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _make_tree_writable(root: Path) -> None:
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


def _completed(
    stdout: bytes = b"",
    *,
    returncode: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=[sys.executable, "-m", "pip"],
        returncode=returncode,
        stdout=stdout,
        stderr=b"",
    )


def test_package_inventory_is_canonical_and_runtime_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages = tmp_path / "python-packages.txt"
    retained = b"alpha==1\ninferdrome @ file:///repo\nzeta==2\n"
    packages.write_bytes(retained)
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        if argv[-2:] == ["freeze", "--all"]:
            return _completed(b"zeta==2\nalpha==1\ninferdrome @ file:///repo\n")
        return _completed(b"No broken requirements found.\n")

    monkeypatch.setattr(demo.subprocess, "run", run)

    demo._require_package_environment_unchanged(packages)

    assert calls == [
        [sys.executable, "-m", "pip", "freeze", "--all"],
        [sys.executable, "-m", "pip", "check"],
    ]


def test_package_inventory_drift_fails_before_pip_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages = tmp_path / "python-packages.txt"
    packages.write_bytes(b"alpha==1\n")
    calls = 0

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        assert argv[-2:] == ["freeze", "--all"]
        return _completed(b"alpha==2\n")

    monkeypatch.setattr(demo.subprocess, "run", run)

    with pytest.raises(demo.DemoError, match="changed after host preparation"):
        demo._require_package_environment_unchanged(packages)

    assert calls == 1


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\n",
        b"zeta==2\nalpha==1\n",
        b"alpha==1\nalpha==1\n",
        b"alpha==1\x00\n",
        b"\xff\n",
    ],
)
def test_noncanonical_package_inventory_is_rejected(
    tmp_path: Path,
    content: bytes,
) -> None:
    packages = tmp_path / "python-packages.txt"
    packages.write_bytes(content)

    with pytest.raises(demo.DemoError, match="package inventory"):
        demo._require_package_environment_unchanged(packages)


def test_real_gpu_comparison_assets_are_statically_compatible() -> None:
    pin = demo._check_static_assets()

    assert pin["model_id"] == "Qwen/Qwen2.5-0.5B-Instruct"
    assert demo.COMPARISON_REPETITIONS_PER_ARM == 2
    assert len(demo.COMPARISON_SCHEDULE_SEED) == 64


def test_real_gpu_demo_modes_are_mutually_exclusive() -> None:
    parser = demo.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--check", "--comparison"])


def test_real_gpu_comparison_sources_freeze_one_verified_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plans_root = tmp_path / "comparison-plans"
    namespace = build_cli_parser().parse_args(
        [
            "comparison-plan",
            "create",
            "--baseline-source",
            str(demo.BASELINE_SOURCE_PATH),
            "--candidate-source",
            str(demo.CANDIDATE_SOURCE_PATH),
            "--title",
            "Managed real-GPU concurrency 2 versus 4",
            "--hypothesis",
            "Increasing concurrency changes attempted throughput.",
            "--repetitions",
            str(demo.COMPARISON_REPETITIONS_PER_ARM),
            "--primary-outcome",
            "attempted_request_throughput_per_s:rate",
            "--schedule-seed",
            demo.COMPARISON_SCHEDULE_SEED,
            "--runs-root",
            str(tmp_path / "runs"),
            "--comparison-plans-root",
            str(plans_root),
        ]
    )

    assert namespace.handler(namespace) == 0
    output = json.loads(capsys.readouterr().out)
    plan = demo._comparison_plan_output(
        output,
        comparison_plans_root=plans_root,
    )

    try:
        assert plan.descriptor.independent_variable.baseline_value == 2
        assert plan.descriptor.independent_variable.candidate_value == 4
        assert len(plan.descriptor.ordered_schedule) == 4
    finally:
        _make_tree_writable(tmp_path)


def test_comparison_output_parser_accepts_fresh_and_reused_cli_results(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_source = REPOSITORY_ROOT / "examples" / "controlled-concurrency-2.yaml"
    candidate_source = REPOSITORY_ROOT / "examples" / "controlled-concurrency-4.yaml"
    monkeypatch.setattr(demo, "BASELINE_SOURCE_PATH", baseline_source)
    monkeypatch.setattr(demo, "CANDIDATE_SOURCE_PATH", candidate_source)
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    plans_root = tmp_path / "comparison-plans"
    results_root = tmp_path / "comparison-results"
    parser = build_cli_parser()

    try:
        create = parser.parse_args(
            [
                "comparison-plan",
                "create",
                "--baseline-source",
                str(baseline_source),
                "--candidate-source",
                str(candidate_source),
                "--title",
                "Synthetic proof-driver contract",
                "--hypothesis",
                "Concurrency changes attempted throughput.",
                "--repetitions",
                "2",
                "--primary-outcome",
                "attempted_request_throughput_per_s:rate",
                "--schedule-seed",
                demo.COMPARISON_SCHEDULE_SEED,
                "--runs-root",
                str(runs_root),
                "--comparison-plans-root",
                str(plans_root),
            ]
        )
        assert create.handler(create) == 0
        plan = demo._comparison_plan_output(
            json.loads(capsys.readouterr().out),
            comparison_plans_root=plans_root,
        )
        execution_arguments = [
            "comparison-plan",
            "execute",
            str(plan.path),
            "--expected-digest",
            plan.comparison_plan_digest,
            "--baseline-source",
            str(baseline_source),
            "--candidate-source",
            str(candidate_source),
            "--runs-root",
            str(runs_root),
            "--trial-sets-root",
            str(trial_sets_root),
            "--comparison-results-root",
            str(results_root),
        ]
        execute = parser.parse_args(execution_arguments)
        assert execute.handler(execute) == 0
        scheduled_run_ids = tuple(
            slot.run_id for slot in plan.descriptor.ordered_schedule
        )
        first = demo._comparison_result_output(
            json.loads(capsys.readouterr().out),
            plan=plan,
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            comparison_plans_root=plans_root,
            comparison_results_root=results_root,
            expected_executed_run_ids=scheduled_run_ids,
            expected_reused_run_ids=(),
        )

        resume = parser.parse_args(execution_arguments)
        assert resume.handler(resume) == 0
        second = demo._comparison_result_output(
            json.loads(capsys.readouterr().out),
            plan=plan,
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            comparison_plans_root=plans_root,
            comparison_results_root=results_root,
            expected_executed_run_ids=(),
            expected_reused_run_ids=scheduled_run_ids,
        )

        assert second.comparison_result_digest == first.comparison_result_digest
    finally:
        _make_tree_writable(tmp_path)


def test_customer_bundle_inspection_is_bounded_to_its_workspace(
    tmp_path: Path,
) -> None:
    run_id = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    bundle = tmp_path / "runs" / run_id / "bundle"
    bundle.mkdir(parents=True)
    output = {
        "bundle": {
            "bundle_digest": f"sha256:{'a' * 64}",
            "environment_completeness": "COMPLETE",
            "evidence_eligibility": "CUSTOMER_ELIGIBLE",
            "path": str(bundle),
        },
        "integrity_status": "VALID",
        "run_id": run_id,
        "state": "COMPLETE",
    }

    inspected = demo._inspected_customer_bundle(
        output,
        run_id=run_id,
        runs_root=tmp_path / "runs",
    )

    assert inspected["bundle_path"] == str(bundle)
    assert inspected["evidence_eligibility"] == "CUSTOMER_ELIGIBLE"

    outside = tmp_path / "outside"
    outside.mkdir()
    output["bundle"]["path"] = str(outside)
    with pytest.raises(demo.DemoError, match="outside its proof root"):
        demo._inspected_customer_bundle(
            output,
            run_id=run_id,
            runs_root=tmp_path / "runs",
        )


def test_cli_wrapper_terminates_its_process_group_on_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InterruptingProcess:
        pid = 4242
        returncode: int | None = None
        calls = 0

        def communicate(self, *, timeout: int) -> tuple[bytes, bytes]:
            del timeout
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt
            self.returncode = -signal.SIGTERM
            return b"partial output", b"interrupted"

    process = InterruptingProcess()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        demo.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        demo.os,
        "killpg",
        lambda pid, selected_signal: signals.append((pid, selected_signal)),
    )

    with pytest.raises(demo.DemoError, match="was interrupted"):
        demo._run_cli(
            tmp_path,
            "interrupted-command",
            ["validate", "example.yaml"],
            expect_success=True,
        )

    assert signals == [(process.pid, signal.SIGTERM)]
    assert (tmp_path / "interrupted-command.stdout").read_bytes() == (
        b"partial output"
    )
    assert (tmp_path / "interrupted-command.stderr").read_bytes() == b"interrupted"
