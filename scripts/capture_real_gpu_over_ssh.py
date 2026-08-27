#!/usr/bin/env python3
"""Run the pinned Inferdrome proof pack on one operator-provided SSH host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from inferdrome.qwen3_gpu_tiers import (
    QWEN3_A10_GPU_TIER_ID,
    QWEN3_ARCHIVE_TRANSFER_SECONDS,
    QWEN3_IMPLEMENTED_GPU_TIERS,
    QWEN3_MAX_ARCHIVE_BYTES,
    QWEN3_METADATA_TRANSFER_SECONDS,
    QWEN3_PHASE_BUDGET_SECONDS,
    QWEN3_POST_REMOTE_BUDGET_SECONDS,
    QWEN3_REMOTE_CAPTURE_SECONDS,
    QWEN3_STARTUP_TIMEOUT_SECONDS,
    QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS,
    Qwen3GpuTierPolicy,
    qwen3_gpu_tier_policy,
)

if __package__:
    from scripts import lambda_gpu_guard, qwen3_gpu_capture, real_gpu_capture
else:
    import lambda_gpu_guard
    import qwen3_gpu_capture
    import real_gpu_capture

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_DESTINATION_PATTERN = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_.-]*@)?(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:]+\])\Z"
)
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_INSTANCE_TYPE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DEFAULT_REMOTE_TIMEOUT_SECONDS = 9_900
_MINIMUM_PROFILE_REMOTE_SECONDS = 300
_LEGACY_MINIMUM_REMOTE_SECONDS = 1_800
_QWEN3_PROFILE_ID = "managed-vllm-0.26-qwen3-8b-bf16-v1"
_QWEN3_A10_POLICY = qwen3_gpu_tier_policy(QWEN3_A10_GPU_TIER_ID)
_QWEN3_A10_HOURLY_RATE_USD = _QWEN3_A10_POLICY.hourly_rate_usd
_QWEN3_A10_MAX_COST_USD = _QWEN3_A10_POLICY.max_session_cost_usd
_QWEN3_EXPECTED_GPU_MODEL = _QWEN3_A10_POLICY.expected_nvidia_smi_name
_QWEN3_EXPECTED_INSTANCE_TYPE = "gpu_1x_a10"
_QWEN3_STARTUP_TIMEOUT_SECONDS = QWEN3_STARTUP_TIMEOUT_SECONDS
_QWEN3_REMOTE_CAPTURE_SECONDS = QWEN3_REMOTE_CAPTURE_SECONDS
_QWEN3_POST_REMOTE_BUDGET_SECONDS = QWEN3_POST_REMOTE_BUDGET_SECONDS
_QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS = (
    QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS
)
_QWEN3_METADATA_TRANSFER_SECONDS = QWEN3_METADATA_TRANSFER_SECONDS
_QWEN3_ARCHIVE_TRANSFER_SECONDS = QWEN3_ARCHIVE_TRANSFER_SECONDS
_QWEN3_MAX_ARCHIVE_BYTES = QWEN3_MAX_ARCHIVE_BYTES
_MAX_SOURCE_ARCHIVE_BYTES = 134_217_728
_QWEN3_PHASE_BUDGET_SECONDS = dict(QWEN3_PHASE_BUDGET_SECONDS)


class RemoteCaptureError(RuntimeError):
    """Expected, user-facing remote capture failure."""


def _run(
    arguments: Sequence[str],
    *,
    label: str,
    capture_output: bool = False,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RemoteCaptureError(f"{label} could not complete") from None
    if check and completed.returncode != 0:
        detail = ""
        if capture_output and completed.stderr:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            if len(detail) > 500:
                detail = detail[:500] + "…"
        suffix = f": {detail}" if detail else ""
        raise RemoteCaptureError(f"{label} failed{suffix}")
    return completed


def _git(*arguments: str) -> str:
    completed = _run(
        ["git", "-C", str(REPOSITORY_ROOT), *arguments],
        label="local Git inspection",
        capture_output=True,
        timeout=30,
    )
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise RemoteCaptureError("local Git output is not UTF-8") from None


def _create_source_archive(destination: Path, commit: str) -> tuple[str, int]:
    """Export only the exact HEAD tree, refusing links, submodules, and secrets."""

    if destination.exists() or destination.is_symlink():
        raise RemoteCaptureError("exact source archive destination already exists")
    listing = _run(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "ls-tree",
            "-rz",
            "-r",
            "--full-tree",
            commit,
        ],
        label="exact source tree inspection",
        capture_output=True,
        timeout=60,
    ).stdout
    records = listing.split(b"\0")
    if not records or records[-1] != b"":
        raise RemoteCaptureError("exact source tree listing is malformed")
    seen: set[str] = set()
    denied_names = {
        ".env",
        ".inferdrome-source-export.json",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
    denied_directories = {".aws", ".git", ".gnupg", ".ssh"}
    for raw in records[:-1]:
        try:
            metadata, raw_path = raw.split(b"\t", 1)
            mode, kind, _object_id = metadata.decode("ascii").split(" ", 2)
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            raise RemoteCaptureError("exact source tree listing is malformed") from None
        pure = PurePosixPath(path)
        folded_parts = tuple(part.casefold() for part in pure.parts)
        folded_name = pure.name.casefold()
        if (
            mode not in {"100644", "100755"}
            or kind != "blob"
            or pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
            or "\\" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or any(part in denied_directories for part in folded_parts)
            or folded_name in denied_names
            or folded_name.startswith(".env.")
            or PurePosixPath(folded_name).suffix in {".key", ".p12", ".pem", ".pfx"}
            or path in seen
        ):
            raise RemoteCaptureError(
                "exact source tree contains an unsafe or secret-bearing entry"
            )
        seen.add(path)
    if not seen or len(seen) > 100_000:
        raise RemoteCaptureError("exact source tree has an invalid file count")
    _run(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "archive",
            "--format=tar",
            f"--output={destination}",
            commit,
        ],
        label="exact source archive creation",
        timeout=120,
    )
    try:
        metadata = os.lstat(destination)
        if (
            destination.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or not 1 <= metadata.st_size <= _MAX_SOURCE_ARCHIVE_BYTES
        ):
            raise OSError
        archived_files: set[str] = set()
        archived_members: set[str] = set()
        with tarfile.open(destination, mode="r:") as retained:
            members = retained.getmembers()
            if not members or len(members) > 200_000:
                raise RemoteCaptureError("exact source archive has invalid members")
            for member in members:
                pure = PurePosixPath(member.name)
                normalized = str(pure)
                if (
                    pure.is_absolute()
                    or not pure.parts
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or "\\" in member.name
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in member.name
                    )
                    or normalized in archived_members
                    or not (member.isdir() or member.isfile())
                ):
                    raise RemoteCaptureError(
                        "exact source archive contains an unsafe member"
                    )
                archived_members.add(normalized)
                if member.isfile():
                    archived_files.add(normalized)
                    if pure.name == ".gitattributes":
                        stream = retained.extractfile(member)
                        if stream is None:
                            raise RemoteCaptureError(
                                "exact source attributes cannot be inspected"
                            )
                        attributes = stream.read(1_048_577)
                        if (
                            len(attributes) > 1_048_576
                            or b"export-ignore" in attributes
                            or b"export-subst" in attributes
                        ):
                            raise RemoteCaptureError(
                                "exact source attributes alter Git archive bytes"
                            )
        if archived_files != seen:
            raise RemoteCaptureError(
                "exact source archive does not match the committed file set"
            )
        digest = hashlib.sha256()
        denied_markers = (
            b"-----BEGIN " + b"EC PRIVATE KEY-----",
            b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----",
            b"-----BEGIN " + b"PRIVATE KEY-----",
            b"-----BEGIN " + b"RSA PRIVATE KEY-----",
        )
        carry = b""
        longest_marker = max(len(marker) for marker in denied_markers)
        with destination.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1_048_576), b""):
                digest.update(chunk)
                inspected = carry + chunk
                if any(marker in inspected for marker in denied_markers):
                    with suppress(OSError):
                        destination.unlink()
                    raise RemoteCaptureError(
                        "exact source tree contains private-key material"
                    )
                carry = inspected[-(longest_marker - 1) :]
    except (OSError, tarfile.TarError):
        raise RemoteCaptureError("exact source archive is unavailable") from None
    return "sha256:" + digest.hexdigest(), metadata.st_size


def _validate_destination(value: str) -> str:
    if _DESTINATION_PATTERN.fullmatch(value) is None or value.startswith("-"):
        raise argparse.ArgumentTypeError(
            "SSH destination must be a simple user@host, hostname, IPv4, "
            "or bracketed IPv6"
        )
    return value


def _bounded_integer(
    value: str,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError(f"{label} must be an integer")
    selected = int(value)
    if not minimum <= selected <= maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return selected


def _port(value: str) -> int:
    return _bounded_integer(value, label="SSH port", minimum=1, maximum=65_535)


def _gpu_index(value: str) -> int:
    return _bounded_integer(value, label="GPU index", minimum=0, maximum=255)


def _startup_timeout(value: str) -> int:
    return _bounded_integer(
        value,
        label="startup timeout",
        minimum=1,
        maximum=3_600,
    )


def _remote_timeout(value: str) -> int:
    return _bounded_integer(
        value,
        label="remote timeout",
        minimum=_MINIMUM_PROFILE_REMOTE_SECONDS,
        maximum=10_800,
    )


def _lambda_instance_type_name(value: str) -> str:
    if _INSTANCE_TYPE_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "Lambda instance type name must contain only letters, digits, '.', '_', "
            "and '-'"
        )
    return value


def _require_checkout(expected_commit: str | None) -> str:
    if shutil.which("git") is None:
        raise RemoteCaptureError("git is required")
    commit = _git("rev-parse", "--verify", "HEAD")
    if _COMMIT_PATTERN.fullmatch(commit) is None:
        raise RemoteCaptureError("local HEAD is not a full lowercase Git commit")
    if expected_commit is not None:
        if _COMMIT_PATTERN.fullmatch(expected_commit) is None:
            raise RemoteCaptureError(
                "--expected-commit must be 40 lowercase hex digits"
            )
        if expected_commit != commit:
            raise RemoteCaptureError("local HEAD is not --expected-commit")
    if _git("status", "--porcelain", "--untracked-files=normal"):
        raise RemoteCaptureError("the local Inferdrome checkout must be clean")
    return commit


def _require_identity(path_text: str | None) -> Path | None:
    if path_text is None:
        return None
    path = Path(path_text).expanduser().absolute()
    try:
        metadata = os.lstat(path)
    except OSError:
        raise RemoteCaptureError("SSH identity file is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise RemoteCaptureError("SSH identity must be a regular, non-symlink file")
    return path


def _host_key_digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError(
            "host-key SHA-256 must be 64 lowercase hex digits"
        )
    return value


def _ssh_options(
    *,
    identity: Path | None,
    known_hosts: Path,
    port: int,
) -> list[str]:
    options = [
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "SendEnv=-*",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "RequestTTY=no",
        "-p",
        str(port),
    ]
    if identity is not None:
        options.extend(["-o", "IdentitiesOnly=yes", "-i", str(identity)])
    return options


def _scp_options(
    *,
    identity: Path | None,
    known_hosts: Path,
    port: int,
) -> list[str]:
    options = _ssh_options(
        identity=identity,
        known_hosts=known_hosts,
        port=port,
    )
    port_index = options.index("-p")
    options[port_index] = "-P"
    return options


def _bash_command(script: str) -> str:
    return "bash -c " + shlex.quote(script)


def _remote_preflight_script(
    remote_root: str,
    *,
    gpu_index: int = 0,
    expected_gpu_model: str | None = None,
) -> str:
    quoted_root = shlex.quote(remote_root)
    gpu_check = ""
    resource_check = ""
    if expected_gpu_model is not None:
        gpu_check = f"""
