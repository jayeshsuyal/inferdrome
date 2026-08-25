#!/usr/bin/env python3
"""Build the runner image with a verified proof/release context."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PLATFORM = "linux/amd64"
RELEVANT_BUILD_INPUTS = (
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "src",
    # The wrapper is a trust-root input. It is checked for cleanliness but is
    # deliberately not copied into the Docker build context.
    "scripts/build_runner_image.py",
)
ARCHIVE_BUILD_INPUTS = (
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "src",
)
VLLM_RELEVANT_BUILD_INPUTS = (
    "Dockerfile.vllm-benchmark-runner",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "src",
    # The wrapper is checked as a trust-root input but is not archived.
    "scripts/build_runner_image.py",
)
VLLM_ARCHIVE_BUILD_INPUTS = (
    "Dockerfile.vllm-benchmark-runner",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "src",
)
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:[.-][0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
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


def _require_clean_relevant_inputs(
    inputs: Sequence[str] | None = None,
) -> None:
    selected_inputs = RELEVANT_BUILD_INPUTS if inputs is None else inputs
    status = _capture(
        (
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            "--",
            *selected_inputs,
        )
    )
    if status:
        raise RunnerImageBuildError("proof/release build inputs are not clean")


def _archive_head(
    archive_path: Path,
    source_commit: str,
    inputs: Sequence[str] | None = None,
) -> None:
    """Materialize allowlisted bytes from the labeled commit into ``archive_path``."""

    selected_inputs = ARCHIVE_BUILD_INPUTS if inputs is None else inputs

    try:
        completed = subprocess.run(
            [
                "git",
                "archive",
                "--format=tar",
                source_commit,
                "--",
                *selected_inputs,
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
        )
    except OSError:
        raise RunnerImageBuildError(
            "exact proof build context cannot be created"
        ) from None
    if completed.returncode != 0 or not isinstance(completed.stdout, bytes):
        raise RunnerImageBuildError("exact proof build context cannot be created")
    try:
        with archive_path.open("wb") as output:
            output.write(completed.stdout)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        raise RunnerImageBuildError(
            "exact proof build context cannot be created"
        ) from None


def _safe_archive_member(member: tarfile.TarInfo, context: Path) -> Path:
    """Return a safe extraction path, rejecting links and traversal."""

    member_path = PurePosixPath(member.name)
    if (
        member_path.is_absolute()
        or not member_path.parts
        or any(part in {"", ".", ".."} for part in member_path.parts)
        or member.issym()
        or member.islnk()
        or not (member.isdir() or member.isreg())
    ):
        raise RunnerImageBuildError("proof build archive contains an unsafe member")
    destination = (context / Path(*member_path.parts)).resolve()
    try:
        if os.path.commonpath((str(context.resolve()), str(destination))) != str(
            context.resolve()
        ):
            raise RunnerImageBuildError("proof build archive contains an unsafe member")
    except ValueError:
        raise RunnerImageBuildError(
            "proof build archive contains an unsafe member"
        ) from None
    return destination


def _extract_archive(archive_path: Path, context: Path) -> None:
    context.mkdir()
    seen: set[Path] = set()
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            for member in archive:
                destination = _safe_archive_member(member, context)
                if destination in seen:
                    raise RunnerImageBuildError(
                        "proof build archive contains duplicate members"
                    )
                seen.add(destination)
                if member.isdir():
                    destination.mkdir()
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as output:
                    source = archive.extractfile(member)
                    if source is None:
                        raise RunnerImageBuildError(
                            "proof build archive contains an invalid file"
                        )
                    with source:
                        shutil.copyfileobj(source, output)
    except RunnerImageBuildError:
        raise
    except (OSError, tarfile.TarError):
        raise RunnerImageBuildError(
            "proof build archive cannot be safely extracted"
        ) from None


@contextlib.contextmanager
def _materialize_proof_context(
    source_commit: str,
    archive_inputs: Sequence[str] | None = None,
) -> Iterator[Path]:
    """Yield a temporary context containing exact allowlisted labeled-commit bytes."""

    try:
        with tempfile.TemporaryDirectory(
            prefix="inferdrome-runner-context-"
        ) as temporary:
            temporary_root = Path(temporary)
            archive_path = temporary_root / "head.tar"
            context = temporary_root / "context"
            _archive_head(archive_path, source_commit, archive_inputs)
            _extract_archive(archive_path, context)
            yield context
    except RunnerImageBuildError:
        raise
    except (OSError, RuntimeError):
        raise RunnerImageBuildError("proof build context cleanup failed") from None


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


def _run_docker_build(command: list[str]) -> None:
    try:
        completed = subprocess.run(command, cwd=REPOSITORY_ROOT, check=False)
    except OSError:
        raise RunnerImageBuildError("Docker build could not be started") from None
    if completed.returncode != 0:
        raise RunnerImageBuildError("Docker build failed")


def build_image(
    *,
    flavor: str = "development",
    platform: str = CANONICAL_PLATFORM,
    tag: str | None = None,
    image_kind: str = "runner",
) -> tuple[str, ...]:
    if flavor not in {"development", "proof", "release"}:
        raise RunnerImageBuildError("unsupported build flavor")
    if platform != CANONICAL_PLATFORM:
        raise RunnerImageBuildError("runner image target platform must be linux/amd64")
    if image_kind == "runner":
        dockerfile = "Dockerfile"
        relevant_inputs = RELEVANT_BUILD_INPUTS
        archive_inputs = ARCHIVE_BUILD_INPUTS
        default_tag = "inferdrome-runner:development"
    elif image_kind == "vllm-benchmark-runner":
        dockerfile = "Dockerfile.vllm-benchmark-runner"
        relevant_inputs = VLLM_RELEVANT_BUILD_INPUTS
        archive_inputs = VLLM_ARCHIVE_BUILD_INPUTS
        default_tag = "inferdrome-vllm-benchmark-runner:development"
    else:
        raise RunnerImageBuildError("unsupported runner image kind")
    selected_tag = default_tag if tag is None else tag
    _validate_tag(selected_tag)

    source_commit: str | None = None
    package_version: str | None = None
    if flavor in {"proof", "release"}:
        if image_kind == "runner":
            _require_clean_relevant_inputs()
        else:
            _require_clean_relevant_inputs(relevant_inputs)
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
        "--file",
        dockerfile,
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
    command.extend(["-t", selected_tag])
    if flavor in {"proof", "release"}:
        if image_kind == "runner":
            context_manager = _materialize_proof_context(source_commit)
        else:
            context_manager = _materialize_proof_context(
                source_commit,
                archive_inputs=archive_inputs,
            )
        with context_manager as context:
            proof_command = [*command, str(context)]
            _run_docker_build(proof_command)
            return tuple(proof_command)

    development_command = [*command, str(REPOSITORY_ROOT)]
    _run_docker_build(development_command)
    return tuple(development_command)


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
    parser.add_argument("--tag")
    parser.add_argument(
        "--image-kind",
        choices=("runner", "vllm-benchmark-runner"),
        default="runner",
    )
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
            image_kind=arguments.image_kind,
        )
    except RunnerImageBuildError as error:
        print(f"runner image build failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
