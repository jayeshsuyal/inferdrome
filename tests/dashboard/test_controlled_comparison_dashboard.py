"""Read-only dashboard projections for controlled-comparison declarations."""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from inferdrome.comparisons import (
    VerifiedComparisonPlan,
    VerifiedComparisonResult,
    create_comparison_plan,
    create_comparison_result,
)
from inferdrome.dashboard.api import create_app
from inferdrome.dashboard.index import DashboardIndex
from inferdrome.domain.controlled_comparison import (
    ComparisonArm,
    ConcurrencyIndependentVariable,
    frozen_outcome_selector,
)
from inferdrome.domain.environment import (
    EnvironmentField,
    EnvironmentFieldName,
    EnvironmentManifest,
    ProvenanceKind,
)
from inferdrome.domain.metrics import Aggregation, MetricId
from inferdrome.domain.states import EnvironmentCompleteness
from inferdrome.execution.orchestrator import run_experiment
from inferdrome.immutable import STAGING_DIRECTORY, STAGING_PREFIX
from inferdrome.resolution import resolve_experiment
from inferdrome.trials import create_trial_set
from inferdrome.workspace import RunWorkspace

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FAKE_SOURCE = REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"
FAKE_WORKLOAD = REPOSITORY_ROOT / "examples" / "workloads" / "fake-smoke.jsonl"
PLAN_ID = "comparison-plan-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _capture_complete_synthetic_environment(
    *,
    run_id: str,
    target_model: str,
    captured_at: datetime,
) -> EnvironmentManifest:
    del target_model
    fields = []
    for field_name in EnvironmentFieldName:
        value: str | int = "synthetic"
        if field_name is EnvironmentFieldName.GPU_COUNT:
            value = 0
        elif field_name is EnvironmentFieldName.PRODUCER_VERSION:
            value = "1.0.0"
        fields.append(
            EnvironmentField(
                name=field_name,
                value=value,
                provenance=ProvenanceKind.DECLARED,
                evidence_path=None,
            )
        )
    return EnvironmentManifest(
        schema_version="inferdrome.environment.v1",
        run_id=run_id,
        field_set_version="inferdrome.environment-fields.v1",
        captured_at=captured_at,
        completeness=EnvironmentCompleteness.COMPLETE,
        fields=tuple(fields),
    )


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


def _create_pending_plan(root: Path) -> VerifiedComparisonPlan:
    source_text = FAKE_SOURCE.read_text(encoding="utf-8")
    sources: list[Path] = []
    for name, concurrency in (("baseline", 2), ("candidate", 4)):
        source_root = root / name
        (source_root / "workloads").mkdir(parents=True)
        shutil.copy2(
            FAKE_WORKLOAD,
            source_root / "workloads" / FAKE_WORKLOAD.name,
        )
        source = source_root / "experiment.yaml"
        source.write_text(
            source_text.replace("concurrency: 2", f"concurrency: {concurrency}"),
            encoding="utf-8",
        )
        sources.append(source)
    baseline = resolve_experiment(
        sources[0],
        run_id="run-11111111111111111111111111111111",
    )
    candidate = resolve_experiment(
        sources[1],
        run_id="run-22222222222222222222222222222222",
    )
    return create_comparison_plan(
        runs_root=root / "runs",
        comparison_plans_root=root / "comparison-plans",
        experiment_id=baseline.resolved_spec.experiment.id,
        title="Dashboard concurrency comparison",
        hypothesis="Concurrency may change measured request throughput.",
        planned_repetitions_per_arm=2,
        independent_variable=ConcurrencyIndependentVariable(
            value_type="integer",
            path="traffic.concurrency",
            baseline_value=2,
            candidate_value=4,
        ),
        primary_outcome=frozen_outcome_selector(
            MetricId.ATTEMPTED_REQUEST_THROUGHPUT,
            Aggregation.RATE,
        ),
        baseline_resolved_experiment=baseline.resolved_spec,
        baseline_source_spec_digest=baseline.source_spec_digest,
        baseline_execution_fingerprint=baseline.execution_fingerprint,
        candidate_resolved_experiment=candidate.resolved_spec,
        candidate_source_spec_digest=candidate.source_spec_digest,
        candidate_execution_fingerprint=candidate.execution_fingerprint,
        comparison_plan_id=PLAN_ID,
        schedule_seed="00" * 32,
    )


