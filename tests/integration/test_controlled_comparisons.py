"""End-to-end controlled comparisons over real sealed synthetic bundles."""

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inferdrome.comparisons import (
    VerifiedComparisonPlan,
    create_comparison_plan,
    create_comparison_result,
    verify_comparison_plan,
    verify_comparison_result,
)
from inferdrome.domain.controlled_comparison import (
    ComparisonArm,
    ConcurrencyIndependentVariable,
    ControlCheckId,
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
from inferdrome.errors import ControlledComparisonError
from inferdrome.execution.orchestrator import run_experiment
from inferdrome.resolution import resolve_experiment
from inferdrome.trials import create_trial_set

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FAKE_SOURCE = REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"
FAKE_WORKLOAD = REPOSITORY_ROOT / "examples" / "workloads" / "fake-smoke.jsonl"
CONTRACT_DIGEST = f"sha256:{'c' * 64}"


@pytest.fixture
def _complete_synthetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    def capture(
        *, run_id: str, target_model: str, captured_at: datetime
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

    monkeypatch.setattr(
        "inferdrome.execution.orchestrator.capture_fake_environment",
        capture,
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


def _source_pair(
    root: Path,
    *,
    contract_digest: str | None = CONTRACT_DIGEST,
) -> tuple[Path, Path]:
    baseline_root = root / "baseline-source"
    candidate_root = root / "candidate-source"
    for source_root in (baseline_root, candidate_root):
        workload_root = source_root / "workloads"
        workload_root.mkdir(parents=True)
        shutil.copy2(FAKE_WORKLOAD, workload_root / FAKE_WORKLOAD.name)

    source_text = FAKE_SOURCE.read_text(encoding="utf-8")
    if contract_digest is not None:
        source_text += (
            f"\nlinks:\n  exitspec_contract_digest: {contract_digest}\n"
        )
    baseline_source = baseline_root / "experiment.yaml"
    candidate_source = candidate_root / "experiment.yaml"
    baseline_source.write_text(source_text, encoding="utf-8")
    assert source_text.count("concurrency: 2") == 1
    candidate_source.write_text(
        source_text.replace("concurrency: 2", "concurrency: 4"),
        encoding="utf-8",
    )
    return baseline_source, candidate_source


def _run_ids(prefix: str) -> tuple[str, str]:
    return (
        f"run-{prefix * 31}1",
        f"run-{prefix * 31}2",
    )


def _create_plan(
    root: Path,
    *,
    schedule_seed: str = "00" * 32,
    contract_digest: str | None = CONTRACT_DIGEST,
) -> tuple[VerifiedComparisonPlan, Path, Path]:
    baseline_source, candidate_source = _source_pair(
        root,
        contract_digest=contract_digest,
    )
    baseline = resolve_experiment(
        baseline_source,
        run_id="run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    candidate = resolve_experiment(
        candidate_source,
        run_id="run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    plan = create_comparison_plan(
        runs_root=root / "runs",
        comparison_plans_root=root / "comparison-plans",
        experiment_id=baseline.resolved_spec.experiment.id,
        title="Concurrency 2 versus 4",
        hypothesis="Changing concurrency may change measured request throughput.",
        planned_repetitions_per_arm=2,
        independent_variable=ConcurrencyIndependentVariable(
            value_type="integer",
            path="traffic.concurrency",
            baseline_value=2,
            candidate_value=4,
        ),
        primary_outcome=frozen_outcome_selector(
            MetricId.MEASURED_REQUEST_COUNT,
            Aggregation.COUNT,
        ),
        baseline_resolved_experiment=baseline.resolved_spec,
        baseline_source_spec_digest=baseline.source_spec_digest,
        baseline_execution_fingerprint=baseline.execution_fingerprint,
        candidate_resolved_experiment=candidate.resolved_spec,
        candidate_source_spec_digest=candidate.source_spec_digest,
        candidate_execution_fingerprint=candidate.execution_fingerprint,
        baseline_run_ids=_run_ids("a"),
        candidate_run_ids=_run_ids("b"),
        comparison_plan_id="comparison-plan-11111111111111111111111111111111",
        baseline_trial_set_id="trial-set-11111111111111111111111111111111",
        candidate_trial_set_id="trial-set-22222222222222222222222222222222",
        schedule_seed=schedule_seed,
        created_at=datetime.now(UTC),
    )
    return plan, baseline_source, candidate_source


def _execute_and_group(
    root: Path,
    plan: VerifiedComparisonPlan,
    baseline_source: Path,
    candidate_source: Path,
    *,
    reverse_schedule: bool = False,
) -> tuple[object, object]:
    schedule = plan.descriptor.ordered_schedule
    if reverse_schedule:
        schedule = tuple(reversed(schedule))
    for slot in schedule:
        source = (
            baseline_source if slot.arm is ComparisonArm.BASELINE else candidate_source
        )
        run_experiment(
            source,
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
    return baseline, candidate


def test_controlled_comparison_recalculates_one_paired_point_estimate(
    tmp_path: Path,
    _complete_synthetic_environment: None,
    emulated_customer_eligible_recalculation: None,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        baseline, candidate = _execute_and_group(
            tmp_path,
            plan,
            baseline_source,
            candidate_source,
        )
        result = create_comparison_result(
            runs_root=tmp_path / "runs",
            trial_sets_root=tmp_path / "trial-sets",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
            comparison_plan_id=plan.descriptor.comparison_plan_id,
            expected_comparison_plan_digest=plan.comparison_plan_digest,
            baseline_trial_set_id=baseline.descriptor.trial_set_id,
            expected_baseline_trial_set_digest=baseline.trial_set_digest,
            candidate_trial_set_id=candidate.descriptor.trial_set_id,
            expected_candidate_trial_set_digest=candidate.trial_set_digest,
            comparison_result_id=("comparison-result-11111111111111111111111111111111"),
        )

        assert result.descriptor.status == "COMPARABLE", (
            result.descriptor.unsatisfied_controls
        )
        assert result.descriptor.predeclaration_assurance == "OPERATOR_ATTESTED"
        assert result.descriptor.environment_control_scope == (
            "OBSERVED_V1_ALLOWLIST_ONLY"
        )
        assert result.descriptor.unsatisfied_controls == ()
        assert len(result.descriptor.outcomes) == 1
        outcome = result.descriptor.outcomes[0]
        assert outcome.baseline_mean == "2"
        assert outcome.candidate_mean == "2"
        assert outcome.estimate == "0"
        assert tuple(
            point.candidate_minus_baseline for point in outcome.paired_differences
        ) == ("0", "0")

        independently_verified = verify_comparison_result(
            result.path,
            runs_root=tmp_path / "runs",
            trial_sets_root=tmp_path / "trial-sets",
            comparison_plans_root=tmp_path / "comparison-plans",
            expected_comparison_result_digest=result.comparison_result_digest,
        )
        assert independently_verified.descriptor == result.descriptor
    finally:
        _make_tree_writable(tmp_path)


@pytest.mark.parametrize(
    "eligibility_fixture",
    [None, "emulated_ineligible_recalculation"],
    ids=["synthetic-only", "ineligible"],
)
def test_ineligible_evidence_suppresses_controlled_outcomes(
    tmp_path: Path,
    _complete_synthetic_environment: None,
    request: pytest.FixtureRequest,
    eligibility_fixture: str | None,
) -> None:
    if eligibility_fixture is not None:
        request.getfixturevalue(eligibility_fixture)
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        baseline, candidate = _execute_and_group(
            tmp_path,
            plan,
            baseline_source,
            candidate_source,
        )
        result = create_comparison_result(
            runs_root=tmp_path / "runs",
            trial_sets_root=tmp_path / "trial-sets",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
            comparison_plan_id=plan.descriptor.comparison_plan_id,
            expected_comparison_plan_digest=plan.comparison_plan_digest,
            baseline_trial_set_id=baseline.descriptor.trial_set_id,
            expected_baseline_trial_set_digest=baseline.trial_set_digest,
            candidate_trial_set_id=candidate.descriptor.trial_set_id,
            expected_candidate_trial_set_digest=candidate.trial_set_digest,
        )

        assert baseline.comparison_authority.scope == (
            "DESCRIPTIVE_ONLY_NON_AUTHORITATIVE"
        )
        assert candidate.comparison_authority.scope == (
            "DESCRIPTIVE_ONLY_NON_AUTHORITATIVE"
        )
        assert result.descriptor.status == "INCOMPARABLE"
        assert result.descriptor.outcomes == ()
        assert ControlCheckId.OUTCOME_COVERAGE_AND_SEMANTICS in (
            result.descriptor.unsatisfied_controls
        )
        assert ControlCheckId.EXACT_ARM_MEMBERSHIP not in (
            result.descriptor.unsatisfied_controls
        )
    finally:
        _make_tree_writable(tmp_path)


def test_missing_exitspec_identity_suppresses_controlled_outcomes(
    tmp_path: Path,
    _complete_synthetic_environment: None,
    emulated_customer_eligible_recalculation: None,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(
            tmp_path,
            contract_digest=None,
        )
        baseline, candidate = _execute_and_group(
            tmp_path,
            plan,
            baseline_source,
            candidate_source,
        )
        result = create_comparison_result(
            runs_root=tmp_path / "runs",
            trial_sets_root=tmp_path / "trial-sets",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
            comparison_plan_id=plan.descriptor.comparison_plan_id,
            expected_comparison_plan_digest=plan.comparison_plan_digest,
            baseline_trial_set_id=baseline.descriptor.trial_set_id,
            expected_baseline_trial_set_digest=baseline.trial_set_digest,
            candidate_trial_set_id=candidate.descriptor.trial_set_id,
            expected_candidate_trial_set_digest=candidate.trial_set_digest,
        )

        assert baseline.comparison_authority.issues == (
            "EXITSPEC_CONTRACT_IDENTITY_MISSING",
        )
        assert candidate.comparison_authority.issues == (
            "EXITSPEC_CONTRACT_IDENTITY_MISSING",
        )
        assert result.descriptor.status == "INCOMPARABLE"
        assert result.descriptor.outcomes == ()
        assert ControlCheckId.OUTCOME_COVERAGE_AND_SEMANTICS in (
            result.descriptor.unsatisfied_controls
        )
    finally:
        _make_tree_writable(tmp_path)


def test_partial_environment_is_incomparable_and_suppresses_outcomes(
    tmp_path: Path,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        baseline, candidate = _execute_and_group(
            tmp_path,
            plan,
            baseline_source,
            candidate_source,
        )
        result = create_comparison_result(
            runs_root=tmp_path / "runs",
            trial_sets_root=tmp_path / "trial-sets",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
            comparison_plan_id=plan.descriptor.comparison_plan_id,
            expected_comparison_plan_digest=plan.comparison_plan_digest,
            baseline_trial_set_id=baseline.descriptor.trial_set_id,
            expected_baseline_trial_set_digest=baseline.trial_set_digest,
            candidate_trial_set_id=candidate.descriptor.trial_set_id,
            expected_candidate_trial_set_digest=candidate.trial_set_digest,
        )

        assert result.descriptor.status == "INCOMPARABLE"
        assert ControlCheckId.COMPLETE_EQUAL_OBSERVED_ENVIRONMENT in (
            result.descriptor.unsatisfied_controls
        )
        assert result.descriptor.outcomes == ()
    finally:
        _make_tree_writable(tmp_path)


def test_schedule_deviation_is_incomparable_and_suppresses_estimate(
    tmp_path: Path,
    _complete_synthetic_environment: None,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        baseline, candidate = _execute_and_group(
            tmp_path,
            plan,
            baseline_source,
            candidate_source,
            reverse_schedule=True,
        )
        result = create_comparison_result(
            runs_root=tmp_path / "runs",
            trial_sets_root=tmp_path / "trial-sets",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
            comparison_plan_id=plan.descriptor.comparison_plan_id,
            expected_comparison_plan_digest=plan.comparison_plan_digest,
            baseline_trial_set_id=baseline.descriptor.trial_set_id,
            expected_baseline_trial_set_digest=baseline.trial_set_digest,
            candidate_trial_set_id=candidate.descriptor.trial_set_id,
            expected_candidate_trial_set_digest=candidate.trial_set_digest,
        )

        assert result.descriptor.status == "INCOMPARABLE"
        assert ControlCheckId.OBSERVED_SCHEDULE in (
            result.descriptor.unsatisfied_controls
        )
        assert result.descriptor.outcomes == ()
    finally:
        _make_tree_writable(tmp_path)


def test_comparison_plan_refuses_a_preexisting_run_workspace(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    reserved = runs_root / _run_ids("a")[0]
    reserved.mkdir(parents=True)
    with pytest.raises(
        ControlledComparisonError,
        match="currently unreserved run IDs",
    ):
        _create_plan(tmp_path)


def test_comparison_plan_rejects_extra_entries_and_hard_links(
    tmp_path: Path,
) -> None:
    try:
        plan, _, _ = _create_plan(tmp_path)
        plan.path.chmod(0o700)
        extra = plan.path / "unexpected.json"
        extra.write_text("{}", encoding="utf-8")
        plan.path.chmod(0o500)
        with pytest.raises(
            ControlledComparisonError,
            match="undeclared entries",
        ):
            verify_comparison_plan(plan.path)

        plan.path.chmod(0o700)
        extra.unlink()
        descriptor = plan.path / "comparison-plan.json"
        descriptor.chmod(0o600)
        hard_link = tmp_path / "comparison-plan-hard-link.json"
        os.link(descriptor, hard_link)
        descriptor.chmod(0o400)
        plan.path.chmod(0o500)
        with pytest.raises(
            ControlledComparisonError,
            match="one bounded read-only file",
        ):
            verify_comparison_plan(plan.path)
    finally:
        _make_tree_writable(tmp_path)


def test_result_recalculation_rejects_invented_arithmetic(
    tmp_path: Path,
    _complete_synthetic_environment: None,
    emulated_customer_eligible_recalculation: None,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        baseline, candidate = _execute_and_group(
            tmp_path,
            plan,
            baseline_source,
            candidate_source,
        )
        result = create_comparison_result(
            runs_root=tmp_path / "runs",
            trial_sets_root=tmp_path / "trial-sets",
            comparison_plans_root=tmp_path / "comparison-plans",
            comparison_results_root=tmp_path / "comparison-results",
            comparison_plan_id=plan.descriptor.comparison_plan_id,
            expected_comparison_plan_digest=plan.comparison_plan_digest,
            baseline_trial_set_id=baseline.descriptor.trial_set_id,
            expected_baseline_trial_set_digest=baseline.trial_set_digest,
            candidate_trial_set_id=candidate.descriptor.trial_set_id,
            expected_candidate_trial_set_digest=candidate.trial_set_digest,
        )
        descriptor_path = result.path / "comparison-result.json"
        result.path.chmod(0o700)
        descriptor_path.chmod(0o600)
        payload = json.loads(descriptor_path.read_text(encoding="utf-8"))
        payload["outcomes"][0]["estimate"] = "999"
        descriptor_path.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        descriptor_path.chmod(0o400)
        result.path.chmod(0o500)

        with pytest.raises(
            ControlledComparisonError,
            match=r"failed contract validation|disagrees with recalculation",
        ):
            verify_comparison_result(
                result.path,
                runs_root=tmp_path / "runs",
                trial_sets_root=tmp_path / "trial-sets",
                comparison_plans_root=tmp_path / "comparison-plans",
            )
    finally:
        _make_tree_writable(tmp_path)
