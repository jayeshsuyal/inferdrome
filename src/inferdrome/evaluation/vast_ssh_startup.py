"""Minimal network-free SSH startup command for the Vast engine container."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
from pathlib import Path
from typing import NoReturn

_STATE_ROOT = Path("/run/inferdrome-sshd")
_PRIVSEP_ROOT = Path("/run/sshd")
_MAX_PUBLIC_KEY_BYTES = 16_384


class VastSshStartupError(RuntimeError):
    """The local startup contract cannot safely start sshd."""


def _fail(message: str) -> NoReturn:
    raise VastSshStartupError(message)


def _public_key(path: Path) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail("authorized key input is unsafe")
        content = path.read_bytes()
    except OSError:
        _fail("authorized key input is unavailable")
    if (
        not content
        or len(content) > _MAX_PUBLIC_KEY_BYTES
        or b"\x00" in content
        or any(
            not line.startswith((b"ssh-ed25519 ", b"sk-ssh-ed25519@openssh.com "))
            for line in content.splitlines()
        )
    ):
        _fail("authorized key input is invalid")
    return content.rstrip(b"\n") + b"\n"


def _checked(command: tuple[str, ...]) -> None:
    try:
        completed = subprocess.run(command, check=False, capture_output=True)
    except OSError:
        _fail("required SSH startup executable is unavailable")
    if (
        completed.returncode != 0
        or len(completed.stdout) > 65_536
        or len(completed.stderr) > 65_536
    ):
        _fail("SSH startup validation failed")


def prepare_startup(*, authorized_keys_file: Path, port: int) -> tuple[str, ...]:
    """Create ephemeral host state and return the final no-shell sshd argv."""

    if os.geteuid() != 0 or not 1 <= port <= 65_535:
        _fail("SSH startup identity or port is invalid")
    content = _public_key(authorized_keys_file)
    try:
        _PRIVSEP_ROOT.mkdir(mode=0o755, exist_ok=True)
        privsep = _PRIVSEP_ROOT.lstat()
        if not stat.S_ISDIR(privsep.st_mode) or privsep.st_uid != 0:
            _fail("SSH privilege-separation state is unsafe")
        _PRIVSEP_ROOT.chmod(0o755)
        _STATE_ROOT.mkdir(mode=0o700)
        host_key = _STATE_ROOT / "ssh_host_ed25519_key"
        authorized_keys = _STATE_ROOT / "authorized_keys"
        config = _STATE_ROOT / "sshd_config"
        authorized_keys.write_bytes(content)
        authorized_keys.chmod(0o600)
        config.write_text(
            "\n".join(
                (
                    f"Port {port}",
                    "ListenAddress 0.0.0.0",
                    f"HostKey {host_key}",
                    f"AuthorizedKeysFile {authorized_keys}",
                    "PasswordAuthentication no",
                    "KbdInteractiveAuthentication no",
                    "PermitRootLogin prohibit-password",
                    "PubkeyAuthentication yes",
                    "UsePAM no",
                    "AllowTcpForwarding no",
                    "X11Forwarding no",
                    "PermitTunnel no",
                    "PidFile /run/inferdrome-sshd/sshd.pid",
                    "Subsystem sftp internal-sftp",
                    "",
                )
            ),
            encoding="utf-8",
        )
        config.chmod(0o600)
    except FileExistsError:
        _fail("SSH startup state already exists")
    except OSError:
        _fail("SSH startup state cannot be created")
    _checked(
        ("/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key))
    )
    _checked(("/usr/sbin/sshd", "-t", "-f", str(config)))
    return ("/usr/sbin/sshd", "-D", "-e", "-f", str(config))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inferdrome-vast-ssh-startup")
    parser.add_argument("--authorized-keys-file", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    arguments = parser.parse_args(argv)
    try:
        command = prepare_startup(
            authorized_keys_file=arguments.authorized_keys_file,
            port=arguments.port,
        )
        os.execv(command[0], command)
    except VastSshStartupError as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