gpu_name=$(nvidia-smi --id={gpu_index} --query-gpu=name --format=csv,noheader,nounits)
[[ $gpu_name == {shlex.quote(expected_gpu_model)} ]] || {{
  echo "expected GPU {expected_gpu_model} at index {gpu_index}" >&2
  echo "observed GPU: $gpu_name" >&2
  exit 1
}}
"""
        resource_check = """if shutil.disk_usage("/tmp").free < 40 * 1024**3:
    raise SystemExit("at least 40 GiB free under /tmp is required")
"""
    return f"""set -euo pipefail
umask 077
[[ $(uname -s) == Linux ]]
for executable in bash curl python3.12 nvidia-smi sha256sum tar timeout; do
  command -v "$executable" >/dev/null || {{
    echo "missing required host executable: $executable" >&2
    exit 1
  }}
done
python3.12 - <<'PY'
import ensurepip
from pathlib import Path
import shutil
import sysconfig
import venv

include = sysconfig.get_path("include")
if not include or not (Path(include) / "Python.h").is_file():
    raise SystemExit("missing Python.h; install the Python 3.12 development headers")
{resource_check}
del ensurepip, venv
PY
[[ ! -e {quoted_root} && ! -L {quoted_root} ]]
mkdir -m 700 -- {quoted_root}
nvidia-smi --query-gpu=index,name,uuid,driver_version --format=csv,noheader,nounits
{gpu_check}
"""


def _remote_capture_script(
    remote_root: str,
    commit: str,
    source_archive_sha256: str,
    *,
    gpu_index: int,
    startup_timeout_seconds: int,
    remote_timeout_seconds: int,
    managed_capability_profile: str | None = None,
    qwen3_gpu_tier: str | None = None,
) -> str:
    if managed_capability_profile not in {None, _QWEN3_PROFILE_ID}:
        raise RemoteCaptureError("managed capability profile is unsupported")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", source_archive_sha256) is None:
        raise RemoteCaptureError("source archive digest is invalid")
    if managed_capability_profile is None:
        if qwen3_gpu_tier is not None:
            raise RemoteCaptureError("Qwen3 GPU tier requires the managed profile")
    else:
        try:
            qwen3_gpu_tier_policy(str(qwen3_gpu_tier))
        except ValueError as error:
            raise RemoteCaptureError(str(error)) from None
    root = shlex.quote(remote_root)
    expected = shlex.quote(commit)
    expected_source_sha256 = shlex.quote(source_archive_sha256.removeprefix("sha256:"))
    profile_argument = ""
    if managed_capability_profile is not None:
        profile_argument = " \\\n  --managed-capability-profile " + shlex.quote(
            managed_capability_profile
        )
        profile_argument += (
            " \\\n  --qwen3-gpu-tier " + shlex.quote(str(qwen3_gpu_tier))
        )
    return f"""set -euo pipefail
umask 077
[[ $(sha256sum {root}/repo.tar | cut -d ' ' -f 1) == {expected_source_sha256} ]]
python3.12 - {root}/repo.tar {root}/repo {expected} \
  {shlex.quote(source_archive_sha256)} <<'PY'
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile

archive = Path(sys.argv[1])
destination = Path(sys.argv[2])
repository_commit = sys.argv[3]
source_archive_sha256 = sys.argv[4]
metadata = os.lstat(archive)
if archive.is_symlink() or not stat.S_ISREG(metadata.st_mode):
    raise SystemExit("source archive is not a regular file")
destination.mkdir(mode=0o700)
with tarfile.open(archive, mode="r:") as retained:
    members = retained.getmembers()
    if not members or len(members) > 100_000:
        raise SystemExit("source archive file count is invalid")
    seen = set()
    total_bytes = 0
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {{"", ".", ".."}} for part in path.parts)
            or "\\\\" in member.name
            or any(ord(character) < 32 for character in member.name)
            or member.name in seen
            or not (member.isdir() or member.isfile())
        ):
            raise SystemExit("source archive contains an unsafe member")
        seen.add(member.name)
        if member.isfile():
            total_bytes += member.size
            if total_bytes > 536_870_912:
                raise SystemExit("source archive expands beyond its limit")
    retained.extractall(destination, members=members, filter="data")
marker = {{
    "repository_commit": repository_commit,
    "schema_version": "inferdrome.source-tree-export.v1",
    "source_archive_sha256": source_archive_sha256,
    "transport": "git-archive-exact-head-tree-v1",
}}
(destination / ".inferdrome-source-export.json").write_text(
    json.dumps(marker, indent=2, sort_keys=True) + "\\n",
    encoding="utf-8",
)
PY
cd {root}/repo
unset CUDA_VISIBLE_DEVICES NVIDIA_VISIBLE_DEVICES
mkdir -m 700 -- {root}/home
env -i \\
  HOME={root}/home \\
  LANG=C.UTF-8 \\
  LC_ALL=C.UTF-8 \\
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \\
  timeout --foreground --signal=TERM --kill-after=60s {remote_timeout_seconds}s \\
  ./scripts/run_real_gpu_capture.sh \\
  --state-root {root}/state \\
  --capture-root {root}/capture \\
  --gpu-index {gpu_index} \\
  --startup-timeout-seconds {startup_timeout_seconds}{profile_argument}
