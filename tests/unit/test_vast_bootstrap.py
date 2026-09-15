"""Empty guest rehearsal: all identity/GPU/serving/transport facts are synthetic."""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from inferdrome.deployment import vast_bootstrap as boot
from inferdrome.deployment import vast_process as prep
from inferdrome.deployment import vast_process_runtime as runtime
from inferdrome.deployment.vast_transfer import BrokerGate
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.contracts import fixed_selected_workload_bytes
from inferdrome.routing_execution.executor import (
    ManualMonotonicClock,
    run_execution_from_bytes,
)
from inferdrome.routing_execution.package import (
    VerificationError,
    verify_execution_package,
)
from tests.routing_execution_support import StaticEndpointTransport
from tests.unit.test_vast_process import SupervisorHarness, process_input_value


def launch_intent() -> boot.LaunchIntent:
    now = datetime.now(UTC).replace(microsecond=0)
    value = process_input_value(Path("/workspace/private"))
    return boot.LaunchIntent.model_validate_json(
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.vast-launch-intent.v1",
                "run_nonce": "1" * 32,
                "source_commit": value["source_commit"],
                "container_image": value["container_image"],
                "offer_id": 202,
                "accountable_operator": "synthetic-operator",
                "module_sha256": prep.module_digests(),
                "transfer_prerequisites": {
                    "broker_contract_evidence_sha256": sha256_digest(
                        b"synthetic contract evidence"
                    ),
                    "trusted_key_source": "OPERATOR_OUT_OF_BAND",
                    "trusted_key_evidence_sha256": sha256_digest(
                        b"synthetic separate key evidence"
                    ),
                    "uid2000_gid0_compatibility": "OPERATOR_VERIFIED",
                    "assertion": "OPERATOR_DECLARED_NOT_PROVIDER_ATTESTED",
                },
                "execution_deadline_utc": (now + timedelta(seconds=3000)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "cleanup_deadline_utc": (now + timedelta(seconds=3060)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "stage_timeout_seconds": 100,
                "approval_timeout_seconds": 100,
                "retrieval_timeout_seconds": 100,
                "request_timeout_ms": 1000,
                "readiness_timeout_seconds": 10,
                "campaign_timeout_seconds": 20,
            }
        )
    )


class FakeTransfer:
    instance_id = 101

    def __init__(self, root: Path) -> None:
        self.root = root
        self.gate = BrokerGate(
            instance_id=101,
            host="fake.example.invalid",
            port=2244,
            identity=root / "fake-unused-identity",
            known_hosts=root / "fake-unused-known-hosts",
            known_hosts_sha256=sha256_digest(b"fake"),
            host_key_sha256=sha256_digest(b"fake"),
            broker_contract_verified=True,
            broker_contract_evidence_sha256=sha256_digest(
                b"synthetic contract evidence"
            ),
            broker_contract_source="synthetic only",
            trusted_key_source="OPERATOR_OUT_OF_BAND",
            trusted_key_evidence_sha256=sha256_digest(
                b"synthetic separate key evidence"
            ),
            remote_uid=2000,
            remote_gid=0,
        )
        self.sent: list[str] = []
        self.received: list[str] = []

    def send(self, local: Path, remote_relative: str, seconds: float) -> None:
        assert seconds > 0
        destination = self.root / remote_relative
        boot.write_record(destination.parent, destination.name, local.read_bytes())
        self.sent.append(remote_relative)

    def receive(self, remote_relative: str, local: Path, seconds: float) -> None:
        assert seconds > 0
        boot.write_record(
            local.parent, local.name, (self.root / remote_relative).read_bytes()
        )
        self.received.append(remote_relative)


class Rehearsal:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.intent = launch_intent()
        self.root = tmp_path / "guest"
        self.guest = boot.GuestBootstrap(
            self.intent.run_nonce,
            boot.record_digest(self.intent),
            self.intent.execution_deadline_utc,
            root=self.root,
        )
        observed = process_input_value(tmp_path)
        monkeypatch.setattr(
            boot,
            "observe_guest",
            lambda root, budget: {
                key: observed[key]
                for key in (
                    "instance_id",
                    "source_commit",
                    "module_sha256",
                    "gpu_uuids",
                    "uid",
                    "gid",
                )
            },
        )
        self.model = tmp_path / "source-model"
        self.model.mkdir(mode=0o700)
        content = b"synthetic pinned model bytes"
        (self.model / "weights.safetensors").write_bytes(content)
        monkeypatch.setattr(
            boot,
            "qwen3_model_manifest",
            lambda: {
                "files": [
                    {
                        "path": "weights.safetensors",
                        "size_bytes": len(content),
                        "sha256": sha256_digest(content),
                    }
                ]
            },
        )
        self.transfer = FakeTransfer(self.root)
        self.reviewed: list[bytes] = []
        self.harness = SupervisorHarness()
        self.executions = 0

        def approve(content: bytes, seconds: float) -> str:
            assert seconds > 0
            self.reviewed.append(content)
            return sha256_digest(content)

        self.workflow = boot.BootstrapWorkflow(
            self.intent,
            lambda instance_id: self.transfer,
            tmp_path / "operator",
            self.model,
            lambda instance_id: prep.VastLaunchReadback.model_validate_json(
                canonical_json_bytes(observed["launch_readback"])
            ),
            approve,
            guest_root=self.root,
        )

        def execute(
            directory: Path, digest: str, operator: str, accepted: bool
        ) -> dict[str, str]:
            assert accepted and operator == "synthetic-operator"
            spec = prep.load_plan(directory, digest)
            self.executions += 1
            # Real supervisor state machine with two fake children and observer;
            # real fixed 18-terminal capture/sealer using injected endpoint IO.
            self.harness.run()
            config = prep.routing_config(spec, {"synthetic": True})
            package = run_execution_from_bytes(
                canonical_json_bytes(config.model_dump(mode="json")),
                fixed_selected_workload_bytes(),
                Path(spec.evidence_path) / "routing-execution-package",
                transport_factory=lambda: StaticEndpointTransport(),
                clock=ManualMonotonicClock(),
            )
            return {"retained_digest": package.retained_digest}

        monkeypatch.setattr(runtime, "execute", execute)

    def stage(self) -> None:
        self.guest.initialize()
        self.workflow.readiness(101, seconds=100)
        self.workflow.stage(101, seconds=100)
        self.guest.stage()


