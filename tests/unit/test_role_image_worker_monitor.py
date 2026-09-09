"""Local fake tests for dedicated role-image worker observations."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

import scripts.role_image_worker_monitor as monitor

_SOURCE_COMMIT = "a" * 40


class _Process:
    def __init__(self, *, returncode: int, complete_after_polls: int = 1) -> None:
        self.returncode = returncode
        self.complete_after_polls = complete_after_polls
        self.polls = 0
        self.terminated = False
        self.waits = 0

    def poll(self) -> int | None:
        self.polls += 1
        if self.terminated or self.polls > self.complete_after_polls:
            return self.returncode
        return None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.waits += 1
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True


def _inputs(tmp_path: Path) -> dict[str, object]:
    return {
        "mode": "BUILD_AND_SMOKE_ONLY",
        "source_commit": _SOURCE_COMMIT,
        "github_repository": "jayeshsuyal/inferdrome",
        "github_ref": "refs/heads/main",
        "github_sha": _SOURCE_COMMIT,
        "runner_os": "Linux",
        "runner_arch": "X64",
        "runner_environment": "self-hosted",
        "runner_name": "role-image-cpu-01",
        "expected_runner_name": "role-image-cpu-01",
        "runner_temp": tmp_path / "runner-temp",
        "workspace": tmp_path / "workspace",
        "build_command": ("/bin/true",),
        "interval_seconds": 1,
        "max_during_samples": 2,
    }


def _capture(calls: list[tuple[str, ...]]) -> Callable[[Sequence[str]], str]:
    values = {
        monitor._DOCKER_INFO: "overlay2\t/tmp/docker-root\n",
        monitor._DOCKER_VERSION: "28.0.4\n",
        monitor._BUILDX_VERSION: "github.com/docker/buildx v0.37.0\n",
        monitor._DOCKER_DF: "Images\t12GB\t10GB (83%)\n",
    }

    def capture(command: Sequence[str]) -> str:
        command_tuple = tuple(command)
        calls.append(command_tuple)
        return values[command_tuple]

    return capture


def _filesystem_usage() -> Callable[[Path], Mapping[str, object]]:
    observations = iter((1000, 900, 800, 700, 600, 500, 400, 300, 200, 100, 90, 80))

    def usage(path: Path) -> Mapping[str, object]:
        available = next(observations)
        return {
            "available_bytes": available,
            "available_inodes": available // 10,
            "canonical_path": str(path),
            "capacity_bytes": 2000,
            "device": 9,
            "inode_capacity": 200,
        }

    return usage


def test_wrong_worker_fails_before_docker_or_build_start(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs["runner_os"] = "Darwin"
    calls: list[tuple[str, ...]] = []
    started: list[Sequence[str]] = []

    with pytest.raises(monitor.RoleImageWorkerMonitorError, match="platform"):
        monitor.run_observed_build(
            **inputs,
            capture_command=_capture(calls),
            filesystem_usage=_filesystem_usage(),
            popen_command=lambda command: started.append(command),  # type: ignore[return-value]
            sleep=lambda _seconds: None,
        )

    assert calls == []
    assert started == []


def test_missing_required_observation_fails_before_build_start(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    started: list[Sequence[str]] = []

    def unavailable(_command: Sequence[str]) -> str:
        raise monitor.RoleImageWorkerMonitorError("required observation unavailable")

    with pytest.raises(monitor.RoleImageWorkerMonitorError, match="unavailable"):
        monitor.run_observed_build(
            **inputs,
            capture_command=unavailable,
            filesystem_usage=_filesystem_usage(),
            popen_command=lambda command: started.append(command),  # type: ignore[return-value]
            sleep=lambda _seconds: None,
        )

    assert started == []


def test_build_failure_keeps_bounded_before_during_after_observations(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    calls: list[tuple[str, ...]] = []
    process = _Process(returncode=19, complete_after_polls=1)

    record, returncode = monitor.run_observed_build(
        **inputs,
        capture_command=_capture(calls),
        filesystem_usage=_filesystem_usage(),
        popen_command=lambda _command: process,
        sleep=lambda _seconds: None,
    )

    assert returncode == 19
    assert record["build_exit_code"] == 19
    assert record["build_timed_out"] is False
    assert record["monitor_exit_code"] == 19
    assert record["sample_count"] == 3
    assert record["during_sample_limit"] == 2
    assert len(record["observations"]["during"]) == 1
    assert record["sampled_minimum_available"]["runner_root"] == {
        "minimum_available_bytes_observed": 100,
        "minimum_available_inodes_observed": 10,
    }
    assert calls.count(monitor._DOCKER_INFO) == 2
    assert process.terminated is False


def test_deadline_expiry_fails_closed_when_the_terminated_child_exits_cleanly(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    process = _Process(returncode=0, complete_after_polls=100)
    clock = iter((0.0, 2.0, 2.0))

    record, returncode = monitor.run_observed_build(
        **inputs,
        build_deadline_seconds=1,
        capture_command=_capture([]),
        filesystem_usage=_filesystem_usage(),
        popen_command=lambda _command: process,
        sleep=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )

    assert returncode == monitor._TIMEOUT_EXIT_CODE
    assert record["build_exit_code"] == 0
    assert record["build_timed_out"] is True
    assert record["monitor_exit_code"] == monitor._TIMEOUT_EXIT_CODE
    assert process.terminated is True


class _WaitTimeoutProcess(_Process):
    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if not self.terminated and timeout is not None:
            raise subprocess.TimeoutExpired("fake-build", timeout)
        return self.returncode


def test_wait_timeout_fails_closed_when_the_terminated_child_exits_cleanly(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    inputs["max_during_samples"] = 1
    process = _WaitTimeoutProcess(returncode=0, complete_after_polls=100)
    clock = iter((0.0, 1.0, 2.0))

    record, returncode = monitor.run_observed_build(
        **inputs,
        build_deadline_seconds=10,
        capture_command=_capture([]),
        filesystem_usage=_filesystem_usage(),
        popen_command=lambda _command: process,
        sleep=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )

    assert returncode == monitor._TIMEOUT_EXIT_CODE
    assert record["build_exit_code"] == 0
    assert record["build_timed_out"] is True
    assert record["monitor_exit_code"] == monitor._TIMEOUT_EXIT_CODE
    assert process.terminated is True


def test_worker_observation_command_has_a_finite_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[int] = []

    def times_out(*_args: object, **kwargs: object) -> object:
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, int)
        timeouts.append(timeout)
        raise subprocess.TimeoutExpired("docker", timeout)

    monkeypatch.setattr(monitor.subprocess, "run", times_out)

    with pytest.raises(monitor.RoleImageWorkerMonitorError, match="timed out"):
        monitor._capture_subprocess(monitor._DOCKER_VERSION)

    assert timeouts == [monitor._OBSERVATION_TIMEOUT_SECONDS]


def test_observation_failure_terminates_an_unfinished_build(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    process = _Process(returncode=143, complete_after_polls=100)
    call_count = 0

    def usage(path: Path) -> Mapping[str, object]:
        nonlocal call_count
        call_count += 1
        if call_count == 5:
            raise monitor.RoleImageWorkerMonitorError("filesystem observation lost")
        return {
            "available_bytes": 1000,
            "available_inodes": 100,
            "canonical_path": str(path),
            "capacity_bytes": 2000,
            "device": 9,
            "inode_capacity": 200,
        }

    with pytest.raises(monitor.RoleImageWorkerMonitorError, match="lost"):
        monitor.run_observed_build(
            **inputs,
            capture_command=_capture([]),
            filesystem_usage=usage,
            popen_command=lambda _command: process,
            sleep=lambda _seconds: None,
        )

    assert process.terminated is True
    assert process.waits == 1


@pytest.mark.parametrize("maximum", (0, monitor._MAX_DURING_SAMPLES + 1))
def test_invalid_sampling_bound_fails_before_build_start(
    tmp_path: Path, maximum: int
) -> None:
    inputs = _inputs(tmp_path)
    inputs["max_during_samples"] = maximum
    started: list[Sequence[str]] = []

    with pytest.raises(monitor.RoleImageWorkerMonitorError, match="sample limit"):
        monitor.run_observed_build(
            **inputs,
            capture_command=_capture([]),
            filesystem_usage=_filesystem_usage(),
            popen_command=lambda command: started.append(command),  # type: ignore[return-value]
            sleep=lambda _seconds: None,
        )

    assert started == []


def test_main_seals_a_failed_build_observation_without_rewriting_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "record.json"
    summary = tmp_path / "summary.md"
    record = {
        "build_exit_code": 23,
        "build_deadline_seconds": 2550,
        "build_timed_out": False,
        "monitor_exit_code": 23,
        "during_sample_limit": 2,
        "execution": {
            "mode": "BUILD_AND_SMOKE_ONLY",
            "runner_alias": "role-image-cpu-01",
            "source_commit": _SOURCE_COMMIT,
        },
        "observations": {
            "before": {
                "docker": {
                    "buildx_version": "v0.37.0",
                    "server_version": "28.0.4",
                    "storage_driver": "overlay2",
                }
            },
            "during": [],
        },
        "sample_count": 3,
        "sampled_minimum_available": {},
    }
    monkeypatch.setattr(monitor, "run_observed_build", lambda **_kwargs: (record, 23))

    assert (
        monitor.main(
            [
                "--mode",
                "BUILD_AND_SMOKE_ONLY",
                "--source-commit",
                _SOURCE_COMMIT,
                "--github-repository",
                "jayeshsuyal/inferdrome",
                "--github-ref",
                "refs/heads/main",
                "--github-sha",
                _SOURCE_COMMIT,
                "--runner-os",
                "Linux",
                "--runner-arch",
                "X64",
                "--runner-environment",
                "self-hosted",
                "--runner-name",
                "role-image-cpu-01",
                "--expected-runner-name",
                "role-image-cpu-01",
                "--runner-temp",
                str(tmp_path),
                "--workspace",
                str(tmp_path),
                "--output",
                str(output),
                "--summary",
                str(summary),
                "--",
                "/bin/false",
            ]
        )
        == 23
    )
    assert json.loads(output.read_text(encoding="utf-8"))["build_exit_code"] == 23
    assert "Observed child build exit code: `23`" in summary.read_text(
        encoding="utf-8"
    )
    assert "Monitor exit code: `23`" in summary.read_text(encoding="utf-8")


def test_main_returns_nonzero_for_a_sealed_timeout_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "timeout-record.json"
    summary = tmp_path / "timeout-summary.md"
    record = {
        "build_exit_code": 0,
        "build_deadline_seconds": 1,
        "build_timed_out": True,
        "during_sample_limit": 1,
        "execution": {
            "mode": "PUBLISH_FIXED_ROLE_IMAGES",
            "runner_alias": "role-image-cpu-01",
            "source_commit": _SOURCE_COMMIT,
        },
        "monitor_exit_code": monitor._TIMEOUT_EXIT_CODE,
        "observations": {
            "before": {
                "docker": {
                    "buildx_version": "v0.37.0",
                    "server_version": "28.0.4",
                    "storage_driver": "overlay2",
                }
            },
            "during": [],
        },
        "sample_count": 2,
        "sampled_minimum_available": {},
    }
    monkeypatch.setattr(
        monitor,
        "run_observed_build",
        lambda **_kwargs: (record, monitor._TIMEOUT_EXIT_CODE),
    )

    assert (
        monitor.main(
            [
                "--mode",
                "PUBLISH_FIXED_ROLE_IMAGES",
                "--source-commit",
                _SOURCE_COMMIT,
                "--github-repository",
                "jayeshsuyal/inferdrome",
                "--github-ref",
                "refs/heads/main",
                "--github-sha",
                _SOURCE_COMMIT,
                "--runner-os",
                "Linux",
                "--runner-arch",
                "X64",
                "--runner-environment",
                "self-hosted",
                "--runner-name",
                "role-image-cpu-01",
                "--expected-runner-name",
                "role-image-cpu-01",
                "--runner-temp",
                str(tmp_path),
                "--workspace",
                str(tmp_path),
                "--output",
                str(output),
                "--summary",
                str(summary),
                "--",
                "/bin/true",
            ]
        )
        == monitor._TIMEOUT_EXIT_CODE
    )
    sealed = json.loads(output.read_text(encoding="utf-8"))
    assert sealed["build_exit_code"] == 0
    assert sealed["monitor_exit_code"] == monitor._TIMEOUT_EXIT_CODE
