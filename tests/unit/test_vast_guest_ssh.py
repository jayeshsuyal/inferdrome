"""Owned SFTP startup fakes; never install/start SSH, change UID or use a GPU."""

from __future__ import annotations

import base64
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from inferdrome.deployment import vast_guest_ssh as ssh
from inferdrome.deployment.vast_ssh_trust import parse_log_announcement


def public_key(value: int) -> str:
    wire = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([value]) * 32
    return "ssh-ed25519 " + base64.b64encode(wire).decode()


class Clock:
    def __init__(self) -> None:
        self.start = datetime(2030, 1, 1, tzinfo=UTC)
        self.elapsed = 0.0
        self.wall_offset = 0.0

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.elapsed + self.wall_offset)

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


def binding(clock: Clock) -> ssh.StartupBinding:
    return ssh.StartupBinding(
        run_nonce="1" * 32,
        intent_sha256="sha256:" + "2" * 64,
        execution_deadline=(clock.start + timedelta(seconds=4)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        cleanup_deadline=(clock.start + timedelta(seconds=14)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        client_public_key=public_key(3),
    )


class Child:
    def __init__(
        self, pid: int, clock: Clock, exits: float | None = None, status: int = 0
    ) -> None:
        self.pid, self.clock, self.exits, self.status = pid, clock, exits, status
        self.stopped = False

    def poll(self) -> int | None:
        if self.stopped:
            return -15
        return (
            self.status
            if self.exits is not None and self.clock.elapsed >= self.exits
            else None
        )

    def wait(self, timeout: float | None = None) -> int:
        assert self.poll() is not None
        return self.poll() or 0


class Supervision:
    def __init__(self, guest_exits: float | None = None, guest_status: int = 0) -> None:
        self.clock = Clock()
        self.binding = binding(self.clock)
        self.commands: list[ssh.Command] = []
        self.children = [
            Child(101, self.clock),
            Child(102, self.clock, guest_exits, guest_status),
        ]
        self.stops: list[tuple[int, float]] = []
        self.stop_budgets: list[float] = []
        self.markers: list[bytes] = []

    def spawn(self, command: ssh.Command) -> Child:
        self.commands.append(command)
        return self.children[len(self.commands) - 1]

    def stop(self, child: ssh.Child, seconds: float) -> None:
        self.stops.append((child.pid, self.clock.elapsed))
        self.stop_budgets.append(seconds)
        assert isinstance(child, Child)
        child.stopped = True

    def run(self, **kwargs: Any) -> None:
        ssh.supervise(
            self.binding,
            101,
            ssh.Prepared(b"synthetic-public-marker\n"),
            spawn_child=self.spawn,
            stop=self.stop,
            emit=self.markers.append,
            monotonic=self.clock.monotonic,
            now=self.clock.now,
            sleep=self.clock.sleep,
            **kwargs,
        )


@pytest.mark.parametrize("status", [0, 2])
def test_management_survives_guest_completion_until_original_teardown(
    status: int,
) -> None:
    test = Supervision(guest_exits=1, guest_status=status)
    test.run(ready=lambda seconds: True)
    assert [command.uid for command in test.commands] == [0, 2000]
    assert test.stops[0][0] == 102 and 1 <= test.stops[0][1] < 2
    assert test.stops[1][0] == 101 and 7.9 <= test.stops[1][1] <= 8.1
    assert len(test.commands) == 2 and len(test.markers) == 1


def test_running_guest_stops_at_execution_deadline_without_restarting() -> None:
    test = Supervision()
    test.run(ready=lambda seconds: True)
    assert test.stops[0][0] == 102 and 4 <= test.stops[0][1] < 4.2
    assert test.stops[1][0] == 101 and test.stops[1][1] < 8.2


@pytest.mark.parametrize("readiness", [False, "true"])
def test_readiness_requires_literal_true_and_failure_stops_only_owned_daemon(
    readiness: object,
) -> None:
    test = Supervision()
    with pytest.raises(ssh.StartupFailure, match="NOT_READY"):
        test.run(ready=lambda seconds: readiness)
    assert len(test.commands) == 1
    assert [pid for pid, _ in test.stops] == [101]
    assert not test.markers


def test_late_readiness_cannot_start_guest() -> None:
    test = Supervision()

    def late(seconds: float) -> bool:
        test.clock.elapsed += seconds
        return True

    with pytest.raises(ssh.StartupFailure, match="NOT_READY"):
        test.run(ready=late)
    assert len(test.commands) == 1 and not test.markers
    assert [pid for pid, _ in test.stops] == [101]


def test_daemon_death_stops_guest_and_never_restarts() -> None:
    test = Supervision()
    test.children[0].exits = 2
    with pytest.raises(ssh.StartupFailure, match="DAEMON_DIED"):
        test.run(ready=lambda seconds: True)
    assert len(test.commands) == 2
    assert [pid for pid, _ in test.stops] == [102, 101]


def test_second_spawn_failure_still_stops_daemon() -> None:
    test = Supervision()
    original = test.spawn

    def fail(command: ssh.Command) -> Child:
        if command.uid == 2000:
            raise OSError("synthetic spawn failure")
        return original(command)

    test.spawn = fail  # type: ignore[method-assign]
    with pytest.raises(OSError):
        test.run(ready=lambda seconds: True)
    assert [pid for pid, _ in test.stops] == [101]


def test_teardown_attempts_both_children_after_cleanup_failure() -> None:
    test = Supervision()

    def fail(child: ssh.Child, seconds: float) -> None:
        del seconds
        test.stops.append((child.pid, test.clock.elapsed))
        if child.pid == 102:
            raise OSError("synthetic teardown failure")

    test.stop = fail  # type: ignore[method-assign]
    with pytest.raises(ssh.StartupFailure, match="TEARDOWN_UNCONFIRMED"):
        test.run(ready=lambda seconds: True)
    assert test.stops[-1][0] == 101


def test_wall_clock_rollback_does_not_renew_startup_lifetime() -> None:
    test = Supervision(guest_exits=1)
    test.clock.wall_offset = -100
    test.run(ready=lambda seconds: True, started_at=(test.clock.start, 0.0))
    assert 7.9 <= test.clock.elapsed <= 8.1


@pytest.mark.parametrize("remaining", [0.0, 0.2])
def test_delayed_spawn_does_not_renew_teardown_budget(remaining: float) -> None:
    test = Supervision()
    original = test.spawn

    def late(command: ssh.Command) -> Child:
        child = original(command)
        if command.uid == 2000:
            test.clock.elapsed = 14.0 - remaining
        return child

    test.spawn = late  # type: ignore[method-assign]
    test.run(ready=lambda seconds: True)
    assert [pid for pid, _ in test.stops] == [102, 101]
    assert test.stop_budgets == pytest.approx([remaining, remaining])


def test_closed_command_and_environment_do_not_inherit_provider_or_ssh_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTAINER_API_KEY", "synthetic-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/synthetic-agent")
    monkeypatch.setenv("HF_TOKEN", "synthetic-hf-token")
    test = Supervision(guest_exits=1)
    test.run(ready=lambda seconds: True)
    daemon, guest = test.commands
    assert daemon.argv == ("/usr/sbin/sshd", "-D", "-e", "-f", str(ssh.CONFIG))
    assert guest.argv == ssh.bootstrap_argv(test.binding)
    assert "guest-sftp-v2" in guest.argv
    assert guest.gid == 0 and guest.umask == 0o077
    assert guest.environment["CONTAINER_ID"] == "101"
    assert not any(
        key in guest.environment
        for key in ("CONTAINER_API_KEY", "SSH_AUTH_SOCK", "HF_TOKEN")
    )
    assert "synthetic-secret" not in repr(test.commands)


def test_native_spawn_clears_supplementary_groups_and_sets_real_child_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake(argv: tuple[str, ...], **kwargs: Any) -> Child:
        calls.append(kwargs)
        return Child(101, Clock())

    monkeypatch.setattr(ssh.subprocess, "Popen", fake)
    ssh.spawn(ssh.Command(("synthetic",), {}, 2000, 0, 0o077))
    assert calls[0]["user"] == 2000 and calls[0]["group"] == 0
    assert calls[0]["extra_groups"] == () and calls[0]["start_new_session"] is True
    assert calls[0]["close_fds"] is True and "preexec_fn" not in calls[0]


def test_dedicated_config_uses_internal_sftp_and_disables_other_access() -> None:
    text = ssh.render_sshd_config().decode()
    for line in (
        "Port 2222",
        "Subsystem sftp internal-sftp",
        "ForceCommand internal-sftp -d /uploads -u 0027",
        "ChrootDirectory /srv/inferdrome-sftp",
        "DisableForwarding yes",
        "PermitTTY no",
        "PermitRootLogin no",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "AuthenticationMethods publickey",
        "AllowUsers inferdrome-transfer",
        "PermitUserRC no",
        "UsePAM no",
        f"HostKey {ssh.HOST_KEY}",
        "AuthorizedKeysFile /run/inferdrome-ssh-public/authorized_keys",
    ):
        assert line + "\n" in text
    assert "Include " not in text and "AcceptEnv " not in text
    assert "8000" not in text and "8001" not in text


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_nonce", "bad"),
        ("intent_sha256", "bad"),
        ("client_public_key", "ssh-rsa bad"),
        ("client_public_key", public_key(3) + " injected-comment"),
        ("cleanup_deadline", "2030-01-01T00:00:05Z"),
    ],
)
def test_startup_binding_is_closed_before_effects(field: str, value: str) -> None:
    data = binding(Clock()).model_dump()
    data[field] = value
    with pytest.raises(ValueError):
        ssh.StartupBinding.model_validate(data)