def test_empty_guest_through_two_fake_engines_approval_and_verified_retrieval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rehearsal = Rehearsal(tmp_path, monkeypatch)
    assert not rehearsal.root.exists()
    rehearsal.stage()
    assert rehearsal.executions == 0 and rehearsal.harness.children == []
    assert rehearsal.transfer.sent[-1] == "inbox/stage.json"
    rehearsal.workflow.approve(101, seconds=100)
    plan = boot.read_record(
        rehearsal.root / "outbox", "plan.json", prep.VastProcessInput
    )
    assert rehearsal.reviewed == [boot.record_bytes(plan)]
    assert rehearsal.transfer.sent[-1] == "inbox/approval.json"
    result = rehearsal.guest.execute_approved()
    assert result.status == "GUEST_EXPORT_VERIFIED_NOT_YET_RETRIEVED"
    assert rehearsal.workflow.retained_digest is None
    rehearsal.workflow.run(101, seconds=100)
    rehearsal.workflow.retrieve(101, seconds=100)
    verified = verify_execution_package(
        rehearsal.workflow.local / "retrieved-package",
        expected_digest=result.retained_digest,
    )
    assert len(verified.producer_receipt.terminal_outcomes) == 18
    assert rehearsal.workflow.retained_digest == result.retained_digest
    assert rehearsal.executions == 1 and len(rehearsal.harness.children) == 3
    assert rehearsal.harness.stopped == [102, 101, 100]
    assert {
        path.name for path in (rehearsal.workflow.local / "retrieved-package").iterdir()
    } == set(boot.EXPORT_FILES)
    with pytest.raises(FileExistsError):
        rehearsal.guest.execute_approved()
    assert rehearsal.executions == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_nonce", "2" * 32),
        ("instance_id", 999),
        ("plan_sha256", "sha256:" + "0" * 64),
        ("accountable_operator", "someone-else"),
        ("execution_deadline_utc", "2000-01-01T00:00:00Z"),
        ("accept_manual_cleanup_risk", False),
    ],
)
def test_wrong_exact_approval_never_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    rehearsal = Rehearsal(tmp_path, monkeypatch)
    rehearsal.stage()
    rehearsal.workflow.approve(101, seconds=100)
    path = rehearsal.root / "inbox" / "approval.json"
    record = json.loads(path.read_bytes())
    record[field] = value
    path.write_bytes(canonical_json_bytes(record))
    with pytest.raises(ValueError):
        rehearsal.guest.execute_approved()
    assert rehearsal.executions == 0


@pytest.mark.parametrize(
    "mutation", ["partial", "tamper", "extra", "symlink", "hardlink"]
)
def test_untrusted_model_staging_fails_before_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    rehearsal = Rehearsal(tmp_path, monkeypatch)
    rehearsal.guest.initialize()
    rehearsal.workflow.readiness(101, seconds=100)
    rehearsal.workflow.stage(101, seconds=100)
    inbox = rehearsal.root / "inbox" / "model"
    path = inbox / "weights.safetensors"
    if mutation == "partial":
        path.write_bytes(b"partial")
    elif mutation == "tamper":
        path.write_bytes(b"X" * path.stat().st_size)
    elif mutation == "extra":
        (inbox / "unexpected").write_bytes(b"unexpected")
    elif mutation == "symlink":
        path.unlink()
        path.symlink_to(rehearsal.model / path.name)
    else:
        os.link(path, tmp_path / "foreign-link")
    with pytest.raises((ValueError, OSError)):
        rehearsal.guest.stage()
    assert not (rehearsal.root / "outbox" / "plan.json").exists()
    assert rehearsal.executions == 0


def test_mismatched_transport_id_is_rejected_before_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rehearsal = Rehearsal(tmp_path, monkeypatch)
    rehearsal.transfer.instance_id = 999
    with pytest.raises(boot.BootstrapFailure, match="TRANSPORT_ID"):
        rehearsal.workflow.readiness(101, seconds=100)
    assert rehearsal.transfer.received == []


