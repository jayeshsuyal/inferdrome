"""Independent raw-record assertions for the native positive catalog scenario."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from inferdrome.evaluation.contracts import EvaluationConfig
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.load_calibration_rehearsal import (
    BoundCandidateRecipe,
    CompiledRehearsal,
    RehearsalResult,
)
from inferdrome.evaluation.report_reader import (
    PopulationCounts,
    StudyReport,
    load_evaluation_report_bytes,
)
from inferdrome.evaluation.runner import EvaluationResult
from inferdrome.evaluation.study import (
    StudyManifest,
    load_study_trial_bytes,
    plan_bytes,
    read_manifest,
)
from inferdrome.evaluation.study_config import CompiledStudy
from inferdrome.evaluation.study_files import (
    MAX_METADATA_BYTES,
    StudyDirectory,
    trial_filename,
)
from inferdrome.evaluation.study_validation import OUTCOMES
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest


@dataclass(frozen=True)
class _MeasuredTrial:
    offered: int
    slo_good: int
    window_ns: int


def _assert_population(
    config: EvaluationConfig, result: EvaluationResult, summary: PopulationCounts
) -> None:
    """Use records as the denominator, independently of the reducer's counts."""
    records = result.records
    assert result.config_sha256 == sha256_digest(
        canonical_json_bytes(config.model_dump(mode="json"))
    )
    assert len(records) == len(config.offers)
    assert [row.request_index for row in records] == list(range(len(config.offers)))
    for record, offer in zip(records, config.offers, strict=True):
        assert record.scheduled_ns == offer.scheduled_ns
        assert record.outcome in OUTCOMES
        assert type(record.terminal_ns) is int
        assert record.terminal_ns >= record.scheduled_ns
    counts = Counter(row.outcome for row in records)
    assert summary.offered_count == len(records)
    assert summary.successful_count == counts["SUCCESS"]
    assert summary.dispatched_count == sum(row.attempts for row in records)
    assert sum(item.count for item in summary.outcomes.values()) == len(records)
    assert set(summary.outcomes) == set(OUTCOMES)
    for outcome, measured in summary.outcomes.items():
        assert measured.count == counts[outcome]
        assert measured.offered_fraction.numerator == counts[outcome]
        assert measured.offered_fraction.denominator == len(records)


def _read_phase(
    study_path: Path, plan: CompiledStudy, expected_manifest: StudyManifest
) -> tuple[_MeasuredTrial, ...]:
    """Validate the retained envelopes, then check report counts from raw records."""
    report_path = study_path.with_name(study_path.name + "-report")
    with StudyDirectory.open(report_path) as directory:
        report = load_evaluation_report_bytes(
            directory.read("report.json", limit=MAX_METADATA_BYTES), kind="STUDY"
        )
    assert isinstance(report, StudyReport)
    assert report.status == "COMPLETED"
    assert report.config_sha256 == plan.config_sha256
    assert report.plan_sha256 == sha256_digest(plan_bytes(plan))
    assert [row.trial_id for row in report.trials] == [
        trial.trial_id for trial in plan.trials
    ]
    lifecycle = json.loads(
        study_path.with_name(study_path.name + "-lifecycle.json").read_bytes()
    )
    assert lifecycle["config_sha256"] == plan.config_sha256
    assert lifecycle["plan_sha256"] == sha256_digest(plan_bytes(plan))
    receipts = lifecycle["receipts"]
    assert [row["source_trial_id"] for row in receipts] == [
        trial.trial_id for trial in plan.trials
    ]
    assert all(
        row["reset_and_warmup"] == "COMPLETED"
        and row["cleanup"] == "CONFIRMED"
        and row["execute_elapsed_ns"] is not None
        for row in receipts
    )
    measured: list[_MeasuredTrial] = []
    with StudyDirectory.open(
        study_path, budget=plan.config.limits.total_output_bytes
    ) as directory:
        manifest = read_manifest(directory, plan)
        assert manifest == expected_manifest
        assert manifest.status == "COMPLETED" and manifest.reason is None
        assert len(manifest.trials) == len(plan.trials)
        for index, (trial, entry, summary) in enumerate(
            zip(plan.trials, manifest.trials, report.trials, strict=True)
        ):
            assert entry.trial_id == trial.trial_id
            assert entry.state == "RETURNED" and entry.result_status == "COMPLETED"
            assert entry.result_filename == trial_filename(index)
            content = directory.read(
                trial_filename(index), limit=plan.config.limits.per_trial_result_bytes
            )
            digest = sha256_digest(content)
            assert entry.result_sha256 == summary.result_sha256 == digest
            validated = load_study_trial_bytes(content, plan, trial)
            assert validated.result_sha256 == digest
            assert validated.config_sha256 == trial.config_sha256
            assert validated.status == summary.status == "COMPLETED"
            summary_fields = summary.model_dump(mode="json")
            for key, value in trial.to_dict().items():
                assert summary_fields[key] == value, (trial.trial_id, key)
            _assert_population(
                trial.config.foreground, validated.foreground, summary.foreground
            )
            if isinstance(trial.config, RoutingFaultConfig):
                assert validated.background is not None
                assert summary.background is not None
                _assert_population(
                    trial.config.background, validated.background, summary.background
                )
            else:
                assert validated.background is summary.background is None

            # Completion is not SLO success. Classify each declared offer using
            # its retained scheduled-origin timestamps, without using selection
            # assessments or the report's already-reduced SLO count.
            assert trial.first_content_slo_ns == 100_000_000
            assert trial.completion_slo_ns == 150_000_000
            records = validated.foreground.records
            assert all(
                trial.window_start_ns <= row.scheduled_ns < trial.window_end_ns
                for row in records
            )
            good = sum(
                row.outcome == "SUCCESS"
                and row.first_content_ns is not None
                and row.first_content_ns - row.scheduled_ns <= 100_000_000
                and row.terminal_ns - row.scheduled_ns <= 150_000_000
                for row in records
            )
            window = trial.window_end_ns - trial.window_start_ns
            population = summary.foreground
            assert population.slo_good_count == good
            assert population.slo_success_fraction.numerator == good
            assert population.slo_success_fraction.denominator == len(records)
            assert population.fixed_offered_window_ns == window
            assert population.slo_goodput_rps.numerator == good * 1_000_000_000
            assert population.slo_goodput_rps.denominator == window
            measured.append(_MeasuredTrial(len(records), good, window))
    return tuple(measured)


