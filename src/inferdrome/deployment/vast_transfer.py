"""Gated, single-file operator transfers through Vast's rsync broker.

This module does not discover/enroll brokers, fetch keys, call provider APIs,
or attest host-side ownership. Construction requires separate operator-verified
broker and host-key evidence. Unknown contracts stay a launch gate. Returned
outbox bytes remain untrusted until the bootstrap validates their schema,
instance, nonce, deadline and package digest. Upload commit markers last.
"""

from __future__ import annotations

import base64
import hashlib
import math
import os
import re
import secrets
import selectors
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.qwen3_campaign import qwen3_model_manifest

_ROOT = "/workspace/vast-bootstrap"
_CONTROL_LIMIT = 131_072
_EXPORT_LIMIT = 8_388_608
_OUTPUT_LIMIT = 8192
_CHUNK = 1_048_576
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HOST = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?\Z")


class TransferFailure(ValueError):
    """Fixed failure codes never expose child output, paths or credentials."""


class TransferConfigurationFailure(TransferFailure):
    """Invalid local/trust configuration; never retry as guest readiness."""


@dataclass(frozen=True)
class BrokerGate:
    """Operator evidence, never inferred from an unauthenticated key scan.

    The contract evidence must establish this exact instance's numeric rsync
    module, absolute container paths, and writes readable by UID 2000:GID 0.
    Evidence digests identify independently retained records; they are assertions
    by the operator, not a provider attestation produced by this module.
    """

    instance_id: int
    host: str
    port: int
    identity: Path
    known_hosts: Path
    known_hosts_sha256: str
    host_key_sha256: str
    broker_contract_verified: bool = False
    broker_contract_evidence_sha256: str | None = None
    broker_contract_source: str | None = None
    trusted_key_source: (
        Literal["PROVIDER_AUTHENTICATED_CHANNEL", "OPERATOR_OUT_OF_BAND"] | None
    ) = None
    trusted_key_evidence_sha256: str | None = None
    remote_uid: int | None = None
    remote_gid: int | None = None


@dataclass(frozen=True)
class FileSpec:
    remote_relative: str
    direction: Literal["send", "receive"]
    maximum_bytes: int
    size_bytes: int | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class TransferReceipt:
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class TransferCommand:
    """Injected runner seam. The runner must respect both byte/time limits."""

    argv: tuple[str, ...]
    environment: Mapping[str, str]
    directory: Path
    data_path: Path
    maximum_bytes: int
    direction: Literal["send", "receive"]


class TransferRunner(Protocol):
    def __call__(self, command: TransferCommand, seconds: float) -> None: ...


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def file_inventory() -> tuple[FileSpec, ...]:
    """Closed paths; callers may tighten controls, never extend the inventory."""
    result = (
        [
            FileSpec(f"inbox/{name}.json", "send", _CONTROL_LIMIT)
            for name in ("intent", "stage", "approval")
        ]
        + [
            FileSpec(f"outbox/{name}.json", "receive", _CONTROL_LIMIT)
            for name in ("ready", "plan", "result")
        ]
        + [
            FileSpec(f"outbox/export/{name}", "receive", _EXPORT_LIMIT)
            for name in (
                "executed-manifest.json",
                "input-transfer-receipt.json",
                "producer-receipt.json",
                "integrity-manifest.json",
            )
        ]
    )
    for item in qwen3_model_manifest()["files"]:
        name = item["path"]
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_.-]+", name) is None:
            raise TransferFailure("VAST_TRANSFER_MODEL_INVENTORY_INVALID")
        result.append(
            FileSpec(
                f"inbox/model/{name}",
                "send",
                item["size_bytes"],
                item["size_bytes"],
                item["sha256"],
            )
        )
    return tuple(result)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TransferFailure("VAST_TRANSFER_DEADLINE")
    return remaining


def _deadline(seconds: float) -> float:
    if (
        isinstance(seconds, bool)
        or not math.isfinite(seconds)
        or not 0 < seconds <= 3600
    ):
        raise TransferFailure("VAST_TRANSFER_BUDGET_INVALID")
    return time.monotonic() + seconds


