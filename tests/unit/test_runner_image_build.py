"""Clean-context runner image wrapper coverage."""

from __future__ import annotations

import subprocess
import tomllib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.build_runner_image as builder


def test_proof_build_rejects_dirty_relevant_inputs_without_echoing_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dirty_marker = "sk-arbitrary-secret-shaped-status"
    captured_commands: list[tuple[str, ...]] = []

    def capture(command: tuple[str, ...]) -> str:
        captured_commands.append(command)
        return dirty_marker

    monkeypatch.setattr(builder, "_capture", capture)

    with pytest.raises(builder.RunnerImageBuildError) as exc_info:
        builder.build_image(flavor="proof")

    assert "clean" in str(exc_info.value)
    assert dirty_marker not in str(exc_info.value)
    assert "--ignored=matching" in captured_commands[0]
    assert "scripts/build_runner_image.py" in captured_commands[0]
    assert "LICENSE" in captured_commands[0]
    assert "THIRD_PARTY_NOTICES.md" in captured_commands[0]
    assert "LICENSES" in captured_commands[0]


@pytest.mark.parametrize(
    "status",
    (
        "!! src/.DS_Store",
        "?? src/untracked.py",
        " M src/modified.py",
        " D src/deleted.py",
        "M  src/staged.py",
    ),
)
def test_proof_build_rejects_ignored_untracked_and_staged_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    monkeypatch.setattr(builder, "_capture", lambda _command: status)

    with pytest.raises(builder.RunnerImageBuildError) as exc_info:
        builder.build_image(flavor="proof")

    assert "clean" in str(exc_info.value)
    assert status not in str(exc_info.value)


