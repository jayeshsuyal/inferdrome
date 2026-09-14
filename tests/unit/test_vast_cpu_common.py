"""Fake clock/manifest checks and harmless Python children; no Docker or network."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import vast_cpu_common as common


def utc_after(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(wall=datetime(2030, 1, 1, tzinfo=UTC), monotonic=100.0)
    monkeypatch.setattr(
        common,
        "datetime",
        SimpleNamespace(
            now=lambda zone: state.wall,
            strptime=datetime.strptime,
        ),
    )
    monkeypatch.setattr(common.time, "monotonic", lambda: state.monotonic)
    return state


def test_child_keeps_original_identity_and_cannot_extend_parent(clock: Any) -> None:
    parent = common.Deadline("2030-01-01T00:00:10Z")
    child = parent.child(3)
    assert child.original_timestamp == parent.original_timestamp
    assert child.remaining() == 3
    assert parent.child(999).remaining() == 10
    clock.monotonic += 2
    clock.wall += timedelta(seconds=2)
    assert child.remaining() == 1
    assert child.child(20).remaining() == 1
    clock.monotonic += 1
    with pytest.raises(common.Failure, match="ORIGINAL_DEADLINE_EXPIRED"):
        child.remaining()


def test_utc_rollback_cannot_restore_spent_monotonic_budget(clock: Any) -> None:
    deadline = common.Deadline("2030-01-01T00:00:10Z")
    clock.monotonic += 7
    clock.wall -= timedelta(hours=1)
    assert deadline.remaining() == 3
    assert deadline.child(60).remaining() == 3
    clock.monotonic += 3
    with pytest.raises(common.Failure, match="ORIGINAL_DEADLINE_EXPIRED"):
        deadline.remaining()


def test_utc_forward_jump_expires_even_with_monotonic_budget_left(clock: Any) -> None:
    deadline = common.Deadline("2030-01-01T00:00:10Z")
    clock.wall += timedelta(seconds=10)
    with pytest.raises(common.Failure, match="ORIGINAL_DEADLINE_EXPIRED"):
        deadline.child(2)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2030-01-01T00:00:00Z",
        "2029-12-31T23:59:59Z",
        "2030-01-01T02:00:01Z",
        "2030-1-1T00:00:10Z",
        "2030-01-01T00:00:10+00:00",
        "not-a-date",
    ],
)
def test_invalid_expired_or_unbounded_deadlines_fail(
    clock: Any, timestamp: str
) -> None:
    with pytest.raises(common.Failure):
        common.Deadline(timestamp)


@pytest.mark.parametrize("seconds", [0, -1])
def test_child_requires_positive_budget(clock: Any, seconds: float) -> None:
    with pytest.raises(common.Failure, match="INVALID_PHASE_BUDGET"):
        common.Deadline("2030-01-01T00:00:10Z").child(seconds)


@pytest.fixture
def runner(tmp_path: Path) -> common.Runner:
    return common.Runner(tmp_path, common.Deadline(utc_after(60)))


def test_runner_does_not_inherit_secrets_and_sends_input_outside_argv(
    runner: common.Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GHCR_TOKEN", "synthetic-parent-token")
    monkeypatch.setenv("PYTHONPATH", "/synthetic-untrusted-python-path")
    program = (
        "import json,os,sys; "
        "print(json.dumps({'env':dict(os.environ),'argv':sys.argv,"
        "'stdin':sys.stdin.buffer.read().decode()}))"
    )
    result = runner.run(
        [sys.executable, "-I", "-c", program],
        deadline=common.Deadline(utc_after(20)),
        input=b"synthetic-stdin-only-token",
    )
    observed = json.loads(result.stdout)
    assert "GHCR_TOKEN" not in observed["env"]
    assert "PYTHONPATH" not in observed["env"]
    assert observed["env"]["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert observed["env"]["HOME"] == str(runner.home)
    assert observed["argv"] == ["-c"]
    assert observed["stdin"] == "synthetic-stdin-only-token"
    assert stat.S_IMODE(runner.home.stat().st_mode) == 0o700


def test_failed_command_has_fixed_diagnostic_and_optional_returncode(
    runner: common.Runner,
) -> None:
    command = [
        sys.executable,
        "-I",
        "-c",
        "import sys;sys.stderr.write('synthetic-token');sys.exit(3)",
    ]
    deadline = common.Deadline(utc_after(20))
    with pytest.raises(common.Failure) as error:
        runner.run(command, deadline=deadline)
    assert str(error.value) == "COMMAND_FAILED"
    assert runner.run(command, deadline=deadline, check=False).returncode == 3


@pytest.mark.parametrize("hide_sizes", [False, True])
def test_combined_output_is_bounded_even_when_size_observation_is_stale(
    runner: common.Runner,
    monkeypatch: pytest.MonkeyPatch,
    hide_sizes: bool,
) -> None:
    if hide_sizes:
        monkeypatch.setattr(common.os, "fstat", lambda fd: SimpleNamespace(st_size=0))
    command = [
        sys.executable,
        "-I",
        "-c",
        "import sys;sys.stdout.write('x'*1500);sys.stderr.write('y'*1500)",
    ]
    with pytest.raises(common.Failure, match="COMMAND_OUTPUT_LIMIT"):
        runner.run(command, deadline=common.Deadline(utc_after(20)), limit=2000)


def test_timeout_terminates_and_reaps_the_owned_python_child(
    runner: common.Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = subprocess.Popen
    children = []

    def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(common.subprocess, "Popen", spawn)
    started = time.monotonic()
    with pytest.raises(common.Failure, match="ORIGINAL_DEADLINE_EXPIRED"):
        runner.run(
            [sys.executable, "-I", "-c", "import time;time.sleep(10)"],
            deadline=common.Deadline(utc_after(20)).child(0.1),
        )
    assert len(children) == 1 and children[0].poll() is not None
    assert time.monotonic() - started < 3


def test_exceeded_egress_prevents_productive_spawn_but_allows_cleanup_child(
    runner: common.Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common.write_json(
        runner.root / "network-start.json",
        {
            "interfaces": {"eth0": 100},
            "max_egress_bytes": 10,
        },
    )
    monkeypatch.setattr(common, "network_bytes", lambda: {"eth0": 111})
    original = subprocess.Popen
    starts = []

    def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        starts.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(common.subprocess, "Popen", spawn)
    command = [sys.executable, "-I", "-c", "pass"]
    with pytest.raises(common.Failure, match="EGRESS_OBSERVATION_LIMIT"):
        runner.run(command, deadline=common.Deadline(utc_after(20)))
    assert starts == []
    assert runner.run(command, deadline=runner.cleanup.child(5)).returncode == 0
    assert len(starts) == 1


@pytest.mark.parametrize("current", [{"eth0": 99}, {"other": 100}])
def test_network_counter_reset_is_not_treated_as_available_budget(
    runner: common.Runner,
    monkeypatch: pytest.MonkeyPatch,
    current: dict[str, int],
) -> None:
    common.write_json(
        runner.root / "network-start.json",
        {
            "interfaces": {"eth0": 100},
            "max_egress_bytes": 1000,
        },
    )
    monkeypatch.setattr(common, "network_bytes", lambda: current)
    with pytest.raises(common.Failure, match="NETWORK_COUNTER_RESET"):
        runner.check_egress()


def test_owned_manifest_records_exact_ids_and_preserves_unrelated_ids(
    runner: common.Runner,
) -> None:
    runner.record_container("a" * 64)
    runner.record_container("b" * 64)
    with pytest.raises(common.Failure, match="DUPLICATE_CONTAINER_ID"):
        runner.record_container("a" * 64)
    runner.forget_container("a" * 64)
    assert common.read_json(runner.root / "owned-containers.json") == ["b" * 64]
    assert not (runner.root / "owned-containers.next").exists()


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"a" * 64: True},
        "a" * 64,
        [None],
        [12],
        ["short"],
        ["A" * 64],
        ["a" * 64, "a" * 64],
    ],
)
@pytest.mark.parametrize("operation", ["record_container", "forget_container"])
def test_malformed_owned_manifest_is_rejected_without_replacement(
    runner: common.Runner,
    value: object,
    operation: str,
) -> None:
    manifest = runner.root / "owned-containers.json"
    common.write_json(manifest, value)
    before = manifest.read_bytes()
    with pytest.raises(common.Failure, match="INVALID_OWNED_CONTAINER_RECORD"):
        getattr(runner, operation)("b" * 64)
    assert manifest.read_bytes() == before


def test_json_evidence_is_private_canonical_and_never_replaced(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    common.write_json(path, {"z": 1, "a": 2})
    assert path.read_bytes() == b'{"a":2,"z":1}\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        common.write_json(path, {"replacement": True})
    assert common.read_json(path) == {"a": 2, "z": 1}


def test_json_rejects_symlinks_and_oversized_files(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    common.write_json(target, [])
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(common.Failure, match="INVALID_JSON_EVIDENCE"):
        common.read_json(link)
    with pytest.raises(FileExistsError):
        common.write_json(link, {"replacement": True})
    assert target.read_bytes() == b"[]\n"
    with target.open("r+b") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(common.Failure, match="INVALID_JSON_EVIDENCE"):
        common.read_json(target)


def test_dangling_owned_manifest_cannot_be_silently_replaced(
    runner: common.Runner,
) -> None:
    path = runner.root / "owned-containers.json"
    path.symlink_to(runner.root / "missing.json")
    with pytest.raises(common.Failure, match="INVALID_JSON_EVIDENCE"):
        runner.record_container("a" * 64)
    assert path.is_symlink()
