#!/usr/bin/env python3
"""Build the runner image with a verified proof/release context."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PLATFORM = "linux/amd64"
RELEVANT_BUILD_INPUTS = (
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "src",
)
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,126}[A-Za-z0-9]$")


class RunnerImageBuildError(RuntimeError):
    """A bounded proof/release build precondition or Docker failure."""


def _capture(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(command),
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        raise RunnerImageBuildError(
            "required local build command is unavailable"
        ) from None
    if completed.returncode != 0:
        raise RunnerImageBuildError("required local build command failed")
    return completed.stdout.strip()


def _require_clean_relevant_inputs() -> None:
    status = _capture(
        (
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *RELEVANT_BUILD_INPUTS,
        )
    )
    if status:
        raise RunnerImageBuildError("proof/release build inputs are not clean")


def _source_commit() -> str:
    value = _capture(("git", "rev-parse", "--verify", "HEAD^{commit}"))
    if _COMMIT_PATTERN.fullmatch(value) is None:
        raise RunnerImageBuildError("source commit identity is invalid")
    return value


def _package_version() -> str:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from inferdrome import __version__; print(__version__)",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        raise RunnerImageBuildError("package version cannot be resolved") from None
    if completed.returncode != 0:
        raise RunnerImageBuildError("package version cannot be resolved")
    value = completed.stdout.strip()
    if _VERSION_PATTERN.fullmatch(value) is None or len(value) > 64:
        raise RunnerImageBuildError("package version identity is invalid")
    return value


def require_packaged_version(expected: str, packaged: str) -> None:
    """Fail closed when a requested image version differs from package code."""

    if expected != packaged:
        raise RunnerImageBuildError(
            "packaged Inferdrome version does not match identity"
        )


def _validate_tag(tag: str) -> None:
    if len(tag) > 128 or _TAG_PATTERN.fullmatch(tag) is None:
        raise RunnerImageBuildError("image tag is invalid")


def build_image(
    *,
    flavor: str = "development",
    platform: str = CANONICAL_PLATFORM,
    tag: str = "inferdrome-runner:development",
) -> tuple[str, ...]:
    if flavor not in {"development", "proof", "release"}:
        raise RunnerImageBuildError("unsupported build flavor")
    if platform != CANONICAL_PLATFORM:
        raise RunnerImageBuildError("runner image target platform must be linux/amd64")
    _validate_tag(tag)

    source_commit: str | None = None
    package_version: str | None = None
    if flavor in {"proof", "release"}:
        _require_clean_relevant_inputs()
        source_commit = _source_commit()
        package_version = _package_version()

    if shutil.which("docker") is None:
        raise RunnerImageBuildError("Docker is unavailable")

    command = [
        "docker",
        "build",
        "--platform",
        CANONICAL_PLATFORM,
        "--pull=false",
        "--build-arg",
        f"BUILD_FLAVOR={flavor}",
    ]
    if source_commit is not None and package_version is not None:
        command.extend(
            [
                "--build-arg",
                f"SOURCE_REPOSITORY_COMMIT={source_commit}",
                "--build-arg",
                f"INFERDROME_VERSION={package_version}",
            ]
        )
    command.extend(["-t", tag, "."])
    try:
        completed = subprocess.run(command, cwd=REPOSITORY_ROOT, check=False)
    except OSError:
        raise RunnerImageBuildError("Docker build could not be started") from None
    if completed.returncode != 0:
        raise RunnerImageBuildError("Docker build failed")
    return tuple(command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_runner_image",
        description="Build the Inferdrome runner image for the canonical target",
    )
    parser.add_argument(
        "--flavor",
        choices=("development", "proof", "release"),
        default="development",
    )
    parser.add_argument("--platform", default=CANONICAL_PLATFORM)
    parser.add_argument("--tag", default="inferdrome-runner:development")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report wrapper and Docker availability without building",
    )
    arguments = parser.parse_args(argv)
    if arguments.check:
        docker_status = "available" if shutil.which("docker") else "unavailable"
        print(
            "runner build wrapper: canonical platform linux/amd64; "
            f"Docker execution gate: {docker_status}"
        )
        return 0
    try:
        build_image(
            flavor=arguments.flavor,
            platform=arguments.platform,
            tag=arguments.tag,
        )
    except RunnerImageBuildError as error:
        print(f"runner image build failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
