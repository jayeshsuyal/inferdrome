"""Atomic reservation and persisted run-state invariants."""

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from inferdrome.domain.ids import sha256_digest
from inferdrome.domain.states import IntegrityStatus, RunState
from inferdrome.errors import WorkLimitError, WorkspaceError
from inferdrome.limits import WorkBudget, WorkLimits
from inferdrome.resolution.resolver import ResolutionResult, resolve_experiment
from inferdrome.workspace.run_workspace import RunWorkspace

RUN_ID = "run-22222222222222222222222222222222"
CREATED_AT = datetime(2026, 8, 5, 22, 0, tzinfo=UTC)


def _resolution(tmp_path: Path) -> ResolutionResult:
    workload = b'{"prompt":"workspace alpha"}\n{"prompt":"workspace beta"}\n'
    (tmp_path / "workload.jsonl").write_bytes(workload)
    source = {
        "schema_version": "inferdrome.source-experiment.v1",
        "experiment": {"id": "workspace-test"},
        "execution": {"mode": "synthetic_fixture"},
        "target": {"engine": "fake"},
        "workload": {
            "path": "workload.jsonl",
            "sha256": sha256_digest(workload),
        },
        "traffic": {
            "kind": "concurrent",
            "measured_requests": 2,
        },
    }
    source_path = tmp_path / "experiment.yaml"
    source_path.write_text(yaml.safe_dump(source, sort_keys=False), encoding="utf-8")
    return resolve_experiment(source_path, run_id=RUN_ID)


def test_reservation_writes_exact_read_only_inputs(tmp_path: Path) -> None:
    resolution = _resolution(tmp_path)
    workspace = RunWorkspace.reserve(
        tmp_path / "runs", resolution, created_at=CREATED_AT
    )

    assert workspace.run_id == RUN_ID
    assert (
        workspace.input_directory / "experiment.original.yaml"
    ).read_bytes() == resolution.source_bytes
    assert (
        workspace.input_directory / "workload.source.jsonl"
    ).read_bytes() == resolution.workload_bytes
    assert (
        workspace.input_directory / "experiment.resolved.json"
    ).read_bytes() == resolution.resolved_spec_bytes
    assert (
        workspace.input_directory / "request-plan.json"
    ).read_bytes() == resolution.request_plan_bytes
    for descriptor in workspace.metadata.frozen_inputs:
        mode = stat.S_IMODE((workspace.path / descriptor.path).stat().st_mode)
        assert mode == 0o400

    state = workspace.current_state()
    assert state.state is RunState.CREATED
    assert state.sequence_index == 0
    assert state.occurred_at == CREATED_AT
    workspace.verify_frozen_inputs()


def test_duplicate_run_reservation_never_overwrites(tmp_path: Path) -> None:
    resolution = _resolution(tmp_path)
    first = RunWorkspace.reserve(tmp_path / "runs", resolution)
    original = (first.input_directory / "experiment.original.yaml").read_bytes()

    with pytest.raises(WorkspaceError, match="already reserved"):
        RunWorkspace.reserve(tmp_path / "runs", resolution)

    assert (first.input_directory / "experiment.original.yaml").read_bytes() == original


def test_valid_lifecycle_persists_append_only_events(tmp_path: Path) -> None:
    workspace = RunWorkspace.reserve(
        tmp_path / "runs", _resolution(tmp_path), created_at=CREATED_AT
    )
    current_time = CREATED_AT
    for target in (
        RunState.PREFLIGHT,
        RunState.WARMUP,
        RunState.MEASURING,
        RunState.FINALIZING,
    ):
        current_time += timedelta(seconds=1)
        state = workspace.transition(target, occurred_at=current_time)
        assert state.state is target

    with pytest.raises(WorkspaceError, match="integrity verification"):
        workspace.transition(
            RunState.COMPLETE,
            occurred_at=current_time + timedelta(seconds=1),
        )

    final = workspace.transition(
        RunState.COMPLETE,
        occurred_at=current_time + timedelta(seconds=1),
        integrity_status=IntegrityStatus.VALID,
    )
    assert final.sequence_index == 5
    assert final.integrity_status is IntegrityStatus.VALID
    state_mode = (workspace.control_directory / "state.json").stat().st_mode
    assert stat.S_IMODE(state_mode) == 0o400
    assert len(list((workspace.control_directory / "events").iterdir())) == 6