def _execute_and_create_result(
    root: Path,
    plan: VerifiedComparisonPlan,
) -> VerifiedComparisonResult:
    for slot in plan.descriptor.ordered_schedule:
        source_name = "baseline" if slot.arm is ComparisonArm.BASELINE else "candidate"
        run_experiment(
            root / source_name / "experiment.yaml",
            runs_root=root / "runs",
            run_id=slot.run_id,
        )
    baseline = create_trial_set(
        runs_root=root / "runs",
        trial_sets_root=root / "trial-sets",
        run_ids=plan.descriptor.baseline_arm.run_ids,
        title="Baseline arm",
        trial_set_id=plan.descriptor.baseline_arm.planned_trial_set_id,
    )
    candidate = create_trial_set(
        runs_root=root / "runs",
        trial_sets_root=root / "trial-sets",
        run_ids=plan.descriptor.candidate_arm.run_ids,
        title="Candidate arm",
        trial_set_id=plan.descriptor.candidate_arm.planned_trial_set_id,
    )
    return create_comparison_result(
        runs_root=root / "runs",
        trial_sets_root=root / "trial-sets",
        comparison_plans_root=root / "comparison-plans",
        comparison_results_root=root / "comparison-results",
        comparison_plan_id=plan.descriptor.comparison_plan_id,
        expected_comparison_plan_digest=plan.comparison_plan_digest,
        baseline_trial_set_id=baseline.descriptor.trial_set_id,
        expected_baseline_trial_set_digest=baseline.trial_set_digest,
        candidate_trial_set_id=candidate.descriptor.trial_set_id,
        expected_candidate_trial_set_digest=candidate.trial_set_digest,
    )


def test_index_and_detail_keep_design_separate_from_result(tmp_path: Path) -> None:
    try:
        plan = _create_pending_plan(tmp_path)
        index = DashboardIndex(
            tmp_path / "runs",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
        )

        snapshot = index.list_controlled_comparisons()
        detail = index.get_controlled_comparison(PLAN_ID)

        assert len(snapshot.comparisons) == 1
        summary = snapshot.comparisons[0]
        assert summary.comparison_plan_digest == plan.comparison_plan_digest
        assert summary.design_status == "PREDECLARED"
        assert summary.predeclaration_assurance == "OPERATOR_ATTESTED"
        assert summary.result_status == "NO_RESULT"
        assert summary.estimate is None
        assert detail.plan.hypothesis == (
            "Concurrency may change measured request throughput."
        )
        assert detail.result is None
        assert detail.result_issue is None
        assert detail.execution.status == "NOT_STARTED"
        assert detail.execution.result_published is False
        assert detail.execution.completed_run_count == 0
        assert detail.execution.next_sequence_index == 0
        assert {slot.state for slot in detail.execution.slots} == {"PENDING"}
    finally:
        _make_tree_writable(tmp_path)


def test_private_publication_stages_are_not_dashboard_artifacts(
    tmp_path: Path,
) -> None:
    try:
        _create_pending_plan(tmp_path)
        for root in (
            tmp_path / "comparison-plans",
            tmp_path / "comparison-results",
        ):
            root.mkdir(parents=True, exist_ok=True)
            (root / STAGING_DIRECTORY).mkdir(exist_ok=True)
            (root / f"{STAGING_PREFIX}orphan").mkdir()

        snapshot = DashboardIndex(
            tmp_path / "runs",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
        ).list_controlled_comparisons()

        assert len(snapshot.comparisons) == 1
        assert snapshot.rejected == ()
        assert snapshot.page.total == 1
    finally:
        _make_tree_writable(tmp_path)


def test_controlled_comparison_api_is_read_only_and_uses_plan_ids(
    tmp_path: Path,
) -> None:
    try:
        _create_pending_plan(tmp_path)
        app = create_app(
            DashboardIndex(
                tmp_path / "runs",
                comparison_plans_root=tmp_path / "comparison-plans",
                comparison_results_root=tmp_path / "comparison-results",
            )
        )
        with TestClient(app) as client:
            listed = client.get("/api/v1/controlled-comparisons")
            detail = client.get(f"/api/v1/controlled-comparisons/{PLAN_ID}")
            missing = client.get(
                "/api/v1/controlled-comparisons/"
                "comparison-plan-ffffffffffffffffffffffffffffffff"
            )
            mutations = [
                client.request(method, "/api/v1/controlled-comparisons")
                for method in ("POST", "PUT", "PATCH", "DELETE")
            ]

        assert listed.status_code == 200
        assert listed.json()["comparisons"][0]["result_status"] == "NO_RESULT"
        assert detail.status_code == 200
        assert detail.json()["plan"]["comparison_plan_id"] == PLAN_ID
        assert "resolved_experiment" not in detail.json()["plan"]["baseline_arm"]
        assert "resolved_experiment" not in detail.json()["plan"]["candidate_arm"]
        assert missing.status_code == 404
        assert all(response.status_code == 405 for response in mutations)
        serialized = json.dumps(
            {"list": listed.json(), "detail": detail.json()},
            sort_keys=True,
        ).lower()
        for forbidden in (
            "winner",
            "better",
            "significant",
            "caused",
            "acceptance verdict",
        ):
            assert forbidden not in serialized
    finally:
        _make_tree_writable(tmp_path)