def assert_native_selected_candidate(
    rehearsal: CompiledRehearsal, result: RehearsalResult, output: Path
) -> BoundCandidateRecipe:
    """Require the highest genuinely admissible candidate and its exact artifacts.

    This helper is for the positive two-level, sixteen-calibration-trial catalog
    fixture. A host where neither level qualifies fails with measured counts;
    it is not silently converted into a skipped positive catalog assertion.
    """
    assert result.engine_choice_bindings_sha256 is None
    assert len(rehearsal.candidates) == 2
    declared = tuple(
        binding.declared_trial
        for candidate in rehearsal.candidates
        for binding in candidate.calibration
    )
    assert declared == rehearsal.calibration_plan.trials
    assert len(declared) == len({trial.trial_id for trial in declared}) == 16
    expected_levels = [item.level.level_id for item in rehearsal.candidates]
    assert [level for level, _ in result.calibration_manifests] == expected_levels
    expected_assessments: list[dict[str, object]] = []
    eligible: list[BoundCandidateRecipe] = []
    diagnostic: dict[str, list[dict[str, int]]] = {}

    for candidate, (_, manifest) in zip(
        rehearsal.candidates, result.calibration_manifests, strict=True
    ):
        level = candidate.level.level_id
        assert [item.source_index for item in candidate.calibration] == list(range(8))
        assert tuple(item.source_trial for item in candidate.calibration) == (
            candidate.calibration_plan.trials
        )
        measured = _read_phase(
            output / f"calibration-{level}", candidate.calibration_plan, manifest
        )
        assert len(measured) == 8
        assert all(row.offered == candidate.level.planned_requests for row in measured)
        assert all(
            row.offered * 1_000_000_000_000
            == candidate.level.offered_rate_millirps * row.window_ns
            for row in measured
        )
        admissible = all(row.slo_good == row.offered for row in measured)
        # Every offered record has a terminal outcome above. If all are SLO
        # good, their p95 necessarily meets both unchanged latency targets too.
        expected_assessments.append(
            {
                "level_id": level,
                "offered_rate_millirps": candidate.level.offered_rate_millirps,
                "minimum_all_offered_slo_goodput_millirps": min(
                    row.slo_good * 1_000_000_000_000 // row.window_ns
                    for row in measured
                ),
                "admissible": admissible,
                "reason": "ALL_TERMINALS_SLO_GOOD"
                if admissible
                else "SLO_MISS_OR_LATENCY_TARGET_MISS",
            }
        )
        diagnostic[level] = [
            {"offered": row.offered, "slo_good": row.slo_good} for row in measured
        ]
        if admissible:
            eligible.append(candidate)

    selection = result.calibration_selection
    assert [row.model_dump(mode="json") for row in selection.assessments] == (
        expected_assessments
    ), diagnostic
    assert eligible, f"No admissible native catalog fixture level: {diagnostic}"
    selected = max(eligible, key=lambda item: item.level.offered_rate_millirps)
    selected_id = selected.level.level_id
    assert selection.status == "SELECTED"
    assert selection.selected_level_id == selected_id, diagnostic
    assert json.loads((output / "calibration-selection.json").read_bytes()) == (
        selection.model_dump(mode="json")
    )
    assert result.confirmation.status == "READY"
    assert result.confirmation.selected_level_id == selected_id
    assert result.confirmation.trials == tuple(
        binding.declared_trial for binding in selected.confirmation
    )
    assert tuple(binding.source_trial for binding in selected.confirmation) == (
        selected.confirmation_plan.trials
    )
    assert [binding.source_index for binding in selected.confirmation] == list(range(8))
    assert json.loads((output / "confirmation-plan.json").read_bytes()) == (
        result.confirmation.model_dump(mode="json")
    )
    assert result.confirmation_manifest is not None
    _read_phase(
        output / f"confirmation-{selected_id}",
        selected.confirmation_plan,
        result.confirmation_manifest,
    )
    assert result.confirmation_report_path == (
        output / f"confirmation-{selected_id}-report" / "report.json"
    )
    assert {path.name for path in output.glob("confirmation-*") if path.is_dir()} == {
        f"confirmation-{selected_id}", f"confirmation-{selected_id}-report"
    }

    bindings_bytes = (output / "candidate-recipe-bindings.json").read_bytes()
    assert sha256_digest(bindings_bytes) == result.candidate_recipe_bindings_sha256
    bindings = json.loads(bindings_bytes)
    assert [row["level_id"] for row in bindings["candidates"]] == expected_levels
    for candidate, row in zip(
        rehearsal.candidates, bindings["candidates"], strict=True
    ):
        for phase, plan, recipe_digest in (
            (
                "calibration",
                candidate.calibration_plan,
                candidate.calibration_recipe_sha256,
            ),
            (
                "confirmation",
                candidate.confirmation_plan,
                candidate.confirmation_recipe_sha256,
            ),
        ):
            assert row[phase] == {
                "config_sha256": plan.config_sha256,
                "plan_sha256": sha256_digest(plan_bytes(plan)),
                "recipe_sha256": recipe_digest,
            }
    return selected
