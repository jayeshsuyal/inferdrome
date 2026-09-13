"""Pinned stock SFTP for the owned Vast guest's untrusted transfer directories.

The caller supplies an authenticated exact-instance TCP2222 mapping and a pin
already enrolled through SshPinJournal. No broker, provider discovery, host-key
scan, shell command or model transfer exists here. Successful sends report the
local snapshot's size/hash, not guest admission or remote durability. The guest
must validate staged controls and publish its private records without replacement.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
import tempfile
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.deployment.vast_ssh_trust import HostKeyPin, SshPinJournal
from inferdrome.deployment.vast_transfer import (
    FileSpec,
    TransferCommand,
    TransferConfigurationFailure,
    TransferFailure,
    TransferReceipt,
    TransferRunner,
    _copy_verified,
    _deadline,
    _private_bytes,
    _publish,
    _remaining,
    _run_bounded,
    _write_staged,
)

__all__ = [
    "FileSpec",
    "SftpGate",
    "SftpTransfer",
    "TransferCommand",
    "TransferConfigurationFailure",
    "TransferFailure",
    "TransferReceipt",
    "TransferRunner",
    "file_inventory",
]

_CONTROL_LIMIT = 131_072
_EXPORT_LIMIT = 8_388_608
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_ACCOUNT = "inferdrome-transfer"


@dataclass(frozen=True)
class SftpGate:
    """Endpoint from authenticated exact-ID readback; identity remains local.

    This value alone does not assert the endpoint's provenance. The controller
    must bind host/port to its retained instance's sole TCP2222 mapping. The
    separate journal supplies the exact ID/nonce host key; SSH proves possession.
    """

    instance_id: int
    run_nonce: str
    host: str
    port: int
    identity: Path


def file_inventory() -> tuple[FileSpec, ...]:
    """Only three control uploads and seven control/evidence downloads."""
    return (
        *(
            FileSpec(f"inbox/{name}.json", "send", _CONTROL_LIMIT)
            for name in ("intent", "stage", "approval")
        ),
        *(
            FileSpec(f"outbox/{name}.json", "receive", _CONTROL_LIMIT)
            for name in ("ready", "plan", "result")
        ),
        *(
            FileSpec(f"outbox/export/{name}", "receive", _EXPORT_LIMIT)
            for name in (
                "executed-manifest.json",
                "input-transfer-receipt.json",
                "producer-receipt.json",
                "integrity-manifest.json",
            )
        ),
    )


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _validate_gate(gate: SftpGate) -> None:
    try:
        address = ipaddress.IPv4Address(gate.host)
        valid = (
            type(gate.instance_id) is int
            and 0 < gate.instance_id <= 9_007_199_254_740_991
            and isinstance(gate.run_nonce, str)
            and _NONCE.fullmatch(gate.run_nonce) is not None
            and isinstance(gate.host, str)
            and str(address) == gate.host
            and address.is_global
            and not address.is_multicast
            and not address.is_reserved
            and type(gate.port) is int
            and 1 <= gate.port <= 65535
            and isinstance(gate.identity, Path)
            and gate.identity.is_absolute()
        )
    except Exception:
        valid = False
    if not valid:
        raise TransferConfigurationFailure("VAST_SFTP_GATE_INVALID")


def _sftp_argv(gate: SftpGate, stage: Path, seconds: float) -> tuple[str, ...]:
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
        "/usr/bin/sftp",
        "-q",
        "-4",
        "-F",
        "/dev/null",
        "-S",
        "/usr/bin/ssh",
        "-b",
        str(stage / "batch"),
        "-R",
        "1",
        *(part for option in options for part in ("-o", option)),
        "-P",
        str(gate.port),
        "-i",
        str(stage / "identity"),
        f"{_ACCOUNT}@{gate.host}",
    )


def _batch(spec: FileSpec) -> bytes:
    # Every name comes from file_inventory, never user-controlled batch syntax.
    # rename may replace an old staged upload. Guest private admission, not this
    # marker, is the canonical no-replace boundary. The batch stops on failure.
    if spec.direction == "send":
        remote = "/uploads/" + spec.remote_relative.removeprefix("inbox/")
        return (
            f'put -f "data" "{remote}.pending"\nrename "{remote}.pending" "{remote}"\n'
        ).encode("ascii")
    remote = "/downloads/" + spec.remote_relative.removeprefix("outbox/")
    return f'get "{remote}" "data"\n'.encode("ascii")


class SftpTransfer:
    """One stock batch per call; failures never publish partial local records.

    The default runner is the shared bounded process primitive, which limits
    child output, wall time and RLIMIT_FSIZE, then kills/reaps its owned process
    group. Tests inject a fake runner and never invoke SSH. Journal lifetime is
    owned by the caller and must cover every transfer.
    """

    def __init__(
        self,
        gate: SftpGate,
        pins: SshPinJournal,
        inventory: Sequence[FileSpec] | None = None,
        *,
        runner: TransferRunner | None = None,
    ) -> None:
        _validate_gate(gate)
        self._gate, self._pins = gate, pins
        self._runner = _run_bounded if runner is None else runner
        self._inventory: dict[str, FileSpec] = {}
        allowed = {item.remote_relative: item for item in file_inventory()}
        try:
            for item in allowed.values() if inventory is None else inventory:
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
                    or (
                        item.sha256 is not None
                        and (
                            not isinstance(item.sha256, str)
                            or _DIGEST.fullmatch(item.sha256) is None
                        )
                    )
                ):
                    raise ValueError
                self._inventory[item.remote_relative] = item
        except Exception:
            raise TransferConfigurationFailure("VAST_SFTP_INVENTORY_INVALID") from None
        self._pin = self._read_pin()
        identity = self._identity(_deadline(5))
        self._identity_sha256 = _digest(identity)

    @property
    def gate(self) -> SftpGate:
        return self._gate

    @property
    def instance_id(self) -> int:
        return self.gate.instance_id

    @property
    def run_nonce(self) -> str:
        return self.gate.run_nonce

    @property
    def host_key_sha256(self) -> str:
        return self._pin.announcement.host_key_sha256

    def _read_pin(self) -> HostKeyPin:
        try:
            pin = self._pins.read(
                instance_id=self.instance_id, run_nonce=self.run_nonce
            )
            if (
                pin is None
                or pin.announcement.instance_id != self.instance_id
                or pin.announcement.run_nonce != self.run_nonce
            ):
                raise ValueError
            return pin
        except Exception:
            raise TransferConfigurationFailure("VAST_SFTP_PIN_INVALID") from None

    def _identity(self, deadline: float) -> bytes:
        try:
            identity = _private_bytes(self.gate.identity, 65_536, deadline)
            if not identity:
                raise ValueError
            return identity
        except Exception:
            raise TransferConfigurationFailure("VAST_SFTP_IDENTITY_INVALID") from None

    def _keys(self, deadline: float) -> tuple[bytes, bytes]:
        if self._read_pin() != self._pin:
            raise TransferConfigurationFailure("VAST_SFTP_PIN_CHANGED")
        identity = self._identity(deadline)
        if _digest(identity) != self._identity_sha256:
            raise TransferConfigurationFailure("VAST_SFTP_IDENTITY_CHANGED")
        host = (
            self.gate.host
            if self.gate.port == 22
            else (f"[{self.gate.host}]:{self.gate.port}")
        )
        known_hosts = f"{host} {self._pin.announcement.host_public_key}\n".encode(
            "ascii"
        )
        return identity, known_hosts

    def send(
        self, local: Path, remote_relative: str, seconds: float
    ) -> TransferReceipt:
        """Local snapshot receipt only; the guest still must admit this upload."""
        return self._transfer(local, remote_relative, "send", seconds)

    def receive(
        self, remote_relative: str, local: Path, seconds: float
    ) -> TransferReceipt:
        """Received bytes remain untrusted until schema/identity/evidence checks."""
        return self._transfer(local, remote_relative, "receive", seconds)

    def _transfer(
        self,
        local: Path,
        remote: str,
        direction: Literal["send", "receive"],
        seconds: float,
    ) -> TransferReceipt:
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or not 0 < seconds <= 3600
        ):
            raise TransferConfigurationFailure("VAST_SFTP_BUDGET_INVALID")
        deadline = _deadline(seconds)
        spec = self._inventory.get(remote) if isinstance(remote, str) else None
        if spec is None or spec.direction != direction:
            raise TransferConfigurationFailure("VAST_SFTP_PATH_FORBIDDEN")
        if not isinstance(local, Path) or not local.is_absolute():
            raise TransferConfigurationFailure("VAST_SFTP_LOCAL_PATH_INVALID")
        if direction == "receive":
            try:
                with closing(SafeDirFD.open(local.parent)) as parent:
                    try:
                        parent.stat_child(local.name)
                    except FileNotFoundError:
                        pass
                    else:
                        raise ValueError
            except Exception:
                raise TransferConfigurationFailure(
                    "VAST_SFTP_DESTINATION_INVALID"
                ) from None
        try:
            return self._perform(local, spec, deadline)
        except TransferConfigurationFailure:
            raise
        except Exception:
            raise TransferFailure("VAST_SFTP_TRANSFER_FAILED") from None

    def _perform(self, local: Path, spec: FileSpec, deadline: float) -> TransferReceipt:
        identity, known_hosts = self._keys(deadline)
        temporary_root = "/private/tmp" if Path("/private/tmp").is_dir() else "/tmp"
        with tempfile.TemporaryDirectory(
            prefix="inferdrome-vast-sftp-", dir=temporary_root
        ) as name:
            stage = Path(name)
            with closing(SafeDirFD.open(stage)) as directory:
                fixed = {
                    "identity": identity,
                    "known_hosts": known_hosts,
                    "batch": _batch(spec),
                }
                for filename, content in fixed.items():
                    _write_staged(directory, filename, content)
                data, sent = stage / "data", None
                if spec.direction == "send":
                    descriptor = directory.open_child(
                        "data", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    )
                    try:
                        sent = _copy_verified(local, spec, deadline, descriptor)
                        # Stock put derives the remote mode from this snapshot.
                        # Public controls need group-read for bootstrap2000 to
                        # admit transfer2001's upload under server umask0027.
                        # Its containing local directory remains private0700.
                        os.fchmod(descriptor, 0o640)
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                environment = {
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(stage),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "TMPDIR": str(stage),
                }
                try:
                    self._runner(
                        TransferCommand(
                            _sftp_argv(self.gate, stage, _remaining(deadline)),
                            environment,
                            stage,
                            data,
                            spec.maximum_bytes,
                            spec.direction,
                        ),
                        _remaining(deadline),
                    )
                except Exception:
                    raise TransferFailure("VAST_SFTP_PROCESS_FAILED") from None
                _remaining(deadline)
                self._keys(deadline)
                if set(os.listdir(directory.fd)) != {*fixed, "data"}:
                    raise TransferFailure("VAST_SFTP_RESULT_INVENTORY_INVALID")
                for filename, content in fixed.items():
                    if (
                        _private_bytes(stage / filename, len(content), deadline)
                        != content
                    ):
                        raise TransferFailure("VAST_SFTP_CONFIGURATION_CHANGED")
                receipt = _copy_verified(data, spec, deadline)
                if sent is not None:
                    if receipt != sent:
                        raise TransferFailure("VAST_SFTP_UPLOAD_CHANGED")
                    return receipt
                _publish(
                    data,
                    local,
                    replace(spec, size_bytes=receipt.size_bytes, sha256=receipt.sha256),
                    deadline,
                )
                return receipt
