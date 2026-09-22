#!/usr/bin/env python3
"""Fail-closed, network-isolated smoke for a built Vast engine image."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

_IMAGE = re.compile(
    r"^(?:ghcr[.]io/jayeshsuyal/inferdrome-private-engine@)?"
    r"sha256:[0-9a-f]{64}$"
)
_PYTHON = "/opt/inferdrome-runtime/bin/python"
_STARTUP_MODULE = "inferdrome.evaluation.vast_ssh_startup"
_PORT = "2222"


class StartupImageSmokeError(RuntimeError):
    """The local image did not satisfy the exact startup-ready contract."""


Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _local_run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, check=False, text=True)


def _checked(run: Run, command: Sequence[str]) -> str:
    try:
        result = run(tuple(command))
    except OSError:
        raise StartupImageSmokeError(
            "required local Docker command is unavailable"
        ) from None
    if (
        result.returncode != 0
        or len(result.stdout) > 1_048_576
        or len(result.stderr) > 65_536
    ):
        raise StartupImageSmokeError("startup image smoke command failed")
    return result.stdout.strip()


def verify_image(
    *,
    image: str,
    authorized_keys_file: Path,
    ssh_private_key_file: Path,
    source_commit: str,
    run: Run = _local_run,
    wait: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Verify one already-built local image without pull or external networking."""

    if (
        _IMAGE.fullmatch(image) is None
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
    ):
        raise StartupImageSmokeError("startup image identity is invalid")
    if not authorized_keys_file.is_absolute() or not authorized_keys_file.is_file():
        raise StartupImageSmokeError("public authorized-key fixture is unavailable")
    if not ssh_private_key_file.is_absolute() or not ssh_private_key_file.is_file():
        raise StartupImageSmokeError("private key fixture is unavailable")
    if ssh_private_key_file.stat().st_mode & 0o077:
        raise StartupImageSmokeError("private key fixture permissions are unsafe")
    inspection = json.loads(_checked(run, ("docker", "image", "inspect", image)))
    if not isinstance(inspection, list) or len(inspection) != 1:
        raise StartupImageSmokeError("startup image inspection is ambiguous")
    config = inspection[0].get("Config", {})
    labels = config.get("Labels", {})
    required = {
        "org.opencontainers.image.revision": source_commit,
        "com.inferdrome.runtime-role": "private-engine",
        "com.inferdrome.vllm-version": "0.26.0",
        "com.inferdrome.vast-startup-profile": "vast-ssh-public-v1",
        "com.inferdrome.vast-ssh-readiness-seconds": "180",
        "com.inferdrome.runtime-package-bootstrap": "forbidden",
        "com.inferdrome.public-pull-contract": (
            "anonymous-public-pull-required-unverified"
        ),
        "com.inferdrome.serving-uid": "2000",
        "com.inferdrome.serving-executable": "/usr/local/bin/vllm",
        "com.inferdrome.vast-startup-command": (f"{_PYTHON} -m {_STARTUP_MODULE}"),
    }
    if config.get("User") != "2000:0" or config.get("Entrypoint") != ["inferdrome"]:
        raise StartupImageSmokeError("startup image default identity is invalid")
    if not isinstance(labels, dict) or any(
        labels.get(key) != value for key, value in required.items()
    ):
        raise StartupImageSmokeError("startup image labels are invalid")

    container = ""
    try:
        container = _checked(
            run,
            (
                "docker",
                "create",
                "--pull",
                "never",
                "--network",
                "none",
                "--user",
                "0:0",
                "--read-only",
                "--tmpfs",
                "/run:rw,noexec,nosuid,size=16m",
                "--mount",
                f"type=bind,source={authorized_keys_file},target=/authorized-key,readonly",
                "--mount",
                f"type=bind,source={ssh_private_key_file},target=/smoke-private-key,readonly",
                "--entrypoint",
                _PYTHON,
                image,
                "-m",
                _STARTUP_MODULE,
                "--authorized-keys-file",
                "/authorized-key",
                "--port",
                _PORT,
            ),
        )
        if re.fullmatch(r"[0-9a-f]{12,64}", container) is None:
            raise StartupImageSmokeError("startup container identity is invalid")
        _checked(run, ("docker", "start", container))
        probe = (
            "import socket; s=socket.create_connection(('127.0.0.1',2222),1); "
            "assert s.recv(4)==b'SSH-'; s.close()"
        )
        deadline = time.monotonic() + 180
        while True:
            result = run(("docker", "exec", container, _PYTHON, "-c", probe))
            if result.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise StartupImageSmokeError("SSH readiness exceeded 180 seconds")
            wait(0.25)
        key_probe = (
            "from pathlib import Path; import glob; "
            "assert Path('/run/inferdrome-sshd/ssh_host_ed25519_key').is_file(); "
            "assert not glob.glob('/etc/ssh/ssh_host_*'); "
            "c=Path('/run/inferdrome-sshd/sshd_config').read_text(); "
            "assert 'PermitRootLogin prohibit-password\\n' in c; "
            "assert 'PasswordAuthentication no\\n' in c; "
            "assert 'KbdInteractiveAuthentication no\\n' in c"
        )
        _checked(run, ("docker", "exec", container, _PYTHON, "-c", key_probe))
        ssh_base = (
            "docker",
            "exec",
            container,
            "/usr/bin/ssh",
            "-p",
            _PORT,
            "-o",
            "BatchMode=yes",
            "-o",
            "UserKnownHostsFile=/run/inferdrome-sshd/known_hosts",
            "-o",
            "IdentitiesOnly=yes",
        )
        remote_uid = _checked(
            run,
            (
                *ssh_base,
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "PasswordAuthentication=no",
                "-i",
                "/smoke-private-key",
                "root@127.0.0.1",
                "/usr/bin/id",
                "-u",
            ),
        )
        if remote_uid != "0":
            raise StartupImageSmokeError("public-key root control login failed")
        known_host_probe = (
            "from pathlib import Path; p=Path('/run/inferdrome-sshd/known_hosts'); "
            "assert p.is_file() and p.stat().st_size > 0"
        )
        _checked(run, ("docker", "exec", container, _PYTHON, "-c", known_host_probe))
        _checked(
            run,
            (
                "docker",
                "exec",
                container,
                "/usr/bin/ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                "/run/inferdrome-sshd/wrong-key",
            ),
        )
        wrong_key = run(
            (
                *ssh_base,
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "PasswordAuthentication=no",
                "-i",
                "/run/inferdrome-sshd/wrong-key",
                "root@127.0.0.1",
                "/usr/bin/true",
            )
        )
        password_only = run(
            (
                *ssh_base,
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
                "root@127.0.0.1",
                "/usr/bin/true",
            )
        )
        if wrong_key.returncode == 0 or password_only.returncode == 0:
            raise StartupImageSmokeError("SSH authentication fail-closed check failed")
        uid = _checked(
            run,
            (
                "docker",
                "exec",
                "--user",
                "0:0",
                container,
                "/usr/local/bin/inferdrome-run-as-serving-user",
                "/usr/bin/id",
                "-u",
            ),
        )
        if uid != "2000":
            raise StartupImageSmokeError("serving command did not drop to UID 2000")
    finally:
        if container:
            result = run(("docker", "rm", "--force", container))
            if result.returncode != 0:
                raise StartupImageSmokeError("startup smoke cleanup is unconfirmed")
    return {
        "schema_version": "inferdrome.vast-startup-image-smoke.v1",
        "image": image,
        "source_commit": source_commit,
        "network": "NONE",
        "runtime_package_install": False,
        "ephemeral_host_keys": True,
        "root_public_key_login": True,
        "password_login": False,
        "sshd_ready_within_seconds": 180,
        "serving_uid": 2000,
        "verified": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_vast_startup_image")
    parser.add_argument("--image", required=True)
    parser.add_argument("--authorized-keys-file", required=True, type=Path)
    parser.add_argument("--ssh-private-key-file", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = verify_image(
            image=arguments.image,
            authorized_keys_file=arguments.authorized_keys_file,
            ssh_private_key_file=arguments.ssh_private_key_file,
            source_commit=arguments.source_commit,
        )
    except (StartupImageSmokeError, json.JSONDecodeError) as error:
        print(f"startup image smoke rejected: {error}")
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
