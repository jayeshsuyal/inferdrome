"""Ambient-target regressions: injected subprocesses and fake shell tools only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from inferdrome.deployment import manual_host_preflight as preflight
from inferdrome.deployment.manual_host import prepare_artifacts
from inferdrome.deployment.manual_host_docker import (
    LOCAL_DOCKER_HOST,
    docker_argv,
    docker_environment,
    docker_target,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_manual_host import ROOT, parsed, synthetic_input
from tests.unit.test_manual_host_preflight import (
    container_observation,
    image_observation,
    inventory,
)


def ambient_routing(root: Path, selector: str) -> dict[str, str]:
    home = root / "ambient-home"
    config = home / ".docker"
    config.mkdir(parents=True)
    # Synthetic review data, not a real Docker configuration or credential.
    (config / "config.json").write_text('{"currentContext":"synthetic-remote"}')
    result = {"HOME": str(home)}
    if selector == "host":
        result["DOCKER_HOST"] = "tcp://192.0.2.10:2375"
    elif selector == "context":
        result["DOCKER_CONTEXT"] = "synthetic-remote"
    elif selector == "config":
        result["DOCKER_CONFIG"] = str(config)
    else:
        assert selector == "home-config"
    return result


def test_docker_environment_and_unbound_argv_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = docker_environment(
        "/srv/inputs",
        {
            "PATH": "/synthetic/bin",
            "DOCKER_HOST": "ssh://synthetic",
            "DOCKER_CONTEXT": "synthetic",
            "DOCKER_CONFIG": "/synthetic/config",
            "DOCKER_TLS_VERIFY": "1",
            "DOCKER_CERT_PATH": "/synthetic/certs",
            "DOCKER_CLI_PLUGIN_EXTRA_DIRS": "/synthetic/plugins",
            "COMPOSE_ENV_FILES": "/synthetic/.env",
            "COMPOSE_FILE": "other.json",
            "COMPOSE_PROJECT_NAME": "other-project",
            "COMPOSE_DISABLE_ENV_FILE": "0",
        },
    )
    assert result == {
        "PATH": "/synthetic/bin",
        "DOCKER_HOST": LOCAL_DOCKER_HOST,
        "DOCKER_CONFIG": "/srv/inputs",
        "COMPOSE_DISABLE_ENV_FILE": "1",
    }

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("unbound Docker command reached subprocess")

    monkeypatch.setattr(preflight.subprocess, "run", forbidden)
    for argv in (
        ["docker", "compose", "version"],
        ["docker", "--host", "tcp://192.0.2.10:2375", "info"],
    ):
        with pytest.raises(ValueError, match="local binding"):
            preflight._run(argv)


@pytest.mark.parametrize("selector", ["host", "context", "config", "home-config"])
@pytest.mark.parametrize("running", [False, True])
def test_every_preflight_docker_child_has_explicit_safe_target_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
    running: bool,
) -> None:
    spec = parsed()
    gpu = inventory()
    images = {
        spec.runner_image.reference: canonical_json_bytes(
            image_observation("cpu-runner-observer")
        ),
        spec.serving_image.reference: canonical_json_bytes(image_observation()),
    }
    containers = {
        str(i + 1) * 64: canonical_json_bytes(container_observation(i))
        for i in range(2)
    }
    for key, value in ambient_routing(tmp_path, selector).items():
        monkeypatch.setenv(key, value)
    calls: list[list[str]] = []

    def fake_subprocess(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        if argv[0] == "nvidia-smi":
            return SimpleNamespace(stdout=gpu)
        assert argv[:5] == docker_argv(spec.preparation_path)
        env = kwargs["env"]
        assert {
            k: v for k, v in env.items() if k.startswith(("DOCKER_", "COMPOSE_"))
        } == {
            "DOCKER_HOST": LOCAL_DOCKER_HOST,
            "DOCKER_CONFIG": spec.preparation_path,
            "COMPOSE_DISABLE_ENV_FILE": "1",
        }
        calls.append(argv)
        command = argv[5:]
        if command[:2] == ["image", "inspect"]:
            content = images[argv[-1]]
        elif command[:2] == ["container", "inspect"]:
            content = containers[argv[-1]]
        elif "ps" in command and running:
            content = ("1" * 64 if argv[-1] == "endpoint-a" else "2" * 64).encode()
        elif command == ["compose", "version"] or "ps" in command:
            content = b""
        else:
            raise AssertionError(argv)
        return SimpleNamespace(stdout=content)

    monkeypatch.setattr(preflight.subprocess, "run", fake_subprocess)
    preflight.check_host(
        spec, running=running, run=preflight._run, system="Linux", machine="x86_64"
    )
    assert len(calls) == (7 if running else 4)


@pytest.mark.parametrize("selector", ["host", "context", "config", "home-config"])
@pytest.mark.parametrize(
    "failure", ["none", "preflight", "start", "allocation", "capture", "verify", "down"]
)
def test_startup_and_failure_cleanup_never_inherit_another_docker_target(
    tmp_path: Path,
    selector: str,
    failure: str,
) -> None:
    spec = parsed()
    files = prepare_artifacts(spec, ROOT)
    script = tmp_path / "startup.sh"
    script.write_bytes(files["startup.sh"])
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = tmp_path / "calls.log"
    record = """