@pytest.mark.parametrize(
    "pid,uid,raw_id", [(2, 0, "101"), (1, 2000, "101"), (1, 0, "00101"), (1, 0, "0")]
)
def test_pid1_root_and_exact_container_identity_required(
    pid: int, uid: int, raw_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ssh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ssh.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(ssh.os, "getpid", lambda: pid)
    monkeypatch.setattr(ssh.os, "getresuid", lambda: (uid, uid, uid), raising=False)
    monkeypatch.setattr(ssh.os, "getresgid", lambda: (0, 0, 0), raising=False)
    monkeypatch.setenv("CONTAINER_ID", raw_id)
    with pytest.raises(ssh.StartupFailure):
        ssh.require_root_pid1()


def fake_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[Any], list[Any]]:
    chroot = tmp_path / "chroot"
    chroot.mkdir()
    (chroot / "uploads").mkdir()
    (chroot / "downloads").mkdir()
    monkeypatch.setattr(ssh, "CHROOT", chroot)
    monkeypatch.setattr(ssh, "require_root_pid1", lambda: 101)
    monkeypatch.setattr(ssh, "_directory", lambda *args: None)
    made: list[Any] = []
    written: list[Any] = []
    monkeypatch.setattr(
        ssh, "_make_directory", lambda *args, **kwargs: made.append((args, kwargs))
    )
    monkeypatch.setattr(ssh, "_write_root", lambda *args: written.append(args))
    monkeypatch.setattr(ssh, "_root_bytes", lambda *args: b"synthetic-private-key")
    monkeypatch.setattr(ssh.os.path, "lexists", lambda name: False)

    def account(name: str) -> SimpleNamespace:
        return SimpleNamespace(
            pw_uid=2001 if name == ssh.TRANSFER_USER else 2000,
            pw_gid=0,
            pw_dir="/uploads" if name == ssh.TRANSFER_USER else "/home/vllm",
            pw_shell="/usr/sbin/nologin",
        )

    monkeypatch.setattr(ssh.pwd, "getpwnam", account)

    def capture(argv: tuple[str, ...], seconds: float) -> bytes:
        if argv[0] == "/usr/bin/passwd":
            return b"inferdrome-transfer P synthetic-status"
        if "-y" in argv:
            assert argv[-1] == str(ssh.HOST_KEY)
            return (public_key(7) + "\n").encode()
        return b""

    monkeypatch.setattr(ssh, "_captured", capture)
    return made, written


