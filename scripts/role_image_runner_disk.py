#!/usr/bin/env python3
"""Observe role-image runner disk capacity and reclaim one fixed SDK path."""

from __future__ import annotations

import argparse
import os
import platform
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

ANDROID_SDK_TARGET = Path("/usr/local/lib/android/sdk")
FILESYSTEM_ROOT = Path("/")
_DOCKER_DF = ("docker", "system", "df")
_DOCKER_ROOT = ("docker", "info", "--format", "{{.DockerRootDir}}")


class RoleImageRunnerDiskError(RuntimeError):
    """An observation or narrowly scoped SDK reclamation is unsafe."""


def _require_hosted_linux(
    runner_os: str,
    runner_environment: str,
    host_platform: Callable[[], str] = platform.system,
) -> None:
    if host_platform() != "Linux":
        raise RoleImageRunnerDiskError("cleanup requires an actual Linux host platform")
    if runner_os != "Linux" or runner_environment != "github-hosted":
        raise RoleImageRunnerDiskError("cleanup requires a GitHub-hosted Linux runner")


def _safe_sdk_directory(
    *, target: Path, expected: Path, root: Path, emit: Callable[[str], None]
) -> bool:
    if target != expected or not target.is_absolute() or not root.is_absolute():
        raise RoleImageRunnerDiskError("Android SDK cleanup target is not exact")
    try:
        components = target.relative_to(root).parts
        root_status = root.lstat()
    except (OSError, ValueError) as error:
        raise RoleImageRunnerDiskError("Android SDK cleanup root is unsafe") from error
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise RoleImageRunnerDiskError("Android SDK cleanup root is unsafe")

    current = root
    for component in components:
        current /= component
        try:
            status = current.lstat()
        except FileNotFoundError:
            emit(f"Android SDK target absent; no cleanup performed: {target}")
            return False
        except OSError as error:
            raise RoleImageRunnerDiskError(
                "Android SDK cleanup target cannot be inspected"
            ) from error
        if stat.S_ISLNK(status.st_mode):
            raise RoleImageRunnerDiskError(
                "Android SDK cleanup target contains a symbolic link"
            )
        if not stat.S_ISDIR(status.st_mode):
            raise RoleImageRunnerDiskError(
                "Android SDK cleanup target contains a non-directory component"
            )
    try:
        canonical = (
            root.resolve(strict=True) == root
            and target.resolve(strict=True) == expected
        )
    except OSError as error:
        raise RoleImageRunnerDiskError(
            "Android SDK cleanup target canonical identity cannot be checked"
        ) from error
    if not canonical:
        raise RoleImageRunnerDiskError(
            "Android SDK cleanup target canonical identity is unsafe"
        )
    return True


def _removal_command(target: Path) -> tuple[str, ...]:
    return (
        "sudo",
        "--non-interactive",
        "rm",
        "-rf",
        "--one-file-system",
        "--",
        str(target),
    )


def reclaim_android_sdk(
    *,
    runner_os: str,
    runner_environment: str,
    run_command: Callable[[Sequence[str]], None],
    target: Path = ANDROID_SDK_TARGET,
    expected: Path = ANDROID_SDK_TARGET,
    root: Path = FILESYSTEM_ROOT,
    emit: Callable[[str], None] = print,
    host_platform: Callable[[], str] = platform.system,
) -> bool:
    _require_hosted_linux(runner_os, runner_environment, host_platform)
    if not _safe_sdk_directory(target=target, expected=expected, root=root, emit=emit):
        return False
    run_command(_removal_command(target))
    emit(f"Android SDK target removed: {target}")
    return True


def filesystem_free_bytes(path: Path) -> int:
    if not path.is_absolute() or not path.is_dir():
        raise RoleImageRunnerDiskError(
            f"filesystem observation path is unavailable: {path}"
        )
    try:
        status = os.statvfs(path)
    except OSError as error:
        raise RoleImageRunnerDiskError(
            f"filesystem free bytes cannot be observed for {path}"
        ) from error
    return status.f_bavail * status.f_frsize


def _docker_root_directory(capture_command: Callable[[Sequence[str]], str]) -> Path:
    value = capture_command(_DOCKER_ROOT).strip()
    if not value or "\n" in value or not Path(value).is_absolute():
        raise RoleImageRunnerDiskError("DockerRootDir observation is invalid")
    return Path(value)


