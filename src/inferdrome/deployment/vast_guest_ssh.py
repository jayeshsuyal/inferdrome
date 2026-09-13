"""Owned PID1 SFTP management; experiments permanently run as UID 2000.

This module defines startup, but importing it activates nothing. It owns one
Ed25519 daemon key and stock internal-sftp, never a shell/file dispatcher.
Transfer UID 2001 can write uploads and only read published downloads; private
experiment files stay outside its root-owned chroot. Provider cleanup remains
the external exact-ID controller's responsibility.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import pwd
import re
import selectors
import signal
import socket
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Protocol

from pydantic import Field, model_validator

from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.deployment.vast_control import _bounded_call
from inferdrome.deployment.vast_process import parse_deadline
from inferdrome.deployment.vast_ssh_trust import (
    HostKeyAnnouncement,
    format_announcement,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.routing_execution.contracts import Digest, ExecutionModel

CHROOT = Path("/srv/inferdrome-sftp")
PRIVATE = Path("/run/inferdrome-ssh")
PUBLIC = Path("/run/inferdrome-ssh-public")
HOST_KEY = PRIVATE / "host_ed25519"
CONFIG = PRIVATE / "sshd_config"
PYTHON = "/opt/inferdrome-runtime/bin/python"
TRANSFER_USER = "inferdrome-transfer"
EXPERIMENT_USER = "inferdrome-experiment"
TEARDOWN_RESERVE = 6.0


class StartupFailure(ValueError):
    """Fixed codes; child output, private keys and inherited secrets stay private."""


class StartupBinding(ExecutionModel):
    run_nonce: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    intent_sha256: Digest
    execution_deadline: str
    cleanup_deadline: str
    client_public_key: Annotated[str, Field(max_length=128)]

    @model_validator(mode="after")
    def _binding(self) -> StartupBinding:
        reserve = (
            parse_deadline(self.cleanup_deadline)
            - parse_deadline(self.execution_deadline)
        ).total_seconds()
        if not 10 <= reserve <= 600:
            raise ValueError("separate finite cleanup reserve required")
        HostKeyAnnouncement(1, self.run_nonce, self.client_public_key)
        return self


@dataclass(frozen=True)
class Prepared:
    announcement: bytes


class Child(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    uid: int
    gid: int
    umask: int


def render_sshd_config() -> bytes:
    # No Include, provider sshd configuration, per-user command or shell hook.
    return (
        "Port 2222\nListenAddress 0.0.0.0\nAddressFamily inet\n"
        f"HostKey {HOST_KEY}\nPidFile {PRIVATE / 'sshd.pid'}\n"
        f"AuthorizedKeysFile {PUBLIC / 'authorized_keys'}\n"
        f"AllowUsers {TRANSFER_USER}\n"
        "HostKeyAlgorithms ssh-ed25519\nPubkeyAcceptedAlgorithms ssh-ed25519\n"
        "PubkeyAuthentication yes\nAuthenticationMethods publickey\n"
        "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
        "PermitEmptyPasswords no\nPermitRootLogin no\nUsePAM no\n"
        "StrictModes yes\nPermitUserEnvironment no\nPermitUserRC no\n"
        "DisableForwarding yes\nPermitTTY no\nX11Forwarding no\n"
        "PermitTunnel no\nGatewayPorts no\nMaxSessions 1\nMaxAuthTries 3\n"
        "LoginGraceTime 10\nClientAliveInterval 5\nClientAliveCountMax 1\n"
        "PrintMotd no\nPrintLastLog no\nLogLevel ERROR\n"
        f"ChrootDirectory {CHROOT}\n"
        "Subsystem sftp internal-sftp\n"
        "ForceCommand internal-sftp -d /uploads -u 0027\n"
    ).encode("ascii")


def bootstrap_argv(binding: StartupBinding) -> tuple[str, ...]:
    return (
        PYTHON,
        "-I",
        "-m",
        "inferdrome.deployment.vast_bootstrap",
        "--profile",
        "guest-sftp-v2",
        "--run-nonce",
        binding.run_nonce,
        "--intent-sha256",
        binding.intent_sha256,
        "--deadline",
        binding.execution_deadline,
    )


def guest_environment(instance_id: int) -> dict[str, str]:
    return {
        "CONTAINER_ID": str(instance_id),
        "HOME": "/home/vllm",
        "PATH": "/opt/inferdrome-runtime/bin:/usr/local/bin:/usr/local/cuda/bin:"
        "/usr/local/nvidia/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "NVIDIA_VISIBLE_DEVICES": "all",
        "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
        "LD_LIBRARY_PATH": "/usr/local/nvidia/lib:/usr/local/nvidia/lib64:"
        "/usr/local/cuda/lib64",
    }


def _enable_no_new_privileges() -> bool:
    library = ctypes.CDLL(None, use_errno=True)
    prctl = library.prctl
    prctl.argtypes = [ctypes.c_int, *(ctypes.c_ulong for _ in range(4))]
    prctl.restype = ctypes.c_int
    # Linux PR_SET_NO_NEW_PRIVS and PR_GET_NO_NEW_PRIVS, with unused args zero.
    return bool(prctl(38, 1, 0, 0, 0) == 0 and prctl(39, 0, 0, 0, 0) == 1)


def _process_status() -> bytes:
    descriptor = os.open("/proc/self/status", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        content = os.read(descriptor, 65_537)
        if not content or len(content) > 65_536:
            raise StartupFailure("VAST_EXPERIMENT_IDENTITY_INVALID")
        return content
    finally:
        os.close(descriptor)


def require_experiment_identity() -> None:
    """Permanently bar privilege gain before the nonroot guest creates files."""
    try:
        uid = getattr(os, "getresuid", None)
        gid = getattr(os, "getresgid", None)
        if (
            platform.system() != "Linux"
            or uid is None
            or gid is None
            or uid() != (2000, 2000, 2000)
            or gid() != (0, 0, 0)
            or os.getgroups() != []
            or _enable_no_new_privileges() is not True
        ):
            raise StartupFailure("VAST_EXPERIMENT_IDENTITY_INVALID")
        required = {b"CapInh", b"CapPrm", b"CapEff", b"CapAmb"}
        observed: set[bytes] = set()
        for line in _process_status().splitlines():
            name, separator, value = line.partition(b":")
            if name in required:
                value = value.strip()
                if (
                    not separator
                    or name in observed
                    or re.fullmatch(rb"[0-9a-fA-F]{16}", value) is None
                    or int(value, 16) != 0
                ):
                    raise StartupFailure("VAST_EXPERIMENT_IDENTITY_INVALID")
                observed.add(name)
        if observed != required:
            raise StartupFailure("VAST_EXPERIMENT_IDENTITY_INVALID")
    except Exception:
        raise StartupFailure("VAST_EXPERIMENT_IDENTITY_INVALID") from None


def _management_environment() -> dict[str, str]:
    return {"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C", "HOME": "/"}


def spawn(command: Command) -> Child:
    # Popen's native user/group handling avoids a Python preexec_fn. setreuid
    # sets real/effective IDs before exec; supplementary groups are cleared.
    return subprocess.Popen(
        command.argv,
        env=dict(command.environment),
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        user=command.uid,
        group=command.gid,
        extra_groups=(),
        umask=command.umask,
    )


def stop_child(child: Child, seconds: float = 3.0) -> None:
    end = time.monotonic() + max(0.0, min(3.0, seconds))
    previous = signal.pthread_sigmask(
        signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT, signal.SIGALRM}
    )
    try:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
        with suppress(subprocess.TimeoutExpired):
            child.wait(timeout=max(0.0, min(1.5, (end - time.monotonic()) / 2)))
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=max(0.0, end - time.monotonic()))
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _captured(argv: Sequence[str], seconds: float) -> bytes:
    """Bound only fixed key/config tools; never emit their diagnostics."""
    deadline = time.monotonic() + seconds
    child = subprocess.Popen(
        tuple(argv),
        env=_management_environment(),
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    content = bytearray()
    try:
        assert child.stdout is not None
        os.set_blocking(child.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StartupFailure("VAST_SSH_TOOL_TIMEOUT")
                for key, _ in selector.select(min(0.1, remaining)):
                    chunk = os.read(key.fd, 4097 - len(content))
                    content.extend(chunk)
                    if len(content) > 4096:
                        raise StartupFailure("VAST_SSH_TOOL_OUTPUT_LIMIT")
                    if not chunk:
                        selector.unregister(key.fileobj)
            if child.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                raise StartupFailure("VAST_SSH_TOOL_FAILED")
        return bytes(content)
    finally:
        if child.poll() is None:
            stop_child(child, max(0.0, deadline - time.monotonic()))
        if child.stdout is not None:
            child.stdout.close()


def _directory(path: Path, uid: int, mode: int) -> None:
    """Check every path component without following links; final owner is exact."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        value = os.fstat(descriptor)
        if (
            value.st_uid != uid
            or value.st_gid != 0
            or stat.S_IMODE(value.st_mode) != mode
        ):
            raise StartupFailure("VAST_SSH_DIRECTORY_INVALID")
    finally:
        os.close(descriptor)