"""


def _write_json(path: Path, value: dict[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o444)
    except OSError:
        raise RemoteCaptureError(f"could not publish {path.name}") from None


def _checksum_file(path: Path) -> str:
    try:
        content = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        raise RemoteCaptureError("remote archive checksum is unreadable") from None
    lines = content.splitlines()
    if len(lines) != 1:
        raise RemoteCaptureError("remote archive checksum has an invalid shape")
    fields = lines[0].split()
    if len(fields) != 2 or re.fullmatch(r"[0-9a-f]{64}", fields[0]) is None:
        raise RemoteCaptureError("remote archive checksum is invalid")
    if Path(fields[1].lstrip("*")).name != "capture.tar.gz":
        raise RemoteCaptureError("remote checksum names an unexpected archive")
    return "sha256:" + fields[0]


def _read_transfer_metadata(path: Path) -> tuple[int, str]:
    try:
        metadata = os.lstat(path)
        content = path.read_bytes()
    except OSError:
        raise RemoteCaptureError("remote transfer metadata is unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not 2 <= metadata.st_size <= 4_096
    ):
        raise RemoteCaptureError("remote transfer metadata is unsafe")
    value = _strict_json_bytes(content, label="remote transfer metadata")
    if (
        not isinstance(value, dict)
        or set(value)
        != {"archive_name", "archive_sha256", "schema_version", "size_bytes"}
        or value.get("archive_name") != "capture.tar.gz"
        or value.get("schema_version") != "inferdrome.qwen3-transfer-metadata.v1"
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(value.get("archive_sha256")),
        )
        is None
        or isinstance(value.get("size_bytes"), bool)
        or not isinstance(value.get("size_bytes"), int)
        or not 1 <= value["size_bytes"] <= _QWEN3_MAX_ARCHIVE_BYTES
    ):
        raise RemoteCaptureError("remote transfer metadata is invalid")
    return value["size_bytes"], value["archive_sha256"]


def _remote_file_stream_command(
    path: str,
    expected_size: int | None = None,
    *,
    minimum_size: int = 1,
    maximum_size: int | None = None,
) -> str:
    if expected_size is not None:
        minimum_size = expected_size
        maximum_size = expected_size
    if (
        isinstance(minimum_size, bool)
        or not isinstance(minimum_size, int)
        or isinstance(maximum_size, bool)
        or not isinstance(maximum_size, int)
        or not 1 <= minimum_size <= maximum_size <= _QWEN3_MAX_ARCHIVE_BYTES
    ):
        raise RemoteCaptureError("remote file stream bounds are invalid")
    code = """import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
