"""Real v2 control/admission/sealing; synthetic SFTP, HTTP, identity and GPU IO.

This exercises transport and guest code through real temporary files, never a
downloader, SSH daemon/client, provider socket, registry or GPU. Linux UID/chroot
enforcement still requires the separately authorized image rehearsal.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from inferdrome.deployment import vast_bootstrap as boot
from inferdrome.deployment import vast_control as control
from inferdrome.deployment import vast_guest_ssh as guest_ssh
from inferdrome.deployment import vast_model_stage as model_stage
from inferdrome.deployment import vast_process as prep
from inferdrome.deployment.vast_provider import HttpReply, VastProvider
from inferdrome.deployment.vast_sftp import SftpTransfer, TransferCommand
from inferdrome.deployment.vast_ssh_trust import SshPinJournal, format_announcement
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.package import verify_execution_package
from tests.unit.test_vast_bootstrap import Rehearsal
from tests.unit.test_vast_control import FakeClock, FakeGuard
from tests.unit.test_vast_guard import FileTransport
from tests.unit.test_vast_model_stage import TINY_FILES, tiny_model_manifest
from tests.unit.test_vast_process import process_input_value
from tests.unit.test_vast_sftp import KEY


class HttpFixture(FileTransport):
    """Use the existing concrete-provider fake, retaining the create body too."""

    def __init__(self, directory: Path, *, missing_volume_info: bool = False) -> None:
        super().__init__(directory, missing_volume_info=missing_volume_info)
        self.creates: list[bytes] = []
        self.log_requests: list[int] = []

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None,
        *,
        seconds: float,
    ) -> HttpReply:
        if method == "PUT" and path == "/api/v0/instances/request_logs/101/":
            assert seconds > 0 and body == b"{}"
            self.log_requests.append(101)
            return HttpReply(
                200,
                canonical_json_bytes(
                    {
                        "success": True,
                        "result_url": "https://s3.amazonaws.com/vast.ai/instance_logs/synthetic_101.log",
                    }
                ),
            )
        if method == "PUT" and path.startswith("/api/v0/asks/"):
            assert body is not None
            self.creates.append(body)
        return super().request(method, path, body, seconds=seconds)


class SftpRehearsal:
    def __init__(
        self,
        directory: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: str | None,
    ) -> None:
        # Reuse the existing actual fixed18 executor/sealer and three-child
        # supervisor harness. Its synthetic v1 scaffolding is not used by v2.
        legacy = directory / "executor-harness"
        legacy.mkdir(mode=0o700)
        self.executor = Rehearsal(legacy, monkeypatch)
        monkeypatch.setattr(boot, "qwen3_model_manifest", tiny_model_manifest)
        monkeypatch.setattr(model_stage, "qwen3_model_manifest", tiny_model_manifest)
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda _path: SimpleNamespace(free=100_000_000_000),
        )
        value = self.executor.intent.model_dump(mode="json")
        now = datetime.now(UTC).replace(microsecond=0)
        value.update(
            {
                "schema_version": "inferdrome.vast-launch-intent.v2",
                "module_sha256": prep.module_digests("v2"),
                "execution_deadline_utc": (now + timedelta(seconds=120)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "cleanup_deadline_utc": (now + timedelta(seconds=140)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "transfer_prerequisites": {
                    "client_public_key": KEY,
                    "host_key_source": "VAST_AUTHENTICATED_EXACT_ID_LOGS",
                    "assertion": (
                        "VAST_CONTROL_PLANE_LOG_BINDING_NOT_HARDWARE_ATTESTATION"
                    ),
                },
                "disk": {
                    "requested_gb": 100,
                    "image_unpacked_bytes": 20_000_000_000,
                    "runtime_headroom_bytes": 1_073_741_824,
                    "measurement_sha256": sha256_digest(b"synthetic image measurement"),
                    "assertion": (
                        "OPERATOR_REVIEWED_IMAGE_FOOTPRINT_NOT_PROVIDER_ATTESTED"
                    ),
                },
            }
        )
        self.intent = boot.SftpLaunchIntent.model_validate_json(
            canonical_json_bytes(value)
        )
        self.root = directory / "v2-private-guest"
        self.exchange = directory / "synthetic-chroot"
        self.exchange.mkdir(mode=0o755)
        for name in ("uploads", "downloads"):
            target = self.exchange / name
            target.mkdir(mode=0o750)
            target.chmod(0o750)
        # Do not fake the admission reader. Substitute only the expected foreign
        # identity for this unprivileged local fixture; mode/link/binding checks
        # run unchanged. This is not a Linux UID separation test.
        monkeypatch.setattr(boot, "_UPLOAD_UID", os.getuid())
        monkeypatch.setattr(boot, "_UPLOAD_GID", os.getgid())
        monkeypatch.setattr(guest_ssh, "require_experiment_identity", lambda: None)
        observed = process_input_value(directory)
        observed["module_sha256"] = self.intent.module_sha256

        def observe(
            root: Path,
            budget: boot.Budget,
            *,
            profile: str = "v1",
        ) -> dict[str, object]:
            assert root == self.root and profile == "v2"
            budget.remaining()
            return {
                name: observed[name]
                for name in (
                    "instance_id",
                    "source_commit",
                    "module_sha256",
                    "gpu_uuids",
                    "uid",
                    "gid",
                )
            }

        monkeypatch.setattr(boot, "observe_guest", observe)
        self.guest = boot.GuestBootstrap(
            self.intent.run_nonce,
            boot.record_digest(self.intent),
            self.intent.execution_deadline_utc,
            root=self.root,
            profile="v2",
            exchange=self.exchange,
        )
        self.readback = prep.VastSftpLaunchReadback.model_validate_json(
            canonical_json_bytes(
                {
                    "instance_id": 101,
                    "requested_image": self.intent.container_image.model_dump(
                        mode="json"
                    ),
                    "launch_mode": "args",
                    "user": "0:0",
                    "transfer_uid": 2001,
                    "transfer_gid": 0,
                    "public_port_mappings": [
                        {
                            "purpose": "SSH_MANAGEMENT",
                            "container_port": 2222,
                            "protocol": "tcp",
                            "public_host": "93.184.215.14",
                            "public_port": 2244,
                        }
                    ],
                    "persistent_volume_ids": [],
                    "assertion": "OPERATOR_SUPPLIED_NOT_PROVIDER_ATTESTED",
                    "resolved_image_digest": None,
                }
            )
        )
        identity = directory / "synthetic-identity"
        identity.write_bytes(b"synthetic operator identity, not a real private key")
        identity.chmod(0o600)
        pins = directory / "pins"
        pins.mkdir(mode=0o700)
        self.pins = SshPinJournal(pins)
        self.log_fetches = 0
        self.sftp_commands: list[TransferCommand] = []
        self.sent: list[str] = []
        self.received: list[str] = []
        self.guest_stage_started = False

        def sftp_runner(command: TransferCommand, seconds: float) -> None:
            assert seconds > 0 and command.argv[0] == "/usr/bin/sftp"
            assert command.argv[-1] == "inferdrome-transfer@93.184.215.14"
            self.sftp_commands.append(command)
            lines = (command.directory / "batch").read_text().splitlines()
            for line in lines:
                parts = shlex.split(line)
                if parts[0] == "put":
                    assert parts[:3] == ["put", "-f", "data"] and len(parts) == 4
                    remote = Path(parts[3])
                    assert remote.parent == Path("/uploads") and remote.name.endswith(
                        ".json.pending"
                    )
                    target = self.exchange / "uploads" / remote.name
                    content = command.data_path.read_bytes()
                    if failure == "bad_upload" and remote.name == "stage.json.pending":
                        record = json.loads(content)
                        record["run_nonce"] = "f" * 32
                        content = canonical_json_bytes(record)
                    target.write_bytes(content)
                    # Model stock put source-mode propagation plus sshd umask.
                    target.chmod((command.data_path.stat().st_mode & 0o777) & ~0o027)
                elif parts[0] == "rename":
                    assert len(parts) == 3
                    before, after = Path(parts[1]), Path(parts[2])
                    assert before.parent == after.parent == Path("/uploads")
                    assert before.name == after.name + ".pending"
                    os.replace(
                        self.exchange / "uploads" / before.name,
                        self.exchange / "uploads" / after.name,
                    )
                    self.sent.append("inbox/" + after.name)
                    if after.name == "stage.json":
                        assert not self.guest_stage_started
                        self.guest_stage_started = True
                        self.guest.stage()
                else:
                    assert parts[0] == "get" and parts[-1] == "data" and len(parts) == 3
                    remote = Path(parts[1]).relative_to("/downloads")
                    if failure == "download" and remote.parts[0] == "export":
                        command.data_path.write_bytes(b"partial failed export")
                        raise RuntimeError("synthetic SFTP failure, no remote call")
                    command.data_path.write_bytes(
                        (self.exchange / "downloads" / remote).read_bytes()
                    )
                    command.data_path.chmod(0o600)
                    self.received.append("outbox/" + str(remote))

        self.model_commands: list[TransferCommand] = []
        original_stage = model_stage.stage_pinned_model

        def model_runner(command: TransferCommand, seconds: float) -> None:
            assert seconds > 0
            assert all(not old.directory.exists() for old in self.model_commands)
            self.model_commands.append(command)
            command.data_path.write_bytes(TINY_FILES[command.data_path.name])

        def download(destination: Path, *, seconds: float, headroom_bytes: int) -> None:
            assert headroom_bytes == self.intent.disk.runtime_headroom_bytes
            original_stage(
                destination,
                seconds=seconds,
                runner=model_runner,
                headroom_bytes=headroom_bytes,
            )

        monkeypatch.setattr(model_stage, "stage_pinned_model", download)
        self.reviewed: list[bytes] = []

        def approval(content: bytes, seconds: float) -> str:
            assert seconds > 0
            self.reviewed.append(content)
            return (
                "sha256:" + "0" * 64
                if failure == "approval"
                else sha256_digest(content)
            )

        def fetch_log(result_url: str, *, seconds: float) -> bytes:
            assert seconds > 0
            assert (
                result_url
                == "https://s3.amazonaws.com/vast.ai/instance_logs/synthetic_101.log"
            )
            self.log_fetches += 1
            return format_announcement(101, self.intent.run_nonce, KEY)

        def attach_provider(provider: VastProvider) -> None:
            self.workflow = boot.sftp_workflow(
                self.intent,
                provider,
                self.pins,
                identity,
                directory / "operator-v2",
                lambda instance_id: self.readback,
                approval,
                guest_root=self.root,
                runner=sftp_runner,
                fetcher=fetch_log,
            )
            ready, approve = self.workflow.readiness, self.workflow.approve

            def drive_ready(instance_id: int, *, seconds: float) -> None:
                self.guest.initialize()
                ready(instance_id, seconds=seconds)

            def drive_approve(instance_id: int, *, seconds: float) -> None:
                approve(instance_id, seconds=seconds)
                self.guest.execute_approved()

            monkeypatch.setattr(self.workflow, "readiness", drive_ready)
            monkeypatch.setattr(self.workflow, "approve", drive_approve)

        self.attach_provider = attach_provider


@pytest.mark.parametrize(
    "failure", [None, "approval", "bad_upload", "download", "missing_volume"]
)
def test_sftp_v2_fresh_guest_through_fixed18_retrieval_and_exact_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    rehearsal = SftpRehearsal(tmp_path, monkeypatch, failure)
    clock = FakeClock()
    clock.start = datetime.now(UTC)
    directory = tmp_path / "control"
    directory.mkdir(mode=0o700)
    journal = control.ControlJournal(directory)
    http = HttpFixture(directory, missing_volume_info=failure == "missing_volume")
    provider = VastProvider(rehearsal.intent, journal=journal, transport=http)
    rehearsal.attach_provider(provider)
    intent = control.ControlIntent(
        launch_request_sha256=rehearsal.workflow.launch_request_sha256,
        execution_deadline_utc=rehearsal.intent.execution_deadline_utc,
        cleanup_deadline_utc=rehearsal.intent.cleanup_deadline_utc,
        operation_timeout_seconds=2.0,
        poll_seconds=1.0,
    )
    try:
        assert not rehearsal.root.exists()
        assert rehearsal.workflow.model is None
        assert not http.creates and not http.log_requests and rehearsal.log_fetches == 0
        assert (
            rehearsal.pins.read(instance_id=101, run_nonce=rehearsal.intent.run_nonce)
            is None
        )
        previous_umask = os.umask(0o077)
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
        finally:
            os.umask(previous_umask)
        assert outcome.instance_id == 101
        assert rehearsal.guest_stage_started
        assert rehearsal.received.count("outbox/plan.json") == (
            0 if failure == "bad_upload" else 1
        )
        assert http.log_requests == [101] and rehearsal.log_fetches == 1
        assert isinstance(rehearsal.workflow.transfer, SftpTransfer)
        assert (
            rehearsal.exchange / "downloads" / "export"
        ).stat().st_mode & 0o777 == 0o750
        assert len(http.creates) == 1
        body = json.loads(http.creates[0])
        assert body["runtype"] == "args" and body["user"] == "0:0"
        assert body["env"] == {"-p 2222:2222": "1"}
        assert body["disk"] == rehearsal.intent.disk.requested_gb
        requests = [
            json.loads(line)
            for line in (directory / "requests.jsonl").read_bytes().splitlines()
        ]
        assert [item["path"] for item in requests if item["method"] == "DELETE"] == [
            "/api/v0/instances/101/"
        ]
        assert journal.has("provider-destroy-ack-101.json")
        assert journal.exact_instance_id() == 101
        assert outcome.cleanup_status == (
            control.UNCONFIRMED if failure == "missing_volume" else control.CONFIRMED
        )
        if failure == "missing_volume":
            assert not journal.has("provider-no-volumes-101.json")
            assert not journal.has("cleanup-confirmed.json")
            assert journal.has("cleanup-unconfirmed.json")
            assert clock.elapsed <= 20
        assert all(
            not command.directory.exists() for command in rehearsal.sftp_commands
        )
        assert all(
            not command.directory.exists() for command in rehearsal.model_commands
        )
        assert all(not name.startswith("inbox/model/") for name in rehearsal.sent)
        if failure in (None, "missing_volume"):
            assert outcome.work_status == "RETRIEVED"
            result = rehearsal.workflow.result
            assert isinstance(result, boot.SftpResult)
            plan = rehearsal.workflow.plan
            assert isinstance(plan, prep.VastSftpProcessInput)
            assert plan.launch_readback.user == "0:0" and plan.uid == 2000
            verified = verify_execution_package(
                rehearsal.workflow.local / "retrieved-package",
                expected_digest=result.retained_digest,
            )
            assert len(verified.producer_receipt.terminal_outcomes) == 18
            manifest = verified.executed_manifest.model_dump(mode="json")
            assert manifest["topology"]["declaration_sha256"] == boot.record_digest(
                plan
            )
            assert rehearsal.workflow.retained_digest == result.retained_digest
            assert rehearsal.reviewed == [boot.record_bytes(plan)]
            assert rehearsal.executor.executions == 1
            assert rehearsal.executor.harness.stopped == [102, 101, 100]
            assert len(rehearsal.sftp_commands) == 10
            assert len(rehearsal.model_commands) == len(TINY_FILES)
            assert (rehearsal.workflow.local / "retrieval-verified.json").exists()
        else:
            assert outcome.work_status == "FAILED"
            assert rehearsal.workflow.retained_digest is None
            assert not (rehearsal.workflow.local / "retrieval-verified.json").exists()
        if failure in ("approval", "bad_upload"):
            assert rehearsal.executor.executions == 0
            assert not (rehearsal.root / "private" / "approval-consumed.json").exists()
        if failure == "approval":
            assert "inbox/approval.json" not in rehearsal.sent
        if failure == "bad_upload":
            assert not rehearsal.model_commands
            assert not (rehearsal.root / "inbox" / "stage.json").exists()
        if failure == "download":
            assert rehearsal.executor.executions == 1
            assert not list((rehearsal.workflow.local / "retrieved-package").iterdir())
    finally:
        journal.close()
        rehearsal.pins.close()
