"""Synthetic broker tests; no SSH, credentials, provider or model download."""

from __future__ import annotations

import base64
import hashlib
import os
import shlex
import signal
import struct
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from inferdrome.deployment import vast_transfer as transfer
from inferdrome.qwen3_campaign import qwen3_model_manifest


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


@pytest.fixture
def gate(tmp_path: Path) -> transfer.BrokerGate:
    key = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"k" * 32
    hosts = (
        b"[broker.example.invalid]:2244 ssh-ed25519 " + base64.b64encode(key) + b"\n"
    )
    identity = tmp_path / "identity"
    identity.write_bytes(b"synthetic private identity, never a real key")
    identity.chmod(0o600)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_bytes(hosts)
    known_hosts.chmod(0o600)
    return transfer.BrokerGate(
        instance_id=123,
        host="broker.example.invalid",
        port=2244,
        identity=identity,
        known_hosts=known_hosts,
        known_hosts_sha256=digest(hosts),
        host_key_sha256=digest(key),
        broker_contract_verified=True,
        broker_contract_evidence_sha256=digest(b"synthetic contract evidence"),
        broker_contract_source="synthetic independently retained broker contract",
        trusted_key_source="OPERATOR_OUT_OF_BAND",
        trusted_key_evidence_sha256=digest(b"synthetic separate key evidence"),
        remote_uid=2000,
        remote_gid=0,
    )


class FakeRunner:
    def __init__(self, content: bytes = b'{"untrusted":true}\n') -> None:
        self.content = content
        self.calls: list[transfer.TransferCommand] = []

    def __call__(self, command: transfer.TransferCommand, seconds: float) -> None:
        assert 0 < seconds <= 3600
        self.calls.append(command)
        if command.direction == "receive":
            command.data_path.write_bytes(self.content)
            command.data_path.chmod(0o600)


def test_unknown_contract_is_a_construction_gate(gate: transfer.BrokerGate) -> None:
    for change in (
        {"broker_contract_verified": False},
        {"broker_contract_source": None},
        {"broker_contract_evidence_sha256": None},
        {"trusted_key_source": None},
        {"trusted_key_source": "SSH_KEYSCAN"},
        {"trusted_key_evidence_sha256": None},
        {"remote_uid": 0},
        {"remote_gid": None},
        {"trusted_key_evidence_sha256": gate.broker_contract_evidence_sha256},
    ):
        with pytest.raises(transfer.TransferFailure, match="UNVERIFIED_BROKER_GATE"):
            transfer.BrokerTransfer(replace(gate, **change))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change",
    [
        {"instance_id": 0},
        {"instance_id": True},
        {"instance_id": "123"},
        {"port": 0},
        {"port": 65536},
        {"port": True},
        {"host": "-evil"},
        {"host": "a;command"},
        {"host": "a\nb"},
        {"host": "user@host"},
    ],
)
def test_endpoint_and_numeric_id_are_closed(
    gate: transfer.BrokerGate,
    change: dict[str, object],
) -> None:
    with pytest.raises(transfer.TransferFailure, match="BROKER_INVALID"):
        transfer.BrokerTransfer(replace(gate, **change))  # type: ignore[arg-type]