def test_incomparable_detail_suppresses_all_run_level_outcome_projections(
    tmp_path: Path,
) -> None:
    try:
        plan = _create_pending_plan(tmp_path)
        _execute_and_create_result(tmp_path, plan)
        app = create_app(
            DashboardIndex(
                tmp_path / "runs",
                trial_sets_root=tmp_path / "trial-sets",
                comparison_plans_root=tmp_path / "comparison-plans",
                comparison_results_root=tmp_path / "comparison-results",
            )
        )

        with TestClient(app) as client:
            detail = client.get(f"/api/v1/controlled-comparisons/{PLAN_ID}")

        assert detail.status_code == 200
        payload = detail.json()
        assert payload["summary"]["result_status"] == "INCOMPARABLE"
        assert payload["summary"]["estimate"] is None
        assert payload["result"]["status"] == "INCOMPARABLE"
        assert payload["result"]["outcomes"] == []
        assert "COMPLETE_EQUAL_OBSERVED_ENVIRONMENT" in payload["result"][
            "unsatisfied_controls"
        ]
        assert payload["baseline_trial_set"] is None
        assert payload["candidate_trial_set"] is None
        assert "variations" not in json.dumps(payload, sort_keys=True).lower()
    finally:
        _make_tree_writable(tmp_path)


def test_comparable_detail_exposes_only_verified_paired_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        monkeypatch.setattr(
            "inferdrome.execution.orchestrator.capture_fake_environment",
            _capture_complete_synthetic_environment,
        )
        plan = _create_pending_plan(tmp_path)
        _execute_and_create_result(tmp_path, plan)
        app = create_app(
            DashboardIndex(
                tmp_path / "runs",
                trial_sets_root=tmp_path / "trial-sets",
                comparison_plans_root=tmp_path / "comparison-plans",
                comparison_results_root=tmp_path / "comparison-results",
            )
        )

        with TestClient(app) as client:
            detail = client.get(f"/api/v1/controlled-comparisons/{PLAN_ID}")

        assert detail.status_code == 200
        payload = detail.json()
        assert payload["summary"]["result_status"] == "COMPARABLE"
        assert payload["result"]["status"] == "COMPARABLE"
        assert payload["execution"]["status"] == "EVIDENCE_COMPLETE"
        assert payload["execution"]["result_published"] is True
        assert payload["execution"]["completed_run_count"] == 4
        assert all(
            slot["verified_bundle"] for slot in payload["execution"]["slots"]
        )
        assert len(payload["result"]["outcomes"]) == 1
        assert payload["baseline_trial_set"] is not None
        assert payload["candidate_trial_set"] is not None
        assert "resolved_experiment" not in json.dumps(payload["plan"])
    finally:
        _make_tree_writable(tmp_path)


def test_pending_detail_derives_verified_prefix_progress(tmp_path: Path) -> None:
    try:
        plan = _create_pending_plan(tmp_path)
        first = plan.descriptor.ordered_schedule[0]
        source_name = (
            "baseline" if first.arm is ComparisonArm.BASELINE else "candidate"
        )
        run_experiment(
            tmp_path / source_name / "experiment.yaml",
            runs_root=tmp_path / "runs",
            run_id=first.run_id,
        )

        detail = DashboardIndex(
            tmp_path / "runs",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
        ).get_controlled_comparison(PLAN_ID)

        assert detail.result is None
        assert detail.execution.status == "PARTIAL"
        assert detail.execution.completed_run_count == 1
        assert detail.execution.next_sequence_index == 1
        assert detail.execution.exact_schedule_prefix is True
        assert detail.execution.slots[0].state == "COMPLETE"
        assert detail.execution.slots[0].verified_bundle is True
        assert all(
            slot.state == "PENDING" for slot in detail.execution.slots[1:]
        )
    finally:
        _make_tree_writable(tmp_path)


