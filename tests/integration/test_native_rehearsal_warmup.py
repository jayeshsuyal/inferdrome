"""A native telemetry warmup failure stops the full deterministic rehearsal."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.load_calibration import load_calibration_protocol_bytes
from inferdrome.evaluation.load_calibration_rehearsal import (
    CandidateStudyRecipe,
    compile_rehearsal,
    run_rehearsal,
)
from inferdrome.evaluation.study import load_study_trial_bytes, read_manifest
from inferdrome.evaluation.study_config import CompiledTrial
from inferdrome.evaluation.study_files import StudyDirectory, trial_filename
from inferdrome.routing_execution.canonical import sha256_digest
from tests.integration.test_load_calibration_rehearsal import (
    _protocol,
    _recipe_configs,
    _rehearsal_failure_diagnostics,
)
from tests.native_rehearsal_executor import (
    NativeRehearsalExecutor,
    native_rehearsal_execution,
)


class _RecordingLifecycle:
    """Lifecycle success is distinct from the native telemetry warmup result."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.events: list[tuple[str, str]] = []

    async def prepare(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        assert not stop.is_set()
        assert (self.output / "candidate-recipe-bindings.json").is_file()
        self.events.append(("prepare", trial.trial_id))

    async def cleanup(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        self.events.append(("cleanup", trial.trial_id))


def test_native_warmup_failure_retains_every_offer_and_stops_later_phases(
    tmp_path: Path,
) -> None:
    origins = ("http://127.0.0.1:8001", "http://127.0.0.1:8002")
    low_calibration, low_confirmation = _recipe_configs(
        origins, level_id="load-low", offered_count=7, seed=11
    )
    high_calibration, high_confirmation = _recipe_configs(
        origins, level_id="load-high", offered_count=14, seed=29
    )
    protocol = load_calibration_protocol_bytes(
        _protocol(
            (
                ("load-low", low_calibration, low_confirmation),
                ("load-high", high_calibration, high_confirmation),
            )
        )
    )
    rehearsal = compile_rehearsal(
        protocol,
        (
            CandidateStudyRecipe("load-low", low_calibration, low_confirmation),
            CandidateStudyRecipe("load-high", high_calibration, high_confirmation),
        ),
    )
    plan = rehearsal.candidates[0].calibration_plan
    first = plan.trials[0]
    assert len(plan.trials) == 8
    assert first.config.telemetry.warmup_ns == 100_000_000
    assert first.config.telemetry.load_freshness_ns == 100_000_000
    assert first.first_content_slo_ns == 100_000_000
    assert first.completion_slo_ns == 150_000_000
    assert protocol.preparation.warmup_reset_max_duration_ns == 500_000_000
    output = tmp_path / "rehearsal"
    output.mkdir(mode=0o700)
    lifecycle = _RecordingLifecycle(output)
    executor = NativeRehearsalExecutor(warmup_failure=True)

    async def exercise() -> None:
        with (
            native_rehearsal_execution(executor),
            pytest.raises(
                EvaluationError,
                match="incomplete calibration study cannot select a level",
            ),
        ):
            await run_rehearsal(
                rehearsal, output, lifecycle=lifecycle, executor=executor
            )
        assert executor.calls == [first]
        executor.assert_quiescent()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(exercise())
    assert lifecycle.events == [
        ("prepare", first.trial_id),
        ("cleanup", first.trial_id),
    ]
    study_path = output / "calibration-load-low"
    with StudyDirectory.open(study_path) as directory:
        manifest = read_manifest(directory, plan)
        content = directory.read(
            trial_filename(0), limit=plan.config.limits.per_trial_result_bytes
        )
    assert manifest.status == "ABORTED" and manifest.reason == "WARMUP_FAILED"
    entry = manifest.trials[0]
    assert entry.state == "RETURNED" and entry.result_status == "WARMUP_FAILED"
    assert entry.failure_reason is None
    assert entry.cleanup == "CONFIRMED_BY_LOCAL_RESULT"
    assert entry.result_sha256 == sha256_digest(content)
    assert all(
        row.state == "NOT_RUN"
        and row.result_filename is None
        and row.result_sha256 is None
        and row.cleanup == "NOT_STARTED"
        for row in manifest.trials[1:]
    )
    assert len(manifest.trials[1:]) == 7
    assert sorted(path.name for path in study_path.glob("trial-*.json")) == [
        trial_filename(0)
    ]

    retained = load_study_trial_bytes(content, plan, first)
    assert retained.status == "WARMUP_FAILED"
    assert (
        retained.evidence_class
        == retained.foreground.evidence_class
        == "SYNTHETIC_ONLY"
    )
    raw = json.loads(content)["result"]
    assert raw["cleanup"] == "CLIENT_TASKS_AND_CONNECTIONS_CLOSED"
    assert raw["decisions"] == []
    foreground = raw["foreground"]
    assert foreground["offered_count"] == 7
    assert foreground["dispatched_count"] == 0
    records = foreground["records"]
    assert [row["request_index"] for row in records] == list(range(7))
    assert [row["scheduled_ns"] for row in records] == [
        offer.scheduled_ns for offer in first.config.foreground.offers
    ]
    assert all(
        row["outcome"] == "CANCELLED"
        and row["attempts"] == 0
        and row["dispatch_ns"] is None
        and row["response_headers_ns"] is None
        and row["first_content_ns"] is None
        for row in records
    )
    warmup_ns = first.config.telemetry.warmup_ns
    assert raw["events"] == [{"kind": "WARMUP_FAILED", "observed_ns": warmup_ns}]
    observations = raw["observations"]
    independent = [row for row in observations if row["channel"] == "INDEPENDENT_LOAD"]
    assert {row["endpoint_id"] for row in independent} == {"endpoint-a", "endpoint-b"}
    assert all(
        row["status"] in {"MISSING", "CANCELLED"}
        and row["running"] is None
        and row["waiting"] is None
        and row["published_to_router"] is None
        for row in independent
    )
    before_warmup = [row for row in independent if row["completed_ns"] < warmup_ns]
    assert {row["endpoint_id"] for row in before_warmup} == {"endpoint-a", "endpoint-b"}
    assert all(row["status"] == "MISSING" for row in before_warmup)
    # Polls due exactly at the failed gate may be cancelled during native cleanup.
    assert all(
        row["started_ns"] == row["completed_ns"] == warmup_ns
        for row in observations
        if row["status"] == "CANCELLED"
    )
    assert all(
        0
        <= row["started_ns"]
        <= row["completed_ns"]
        <= row["published_ns"]
        <= warmup_ns
        for row in observations
    )
    assert {row["channel"] for row in observations if row["status"] == "VALID"} == {
        "HEALTH",
        "ROUTER_LOAD",
    }

    lifecycle_receipt = json.loads(
        (output / "calibration-load-low-lifecycle.json").read_bytes()
    )
    assert lifecycle_receipt["phase"] == "CALIBRATION"
    assert len(lifecycle_receipt["receipts"]) == 1
    receipt = lifecycle_receipt["receipts"][0]
    assert receipt["source_trial_id"] == first.trial_id
    assert receipt["reset_and_warmup"] == "COMPLETED"
    assert receipt["cleanup"] == "CONFIRMED"
    assert receipt["execute_elapsed_ns"] is not None

    # The failed phase may retain its diagnostic report; it cannot select a
    # candidate, begin the next calibration, or publish confirmation/catalog data.
    report = json.loads(
        (output / "calibration-load-low-report" / "report.json").read_bytes()
    )
    assert report["status"] == "ABORTED"
    assert report["coverage"]["warmup_failed_results"] == 1
    assert report["coverage"]["foreground"]["returned_records"] == 7
    assert report["coverage"]["not_run_trials"] == 7
    assert not any(output.glob("calibration-load-high*"))
    assert not any(output.glob("calibration-selection*"))
    assert not any(output.glob("confirmation*"))
    assert not any(output.glob("*catalog*"))
    diagnostics = _rehearsal_failure_diagnostics(output)
    assert len(diagnostics) <= 32_768
    for expected in (
        "warmup telemetry:",
        "INDEPENDENT_LOAD",
        "MISSING",
        "WARMUP_FAILED",
        "started_ns",
        "completed_ns",
        "published_ns",
        "observed_ns",
    ):
        assert expected in diagnostics
    for private in (
        *origins,
        first.config.foreground.model,
        *(offer.prompt for offer in first.config.foreground.offers),
    ):
        assert private not in diagnostics
