"""CPU qualification orchestration under fake Docker/keygen/guest results only."""

from __future__ import annotations

import argparse
import base64
import copy
import json
import secrets
import struct
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from inferdrome.deployment.vast_ssh_trust import (
    HostKeyAnnouncement,
    format_announcement,
)
from inferdrome.qwen3_campaign import (
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
    qwen3_model_manifest,
)
from scripts import vast_cpu_guest_probe as probe
from scripts import vast_cpu_qualify as qualify
from scripts.vast_cpu_common import Deadline, Failure, Result, Runner

IMAGE = "sha256:" + "a" * 64
NONCE = "b" * 32


def key(value: bytes) -> str:
    return (
        "ssh-ed25519 "
        + base64.b64encode(
            struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + value * 32
        ).decode()
    )


HOST_KEY, CLIENT_KEY, WRONG_KEY = key(b"h"), key(b"c"), key(b"w")


class Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


class FakeDeadline:
    def __init__(self, clock: Clock, end: float, original: float | None = None):
        self.clock, self.end = clock, end
        self.original = end if original is None else original

    @staticmethod
    def utc(value: float) -> str:
        return (datetime(2030, 1, 1, tzinfo=UTC) + timedelta(seconds=value)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    @property
    def timestamp(self) -> str:
        return self.utc(self.end)

    @property
    def original_timestamp(self) -> str:
        return self.utc(self.original)

    def remaining(self) -> float:
        result = self.end - self.clock.seconds
        if result <= 0:
            raise Failure("ORIGINAL_DEADLINE_EXPIRED")
        return result

    def child(self, seconds: float) -> FakeDeadline:
        self.remaining()
        if seconds <= 0:
            raise Failure("INVALID_PHASE_BUDGET")
        return FakeDeadline(
            self.clock, min(self.end, self.clock.seconds + seconds), self.original
        )


def identity_receipt(uid: int) -> dict[str, Any]:
    return {
        "uids": [uid] * 3,
        "gids": [0] * 3,
        "groups": [],
        "no_new_privs": 1,
        "capabilities": dict.fromkeys(
            ("CapInh", "CapPrm", "CapEff", "CapAmb"), "0000000000000000"
        ),
    }


def model_result() -> dict[str, Any]:
    files = qwen3_model_manifest()["files"]
    total = sum(item["size_bytes"] for item in files)
    return {
        "schema_version": "inferdrome.vast-cpu-guest-probe.v1",
        "mode": "model",
        "status": "PASS",
        "checks": dict.fromkeys(qualify.MODEL_CHECKS, True),
        "frozen_revision": QWEN3_8B_REVISION,
        "snapshot_sha256": qwen3_expected_snapshot_sha256(),
        "inventory_file_count": len(files),
        "inventory_total_bytes": total,
        "experiment_identity": identity_receipt(2000),
        "stage_files": [
            {**item, "uid": 2000, "gid": 0, "mode": "0600", "nlink": 1}
            for item in files
        ],
        "snapshot_files": [
            {**item, "uid": 2000, "gid": 0, "mode": "0600", "nlink": 1}
            for item in files
        ],
        "disk": {
            "required_free_bytes": 2 * total
            + max(item["size_bytes"] for item in files)
            + 1073741824,
            "free_before_bytes": 100_000_000_000,
            "free_after_bytes": 100_000_000_000 - 2 * total,
            "minimum_free_observed_bytes": 100_000_000_000 - 2 * total,
            "peak_observed_consumption_bytes": 2 * total,
            "peak_observed_tree_bytes": 2 * total,
        },
    }


class FakeRunner:
    def __init__(self, clock: Clock, failure: str | None = None):
        self.clock, self.failure = clock, failure
        self.calls: list[tuple[tuple[str, ...], float, float]] = []
        self.registered: list[str] = []
        self.forgotten: list[str] = []
        self.containers: dict[str, dict[str, Any]] = {}
        self.create_count = 0
        self.log_count = 0
        self.management: dict[str, Any] = {
            "schema_version": "inferdrome.vast-cpu-guest-probe.v1",
            "mode": "management",
            "status": "PASS",
            "checks": dict.fromkeys(qualify.MANAGEMENT_CHECKS, True),
            "run_nonce": NONCE,
            "host_key_sha256": HostKeyAnnouncement(
                101, NONCE, HOST_KEY
            ).host_key_sha256,
            "experiment_identity": identity_receipt(2000),
            "transfer_identity": identity_receipt(2001),
            "root_filesystem_allocated_bytes": 30_000_000_000,
        }
        self.model = model_result()
        self.image: dict[str, Any] = {
            "Id": IMAGE,
            "Os": "linux",
            "Architecture": "amd64",
            "Config": {
                "User": "0:0",
                "Entrypoint": qualify._ENTRYPOINT,
                "Cmd": None,
                "ExposedPorts": None,
                "Volumes": None,
                "Healthcheck": None,
            },
        }

    def record_container(self, cid: str) -> None:
        assert cid in self.containers
        self.registered.append(cid)
        if self.failure == "registration":
            raise Failure("synthetic registration failure")

    def forget_container(self, cid: str) -> None:
        assert cid not in self.containers
        self.forgotten.append(cid)

    @staticmethod
    def result(value: Any) -> Result:
        return Result(0, json.dumps(value).encode(), b"")

    def run(
        self,
        argv: tuple[str, ...],
        *,
        deadline: Deadline,
        input: bytes | None = None,
        limit: int = 1048576,
        check: bool = True,
        env: Any = None,
    ) -> Result:
        del input, env
        bound = cast(FakeDeadline, deadline)
        assert bound.remaining() > 0 and 0 < limit <= 1048576
        self.calls.append((argv, self.clock.seconds, bound.end))
        if argv[0] == "/usr/bin/ssh-keygen":
            target = Path(argv[argv.index("-f") + 1])
            assert argv[1:9] == ("-q", "-t", "ed25519", "-N", "", "-C", "", "-f")
            target.write_bytes(b"synthetic private bytes; never usable by SSH")
            target.chmod(0o600)
            public = WRONG_KEY if target.name == "wrong_identity" else CLIENT_KEY
            target.with_suffix(".pub").write_text(public + "\n")
            return Result(0, b"", b"")
        assert argv[0] == "docker"
        if argv[1:3] == ("image", "inspect"):
            assert argv[3] == IMAGE
            return self.result([self.image])
        if argv[1] == "create":
            assert "--pull=never" in argv and "--runtime=runc" in argv
            assert not {"--gpus", "--privileged", "-p", "--publish", "--init"} & set(
                argv
            )
            self.create_count += 1
            cid = str(self.create_count) * 64
            cidfile = Path(argv[argv.index("--cidfile") + 1])
            cidfile.write_text(cid + "\n")
            cidfile.chmod(0o600)
            network = argv[argv.index("--network") + 1]
            self.containers[cid] = {
                "Id": cid,
                "Image": IMAGE,
                "Mounts": [],
                "HostConfig": {
                    "NetworkMode": network,
                    "Runtime": "runc",
                    "Privileged": False,
                    "DeviceRequests": [],
                    "Devices": [],
                    "PortBindings": {},
                    "PidMode": "",
                    "Binds": [],
                },
                "State": {"Running": False, "ExitCode": 0},
            }
            if self.failure == "create_after_cidfile":
                raise Failure("synthetic private error must not escape")
            return Result(0, (cid + "\n").encode(), b"")
        if argv[1:3] == ("container", "inspect"):
            cid = argv[3]
            if cid in self.containers:
                return self.result([self.containers[cid]])
            assert check is False
            if self.failure == "ambiguous_absence":
                return Result(1, b"", b"daemon unavailable; not absence")
            return Result(1, b"[]\n", ("Error: No such object: " + cid + "\n").encode())
        if argv[1] == "start":
            cid = argv[-1]
            assert cid in self.registered
            if "--attach" in argv:
                assert self.containers[cid]["HostConfig"]["NetworkMode"] == "bridge"
                if self.failure == "model_command":
                    raise Failure("synthetic model diagnostic with secret canary")
                self.clock.sleep(30)
                return self.result(self.model)
            self.containers[cid]["State"]["Running"] = True
            return Result(0, (cid + "\n").encode(), b"")
        if argv[1] == "logs":
            self.log_count += 1
            if self.failure == "missing_marker" or self.log_count == 1:
                return Result(0, b"ordinary synthetic startup\n", b"")
            nonce = "c" * 32 if self.failure == "stale_pin" else NONCE
            return Result(0, format_announcement(101, nonce, HOST_KEY), b"")
        if argv[1] == "exec":
            assert argv[2] in self.registered
            if "management" in argv:
                self.clock.sleep(10)
                return self.result(self.management)
            assert argv[3] in ("/usr/bin/install", "/bin/chmod")
            return Result(0, b"", b"")
        if argv[1] == "cp":
            assert argv[-1].split(":", 1)[0] in self.registered
            return Result(0, b"", b"")
        if argv[1:3] == ("rm", "--force"):
            cid = argv[3]
            assert cid in self.registered and check is False
            self.containers.pop(cid, None)
            return Result(0, (cid + "\n").encode(), b"")
        raise AssertionError("unimplemented fake command; never execute it")


Harness = tuple[FakeRunner, Clock, Callable[[], dict[str, Any]], Path]


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    clock = Clock()
    runner = FakeRunner(clock)
    monkeypatch.setattr(secrets, "token_hex", lambda count: NONCE)
    monkeypatch.setattr(time, "sleep", clock.sleep)

    def run() -> dict[str, Any]:
        return qualify.qualify(
            IMAGE,
            cast(Runner, runner),
            tmp_path / "qualification",
            cast(Deadline, FakeDeadline(clock, 5000)),
            cast(Deadline, FakeDeadline(clock, 5900)),
        )

    return runner, clock, run, tmp_path / "qualification"


def test_success_qualifies_two_bound_containers_and_removes_private_keys(
    harness: Harness,
) -> None:
    runner, _clock, run, root = harness
    report = run()
    assert report["status"] == "PASS" and report["cleanup"] == "CONFIRMED"
    assert report["gpu"] == report["campaign"] == "NOT_RUN"
    assert runner.registered == runner.forgotten == ["1" * 64, "2" * 64]
    assert not runner.containers
    assert not any(
        (root / name).exists()
        for name in (
            "identity",
            "identity.pub",
            "wrong_identity",
            "wrong_identity.pub",
            "known_hosts",
        )
    )
    assert runner.log_count == 2
    commands = [item[0] for item in runner.calls]
    first_remove = commands.index(("docker", "rm", "--force", "1" * 64))
    second_create = [
        index for index, argv in enumerate(commands) if argv[:2] == ("docker", "create")
    ][1]
    assert first_remove < second_create
    assert all(
        argv[1] not in {"pull", "build", "buildx", "push", "run", "prune"}
        for argv in commands
        if argv[0] == "docker"
    )
    assert all(bound <= 5900 for _, _, bound in runner.calls)
    models = [
        (argv, start, end)
        for argv, start, end in runner.calls
        if argv[:3] == ("docker", "start", "--attach")
    ]
    assert len(models) == 1 and models[0][2] - models[0][1] <= 2100
    assert report["execution_deadline"] == FakeDeadline.utc(5000)
    assert report["model_staging"]["stage_files"] == runner.model["stage_files"]
    assert report["model_staging"]["snapshot_files"] == runner.model["snapshot_files"]
    assert report["ssh_management"]["root_filesystem_allocated_bytes"] == 30_000_000_000
    assert report["ssh_management"]["experiment_identity"] == identity_receipt(2000)
    assert report["ssh_management"]["transfer_identity"] == identity_receipt(2001)
    assert json.loads((root / "qualification.json").read_text()) == report


@pytest.mark.parametrize(
    "failure",
    [
        "create_after_cidfile",
        "registration",
        "stale_pin",
        "missing_marker",
        "model_command",
    ],
)
def test_partial_failure_retains_exact_id_cleans_and_sanitizes(
    harness: Harness, failure: str
) -> None:
    runner, _clock, run, root = harness
    runner.failure = failure
    with pytest.raises(Failure, match=r"^CPU_QUALIFICATION_FAILED$"):
        run()
    assert runner.registered and not runner.containers
    assert set(runner.forgotten) == set(runner.registered)
    report = json.loads((root / "qualification.json").read_text())
    assert report["status"] == "FAILED" and report["cleanup"] == "CONFIRMED"
    assert "secret" not in json.dumps(report) and "diagnostic" not in json.dumps(report)
    if failure != "model_command":
        assert runner.create_count == 1


def test_ambiguous_cleanup_is_not_absence_and_prevents_model_or_pass(
    harness: Harness,
) -> None:
    runner, _clock, run, root = harness
    runner.failure = "ambiguous_absence"
    with pytest.raises(Failure):
        run()
    assert runner.create_count == 1 and not runner.forgotten
    report = json.loads((root / "qualification.json").read_text())
    assert report["status"] == "FAILED" and report["cleanup"] == "UNCONFIRMED"


@pytest.mark.parametrize(
    "change",
    ["false_negative", "missing_negative", "extra", "wrong_key", "wrong_nonce"],
)
def test_guest_success_label_never_overrides_incomplete_or_wrong_evidence(
    harness: Harness, change: str
) -> None:
    runner, _clock, run, root = harness
    if change == "false_negative":
        runner.management["checks"]["wrong_client_key"] = False
    elif change == "missing_negative":
        del runner.management["checks"]["shell_exec"]
    elif change == "extra":
        runner.management["checks"]["unknown"] = True
    elif change == "wrong_key":
        runner.management["host_key_sha256"] = "sha256:" + "0" * 64
    else:
        runner.management["run_nonce"] = "d" * 32
    with pytest.raises(Failure):
        run()
    assert runner.create_count == 1 and not runner.containers
    assert json.loads((root / "qualification.json").read_text())["status"] == "FAILED"


@pytest.mark.parametrize(
    "field", ["frozen_revision", "snapshot_sha256", "inventory_total_bytes", "disk"]
)
def test_model_requires_exact_frozen_identity_and_disk_evidence(
    harness: Harness, field: str
) -> None:
    runner, _clock, run, _root = harness
    runner.model[field] = None
    with pytest.raises(Failure):
        run()
    assert runner.create_count == 2 and not runner.containers


@pytest.mark.parametrize("inventory", ["stage_files", "snapshot_files"])
@pytest.mark.parametrize(
    "change", ["missing", "hash", "size", "owner", "link", "mode", "bool"]
)
def test_every_model_file_receipt_is_required_and_exact(
    harness: Harness, inventory: str, change: str
) -> None:
    runner, _clock, run, _root = harness
    records = runner.model[inventory]
    if change == "missing":
        records.pop()
    elif change == "hash":
        records[0]["sha256"] = "sha256:" + "0" * 64
    elif change == "size":
        records[0]["size_bytes"] += 1
    elif change == "owner":
        records[0]["uid"] = 0
    elif change == "link":
        records[0]["nlink"] = 2
    elif change == "mode":
        records[0]["mode"] = "0640"
    else:
        records[0]["nlink"] = True
    with pytest.raises(Failure):
        run()
    assert runner.create_count == 2 and not runner.containers


@pytest.mark.parametrize(
    "field",
    ["experiment_identity", "transfer_identity", "root_filesystem_allocated_bytes"],
)
def test_management_requires_kernel_identity_and_allocated_disk_receipts(
    harness: Harness, field: str
) -> None:
    runner, _clock, run, _root = harness
    runner.management[field] = None
    with pytest.raises(Failure):
        run()
    assert runner.create_count == 1 and not runner.containers


def test_capabilities_or_new_privileges_prevent_success(harness: Harness) -> None:
    runner, _clock, run, _root = harness
    runner.model["experiment_identity"]["capabilities"]["CapEff"] = "0000000000000001"
    with pytest.raises(Failure):
        run()
    assert not runner.containers


def test_inconsistent_peak_disk_receipt_is_not_accepted(harness: Harness) -> None:
    runner, _clock, run, _root = harness
    runner.model["disk"]["peak_observed_consumption_bytes"] = 0
    with pytest.raises(Failure):
        run()
    assert not runner.containers


@pytest.mark.parametrize(
    "field,value",
    [
        ("User", "2000:0"),
        ("ExposedPorts", {"8000/tcp": {}}),
        ("Entrypoint", ["/bin/sh"]),
        ("Volumes", {"/workspace": {}}),
    ],
)
def test_invalid_image_metadata_fails_before_keys_or_container(
    harness: Harness, field: str, value: Any
) -> None:
    runner, _clock, run, _root = harness
    runner.image["Config"][field] = copy.deepcopy(value)
    with pytest.raises(Failure):
        run()
    assert runner.create_count == 0
    assert len(runner.calls) == 1


def test_phase_deadlines_never_extend_original_execution(harness: Harness) -> None:
    runner, clock, _run, root = harness
    clock.seconds = 4200
    report = qualify.qualify(
        IMAGE,
        cast(Runner, runner),
        root,
        cast(Deadline, FakeDeadline(clock, 5000)),
        cast(Deadline, FakeDeadline(clock, 5900)),
    )
    assert report["status"] == "PASS"
    for argv, _started, end in runner.calls:
        if argv[1:3] not in (("rm", "--force"), ("container", "inspect")):
            assert end <= 5000
    assert report["model_staging"]["phase_deadline"] == FakeDeadline.utc(5000)


REFUSALS = (
    (
        probe._require_auth_refusal,
        probe.Captured(
            255, b"", b"inferdrome-transfer@127.0.0.1: Permission denied (publickey).\n"
        ),
    ),
    (
        probe._require_host_key_refusal,
        probe.Captured(
            255,
            b"",
            b"REMOTE HOST IDENTIFICATION HAS CHANGED!\nHost key verification failed.\n",
        ),
    ),
    (
        probe._require_shell_refusal,
        probe.Captured(1, b"This service allows sftp connections only.\n", b""),
    ),
    (
        probe._require_pty_refusal,
        probe.Captured(255, b"", b"PTY allocation request failed on channel 0\n"),
    ),
    (
        probe._require_direct_forward_refusal,
        probe.Captured(
            255,
            b"",
            b"channel 0: administratively prohibited\nstdio forwarding failed\n",
        ),
    ),
    (
        probe._require_remote_forward_refusal,
        probe.Captured(255, b"", b"remote port forwarding failed for listen port 0\n"),
    ),
)


@pytest.mark.parametrize("check,expected", REFUSALS)
def test_only_completed_specific_ssh_refusal_is_accepted(
    check: Callable[[probe.Captured], None], expected: probe.Captured
) -> None:
    check(expected)
    for ambiguous in (
        probe.Captured(0, expected.stdout, expected.stderr),
        probe.Captured(124, b"", b"timeout"),
        probe.Captured(255, b"", b"Connection refused"),
        probe.Captured(255, b"", b"Connection reset by peer"),
        probe.Captured(-9, expected.stdout, expected.stderr),
    ):
        with pytest.raises(probe.ProbeFailure):
            check(ambiguous)


def test_sftp_failure_does_not_confuse_authentication_or_timeout_with_dac() -> None:
    probe._require_sftp_refusal(
        probe.Captured(1, b"", b'dest open "/downloads/blocked": Permission denied\n'),
        b"Permission denied",
    )
    probe._require_sftp_refusal(
        probe.Captured(1, b"", b'File "/../../etc/passwd" not found.\n'), b"not found"
    )
    for result in (
        probe.Captured(255, b"", b"Permission denied (publickey)."),
        probe.Captured(1, b"", b"Permission denied (publickey)."),
        probe.Captured(124, b"", b"Permission denied"),
        probe.Captured(1, b"", b"Connection closed"),
    ):
        with pytest.raises(probe.ProbeFailure):
            probe._require_sftp_refusal(result, b"Permission denied")
    with pytest.raises(probe.ProbeFailure):
        probe._require_sftp_refusal(
            probe.Captured(1, b"", b"command not found\n"), b"not found"
        )


def test_stock_client_has_explicit_pin_identity_and_no_ambient_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    budget = FakeDeadline(clock, 10)
    commands: list[tuple[str, ...]] = []

    def captured(argv: tuple[str, ...], phase: Any) -> probe.Captured:
        assert phase is budget
        commands.append(argv)
        if argv[0] == "/usr/bin/sftp":
            batch = Path(argv[argv.index("-b") + 1])
            assert batch.stat().st_mode & 0o777 == 0o600
            assert batch.read_text() == "get /downloads/ready.json /tmp/owned-result\n"
        return probe.Captured(0, b"", b"")

    monkeypatch.setattr(probe, "_capture", captured)
    probe._ssh(tmp_path, cast(Any, budget))
    probe._sftp(
        tmp_path, "get /downloads/ready.json /tmp/owned-result\n", cast(Any, budget)
    )
    for argv in commands:
        assert argv[1:4] == ("-F", "/dev/null", "-4")
        assert argv[argv.index("-i") + 1] == str(tmp_path / "identity")
        assert {
            "StrictHostKeyChecking=yes",
            "IdentityAgent=none",
            "ProxyCommand=none",
            "ProxyJump=none",
            "GlobalKnownHostsFile=/dev/null",
            "UserKnownHostsFile=" + str(tmp_path / "known_hosts"),
            "ControlPath=none",
            "UpdateHostKeys=no",
        } <= set(argv)
        assert "inferdrome-transfer@127.0.0.1" in argv
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize(
    "failure", [None, "stage_expired", "stage_partial", "snapshot_expired"]
)
def test_guest_model_steps_and_hashing_share_original_budget_and_never_adopt_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    clock = Clock()
    root = tmp_path / "model"
    root.mkdir(mode=0o700)
    bound = FakeDeadline(clock, 80)
    phases: list[FakeDeadline] = []
    operations: list[str] = []

    def budget(_deadline: str) -> FakeDeadline:
        phases.append(bound.child(80))
        return phases[-1]

    def stage(destination: Path, *, seconds: float) -> None:
        assert seconds == 80 and not tuple(destination.iterdir())
        operations.append("stage")
        (destination / "tiny.bin").write_bytes(b"synthetic")
        if failure == "stage_partial":
            raise RuntimeError("fake downloader failed; no downloader was invoked")
        clock.sleep(90 if failure == "stage_expired" else 30)

    def snapshot(source: Path, destination: Path, phase: FakeDeadline) -> None:
        phase.remaining()
        assert phase.end == 80 and phase.remaining() == 50
        operations.append("snapshot")
        (destination / "tiny.bin").write_bytes((source / "tiny.bin").read_bytes())
        clock.sleep(60 if failure == "snapshot_expired" else 20)

    def inventory(directory: Path, phase: FakeDeadline) -> list[dict[str, Any]]:
        assert phase.end == 80
        phase.remaining()
        operations.append("verify-" + directory.name)
        clock.sleep(5)
        return [{"path": "tiny.bin", "size_bytes": 9}]

    class Sampler:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def join(self, timeout: int) -> None:
            assert timeout == 3

        def is_alive(self) -> bool:
            return False

    def bounded(call: Callable[[], Any], seconds: float) -> Any:
        assert seconds == 80
        return call()

    # The temporary directory stands in for the container's fixed workspace;
    # OS identity, network/downloader, threading and alarm effects are all fake.
    monkeypatch.setattr(probe, "Budget", budget)
    monkeypatch.setattr(probe, "_identity", identity_receipt)
    monkeypatch.setattr(probe, "_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(probe, "re", SimpleNamespace(fullmatch=lambda *_args: True))
    monkeypatch.setattr(probe, "stage_pinned_model", stage)
    monkeypatch.setattr(probe, "stage_snapshot", snapshot)
    monkeypatch.setattr(probe, "_verify_inventory", inventory)
    monkeypatch.setattr(probe, "required_free_bytes", lambda: 1000)
    monkeypatch.setattr(
        probe,
        "shutil",
        SimpleNamespace(disk_usage=lambda _path: SimpleNamespace(free=10000)),
    )
    monkeypatch.setattr(
        probe, "threading", SimpleNamespace(Thread=Sampler, Event=threading.Event)
    )
    monkeypatch.setattr(probe, "_bounded_call", bounded)
    args = argparse.Namespace(root=str(root), deadline=FakeDeadline.utc(80), seconds=80)
    if failure:
        with pytest.raises((Failure, RuntimeError)):
            probe._model_child(args)
        assert not (root / "report.json").exists()
        if failure.startswith("stage_"):
            assert operations == ["stage"]
        return
    report = probe._model_child(args)
    assert report["status"] == "PASS" and report["gpu"] == "NOT_RUN"
    assert operations == ["stage", "snapshot", "verify-stage", "verify-snapshot"]
    assert len(phases) == 1 and clock.seconds == 60
    assert report["disk"]["peak_observed_tree_bytes"] > 0
    assert (root / "report.json").exists()
