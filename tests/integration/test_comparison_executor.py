"""One-command execution of frozen controlled-comparison schedules."""

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from inferdrome.comparisons import (
    ExecutedComparison,
    VerifiedComparisonPlan,
    create_comparison_plan,
    execute_comparison_plan,
)
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
from inferdrome.domain.states import EnvironmentCompleteness, RunState
from inferdrome.errors import (
    CancellationRequested,
    ControlledComparisonError,
    ControlledComparisonExecutionError,
)
from inferdrome.execution.cancellation import CancellationReason, CancellationToken
from inferdrome.execution.orchestrator import run_experiment
from inferdrome.resolution import resolve_experiment
from inferdrome.workspace import RunWorkspace

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FAKE_SOURCE = REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"
FAKE_WORKLOAD = REPOSITORY_ROOT / "examples" / "workloads" / "fake-smoke.jsonl"
PLAN_ID = "comparison-plan-99999999999999999999999999999999"
CONTRACT_DIGEST = f"sha256:{'c' * 64}"


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


@pytest.fixture
def _complete_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "inferdrome.execution.orchestrator.capture_fake_environment",
        _capture_complete_synthetic_environment,
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


def _source_pair(root: Path) -> tuple[Path, Path]:
    source_text = FAKE_SOURCE.read_text(encoding="utf-8") + (
        f"\nlinks:\n  exitspec_contract_digest: {CONTRACT_DIGEST}\n"
    )
    sources = []
    for name, concurrency in (("baseline", 2), ("candidate", 4)):
        source_root = root / name
        (source_root / "workloads").mkdir(parents=True)
        shutil.copy2(
            FAKE_WORKLOAD,
            source_root / "workloads" / FAKE_WORKLOAD.name,
        )
        source = source_root / "experiment.yaml"
        source.write_text(
            source_text.replace(
                "concurrency: 2",
                f"concurrency: {concurrency}",
            ),
            encoding="utf-8",
        )
        sources.append(source)
    return sources[0], sources[1]


def _create_plan(
    root: Path,
) -> tuple[VerifiedComparisonPlan, Path, Path]:
    baseline_source, candidate_source = _source_pair(root)
    baseline = resolve_experiment(
        baseline_source,
        run_id="run-11111111111111111111111111111111",
    )
    candidate = resolve_experiment(
        candidate_source,
        run_id="run-22222222222222222222222222222222",
    )
    plan = create_comparison_plan(
        runs_root=root / "runs",
        comparison_plans_root=root / "comparison-plans",
        experiment_id=baseline.resolved_spec.experiment.id,
        title="Executor concurrency comparison",
        hypothesis="Concurrency may change measured request throughput.",
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
        comparison_plan_id=PLAN_ID,
        baseline_run_ids=(
            "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
            "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa2",
        ),
        candidate_run_ids=(
            "run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb1",
            "run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2",
        ),
        baseline_trial_set_id=(
            "trial-set-11111111111111111111111111111111"
        ),
        candidate_trial_set_id=(
            "trial-set-22222222222222222222222222222222"
        ),
        schedule_seed="00" * 32,
    )
    return plan, baseline_source, candidate_source


def _execute(
    root: Path,
    plan: VerifiedComparisonPlan,
    baseline_source: Path,
    candidate_source: Path,
    *,
    cancellation: CancellationToken | None = None,
) -> ExecutedComparison:
    return execute_comparison_plan(
        plan.path,
        expected_comparison_plan_digest=plan.comparison_plan_digest,
        baseline_source=baseline_source,
        candidate_source=candidate_source,
        runs_root=root / "runs",
        trial_sets_root=root / "trial-sets",
        comparison_results_root=root / "comparison-results",
        cancellation=cancellation,
    )