def test_partial_stage_record_and_reused_root_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rehearsal = Rehearsal(tmp_path, monkeypatch)
    rehearsal.guest.initialize()
    with pytest.raises(FileExistsError):
        rehearsal.guest.initialize()
    rehearsal.workflow.readiness(101, seconds=100)
    rehearsal.workflow.stage(101, seconds=100)
    (rehearsal.root / "inbox" / "stage.json").write_bytes(b'{"schema_version":')
    with pytest.raises(ValueError):
        rehearsal.guest.stage()
    assert rehearsal.executions == 0


def test_retrieval_rejects_tampered_package_and_keeps_receipt_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rehearsal = Rehearsal(tmp_path, monkeypatch)
    rehearsal.stage()
    rehearsal.workflow.approve(101, seconds=100)
    rehearsal.guest.execute_approved()
    rehearsal.workflow.run(101, seconds=100)
    tampered = rehearsal.root / "outbox" / "export" / "producer-receipt.json"
    tampered.chmod(0o600)
    tampered.write_bytes(b"{}")
    with pytest.raises(VerificationError):
        rehearsal.workflow.retrieve(101, seconds=100)
    assert rehearsal.workflow.retained_digest is None
    assert not (rehearsal.workflow.local / "retrieval-verified.json").exists()


def test_missing_or_declined_approval_never_starts_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rehearsal = Rehearsal(tmp_path, monkeypatch)
    rehearsal.stage()
    with pytest.raises(FileNotFoundError):
        rehearsal.guest.execute_approved()
    rehearsal.workflow.approve_plan = lambda content, seconds: "sha256:" + "0" * 64
    with pytest.raises(boot.BootstrapFailure, match="APPROVAL_REQUIRED"):
        rehearsal.workflow.approve(101, seconds=100)
    assert rehearsal.executions == 0


def test_expired_and_rollback_clocks_cannot_renew_guest_budget() -> None:
    now = datetime(2026, 9, 13, tzinfo=UTC)
    ticks = [0.0]
    wall = [now]
    budget = boot.Budget(
        (now + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        monotonic=lambda: ticks[0],
        now=lambda: wall[0],
    )
    ticks[0] = 1.1
    wall[0] = now - timedelta(hours=1)
    with pytest.raises(boot.BootstrapFailure, match="DEADLINE"):
        budget.remaining()


def test_record_publication_is_private_durable_and_no_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(boot.os, "fsync", fsync)
    boot.write_record(tmp_path, "record.json", b"{}")
    assert len(calls) >= 2
    assert (tmp_path / "record.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        boot.write_record(tmp_path, "record.json", b"different")
    assert (tmp_path / "record.json").read_bytes() == b"{}"


def test_bootstrap_alarm_cannot_skip_remaining_child_cleanup() -> None:
    harness = SupervisorHarness()
    original_stop = harness.stop

    def stop(child: runtime.Child) -> None:
        original_stop(child)
        if len(harness.stopped) == 1:
            os.kill(os.getpid(), signal.SIGALRM)

    def expired(signum: int, frame: Any) -> None:
        del signum, frame
        raise boot.BootstrapFailure("SYNTHETIC_OUTER_DEADLINE")

    previous = signal.signal(signal.SIGALRM, expired)
    harness.stop = stop  # type: ignore[method-assign]
    try:
        with pytest.raises(boot.BootstrapFailure, match="OUTER_DEADLINE"):
            harness.run()
        assert harness.stopped == [102, 101, 100]
    finally:
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize("change", [None, {}, {"trusted_key_source": "SSH_KEYSCAN"}])
def test_unknown_transfer_prerequisites_cannot_form_launch_intent(change: Any) -> None:
    value = launch_intent().model_dump(mode="json")
    value["transfer_prerequisites"] = change
    with pytest.raises(ValueError):
        boot.LaunchIntent.model_validate_json(canonical_json_bytes(value))


def test_postcreate_broker_must_match_approved_trust_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rehearsal = Rehearsal(tmp_path, monkeypatch)
    rehearsal.transfer.gate = replace(
        rehearsal.transfer.gate,
        trusted_key_evidence_sha256=sha256_digest(b"different source"),
    )
    with pytest.raises(boot.BootstrapFailure, match="TRANSPORT_ID"):
        rehearsal.workflow.readiness(101, seconds=100)
    assert not rehearsal.transfer.received


def test_blocking_approver_is_interrupted_at_phase_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inferdrome.deployment.vast_control import ControlFailure

    rehearsal = Rehearsal(tmp_path, monkeypatch)
    rehearsal.stage()

    def blocking(content: bytes, seconds: float) -> str:
        time.sleep(5)
        return sha256_digest(content)

    rehearsal.workflow.approve_plan = blocking
    started = time.monotonic()
    with pytest.raises(ControlFailure, match="TIMEOUT"):
        rehearsal.workflow.approve(101, seconds=0.05)
    assert time.monotonic() - started < 1
    assert not (rehearsal.root / "inbox" / "approval.json").exists()
    assert rehearsal.executions == 0
