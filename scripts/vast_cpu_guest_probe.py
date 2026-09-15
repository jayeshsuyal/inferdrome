#!/usr/bin/env python3
"""Explicit container-side CPU qualification; importing this script does no work.

The host qualifier owns the disposable container, client keys and deadlines.
Management uses only its loopback daemon. Model mode deliberately invokes the
production downloader and must be separately selected by the host qualifier.
Neither mode starts a GPU workload or contacts the Vast control plane.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import platform
import pwd
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

from inferdrome.deployment import vast_guest_ssh as guest
from inferdrome.deployment.vast_bootstrap import Budget, stage_snapshot
from inferdrome.deployment.vast_control import _bounded_call
from inferdrome.deployment.vast_model_stage import (
    required_free_bytes,
    stage_pinned_model,
)
from inferdrome.deployment.vast_process import SFTP_MODULES
from inferdrome.deployment.vast_ssh_trust import HostKeyAnnouncement
from inferdrome.qwen3_campaign import (
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
    qwen3_model_manifest,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes

SCHEMA = "inferdrome.vast-cpu-guest-probe.v1"
SCRIPT = "/run/inferdrome-cpu-qualify.py"
CLIENT_ROOT = Path("/run/inferdrome-cpu-client")
OUTPUT_LIMIT = 32_768
PAYLOAD = b"inferdrome-owned-cpu-sftp-qualification-v1\n"
MANAGEMENT_CHECKS = (
    "pid1_root_supervisor",
    "accounts_and_permissions",
    "daemon_config",
    "host_key_binding",
    "client_key_binding",
    "experiment_identity",
    "experiment_dac",
    "transfer_dac",
    "sftp_before",
    "wrong_host_key",
    "wrong_client_key",
    "root_login",
    "password_auth",
    "shell_exec",
    "pty",
    "direct_forward",
    "remote_forward",
    "chroot_escape",
    "private_path",
    "download_write",
    "download_remove",
    "download_rename",
    "sftp_after",
    "upload_ownership",
)
MODEL_CHECKS = (
    "experiment_identity",
    "pinned_download",
    "snapshot_copy",
    "frozen_inventory",
    "file_ownership",
    "shared_stage_budget",
    "disk_budget",
)


class ProbeFailure(ValueError):
    """Only fixed codes may cross the process boundary."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProbeFailure(code)


def _metadata(path: Path, uid: int, mode: int, *, directory: bool = False) -> None:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if directory:
            flags |= os.O_DIRECTORY
        child = os.open(path.name, flags, dir_fd=descriptor)
        try:
            value = os.fstat(child)
            require(
                (value.st_uid, value.st_gid, stat.S_IMODE(value.st_mode))
                == (uid, 0, mode)
                and (
                    stat.S_ISDIR(value.st_mode)
                    if directory
                    else stat.S_ISREG(value.st_mode) and value.st_nlink == 1
                ),
                "FILE_METADATA_INVALID",
            )
        finally:
            os.close(child)
    finally:
        os.close(descriptor)


def _read(path: Path, limit: int = OUTPUT_LIMIT) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        content = os.read(fd, limit + 1)
        require(len(content) <= limit, "FILE_OUTPUT_LIMIT")
        return content
    finally:
        os.close(fd)