def test_receive_uses_pinned_isolated_ssh_and_exact_instance_path(
    gate: transfer.BrokerGate,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/ambient/agent")
    monkeypatch.setenv("HTTPS_PROXY", "ambient-secret")
    runner = FakeRunner()
    local = tmp_path / "ready.json"
    receipt = transfer.BrokerTransfer(gate, runner=runner).receive(
        "outbox/ready.json",
        local,
        10,
    )
    assert receipt == transfer.TransferReceipt(
        len(runner.content), digest(runner.content)
    )
    assert local.read_bytes() == runner.content
    assert local.stat().st_mode & 0o777 == 0o600
    (command,) = runner.calls
    assert command.argv[-2] == (
        "vastai_kaalia@broker.example.invalid::123//workspace/vast-bootstrap/"
        "outbox/ready.json"
    )
    assert command.argv[0] == "/usr/bin/rsync"
    assert not {"-a", "-r", "--recursive", "--delete"} & set(command.argv)
    ssh = shlex.split(command.argv[command.argv.index("-e") + 1])
    assert ssh[:3] == ["/usr/bin/ssh", "-F", "/dev/null"]
    for required in (
        "StrictHostKeyChecking=yes",
        "GlobalKnownHostsFile=/dev/null",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "ForwardAgent=no",
        "ProxyCommand=none",
        "ProxyJump=none",
        "PermitLocalCommand=no",
        "SendEnv=-*",
        "ControlPath=none",
        "BatchMode=yes",
        "UpdateHostKeys=no",
        "KnownHostsCommand=none",
        "VerifyHostKeyDNS=no",
    ):
        assert required in ssh
    assert ssh[ssh.index("-i") + 1] == str(command.directory / "identity")
    assert set(command.environment) == {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
    assert not command.directory.exists()


def test_send_validates_exact_control_digest_before_runner(
    gate: transfer.BrokerGate,
    tmp_path: Path,
) -> None:
    content = b'{"nonce":"synthetic"}'
    local = tmp_path / "intent.json"
    local.write_bytes(content)
    inventory = [
        transfer.FileSpec(
            "inbox/intent.json", "send", 100, len(content), digest(content)
        )
    ]
    runner = FakeRunner()
    broker = transfer.BrokerTransfer(gate, inventory, runner=runner)
    assert broker.send(local, "inbox/intent.json", 10).sha256 == digest(content)
    local.write_bytes(b"x" * len(content))
    with pytest.raises(transfer.TransferFailure, match="HASH_MISMATCH"):
        broker.send(local, "inbox/intent.json", 10)
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "remote",
    [
        "../outbox/ready.json",
        "/outbox/ready.json",
        "outbox/../ready.json",
        "outbox/*.json",
        "outbox/ready.json\n",
        "outbox/export/archive.tar",
        "inbox/model/subdir/config.json",
        "outbox/export",
        "outbox/ready.json/",
    ],
)
def test_unlisted_remote_paths_never_call_runner(
    gate: transfer.BrokerGate,
    tmp_path: Path,
    remote: str,
) -> None:
    runner = FakeRunner()
    with pytest.raises(transfer.TransferFailure, match="PATH_FORBIDDEN"):
        transfer.BrokerTransfer(gate, runner=runner).receive(
            remote, tmp_path / "out", 10
        )
    assert not runner.calls


def test_direction_is_bound(gate: transfer.BrokerGate, tmp_path: Path) -> None:
    runner = FakeRunner()
    with pytest.raises(transfer.TransferFailure, match="PATH_FORBIDDEN"):
        transfer.BrokerTransfer(gate, runner=runner).send(
            tmp_path / "out", "outbox/ready.json", 10
        )
    assert not runner.calls


def test_model_inventory_matches_all_frozen_files_and_cannot_be_weakened(
    gate: transfer.BrokerGate,
) -> None:
    model = [
        item
        for item in transfer.file_inventory()
        if item.remote_relative.startswith("inbox/model/")
    ]
    expected = qwen3_model_manifest()["files"]
    assert len(model) == len(expected)
    for spec, item in zip(model, expected, strict=True):
        assert spec == transfer.FileSpec(
            "inbox/model/" + item["path"],
            "send",
            item["size_bytes"],
            item["size_bytes"],
            item["sha256"],
        )
    with pytest.raises(transfer.TransferFailure, match="INVENTORY_INVALID"):
        transfer.BrokerTransfer(gate, [replace(model[0], sha256=None)])
    with pytest.raises(transfer.TransferFailure, match="INVENTORY_INVALID"):
        transfer.BrokerTransfer(
            gate, [transfer.FileSpec("outbox/unknown", "receive", 10)]
        )


@pytest.mark.parametrize("kind", ["oversized", "symlink", "hardlink", "fifo"])
def test_unsafe_source_rejected_before_network(
    gate: transfer.BrokerGate,
    tmp_path: Path,
    kind: str,
) -> None:
    source = tmp_path / "source"
    other = tmp_path / "other"
    other.write_bytes(b"a")
    if kind == "oversized":
        source.write_bytes(b"x" * 131_073)
    elif kind == "symlink":
        source.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, source)
    else:
        os.mkfifo(source)
    runner = FakeRunner()
    with pytest.raises(transfer.TransferFailure):
        transfer.BrokerTransfer(gate, runner=runner).send(
            source, "inbox/intent.json", 10
        )
    assert not runner.calls