def test_preparation_announces_key_derived_from_daemon_private_file_and_exact_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made, written = fake_preparation(tmp_path, monkeypatch)
    value = binding(Clock())
    prepared = ssh.prepare_startup(value, 101)
    announcement = parse_log_announcement(
        prepared.announcement, instance_id=101, run_nonce=value.run_nonce
    )
    assert announcement.host_public_key == public_key(7)
    assert announcement.host_public_key != value.client_public_key
    assert ((ssh.CHROOT / "uploads", 2001, 0o750), {}) in made
    assert ((ssh.CHROOT / "downloads", 2000, 0o750), {}) in made
    assert ((ssh.PRIVATE, 0, 0o700), {"fresh": True}) in made
    assert ((ssh.PUBLIC, 0, 0o755), {"fresh": True}) in made
    assert (
        ssh.PUBLIC / "authorized_keys",
        ("restrict " + value.client_public_key + "\n").encode(),
        0o644,
    ) in written
    assert b"synthetic-private-key" not in prepared.announcement


def test_changed_private_key_during_derivation_fails_before_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, written = fake_preparation(tmp_path, monkeypatch)
    keys = iter((b"first-private", b"second-private"))
    monkeypatch.setattr(ssh, "_root_bytes", lambda *args: next(keys))
    with pytest.raises(ssh.StartupFailure, match="HOST_KEY_CHANGED"):
        ssh.prepare_startup(binding(Clock()), 101)
    assert not any(path == ssh.CONFIG for path, _, _ in written)