def test_pending_detail_marks_out_of_order_evidence_blocked(tmp_path: Path) -> None:
    try:
        plan = _create_pending_plan(tmp_path)
        second = plan.descriptor.ordered_schedule[1]
        source_name = (
            "baseline" if second.arm is ComparisonArm.BASELINE else "candidate"
        )
        run_experiment(
            tmp_path / source_name / "experiment.yaml",
            runs_root=tmp_path / "runs",
            run_id=second.run_id,
        )

        detail = DashboardIndex(
            tmp_path / "runs",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
        ).get_controlled_comparison(PLAN_ID)

        assert detail.execution.status == "BLOCKED"
        assert detail.execution.completed_run_count == 1
        assert detail.execution.next_sequence_index is None
        assert detail.execution.exact_schedule_prefix is False
    finally:
        _make_tree_writable(tmp_path)


def test_pending_detail_marks_out_of_order_nonterminal_workspace_blocked(
    tmp_path: Path,
) -> None:
    try:
        plan = _create_pending_plan(tmp_path)
        second = plan.descriptor.ordered_schedule[1]
        source_name = (
            "baseline" if second.arm is ComparisonArm.BASELINE else "candidate"
        )
        resolution = resolve_experiment(
            tmp_path / source_name / "experiment.yaml",
            run_id=second.run_id,
        )
        RunWorkspace.reserve(tmp_path / "runs", resolution)

        detail = DashboardIndex(
            tmp_path / "runs",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
        ).get_controlled_comparison(PLAN_ID)

        assert detail.execution.status == "BLOCKED"
        assert detail.execution.completed_run_count == 0
        assert detail.execution.next_sequence_index is None
        assert detail.execution.exact_schedule_prefix is False
        assert detail.execution.slots[0].state == "PENDING"
        assert detail.execution.slots[1].state == "CREATED"
    finally:
        _make_tree_writable(tmp_path)


def test_published_result_does_not_fabricate_current_workspace_state(
    tmp_path: Path,
) -> None:
    try:
        plan = _create_pending_plan(tmp_path)
        _execute_and_create_result(tmp_path, plan)
        first = plan.descriptor.ordered_schedule[0]
        state_path = tmp_path / "runs" / first.run_id / "control" / "state.json"
        state_path.chmod(0o600)
        state_path.write_text("{}", encoding="utf-8")

        detail = DashboardIndex(
            tmp_path / "runs",
            trial_sets_root=tmp_path / "trial-sets",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
        ).get_controlled_comparison(PLAN_ID)

        assert detail.result is not None
        assert detail.execution.result_published is True
        assert detail.execution.status == "BLOCKED"
        assert detail.execution.exact_schedule_prefix is False
        assert detail.execution.slots[0].state == "INVALID"
        assert detail.execution.slots[0].verified_bundle is False
    finally:
        _make_tree_writable(tmp_path)


def test_result_with_a_stale_plan_digest_is_withheld_consistently(
    tmp_path: Path,
) -> None:
    try:
        plan = _create_pending_plan(tmp_path)
        result = _execute_and_create_result(tmp_path, plan)
        descriptor_path = result.path / "comparison-result.json"
        result.path.chmod(0o700)
        descriptor_path.chmod(0o600)
        payload = json.loads(descriptor_path.read_text(encoding="utf-8"))
        payload["comparison_plan_digest"] = f"sha256:{'f' * 64}"
        descriptor_path.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        descriptor_path.chmod(0o400)
        result.path.chmod(0o500)

        index = DashboardIndex(
            tmp_path / "runs",
            trial_sets_root=tmp_path / "trial-sets",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
        )
        snapshot = index.list_controlled_comparisons()
        detail = index.get_controlled_comparison(PLAN_ID)

        assert snapshot.comparisons[0].result_status == "WITHHELD"
        assert {item.code for item in snapshot.rejected} == {
            "RESULT_VERIFICATION_FAILED"
        }
        assert detail.summary.result_status == "WITHHELD"
        assert detail.result is None
        assert detail.result_issue == "RESULT_VERIFICATION_FAILED"
    finally:
        _make_tree_writable(tmp_path)


def test_invalid_plan_is_withheld_with_a_snapshot_fingerprint(tmp_path: Path) -> None:
    try:
        plan = _create_pending_plan(tmp_path)
        plan.path.chmod(0o700)
        (plan.path / "unexpected.json").write_text("{}", encoding="utf-8")
        plan.path.chmod(0o500)

        snapshot = DashboardIndex(
            tmp_path / "runs",
            comparison_plans_root=tmp_path / "comparison-plans",
        ).list_controlled_comparisons()

        assert snapshot.comparisons == ()
        assert len(snapshot.rejected) == 1
        rejected = snapshot.rejected[0]
        assert rejected.code == "PLAN_VERIFICATION_FAILED"
        assert len(rejected.failure_fingerprint) == 64
    finally:
        _make_tree_writable(tmp_path)
