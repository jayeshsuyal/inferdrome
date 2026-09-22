"""CPU/fake coverage for the post-build Vast startup image smoke."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.verify_vast_startup_image as smoke
from inferdrome.evaluation import vast_ssh_startup as startup

_COMMIT = "a" * 40
_IMAGE = "ghcr.io/jayeshsuyal/inferdrome-private-engine@sha256:" + "b" * 64


def _inspection(*, role: str = "private-engine") -> str:
    labels = {
        "org.opencontainers.image.revision": _COMMIT,
        "com.inferdrome.runtime-role": role,
        "com.inferdrome.vllm-version": "0.26.0",
        "com.inferdrome.vast-startup-profile": "vast-ssh-public-v1",
        "com.inferdrome.vast-ssh-readiness-seconds": "180",
        "com.inferdrome.runtime-package-bootstrap": "forbidden",
        "com.inferdrome.public-pull-contract": (
            "anonymous-public-pull-required-unverified"
        ),
        "com.inferdrome.serving-uid": "2000",
        "com.inferdrome.serving-executable": "/usr/local/bin/vllm",
        "com.inferdrome.vast-startup-command": (
            "/opt/inferdrome-runtime/bin/python -m "
            "inferdrome.evaluation.vast_ssh_startup"
        ),
    }
    return json.dumps(
        [{"Config": {"User": "2000:0", "Entrypoint": ["inferdrome"], "Labels": labels}}]
    )


class FakeDocker:
    def __init__(
        self,
        *,
        inspection: str | None = None,
        fail_probe: bool = False,
        bad_server_config: bool = False,
        missing_host_key: bool = False,
        allow_wrong_key: bool = False,
        allow_password: bool = False,
        serving_uid: str = "2000",
    ) -> None:
        self.inspection = inspection or _inspection()
        self.fail_probe = fail_probe
        self.bad_server_config = bad_server_config
        self.missing_host_key = missing_host_key
        self.allow_wrong_key = allow_wrong_key
        self.allow_password = allow_password
        self.serving_uid = serving_uid
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: object) -> subprocess.CompletedProcess[str]:
        argv = tuple(command)  # type: ignore[arg-type]
        self.commands.append(argv)
        if argv[:3] == ("docker", "image", "inspect"):
            return subprocess.CompletedProcess(argv, 0, self.inspection, "")
        if argv[:2] == ("docker", "create"):
            return subprocess.CompletedProcess(argv, 0, "c" * 64 + "\n", "")
        if argv[:2] == ("docker", "start"):
            return subprocess.CompletedProcess(argv, 0, argv[-1] + "\n", "")
        if argv[:2] == ("docker", "exec") and "socket.create_connection" in argv[-1]:
            return subprocess.CompletedProcess(
                argv, 1 if self.fail_probe else 0, "", ""
            )
        if argv[:2] == ("docker", "exec") and "ssh_host_ed25519_key" in argv[-1]:
            failed = self.bad_server_config or self.missing_host_key
            return subprocess.CompletedProcess(argv, 1 if failed else 0, "", "")
        if argv[:2] == ("docker", "exec") and argv[-2:] == ("/usr/bin/id", "-u"):
            if "/usr/bin/ssh" in argv:
                return subprocess.CompletedProcess(argv, 0, "0\n", "")
            return subprocess.CompletedProcess(argv, 0, self.serving_uid + "\n", "")
        if argv[:2] == ("docker", "exec") and "/usr/bin/true" in argv:
            if any(value.endswith("/wrong-key") for value in argv):
                return subprocess.CompletedProcess(
                    argv, 0 if self.allow_wrong_key else 255, "", ""
                )
            return subprocess.CompletedProcess(
                argv, 0 if self.allow_password else 255, "", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")


def _public_key(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.pub"
    path.write_text("ssh-ed25519 " + "A" * 68 + " fixture\n")
    return path


def _private_key(tmp_path: Path) -> Path:
    path = tmp_path / "fixture"
    path.write_text("ephemeral-test-private-key\n")
    path.chmod(0o600)
    return path


def _mock_root_owned_privsep(monkeypatch: pytest.MonkeyPatch, privsep: Path) -> None:
    original = Path.lstat

    def fake_lstat(path: Path) -> object:
        value = original(path)
        if path == privsep:
            return SimpleNamespace(st_mode=value.st_mode, st_uid=0)
        return value

    monkeypatch.setattr(Path, "lstat", fake_lstat)


def test_smoke_uses_no_pull_no_network_runtime_startup_and_nonroot_serving(
    tmp_path: Path,
) -> None:
    docker = FakeDocker()
    result = smoke.verify_image(
        image=_IMAGE,
        authorized_keys_file=_public_key(tmp_path),
        ssh_private_key_file=_private_key(tmp_path),
        source_commit=_COMMIT,
        run=docker,
        wait=lambda _: None,
    )
    assert result["verified"] is True
    create = next(
        command for command in docker.commands if command[:2] == ("docker", "create")
    )
    assert create[2:4] == ("--pull", "never")
    assert create[4:6] == ("--network", "none")
    assert not any("apt" in item or "pip" in item for item in create)
    assert any(
        command[:3] == ("docker", "rm", "--force") for command in docker.commands
    )


@pytest.mark.parametrize(
    ("docker", "message"),
    (
        (FakeDocker(bad_server_config=True), "smoke command failed"),
        (FakeDocker(missing_host_key=True), "smoke command failed"),
        (FakeDocker(allow_wrong_key=True), "authentication fail-closed"),
        (FakeDocker(allow_password=True), "authentication fail-closed"),
        (FakeDocker(serving_uid="0"), "UID 2000"),
    ),
)
def test_smoke_rejects_root_policy_key_password_host_key_and_uid_failures(
    tmp_path: Path, docker: FakeDocker, message: str
) -> None:
    with pytest.raises(smoke.StartupImageSmokeError, match=message):
        smoke.verify_image(
            image=_IMAGE,
            authorized_keys_file=_public_key(tmp_path),
            ssh_private_key_file=_private_key(tmp_path),
            source_commit=_COMMIT,
            run=docker,
            wait=lambda _: None,
        )
    assert any(
        command[:3] == ("docker", "rm", "--force") for command in docker.commands
    )


@pytest.mark.parametrize(
    "inspection",
    (
        _inspection(role="cpu-runner-observer"),
        _inspection().replace("vast-ssh-public-v1", "not-applicable"),
        _inspection().replace(_COMMIT, "c" * 40),
        _inspection().replace("2000:0", "0:0"),
    ),
)
def test_smoke_rejects_wrong_role_profile_source_or_default_user_before_create(
    tmp_path: Path, inspection: str
) -> None:
    docker = FakeDocker(inspection=inspection)
    with pytest.raises(smoke.StartupImageSmokeError):
        smoke.verify_image(
            image=_IMAGE,
            authorized_keys_file=_public_key(tmp_path),
            ssh_private_key_file=_private_key(tmp_path),
            source_commit=_COMMIT,
            run=docker,
        )
    assert not any(command[:2] == ("docker", "create") for command in docker.commands)


def test_smoke_always_removes_exact_container_after_post_create_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(fail_probe=True)
    ticks = iter((0.0, 181.0))
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(ticks))
    with pytest.raises(smoke.StartupImageSmokeError, match="180 seconds"):
        smoke.verify_image(
            image=_IMAGE,
            authorized_keys_file=_public_key(tmp_path),
            ssh_private_key_file=_private_key(tmp_path),
            source_commit=_COMMIT,
            run=docker,
            wait=lambda _: None,
        )
    cleanup = [
        command
        for command in docker.commands
        if command[:3] == ("docker", "rm", "--force")
    ]
    assert cleanup == [("docker", "rm", "--force", "c" * 64)]


def test_smoke_rejects_mutable_or_other_role_image_without_docker(
    tmp_path: Path,
) -> None:
    key = _public_key(tmp_path)
    for image in (
        "ghcr.io/jayeshsuyal/inferdrome-private-engine:latest",
        "ghcr.io/jayeshsuyal/inferdrome-cpu-runner-observer@sha256:" + "b" * 64,
    ):
        docker = FakeDocker()
        with pytest.raises(smoke.StartupImageSmokeError):
            smoke.verify_image(
                image=image,
                authorized_keys_file=key,
                ssh_private_key_file=_private_key(tmp_path),
                source_commit=_COMMIT,
                run=docker,
            )
        assert docker.commands == []


def test_smoke_accepts_an_immutable_local_image_id(tmp_path: Path) -> None:
    docker = FakeDocker()
    result = smoke.verify_image(
        image="sha256:" + "d" * 64,
        authorized_keys_file=_public_key(tmp_path),
        ssh_private_key_file=_private_key(tmp_path),
        source_commit=_COMMIT,
        run=docker,
        wait=lambda _: None,
    )
    assert result["verified"] is True


def test_startup_generates_ephemeral_key_and_validates_sshd_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "runtime-state"
    key = _public_key(tmp_path)
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(startup, "_STATE_ROOT", state)
    privsep = tmp_path / "run-sshd"
    monkeypatch.setattr(startup, "_PRIVSEP_ROOT", privsep)
    _mock_root_owned_privsep(monkeypatch, privsep)
    monkeypatch.setattr(startup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(startup, "_checked", commands.append)

    command = startup.prepare_startup(authorized_keys_file=key, port=2222)

    assert command == ("/usr/sbin/sshd", "-D", "-e", "-f", str(state / "sshd_config"))
    assert commands[0][:6] == ("/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "")
    assert commands[1] == ("/usr/sbin/sshd", "-t", "-f", str(state / "sshd_config"))
    config = (state / "sshd_config").read_text()
    assert "PasswordAuthentication no" in config
    assert "PermitRootLogin prohibit-password" in config
    assert str(state / "ssh_host_ed25519_key") in config
    assert (state / "authorized_keys").read_bytes() == key.read_bytes()


def test_startup_rejects_nonroot_unsafe_key_and_reused_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _public_key(tmp_path)
    monkeypatch.setattr(startup.os, "geteuid", lambda: 2000)
    with pytest.raises(startup.VastSshStartupError):
        startup.prepare_startup(authorized_keys_file=key, port=2222)

    monkeypatch.setattr(startup.os, "geteuid", lambda: 0)
    state = tmp_path / "existing"
    state.mkdir()
    monkeypatch.setattr(startup, "_STATE_ROOT", state)
    privsep = tmp_path / "run-sshd"
    monkeypatch.setattr(startup, "_PRIVSEP_ROOT", privsep)
    _mock_root_owned_privsep(monkeypatch, privsep)
    with pytest.raises(startup.VastSshStartupError, match="already exists"):
        startup.prepare_startup(authorized_keys_file=key, port=2222)

    bad = tmp_path / "bad.pub"
    bad.write_text("not-a-public-key\n")
    monkeypatch.setattr(startup, "_STATE_ROOT", tmp_path / "unused")
    with pytest.raises(startup.VastSshStartupError, match="invalid"):
        startup.prepare_startup(authorized_keys_file=bad, port=2222)