def test_locked_account_fails_before_any_directory_or_key_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made, written = fake_preparation(tmp_path, monkeypatch)
    monkeypatch.setattr(ssh, "_captured", lambda *args: b"inferdrome-transfer L locked")
    with pytest.raises(ssh.StartupFailure, match="ACCOUNT_LOCKED"):
        ssh.prepare_startup(binding(Clock()), 101)
    assert made == written == []


def test_root_publication_is_create_only_private_and_rejects_links(
    tmp_path: Path,
) -> None:
    path = tmp_path / "binding"
    ssh._write_root(path, b"synthetic", 0o600)
    assert path.read_bytes() == b"synthetic"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        ssh._write_root(path, b"replacement", 0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        ssh._root_bytes(link, 100)
    os.link(path, tmp_path / "hardlink")
    with pytest.raises(OSError):
        ssh._root_bytes(path, 100)


def test_stop_masks_repeated_interrupts_and_escalates_owned_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    child = Child(101, Clock())

    def wait(timeout: float | None = None) -> int:
        calls.append(("wait", timeout))
        if len([call for call in calls if call[0] == "wait"]) == 1:
            raise subprocess.TimeoutExpired("synthetic", timeout)
        return -9

    child.wait = wait  # type: ignore[method-assign]
    monkeypatch.setattr(ssh.os, "killpg", lambda *args: calls.append(("signal", *args)))
    monkeypatch.setattr(
        ssh.signal,
        "pthread_sigmask",
        lambda *args: calls.append(("mask", *args)) or set(),
    )
    ssh.stop_child(child)
    assert [call[1:] for call in calls if call[0] == "signal"] == [
        (101, ssh.signal.SIGTERM),
        (101, ssh.signal.SIGKILL),
    ]
    assert calls[0][0] == calls[-1][0] == "mask"


def test_expired_teardown_still_kills_without_renewed_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = Child(101, Clock())
    waits: list[float | None] = []
    signals: list[tuple[int, int]] = []

    def waiting(timeout: float | None = None) -> int:
        waits.append(timeout)
        raise subprocess.TimeoutExpired("synthetic", timeout)

    child.wait = waiting  # type: ignore[method-assign]
    monkeypatch.setattr(ssh.os, "killpg", lambda *args: signals.append(args))
    with pytest.raises(subprocess.TimeoutExpired):
        ssh.stop_child(child, 0.0)
    assert waits == [0.0, 0.0]
    assert signals == [(101, ssh.signal.SIGTERM), (101, ssh.signal.SIGKILL)]


def fake_experiment_identity(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(ssh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ssh.os, "getresuid", lambda: (2000, 2000, 2000), raising=False)
    monkeypatch.setattr(ssh.os, "getresgid", lambda: (0, 0, 0), raising=False)
    monkeypatch.setattr(ssh.os, "getgroups", lambda: [])
    monkeypatch.setattr(
        ssh, "_enable_no_new_privileges", lambda: calls.append("nnp") or True
    )
    monkeypatch.setattr(
        ssh,
        "_process_status",
        lambda: b"".join(
            name + b":\t0000000000000000\n"
            for name in (b"CapInh", b"CapPrm", b"CapEff", b"CapAmb")
        ),
    )
    return calls


def test_experiment_identity_requires_no_new_privileges_before_continuing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = fake_experiment_identity(monkeypatch)
    ssh.require_experiment_identity()
    assert calls == ["nnp"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("getresuid", (2000, 2000, 0)),
        ("getresuid", (0, 2000, 2000)),
        ("getresgid", (0, 0, 1)),
        ("getgroups", [0]),
    ],
)
def test_saved_identity_and_supplementary_groups_fail_before_prctl(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    calls = fake_experiment_identity(monkeypatch)
    monkeypatch.setattr(ssh.os, field, lambda: value)
    with pytest.raises(ssh.StartupFailure, match="EXPERIMENT_IDENTITY_INVALID"):
        ssh.require_experiment_identity()
    assert calls == []


@pytest.mark.parametrize("value", [False, 1, "true"])
def test_failed_no_new_privileges_readback_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    value: Any,
) -> None:
    fake_experiment_identity(monkeypatch)
    monkeypatch.setattr(ssh, "_enable_no_new_privileges", lambda: value)
    with pytest.raises(ssh.StartupFailure, match="EXPERIMENT_IDENTITY_INVALID"):
        ssh.require_experiment_identity()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"CapInh: 0\n",
        b"CapInh: 0000000000000001\n",
        b"CapInh: 0000000000000000\nCapInh: 0000000000000000\n",
        b"CapPrm: not-hexadecimal!\n",
    ],
)
def test_missing_nonzero_or_malformed_capability_status_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
) -> None:
    fake_experiment_identity(monkeypatch)
    monkeypatch.setattr(ssh, "_process_status", lambda: content)
    with pytest.raises(ssh.StartupFailure, match="EXPERIMENT_IDENTITY_INVALID"):
        ssh.require_experiment_identity()