def _write(path: Path, content: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _status(path: Path) -> dict[str, str]:
    result = {}
    for line in _read(path, 65_536).decode("ascii").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            require(key not in result, "PROCESS_STATUS_INVALID")
            result[key] = value.strip()
    return result


def _root() -> None:
    require(
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and _ids("getresuid") == (0, 0, 0)
        and _ids("getresgid") == (0, 0, 0),
        "ROOT_CONTAINER_REQUIRED",
    )


def _ids(name: str) -> tuple[int, int, int]:
    method = getattr(os, name, None)
    if not callable(method):
        raise ProbeFailure("LINUX_IDENTITIES_UNAVAILABLE")
    value = method()
    require(
        isinstance(value, tuple)
        and len(value) == 3
        and all(type(item) is int for item in value),
        "PROCESS_IDS_INVALID",
    )
    return value[0], value[1], value[2]


def _identity(uid: int) -> dict[str, Any]:
    if uid == 2000:
        guest.require_experiment_identity()
    else:
        require(guest._enable_no_new_privileges(), "NO_NEW_PRIVILEGES_FAILED")
    value = _status(Path("/proc/self/status"))
    caps = {key: value.get(key) for key in ("CapInh", "CapPrm", "CapEff", "CapAmb")}
    require(
        _ids("getresuid") == (uid, uid, uid)
        and _ids("getresgid") == (0, 0, 0)
        and os.getgroups() == []
        and value.get("NoNewPrivs") == "1"
        and all(item == "0000000000000000" for item in caps.values()),
        "CHILD_IDENTITY_INVALID",
    )
    return {
        "uids": list(_ids("getresuid")),
        "gids": list(_ids("getresgid")),
        "groups": os.getgroups(),
        "no_new_privs": 1,
        "capabilities": caps,
    }


@dataclass(frozen=True)
class Captured:
    returncode: int
    stdout: bytes
    stderr: bytes


def _require_auth_refusal(result: Captured) -> None:
    require(
        result.returncode == 255
        and not result.stdout
        and re.fullmatch(
            rb"(?:root|inferdrome-transfer)@127\.0\.0\.1: "
            rb"Permission denied \(publickey\)\.\r?\n",
            result.stderr,
        )
        is not None,
        "AUTH_REFUSAL_AMBIGUOUS",
    )


def _require_host_key_refusal(result: Captured) -> None:
    require(
        result.returncode == 255
        and not result.stdout
        and b"REMOTE HOST IDENTIFICATION HAS CHANGED!" in result.stderr
        and b"Host key verification failed." in result.stderr,
        "WRONG_HOST_NOT_REFUSED",
    )


def _require_shell_refusal(result: Captured) -> None:
    require(
        result.returncode == 1
        and result.stdout.replace(b"\r\n", b"\n")
        == b"This service allows sftp connections only.\n",
        "SHELL_REFUSAL_AMBIGUOUS",
    )


def _require_pty_refusal(result: Captured) -> None:
    require(
        result.returncode == 255
        and not result.stdout
        and b"PTY allocation request failed on channel 0" in result.stderr,
        "PTY_REFUSAL_AMBIGUOUS",
    )


def _require_direct_forward_refusal(result: Captured) -> None:
    require(
        result.returncode == 255
        and not result.stdout
        and b"administratively prohibited" in result.stderr
        and b"stdio forwarding failed" in result.stderr,
        "DIRECT_FORWARD_AMBIGUOUS",
    )


def _require_remote_forward_refusal(result: Captured) -> None:
    require(
        result.returncode == 255
        and not result.stdout
        and b"remote port forwarding failed for listen port 0" in result.stderr,
        "REMOTE_FORWARD_AMBIGUOUS",
    )


def _require_sftp_refusal(result: Captured, expected: bytes) -> None:
    require(
        expected in {b"not found", b"Permission denied"}, "SFTP_EXPECTATION_INVALID"
    )
    # OpenSSH sftp.c process_get uses quiet remote glob/stat even for a literal
    # path; a missing file takes this branch before any canonicalization step.
    pattern = (
        rb'File "[^"\r\n]+" not found\.\r?\n'
        if expected == b"not found"
        else rb'(?:dest open "[^"\r\n]+"|remote delete /[^\r\n]+|'
        rb'remote rename "[^"\r\n]+" to "[^"\r\n]+"):'
        rb" Permission denied\r?\n"
    )
    require(
        result.returncode == 1 and re.fullmatch(pattern, result.stderr) is not None,
        "SFTP_REFUSAL_AMBIGUOUS",
    )


def _capture(argv: Sequence[str], budget: Budget) -> Captured:
    """No ambient credentials; bounded output and time; reap only our own group."""
    phase = budget.child(min(20, budget.remaining()))
    child = subprocess.Popen(
        tuple(argv),
        cwd="/",
        env={
            "PATH": "/usr/sbin:/usr/bin:/bin",
            "LANG": "C",
            "HOME": "/",
            "SSH_ASKPASS_REQUIRE": "never",
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
    )
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                stream = getattr(child, name)
                require(stream is not None, "PROCESS_PIPE_MISSING")
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                for key, _ in selector.select(min(0.1, phase.remaining())):
                    chunk = os.read(key.fd, 4096)
                    buffers[key.data].extend(chunk)
                    require(
                        sum(map(len, buffers.values())) <= OUTPUT_LIMIT,
                        "PROCESS_OUTPUT_LIMIT",
                    )
                    if not chunk:
                        selector.unregister(key.fileobj)
        returncode = child.wait(timeout=phase.remaining())
        return Captured(returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"]))
    finally:
        previous = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGALRM}
        )
        try:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _child(
    argv: Sequence[str],
    uid: int,
    report: Path,
    budget: Budget,
) -> dict[str, Any]:
    child = guest.spawn(
        guest.Command(
            (guest.PYTHON, "-I", SCRIPT, *argv),
            {
                "PATH": "/usr/bin:/bin",
                "HOME": "/home/vllm",
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            uid,
            0,
            0o077,
        )
    )
    try:
        while child.poll() is None:
            time.sleep(min(0.1, budget.remaining()))
        require(child.wait(timeout=budget.remaining()) == 0, "CHILD_FAILED")
        _metadata(report, uid, 0o600)
        value = json.loads(_read(report))
        if not isinstance(value, dict) or value.get("status") != "PASS":
            raise ProbeFailure("CHILD_REPORT_INVALID")
        return value
    finally:
        guest.stop_child(child, 3)


def _ssh_options(
    client: Path,
    budget: Budget,
    *,
    identity: str = "identity",
    known_hosts: str = "known_hosts",
    overrides: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    options = {
        "BatchMode": "yes",
        "StrictHostKeyChecking": "yes",
        "UserKnownHostsFile": str(client / known_hosts),
        "GlobalKnownHostsFile": "/dev/null",
        "HostKeyAlgorithms": "ssh-ed25519",
        "PubkeyAcceptedAlgorithms": "ssh-ed25519",
        "UpdateHostKeys": "no",
        "VerifyHostKeyDNS": "no",
        "CheckHostIP": "no",
        "CanonicalizeHostname": "no",
        "KnownHostsCommand": "none",
        "IdentitiesOnly": "yes",
        "IdentityAgent": "none",
        "AddKeysToAgent": "no",
        "ForwardAgent": "no",
        "ForwardX11": "no",
        "ClearAllForwardings": "yes",
        "PermitLocalCommand": "no",
        "SendEnv": "-*",
        "ProxyCommand": "none",
        "ProxyJump": "none",
        "RequestTTY": "no",
        "PasswordAuthentication": "no",
        "KbdInteractiveAuthentication": "no",
        "PreferredAuthentications": "publickey",
        "ControlMaster": "no",
        "ControlPath": "none",
        "ControlPersist": "no",
        "LogLevel": "ERROR",
        "ConnectionAttempts": "1",
        "ConnectTimeout": str(max(1, min(10, math.ceil(budget.remaining())))),
        "ServerAliveInterval": "3",
        "ServerAliveCountMax": "1",
    }
    options.update(overrides or {})
    return (
        "-F",
        "/dev/null",
        "-4",
        *(part for key, value in options.items() for part in ("-o", f"{key}={value}")),
        "-i",
        str(client / identity),
    )


def _ssh(
    client: Path,
    budget: Budget,
    *,
    user: str = guest.TRANSFER_USER,
    flags: Sequence[str] = (),
    command: Sequence[str] = (),
    identity: str = "identity",
    known_hosts: str = "known_hosts",
    overrides: Mapping[str, str] | None = None,
) -> Captured:
    return _capture(
        (
            "/usr/bin/ssh",
            *_ssh_options(
                client,
                budget,
                identity=identity,
                known_hosts=known_hosts,
                overrides=overrides,
            ),
            "-p",
            "2222",
            *flags,
            f"{user}@127.0.0.1",
            *command,
        ),
        budget,
    )


def _sftp(client: Path, batch: str, budget: Budget) -> Captured:
    fd, name = tempfile.mkstemp(prefix="batch-", dir=client)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(batch.encode("ascii"))
        return _capture(
            (
                "/usr/bin/sftp",
                *_ssh_options(client, budget),
                "-S",
                "/usr/bin/ssh",
                "-P",
                "2222",
                "-R",
                "1",
                "-b",
                name,
                f"{guest.TRANSFER_USER}@127.0.0.1",
            ),
            budget,
        )
    finally:
        Path(name).unlink()


def _denied(call: Any) -> None:
    try:
        result = call()
    except OSError as error:
        require(error.errno in {errno.EACCES, errno.EPERM}, "DAC_REFUSAL_AMBIGUOUS")
    else:
        if isinstance(result, int):
            os.close(result)
        raise ProbeFailure("DAC_UNEXPECTED_ACCESS")


def _immutable_code_dac(nonce: str, uid: int) -> None:
    """Inspect the owned startup paths, never recursively scan unrelated files."""
    deployment = Path(guest.__file__).parent
    require(
        str(deployment) == "/opt/inferdrome-runtime/lib/python3.12/"
        "site-packages/inferdrome/deployment",
        "INSTALLED_PACKAGE_PATH_INVALID",
    )
    targets = {
        Path(guest.PYTHON),
        Path("/opt/inferdrome-downloader/bin/python"),
        Path(SCRIPT),
        Path("/usr/sbin/sshd"),
        Path("/usr/bin/ssh"),
        Path("/usr/bin/sftp"),
        deployment / "__init__.py",
        deployment.parent / "__init__.py",
        *(deployment / f"{name}.py" for name in SFTP_MODULES),
    }
    directories: set[Path] = set()
    files: set[Path] = set()
    for target in targets:
        resolved = target.resolve(strict=True)
        files.add(resolved)
        for path in (target, resolved):
            directories.update(path.parents)
            value = path.lstat()
            require(
                value.st_uid == 0 and value.st_gid == 0, "MANAGEMENT_CODE_OWNER_INVALID"
            )
            if not stat.S_ISLNK(value.st_mode):
                require(
                    stat.S_ISREG(value.st_mode) and not value.st_mode & 0o022,
                    "MANAGEMENT_CODE_WRITABLE",
                )
    for path in sorted(directories):
        value = path.lstat()
        require(
            value.st_uid == 0 and value.st_gid == 0, "MANAGEMENT_ANCESTOR_OWNER_INVALID"
        )
        resolved = path.resolve(strict=True)
        value = resolved.stat()
        require(
            stat.S_ISDIR(value.st_mode)
            and value.st_uid == 0
            and value.st_gid == 0
            and not value.st_mode & 0o022,
            "MANAGEMENT_ANCESTOR_WRITABLE",
        )
        marker = resolved / f".inferdrome-cpu-dac-{nonce}-{uid}"
        _denied(
            lambda marker=marker: os.open(
                marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        )
    for path in sorted(files):
        _denied(lambda path=path: os.open(path, os.O_WRONLY | os.O_NOFOLLOW))


def _identity_child(arguments: argparse.Namespace) -> dict[str, Any]:
    root = Path(arguments.root)
    require(
        re.fullmatch(r"/workspace/inferdrome-cpu-management-[0-9a-f]{32}", str(root))
        is not None,
        "CHILD_PATH_INVALID",
    )
    nonce = root.name.removeprefix("inferdrome-cpu-management-")
    phase = arguments.phase
    uid = 2001 if phase == "transfer-dac" else 2000
    identity = _identity(uid)
    _immutable_code_dac(nonce, uid)
    for path in (guest.HOST_KEY, CLIENT_ROOT / "identity", Path("/etc/shadow")):
        _denied(lambda path=path: os.open(path, os.O_RDONLY))
    _denied(lambda: os.open("/etc/passwd", os.O_WRONLY))
    _denied(lambda: os.setuid(0))
    download = guest.CHROOT / "downloads" / f"cpu-{nonce}.bin"
    if phase == "prepare":
        _write(root / "private.bin", PAYLOAD)
        _write(download, PAYLOAD, 0o640)
    elif phase == "verify":
        for suffix in ("before", "after"):
            uploaded = guest.CHROOT / "uploads" / f"cpu-{nonce}-{suffix}.bin"
            _metadata(uploaded, 2001, 0o640)
            require(_read(uploaded) == PAYLOAD, "UPLOAD_CONTENT_INVALID")
    else:
        require(phase == "transfer-dac", "CHILD_PHASE_INVALID")
        _denied(lambda: os.open(root / "private.bin", os.O_RDONLY))
        _denied(lambda: os.open(download, os.O_WRONLY))
        require(_read(download) == PAYLOAD, "DOWNLOAD_READ_INVALID")
    value = {"status": "PASS", "identity": identity, "dac": True}
    report_root = (
        Path("/run") / f"inferdrome-cpu-transfer-{nonce}" if uid == 2001 else root
    )
    report = report_root / f"{phase}.json"
    _write(report, canonical_json_bytes(value))
    return value


def _management_bindings(
    arguments: argparse.Namespace,
    budget: Budget,
    checks: dict[str, bool],
) -> HostKeyAnnouncement:
    _root()
    require(Path(arguments.client_root) == CLIENT_ROOT, "CLIENT_PATH_INVALID")
    status = _status(Path("/proc/1/status"))
    require(
        status.get("Uid", "").split() == ["0"] * 4
        and status.get("Gid", "").split() == ["0"] * 4,
        "PID1_IDENTITY_INVALID",
    )
    cmd = _read(Path("/proc/1/cmdline")).rstrip(b"\0").decode("ascii").split("\0")
    prefix = [guest.PYTHON, "-I", "-m", "inferdrome.deployment.vast_guest_ssh", "serve"]
    require(cmd[:5] == prefix and len(cmd[5:]) == 10, "PID1_COMMAND_INVALID")
    pairs = dict(zip(cmd[5::2], cmd[6::2], strict=True))
    binding = guest.StartupBinding.model_validate_json(
        _read(guest.PRIVATE / "binding.json")
    )
    expected = {
        "--" + key.replace("_", "-"): value
        for key, value in binding.model_dump().items()
    }
    require(
        pairs == expected
        and binding.run_nonce == arguments.nonce
        and binding.execution_deadline == arguments.deadline,
        "PID1_BINDING_INVALID",
    )
    checks["pid1_root_supervisor"] = True
    accounts = pwd.getpwall()
    for name, uid, home in (
        (guest.EXPERIMENT_USER, 2000, "/home/vllm"),
        (guest.TRANSFER_USER, 2001, "/uploads"),
    ):
        entries = [item for item in accounts if item.pw_uid == uid]
        require(len(entries) == 1, "ACCOUNT_IDENTITY_INVALID")
        item = entries[0]
        require(
            (item.pw_name, item.pw_gid, item.pw_dir, item.pw_shell)
            == (name, 0, home, "/usr/sbin/nologin"),
            "ACCOUNT_IDENTITY_INVALID",
        )
    for path, uid, mode in (
        (Path("/workspace"), 2000, 0o700),
        (Path("/home/vllm"), 2000, 0o700),
        (Path("/srv"), 0, 0o755),
        (guest.CHROOT, 0, 0o755),
        (guest.CHROOT / "uploads", 2001, 0o750),
        (guest.CHROOT / "downloads", 2000, 0o750),
        (guest.PRIVATE, 0, 0o700),
        (guest.PUBLIC, 0, 0o755),
        (Path("/run/sshd"), 0, 0o755),
        (CLIENT_ROOT, 0, 0o700),
    ):
        _metadata(path, uid, mode, directory=True)
    for name, mode in (
        ("passwd", 0o644),
        ("group", 0o644),
        ("shadow", 0o600),
        ("gshadow", 0o600),
    ):
        _metadata(Path("/etc") / name, 0, mode)
    require(not os.path.lexists("/etc/ssh/sshrc"), "GLOBAL_SSH_RC_PRESENT")
    require(
        set(item.name for item in guest.CHROOT.iterdir()) == {"uploads", "downloads"},
        "CHROOT_LAYOUT_INVALID",
    )
    account_status = guest._captured(
        ("/usr/bin/passwd", "-S", guest.TRANSFER_USER), min(5, budget.remaining())
    ).split()
    require(
        account_status[:2] == [guest.TRANSFER_USER.encode(), b"P"],
        "TRANSFER_ACCOUNT_LOCKED",
    )
    checks["accounts_and_permissions"] = True
    _metadata(guest.CONFIG, 0, 0o600)
    _metadata(guest.PRIVATE / "binding.json", 0, 0o600)
    require(_read(guest.CONFIG) == guest.render_sshd_config(), "DAEMON_CONFIG_CHANGED")
    guest._captured(
        ("/usr/sbin/sshd", "-t", "-f", str(guest.CONFIG)), min(5, budget.remaining())
    )
    checks["daemon_config"] = True
    _metadata(guest.HOST_KEY, 0, 0o600)
    private_hash = hashlib.sha256(_read(guest.HOST_KEY, 4096)).digest()
    key = (
        guest._captured(
            ("/usr/bin/ssh-keygen", "-y", "-f", str(guest.HOST_KEY)),
            min(5, budget.remaining()),
        )
        .decode("ascii")
        .removesuffix("\n")
    )
    require(
        key == arguments.host_public_key
        and hashlib.sha256(_read(guest.HOST_KEY, 4096)).digest() == private_hash,
        "HOST_KEY_BINDING_INVALID",
    )
    announcement = HostKeyAnnouncement(1, arguments.nonce, key)
    checks["host_key_binding"] = True
    for name in ("identity", "wrong_identity", "known_hosts"):
        _metadata(CLIENT_ROOT / name, 0, 0o600)
    require(
        _read(CLIENT_ROOT / "known_hosts") == f"[127.0.0.1]:2222 {key}\n".encode(),
        "LOOPBACK_PIN_INVALID",
    )
    client_key = (
        guest._captured(
            ("/usr/bin/ssh-keygen", "-y", "-f", str(CLIENT_ROOT / "identity")),
            min(5, budget.remaining()),
        )
        .decode("ascii")
        .strip()
    )
    _metadata(guest.PUBLIC / "authorized_keys", 0, 0o644)
    require(
        client_key == binding.client_public_key
        and _read(guest.PUBLIC / "authorized_keys")
        == f"restrict {client_key}\n".encode(),
        "CLIENT_KEY_BINDING_INVALID",
    )
    wrong_key = (
        guest._captured(
            ("/usr/bin/ssh-keygen", "-y", "-f", str(CLIENT_ROOT / "wrong_identity")),
            min(5, budget.remaining()),
        )
        .decode("ascii")
        .strip()
    )
    HostKeyAnnouncement(1, arguments.nonce, wrong_key)
    require(wrong_key not in {client_key, key}, "WRONG_KEY_NOT_DISTINCT")
    _write(
        CLIENT_ROOT / "wrong_known_hosts", f"[127.0.0.1]:2222 {wrong_key}\n".encode()
    )
    checks["client_key_binding"] = True
    return announcement


def _management(arguments: argparse.Namespace) -> dict[str, Any]:
    budget = Budget(arguments.deadline)
    checks = dict.fromkeys(MANAGEMENT_CHECKS, False)
    announcement = _management_bindings(arguments, budget, checks)
    root = Path("/workspace") / f"inferdrome-cpu-management-{arguments.nonce}"
    root.mkdir(mode=0o700)
    os.chown(root, 2000, 0)
    os.chmod(root, 0o700)
    # UID2001 cannot traverse the client key directory. Its report directory is
    # independent, outside that root, and never receives a key or host pin.
    transfer_report = Path("/run") / f"inferdrome-cpu-transfer-{arguments.nonce}"
    transfer_report.mkdir(mode=0o700)
    os.chown(transfer_report, 2001, 0)
    os.chmod(transfer_report, 0o700)
    identity_args = ("identity-child", "--root", str(root))
    first = _child(
        (*identity_args, "--phase", "prepare"), 2000, root / "prepare.json", budget
    )
    checks["experiment_identity"] = True
    checks["experiment_dac"] = first.get("dac") is True
    # Transfer DAC is a real native UID2001 process, independent of chroot tests.
    transfer = _child(
        (*identity_args, "--phase", "transfer-dac"),
        2001,
        transfer_report / "transfer-dac.json",
        budget,
    )
    checks["transfer_dac"] = True
    client = CLIENT_ROOT
    _write(client / "payload.bin", PAYLOAD, 0o640)
    download = f"/downloads/cpu-{arguments.nonce}.bin"

    def positive(suffix: str) -> None:
        remote = f"/uploads/cpu-{arguments.nonce}-{suffix}.bin"
        received = client / f"received-{suffix}.bin"
        result = _sftp(
            client,
            f"put {client / 'payload.bin'} {remote}.pending\n"
            f"rename {remote}.pending {remote}\nget {download} {received}\n",
            budget,
        )
        require(
            result.returncode == 0 and _read(received) == PAYLOAD,
            "SFTP_POSITIVE_FAILED",
        )
        _metadata(guest.CHROOT / remote.lstrip("/"), 2001, 0o640)
        require(
            not (guest.CHROOT / (remote.lstrip("/") + ".pending")).exists(),
            "SFTP_RENAME_FAILED",
        )
        checks["sftp_" + suffix] = True

    positive("before")
    result = _ssh(client, budget, known_hosts="wrong_known_hosts", command=("true",))
    _require_host_key_refusal(result)
    checks["wrong_host_key"] = True
    for name, kwargs in (
        ("wrong_client_key", {"identity": "wrong_identity"}),
        ("root_login", {"user": "root"}),
        (
            "password_auth",
            {
                "overrides": {
                    "PubkeyAuthentication": "no",
                    "PasswordAuthentication": "yes",
                    "KbdInteractiveAuthentication": "yes",
                    "NumberOfPasswordPrompts": "0",
                    "PreferredAuthentications": "password,keyboard-interactive",
                }
            },
        ),
    ):
        result = _ssh(client, budget, command=("true",), **kwargs)
        _require_auth_refusal(result)
        checks[name] = True
    result = _ssh(client, budget, command=("printf INFERDROME_UNEXPECTED_SHELL",))
    _require_shell_refusal(result)
    checks["shell_exec"] = True
    result = _ssh(
        client,
        budget,
        flags=("-tt", "-s"),
        command=("sftp",),
        overrides={"RequestTTY": "force"},
    )
    _require_pty_refusal(result)
    checks["pty"] = True
    result = _ssh(
        client,
        budget,
        flags=("-W", "127.0.0.1:2222"),
        overrides={"ClearAllForwardings": "no"},
    )
    _require_direct_forward_refusal(result)
    checks["direct_forward"] = True
    result = _ssh(
        client,
        budget,
        flags=("-N", "-R", "0:127.0.0.1:2222"),
        overrides={"ClearAllForwardings": "no", "ExitOnForwardFailure": "yes"},
    )
    _require_remote_forward_refusal(result)
    checks["remote_forward"] = True
    for name, batch, expected in (
        (
            "chroot_escape",
            f"get /../../etc/passwd {client / 'escaped'}\n",
            b"not found",
        ),
        ("private_path", f"get {guest.HOST_KEY} {client / 'private'}\n", b"not found"),
        (
            "download_write",
            f"put {client / 'payload.bin'} /downloads/blocked\n",
            b"Permission denied",
        ),
        ("download_remove", f"rm {download}\n", b"Permission denied"),
        (
            "download_rename",
            f"rename {download} /uploads/stolen\n",
            b"Permission denied",
        ),
    ):
        result = _sftp(client, batch, budget)
        _require_sftp_refusal(result, expected)
        checks[name] = True
    for name in ("escaped", "private"):
        require(not (client / name).exists(), "CHROOT_LEAKED_FILE")
    require(
        not (guest.CHROOT / "downloads/blocked").exists()
        and not (guest.CHROOT / "uploads/stolen").exists(),
        "SFTP_NEGATIVE_MUTATED",
    )
    positive("after")
    _child((*identity_args, "--phase", "verify"), 2000, root / "verify.json", budget)
    checks["upload_ownership"] = True
    require(all(checks.values()), "MANAGEMENT_CHECKS_INCOMPLETE")
    disk = _capture(("/usr/bin/du", "-sx", "-B1", "/"), budget)
    require(
        disk.returncode == 0
        and not disk.stderr
        and re.fullmatch(rb"[1-9][0-9]*[\t ]+/\n", disk.stdout) is not None,
        "ROOT_DISK_MEASUREMENT_INVALID",
    )
    allocated = int(disk.stdout.split()[0])
    budget.remaining()
    return {
        "schema_version": SCHEMA,
        "mode": "management",
        "status": "PASS",
        "run_nonce": arguments.nonce,
        "host_key_sha256": announcement.host_key_sha256,
        "checks": checks,
        "experiment_identity": first["identity"],
        "transfer_identity": transfer["identity"],
        "root_filesystem_allocated_bytes": allocated,
        "gpu": "NOT_RUN",
        "campaign": "NOT_RUN",
    }


def _verify_inventory(directory: Path, budget: Budget) -> list[dict[str, Any]]:
    files = qwen3_model_manifest()["files"]
    require(
        set(item.name for item in directory.iterdir())
        == {item["path"] for item in files},
        "MODEL_INVENTORY_INVALID",
    )
    receipts = []
    for item in files:
        path = directory / item["path"]
        _metadata(path, 2000, 0o600)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            require(before.st_size == item["size_bytes"], "MODEL_SIZE_INVALID")
            digest, count = hashlib.sha256(), 0
            while True:
                budget.remaining()
                chunk = os.read(fd, min(1_048_576, item["size_bytes"] + 1 - count))
                if not chunk:
                    break
                digest.update(chunk)
                count += len(chunk)
                require(count <= item["size_bytes"], "MODEL_SIZE_INVALID")
            after, named = os.fstat(fd), path.lstat()
            fields = (
                "st_dev",
                "st_ino",
                "st_nlink",
                "st_uid",
                "st_gid",
                "st_mode",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            require(
                count == item["size_bytes"]
                and "sha256:" + digest.hexdigest() == item["sha256"]
                and all(
                    getattr(before, key) == getattr(after, key) == getattr(named, key)
                    for key in fields
                ),
                "MODEL_HASH_INVALID",
            )
            receipts.append(
                {
                    "path": item["path"],
                    "size_bytes": count,
                    "sha256": "sha256:" + digest.hexdigest(),
                    "uid": after.st_uid,
                    "gid": after.st_gid,
                    "mode": "0600",
                    "nlink": after.st_nlink,
                }
            )
        finally:
            os.close(fd)
    return receipts


def _model_child(arguments: argparse.Namespace) -> dict[str, Any]:
    identity = _identity(2000)
    require(
        math.isfinite(arguments.seconds) and arguments.seconds > 0,
        "MODEL_SECONDS_INVALID",
    )
    budget = Budget(arguments.deadline).child(arguments.seconds)
    root = Path(arguments.root)
    require(
        re.fullmatch(r"/workspace/inferdrome-cpu-model-[a-z0-9_]{8}", str(root))
        is not None,
        "MODEL_PATH_INVALID",
    )
    _metadata(root, 2000, 0o700, directory=True)
    require(not any(root.iterdir()), "MODEL_DIRECTORY_NOT_FRESH")
    stage, snapshot = root / "stage", root / "snapshot"
    stage.mkdir(mode=0o700)
    snapshot.mkdir(mode=0o700)
    free_before = shutil.disk_usage(root).free
    required = required_free_bytes()
    require(free_before >= required, "MODEL_DISK_BUDGET_FAILED")
    observed = {"minimum_free": free_before, "peak_tree": 0}
    stopped = threading.Event()
    failures: list[bool] = []

    def sample_once() -> None:
        observed["minimum_free"] = min(
            observed["minimum_free"], shutil.disk_usage(root).free
        )
        allocated = 0
        for parent, dirs, files in os.walk(root, followlinks=False):
            for name in (*dirs, *files):
                # The production downloader removes scratch files.
                with suppress(FileNotFoundError):
                    allocated += (Path(parent) / name).lstat().st_blocks * 512
        observed["peak_tree"] = max(observed["peak_tree"], allocated)

    def sample() -> None:
        try:
            while not stopped.is_set():
                sample_once()
                stopped.wait(0.1)
        except Exception:
            failures.append(True)

    sampler = threading.Thread(target=sample, name="cpu-disk-sample", daemon=True)
    sampler.start()
    try:
        # One original phase Budget covers both production steps and all hashing.
        def stage_and_verify() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            stage_pinned_model(stage, seconds=budget.remaining())
            stage_snapshot(stage, snapshot, budget)
            first = _verify_inventory(stage, budget)
            second = _verify_inventory(snapshot, budget)
            require(second == first, "MODEL_SNAPSHOTS_DIFFER")
            require(
                set(item.name for item in root.iterdir()) == {"stage", "snapshot"},
                "MODEL_SCRATCH_RETAINED",
            )
            budget.remaining()
            return first, second

        staged_files, snapshot_files = _bounded_call(
            stage_and_verify, budget.remaining()
        )
    finally:
        stopped.set()
        sampler.join(timeout=3)
    require(not sampler.is_alive() and not failures, "DISK_SAMPLE_FAILED")
    budget.remaining()
    sample_once()
    free_after = shutil.disk_usage(root).free
    observed["minimum_free"] = min(observed["minimum_free"], free_after)
    value = {
        "schema_version": SCHEMA,
        "mode": "model",
        "status": "PASS",
        "checks": dict.fromkeys(MODEL_CHECKS, True),
        "experiment_identity": identity,
        "frozen_revision": QWEN3_8B_REVISION,
        "snapshot_sha256": qwen3_expected_snapshot_sha256(),
        "inventory_file_count": len(staged_files),
        "inventory_total_bytes": sum(item["size_bytes"] for item in staged_files),
        "stage_files": staged_files,
        "snapshot_files": snapshot_files,
        "disk": {
            "required_free_bytes": required,
            "free_before_bytes": free_before,
            "free_after_bytes": free_after,
            "minimum_free_observed_bytes": observed["minimum_free"],
            "peak_observed_consumption_bytes": max(
                0, free_before - observed["minimum_free"]
            ),
            "peak_observed_tree_bytes": observed["peak_tree"],
        },
        "disk_sampling_interval_seconds": 0.1,
        "gpu": "NOT_RUN",
        "campaign": "NOT_RUN",
    }
    _write(root / "report.json", canonical_json_bytes(value))
    return value


def _model(arguments: argparse.Namespace) -> dict[str, Any]:
    _root()
    require(os.getpid() == 1, "MODEL_ROOT_PID1_REQUIRED")
    require(
        math.isfinite(arguments.seconds) and arguments.seconds > 0,
        "MODEL_SECONDS_INVALID",
    )
    budget = Budget(arguments.deadline).child(arguments.seconds)
    root = Path(tempfile.mkdtemp(prefix="inferdrome-cpu-model-", dir="/workspace"))
    os.chown(root, 2000, 0)
    os.chmod(root, 0o700)
    result = _child(
        (
            "model-child",
            "--root",
            str(root),
            "--deadline",
            arguments.deadline,
            "--seconds",
            str(budget.remaining()),
        ),
        2000,
        root / "report.json",
        budget,
    )
    budget.remaining()
    return result


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ProbeFailure("ARGUMENTS_INVALID")


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = _Parser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True, parser_class=_Parser)
    management = sub.add_parser("management")
    for name in ("nonce", "host-public-key", "client-root", "deadline"):
        management.add_argument("--" + name, required=True)
    model = sub.add_parser("model")
    model.add_argument("--deadline", required=True)
    model.add_argument("--seconds", required=True, type=float)
    identity = sub.add_parser("identity-child")
    identity.add_argument("--root", required=True)
    identity.add_argument(
        "--phase", choices=("prepare", "verify", "transfer-dac"), required=True
    )
    model_child = sub.add_parser("model-child")
    model_child.add_argument("--root", required=True)
    model_child.add_argument("--deadline", required=True)
    model_child.add_argument("--seconds", required=True, type=float)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    mode = "invalid"

    def interrupted(_number: int, _frame: Any) -> None:
        raise ProbeFailure("PROBE_INTERRUPTED")

    try:
        arguments = _arguments(argv)
        mode = arguments.mode
        os.umask(0o077)
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, interrupted)
        handlers = {
            "management": _management,
            "model": _model,
            "identity-child": _identity_child,
            "model-child": _model_child,
        }
        value = handlers[mode](arguments)
        output = canonical_json_bytes(value)
        require(len(output) <= OUTPUT_LIMIT, "REPORT_OUTPUT_LIMIT")
        if not mode.endswith("-child"):
            sys.stdout.buffer.write(output + b"\n")
            sys.stdout.buffer.flush()
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ProbeFailure) else "PROBE_FAILED"
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) is None:
            code = "PROBE_FAILED"
        if not mode.endswith("-child"):
            print(
                json.dumps(
                    {
                        "schema_version": SCHEMA,
                        "mode": mode,
                        "status": "FAIL",
                        "code": code,
                    },
                    sort_keys=True,
                )
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