def _copy_verified(
    path: Path,
    spec: FileSpec,
    deadline: float,
    target: int | None = None,
    *,
    private: bool = False,
) -> TransferReceipt:
    """Stream one descriptor-anchored file; no directory walks or archives."""
    directory = SafeDirFD.open(path.parent)
    descriptor: int | None = None
    try:
        descriptor = directory.open_child(path.name, os.O_RDONLY | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if (
            before.st_size > spec.maximum_bytes
            or (private and before.st_mode & 0o077)
            or (spec.size_bytes is not None and before.st_size != spec.size_bytes)
        ):
            raise TransferFailure("VAST_TRANSFER_FILE_INVALID")
        digest = hashlib.sha256()
        size = 0
        while True:
            _remaining(deadline)
            chunk = os.read(descriptor, min(_CHUNK, spec.maximum_bytes + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > spec.maximum_bytes:
                raise TransferFailure("VAST_TRANSFER_FILE_TOO_LARGE")
            digest.update(chunk)
            if target is not None:
                view = memoryview(chunk)
                while view:
                    _remaining(deadline)
                    written = os.write(target, view)
                    view = view[written:]
        after = directory.validated_regular_child(path.name, descriptor=descriptor)
        if size != before.st_size or any(
            getattr(before, field) != getattr(after, field)
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise TransferFailure("VAST_TRANSFER_FILE_CHANGED")
        receipt = TransferReceipt(size, "sha256:" + digest.hexdigest())
        if spec.sha256 is not None and receipt.sha256 != spec.sha256:
            raise TransferFailure("VAST_TRANSFER_HASH_MISMATCH")
        return receipt
    finally:
        if descriptor is not None:
            os.close(descriptor)
        directory.close()


def _private_bytes(path: Path, maximum: int, deadline: float) -> bytes:
    with tempfile.TemporaryFile() as snapshot:
        _copy_verified(
            path,
            FileSpec("", "send", maximum),
            deadline,
            snapshot.fileno(),
            private=True,
        )
        snapshot.seek(0)
        return snapshot.read(maximum + 1)


def _validate_gate(gate: BrokerGate) -> None:
    if (
        type(gate.instance_id) is not int
        or not 0 < gate.instance_id < 2**63
        or not isinstance(gate.host, str)
        or _HOST.fullmatch(gate.host) is None
        or ".." in gate.host
        or type(gate.port) is not int
        or not 1 <= gate.port <= 65535
    ):
        raise TransferConfigurationFailure("VAST_TRANSFER_BROKER_INVALID")
    if (
        gate.broker_contract_verified is not True
        or gate.remote_uid != 2000
        or type(gate.remote_uid) is not int
        or gate.remote_gid != 0
        or type(gate.remote_gid) is not int
        or not gate.broker_contract_source
        or len(gate.broker_contract_source) > 1024
        or gate.trusted_key_source
        not in {"PROVIDER_AUTHENTICATED_CHANNEL", "OPERATOR_OUT_OF_BAND"}
        or any(
            not isinstance(value, str) or _DIGEST.fullmatch(value) is None
            for value in (
                gate.broker_contract_evidence_sha256,
                gate.trusted_key_evidence_sha256,
                gate.known_hosts_sha256,
                gate.host_key_sha256,
            )
        )
        or gate.broker_contract_evidence_sha256 == gate.trusted_key_evidence_sha256
    ):
        raise TransferConfigurationFailure("VAST_TRANSFER_UNVERIFIED_BROKER_GATE")


def _known_hosts(gate: BrokerGate, content: bytes) -> None:
    """One exact ED25519 key, no wildcard, CA, hashed name or extra host."""
    if _digest(content) != gate.known_hosts_sha256:
        raise TransferConfigurationFailure("VAST_TRANSFER_HOST_KEY_MISMATCH")
    host = gate.host if gate.port == 22 else f"[{gate.host}]:{gate.port}"
    try:
        parts = content.decode("ascii").removesuffix("\n").split(" ")
        if len(parts) != 3 or parts[:2] != [host, "ssh-ed25519"]:
            raise ValueError
        key = base64.b64decode(parts[2], validate=True)
        expected_prefix = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
        if len(key) != len(expected_prefix) + 32 or not key.startswith(expected_prefix):
            raise ValueError
        if _digest(key) != gate.host_key_sha256:
            raise ValueError
    except (ValueError, UnicodeError) as error:
        raise TransferConfigurationFailure("VAST_TRANSFER_HOST_KEY_MISMATCH") from error


def _publish(
    data: Path,
    local: Path,
    spec: FileSpec,
    deadline: float,
) -> None:
    """Publish complete verified bytes atomically without replacing any name."""
    destination = SafeDirFD.open(local.parent)
    name = ".vast-transfer-" + secrets.token_hex(16)
    fd: int | None = None
    linked = False
    try:
        fd = destination.open_child(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        _copy_verified(data, spec, deadline, fd)
        os.fsync(fd)
        destination.validated_regular_child(name, descriptor=fd)
        _remaining(deadline)
        # linkat is an atomic no-replace publication. Remove the temporary
        # link before returning, restoring the private single-link invariant.
        os.link(
            name,
            local.name,
            src_dir_fd=destination.fd,
            dst_dir_fd=destination.fd,
            follow_symlinks=False,
        )
        linked = True
        os.unlink(name, dir_fd=destination.fd)
        destination.validated_regular_child(local.name, descriptor=fd)
        destination.fsync()
    except BaseException:
        if fd is not None:
            with suppress(OSError):
                current = destination.stat_child(name)
                opened = os.fstat(fd)
                if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                    os.unlink(name, dir_fd=destination.fd)
            if linked:
                with suppress(OSError):
                    destination.unlink_child(local.name, expected=os.fstat(fd))
        raise
    finally:
        if fd is not None:
            os.close(fd)
        destination.close()


def _write_staged(directory: SafeDirFD, name: str, content: bytes) -> Path:
    descriptor = directory.open_child(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return directory.path / name


def _ssh_argv(gate: BrokerGate, stage: Path, seconds: float) -> tuple[str, ...]:
    options = (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        f"UserKnownHostsFile={stage / 'known_hosts'}",
        "GlobalKnownHostsFile=/dev/null",
        "HostKeyAlgorithms=ssh-ed25519",
        "UpdateHostKeys=no",
        "VerifyHostKeyDNS=no",
        "CheckHostIP=no",
        "CanonicalizeHostname=no",
        "KnownHostsCommand=none",
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
        "PreferredAuthentications=publickey",
        "ControlMaster=no",
        "ControlPath=none",
        "ControlPersist=no",
        "LogLevel=ERROR",
        f"ConnectTimeout={max(1, min(20, math.ceil(seconds)))}",
        "ServerAliveInterval=5",
        "ServerAliveCountMax=1",
    )
    return (
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        *(part for option in options for part in ("-o", option)),
        "-p",
        str(gate.port),
        "-i",
        str(stage / "identity"),
    )


def _run_bounded(command: TransferCommand, seconds: float) -> None:
    """Bound output, file growth and wall time; kill/reap the owned group."""
    deadline = _deadline(seconds)
    # An isolated Python exec shim avoids preexec_fn in a threaded operator.
    shim = (
        "import os,resource,sys; n=int(sys.argv[1]); "
        "resource.setrlimit(resource.RLIMIT_FSIZE,(n,n)); "
        "os.execv(sys.argv[2],sys.argv[2:])"
    )
    child = subprocess.Popen(
        (sys.executable, "-I", "-c", shim, str(command.maximum_bytes), *command.argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=dict(command.environment),
        cwd=command.directory,
        start_new_session=True,
    )
    try:
        assert child.stdout is not None
        os.set_blocking(child.stdout.fileno(), False)
        total = 0
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map():
                for key, _ in selector.select(min(0.1, _remaining(deadline))):
                    chunk = os.read(key.fd, 4096)
                    total += len(chunk)
                    if total > _OUTPUT_LIMIT:
                        raise TransferFailure("VAST_TRANSFER_OUTPUT_LIMIT")
                    if not chunk:
                        selector.unregister(key.fileobj)
            if child.wait(timeout=_remaining(deadline)) != 0:
                raise TransferFailure("VAST_TRANSFER_PROCESS_FAILED")
    except subprocess.TimeoutExpired as error:
        raise TransferFailure("VAST_TRANSFER_DEADLINE") from error
    finally:
        old = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGALRM}
        )
        try:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)
            if child.stdout is not None:
                child.stdout.close()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old)


class BrokerTransfer:
    def __init__(
        self,
        gate: BrokerGate,
        inventory: Sequence[FileSpec] | None = None,
        *,
        runner: TransferRunner | None = None,
    ) -> None:
        _validate_gate(gate)
        allowed = {item.remote_relative: item for item in file_inventory()}
        chosen = tuple(allowed.values()) if inventory is None else tuple(inventory)
        self._inventory: dict[str, FileSpec] = {}
        for item in chosen:
            original = allowed.get(item.remote_relative)
            if (
                original is None
                or item.remote_relative in self._inventory
                or item.direction != original.direction
                or type(item.maximum_bytes) is not int
                or not 0 < item.maximum_bytes <= original.maximum_bytes
                or (
                    item.size_bytes is not None
                    and (
                        type(item.size_bytes) is not int
                        or not 0 <= item.size_bytes <= item.maximum_bytes
                    )
                )
                or (item.sha256 is not None and _DIGEST.fullmatch(item.sha256) is None)
                or (original.sha256 is not None and item != original)
            ):
                raise TransferConfigurationFailure("VAST_TRANSFER_INVENTORY_INVALID")
            self._inventory[item.remote_relative] = item
        self.gate = gate
        self._runner = _run_bounded if runner is None else runner

    @property
    def instance_id(self) -> int:
        return self.gate.instance_id

    def send(
        self, local: Path, remote_relative: str, seconds: float
    ) -> TransferReceipt:
        return self._transfer(local, remote_relative, "send", seconds)

    def receive(
        self,
        remote_relative: str,
        local: Path,
        seconds: float,
    ) -> TransferReceipt:
        return self._transfer(local, remote_relative, "receive", seconds)

    def _transfer(
        self,
        local: Path,
        remote: str,
        direction: Literal["send", "receive"],
        seconds: float,
    ) -> TransferReceipt:
        deadline = _deadline(seconds)
        spec = self._inventory.get(remote)
        if spec is None or spec.direction != direction:
            raise TransferConfigurationFailure("VAST_TRANSFER_PATH_FORBIDDEN")
        if spec.direction == "receive":
            parent: SafeDirFD | None = None
            try:
                parent = SafeDirFD.open(local.parent)
                try:
                    parent.stat_child(local.name)
                except FileNotFoundError:
                    pass
                else:
                    raise TransferConfigurationFailure(
                        "VAST_TRANSFER_DESTINATION_EXISTS"
                    )
            except OSError as error:
                raise TransferConfigurationFailure(
                    "VAST_TRANSFER_DESTINATION_INVALID"
                ) from error
            finally:
                if parent is not None:
                    parent.close()
        try:
            return self._perform(local, spec, deadline)
        except OSError as error:
            raise TransferFailure("VAST_TRANSFER_LOCAL_IO_FAILED") from error

    def _perform(self, local: Path, spec: FileSpec, deadline: float) -> TransferReceipt:
        try:
            identity = _private_bytes(self.gate.identity, 65_536, deadline)
            if not identity:
                raise TransferConfigurationFailure("VAST_TRANSFER_IDENTITY_INVALID")
            known_hosts = _private_bytes(self.gate.known_hosts, 4096, deadline)
            _known_hosts(self.gate, known_hosts)
        except TransferConfigurationFailure:
            raise
        except (OSError, TransferFailure) as error:
            raise TransferConfigurationFailure(
                "VAST_TRANSFER_KEY_CONFIGURATION"
            ) from error
        temporary_root = "/private/tmp" if Path("/private/tmp").is_dir() else "/tmp"
        with tempfile.TemporaryDirectory(
            prefix="inferdrome-vast-transfer-", dir=temporary_root
        ) as name:
            stage = Path(name)
            directory = SafeDirFD.open(stage)
            try:
                _write_staged(directory, "identity", identity)
                _write_staged(directory, "known_hosts", known_hosts)
                data = stage / "data"
                sent: TransferReceipt | None = None
                if spec.direction == "send":
                    fd = directory.open_child(
                        "data", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    )
                    try:
                        sent = _copy_verified(local, spec, deadline, fd)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                remote = (
                    f"vastai_kaalia@{self.gate.host}::{self.gate.instance_id}/"
                    f"{_ROOT}/{spec.remote_relative}"
                )
                endpoints = (
                    (str(data), remote)
                    if spec.direction == "send"
                    else (remote, str(data))
                )
                argv = (
                    "/usr/bin/rsync",
                    "--no-recursive",
                    "--no-links",
                    "--no-specials",
                    "--no-devices",
                    "--no-owner",
                    "--no-group",
                    "--no-perms",
                    "--ignore-times",
                    "--checksum",
                    *(("--inplace",) if spec.direction == "receive" else ()),
                    f"--max-size={spec.maximum_bytes}",
                    "-e",
                    shlex.join(_ssh_argv(self.gate, stage, _remaining(deadline))),
                    "--",
                    *endpoints,
                )
                environment = {
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(stage),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "TMPDIR": str(stage),
                }
                self._runner(
                    TransferCommand(
                        argv,
                        environment,
                        stage,
                        data,
                        spec.maximum_bytes,
                        spec.direction,
                    ),
                    _remaining(deadline),
                )
                _remaining(deadline)
                if set(os.listdir(directory.fd)) != {"identity", "known_hosts", "data"}:
                    raise TransferFailure("VAST_TRANSFER_RESULT_INVENTORY_INVALID")
                receipt = _copy_verified(data, spec, deadline)
                if sent is not None:
                    if receipt != sent:
                        raise TransferFailure("VAST_TRANSFER_FILE_CHANGED")
                    return receipt
                _publish(
                    data,
                    local,
                    replace(spec, size_bytes=receipt.size_bytes, sha256=receipt.sha256),
                    deadline,
                )
                return receipt
            finally:
                directory.close()