def test_prctl_wrapper_sets_then_reads_without_invoking_native_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, ...]] = []

    class Prctl:
        argtypes: Any
        restype: Any

        def __call__(self, *args: int) -> int:
            calls.append(args)
            return 0 if args[0] == 38 else 1

    monkeypatch.setattr(
        ssh.ctypes, "CDLL", lambda *a, **kw: SimpleNamespace(prctl=Prctl())
    )
    assert ssh._enable_no_new_privileges() is True
    assert calls == [(38, 1, 0, 0, 0), (39, 0, 0, 0, 0)]


@pytest.mark.parametrize("capability", [b"CapInh", b"CapPrm", b"CapEff", b"CapAmb"])
def test_any_remaining_capability_fails_with_complete_status(
    monkeypatch: pytest.MonkeyPatch,
    capability: bytes,
) -> None:
    fake_experiment_identity(monkeypatch)
    content = b"".join(
        name
        + b": "
        + (b"0000000000000001" if name == capability else b"0000000000000000")
        + b"\n"
        for name in (b"CapInh", b"CapPrm", b"CapEff", b"CapAmb")
    )
    monkeypatch.setattr(ssh, "_process_status", lambda: content)
    with pytest.raises(ssh.StartupFailure, match="EXPERIMENT_IDENTITY_INVALID"):
        ssh.require_experiment_identity()