def test_linked_parent_and_public_identity_are_rejected(
    gate: transfer.BrokerGate,
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    gate.identity.chmod(0o644)
    with pytest.raises(transfer.TransferFailure):
        transfer.BrokerTransfer(gate, runner=runner).receive(
            "outbox/ready.json", tmp_path / "out", 10
        )
    gate.identity.chmod(0o600)
    link = tmp_path / "linked"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(transfer.TransferFailure):
        transfer.BrokerTransfer(
            replace(gate, identity=link / "identity"), runner=runner
        ).receive(
            "outbox/ready.json",
            tmp_path / "out",
            10,
        )
    assert not runner.calls


@pytest.mark.parametrize("change", ["extra", "wrong-host", "wrong-key", "wildcard"])
def test_known_hosts_has_one_exact_independently_pinned_key(
    gate: transfer.BrokerGate,
    tmp_path: Path,
    change: str,
) -> None:
    content = gate.known_hosts.read_bytes()
    if change == "extra":
        content *= 2
    elif change == "wrong-host":
        content = content.replace(b"broker.example.invalid", b"other.example.invalid")
    elif change == "wildcard":
        content = content.replace(b"[broker.example.invalid]:2244", b"*")
    else:
        content = content[:-4] + b"AAAA"
    gate.known_hosts.write_bytes(content)
    runner = FakeRunner()
    with pytest.raises(transfer.TransferFailure, match="HOST_KEY_MISMATCH"):
        transfer.BrokerTransfer(
            replace(gate, known_hosts_sha256=digest(content)), runner=runner
        ).receive(
            "outbox/ready.json",
            tmp_path / "out",
            10,
        )
    assert not runner.calls


@pytest.mark.parametrize(
    "kind", ["missing", "extra", "oversized", "symlink", "hardlink", "failure"]
)
def test_failed_or_malformed_receive_never_publishes(
    gate: transfer.BrokerGate,
    tmp_path: Path,
    kind: str,
) -> None:
    directories: list[Path] = []

    def runner(command: transfer.TransferCommand, seconds: float) -> None:
        del seconds
        directories.append(command.directory)
        if kind == "missing":
            return  # rsync --max-size can report success while skipping a file.
        if kind == "failure":
            raise transfer.TransferFailure("VAST_TRANSFER_PROCESS_FAILED")
        if kind == "symlink":
            command.data_path.symlink_to(gate.identity)
        elif kind == "hardlink":
            os.link(command.directory / "identity", command.data_path)
        else:
            command.data_path.write_bytes(
                b"a" * (131_073 if kind == "oversized" else 1)
            )
            if kind == "extra":
                (command.directory / "unexpected").write_bytes(b"x")

    destination = tmp_path / "out"
    with pytest.raises(transfer.TransferFailure):
        transfer.BrokerTransfer(gate, runner=runner).receive(
            "outbox/ready.json", destination, 10
        )
    assert not destination.exists()
    assert all(not directory.exists() for directory in directories)


def test_receive_never_overwrites_existing_destination(
    gate: transfer.BrokerGate,
    tmp_path: Path,
) -> None:
    local = tmp_path / "ready.json"
    local.write_bytes(b"existing")
    with pytest.raises(transfer.TransferFailure):
        transfer.BrokerTransfer(gate, runner=FakeRunner()).receive(
            "outbox/ready.json", local, 10
        )
    assert local.read_bytes() == b"existing"


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan"), True, 3601])
def test_invalid_budgets_never_call_runner(
    gate: transfer.BrokerGate,
    tmp_path: Path,
    seconds: float,
) -> None:
    runner = FakeRunner()
    with pytest.raises(transfer.TransferFailure, match="BUDGET_INVALID"):
        transfer.BrokerTransfer(gate, runner=runner).receive(
            "outbox/ready.json", tmp_path / "out", seconds
        )
    assert not runner.calls


def local_command(
    tmp_path: Path, code: str, maximum: int = 100
) -> transfer.TransferCommand:
    return transfer.TransferCommand(
        (sys.executable, "-I", "-c", code),
        {"PATH": "/usr/bin:/bin"},
        tmp_path,
        tmp_path / "data",
        maximum,
        "receive",
    )


def test_real_local_runner_enforces_file_growth_and_nonzero_exit(
    tmp_path: Path,
) -> None:
    command = local_command(tmp_path, "open('data','wb').write(b'x'*10000)")
    with pytest.raises(transfer.TransferFailure, match="PROCESS_FAILED"):
        transfer._run_bounded(command, 5)
    assert (tmp_path / "data").stat().st_size <= 100


def test_real_local_runner_caps_output_without_exposing_it(tmp_path: Path) -> None:
    command = local_command(tmp_path, "import os; os.write(1,b'PRIVATE'*2000)")
    with pytest.raises(transfer.TransferFailure, match="OUTPUT_LIMIT") as error:
        transfer._run_bounded(command, 5)
    assert "PRIVATE" not in str(error.value)


def test_real_local_runner_deadline_reaps_sigterm_ignoring_child(
    tmp_path: Path,
) -> None:
    command = local_command(
        tmp_path,
        (
            "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "open('data','w').write(str(os.getpid())); time.sleep(60)"
        ),
    )
    start = time.monotonic()
    with pytest.raises(transfer.TransferFailure, match="DEADLINE"):
        transfer._run_bounded(command, 0.2)
    assert time.monotonic() - start < 4
    pid = int((tmp_path / "data").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, signal.SIGCONT)


def test_configuration_failures_are_not_readiness_failures(
    gate: transfer.BrokerGate,
    tmp_path: Path,
) -> None:
    gate.identity.unlink()
    runner = FakeRunner()
    with pytest.raises(transfer.TransferConfigurationFailure):
        transfer.BrokerTransfer(gate, runner=runner).receive(
            "outbox/ready.json",
            tmp_path / "ready",
            10,
        )
    assert not runner.calls


def test_failed_local_publication_leaves_no_final_or_partial_record(
    gate: transfer.BrokerGate,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = transfer._copy_verified
    destination = tmp_path / "ready"

    def partial_copy(
        path: Path,
        spec: transfer.FileSpec,
        deadline: float,
        target: int | None = None,
        *,
        private: bool = False,
    ) -> transfer.TransferReceipt:
        if path.name == "data" and target is not None:
            assert not destination.exists()
            os.write(target, b"partial")
            raise OSError("synthetic write failure")
        return original(path, spec, deadline, target, private=private)

    monkeypatch.setattr(transfer, "_copy_verified", partial_copy)
    with pytest.raises(transfer.TransferFailure):
        transfer.BrokerTransfer(gate, runner=FakeRunner()).receive(
            "outbox/ready.json",
            destination,
            10,
        )
    assert not destination.exists()
    assert set(path.name for path in tmp_path.iterdir()) == {"identity", "known_hosts"}


def test_destination_created_during_transfer_is_preserved(
    gate: transfer.BrokerGate,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "ready"

    def runner(command: transfer.TransferCommand, seconds: float) -> None:
        FakeRunner()(command, seconds)
        destination.write_bytes(b"concurrent existing record")

    with pytest.raises(transfer.TransferFailure):
        transfer.BrokerTransfer(gate, runner=runner).receive(
            "outbox/ready.json",
            destination,
            10,
        )
    assert destination.read_bytes() == b"concurrent existing record"
    assert not list(tmp_path.glob(".vast-transfer-*"))


def test_published_bytes_are_bound_to_returned_digest(
    gate: transfer.BrokerGate,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = transfer._publish

    def change_before_publish(
        data: Path,
        local: Path,
        spec: transfer.FileSpec,
        deadline: float,
    ) -> None:
        data.write_bytes(b"x" * data.stat().st_size)
        original(data, local, spec, deadline)

    monkeypatch.setattr(transfer, "_publish", change_before_publish)
    destination = tmp_path / "ready"
    with pytest.raises(transfer.TransferFailure, match="HASH_MISMATCH"):
        transfer.BrokerTransfer(gate, runner=FakeRunner()).receive(
            "outbox/ready.json",
            destination,
            10,
        )
    assert not destination.exists()
    assert not list(tmp_path.glob(".vast-transfer-*"))


def test_successful_publication_is_complete_and_single_linked(
    gate: transfer.BrokerGate,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = transfer._publish
    content = b"a" * 120_000
    destination = tmp_path / "ready"

    def inspect_before_publish(
        data: Path,
        local: Path,
        spec: transfer.FileSpec,
        deadline: float,
    ) -> None:
        assert not destination.exists()
        assert data.read_bytes() == content
        assert spec.sha256 == digest(content)
        original(data, local, spec, deadline)

    monkeypatch.setattr(transfer, "_publish", inspect_before_publish)
    broker = transfer.BrokerTransfer(gate, runner=FakeRunner(content))
    assert broker.instance_id == 123
    assert broker.receive("outbox/ready.json", destination, 10).sha256 == digest(
        content
    )
    assert destination.stat().st_nlink == 1
    assert destination.read_bytes() == content


def test_enclosing_alarm_is_deferred_until_owned_child_is_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_killpg = os.killpg
    original_handler = signal.getsignal(signal.SIGALRM)
    children: list[int] = []

    def expired(number: int, frame: object) -> None:
        del number, frame
        raise RuntimeError("synthetic enclosing controller deadline")

    def alarm_during_cleanup(pid: int, number: int) -> None:
        children.append(pid)
        os.kill(os.getpid(), signal.SIGALRM)
        original_killpg(pid, number)

    monkeypatch.setattr(transfer.os, "killpg", alarm_during_cleanup)
    signal.signal(signal.SIGALRM, expired)
    try:
        with pytest.raises(RuntimeError, match="synthetic enclosing"):
            transfer._run_bounded(
                local_command(tmp_path, "import time; time.sleep(60)"), 0.05
            )
        assert len(children) == 1
        with pytest.raises(ProcessLookupError):
            os.kill(children[0], 0)
    finally:
        signal.signal(signal.SIGALRM, original_handler)
        for pid in children:
            try:
                original_killpg(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
