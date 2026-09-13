"""Full local rehearsal; broker/provider/guard/GPU IO is explicitly fake."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inferdrome.deployment import vast_bootstrap as boot
from inferdrome.deployment import vast_control as control
from inferdrome.deployment import vast_transfer as transfer
from tests.unit.test_vast_bootstrap import Rehearsal
from tests.unit.test_vast_control import FakeClock, FakeGuard, FakeProvider
from tests.unit.test_vast_transfer import gate as broker_gate  # noqa: F401


@pytest.mark.parametrize(
    "failure", [None, "engine", "retrieval", "approval", "never-started", "destroy"]
)
def test_empty_guest_broker_control_and_external_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broker_gate: transfer.BrokerGate,  # noqa: F811 - imported pytest fixture
    failure: str | None,
) -> None:
    rehearsal = Rehearsal(tmp_path, monkeypatch)
    monkeypatch.setattr(transfer, "qwen3_model_manifest", boot.qwen3_model_manifest)
    commands: list[transfer.TransferCommand] = []

    def broker_runner(command: transfer.TransferCommand, seconds: float) -> None:
        assert seconds > 0
        commands.append(command)
        remote = command.argv[-1 if command.direction == "send" else -2]
        prefix = "vastai_kaalia@broker.example.invalid::101//workspace/vast-bootstrap/"
        assert remote.startswith(prefix)
        relative = remote.removeprefix(prefix)
        location = rehearsal.root / relative
        if command.direction == "send":
            boot.write_record(
                location.parent, location.name, command.data_path.read_bytes()
            )
        else:
            if failure == "retrieval" and relative.startswith("outbox/export/"):
                raise transfer.TransferFailure("SYNTHETIC_RETRIEVAL_FAILURE")
            command.data_path.write_bytes(location.read_bytes())
            command.data_path.chmod(0o600)

    broker = transfer.BrokerTransfer(
        replace(broker_gate, instance_id=101), runner=broker_runner
    )
    rehearsal.workflow.transfer_for = lambda instance_id: broker
    original_ready = rehearsal.workflow.readiness
    original_stage = rehearsal.workflow.stage
    original_approve = rehearsal.workflow.approve

    def readiness(instance_id: int, *, seconds: float) -> None:
        if failure == "never-started":
            raise TimeoutError("synthetic guest did not start")
        rehearsal.guest.initialize()
        original_ready(instance_id, seconds=seconds)

    def stage(instance_id: int, *, seconds: float) -> None:
        original_stage(instance_id, seconds=seconds)
        rehearsal.guest.stage()

    def approve(instance_id: int, *, seconds: float) -> None:
        original_approve(instance_id, seconds=seconds)
        rehearsal.guest.execute_approved()

    monkeypatch.setattr(rehearsal.workflow, "readiness", readiness)
    monkeypatch.setattr(rehearsal.workflow, "stage", stage)
    monkeypatch.setattr(rehearsal.workflow, "approve", approve)
    if failure == "engine":
        rehearsal.harness.engine_death = 0.0
    if failure == "approval":
        rehearsal.workflow.approve_plan = lambda content, seconds: "sha256:" + "0" * 64

    clock = FakeClock()
    clock.start = datetime.now(UTC)
    intent = control.ControlIntent(
        launch_request_sha256=boot.record_digest(rehearsal.intent),
        execution_deadline_utc=rehearsal.intent.execution_deadline_utc,
        cleanup_deadline_utc=rehearsal.intent.cleanup_deadline_utc,
        operation_timeout_seconds=2.0,
        poll_seconds=1.0,
    )
    directory = tmp_path / "control"
    directory.mkdir(mode=0o700)
    journal = control.ControlJournal(directory)
    provider = FakeProvider(clock, journal)
    provider.create_value = control.CreateResult(new_contract=101)
    provider.destroy_value = control.DestroyResult(instance_id=101, acknowledged=True)
    provider.observe_value = control.AbsenceResult(
        query_instance_id=101,
        succeeded=True,
        matching_instance_ids=(),
        pagination_exhausted=True,
        persistent_volume_ids=(),
    )
    provider.destroy_error = failure == "destroy"
    try:
        outcome = control.execute_control(
            intent,
            journal,
            approved_intent_sha256=intent.intent_sha256,
            provider=provider,
            workflow=rehearsal.workflow,
            guard=FakeGuard(),
            clock=clock,
        )
        assert outcome.instance_id == 101
        assert len([call for call in provider.calls if call[0] == "create"]) == 1
        assert all(
            identity == 101 for kind, identity, _ in provider.calls if kind != "create"
        )
        assert outcome.cleanup_status == (
            control.UNCONFIRMED if failure == "destroy" else control.CONFIRMED
        )
        if failure in (None, "destroy"):
            assert outcome.work_status == "RETRIEVED"
            assert rehearsal.workflow.retained_digest is not None
            assert rehearsal.executions == 1 and len(rehearsal.harness.children) == 3
            assert rehearsal.harness.stopped == [102, 101, 100]
            assert (
                len(commands) == 11
            )  # ready + intent/model/stage + plan/approval + result + four exports
        else:
            assert outcome.work_status == "FAILED"
            assert rehearsal.workflow.retained_digest is None
        if failure in ("approval", "never-started"):
            assert rehearsal.executions == 0
        if failure == "destroy":
            assert clock.elapsed <= 61
    finally:
        journal.close()
