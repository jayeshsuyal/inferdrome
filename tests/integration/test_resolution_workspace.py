"""Resolved inputs remain stable through the persisted lifecycle."""

from pathlib import Path

import yaml

from inferdrome.domain.ids import sha256_digest
from inferdrome.domain.states import IntegrityStatus, RunState
from inferdrome.resolution.resolver import resolve_experiment
from inferdrome.workspace.run_workspace import RunWorkspace


def test_resolve_reserve_reopen_and_complete(tmp_path: Path) -> None:
    workload_bytes = b'{"prompt":"integrated evidence"}\n'
    (tmp_path / "workload.jsonl").write_bytes(workload_bytes)
    source = {
        "schema_version": "inferdrome.source-experiment.v1",
        "experiment": {"id": "integrated"},
        "execution": {"mode": "synthetic_fixture"},
        "target": {"engine": "fake"},
        "workload": {
            "path": "workload.jsonl",
            "sha256": sha256_digest(workload_bytes),
        },
        "traffic": {"kind": "concurrent", "measured_requests": 1},
    }
    source_path = tmp_path / "experiment.yaml"
    source_path.write_text(yaml.safe_dump(source, sort_keys=False), encoding="utf-8")

    resolution = resolve_experiment(
        source_path,
        run_id="run-33333333333333333333333333333333",
    )
    workspace = RunWorkspace.reserve(tmp_path / "runs", resolution)
    for state in (
        RunState.PREFLIGHT,
        RunState.WARMUP,
        RunState.MEASURING,
        RunState.FINALIZING,
    ):
        workspace = RunWorkspace.open(workspace.path)
        workspace.transition(state)

    workspace = RunWorkspace.open(workspace.path)
    workspace.transition(RunState.COMPLETE, integrity_status=IntegrityStatus.VALID)

    completed = RunWorkspace.open(workspace.path)
    assert completed.current_state().state is RunState.COMPLETE
    assert completed.current_state().integrity_status is IntegrityStatus.VALID
    completed.verify_frozen_inputs()
