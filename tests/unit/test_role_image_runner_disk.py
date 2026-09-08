"""Local safety tests for role-image runner disk observations and cleanup."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import scripts.role_image_runner_disk as runner_disk


def _sdk_target(root: Path) -> Path:
    target = root / "usr/local/lib/android/sdk"
    target.mkdir(parents=True)
    return target


def _linux_host() -> str:
    return "Linux"


@pytest.mark.parametrize(
    ("runner_os", "runner_environment", "host_platform", "message"),
    (
        ("Linux", "github-hosted", lambda: "Darwin", "actual Linux host"),
        ("Linux", "self-hosted", _linux_host, "GitHub-hosted"),
    ),
)
def test_wrong_runner_context_fails_before_any_command(
    tmp_path: Path,
    runner_os: str,
    runner_environment: str,
    host_platform: Callable[[], str],
    message: str,
) -> None:
    commands: list[tuple[str, ...]] = []
    target = _sdk_target(tmp_path)

    with pytest.raises(runner_disk.RoleImageRunnerDiskError, match=message):
        runner_disk.reclaim_android_sdk(
            runner_os=runner_os,
            runner_environment=runner_environment,
            target=target,
            expected=target,
            root=tmp_path,
            run_command=lambda command: commands.append(tuple(command)),
            host_platform=host_platform,
        )

    assert commands == []


def test_unsafe_target_path_fails_before_any_command(tmp_path: Path) -> None:
    expected = _sdk_target(tmp_path)
    commands: list[tuple[str, ...]] = []

    with pytest.raises(runner_disk.RoleImageRunnerDiskError, match="not exact"):
        runner_disk.reclaim_android_sdk(
            runner_os="Linux",
            runner_environment="github-hosted",
            target=tmp_path / "other-sdk",
            expected=expected,
            root=tmp_path,
            run_command=lambda command: commands.append(tuple(command)),
            host_platform=_linux_host,
        )

    assert commands == []


def test_symlinked_component_fails_before_any_command(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "sdk").mkdir(parents=True)
    target = tmp_path / "usr/local/lib/android/sdk"
    (tmp_path / "usr/local/lib").mkdir(parents=True)
    target.parent.symlink_to(outside, target_is_directory=True)
    commands: list[tuple[str, ...]] = []

    with pytest.raises(runner_disk.RoleImageRunnerDiskError, match="symbolic link"):
        runner_disk.reclaim_android_sdk(
            runner_os="Linux",
            runner_environment="github-hosted",
            target=target,
            expected=target,
            root=tmp_path,
            run_command=lambda command: commands.append(tuple(command)),
            host_platform=_linux_host,
        )

    assert commands == []
    assert outside.is_dir()


def test_missing_target_is_a_disclosed_noop(tmp_path: Path) -> None:
    target = tmp_path / "usr/local/lib/android/sdk"
    (tmp_path / "usr/local/lib/android").mkdir(parents=True)
    commands: list[tuple[str, ...]] = []
    messages: list[str] = []

    removed = runner_disk.reclaim_android_sdk(
        runner_os="Linux",
        runner_environment="github-hosted",
        target=target,
        expected=target,
        root=tmp_path,
        run_command=lambda command: commands.append(tuple(command)),
        emit=messages.append,
        host_platform=_linux_host,
    )

    assert removed is False
    assert commands == []
    assert messages == [f"Android SDK target absent; no cleanup performed: {target}"]


def test_safe_removal_command_is_fixed_and_shell_free(tmp_path: Path) -> None:
    target = _sdk_target(tmp_path)
    commands: list[tuple[str, ...]] = []

    removed = runner_disk.reclaim_android_sdk(
        runner_os="Linux",
        runner_environment="github-hosted",
        target=target,
        expected=target,
        root=tmp_path,
        run_command=lambda command: commands.append(tuple(command)),
        host_platform=_linux_host,
    )

    assert removed is True
    assert commands == [
        (
            "sudo",
            "--non-interactive",
            "rm",
            "-rf",
            "--one-file-system",
            "--",
            str(target),
        )
    ]
    assert target.is_dir()


def test_observation_records_before_after_and_honest_delta(tmp_path: Path) -> None:
    target = _sdk_target(tmp_path)
    docker_root = tmp_path / "docker-root"
    runner_temp = tmp_path / "runner-temp"
    docker_root.mkdir()
    runner_temp.mkdir()
    commands: list[tuple[str, ...]] = []
    captured: list[tuple[str, ...]] = []
    messages: list[str] = []
    samples = {
        runner_disk.FILESYSTEM_ROOT: iter((100, 140)),
        docker_root: iter((200, 260)),
        runner_temp: iter((300, 305)),
    }

    def free_bytes(path: Path) -> int:
        return next(samples[path])

    def capture_command(command: list[str] | tuple[str, ...]) -> str:
        captured.append(tuple(command))
        return str(docker_root)

    runner_disk.observe_and_reclaim_android_sdk(
        runner_os="Linux",
        runner_environment="github-hosted",
        target=target,
        expected=target,
        root=tmp_path,
        run_command=lambda command: commands.append(tuple(command)),
        capture_command=capture_command,
        runner_temp=runner_temp,
        free_bytes=free_bytes,
        emit=messages.append,
        host_platform=_linux_host,
    )

    assert captured == [("docker", "info", "--format", "{{.DockerRootDir}}")]
    assert commands[0] == ("docker", "system", "df")
    assert commands[1][-1] == str(target)
    assert f"- DockerRootDir: {docker_root}" in messages
    before = (
        "- Runner root filesystem `/` available bytes before SDK reclamation: 100",
        (
            f"- Docker root filesystem `{docker_root}` available bytes before SDK "
            "reclamation: 200"
        ),
        (
            f"- RUNNER_TEMP filesystem `{runner_temp}` available bytes before SDK "
            "reclamation: 300"
        ),
    )
    assert all(message in messages for message in before)
    deltas = (
        "Runner root filesystem available-byte delta after SDK reclamation: 40",
        "Docker root filesystem available-byte delta after SDK reclamation: 60",
        "RUNNER_TEMP filesystem available-byte delta after SDK reclamation: 5",
    )
    assert all(
        delta in message
        for delta, message in zip(deltas, messages[-3:], strict=True)
    )
    assert "not a guaranteed reclaimed-byte count" in messages[-1]


def test_diagnose_only_observes_capacity_without_sdk_removal(tmp_path: Path) -> None:
    target = _sdk_target(tmp_path)
    docker_root = tmp_path / "docker-root"
    runner_temp = tmp_path / "runner-temp"
    docker_root.mkdir()
    runner_temp.mkdir()
    commands: list[tuple[str, ...]] = []

    runner_disk.observe_runner_capacity(
        runner_os="Linux",
        runner_environment="github-hosted",
        run_command=lambda command: commands.append(tuple(command)),
        capture_command=lambda _command: str(docker_root),
        runner_temp=runner_temp,
        phase="after failed build",
        free_bytes=lambda _path: 123,
        host_platform=_linux_host,
    )

    assert commands == [("docker", "system", "df")]
    assert target.is_dir()


def test_missing_docker_root_observation_fails_before_cleanup(tmp_path: Path) -> None:
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    commands: list[tuple[str, ...]] = []

    with pytest.raises(runner_disk.RoleImageRunnerDiskError, match="DockerRootDir"):
        runner_disk.observe_runner_capacity(
            runner_os="Linux",
            runner_environment="github-hosted",
            run_command=lambda command: commands.append(tuple(command)),
            capture_command=lambda _command: "relative/docker-root",
            runner_temp=runner_temp,
            phase="after failed build",
            free_bytes=lambda _path: 123,
            host_platform=_linux_host,
        )

    assert commands == []


def test_command_failure_preserves_its_nonzero_exit_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(**_kwargs: object) -> None:
        raise runner_disk.subprocess.CalledProcessError(23, ["docker", "system", "df"])

    monkeypatch.setattr(runner_disk, "observe_runner_capacity", fail)

    assert (
        runner_disk.main(
            [
                "--runner-os",
                "Linux",
                "--runner-environment",
                "github-hosted",
                "--runner-temp",
                "/tmp",
                "--diagnose-only",
            ]
        )
        == 23
    )