def _make_directory(path: Path, uid: int, mode: int, *, fresh: bool = False) -> None:
    parent = SafeDirFD.open(path.parent)
    try:
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=parent.fd)
        except FileExistsError:
            if fresh:
                raise StartupFailure("VAST_SSH_STARTUP_ALREADY_ATTEMPTED") from None
        else:
            fd = os.open(
                path.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent.fd,
            )
            try:
                os.fchown(fd, uid, 0)
                os.fchmod(fd, mode)
                os.fsync(fd)
            finally:
                os.close(fd)
            parent.fsync()
    finally:
        parent.close()
    _directory(path, uid, mode)


def _write_root(path: Path, content: bytes, mode: int) -> None:
    parent = SafeDirFD.open(path.parent)
    try:
        fd = parent.open_child(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        parent.fsync()
    finally:
        parent.close()


def _root_bytes(path: Path, maximum: int) -> bytes:
    parent = SafeDirFD.open(path.parent)
    try:
        fd = parent.open_child(path.name, os.O_RDONLY | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if before.st_size > maximum or before.st_mode & 0o077:
                raise StartupFailure("VAST_SSH_PRIVATE_FILE_INVALID")
            content = os.read(fd, maximum + 1)
            after = parent.validated_regular_child(path.name, descriptor=fd)
            if len(content) != before.st_size or any(
                getattr(before, field) != getattr(after, field)
                for field in ("st_size", "st_mtime_ns", "st_ctime_ns")
            ):
                raise StartupFailure("VAST_SSH_PRIVATE_FILE_CHANGED")
            os.fsync(fd)
            return content
        finally:
            os.close(fd)
    finally:
        parent.close()


def require_root_pid1() -> int:
    real_uids = getattr(os, "getresuid", None)
    real_gids = getattr(os, "getresgid", None)
    if (
        platform.system() != "Linux"
        or platform.machine() != "x86_64"
        or os.getpid() != 1
        or not callable(real_uids)
        or not callable(real_gids)
        or real_uids() != (0, 0, 0)
        or real_gids() != (0, 0, 0)
    ):
        raise StartupFailure("VAST_SSH_ROOT_PID1_REQUIRED")
    raw = os.environ.get("CONTAINER_ID", "")
    if not raw.isascii() or not raw.isdigit() or str(int(raw)) != raw:
        raise StartupFailure("VAST_SSH_INSTANCE_ID_INVALID")
    value = int(raw)
    if not 1 <= value <= 9_007_199_254_740_991:
        raise StartupFailure("VAST_SSH_INSTANCE_ID_INVALID")
    return value


def prepare_startup(binding: StartupBinding, instance_id: int) -> Prepared:
    if require_root_pid1() != instance_id:
        raise StartupFailure("VAST_SSH_INSTANCE_ID_INVALID")
    for name, uid, home in (
        (TRANSFER_USER, 2001, "/uploads"),
        (EXPERIMENT_USER, 2000, "/home/vllm"),
    ):
        account = pwd.getpwnam(name)
        if (account.pw_uid, account.pw_gid, account.pw_dir, account.pw_shell) != (
            uid,
            0,
            home,
            "/usr/sbin/nologin",
        ):
            raise StartupFailure("VAST_SSH_ACCOUNT_INVALID")
    if os.path.lexists("/etc/ssh/sshrc"):
        raise StartupFailure("VAST_SSH_GLOBAL_RC_FORBIDDEN")
    status = _captured(("/usr/bin/passwd", "-S", TRANSFER_USER), 5).split()
    if status[:2] != [TRANSFER_USER.encode(), b"P"]:
        raise StartupFailure("VAST_SSH_TRANSFER_ACCOUNT_LOCKED")
    _directory(Path("/workspace"), 2000, 0o700)
    _directory(Path("/srv"), 0, 0o755)
    _make_directory(CHROOT, 0, 0o755)
    _make_directory(CHROOT / "uploads", 2001, 0o750)
    _make_directory(CHROOT / "downloads", 2000, 0o750)
    for name in ("uploads", "downloads"):
        if any((CHROOT / name).iterdir()):
            raise StartupFailure("VAST_SSH_EXCHANGE_NOT_EMPTY")
    _make_directory(Path("/run/sshd"), 0, 0o755)
    _make_directory(PRIVATE, 0, 0o700, fresh=True)
    _make_directory(PUBLIC, 0, 0o755, fresh=True)
    _write_root(
        PRIVATE / "binding.json",
        canonical_json_bytes(binding.model_dump(mode="json")),
        0o600,
    )
    _write_root(
        PUBLIC / "authorized_keys",
        ("restrict " + binding.client_public_key + "\n").encode(),
        0o644,
    )
    _captured(
        (
            "/usr/bin/ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "",
            "-f",
            str(HOST_KEY),
        ),
        5,
    )
    private_before = _root_bytes(HOST_KEY, 4096)
    derived = _captured(("/usr/bin/ssh-keygen", "-y", "-f", str(HOST_KEY)), 5)
    if _root_bytes(HOST_KEY, 4096) != private_before:
        raise StartupFailure("VAST_SSH_HOST_KEY_CHANGED")
    public_key = derived.decode("ascii").removesuffix("\n")
    announcement = format_announcement(instance_id, binding.run_nonce, public_key)
    _write_root(CONFIG, render_sshd_config(), 0o600)
    _captured(("/usr/sbin/sshd", "-t", "-f", str(CONFIG)), 5)
    return Prepared(announcement)


def daemon_ready(seconds: float) -> bool:
    try:
        with socket.create_connection(
            ("127.0.0.1", 2222), timeout=min(0.2, seconds)
        ) as stream:
            return stream.recv(256).startswith(b"SSH-2.0-OpenSSH_")
    except OSError:
        return False


def supervise(
    binding: StartupBinding,
    instance_id: int,
    prepared: Prepared,
    *,
    spawn_child: Callable[[Command], Child] = spawn,
    stop: Callable[[Child, float], None] = stop_child,
    ready: Callable[[float], bool] = daemon_ready,
    emit: Callable[[bytes], None] = lambda value: print(
        value.decode("ascii"), end="", flush=True
    ),
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    started_at: tuple[datetime, float] | None = None,
) -> None:
    wall_started, started = started_at or (now(), monotonic())
    execution = parse_deadline(binding.execution_deadline)
    cleanup = parse_deadline(binding.cleanup_deadline)
    execution_end = started + (execution - wall_started).total_seconds()
    cleanup_end = started + (cleanup - wall_started).total_seconds()

    def remaining(deadline: datetime, end: float) -> float:
        return min((deadline - now()).total_seconds(), end - monotonic())

    children: list[Child] = []
    try:
        if not 0 < remaining(execution, execution_end) <= 7200:
            raise StartupFailure("VAST_SSH_EXECUTION_DEADLINE")
        daemon = spawn_child(
            Command(
                ("/usr/sbin/sshd", "-D", "-e", "-f", str(CONFIG)),
                _management_environment(),
                0,
                0,
                0o077,
            )
        )
        children.append(daemon)
        readiness_end = min(execution_end, monotonic() + 10)
        while True:
            seconds = min(
                readiness_end - monotonic(), remaining(execution, execution_end)
            )
            if seconds <= 0 or daemon.poll() is not None:
                raise StartupFailure("VAST_SSH_DAEMON_NOT_READY")
            observed = _bounded_call(partial(ready, seconds), seconds)
            if (
                remaining(execution, execution_end) <= 0
                or monotonic() >= readiness_end
                or daemon.poll() is not None
            ):
                raise StartupFailure("VAST_SSH_DAEMON_NOT_READY")
            if observed is True:
                break
            sleep(min(0.1, seconds))
        emit(prepared.announcement)
        if remaining(execution, execution_end) <= 0:
            raise StartupFailure("VAST_SSH_EXECUTION_DEADLINE")
        guest = spawn_child(
            Command(
                bootstrap_argv(binding), guest_environment(instance_id), 2000, 0, 0o077
            )
        )
        children.append(guest)
        guest_stopped = False
        while remaining(cleanup, cleanup_end) > TEARDOWN_RESERVE:
            if daemon.poll() is not None:
                raise StartupFailure("VAST_SSH_DAEMON_DIED")
            if not guest_stopped and (
                guest.poll() is not None or remaining(execution, execution_end) <= 0
            ):
                stop(guest, max(0.0, min(3.0, remaining(cleanup, cleanup_end))))
                guest_stopped = True
                children.remove(guest)
            sleep(
                min(0.1, max(0.0, remaining(cleanup, cleanup_end) - TEARDOWN_RESERVE))
            )
    finally:
        failed = False
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT, signal.SIGALRM}
        )
        try:
            for child in reversed(children):
                try:
                    stop(child, max(0.0, min(3.0, remaining(cleanup, cleanup_end))))
                except (OSError, subprocess.SubprocessError, ValueError):
                    failed = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if failed:
            raise StartupFailure("VAST_SSH_TEARDOWN_UNCONFIRMED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve",))
    for name in (
        "run-nonce",
        "intent-sha256",
        "execution-deadline",
        "cleanup-deadline",
        "client-public-key",
    ):
        parser.add_argument("--" + name, required=True)
    arguments = vars(parser.parse_args(argv))
    arguments.pop("command")

    def interrupted(_number: int, _frame: Any) -> None:
        raise StartupFailure("VAST_SSH_INTERRUPTED")

    try:
        binding = StartupBinding.model_validate(arguments)
        instance_id = require_root_pid1()
        started_at = (datetime.now(UTC), time.monotonic())
        seconds = (
            parse_deadline(binding.execution_deadline) - started_at[0]
        ).total_seconds()
        if not 0 < seconds <= 7200:
            raise StartupFailure("VAST_SSH_EXECUTION_DEADLINE")
        os.umask(0o077)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        prepared = _bounded_call(
            lambda: prepare_startup(binding, instance_id), min(20, seconds)
        )
        total = (
            parse_deadline(binding.cleanup_deadline) - started_at[0]
        ).total_seconds()
        _bounded_call(
            lambda: supervise(binding, instance_id, prepared, started_at=started_at),
            total - (time.monotonic() - started_at[1]),
        )
        return 0
    except Exception:
        print('{"status":"GUEST_SSH_FAILED","provider_cleanup":"CLEANUP_UNCONFIRMED"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