def test_proof_build_derives_commit_version_and_canonical_platform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_commit = "a" * 40
    captures: list[tuple[str, ...]] = []

    def capture(command: tuple[str, ...]) -> str:
        captures.append(command)
        return "" if command[1] == "status" else source_commit

    docker_commands: list[list[str]] = []
    context_existed_during_build: list[bool] = []
    dockerfile_bytes_during_build: list[bytes] = []
    ambient_checkout = tmp_path / "ambient-checkout"
    ambient_checkout.mkdir()
    (ambient_checkout / "Dockerfile").write_text(
        "dirty ambient Dockerfile", encoding="utf-8"
    )

    @contextmanager
    def materialized_context(_source_commit: str):
        context = tmp_path / "proof-context"
        context.mkdir()
        (context / "Dockerfile").write_text("archived Dockerfile", encoding="utf-8")
        try:
            yield context
        finally:
            (context / "Dockerfile").unlink()
            context.rmdir()

    monkeypatch.setattr(builder, "_capture", capture)
    monkeypatch.setattr(builder, "REPOSITORY_ROOT", ambient_checkout)
    monkeypatch.setattr(builder, "_package_version", lambda: "0.1.0.dev0")
    monkeypatch.setattr(builder.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(builder, "_materialize_proof_context", materialized_context)

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        docker_commands.append(list(command))
        context_existed_during_build.append(Path(command[-1]).exists())
        dockerfile_path = Path(command[command.index("--file") + 1])
        dockerfile_bytes_during_build.append(dockerfile_path.read_bytes())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    command = builder.build_image(flavor="proof", tag="inferdrome-runner:test")

    assert command[0:4] == ("docker", "build", "--platform", "linux/amd64")
    assert "--pull=false" in command
    assert f"SOURCE_REPOSITORY_COMMIT={source_commit}" in command
    assert "INFERDROME_VERSION=0.1.0.dev0" in command
    assert docker_commands == [list(command)]
    assert context_existed_during_build == [True]
    assert command[-1] != "."
    assert not Path(command[-1]).exists()
    dockerfile_index = command.index("--file") + 1
    assert Path(command[dockerfile_index]).parent == Path(command[-1])
    assert Path(command[dockerfile_index]).name == "Dockerfile"
    assert dockerfile_bytes_during_build == [b"archived Dockerfile"]
    assert Path(command[dockerfile_index]) != ambient_checkout / "Dockerfile"
    assert captures[0][0:2] == ("git", "status")


def test_materialized_proof_context_contains_only_tracked_allowlist() -> None:
    with builder._materialize_proof_context(builder._source_commit()) as context:
        head_dockerfile = subprocess.run(
            ["git", "show", "HEAD:Dockerfile"],
            cwd=builder.REPOSITORY_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        assert (context / "Dockerfile").is_file()
        assert (context / "Dockerfile").read_bytes() == head_dockerfile
        assert (context / ".dockerignore").is_file()
        assert (context / "pyproject.toml").is_file()
        assert (context / "uv.lock").is_file()
        assert (context / "README.md").is_file()
        assert (context / "LICENSE").is_file()
        assert (context / "THIRD_PARTY_NOTICES.md").is_file()
        assert (context / "LICENSES").is_dir()
        assert (context / "src").is_dir()
        assert not (context / "scripts").exists()
        assert not any(path.is_symlink() for path in context.rglob("*"))
        context_path = context
    assert not context_path.exists()


@pytest.mark.parametrize("return_code", (1,))
def test_proof_context_is_removed_after_docker_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    return_code: int,
) -> None:
    created: list[Path] = []

    @contextmanager
    def materialized_context(_source_commit: str):
        context = tmp_path / "failed-proof-context"
        context.mkdir()
        (context / "Dockerfile").write_text("archived Dockerfile", encoding="utf-8")
        created.append(context)
        try:
            yield context
        finally:
            (context / "Dockerfile").unlink()
            context.rmdir()

    monkeypatch.setattr(
        builder,
        "_capture",
        lambda command: "" if command[1] == "status" else "a" * 40,
    )
    monkeypatch.setattr(builder, "_package_version", lambda: "0.1.0.dev0")
    monkeypatch.setattr(builder.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(builder, "_materialize_proof_context", materialized_context)
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda _command, **_kwargs: SimpleNamespace(returncode=return_code),
    )

    with pytest.raises(builder.RunnerImageBuildError, match="Docker build failed"):
        builder.build_image(flavor="proof")

    assert created and not created[0].exists()


def test_proof_context_is_removed_after_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: list[Path] = []

    @contextmanager
    def materialized_context(_source_commit: str):
        context = tmp_path / "interrupted-proof-context"
        context.mkdir()
        (context / "Dockerfile").write_text("archived Dockerfile", encoding="utf-8")
        created.append(context)
        try:
            yield context
        finally:
            (context / "Dockerfile").unlink()
            context.rmdir()

    monkeypatch.setattr(
        builder,
        "_capture",
        lambda command: "" if command[1] == "status" else "a" * 40,
    )
    monkeypatch.setattr(builder, "_package_version", lambda: "0.1.0.dev0")
    monkeypatch.setattr(builder.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(builder, "_materialize_proof_context", materialized_context)

    def interrupt(_command, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(builder.subprocess, "run", interrupt)

    with pytest.raises(KeyboardInterrupt):
        builder.build_image(flavor="proof")

    assert created and not created[0].exists()


def test_packaged_version_mismatch_is_bounded_and_non_disclosing() -> None:
    submitted = "9.9.9-secret-shaped-value"

    with pytest.raises(builder.RunnerImageBuildError) as exc_info:
        builder.require_packaged_version(submitted, "0.1.0.dev0")

    assert "match" in str(exc_info.value)
    assert submitted not in str(exc_info.value)


def test_repository_package_version_matches_project_metadata() -> None:
    project = tomllib.loads(
        (builder.REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert builder._package_version() == project["project"]["version"]


def test_wrapper_rejects_noncanonical_platform_without_docker() -> None:
    with pytest.raises(builder.RunnerImageBuildError) as exc_info:
        builder.build_image(platform="linux/arm64")

    assert "linux/amd64" in str(exc_info.value)


def test_vllm_runner_proof_build_uses_specialized_archive_and_dockerfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_commit = "b" * 40
    captures: list[tuple[str, ...]] = []

    def capture(command: tuple[str, ...]) -> str:
        captures.append(command)
        return "" if command[1] == "status" else source_commit

    @contextmanager
    def materialized_context(
        _source_commit: str,
        *,
        archive_inputs: tuple[str, ...] | None = None,
    ):
        assert archive_inputs == builder.VLLM_ARCHIVE_BUILD_INPUTS
        context = tmp_path / "vllm-proof-context"
        context.mkdir()
        (context / "Dockerfile.vllm-benchmark-runner").write_text(
            "archived Dockerfile", encoding="utf-8"
        )
        try:
            yield context
        finally:
            (context / "Dockerfile.vllm-benchmark-runner").unlink()
            context.rmdir()

    docker_commands: list[list[str]] = []
    dockerfile_bytes_during_build: list[bytes] = []
    monkeypatch.setattr(builder, "_capture", capture)
    monkeypatch.setattr(builder, "_package_version", lambda: "0.1.0.dev0")
    monkeypatch.setattr(builder.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(builder, "_materialize_proof_context", materialized_context)

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        docker_commands.append(list(command))
        dockerfile_path = Path(command[command.index("--file") + 1])
        dockerfile_bytes_during_build.append(dockerfile_path.read_bytes())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    command = builder.build_image(
        flavor="proof",
        image_kind="vllm-benchmark-runner",
        tag="inferdrome-vllm-runner:test",
    )

    assert "--file" in command
    dockerfile_path = Path(command[command.index("--file") + 1])
    assert dockerfile_path.name == "Dockerfile.vllm-benchmark-runner"
    assert dockerfile_path.parent == Path(command[-1])
    assert dockerfile_bytes_during_build == [b"archived Dockerfile"]
    assert f"SOURCE_REPOSITORY_COMMIT={source_commit}" in command
    assert docker_commands == [list(command)]
    assert captures[0][0:2] == ("git", "status")


def test_specialized_proof_context_is_exact_allowlist() -> None:
    with builder._materialize_proof_context(
        builder._source_commit(),
        archive_inputs=builder.VLLM_ARCHIVE_BUILD_INPUTS,
    ) as context:
        assert (context / "Dockerfile.vllm-benchmark-runner").is_file()
        assert not (context / "Dockerfile").exists()
        assert (context / "uv.lock").is_file()
        assert (context / "LICENSE").is_file()
        assert (context / "THIRD_PARTY_NOTICES.md").is_file()
        assert (context / "LICENSES").is_dir()
        assert (context / "src").is_dir()
        assert not (context / "scripts").exists()
        assert not any(path.is_symlink() for path in context.rglob("*"))