def test_invalid_or_backward_transition_does_not_advance_state(tmp_path: Path) -> None:
    workspace = RunWorkspace.reserve(
        tmp_path / "runs", _resolution(tmp_path), created_at=CREATED_AT
    )

    with pytest.raises(WorkspaceError, match="invalid run-state transition"):
        workspace.transition(RunState.MEASURING)
    with pytest.raises(WorkspaceError, match="move backward"):
        workspace.transition(
            RunState.PREFLIGHT,
            occurred_at=CREATED_AT - timedelta(seconds=1),
        )
    with pytest.raises(WorkspaceError, match="reserved for COMPLETE"):
        workspace.transition(
            RunState.PREFLIGHT,
            integrity_status=IntegrityStatus.VALID,
        )
    assert workspace.current_state().state is RunState.CREATED


def test_reopen_verifies_metadata_inputs_and_latest_event(tmp_path: Path) -> None:
    workspace = RunWorkspace.reserve(tmp_path / "runs", _resolution(tmp_path))
    workspace.transition(RunState.PREFLIGHT)

    reopened = RunWorkspace.open(workspace.path)

    assert reopened.run_id == RUN_ID
    assert reopened.current_state().state is RunState.PREFLIGHT
    reopened.transition(RunState.WARMUP)
    assert workspace.current_state().state is RunState.WARMUP


def test_reopen_charges_exact_authoritative_workspace_bytes(tmp_path: Path) -> None:
    workspace = RunWorkspace.reserve(tmp_path / "runs", _resolution(tmp_path))
    charged_paths = (
        workspace.control_directory / "resolution.json",
        *(workspace.path / item.path for item in workspace.metadata.frozen_inputs),
        workspace.control_directory / "state.json",
        workspace.control_directory / "events" / "00000000-CREATED.json",
    )
    expected_bytes = sum(path.stat().st_size for path in charged_paths)
    exact_budget = WorkBudget(
        WorkLimits(max_units=1, max_bytes=expected_bytes, max_seconds=1.0),
        clock=lambda: 1.0,
    )

    RunWorkspace.open(workspace.path, work_budget=exact_budget)

    assert exact_budget.bytes == expected_bytes

    bounded_budget = WorkBudget(
        WorkLimits(max_units=1, max_bytes=expected_bytes - 1, max_seconds=1.0),
        clock=lambda: 1.0,
    )
    with pytest.raises(WorkLimitError, match="byte limit"):
        RunWorkspace.open(workspace.path, work_budget=bounded_budget)
    assert bounded_budget.bytes < expected_bytes


def test_state_history_is_bounded_before_event_reads(tmp_path: Path) -> None:
    workspace = RunWorkspace.reserve(tmp_path / "runs", _resolution(tmp_path))
    events = workspace.control_directory / "events"
    for index in range(1, 7):
        (events / f"{index:08d}-UNTRUSTED.json").write_bytes(b"not-json")

    with pytest.raises(WorkspaceError, match="exceeds its event limit"):
        workspace.current_state()


def test_frozen_input_mutation_is_detected_before_transition(tmp_path: Path) -> None:
    workspace = RunWorkspace.reserve(tmp_path / "runs", _resolution(tmp_path))
    target = workspace.input_directory / "request-plan.json"
    target.chmod(0o600)
    target.write_bytes(target.read_bytes() + b" ")

    with pytest.raises(WorkspaceError, match="writable"):
        workspace.verify_frozen_inputs()
    with pytest.raises(WorkspaceError, match="writable"):
        workspace.transition(RunState.PREFLIGHT)
    assert workspace.current_state().state is RunState.CREATED


def test_state_snapshot_disagreement_is_detected(tmp_path: Path) -> None:
    workspace = RunWorkspace.reserve(tmp_path / "runs", _resolution(tmp_path))
    state_path = workspace.control_directory / "state.json"
    state = json.loads(state_path.read_bytes())
    state["integrity_status"] = "INVALID"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(WorkspaceError, match="disagrees"):
        workspace.current_state()


def test_missing_historical_event_is_detected(tmp_path: Path) -> None:
    workspace = RunWorkspace.reserve(tmp_path / "runs", _resolution(tmp_path))
    workspace.transition(RunState.PREFLIGHT)
    event = workspace.control_directory / "events" / "00000000-CREATED.json"
    event.unlink()

    with pytest.raises(WorkspaceError, match="incomplete"):
        workspace.current_state()