def _record_free_bytes(
    paths: tuple[tuple[str, Path], ...],
    phase: str,
    free_bytes: Callable[[Path], int],
    emit: Callable[[str], None],
) -> tuple[int, ...]:
    values = tuple(free_bytes(path) for _, path in paths)
    for (label, path), value in zip(paths, values, strict=True):
        emit(f"- {label} filesystem `{path}` available bytes {phase}: {value}")
    return values


def observe_runner_capacity(
    *,
    runner_os: str,
    runner_environment: str,
    runner_temp: Path,
    run_command: Callable[[Sequence[str]], None],
    capture_command: Callable[[Sequence[str]], str],
    phase: str,
    free_bytes: Callable[[Path], int] = filesystem_free_bytes,
    emit: Callable[[str], None] = print,
    host_platform: Callable[[], str] = platform.system,
) -> tuple[tuple[tuple[str, Path], ...], tuple[int, ...]]:
    _require_hosted_linux(runner_os, runner_environment, host_platform)
    docker_root = _docker_root_directory(capture_command)
    paths = (
        ("Runner root", FILESYSTEM_ROOT),
        ("Docker root", docker_root),
        ("RUNNER_TEMP", runner_temp),
    )
    emit("## Role-image runner capacity observations")
    emit(f"- DockerRootDir: {docker_root}")
    values = _record_free_bytes(paths, phase, free_bytes, emit)
    emit(f"- Docker storage report {phase}:")
    run_command(_DOCKER_DF)
    return paths, values


def observe_and_reclaim_android_sdk(
    *,
    runner_os: str,
    runner_environment: str,
    runner_temp: Path,
    run_command: Callable[[Sequence[str]], None],
    capture_command: Callable[[Sequence[str]], str],
    free_bytes: Callable[[Path], int] = filesystem_free_bytes,
    emit: Callable[[str], None] = print,
    target: Path = ANDROID_SDK_TARGET,
    expected: Path = ANDROID_SDK_TARGET,
    root: Path = FILESYSTEM_ROOT,
    host_platform: Callable[[], str] = platform.system,
) -> None:
    paths, before = observe_runner_capacity(
        runner_os=runner_os,
        runner_environment=runner_environment,
        runner_temp=runner_temp,
        run_command=run_command,
        capture_command=capture_command,
        phase="before SDK reclamation",
        free_bytes=free_bytes,
        emit=emit,
        host_platform=host_platform,
    )
    reclaim_android_sdk(
        runner_os=runner_os,
        runner_environment=runner_environment,
        run_command=run_command,
        target=target,
        expected=expected,
        root=root,
        emit=emit,
        host_platform=host_platform,
    )
    after = _record_free_bytes(paths, "after SDK reclamation", free_bytes, emit)
    for (label, _), prior, observed in zip(paths, before, after, strict=True):
        emit(
            f"- {label} filesystem available-byte delta after SDK reclamation: "
            f"{observed - prior} (observed delta only; concurrent runner writes mean "
            "this is not a guaranteed reclaimed-byte count or a build-fit guarantee)."
        )


def _run_command(command: Sequence[str]) -> None:
    subprocess.run(list(command), check=True)


def _capture_command(command: Sequence[str]) -> str:
    completed = subprocess.run(
        list(command), capture_output=True, check=False, text=True
    )
    if completed.returncode:
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        raise subprocess.CalledProcessError(completed.returncode, list(command))
    return completed.stdout


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-os", required=True)
    parser.add_argument("--runner-environment", required=True)
    parser.add_argument("--runner-temp", required=True)
    parser.add_argument("--diagnose-only", action="store_true")
    parsed = parser.parse_args(arguments)
    command_args = {
        "runner_os": parsed.runner_os,
        "runner_environment": parsed.runner_environment,
        "runner_temp": Path(parsed.runner_temp),
        "run_command": _run_command,
        "capture_command": _capture_command,
    }
    try:
        if parsed.diagnose_only:
            observe_runner_capacity(phase="after failed build", **command_args)
        else:
            observe_and_reclaim_android_sdk(**command_args)
    except RoleImageRunnerDiskError as error:
        print(
            f"role-image runner capacity preparation failed: {error}",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as error:
        print(
            "role-image runner capacity command failed with exit status "
            f"{error.returncode}",
            file=sys.stderr,
        )
        return error.returncode if error.returncode > 0 else 1
    except OSError as error:
        print(
            f"role-image runner capacity command could not run: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