def test_executor_runs_the_exact_schedule_and_finalizes_every_artifact(
    tmp_path: Path,
    _complete_environment: None,
    emulated_customer_eligible_recalculation: None,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)

        executed = _execute(
            tmp_path,
            plan,
            baseline_source,
            candidate_source,
        )

        planned_order = tuple(
            slot.run_id for slot in plan.descriptor.ordered_schedule
        )
        assert executed.executed_run_ids == planned_order
        assert executed.reused_run_ids == ()
        assert executed.baseline_trial_set.descriptor.trial_set_id == (
            plan.descriptor.baseline_arm.planned_trial_set_id
        )
        assert executed.candidate_trial_set.descriptor.trial_set_id == (
            plan.descriptor.candidate_arm.planned_trial_set_id
        )
        assert executed.result.descriptor.comparison_result_id == (
            "comparison-result-99999999999999999999999999999999"
        )
        assert executed.result.descriptor.status == "COMPARABLE"
        assert executed.result.descriptor.unsatisfied_controls == ()
    finally:
        _make_tree_writable(tmp_path)


def test_executor_resumes_only_a_verified_complete_prefix_and_is_idempotent(
    tmp_path: Path,
    _complete_environment: None,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        prefix = plan.descriptor.ordered_schedule[:2]
        for slot in prefix:
            source = (
                baseline_source
                if slot.arm is ComparisonArm.BASELINE
                else candidate_source
            )
            run_experiment(
                source,
                runs_root=tmp_path / "runs",
                run_id=slot.run_id,
            )

        first = _execute(
            tmp_path,
            plan,
            baseline_source,
            candidate_source,
        )
        second = _execute(
            tmp_path,
            plan,
            baseline_source,
            candidate_source,
        )

        planned_order = tuple(
            slot.run_id for slot in plan.descriptor.ordered_schedule
        )
        assert first.reused_run_ids == tuple(slot.run_id for slot in prefix)
        assert first.executed_run_ids == planned_order[2:]
        assert second.reused_run_ids == planned_order
        assert second.executed_run_ids == ()
        assert second.baseline_trial_set.trial_set_digest == (
            first.baseline_trial_set.trial_set_digest
        )
        assert second.candidate_trial_set.trial_set_digest == (
            first.candidate_trial_set.trial_set_digest
        )
        assert second.result.comparison_result_digest == (
            first.result.comparison_result_digest
        )
    finally:
        _make_tree_writable(tmp_path)


@pytest.mark.parametrize("prefix_length", range(5))
def test_executor_resumes_from_every_exact_schedule_prefix(
    tmp_path: Path,
    _complete_environment: None,
    prefix_length: int,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        prefix = plan.descriptor.ordered_schedule[:prefix_length]
        for slot in prefix:
            source = (
                baseline_source
                if slot.arm is ComparisonArm.BASELINE
                else candidate_source
            )
            run_experiment(
                source,
                runs_root=tmp_path / "runs",
                run_id=slot.run_id,
            )

        executed = _execute(
            tmp_path,
            plan,
            baseline_source,
            candidate_source,
        )
        schedule = plan.descriptor.ordered_schedule

        assert executed.reused_run_ids == tuple(
            slot.run_id for slot in schedule[:prefix_length]
        )
        assert executed.executed_run_ids == tuple(
            slot.run_id for slot in schedule[prefix_length:]
        )
    finally:
        _make_tree_writable(tmp_path)


def test_executor_rejects_a_completed_run_outside_the_schedule_prefix(
    tmp_path: Path,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        slot = plan.descriptor.ordered_schedule[1]
        source = (
            baseline_source
            if slot.arm is ComparisonArm.BASELINE
            else candidate_source
        )
        run_experiment(
            source,
            runs_root=tmp_path / "runs",
            run_id=slot.run_id,
        )

        with pytest.raises(
            ControlledComparisonExecutionError,
            match="exact schedule prefix",
        ):
            _execute(
                tmp_path,
                plan,
                baseline_source,
                candidate_source,
            )
    finally:
        _make_tree_writable(tmp_path)


def test_executor_rejects_changed_source_before_reserving_any_run(
    tmp_path: Path,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        candidate_source.write_text(
            candidate_source.read_text(encoding="utf-8").replace(
                "concurrency: 4",
                "concurrency: 8",
            ),
            encoding="utf-8",
        )

        with pytest.raises(
            ControlledComparisonExecutionError,
            match="candidate source does not match the frozen plan",
        ):
            _execute(
                tmp_path,
                plan,
                baseline_source,
                candidate_source,
            )

        assert not any(
            path.name.startswith("run-") for path in (tmp_path / "runs").iterdir()
        )
    finally:
        _make_tree_writable(tmp_path)


def test_original_input_mutation_after_first_slot_cannot_change_frozen_execution(
    tmp_path: Path,
    _complete_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    emulated_customer_eligible_recalculation: None,
) -> None:
    import inferdrome.comparisons.executor as executor_module

    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        original_run = executor_module.run_resolved_experiment
        call_count = 0

        def execute_then_mutate(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            result = original_run(*args, **kwargs)  # type: ignore[arg-type]
            call_count += 1
            if call_count == 1:
                candidate_source.write_text(
                    candidate_source.read_text(encoding="utf-8").replace(
                        "concurrency: 4",
                        "concurrency: 8",
                    ),
                    encoding="utf-8",
                )
                candidate_source.parent.joinpath(
                    "workloads",
                    FAKE_WORKLOAD.name,
                ).write_bytes(b'{"prompt":"mutated after snapshot"}\n')
            return result

        monkeypatch.setattr(
            executor_module,
            "run_resolved_experiment",
            execute_then_mutate,
        )

        executed = _execute(
            tmp_path,
            plan,
            baseline_source,
            candidate_source,
        )

        assert executed.result.descriptor.status == "COMPARABLE"
        assert executed.executed_run_ids == tuple(
            slot.run_id for slot in plan.descriptor.ordered_schedule
        )
    finally:
        _make_tree_writable(tmp_path)


def test_executor_refuses_to_retry_a_reserved_noncomplete_run(
    tmp_path: Path,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        first = plan.descriptor.ordered_schedule[0]
        source = (
            baseline_source
            if first.arm is ComparisonArm.BASELINE
            else candidate_source
        )
        resolution = resolve_experiment(source, run_id=first.run_id)
        RunWorkspace.reserve(tmp_path / "runs", resolution)

        with pytest.raises(
            ControlledComparisonExecutionError,
            match="v1 forbids retries and replacement runs",
        ):
            _execute(
                tmp_path,
                plan,
                baseline_source,
                candidate_source,
            )
    finally:
        _make_tree_writable(tmp_path)


def test_executor_requires_the_retained_plan_digest_before_any_mutation(
    tmp_path: Path,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        with pytest.raises(
            ControlledComparisonError,
            match="digest does not match",
        ):
            execute_comparison_plan(
                plan.path,
                expected_comparison_plan_digest=f"sha256:{'f' * 64}",
                baseline_source=baseline_source,
                candidate_source=candidate_source,
                runs_root=tmp_path / "runs",
                trial_sets_root=tmp_path / "trial-sets",
                comparison_results_root=tmp_path / "comparison-results",
            )
        assert not any(
            path.name.startswith("run-") for path in (tmp_path / "runs").iterdir()
        )
    finally:
        _make_tree_writable(tmp_path)


def test_executor_rejects_swapped_arm_sources_before_any_run(
    tmp_path: Path,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        with pytest.raises(
            ControlledComparisonExecutionError,
            match="baseline source does not match the frozen plan",
        ):
            _execute(
                tmp_path,
                plan,
                candidate_source,
                baseline_source,
            )
        assert not any(
            path.name.startswith("run-") for path in (tmp_path / "runs").iterdir()
        )
    finally:
        _make_tree_writable(tmp_path)


@pytest.mark.parametrize("terminal_state", [RunState.FAILED, RunState.INTERRUPTED])
def test_executor_never_retries_a_terminal_planned_attempt(
    tmp_path: Path,
    terminal_state: RunState,
) -> None:
    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        first = plan.descriptor.ordered_schedule[0]
        source = (
            baseline_source
            if first.arm is ComparisonArm.BASELINE
            else candidate_source
        )
        workspace = RunWorkspace.reserve(
            tmp_path / "runs",
            resolve_experiment(source, run_id=first.run_id),
        )
        workspace.transition(terminal_state)

        with pytest.raises(
            ControlledComparisonExecutionError,
            match=rf"is {terminal_state.value}; v1 forbids retries",
        ):
            _execute(
                tmp_path,
                plan,
                baseline_source,
                candidate_source,
            )
    finally:
        _make_tree_writable(tmp_path)


def test_executor_lock_is_shared_across_result_roots(
    tmp_path: Path,
) -> None:
    from inferdrome.comparisons.executor import _execution_lock

    runs_root = tmp_path / "runs"
    digest = f"sha256:{'1' * 64}"
    with (
        _execution_lock(runs_root, PLAN_ID, digest),
        pytest.raises(
            ControlledComparisonExecutionError,
            match="another executor already holds",
        ),
        _execution_lock(runs_root, PLAN_ID, digest),
    ):
        raise AssertionError("second executor unexpectedly acquired the lock")


def test_executor_lock_excludes_a_second_process_for_the_same_runs_root(
    tmp_path: Path,
) -> None:
    from inferdrome.comparisons.executor import _execution_lock

    runs_root = tmp_path / "runs"
    digest = f"sha256:{'2' * 64}"
    child = """
import sys
from pathlib import Path
from inferdrome.comparisons.executor import _execution_lock
from inferdrome.errors import ControlledComparisonExecutionError

try:
    with _execution_lock(Path(sys.argv[1]), sys.argv[2], sys.argv[3]):
        raise SystemExit(3)
except ControlledComparisonExecutionError:
    raise SystemExit(0)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")

    with _execution_lock(runs_root, PLAN_ID, digest):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                child,
                str(runs_root),
                PLAN_ID,
                digest,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )

    assert completed.returncode == 0, completed.stderr


def test_cancellation_between_trial_sets_resumes_from_complete_evidence(
    tmp_path: Path,
    _complete_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    emulated_customer_eligible_recalculation: None,
) -> None:
    import inferdrome.comparisons.executor as executor_module

    try:
        plan, baseline_source, candidate_source = _create_plan(tmp_path)
        cancellation = CancellationToken()
        original = executor_module._get_or_create_trial_set
        call_count = 0

        def cancel_after_first_trial_set(**kwargs):
            nonlocal call_count
            verified = original(**kwargs)
            call_count += 1
            if call_count == 1:
                cancellation.request(CancellationReason.USER)
            return verified

        monkeypatch.setattr(
            executor_module,
            "_get_or_create_trial_set",
            cancel_after_first_trial_set,
        )
        with pytest.raises(CancellationRequested, match="USER"):
            _execute(
                tmp_path,
                plan,
                baseline_source,
                candidate_source,
                cancellation=cancellation,
            )

        assert (
            tmp_path
            / "trial-sets"
            / plan.descriptor.baseline_arm.planned_trial_set_id
        ).is_dir()
        assert not (
            tmp_path
            / "trial-sets"
            / plan.descriptor.candidate_arm.planned_trial_set_id
        ).exists()
        assert not (tmp_path / "comparison-results").exists()

        monkeypatch.setattr(
            executor_module,
            "_get_or_create_trial_set",
            original,
        )
        resumed = _execute(
            tmp_path,
            plan,
            baseline_source,
            candidate_source,
        )
        assert resumed.executed_run_ids == ()
        assert resumed.reused_run_ids == tuple(
            slot.run_id for slot in plan.descriptor.ordered_schedule
        )
        assert resumed.result.descriptor.status == "COMPARABLE"
    finally:
        _make_tree_writable(tmp_path)
