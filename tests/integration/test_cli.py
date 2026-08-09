"""The public CLI exercises the same verified library boundaries."""

import json
import shutil
from pathlib import Path

import pytest

from inferdrome.cli import main

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "run-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
SECOND_RUN_ID = "run-ffffffffffffffffffffffffffffffff"


def test_cli_fake_run_inspect_verify_reduce_and_summarize(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root = tmp_path / "runs"
    assert (
        main(
            [
                "run",
                str(REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"),
                "--runs-root",
                str(runs_root),
                "--run-id",
                RUN_ID,
            ]
        )
        == 0
    )
    run_output = json.loads(capsys.readouterr().out)
    bundle_path = run_output["bundle_path"]
    bundle_digest = run_output["bundle_digest"]
    assert run_output["evidence_eligibility"] == "SYNTHETIC_ONLY"

    assert main(["inspect", RUN_ID, "--runs-root", str(runs_root)]) == 0
    inspect_output = json.loads(capsys.readouterr().out)
    assert inspect_output["state"] == "COMPLETE"
    assert inspect_output["bundle"]["bundle_digest"] == bundle_digest

    assert (
        main(
            [
                "bundle",
                "verify",
                bundle_path,
                "--expected-digest",
                bundle_digest,
            ]
        )
        == 0
    )
    verify_output = json.loads(capsys.readouterr().out)
    assert verify_output["valid"] is True

    assert main(["reduce", bundle_path]) == 0
    reduced = json.loads(capsys.readouterr().out)
    assert reduced["schema_version"] == "inferdrome.measurements.v1"

    assert main(["summarize", bundle_path]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["run_id"] == RUN_ID
    assert any(
        item["metric"] == "measured_request_count" and item["value"] == 2
        for item in summary["measurements"]
    )


def test_cli_rejects_wrong_external_digest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root = tmp_path / "runs"
    assert (
        main(
            [
                "run",
                str(REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"),
                "--runs-root",
                str(runs_root),
                "--run-id",
                RUN_ID,
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    assert (
        main(
            [
                "bundle",
                "verify",
                output["bundle_path"],
                "--expected-digest",
                f"sha256:{'0' * 64}",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bundle digest does not match retained digest" in captured.err


def test_cli_inspect_rejects_complete_workspace_without_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root = tmp_path / "runs"
    assert (
        main(
            [
                "run",
                str(REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"),
                "--runs-root",
                str(runs_root),
                "--run-id",
                RUN_ID,
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    bundle_path = Path(output["bundle_path"])
    bundle_path.rename(bundle_path.with_name("bundle-hidden"))

    assert main(["inspect", RUN_ID, "--runs-root", str(runs_root)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "complete workspace is missing its sealed bundle" in captured.err


def test_cli_customer_flow_rejects_synthetic_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root = tmp_path / "runs"
    assert (
        main(
            [
                "run",
                str(REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"),
                "--runs-root",
                str(runs_root),
                "--run-id",
                RUN_ID,
            ]
        )
        == 0
    )
    bundle_path = json.loads(capsys.readouterr().out)["bundle_path"]

    assert (
        main(
            [
                "bundle",
                "verify",
                bundle_path,
                "--require-customer-eligible",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bundle is not customer-eligible" in captured.err


def test_cli_managed_options_fail_before_reserving_fake_workspace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root = tmp_path / "runs"
    assert (
        main(
            [
                "run",
                str(REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"),
                "--runs-root",
                str(runs_root),
                "--managed-local-vllm",
                "--managed-model-path",
                str(tmp_path),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "managed vLLM is only valid for attached execution" in captured.err
    assert not runs_root.exists()


def test_cli_trial_set_create_verify_and_summarize(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    source = str(REPOSITORY_ROOT / "examples" / "fake-smoke.yaml")
    for run_id in (RUN_ID, SECOND_RUN_ID):
        assert (
            main(
                [
                    "run",
                    source,
                    "--runs-root",
                    str(runs_root),
                    "--run-id",
                    run_id,
                ]
            )
            == 0
        )
        capsys.readouterr()

    assert (
        main(
            [
                "trial-set",
                "create",
                "--run",
                RUN_ID,
                "--run",
                SECOND_RUN_ID,
                "--title",
                "CLI repeated trial",
                "--runs-root",
                str(runs_root),
                "--trial-sets-root",
                str(trial_sets_root),
                "--trial-set-id",
                "trial-set-11111111111111111111111111111111",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["member_count"] == 2

    assert (
        main(
            [
                "trial-set",
                "verify",
                created["path"],
                "--runs-root",
                str(runs_root),
                "--expected-digest",
                created["trial_set_digest"],
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["valid"] is True

    assert (
        main(
            [
                "trial-set",
                "summarize",
                created["path"],
                "--runs-root",
                str(runs_root),
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["inference"] == "DESCRIPTIVE_ONLY"
    assert summary["weighting"] == "EQUAL_PER_RUN"
    assert summary["request_population_policy"] == "separate_per_run_v1"
    assert all(item["available_run_count"] == 2 for item in summary["variations"])


def test_cli_comparison_plan_create_verify_and_execute(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_root = tmp_path / "baseline"
    candidate_root = tmp_path / "candidate"
    for root in (baseline_root, candidate_root):
        (root / "workloads").mkdir(parents=True)
        shutil.copy2(
            REPOSITORY_ROOT / "examples" / "workloads" / "fake-smoke.jsonl",
            root / "workloads" / "fake-smoke.jsonl",
        )
    source_text = (REPOSITORY_ROOT / "examples" / "fake-smoke.yaml").read_text(
        encoding="utf-8"
    )
    baseline_source = baseline_root / "experiment.yaml"
    candidate_source = candidate_root / "experiment.yaml"
    baseline_source.write_text(source_text, encoding="utf-8")
    candidate_source.write_text(
        source_text.replace("concurrency: 2", "concurrency: 4"),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "comparison-plan",
                "create",
                "--baseline-source",
                str(baseline_source),
                "--candidate-source",
                str(candidate_source),
                "--title",
                "CLI controlled comparison",
                "--hypothesis",
                "Concurrency may change throughput.",
                "--repetitions",
                "2",
                "--primary-outcome",
                "measured_request_count:count",
                "--runs-root",
                str(tmp_path / "runs"),
                "--comparison-plans-root",
                str(tmp_path / "comparison-plans"),
                "--schedule-seed",
                "00" * 32,
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["predeclaration_assurance"] == "OPERATOR_ATTESTED"
    assert len(created["schedule"]) == 4
    assert created["arms"]["baseline"]["concurrency"] == 2
    assert created["arms"]["candidate"]["concurrency"] == 4

    assert (
        main(
            [
                "comparison-plan",
                "verify",
                created["path"],
                "--expected-digest",
                created["comparison_plan_digest"],
            ]
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["comparison_plan_id"] == created["comparison_plan_id"]
    assert verified["valid"] is True

    assert (
        main(
            [
                "comparison-plan",
                "execute",
                created["path"],
                "--expected-digest",
                created["comparison_plan_digest"],
                "--baseline-source",
                str(baseline_source),
                "--candidate-source",
                str(candidate_source),
                "--runs-root",
                str(tmp_path / "runs"),
                "--trial-sets-root",
                str(tmp_path / "trial-sets"),
                "--comparison-results-root",
                str(tmp_path / "comparison-results"),
            ]
        )
        == 0
    )
    executed = json.loads(capsys.readouterr().out)
    assert executed["comparison_plan_id"] == created["comparison_plan_id"]
    assert executed["planned_run_count"] == 4
    assert executed["executed_run_ids"] == [
        slot["run_id"] for slot in created["schedule"]
    ]
    assert executed["reused_run_ids"] == []
    assert executed["status"] == "INCOMPARABLE"
    assert executed["valid"] is True
