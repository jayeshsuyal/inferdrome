"""Stock-command compilation and fake file IO only; never invoke SSH/network."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from inferdrome.deployment import vast_sftp as sftp
from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.deployment.vast_ssh_trust import SshPinJournal, format_announcement

NONCE = "a" * 32
URL = "https://s3.amazonaws.com/vast.ai/instance_logs/synthetic_123.log"
KEY = "ssh-ed25519 " + base64.b64encode(
    struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"k" * 32
).decode("ascii")


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


@dataclass
class Setup:
    gate: sftp.SftpGate
    pins: SshPinJournal
    pin_path: Path


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Setup]:
    identity = tmp_path / "explicit identity"
    identity.write_bytes(b"synthetic private identity, never a real credential")
    identity.chmod(0o600)
    directory = tmp_path / "pins"
    directory.mkdir(mode=0o700)
    with SshPinJournal(directory) as pins:
        pins.enroll(
            format_announcement(123, NONCE, KEY),
            instance_id=123,
            run_nonce=NONCE,
            result_url=URL,
        )
        yield Setup(
            sftp.SftpGate(123, NONCE, "93.184.215.14", 2244, identity),
            pins,
            directory / "instance-123.json",
        )


class FakeRunner:
    def __init__(
        self,
        content: bytes = b'{"untrusted":true}',
        after: Callable[[sftp.TransferCommand], None] | None = None,
    ) -> None:
        self.content, self.after = content, after
        self.calls: list[sftp.TransferCommand] = []
        self.batches: list[bytes] = []
        self.hosts: list[bytes] = []

    def __call__(self, command: sftp.TransferCommand, seconds: float) -> None:
        assert 0 < seconds <= 3600
        self.calls.append(command)
        for name in ("batch", "identity", "known_hosts"):
            assert (command.directory / name).stat().st_mode & 0o777 == 0o600
        assert command.directory.stat().st_mode & 0o777 == 0o700
        self.batches.append((command.directory / "batch").read_bytes())
        self.hosts.append((command.directory / "known_hosts").read_bytes())
        if command.direction == "receive":
            command.data_path.write_bytes(self.content)
            command.data_path.chmod(0o600)
        else:
            # Stock put copies source permissions; server umask0027 cannot add
            # group-read needed by bootstrap2000 to admit transfer2001's file.
            assert command.data_path.stat().st_mode & 0o777 == 0o640
        if self.after is not None:
            self.after(command)


@pytest.mark.parametrize("name", ["intent", "stage", "approval"])
def test_upload_uses_pending_then_staging_rename_and_local_receipt(
    setup: Setup,
    tmp_path: Path,
    name: str,
) -> None:
    local = tmp_path / 'local "odd"\nname.json'
    content = b'{"locally_checked":true}'
    local.write_bytes(content)
    runner = FakeRunner()
    spec = sftp.FileSpec(
        f"inbox/{name}.json", "send", 100, len(content), digest(content)
    )
    transfer = sftp.SftpTransfer(setup.gate, setup.pins, [spec], runner=runner)
    receipt = transfer.send(local, spec.remote_relative, 10)
    assert receipt == sftp.TransferReceipt(len(content), digest(content))
    assert runner.batches == [
        (
            f'put -f "data" "/uploads/{name}.json.pending"\n'
            f'rename "/uploads/{name}.json.pending" "/uploads/{name}.json"\n'
        ).encode()
    ]
    assert transfer.instance_id == 123 and transfer.run_nonce == NONCE
    pin = setup.pins.read(instance_id=123, run_nonce=NONCE)
    assert pin is not None
    assert transfer.host_key_sha256 == pin.announcement.host_key_sha256
    assert not runner.calls[0].directory.exists()


@pytest.mark.parametrize(
    "spec", [item for item in sftp.file_inventory() if item.direction == "receive"]
)
def test_downloads_use_only_exact_paths_and_publish_private_untrusted_bytes(
    setup: Setup,
    tmp_path: Path,
    spec: sftp.FileSpec,
) -> None:
    # Valid transport bytes need not be valid control JSON. Admission belongs
    # to the caller's strict schema/identity/evidence verifier.
    runner = FakeRunner(b"not yet validated JSON")
    local = tmp_path / "received"
    receipt = sftp.SftpTransfer(setup.gate, setup.pins, runner=runner).receive(
        spec.remote_relative,
        local,
        10,
    )
    remote = spec.remote_relative.removeprefix("outbox/")
    assert runner.batches == [f'get "/downloads/{remote}" "data"\n'.encode()]
    assert runner.calls[0].maximum_bytes == spec.maximum_bytes
    assert receipt == sftp.TransferReceipt(len(runner.content), digest(runner.content))
    assert local.read_bytes() == runner.content
    assert local.stat().st_mode & 0o777 == 0o600
    assert local.stat().st_nlink == 1
    assert not runner.calls[0].directory.exists()


def test_stock_sftp_has_private_batch_pinned_endpoint_and_no_ambient_ssh(
    setup: Setup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("SSH_AUTH_SOCK", "SSH_ASKPASS", "HTTPS_PROXY", "HF_TOKEN"):
        monkeypatch.setenv(name, "ambient secret")
    runner = FakeRunner()
    sftp.SftpTransfer(setup.gate, setup.pins, runner=runner).receive(
        "outbox/ready.json",
        tmp_path / "ready",
        3,
    )
    (command,) = runner.calls
    argv = command.argv
    assert argv[:7] == (
        "/usr/bin/sftp",
        "-q",
        "-4",
        "-F",
        "/dev/null",
        "-S",
        "/usr/bin/ssh",
    )
    assert argv[-1] == "inferdrome-transfer@93.184.215.14"
    assert argv[argv.index("-P") + 1] == "2244"
    assert argv[argv.index("-b") + 1] == str(command.directory / "batch")
    assert argv[argv.index("-i") + 1] == str(command.directory / "identity")
    assert argv[argv.index("-R") + 1] == "1"
    for option in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "GlobalKnownHostsFile=/dev/null",
        "HostKeyAlgorithms=ssh-ed25519",
        "UpdateHostKeys=no",
        "VerifyHostKeyDNS=no",
        "KnownHostsCommand=none",
        "CanonicalizeHostname=no",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "AddKeysToAgent=no",
        "ForwardAgent=no",
        "ForwardX11=no",
        "ClearAllForwardings=yes",
        "PermitLocalCommand=no",
        "SendEnv=-*",
        "ProxyCommand=none",
        "ProxyJump=none",
        "RequestTTY=no",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "ControlMaster=no",
        "ControlPath=none",
        "ControlPersist=no",
    ):
        assert option in argv
    assert runner.hosts == [f"[93.184.215.14]:2244 {KEY}\n".encode()]
    assert set(command.environment) == {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
    assert "ambient secret" not in repr(command)
    assert not {"-r", "-a", "-p", "-s", "-J", "-D"} & set(argv)


@pytest.mark.parametrize(
    "change",
    [
        {"instance_id": True},
        {"instance_id": 0},
        {"instance_id": "123"},
        {"instance_id": 9_007_199_254_740_992},
        {"port": True},
        {"port": 0},
        {"port": 65536},
        {"run_nonce": "A" * 32},
        {"run_nonce": "a" * 31},
        {"run_nonce": None},
        {"host": "127.0.0.1"},
        {"host": "10.0.0.1"},
        {"host": "169.254.169.254"},
        {"host": "100.64.0.1"},
        {"host": "203.0.113.1"},
        {"host": "224.0.0.1"},
        {"host": "240.0.0.1"},
        {"host": "0.0.0.0"},
        {"host": "::1"},
        {"host": "broker.example.invalid"},
        {"host": "93.184.215.014"},
        {"host": "93.184.215.14\n"},
        {"identity": Path("relative")},
    ],
)
def test_invalid_gate_rejected_before_any_runner(
    setup: Setup, change: dict[str, object]
) -> None:
    with pytest.raises(sftp.TransferConfigurationFailure, match="GATE_INVALID"):
        sftp.SftpTransfer(replace(setup.gate, **change), setup.pins)  # type: ignore[arg-type]


@pytest.mark.parametrize("change", [{"instance_id": 124}, {"run_nonce": "b" * 32}])
def test_pin_must_match_exact_instance_and_nonce(
    setup: Setup, change: dict[str, object]
) -> None:
    with pytest.raises(sftp.TransferConfigurationFailure, match="PIN_INVALID"):
        sftp.SftpTransfer(replace(setup.gate, **change), setup.pins)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "remote",
    [
        "../outbox/ready.json",
        "/outbox/ready.json",
        "outbox/../ready.json",
        "outbox/*.json",
        "outbox/ready.json\nput secret /uploads/stolen",
        "outbox/ready.json/",
        'outbox/ready.json"',
        "outbox/export",
        "outbox/export/archive.tar",
        "inbox/model/config.json",
        "inbox/intent.json",
        "outbox/extra.json",
    ],
)
def test_remote_paths_are_closed_and_direction_bound(
    setup: Setup, tmp_path: Path, remote: str
) -> None:
    runner = FakeRunner()
    with pytest.raises(sftp.TransferConfigurationFailure, match="PATH_FORBIDDEN"):
        sftp.SftpTransfer(setup.gate, setup.pins, runner=runner).receive(
            remote, tmp_path / "out", 10
        )
    assert not runner.calls


@pytest.mark.parametrize(
    "spec",
    [
        sftp.FileSpec("inbox/model/config.json", "send", 100),
        sftp.FileSpec("outbox/ready.json", "send", 100),
        sftp.FileSpec("outbox/ready.json", "receive", 131_073),
        sftp.FileSpec("outbox/ready.json", "receive", 0),
        sftp.FileSpec("outbox/ready.json", "receive", True),
        sftp.FileSpec("outbox/ready.json", "receive", 100, 101),
        sftp.FileSpec("outbox/ready.json", "receive", 100, True),
        sftp.FileSpec("outbox/ready.json", "receive", 100, sha256="bad"),
    ],
)
def test_inventory_can_only_tighten_existing_caps(
    setup: Setup, spec: sftp.FileSpec
) -> None:
    with pytest.raises(sftp.TransferConfigurationFailure, match="INVENTORY_INVALID"):
        sftp.SftpTransfer(setup.gate, setup.pins, [spec])


@pytest.mark.parametrize(
    "seconds", [True, 0, -1, 3601, float("inf"), float("nan"), "10"]
)
def test_invalid_budget_never_runs(
    setup: Setup, tmp_path: Path, seconds: object
) -> None:
    runner = FakeRunner()
    with pytest.raises(sftp.TransferConfigurationFailure, match="BUDGET_INVALID"):
        sftp.SftpTransfer(setup.gate, setup.pins, runner=runner).receive(
            "outbox/ready.json",
            tmp_path / "out",
            seconds,  # type: ignore[arg-type]
        )
    assert not runner.calls


@pytest.mark.parametrize(
    "kind", ["missing", "empty", "public", "oversized", "symlink", "hardlink"]
)
def test_identity_is_explicit_private_single_link_and_bounded(
    setup: Setup, tmp_path: Path, kind: str
) -> None:
    path = setup.gate.identity
    if kind == "missing":
        path.unlink()
    elif kind == "empty":
        path.write_bytes(b"")
    elif kind == "public":
        path.chmod(0o644)
    elif kind == "oversized":
        path.write_bytes(b"x" * 65_537)
    elif kind == "hardlink":
        os.link(path, tmp_path / "identity-alias")
    else:
        moved = tmp_path / "moved"
        path.rename(moved)
        path.symlink_to(moved)
    with pytest.raises(sftp.TransferConfigurationFailure, match="IDENTITY_INVALID"):
        sftp.SftpTransfer(setup.gate, setup.pins)


@pytest.mark.parametrize("change", ["pin_delete", "pin_change", "identity_change"])
@pytest.mark.parametrize("during", [False, True])
def test_changed_trust_never_publishes_a_download(
    setup: Setup,
    tmp_path: Path,
    change: str,
    during: bool,
) -> None:
    def mutate(_command: sftp.TransferCommand | None = None) -> None:
        if change == "pin_delete":
            setup.pin_path.unlink()
        elif change == "identity_change":
            setup.gate.identity.write_bytes(b"replacement private identity")
        else:
            value = json.loads(setup.pin_path.read_bytes())
            value["log_sha256"] = "sha256:" + "b" * 64
            setup.pin_path.write_text(
                json.dumps(value, sort_keys=True, separators=(",", ":"))
            )

    runner = FakeRunner(after=mutate if during else None)
    transfer = sftp.SftpTransfer(setup.gate, setup.pins, runner=runner)
    if not during:
        mutate()
    local = tmp_path / "received"
    with pytest.raises(sftp.TransferConfigurationFailure):
        transfer.receive("outbox/ready.json", local, 10)
    assert not local.exists()
    assert len(runner.calls) == int(during)
    assert all(not command.directory.exists() for command in runner.calls)


@pytest.mark.parametrize(
    "failure",
    [
        "exception",
        "missing",
        "oversized",
        "symlink",
        "hardlink",
        "extra",
        "batch_change",
        "existing_destination",
    ],
)
def test_partial_or_changed_results_never_publish(
    setup: Setup,
    tmp_path: Path,
    failure: str,
) -> None:
    local = tmp_path / "received"

    def corrupt(command: sftp.TransferCommand) -> None:
        if failure == "exception":
            raise RuntimeError("must never disclose secret credentials")
        if failure == "missing":
            command.data_path.unlink()
        elif failure == "oversized":
            command.data_path.write_bytes(b"x" * (command.maximum_bytes + 1))
        elif failure == "symlink":
            command.data_path.unlink()
            command.data_path.symlink_to(setup.gate.identity)
        elif failure == "hardlink":
            command.data_path.unlink()
            os.link(setup.gate.identity, command.data_path)
        elif failure == "extra":
            (command.directory / "unexpected").write_bytes(b"x")
        elif failure == "batch_change":
            (command.directory / "batch").write_bytes(b"changed")
        elif failure == "existing_destination":
            local.write_bytes(b"prior record")

    runner = FakeRunner(after=corrupt)
    with pytest.raises(sftp.TransferFailure) as caught:
        sftp.SftpTransfer(setup.gate, setup.pins, runner=runner).receive(
            "outbox/ready.json", local, 10
        )
    assert "secret" not in str(caught.value)
    if failure == "existing_destination":
        assert local.read_bytes() == b"prior record"
    else:
        assert not local.exists()
    assert not runner.calls[0].directory.exists()


def test_preexisting_destination_rejected_without_transfer(
    setup: Setup, tmp_path: Path
) -> None:
    local = tmp_path / "ready"
    local.write_bytes(b"keep")
    runner = FakeRunner()
    with pytest.raises(sftp.TransferConfigurationFailure, match="DESTINATION_INVALID"):
        sftp.SftpTransfer(setup.gate, setup.pins, runner=runner).receive(
            "outbox/ready.json", local, 10
        )
    assert local.read_bytes() == b"keep" and not runner.calls


def test_expected_digest_and_size_are_checked_before_upload_and_after_download(
    setup: Setup, tmp_path: Path
) -> None:
    local = tmp_path / "intent"
    local.write_bytes(b"bad")
    runner = FakeRunner(b"bad")
    specs = [
        sftp.FileSpec("inbox/intent.json", "send", 10, 3, digest(b"yes")),
        sftp.FileSpec("outbox/ready.json", "receive", 10, 3, digest(b"yes")),
    ]
    transfer = sftp.SftpTransfer(setup.gate, setup.pins, specs, runner=runner)
    with pytest.raises(sftp.TransferFailure):
        transfer.send(local, "inbox/intent.json", 10)
    assert not runner.calls
    with pytest.raises(sftp.TransferFailure):
        transfer.receive("outbox/ready.json", tmp_path / "ready", 10)
    assert len(runner.calls) == 1 and not (tmp_path / "ready").exists()


def test_local_publish_fsync_failure_rolls_back_final_name(
    setup: Setup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner()

    def failed_fsync(_directory: object) -> None:
        raise OSError("sensitive local filesystem detail")

    monkeypatch.setattr(SafeDirFD, "fsync", failed_fsync)
    local = tmp_path / "ready"
    with pytest.raises(sftp.TransferFailure, match="TRANSFER_FAILED"):
        sftp.SftpTransfer(setup.gate, setup.pins, runner=runner).receive(
            "outbox/ready.json", local, 10
        )
    assert not local.exists()
    assert not list(tmp_path.glob(".vast-transfer-*"))


def test_default_uses_existing_bounded_runner_without_invoking_it(
    setup: Setup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(sftp, "_run_bounded", runner)
    sftp.SftpTransfer(setup.gate, setup.pins).receive(
        "outbox/ready.json", tmp_path / "ready", 1
    )
    assert len(runner.calls) == 1
    assert runner.calls[0].maximum_bytes == 131_072
