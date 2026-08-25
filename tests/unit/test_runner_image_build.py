"""Clean-context runner image wrapper coverage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import scripts.build_runner_image as builder


def test_proof_build_rejects_dirty_relevant_inputs_without_echoing_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dirty_marker = "sk-arbitrary-secret-shaped-status"
    monkeypatch.setattr(builder, "_capture", lambda _command: dirty_marker)

    with pytest.raises(builder.RunnerImageBuildError) as exc_info:
        builder.build_image(flavor="proof")

    assert "clean" in str(exc_info.value)
    assert dirty_marker not in str(exc_info.value)


def test_proof_build_derives_commit_version_and_canonical_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_commit = "a" * 40
    captures: list[tuple[str, ...]] = []

    def capture(command: tuple[str, ...]) -> str:
        captures.append(command)
        return "" if command[1] == "status" else source_commit

    docker_commands: list[list[str]] = []
    monkeypatch.setattr(builder, "_capture", capture)
    monkeypatch.setattr(builder, "_package_version", lambda: "0.1.0.dev0")
    monkeypatch.setattr(builder.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda command, **_kwargs: (
            docker_commands.append(list(command)) or SimpleNamespace(returncode=0)
        ),
    )

    command = builder.build_image(flavor="proof", tag="inferdrome-runner:test")

    assert command[0:4] == ("docker", "build", "--platform", "linux/amd64")
    assert "--pull=false" in command
    assert f"SOURCE_REPOSITORY_COMMIT={source_commit}" in command
    assert "INFERDROME_VERSION=0.1.0.dev0" in command
    assert docker_commands == [list(command)]
    assert captures[0][0:2] == ("git", "status")


def test_packaged_version_mismatch_is_bounded_and_non_disclosing() -> None:
    submitted = "9.9.9-secret-shaped-value"

    with pytest.raises(builder.RunnerImageBuildError) as exc_info:
        builder.require_packaged_version(submitted, "0.1.0.dev0")

    assert "match" in str(exc_info.value)
    assert submitted not in str(exc_info.value)


def test_wrapper_rejects_noncanonical_platform_without_docker() -> None:
    with pytest.raises(builder.RunnerImageBuildError) as exc_info:
        builder.build_image(platform="linux/arm64")

    assert "linux/amd64" in str(exc_info.value)