minimum_size = int(sys.argv[2])
maximum_size = int(sys.argv[3])
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
try:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not minimum_size <= before.st_size <= maximum_size
    ):
        raise SystemExit("remote file identity is invalid")
    remaining = before.st_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 65_536))
        if not chunk:
            raise SystemExit("remote file was truncated")
        sys.stdout.buffer.write(chunk)
        remaining -= len(chunk)
    sys.stdout.buffer.flush()
    if os.read(descriptor, 1):
        raise SystemExit("remote file grew")
    after = os.fstat(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise SystemExit("remote file changed during transfer")
finally:
    os.close(descriptor)
"""
    return shlex.join(
        [
            "python3.12",
            "-c",
            code,
            path,
            str(minimum_size),
            str(maximum_size),
        ]
    )


def _download_remote_file(
    arguments: Sequence[str],
    destination: Path,
    *,
    minimum_size: int,
    maximum_size: int,
    timeout: int,
    expected_sha256: str | None = None,
) -> tuple[int, str]:
    """Stream one remote file without writing more than the local byte bound."""

    if (
        isinstance(minimum_size, bool)
        or not isinstance(minimum_size, int)
        or isinstance(maximum_size, bool)
        or not isinstance(maximum_size, int)
        or not 1 <= minimum_size <= maximum_size <= _QWEN3_MAX_ARCHIVE_BYTES
        or timeout < 1
        or (
            expected_sha256 is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", expected_sha256) is None
        )
    ):
        raise RemoteCaptureError("bounded archive transfer inputs are invalid")
    descriptor: int | None = None
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    completed = False
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        process = subprocess.Popen(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        if process.stdout is None or process.stderr is None:
            raise OSError
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + timeout
        digest = hashlib.sha256()
        received = 0
        stderr = bytearray()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(arguments, timeout)
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    if len(stderr) < 8_192:
                        stderr.extend(chunk[: 8_192 - len(stderr)])
                    continue
                if received + len(chunk) > maximum_size:
                    raise RemoteCaptureError(
                        "remote file exceeded its maximum byte count"
                    )
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
                received += len(chunk)
                digest.update(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(arguments, timeout)
        returncode = process.wait(timeout=remaining)
        if returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:500]
            suffix = f": {detail}" if detail else ""
            raise RemoteCaptureError(f"bounded archive retrieval failed{suffix}")
        if not minimum_size <= received <= maximum_size:
            raise RemoteCaptureError("remote file byte count is outside its bounds")
        actual = "sha256:" + digest.hexdigest()
        if expected_sha256 is not None and actual != expected_sha256:
            raise RemoteCaptureError("remote archive SHA-256 disagrees")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        destination.chmod(0o444)
        completed = True
        return received, actual
    except (OSError, subprocess.TimeoutExpired):
        raise RemoteCaptureError(
            "bounded archive retrieval could not complete"
        ) from None
    finally:
        if selector is not None:
            selector.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if descriptor is not None:
            os.close(descriptor)
        if destination.exists() and not completed:
            destination.unlink(missing_ok=True)


def _download_bounded_remote_file(
    arguments: Sequence[str],
    destination: Path,
    *,
    minimum_size: int,
    maximum_size: int,
    timeout: int,
) -> None:
    _download_remote_file(
        arguments,
        destination,
        minimum_size=minimum_size,
        maximum_size=maximum_size,
        timeout=timeout,
    )


def _download_exact_remote_file(
    arguments: Sequence[str],
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    timeout: int,
) -> None:
    _download_remote_file(
        arguments,
        destination,
        minimum_size=expected_size,
        maximum_size=expected_size,
        expected_sha256=expected_sha256,
        timeout=timeout,
    )


def _host_identity_digest(known_hosts: Path) -> str:
    try:
        content = known_hosts.read_bytes()
    except OSError:
        raise RemoteCaptureError("SSH host identity record is unavailable") from None
    if not content or len(content) > 1_048_576:
        raise RemoteCaptureError("SSH host identity record is invalid")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _static_check() -> None:
    required = [
        REPOSITORY_ROOT / "scripts" / "prepare_real_gpu_host.sh",
        REPOSITORY_ROOT / "scripts" / "run_real_gpu_capture.sh",
        REPOSITORY_ROOT / "scripts" / "run_real_gpu_demo.py",
        REPOSITORY_ROOT / "scripts" / "real_gpu_capture.py",
        REPOSITORY_ROOT / "scripts" / "qwen3_gpu_capture.py",
        REPOSITORY_ROOT / "scripts" / "lambda_gpu_guard.py",
    ]
    for path in required:
        if not path.is_file() or path.is_symlink():
            raise RemoteCaptureError(
                f"required capture asset is unavailable: {path.name}"
            )


def _destination_host(destination: str) -> str:
    host = destination.rsplit("@", 1)[-1]
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _lambda_guard_requested(args: argparse.Namespace) -> bool:
    selected = (
        getattr(args, "lambda_instance_id", None),
        getattr(args, "lambda_instance_type_name", None),
        getattr(args, "lambda_hourly_rate_usd", None),
        getattr(args, "max_cost_usd", None),
        getattr(args, "lambda_billing_started_at", None),
    )
    if not any(value is not None for value in selected):
        return False
    if (
        args.lambda_hourly_rate_usd is None
        or args.max_cost_usd is None
        or args.lambda_billing_started_at is None
    ):
        raise RemoteCaptureError(
            "Lambda protection requires --lambda-hourly-rate-usd, --max-cost-usd, "
            "and --lambda-billing-started-at"
        )
    return True


def _qwen3_profile_requested(args: argparse.Namespace) -> bool:
    return getattr(args, "managed_capability_profile", None) == _QWEN3_PROFILE_ID


def _qwen3_tier_policy(args: argparse.Namespace) -> Qwen3GpuTierPolicy:
    gpu_tier_id = getattr(args, "qwen3_gpu_tier", None)
    if gpu_tier_id is None:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires --qwen3-gpu-tier"
        )
    try:
        return qwen3_gpu_tier_policy(gpu_tier_id)
    except ValueError as error:
        raise RemoteCaptureError(str(error)) from None


def _validate_capture_mode(args: argparse.Namespace) -> None:
    """Keep the legacy workflow stable and make the campaign path fail closed."""

    profile = getattr(args, "managed_capability_profile", None)
    if profile is None:
        if (
            getattr(args, "qwen3_gpu_tier", None) is not None
            or getattr(args, "lambda_instance_type_name", None) is not None
        ):
            raise RemoteCaptureError(
                "Qwen3 GPU tier and Lambda instance type require the managed profile"
            )
        if args.remote_timeout_seconds < _LEGACY_MINIMUM_REMOTE_SECONDS:
            raise RemoteCaptureError(
                "legacy remote timeout must be at least 1800 seconds"
            )
        return
    if profile != _QWEN3_PROFILE_ID:
        raise RemoteCaptureError("managed capability profile is unsupported")
    policy = _qwen3_tier_policy(args)
    if not _lambda_guard_requested(args):
        raise RemoteCaptureError(
            "Qwen3 capability capture requires the complete Lambda cost guard"
        )
    if args.lambda_instance_id is None:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires --lambda-instance-id"
        )
    if getattr(args, "lambda_instance_type_name", None) is None:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires --lambda-instance-type-name"
        )
    if args.identity_file is None:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires an explicit SSH identity file"
        )
    if args.startup_timeout_seconds != _QWEN3_STARTUP_TIMEOUT_SECONDS:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires the frozen "
            "300-second startup timeout"
        )
    if args.remote_timeout_seconds < _QWEN3_REMOTE_CAPTURE_SECONDS:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires at least 1300 remote seconds"
        )
    if args.lambda_hourly_rate_usd != policy.hourly_rate_usd:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires the frozen "
            f"${policy.hourly_rate_usd} hourly rate for {policy.gpu_tier_id}"
        )
    if args.max_cost_usd != policy.max_session_cost_usd:
        raise RemoteCaptureError(
            "Qwen3 capability capture requires the exact "
            f"${policy.max_session_cost_usd} session cap for {policy.gpu_tier_id}"
        )
    if sum(_QWEN3_PHASE_BUDGET_SECONDS.values()) > policy.allowed_seconds:
        raise RemoteCaptureError("Qwen3 paid-session phase budget exceeds its cost cap")


def _effective_remote_timeout(
    requested_seconds: int,
    *,
    termination_deadline: datetime | None,
    now: datetime | None = None,
) -> int:
    """Clamp remote work so control returns before provider termination."""

    if termination_deadline is None:
        return requested_seconds
    selected_now = (now or datetime.now(UTC)).astimezone(UTC)
    deadline = termination_deadline.astimezone(UTC)
    available = int((deadline - selected_now).total_seconds())
    bounded = min(
        requested_seconds,
        _QWEN3_REMOTE_CAPTURE_SECONDS,
        available - _QWEN3_POST_REMOTE_BUDGET_SECONDS,
    )
    if bounded < _MINIMUM_PROFILE_REMOTE_SECONDS:
        raise RemoteCaptureError(
            "Lambda termination window leaves less than 300 seconds for remote work"
        )
    return bounded


def _transfer_deadline(
    *,
    termination_deadline: datetime,
    now: datetime | None = None,
    monotonic: float | None = None,
) -> float:
    selected_now = (now or datetime.now(UTC)).astimezone(UTC)
    available = (
        termination_deadline.astimezone(UTC) - selected_now
    ).total_seconds() - _QWEN3_PHASE_BUDGET_SECONDS["controller_handoff"]
    if available < 1:
        raise RemoteCaptureError("Lambda termination window is exhausted")
    return (time.monotonic() if monotonic is None else monotonic) + available


def _remaining_transfer_timeout(
    deadline: float,
    *,
    phase_limit_seconds: int,
    monotonic: float | None = None,
) -> int:
    remaining = deadline - (time.monotonic() if monotonic is None else monotonic)
    bounded = min(phase_limit_seconds, int(remaining))
    if bounded < 1:
        raise RemoteCaptureError("Lambda retrieval deadline is exhausted")
    return bounded


def _arm_lambda_watchdog(
    args: argparse.Namespace,
) -> lambda_gpu_guard.WatchdogHandle | None:
    if not _lambda_guard_requested(args):
        return None
    host = _destination_host(args.destination)
    reference = args.lambda_instance_id or host
    try:
        handle = lambda_gpu_guard.arm_watchdog(
            reference,
            hourly_rate_usd=args.lambda_hourly_rate_usd,
            max_cost_usd=args.max_cost_usd,
            billing_started_at=args.lambda_billing_started_at,
            state_root=Path(args.lambda_guard_state_root),
            expected_endpoint=host,
            termination_safety_margin_seconds=(
                _QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS
                if _qwen3_profile_requested(args)
                else lambda_gpu_guard._TERMINATION_SAFETY_MARGIN_SECONDS
            ),
        )
    except lambda_gpu_guard.LambdaGuardError as error:
        raise RemoteCaptureError(
            f"Lambda cost guard could not be armed: {error}"
        ) from None
    if _qwen3_profile_requested(args):
        policy = _qwen3_tier_policy(args)
        expected_instance_type = args.lambda_instance_type_name
        try:
            active_ids = [
                instance.instance_id
                for instance in handle.client.list_instances()
                if instance.status not in {"terminated", "preempted"}
            ]
            if (
                handle.instance.status != "active"
                or active_ids != [handle.instance.instance_id]
                or handle.instance.instance_type_name != expected_instance_type
            ):
                raise lambda_gpu_guard.LambdaGuardError(
                    "Qwen3 campaign requires exactly one active "
                    f"{expected_instance_type} target for {policy.gpu_tier_id}"
                )
        except lambda_gpu_guard.LambdaGuardError as error:
            try:
                lambda_gpu_guard.terminate_guarded_instance(
                    handle,
                    trigger="campaign-single-instance-check-failed",
                )
            except lambda_gpu_guard.LambdaGuardError as termination_error:
                raise RemoteCaptureError(
                    "Qwen3 single-instance check failed and immediate "
                    "termination was not confirmed; the watchdog remains armed: "
                    f"{termination_error}"
                ) from None
            raise RemoteCaptureError(
                f"Qwen3 single-instance check failed; target terminated: {error}"
            ) from None
    print(
        "Lambda termination watchdog armed for "
        f"{lambda_gpu_guard._timestamp(handle.cost_window.deadline)}."
    )
    print(f"Lambda guard state: {handle.state_directory}")
    return handle


def _dry_run(args: argparse.Namespace, commit: str, identity: Path | None) -> None:
    remote_root = f"/tmp/inferdrome-gpu-{commit[:12]}-<random>"
    guarded = _lambda_guard_requested(args)
    policy = _qwen3_tier_policy(args) if _qwen3_profile_requested(args) else None
    with tempfile.TemporaryDirectory(prefix="inferdrome-source-tree-") as temporary:
        source_archive_sha256, source_archive_bytes = _create_source_archive(
            Path(temporary) / "repo.tar",
            commit,
        )
    plan = {
        "billing_boundary": (
            "LAMBDA API TERMINATION WATCHDOG PLUS CONTROLLER FINALLY"
            if guarded
            else "OPERATOR MUST TERMINATE THE CLOUD INSTANCE"
        ),
        "destination": args.destination,
        "expected_gpu_model": (
            policy.expected_nvidia_smi_name if policy is not None else None
        ),
        "expected_lambda_instance_type": (
            args.lambda_instance_type_name if policy is not None else None
        ),
        "gpu_index": args.gpu_index,
        "identity_file_configured": identity is not None,
        "lambda_cost_guard": (
            {
                "billing_started_at": lambda_gpu_guard._timestamp(
                    args.lambda_billing_started_at
                ),
                "hourly_rate_usd": str(args.lambda_hourly_rate_usd),
                "instance_reference": args.lambda_instance_id
                or _destination_host(args.destination),
                "max_cost_usd": str(args.max_cost_usd),
            }
            if guarded
            else None
        ),
        "repository_commit": commit,
        "source_archive_bytes": source_archive_bytes,
        "source_archive_sha256": source_archive_sha256,
        "managed_capability_profile": args.managed_capability_profile,
        "qwen3_gpu_tier": policy.gpu_tier_id if policy is not None else None,
        "phase_budget_seconds": (
            _QWEN3_PHASE_BUDGET_SECONDS if _qwen3_profile_requested(args) else None
        ),
        "remote_root": remote_root,
        "remote_timeout_seconds": args.remote_timeout_seconds,
        "startup_timeout_seconds": args.startup_timeout_seconds,
        "steps": [
            "verify a clean exact local commit",
            "validate an exact-HEAD tree archive locally without Git history",
            "arm and validate the independent Lambda termination watchdog",
            "rebuild the checked source archive under watchdog protection",
            "preflight Linux, Python 3.12, NVIDIA, and required host tools",
            "upload and digest-check the exact source archive",
            "prepare the pinned vLLM/model environment",
            (
                "run one Qwen3-8B concurrency-1 capability spike on "
                f"{policy.expected_nvidia_smi_name}"
                if policy is not None
                else "run the single proof and four-run controlled comparison"
            ),
            "retrieve bounded size/checksum metadata, then exactly those archive bytes",
            (
                "terminate and confirm the Lambda instance through its API"
                if guarded
                else "prompt the operator to terminate the billable instance"
            ),
            "independently recalculate and verify every retrieved proof artifact",
        ],
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))


def _capture_over_ssh(
    args: argparse.Namespace,
    commit: str,
    identity: Path | None,
    source_archive: Path,
    source_archive_sha256: str,
    *,
    termination_deadline: datetime | None = None,
) -> Path:
    for executable in ("ssh", "scp"):
        if shutil.which(executable) is None:
            raise RemoteCaptureError(f"{executable} is required")
    output_root = Path(args.output_root).expanduser().absolute()
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        output_metadata = os.lstat(output_root)
    except OSError:
        raise RemoteCaptureError("local capture output root is unavailable") from None
    if output_root.is_symlink() or not stat.S_ISDIR(output_metadata.st_mode):
        raise RemoteCaptureError("local capture output root is unsafe")
    token = os.urandom(4).hex()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{timestamp}-{commit[:12]}-{token}"
    final_path = output_root / name
    staging_path = output_root / f".{name}.staging"
    if final_path.exists() or staging_path.exists():
        raise RemoteCaptureError("local capture destination already exists")
    staging_path.mkdir(mode=0o700)
    known_hosts = staging_path / "ssh-known-hosts"
    archive = staging_path / "capture.tar.gz"
    checksum = staging_path / "capture.tar.gz.sha256"
    transfer_metadata = staging_path / "capture.tar.gz.metadata.json"
    remote_root = f"/tmp/inferdrome-gpu-{commit[:12]}-{token}"
    pinned_host_key = (
        bytes.fromhex(args.host_key_sha256)
        if args.host_key_sha256 is not None
        else None
    )
    ssh_options = _ssh_options(
        identity=identity,
        known_hosts=known_hosts,
        port=args.port,
    )
    scp_options = _scp_options(
        identity=identity,
        known_hosts=known_hosts,
        port=args.port,
    )
    policy = _qwen3_tier_policy(args) if _qwen3_profile_requested(args) else None
    profile_deadline = termination_deadline if _qwen3_profile_requested(args) else None
    if _qwen3_profile_requested(args):
        _effective_remote_timeout(
            args.remote_timeout_seconds,
            termination_deadline=profile_deadline,
        )

    print(f"Inferdrome commit: {commit}")
    print(f"Remote workspace: {remote_root}")
    print("Preflighting the operator-provided GPU host…", flush=True)
    _run(
        [
            "ssh",
            *ssh_options,
            args.destination,
            _bash_command(
                _remote_preflight_script(
                    remote_root,
                    gpu_index=args.gpu_index,
                    expected_gpu_model=(
                        policy.expected_nvidia_smi_name
                        if policy is not None
                        else None
                    ),
                )
            ),
        ],
        label="remote GPU preflight",
        timeout=90,
    )
    observed_host_identity_sha256 = _host_identity_digest(known_hosts)
    if (
        pinned_host_key is not None
        and hashlib.sha256(known_hosts.read_bytes()).digest() != pinned_host_key
    ):
        raise RemoteCaptureError("SSH host identity does not match its expected digest")
    try:
        _run(
            [
                "scp",
                *scp_options,
                str(source_archive),
                f"{args.destination}:{remote_root}/repo.tar",
            ],
            label="exact source tree upload",
            timeout=(
                _QWEN3_PHASE_BUDGET_SECONDS["source_upload"]
                if _qwen3_profile_requested(args)
                else 600
            ),
        )

        print(
            "Running the bounded proof pack; model preparation is usually "
            "the slowest step…",
            flush=True,
        )
        effective_remote_timeout = _effective_remote_timeout(
            args.remote_timeout_seconds,
            termination_deadline=profile_deadline,
        )
        if effective_remote_timeout != args.remote_timeout_seconds:
            print(
                "Remote timeout clamped to the live Lambda termination window: "
                f"{effective_remote_timeout}s",
                flush=True,
            )
        remote_result = _run(
            [
                "ssh",
                *ssh_options,
                args.destination,
                _bash_command(
                    _remote_capture_script(
                        remote_root,
                        commit,
                        source_archive_sha256,
                        gpu_index=args.gpu_index,
                        startup_timeout_seconds=args.startup_timeout_seconds,
                        remote_timeout_seconds=effective_remote_timeout,
                        managed_capability_profile=args.managed_capability_profile,
                        qwen3_gpu_tier=(
                            policy.gpu_tier_id if policy is not None else None
                        ),
                    )
                ),
            ],
            label="remote proof pack",
            timeout=effective_remote_timeout
            + (65 if _qwen3_profile_requested(args) else 600),
            check=False,
        )
        retrieval_error: RemoteCaptureError | None = None
        try:
            if _qwen3_profile_requested(args):
                if profile_deadline is None:
                    raise RemoteCaptureError(
                        "Qwen3 retrieval requires a provider termination deadline"
                    )
                shared_deadline = _transfer_deadline(
                    termination_deadline=profile_deadline
                )
                _download_bounded_remote_file(
                    [
                        "ssh",
                        *ssh_options,
                        args.destination,
                        _remote_file_stream_command(
                            f"{remote_root}/capture.tar.gz.metadata.json",
                            minimum_size=2,
                            maximum_size=4_096,
                        ),
                    ],
                    transfer_metadata,
                    minimum_size=2,
                    maximum_size=4_096,
                    timeout=_remaining_transfer_timeout(
                        shared_deadline,
                        phase_limit_seconds=_QWEN3_METADATA_TRANSFER_SECONDS,
                    ),
                )
                expected_size, expected_archive_sha256 = _read_transfer_metadata(
                    transfer_metadata
                )
                _download_exact_remote_file(
                    [
                        "ssh",
                        *ssh_options,
                        args.destination,
                        _remote_file_stream_command(
                            f"{remote_root}/capture.tar.gz",
                            expected_size,
                        ),
                    ],
                    archive,
                    expected_size=expected_size,
                    expected_sha256=expected_archive_sha256,
                    timeout=_remaining_transfer_timeout(
                        shared_deadline,
                        phase_limit_seconds=_QWEN3_ARCHIVE_TRANSFER_SECONDS,
                    ),
                )
                _write_bytes_exclusive(
                    checksum,
                    (
                        expected_archive_sha256.removeprefix("sha256:")
                        + "  capture.tar.gz\n"
                    ).encode("ascii"),
                )
                actual_archive_sha256 = expected_archive_sha256
            else:
                for remote_name, local_path in (
                    ("capture.tar.gz", archive),
                    ("capture.tar.gz.sha256", checksum),
                ):
                    _run(
                        [
                            "scp",
                            *scp_options,
                            f"{args.destination}:{remote_root}/{remote_name}",
                            str(local_path),
                        ],
                        label=f"{remote_name} retrieval",
                        timeout=1_800,
                    )
        except RemoteCaptureError as error:
            retrieval_error = error
        if retrieval_error is not None:
            raise retrieval_error

        expected_archive_sha256 = _checksum_file(checksum)
        if not _qwen3_profile_requested(args):
            try:
                actual_archive_sha256 = real_gpu_capture.archive_sha256(archive)
                if actual_archive_sha256 != expected_archive_sha256:
                    raise RemoteCaptureError(
                        "retrieved archive failed SHA-256 verification"
                    )
            except real_gpu_capture.CaptureError as error:
                raise RemoteCaptureError(str(error)) from None
        if remote_result.returncode != 0:
            if not _qwen3_profile_requested(args):
                try:
                    real_gpu_capture.extract_capture_archive(
                        archive,
                        staging_path,
                        expected_archive_sha256=expected_archive_sha256,
                    )
                except real_gpu_capture.CaptureError as error:
                    raise RemoteCaptureError(str(error)) from None
            failure_path = output_root / f"{name}-FAILED"
            os.replace(staging_path, failure_path)
            raise RemoteCaptureError(
                "remote proof pack failed; retained diagnostics at "
                f"{failure_path} (remote workspace {remote_root})"
            )
        if _qwen3_profile_requested(args):
            if policy is None:
                raise AssertionError
            retrieval = {
                "archive_sha256": actual_archive_sha256,
                "billing_action_required": "PROVIDER_TERMINATION_PENDING",
                "gpu_tier_id": policy.gpu_tier_id,
                "lambda_instance_type_name": args.lambda_instance_type_name,
                "managed_capability_profile": _QWEN3_PROFILE_ID,
                "repository_commit": commit,
                "schema_version": "inferdrome.qwen3-gpu-retrieval.v2",
                "source_archive_sha256": source_archive_sha256,
                "semantic_verification": (
                    "PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"
                ),
                "ssh_host_identity_sha256": observed_host_identity_sha256,
                "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        else:
            try:
                real_gpu_capture.extract_capture_archive(
                    archive,
                    staging_path,
                    expected_archive_sha256=expected_archive_sha256,
                )
                archive_verification = real_gpu_capture.verify_capture_archive(
                    archive,
                    expected_archive_sha256=expected_archive_sha256,
                    expected_repository_commit=commit,
                )
            except real_gpu_capture.CaptureError as error:
                failure_path = output_root / f"{name}-UNVERIFIED"
                os.replace(staging_path, failure_path)
                raise RemoteCaptureError(
                    f"retrieved capture failed local verification: {error}; "
                    f"retained at {failure_path}"
                ) from None
            retrieval = {
                "archive_sha256": archive_verification["archive_sha256"],
                "billing_action_required": "TERMINATE_THE_GPU_INSTANCE",
                "capture_manifest_sha256": archive_verification[
                    "capture_manifest_sha256"
                ],
                "repository_commit": commit,
                "schema_version": "inferdrome.real-gpu-retrieval.v1",
                "ssh_host_identity_sha256": observed_host_identity_sha256,
                "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "verification": archive_verification["verification"],
            }
        _write_json(staging_path / "retrieval-receipt.json", retrieval)
        os.replace(staging_path, final_path)
    except Exception:
        if staging_path.exists():
            print(
                f"Partial local diagnostics remain at {staging_path}",
                file=sys.stderr,
            )
        raise

    if _qwen3_profile_requested(args):
        print(
            "\nARCHIVE CHECKSUM VERIFIED. LAMBDA TERMINATION IS BEING CONFIRMED "
            "BEFORE OFFLINE SEMANTIC VERIFICATION."
        )
        print(f"Pending local capture record: {final_path}")
        print(f"Remote workspace retained until instance termination: {remote_root}")
        return final_path
    if _lambda_guard_requested(args):
        print("\nCAPTURE VERIFIED. LAMBDA TERMINATION IS BEING CONFIRMED.")
    else:
        print("\nCAPTURE VERIFIED. TERMINATE THE BILLABLE GPU INSTANCE NOW.")
    print(f"Local capture record: {final_path}")
    print(f"Remote workspace retained until instance termination: {remote_root}")
    comparison_roots = sorted(
        (final_path / "capture" / "comparison").glob("real-gpu-comparison-*")
    )
    if len(comparison_roots) == 1:
        comparison_root = comparison_roots[0]
        dashboard_executable = shutil.which("inferdrome") or "inferdrome"
        dashboard_arguments = [
            dashboard_executable,
            "dashboard",
            "--runs-root",
            str(comparison_root / "runs"),
            "--trial-sets-root",
            str(comparison_root / "trial-sets"),
            "--comparison-plans-root",
            str(comparison_root / "comparison-plans"),
            "--comparison-results-root",
            str(comparison_root / "comparison-results"),
            "--open",
        ]
        print("Dashboard inspection command:")
        print("  " + shlex.join(dashboard_arguments))
    return final_path


def _write_bytes_exclusive(path: Path, content: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise RemoteCaptureError(f"could not publish {path.name}") from None


def _write_bytes_idempotent(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        try:
            metadata = os.lstat(path)
            existing = path.read_bytes()
        except OSError:
            raise RemoteCaptureError(f"could not verify {path.name}") from None
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or existing != content
        ):
            raise RemoteCaptureError(f"existing {path.name} disagrees")
        return
    _write_bytes_exclusive(path, content)


def _strict_json_bytes(content: bytes, *, label: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _token: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RemoteCaptureError(f"{label} is invalid") from None
    if not isinstance(value, dict):
        raise RemoteCaptureError(f"{label} is invalid")
    return value


def _safe_record_bytes(path: Path, *, label: str) -> bytes:
    try:
        metadata = os.lstat(path)
        content = path.read_bytes()
    except OSError:
        raise RemoteCaptureError(f"{label} is unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not content
        or len(content) > 65_536
    ):
        raise RemoteCaptureError(f"{label} is unsafe")
    return content


@dataclass(frozen=True)
class _TerminationEvidence:
    cost_window: dict[str, Any]
    instance: dict[str, Any]
    receipt: bytes
    termination: dict[str, Any]
    trigger: str


@dataclass(frozen=True)
class _Qwen3RetrievalIdentity:
    gpu_tier_id: str
    instance_type_name: str
    legacy: bool
    receipt: bytes
    schema_version: str
    value: dict[str, Any]


def _qwen3_retrieval_identity(capture_path: Path) -> _Qwen3RetrievalIdentity:
    receipt = _safe_record_bytes(
        capture_path / "retrieval-receipt.json",
        label="Qwen3 retrieval receipt",
    )
    value = _strict_json_bytes(receipt, label="Qwen3 retrieval receipt")
    schema_version = value.get("schema_version")
    if schema_version == "inferdrome.qwen3-gpu-retrieval.v1":
        return _Qwen3RetrievalIdentity(
            gpu_tier_id=QWEN3_A10_GPU_TIER_ID,
            instance_type_name=_QWEN3_EXPECTED_INSTANCE_TYPE,
            legacy=True,
            receipt=receipt,
            schema_version=schema_version,
            value=value,
        )
    if schema_version != "inferdrome.qwen3-gpu-retrieval.v2":
        raise RemoteCaptureError("Qwen3 retrieval receipt schema is unsupported")
    gpu_tier_id = value.get("gpu_tier_id")
    instance_type_name = value.get("lambda_instance_type_name")
    if not isinstance(gpu_tier_id, str):
        raise RemoteCaptureError("Qwen3 retrieval GPU tier is invalid")
    try:
        qwen3_gpu_tier_policy(gpu_tier_id)
    except ValueError as error:
        raise RemoteCaptureError(str(error)) from None
    if (
        not isinstance(instance_type_name, str)
        or _INSTANCE_TYPE_PATTERN.fullmatch(instance_type_name) is None
    ):
        raise RemoteCaptureError("Qwen3 retrieval instance type is invalid")
    return _Qwen3RetrievalIdentity(
        gpu_tier_id=gpu_tier_id,
        instance_type_name=instance_type_name,
        legacy=False,
        receipt=receipt,
        schema_version=schema_version,
        value=value,
    )


def _timestamp_value(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RemoteCaptureError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise RemoteCaptureError(f"{label} is invalid") from None
    if parsed.tzinfo != UTC:
        raise RemoteCaptureError(f"{label} is invalid")
    return parsed


def _validate_termination_evidence(
    *,
    cost_window: object,
    instance: object,
    receipt_value: dict[str, Any],
    identity: _Qwen3RetrievalIdentity,
) -> _TerminationEvidence:
    policy = qwen3_gpu_tier_policy(identity.gpu_tier_id)
    if (
        not isinstance(cost_window, dict)
        or set(cost_window)
        != {
            "allowed_seconds",
            "billing_started_at",
            "cost_limit_deadline",
            "deadline",
            "hourly_rate_usd",
            "max_cost_usd",
            "termination_safety_margin_seconds",
        }
        or cost_window.get("allowed_seconds") != policy.allowed_seconds
        or cost_window.get("hourly_rate_usd") != str(policy.hourly_rate_usd)
        or cost_window.get("max_cost_usd") != str(policy.max_session_cost_usd)
        or cost_window.get("termination_safety_margin_seconds")
        != _QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS
    ):
        raise RemoteCaptureError("Lambda Qwen3 cost window is invalid")
    started = _timestamp_value(
        cost_window.get("billing_started_at"),
        label="Lambda billing start",
    )
    deadline = _timestamp_value(
        cost_window.get("deadline"),
        label="Lambda termination deadline",
    )
    cost_limit = _timestamp_value(
        cost_window.get("cost_limit_deadline"),
        label="Lambda cost-limit deadline",
    )
    if (
        int((cost_limit - started).total_seconds()) != policy.allowed_seconds
        or int((cost_limit - deadline).total_seconds())
        != _QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS
    ):
        raise RemoteCaptureError("Lambda Qwen3 cost-window timestamps disagree")
    if (
        not isinstance(instance, dict)
        or set(instance)
        != {
            "hostname",
            "hourly_rate_usd",
            "instance_id",
            "instance_type_name",
            "ip",
            "status",
        }
        or re.fullmatch(r"[0-9a-f]{32}", str(instance.get("instance_id"))) is None
        or instance.get("hourly_rate_usd") != str(policy.hourly_rate_usd)
        or instance.get("instance_type_name") != identity.instance_type_name
        or instance.get("status") != "active"
    ):
        raise RemoteCaptureError("Lambda Qwen3 instance record is invalid")
    if (
        set(receipt_value)
        != {
            "cost_window",
            "record_kind",
            "schema_version",
            "termination",
            "trigger",
        }
        or receipt_value.get("cost_window") != cost_window
        or receipt_value.get("record_kind")
        != "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
        or receipt_value.get("schema_version")
        != "inferdrome.lambda-termination-receipt.v2"
        or receipt_value.get("trigger") not in {"controller-finally", "cost-deadline"}
    ):
        raise RemoteCaptureError("Lambda termination receipt is invalid")
    termination = receipt_value.get("termination")
    if (
        not isinstance(termination, dict)
        or set(termination)
        != {"confirmed_at", "final_status", "instance_id", "request_sent"}
        or termination.get("instance_id") != instance.get("instance_id")
        or termination.get("final_status") not in {"terminated", "preempted", "absent"}
        or not isinstance(termination.get("request_sent"), bool)
    ):
        raise RemoteCaptureError("Lambda termination confirmation is invalid")
    _timestamp_value(
        termination.get("confirmed_at"),
        label="Lambda termination confirmation time",
    )
    return _TerminationEvidence(
        cost_window=cost_window,
        instance=instance,
        receipt=b"",
        termination=termination,
        trigger=receipt_value["trigger"],
    )


def _controller_termination_evidence(
    watchdog: lambda_gpu_guard.WatchdogHandle,
    termination: lambda_gpu_guard.TerminationResult,
    identity: _Qwen3RetrievalIdentity,
) -> _TerminationEvidence:
    receipt = _safe_record_bytes(
        watchdog.receipt_path,
        label="Lambda termination receipt",
    )
    receipt_value = _strict_json_bytes(
        receipt,
        label="Lambda termination receipt",
    )
    evidence = _validate_termination_evidence(
        cost_window=watchdog.cost_window.public_record(),
        instance=watchdog.instance.public_record(),
        receipt_value=receipt_value,
        identity=identity,
    )
    if evidence.termination != termination.public_record():
        raise RemoteCaptureError(
            "Lambda termination receipt disagrees with confirmation"
        )
    return _TerminationEvidence(
        cost_window=evidence.cost_window,
        instance=evidence.instance,
        receipt=receipt,
        termination=evidence.termination,
        trigger=evidence.trigger,
    )


def _retained_termination_evidence(
    state_directory: Path,
    identity: _Qwen3RetrievalIdentity,
) -> _TerminationEvidence:
    selected = state_directory.expanduser().absolute()
    try:
        metadata = os.lstat(selected)
    except OSError:
        raise RemoteCaptureError(
            "Lambda guard state directory is unavailable"
        ) from None
    if selected.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise RemoteCaptureError("Lambda guard state directory is unsafe")
    armed_bytes = _safe_record_bytes(
        selected / "guard-armed.json",
        label="Lambda guard-armed receipt",
    )
    armed = _strict_json_bytes(
        armed_bytes,
        label="Lambda guard-armed receipt",
    )
    if (
        set(armed)
        != {
            "cost_window",
            "instance",
            "record_kind",
            "schema_version",
            "watchdog_ready",
        }
        or armed.get("record_kind") != "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
        or armed.get("schema_version") != "inferdrome.lambda-guard-armed.v2"
        or armed.get("watchdog_ready") is not True
    ):
        raise RemoteCaptureError("Lambda guard-armed receipt is invalid")
    receipt = _safe_record_bytes(
        selected / "termination-receipt.json",
        label="Lambda termination receipt",
    )
    receipt_value = _strict_json_bytes(
        receipt,
        label="Lambda termination receipt",
    )
    evidence = _validate_termination_evidence(
        cost_window=armed.get("cost_window"),
        instance=armed.get("instance"),
        receipt_value=receipt_value,
        identity=identity,
    )
    return _TerminationEvidence(
        cost_window=evidence.cost_window,
        instance=evidence.instance,
        receipt=receipt,
        termination=evidence.termination,
        trigger=evidence.trigger,
    )


def _finalize_qwen3_capture_with_evidence(
    capture_path: Path,
    *,
    commit: str,
    evidence: _TerminationEvidence,
) -> Path:
    """Idempotently verify and publish only after confirmed provider termination."""

    try:
        capture_metadata = os.lstat(capture_path)
    except OSError:
        raise RemoteCaptureError("Qwen3 capture directory is unavailable") from None
    if capture_path.is_symlink() or not stat.S_ISDIR(capture_metadata.st_mode):
        raise RemoteCaptureError("Qwen3 capture directory is unsafe")
    identity = _qwen3_retrieval_identity(capture_path)
    policy = qwen3_gpu_tier_policy(identity.gpu_tier_id)
    if (
        evidence.instance.get("instance_type_name") != identity.instance_type_name
        or evidence.instance.get("hourly_rate_usd") != str(policy.hourly_rate_usd)
        or evidence.cost_window.get("max_cost_usd")
        != str(policy.max_session_cost_usd)
    ):
        raise RemoteCaptureError(
            "Lambda termination evidence disagrees with Qwen3 retrieval identity"
        )

    archive = capture_path / "capture.tar.gz"
    checksum = capture_path / "capture.tar.gz.sha256"
    expected_archive_sha256 = _checksum_file(checksum)
    try:
        archive_verification = qwen3_gpu_capture.verify_capture_archive(
            archive,
            expected_archive_sha256=expected_archive_sha256,
            expected_repository_commit=commit,
            expected_gpu_tier_id=identity.gpu_tier_id,
        )
        extracted = capture_path / "capture"
        if extracted.exists() or extracted.is_symlink():
            extracted_verification = qwen3_gpu_capture.verify_capture(
                extracted,
                expected_repository_commit=commit,
                expected_gpu_tier_id=identity.gpu_tier_id,
            )
        else:
            with tempfile.TemporaryDirectory(
                prefix=".qwen3-finalize-",
                dir=capture_path,
            ) as temporary:
                staged = real_gpu_capture.extract_capture_archive(
                    archive,
                    Path(temporary),
                    expected_archive_sha256=expected_archive_sha256,
                )
                staged_verification = qwen3_gpu_capture.verify_capture(
                    staged,
                    expected_repository_commit=commit,
                    expected_gpu_tier_id=identity.gpu_tier_id,
                )
                os.replace(staged, extracted)
                extracted_verification = staged_verification
    except (
        qwen3_gpu_capture.Qwen3CaptureError,
        real_gpu_capture.CaptureError,
        OSError,
    ) as error:
        raise RemoteCaptureError(
            "terminated Qwen3 capture failed offline semantic verification: "
            f"{error}; retained at {capture_path}"
        ) from None
    if archive_verification["verification"] != extracted_verification:
        raise RemoteCaptureError(
            "Qwen3 archive verification changed after retained extraction"
        )
    retrieval_bytes = identity.receipt
    retrieval = identity.value
    retrieval_fields = {
        "archive_sha256",
        "billing_action_required",
        "managed_capability_profile",
        "repository_commit",
        "schema_version",
        "semantic_verification",
        "source_archive_sha256",
        "ssh_host_identity_sha256",
        "verified_at",
    }
    if not identity.legacy:
        retrieval_fields.update({"gpu_tier_id", "lambda_instance_type_name"})
    if (
        set(retrieval) != retrieval_fields
        or retrieval.get("archive_sha256") != archive_verification["archive_sha256"]
        or retrieval.get("billing_action_required") != "PROVIDER_TERMINATION_PENDING"
        or retrieval.get("managed_capability_profile") != _QWEN3_PROFILE_ID
        or retrieval.get("repository_commit") != commit
        or retrieval.get("schema_version") != identity.schema_version
        or retrieval.get("semantic_verification")
        != "PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"
        or retrieval.get("source_archive_sha256")
        != extracted_verification["source_archive_sha256"]
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(retrieval.get("ssh_host_identity_sha256")),
        )
        is None
        or (
            not identity.legacy
            and (
                retrieval.get("gpu_tier_id") != identity.gpu_tier_id
                or retrieval.get("lambda_instance_type_name")
                != identity.instance_type_name
            )
        )
    ):
        raise RemoteCaptureError("Qwen3 retrieval receipt disagrees with capture")
    _timestamp_value(
        retrieval.get("verified_at"),
        label="Qwen3 retrieval verification time",
    )

    retained_termination_path = capture_path / "lambda-termination-receipt.json"
    _write_bytes_idempotent(retained_termination_path, evidence.receipt)
    termination_receipt_sha256 = (
        "sha256:" + hashlib.sha256(evidence.receipt).hexdigest()
    )
    semantic_core: dict[str, Any] = {
        "archive_sha256": archive_verification["archive_sha256"],
        "capture_manifest_sha256": archive_verification["capture_manifest_sha256"],
        "managed_capability_profile": _QWEN3_PROFILE_ID,
        "provider_instance": evidence.instance,
        "provider_termination": evidence.termination,
        "retrieval_receipt_sha256": (
            "sha256:" + hashlib.sha256(retrieval_bytes).hexdigest()
        ),
        "repository_commit": commit,
        "run": extracted_verification["run"],
        "schema_version": (
            "inferdrome.qwen3-offline-verification.v1"
            if identity.legacy
            else "inferdrome.qwen3-offline-verification.v2"
        ),
        "semantic_verification": "VALID_AFTER_PROVIDER_TERMINATION",
        "termination_receipt_sha256": termination_receipt_sha256,
        "termination_trigger": evidence.trigger,
    }
    if not identity.legacy:
        semantic_core.update(
            {
                "gpu_target": extracted_verification["gpu_target"],
                "gpu_tier_id": identity.gpu_tier_id,
                "lambda_instance_type_name": identity.instance_type_name,
            }
        )
    semantic_receipt = {
        **semantic_core,
        "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    semantic_path = capture_path / "semantic-verification.json"
    if semantic_path.exists() or semantic_path.is_symlink():
        existing_bytes = _safe_record_bytes(
            semantic_path,
            label="Qwen3 semantic-verification receipt",
        )
        existing = _strict_json_bytes(
            existing_bytes,
            label="Qwen3 semantic-verification receipt",
        )
        verified_at = existing.pop("verified_at", None)
        _timestamp_value(verified_at, label="Qwen3 semantic verification time")
        if existing != semantic_core:
            raise RemoteCaptureError(
                "existing Qwen3 semantic-verification receipt disagrees"
            )
    else:
        _write_json(semantic_path, semantic_receipt)

    print("\nQWEN3 CAPTURE VERIFIED AFTER PROVIDER TERMINATION.")
    print(f"Local capture record: {capture_path}")
    dashboard_executable = shutil.which("inferdrome") or "inferdrome"
    dashboard_arguments = [
        dashboard_executable,
        "dashboard",
        "--runs-root",
        str(extracted / "runs"),
        "--open",
    ]
    print("Dashboard inspection command:")
    print("  " + shlex.join(dashboard_arguments))
    return capture_path


def _finalize_qwen3_capture(
    capture_path: Path,
    *,
    commit: str,
    watchdog: lambda_gpu_guard.WatchdogHandle,
    termination: lambda_gpu_guard.TerminationResult,
) -> Path:
    identity = _qwen3_retrieval_identity(capture_path)
    evidence = _controller_termination_evidence(watchdog, termination, identity)
    return _finalize_qwen3_capture_with_evidence(
        capture_path,
        commit=commit,
        evidence=evidence,
    )


def _resume_qwen3_finalization(
    capture_path: Path,
    *,
    commit: str,
    guard_state_directory: Path,
) -> Path:
    selected_capture_path = capture_path.expanduser().absolute()
    identity = _qwen3_retrieval_identity(selected_capture_path)
    evidence = _retained_termination_evidence(guard_state_directory, identity)
    return _finalize_qwen3_capture_with_evidence(
        selected_capture_path,
        commit=commit,
        evidence=evidence,
    )


def _capture_with_source(
    args: argparse.Namespace,
    commit: str,
    identity: Path | None,
    source_archive: Path | None = None,
    source_archive_sha256: str | None = None,
) -> Path:
    if (source_archive is None) != (source_archive_sha256 is None):
        raise RemoteCaptureError("source archive and digest must be supplied together")
    watchdog = _arm_lambda_watchdog(args)
    captured: Path | None = None
    termination: lambda_gpu_guard.TerminationResult | None = None
    termination_deadline = getattr(
        getattr(watchdog, "cost_window", None),
        "deadline",
        None,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="inferdrome-source-tree-") as temporary:
            selected_archive = source_archive
            selected_digest = source_archive_sha256
            if selected_archive is None:
                selected_archive = Path(temporary) / "repo.tar"
                selected_digest, source_archive_bytes = _create_source_archive(
                    selected_archive,
                    commit,
                )
                print(
                    "Exact source tree prepared without Git history: "
                    f"{selected_digest} ({source_archive_bytes} bytes)"
                )
            if selected_digest is None:
                raise AssertionError
            if termination_deadline is None:
                captured = _capture_over_ssh(
                    args,
                    commit,
                    identity,
                    selected_archive,
                    selected_digest,
                )
            else:
                captured = _capture_over_ssh(
                    args,
                    commit,
                    identity,
                    selected_archive,
                    selected_digest,
                    termination_deadline=termination_deadline,
                )
    finally:
        if watchdog is not None:
            capture_failed = sys.exc_info()[0] is not None
            try:
                termination = lambda_gpu_guard.terminate_guarded_instance(watchdog)
            except lambda_gpu_guard.LambdaGuardFinalizationError as error:
                print(
                    "WARNING: local Lambda guard finalization failed after confirmed "
                    f"termination: {error}",
                    file=sys.stderr,
                )
                if not capture_failed:
                    raise RemoteCaptureError(
                        "capture completed and Lambda terminated, but local guard "
                        "finalization failed"
                    ) from None
            except lambda_gpu_guard.LambdaGuardError as error:
                print(
                    "CRITICAL: immediate Lambda termination was not confirmed; "
                    f"the deadline watchdog remains armed: {error}",
                    file=sys.stderr,
                )
                if not capture_failed:
                    raise RemoteCaptureError(
                        "capture completed, but Lambda termination was not confirmed"
                    ) from None
            else:
                print(
                    "Lambda termination confirmed: "
                    f"{watchdog.instance.instance_id} ({termination.final_status})."
                )
    if captured is None:
        raise AssertionError
    if _qwen3_profile_requested(args):
        if watchdog is None or termination is None:
            raise RemoteCaptureError(
                "Qwen3 capture cannot verify before provider termination"
            )
        return _finalize_qwen3_capture(
            captured,
            commit=commit,
            watchdog=watchdog,
            termination=termination,
        )
    return captured


def _capture(args: argparse.Namespace, commit: str, identity: Path | None) -> Path:
    """Protect the active instance before rebuilding the checked source payload."""

    return _capture_with_source(args, commit, identity)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture the Inferdrome real-GPU proof pack over SSH"
    )
    parser.add_argument("destination", nargs="?", type=_validate_destination)
    parser.add_argument("--check", action="store_true", help="check local assets only")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the bounded workflow without contacting a host",
    )
    parser.add_argument("--expected-commit")
    parser.add_argument(
        "--resume-qwen3-finalization",
        metavar="CAPTURE_PATH",
        help="offline-resume a retained Qwen3 capture after confirmed termination",
    )
    parser.add_argument(
        "--lambda-guard-state-directory",
        help="retained guard directory used only by offline Qwen3 finalization",
    )
    parser.add_argument("--identity-file")
    parser.add_argument(
        "--host-key-sha256",
        type=_host_key_digest,
        help="optional SHA-256 hex digest of the capture-specific known_hosts bytes",
    )
    parser.add_argument("--port", type=_port, default=22)
    parser.add_argument("--gpu-index", type=_gpu_index, default=0)
    parser.add_argument(
        "--startup-timeout-seconds",
        type=_startup_timeout,
        default=900,
    )
    parser.add_argument(
        "--remote-timeout-seconds",
        type=_remote_timeout,
        default=_DEFAULT_REMOTE_TIMEOUT_SECONDS,
        help="bounded host workload time; this does not terminate the cloud instance",
    )
    parser.add_argument(
        "--managed-capability-profile",
        choices=(_QWEN3_PROFILE_ID,),
        help="explicit bounded campaign profile; omission preserves the legacy pack",
    )
    parser.add_argument(
        "--qwen3-gpu-tier",
        choices=QWEN3_IMPLEMENTED_GPU_TIERS,
        help="exact implemented Qwen3 campaign GPU tier",
    )
    parser.add_argument(
        "--output-root",
        default=str(REPOSITORY_ROOT / "gpu-proof-retrieved"),
    )
    parser.add_argument(
        "--lambda-instance-id",
        type=lambda_gpu_guard.parse_instance_id,
        help=(
            "optional Lambda instance ID; otherwise resolve the SSH host "
            "through the API"
        ),
    )
    parser.add_argument(
        "--lambda-instance-type-name",
        type=_lambda_instance_type_name,
        help=(
            "exact instance_type_name reported by the Lambda API; required for "
            "Qwen3 campaign capture"
        ),
    )
    parser.add_argument(
        "--lambda-hourly-rate-usd",
        type=lambda_gpu_guard.parse_hourly_rate,
        help="displayed Lambda hourly rate; checked against the API",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=lambda_gpu_guard.parse_max_cost,
        help="operator spend budget used for the buffered termination deadline",
    )
    parser.add_argument(
        "--lambda-billing-started-at",
        type=lambda_gpu_guard.parse_utc_timestamp,
        help="required actual provider billing start in ISO 8601",
    )
    parser.add_argument(
        "--lambda-guard-state-root",
        default=str(Path.home() / ".inferdrome" / "lambda-guards"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        _static_check()
        if args.resume_qwen3_finalization is not None:
            if (
                args.destination is not None
                or args.check
                or args.dry_run
                or args.managed_capability_profile is not None
                or args.qwen3_gpu_tier is not None
                or args.identity_file is not None
                or args.host_key_sha256 is not None
                or _lambda_guard_requested(args)
            ):
                raise RemoteCaptureError(
                    "offline Qwen3 finalization does not accept capture "
                    "or cloud options"
                )
            if args.expected_commit is None:
                raise RemoteCaptureError(
                    "offline Qwen3 finalization requires --expected-commit"
                )
            if args.lambda_guard_state_directory is None:
                raise RemoteCaptureError(
                    "offline Qwen3 finalization requires the Lambda guard "
                    "state directory"
                )
            commit = _require_checkout(args.expected_commit)
            _resume_qwen3_finalization(
                Path(args.resume_qwen3_finalization),
                commit=commit,
                guard_state_directory=Path(args.lambda_guard_state_directory),
            )
            return 0
        if args.lambda_guard_state_directory is not None:
            raise RemoteCaptureError(
                "--lambda-guard-state-directory requires offline Qwen3 finalization"
            )
        if args.check:
            if (
                args.destination is not None
                or args.dry_run
                or args.managed_capability_profile is not None
                or args.qwen3_gpu_tier is not None
                or args.lambda_instance_type_name is not None
                or _lambda_guard_requested(args)
            ):
                raise RemoteCaptureError(
                    "--check does not accept a destination, --dry-run, profile, "
                    "or Lambda guard"
                )
            print("real-GPU remote capture assets: OK")
            return 0
        if args.destination is None:
            raise RemoteCaptureError("an SSH destination is required")
        _validate_capture_mode(args)
        identity = _require_identity(args.identity_file)
        commit = _require_checkout(args.expected_commit)
        if args.dry_run:
            _dry_run(args, commit, identity)
        else:
            _capture(args, commit, identity)
    except RemoteCaptureError as error:
        print(f"remote-real-gpu-capture: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