printf '%s|%s|%s|%s|%s|%s|%s|%s\\n' "$kind" "${DOCKER_HOST-}" \\
  "${DOCKER_CONTEXT-}" "${DOCKER_CONFIG-}" "${COMPOSE_DISABLE_ENV_FILE-}" \\
  "${COMPOSE_ENV_FILES-}" "${DOCKER_TLS_VERIFY-}" "$*" >> "$TEST_LOG"
[[ "$TEST_FAILURE" != "$kind" ]] || exit 23
"""
    (fakebin / "python3.12").write_text(
        """#!/bin/bash
kind=preflight
for arg in "$@"; do
  [[ "$arg" != --check-running-allocation ]] || kind=allocation
done
"""
        + record
    )
    (fakebin / "docker").write_text(
        """#!/bin/bash
kind=unknown
for arg in "$@"; do
  case "$arg" in
    up) kind=start;; run) kind=capture;; verify) kind=verify;; down) kind=down;;
  esac
done
"""
        + record
    )
    for path in fakebin.iterdir():
        path.chmod(0o700)
    env = {
        "PATH": str(fakebin),
        "TEST_LOG": str(log),
        "TEST_FAILURE": failure,
        "COMPOSE_ENV_FILES": "/synthetic/remote.env",
        "DOCKER_TLS_VERIFY": "1",
        **ambient_routing(tmp_path, selector),
    }
    result = subprocess.run(
        [
            "/bin/bash",
            str(script),
            "--execute-plan",
            sha256_digest(files["plan.json"]),
            "--operator",
            spec.cleanup.accountable_operator,
            "--accept-manual-cleanup-risk",
        ],
        env=env,
        capture_output=True,
        timeout=5,
    )
    rows = [line.split("|") for line in log.read_text().splitlines()]
    kinds = [row[0] for row in rows]
    for row in rows:
        assert row[1:7] == [LOCAL_DOCKER_HOST, "", spec.preparation_path, "1", "", ""]
        if row[0] not in {"preflight", "allocation"}:
            assert row[7].split()[:9] == [
                "--host",
                LOCAL_DOCKER_HOST,
                "--config",
                spec.preparation_path,
                "compose",
                "--project-name",
                spec.compose_project,
                "--file",
                f"{spec.preparation_path}/compose.manual-host.json",
            ]
    if failure == "none":
        assert kinds == [
            "preflight",
            "start",
            "allocation",
            "capture",
            "verify",
            "down",
        ]
        assert result.returncode == 0
    elif failure == "preflight":
        assert kinds == ["preflight"] and result.returncode == 23
    else:
        assert kinds[-1] == "down"
        assert result.returncode == (74 if failure == "down" else 23)
        if failure in {"start", "allocation"}:
            assert "capture" not in kinds and "verify" not in kinds
        if failure == "capture":
            assert "verify" not in kinds
    assert b"cleanup" in result.stderr or b"billing" in result.stderr


@pytest.mark.parametrize("mutation", ["legacy", "remote", "context-config"])
def test_old_or_rebound_preparations_fail_before_host_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    directory = tmp_path / "inputs"
    directory.mkdir(mode=0o700)
    value = synthetic_input()
    value["preparation_path"] = str(directory)
    files = prepare_artifacts(parsed(value), ROOT)
    plan = json.loads(files["plan.json"])
    assert files["config.json"] == b"{}"
    assert plan["docker_target"] == docker_target(str(directory))
    if mutation == "legacy":
        del plan["input_files"]["config.json"]
        del plan["docker_target"]
    elif mutation == "remote":
        plan["docker_target"]["host"] = "tcp://192.0.2.10:2375"
    else:
        files["config.json"] = b'{"currentContext":"synthetic-remote"}'
        plan["input_files"]["config.json"] = sha256_digest(files["config.json"])
    files["plan.json"] = canonical_json_bytes(plan)
    for name, content in files.items():
        (directory / name).write_bytes(content)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("rebound input reached host commands")

    monkeypatch.setattr(preflight, "check_host", forbidden)
    assert (
        preflight.main(
            [
                "--directory",
                str(directory),
                "--expected-plan-digest",
                sha256_digest(files["plan.json"]),
            ]
        )
        == 2
    )
