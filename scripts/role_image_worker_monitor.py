#!/usr/bin/env python3
"""Observe one dedicated role-image worker while it builds and CPU-smokes.

This helper is deliberately observation-only.  It never reclaims host paths,
prunes Docker storage, authenticates, publishes, or creates cloud resources.
The manually dispatched workflow supplies a fixed build command after this
module has fail-closed on its source, worker, and mode contract.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NoReturn, Protocol

_SCHEMA_VERSION = "inferdrome.role-image-worker-observation.v1"
_EXPECTED_REPOSITORY = "jayeshsuyal/inferdrome"
_EXPECTED_REF = "refs/heads/main"
_ALLOWED_MODES = frozenset({"BUILD_AND_SMOKE_ONLY", "PUBLISH_FIXED_ROLE_IMAGES"})
_SHA256 = re.compile(r"^[0-9a-f]{40}$")
_RUNNER_ALIAS = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
_STORAGE_DRIVER = re.compile(r"^[A-Za-z0-9_.+-]{1,128}$")
_MAX_CAPTURE_BYTES = 4_096
_MAX_DURING_SAMPLES = 180
_DOCKER_VERSION = ("docker", "version", "--format", "{{.Server.Version}}")
_BUILDX_VERSION = ("docker", "buildx", "version")
_DOCKER_INFO = ("docker", "info", "--format", "{{.Driver}}\t{{.DockerRootDir}}")
_DOCKER_DF = (
    "docker",
    "system",
    "df",
    "--format",
    "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}",
)


class RoleImageWorkerMonitorError(RuntimeError):
    """A worker identity or capacity observation cannot be trusted."""


class _RunningCommand(Protocol):
    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...


CaptureCommand = Callable[[Sequence[str]], str]
FilesystemUsage = Callable[[Path], Mapping[str, object]]
PopenCommand = Callable[[Sequence[str]], _RunningCommand]


def _fail(message: str) -> NoReturn:
    raise RoleImageWorkerMonitorError(message)


def validate_worker_inputs(
    *,
    mode: str,
    source_commit: str,
    github_repository: str,
    github_ref: str,
    github_sha: str,
    runner_os: str,
    runner_arch: str,
    runner_environment: str,
    runner_name: str,
    expected_runner_name: str,
) -> None:
    """Fail closed before Docker inspection or child-process creation."""

    if mode not in _ALLOWED_MODES:
        _fail("worker mode is not approved")
    if not _SHA256.fullmatch(source_commit) or source_commit != github_sha:
        _fail("source commit does not bind the dispatched revision")
    if github_repository != _EXPECTED_REPOSITORY or github_ref != _EXPECTED_REF:
        _fail("repository or ref is outside the reviewed-main-only boundary")
    if (
        runner_os != "Linux"
        or runner_arch != "X64"
        or runner_environment != "self-hosted"
    ):
        _fail("runner platform is not the dedicated Linux X64 self-hosted worker")
    if (
        _RUNNER_ALIAS.fullmatch(expected_runner_name) is None
        or runner_name != expected_runner_name
    ):
        _fail("runner name does not match the required non-sensitive worker alias")


def _bounded_scalar(value: str, *, field: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized.encode("utf-8")) > _MAX_CAPTURE_BYTES
        or "\n" in normalized
        or "\r" in normalized
    ):
        _fail(f"{field} observation is invalid")
    return normalized


def _bounded_report(value: str, *, field: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized.encode("utf-8")) > _MAX_CAPTURE_BYTES
        or "\r" in normalized
        or len(normalized.splitlines()) > 16
    ):
        _fail(f"{field} observation is invalid")
    return normalized


def _capture_subprocess(command: Sequence[str]) -> str:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        _fail(f"required worker observation failed: {' '.join(command[:2])}")
    return completed.stdout


def _filesystem_usage(path: Path) -> Mapping[str, object]:
    if not path.is_absolute():
        _fail("filesystem observation path is not absolute")
    try:
        canonical = Path(os.path.realpath(path))
        status = os.stat(canonical)
        capacity = os.statvfs(canonical)
    except OSError:
        _fail("filesystem observation path is unavailable")
    if (
        capacity.f_frsize <= 0
        or capacity.f_blocks < 0
        or capacity.f_bavail < 0
        or capacity.f_files < 0
        or capacity.f_favail < 0
    ):
        _fail("filesystem observation is invalid")
    return {
        "available_bytes": capacity.f_bavail * capacity.f_frsize,
        "available_inodes": capacity.f_favail,
        "canonical_path": str(canonical),
        "capacity_bytes": capacity.f_blocks * capacity.f_frsize,
        "device": status.st_dev,
        "inode_capacity": capacity.f_files,
    }


def _docker_storage(capture_command: CaptureCommand) -> tuple[str, Path]:
    raw = _bounded_scalar(capture_command(_DOCKER_INFO), field="Docker storage")
    fields = raw.split("\t")
    if len(fields) != 2 or _STORAGE_DRIVER.fullmatch(fields[0]) is None:
        _fail("Docker storage observation is invalid")
    root = Path(fields[1])
    if not root.is_absolute():
        _fail("Docker storage root observation is invalid")
    return fields[0], root


def _filesystem_observations(
    *,
    docker_root: Path,
    runner_temp: Path,
    workspace: Path,
    filesystem_usage: FilesystemUsage,
) -> dict[str, Mapping[str, object]]:
    paths = {
        "docker_root": docker_root,
        "runner_root": Path("/"),
        "runner_temp": runner_temp,
        "workspace": workspace,
    }
    return {name: dict(filesystem_usage(path)) for name, path in paths.items()}


def collect_worker_observation(
    *,
    runner_temp: Path,
    workspace: Path,
    capture_command: CaptureCommand = _capture_subprocess,
    filesystem_usage: FilesystemUsage = _filesystem_usage,
) -> Mapping[str, object]:
    """Collect one bounded point-in-time worker observation without mutation."""

    storage_driver, docker_root = _docker_storage(capture_command)
    return {
        "docker": {
            "buildx_version": _bounded_scalar(
                capture_command(_BUILDX_VERSION), field="Docker Buildx version"
            ),
            "server_version": _bounded_scalar(
                capture_command(_DOCKER_VERSION), field="Docker server version"
            ),
            "storage_driver": storage_driver,
            "storage_report": _bounded_report(
                capture_command(_DOCKER_DF), field="Docker storage report"
            ),
        },
        "filesystems": _filesystem_observations(
            docker_root=docker_root,
            runner_temp=runner_temp,
            workspace=workspace,
            filesystem_usage=filesystem_usage,
        ),
    }


def _popen_subprocess(command: Sequence[str]) -> _RunningCommand:
    return subprocess.Popen(list(command))


def _assert_same_filesystems(
    first: Mapping[str, object], current: Mapping[str, object]
) -> None:
    first_filesystems = first.get("filesystems")
    current_filesystems = current.get("filesystems")
    if not isinstance(first_filesystems, Mapping) or not isinstance(
        current_filesystems, Mapping
    ):
        _fail("filesystem observation is invalid")
    if set(first_filesystems) != set(current_filesystems):
        _fail("filesystem mapping changed while the build was running")
    for name in first_filesystems:
        expected = first_filesystems[name]
        observed = current_filesystems[name]
        if not isinstance(expected, Mapping) or not isinstance(observed, Mapping):
            _fail("filesystem observation is invalid")
        if (
            expected.get("canonical_path") != observed.get("canonical_path")
            or expected.get("device") != observed.get("device")
        ):
            _fail("filesystem mapping changed while the build was running")


def _sampled_minima(
    samples: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, int]]:
    if not samples:
        _fail("worker observation has no filesystem samples")
    names: set[str] | None = None
    minima: dict[str, dict[str, int]] = {}
    for sample in samples:
        filesystems = sample.get("filesystems")
        if not isinstance(filesystems, Mapping):
            _fail("worker observation is invalid")
        current_names = set(filesystems)
        if names is None:
            names = current_names
            minima = {
                name: {
                    "minimum_available_bytes_observed": sys.maxsize,
                    "minimum_available_inodes_observed": sys.maxsize,
                }
                for name in current_names
            }
        elif names != current_names:
            _fail("filesystem mapping changed while the build was running")
        for name, filesystem in filesystems.items():
            if not isinstance(filesystem, Mapping):
                _fail("worker observation is invalid")
            available_bytes = filesystem.get("available_bytes")
            available_inodes = filesystem.get("available_inodes")
            if not isinstance(available_bytes, int) or not isinstance(
                available_inodes, int
            ):
                _fail("worker observation is invalid")
            minima[name]["minimum_available_bytes_observed"] = min(
                minima[name]["minimum_available_bytes_observed"], available_bytes
            )
            minima[name]["minimum_available_inodes_observed"] = min(
                minima[name]["minimum_available_inodes_observed"], available_inodes
            )
    return minima


def _terminate_and_wait(process: _RunningCommand) -> int:
    process.terminate()
    try:
        return process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _fail("worker build did not stop within the bounded monitor shutdown window")


def run_observed_build(
    *,
    mode: str,
    source_commit: str,
    github_repository: str,
    github_ref: str,
    github_sha: str,
    runner_os: str,
    runner_arch: str,
    runner_environment: str,
    runner_name: str,
    expected_runner_name: str,
    runner_temp: Path,
    workspace: Path,
    build_command: Sequence[str],
    interval_seconds: int,
    max_during_samples: int,
    build_deadline_seconds: int = 2_550,
    capture_command: CaptureCommand = _capture_subprocess,
    filesystem_usage: FilesystemUsage = _filesystem_usage,
    popen_command: PopenCommand = _popen_subprocess,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[Mapping[str, object], int]:
    """Run one fixed build command while retaining bounded capacity observations."""

    validate_worker_inputs(
        mode=mode,
        source_commit=source_commit,
        github_repository=github_repository,
        github_ref=github_ref,
        github_sha=github_sha,
        runner_os=runner_os,
        runner_arch=runner_arch,
        runner_environment=runner_environment,
        runner_name=runner_name,
        expected_runner_name=expected_runner_name,
    )
    if not build_command:
        _fail("worker build command is missing")
    if interval_seconds < 1 or interval_seconds > 60:
        _fail("worker observation interval is outside the bounded range")
    if max_during_samples < 1 or max_during_samples > _MAX_DURING_SAMPLES:
        _fail("worker observation sample limit is outside the bounded range")
    if build_deadline_seconds < 1 or build_deadline_seconds > 2_550:
        _fail("worker build deadline is outside the bounded range")

    before = collect_worker_observation(
        runner_temp=runner_temp,
        workspace=workspace,
        capture_command=capture_command,
        filesystem_usage=filesystem_usage,
    )
    samples: list[Mapping[str, object]] = [
        {"filesystems": before["filesystems"], "phase": "before"}
    ]
    process: _RunningCommand | None = None
    timed_out = False
    try:
        process = popen_command(build_command)
        deadline = monotonic() + build_deadline_seconds
        for _ in range(max_during_samples):
            if process.poll() is not None:
                break
            during_filesystems = _filesystem_observations(
                docker_root=Path(
                    str(before["filesystems"]["docker_root"]["canonical_path"])
                ),
                runner_temp=runner_temp,
                workspace=workspace,
                filesystem_usage=filesystem_usage,
            )
            _assert_same_filesystems(
                before, {"filesystems": during_filesystems}
            )
            samples.append(
                {
                    "filesystems": during_filesystems,
                    "phase": "during",
                }
            )
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sleep(min(float(interval_seconds), remaining))
        remaining = deadline - monotonic()
        if process.poll() is None and remaining > 0:
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                returncode = _terminate_and_wait(process)
                timed_out = True
        elif process.poll() is None:
            returncode = _terminate_and_wait(process)
            timed_out = True
        else:
            returncode = process.wait(timeout=0)
        after = collect_worker_observation(
            runner_temp=runner_temp,
            workspace=workspace,
            capture_command=capture_command,
            filesystem_usage=filesystem_usage,
        )
    finally:
        if process is not None and process.poll() is None:
            _terminate_and_wait(process)

    _assert_same_filesystems(before, after)
    samples.append({"filesystems": after["filesystems"], "phase": "after"})
    return (
        {
            "build_exit_code": returncode,
            "build_timed_out": timed_out,
            "build_deadline_seconds": build_deadline_seconds,
            "during_sample_limit": max_during_samples,
            "execution": {
                "mode": mode,
                "runner_alias": runner_name,
                "source_commit": source_commit,
            },
            "observations": {
                "after": after,
                "before": before,
                "during": [
                    sample["filesystems"]
                    for sample in samples
                    if sample["phase"] == "during"
                ],
            },
            "sample_count": len(samples),
            "sampled_minimum_available": _sampled_minima(samples),
            "schema_version": _SCHEMA_VERSION,
        },
        returncode,
    )


def _canonical_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError):
        _fail("worker observation cannot be canonicalized")


def _write_create_no_replace(path: Path, content: bytes) -> None:
    if not path.is_absolute():
        _fail("worker observation output path is not absolute")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        _fail("worker observation output cannot be created")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        _fail("worker observation output cannot be written")


def _append_summary(path: Path, record: Mapping[str, object]) -> None:
    if not path.is_absolute():
        _fail("job summary path is not absolute")
    execution = record["execution"]
    observations = record["observations"]
    minima = record["sampled_minimum_available"]
    assert isinstance(execution, Mapping)
    assert isinstance(observations, Mapping)
    assert isinstance(minima, Mapping)
    before = observations["before"]
    assert isinstance(before, Mapping)
    docker = before["docker"]
    assert isinstance(docker, Mapping)
    lines = [
        "## Dedicated role-image worker observations",
        "",
        f"- Mode: `{execution['mode']}`.",
        f"- Worker alias: `{execution['runner_alias']}`.",
        f"- Docker server: `{docker['server_version']}`; "
        f"Buildx: `{docker['buildx_version']}`.",
        f"- Docker storage driver: `{docker['storage_driver']}`.",
        f"- Bounded capacity samples: `{record['sample_count']}` of at most "
        f"`{record['during_sample_limit'] + 2}` including before/after.",
        "- Sampled minima are observations, not an exact peak, fit guarantee, "
        "or admission threshold.",
        "",
    ]
    for name in sorted(minima):
        minimum = minima[name]
        assert isinstance(minimum, Mapping)
        lines.append(
            f"- `{name}` minimum observed available bytes/inodes: "
            f"`{minimum['minimum_available_bytes_observed']}` / "
            f"`{minimum['minimum_available_inodes_observed']}`."
        )
    lines.extend(
        [
            f"- Build exit code: `{record['build_exit_code']}`.",
            "- This worker monitor did not delete host content, prune Docker, "
            "authenticate, or publish.",
            "",
        ]
    )
    try:
        with path.open("a", encoding="utf-8") as summary:
            summary.write("\n".join(lines))
    except OSError:
        _fail("job summary cannot be written")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--github-repository", required=True)
    parser.add_argument("--github-ref", required=True)
    parser.add_argument("--github-sha", required=True)
    parser.add_argument("--runner-os", required=True)
    parser.add_argument("--runner-arch", required=True)
    parser.add_argument("--runner-environment", required=True)
    parser.add_argument("--runner-name", required=True)
    parser.add_argument("--expected-runner-name", required=True)
    parser.add_argument("--runner-temp", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--interval-seconds", type=int, default=15)
    parser.add_argument("--max-during-samples", type=int, default=180)
    parser.add_argument("--build-deadline-seconds", type=int, default=2550)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("build_command", nargs=argparse.REMAINDER)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    build_command = list(parsed.build_command)
    if build_command[:1] == ["--"]:
        build_command = build_command[1:]
    try:
        record, returncode = run_observed_build(
            mode=parsed.mode,
            source_commit=parsed.source_commit,
            github_repository=parsed.github_repository,
            github_ref=parsed.github_ref,
            github_sha=parsed.github_sha,
            runner_os=parsed.runner_os,
            runner_arch=parsed.runner_arch,
            runner_environment=parsed.runner_environment,
            runner_name=parsed.runner_name,
            expected_runner_name=parsed.expected_runner_name,
            runner_temp=parsed.runner_temp,
            workspace=parsed.workspace,
            build_command=build_command,
            interval_seconds=parsed.interval_seconds,
            max_during_samples=parsed.max_during_samples,
            build_deadline_seconds=parsed.build_deadline_seconds,
        )
        _write_create_no_replace(parsed.output, _canonical_bytes(record))
        _append_summary(parsed.summary, record)
        return returncode
    except RoleImageWorkerMonitorError as error:
        print(f"role-image worker observation failed: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"role-image worker observation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
