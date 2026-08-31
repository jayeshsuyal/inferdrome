"""Remote real-GPU capture transport and verification boundaries."""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import tarfile
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest

import scripts.capture_real_gpu_over_ssh as remote
import scripts.real_gpu_capture as capture

COMMIT = "a" * 40
SOURCE_ARCHIVE_SHA256 = "sha256:" + "f" * 64
HOST_KEY_BYTES = b"[gpu.example.test]:22 ssh-ed25519 AAAATEST\n"
HOST_KEY_DIGEST = hashlib.sha256(HOST_KEY_BYTES).hexdigest()


def _waitpid_bounded(pid: int, *, timeout_seconds: float = 3.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return status
        time.sleep(0.01)
    raise AssertionError("signal-test child did not exit within the bound")


def _assert_embedded_python_compiles(script: str) -> None:
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY(?:\n|$)", script, flags=re.DOTALL)
    assert blocks
    for index, block in enumerate(blocks):
        compile(block, f"<generated-remote-python-{index}>", "exec")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _capture_tree(root: Path, *, receipt_commit: str = COMMIT) -> str:
    support = root / "support"
    support.mkdir(parents=True)
    packages = b"inferdrome==0.1\n"
    host_preparation = {
        "architecture": "x86_64",
        "model_directory": "/tmp/model",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "model_revision": "b" * 40,
        "prepared_at": "2026-08-11T00:00:00Z",
        "python_packages_sha256": "sha256:" + hashlib.sha256(packages).hexdigest(),
        "repository_commit": COMMIT,
        "schema_version": "inferdrome.real-gpu-host-preparation.v1",
        "vllm_wheel_filename": "vllm-0.26.0.whl",
        "vllm_wheel_sha256": f"sha256:{'c' * 64}",
    }
    _write_json(support / "host-preparation.json", host_preparation)
    (support / "python-packages.txt").write_bytes(packages)
    (support / "vllm-version.txt").write_text("0.26.0\n", encoding="utf-8")
    (support / "inferdrome-version.txt").write_text("0.1.0.dev0\n", encoding="utf-8")
    host_digest = (
        "sha256:"
        + hashlib.sha256((support / "host-preparation.json").read_bytes()).hexdigest()
    )
    _write_json(
        root / "single" / "real-gpu-example" / "demo-receipt.json",
        {
            "host_preparation_sha256": host_digest,
            "repository_commit": receipt_commit,
            "schema_version": "inferdrome.real-gpu-demo-receipt.v1",
        },
    )
    _write_json(
        root
        / "comparison"
        / "real-gpu-comparison-example"
        / "comparison-demo-receipt.json",
        {
            "host_preparation_sha256": host_digest,
            "repository_commit": receipt_commit,
            "schema_version": ("inferdrome.real-gpu-comparison-demo-receipt.v1"),
        },
    )
    return host_digest


@pytest.mark.parametrize(
    "value",
    ["ubuntu@203.0.113.4", "gpu.example.test", "[2001:db8::1]"],
)
def test_remote_destination_accepts_bounded_openssh_targets(value: str) -> None:
    assert remote._validate_destination(value) == value


@pytest.mark.parametrize(
    "value",
    ["-oProxyCommand=bad", "user@host name", "user@host;bad", "user@@host", ""],
)
def test_remote_destination_rejects_option_and_shell_injection(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        remote._validate_destination(value)


def test_optional_host_identity_digest_is_strict_lowercase_hex() -> None:
    assert remote._host_key_digest("a" * 64) == "a" * 64

    for value in ("A" * 64, "a" * 63, "g" * 64):
        with pytest.raises(argparse.ArgumentTypeError):
            remote._host_key_digest(value)


def test_ssh_transport_ignores_user_config_and_disables_forwarding(
    tmp_path: Path,
) -> None:
    options = remote._ssh_options(
        identity=tmp_path / "id_ed25519",
        known_hosts=tmp_path / "known-hosts",
        port=22,
    )
    rendered = " ".join(options)

    assert options[:2] == ["-F", "/dev/null"]
    assert "ForwardAgent=no" in rendered
    assert "IdentityAgent=none" in rendered
    assert "StrictHostKeyChecking=yes" in rendered
    assert "ForwardX11=no" in rendered
    assert "ClearAllForwardings=yes" in rendered
    assert "SendEnv=-*" in rendered
    assert "ProxyCommand=none" in rendered
    assert "ProxyJump=none" in rendered
    assert "IdentitiesOnly=yes" in rendered
    scp_rendered = " ".join(
        remote._scp_options(
            identity=None,
            known_hosts=tmp_path / "known-hosts",
            port=22,
        )
    )
    assert "IdentityAgent=none" in scp_rendered
    assert "StrictHostKeyChecking=yes" in scp_rendered


def test_mismatched_host_pin_fails_before_ssh_or_scp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_key_file = tmp_path / "known-hosts.pin"
    host_key_file.write_bytes(HOST_KEY_BYTES)
    source_archive = tmp_path / "repo.tar"
    source_archive.write_bytes(b"synthetic source archive")
    calls: list[object] = []
    local_identity = remote.resolve_executable_identity(sys.executable)
    monkeypatch.setattr(remote, "_local_executable", lambda _name: local_identity)
    monkeypatch.setattr(
        remote,
        "_run",
        lambda *args, **kwargs: calls.append((args, kwargs))
        or pytest.fail("mismatched pin must precede SSH/SCP"),
    )
    args = SimpleNamespace(
        destination="ubuntu@gpu.example.test",
        gpu_index=0,
        host_key_file=str(host_key_file),
        host_key_sha256="0" * 64,
        managed_capability_profile=None,
        output_root=str(tmp_path / "retrieved"),
        port=22,
        qwen3_gpu_tier=None,
        remote_timeout_seconds=1_800,
        startup_timeout_seconds=300,
    )

    with pytest.raises(remote.RemoteCaptureError, match="do not match their pin"):
        remote._capture_over_ssh(
            args,
            COMMIT,
            None,
            source_archive,
            SOURCE_ARCHIVE_SHA256,
        )

    assert calls == []


def test_every_capture_mode_requires_an_explicit_host_key_pin() -> None:
    with pytest.raises(remote.RemoteCaptureError, match="host-key-sha256"):
        remote._validate_capture_mode(
            SimpleNamespace(
                host_key_sha256=None,
                prospective=False,
            )
        )


def test_local_helpers_drop_provider_and_agent_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAMBDA_CLOUD_API_KEY", "synthetic-placeholder")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-placeholder")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/synthetic/agent.sock")
    remote._EXECUTABLE_IDENTITIES.clear()
    child = (
        "import os,sys; forbidden=("
        "'LAMBDA_CLOUD_API_KEY','AWS_ACCESS_KEY_ID','SSH_AUTH_SOCK'); "
        "sys.stdout.write('clean' if all(k not in os.environ for k in forbidden) "
        "else 'inherited')"
    )

    result = remote._run(
        [sys.executable, "-c", child],
        label="synthetic local helper",
        capture_output=True,
        timeout=5,
    )

    assert result.stdout == b"clean"


def test_local_helper_redacts_credential_shaped_failure_output() -> None:
    remote._EXECUTABLE_IDENTITIES.clear()
    child = (
        "import sys; "
        "sys.stderr.write('LAMBDA_CLOUD_API_KEY=synthetic-placeholder\\n'); "
        "raise SystemExit(7)"
    )

    with pytest.raises(remote.RemoteCaptureError) as caught:
        remote._run(
            [sys.executable, "-c", child],
            label="synthetic local helper",
            capture_output=True,
            timeout=5,
        )

    assert "synthetic-placeholder" not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


def test_uncaptured_local_helper_output_is_suppressed(
    capfd: pytest.CaptureFixture[str],
) -> None:
    remote._EXECUTABLE_IDENTITIES.clear()
    child = (
        "import sys; "
        "print('Bearer synthetic-placeholder'); "
        "print('API_KEY synthetic-placeholder', file=sys.stderr)"
    )

    result = remote._run(
        [sys.executable, "-c", child],
        label="synthetic local helper",
        capture_output=False,
        timeout=5,
    )

    captured = capfd.readouterr()
    assert result.returncode == 0
    assert "synthetic-placeholder" not in captured.out
    assert "synthetic-placeholder" not in captured.err


def test_bounded_download_child_uses_minimal_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAMBDA_CLOUD_API_KEY", "synthetic-placeholder")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/synthetic/agent.sock")
    remote._EXECUTABLE_IDENTITIES.clear()
    destination = tmp_path / "bounded.bin"
    child = (
        "import os,sys; forbidden=('LAMBDA_CLOUD_API_KEY','SSH_AUTH_SOCK'); "
        "sys.stdout.buffer.write(b'ok' if all(k not in os.environ for k in forbidden) "
        "else b'bad')"
    )

    remote._download_bounded_remote_file(
        [sys.executable, "-c", child],
        destination,
        minimum_size=2,
        maximum_size=3,
        timeout=5,
    )

    assert destination.read_bytes() == b"ok"


def test_exact_tree_export_excludes_removed_secret_and_git_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Inferdrome Test"],
        check=True,
    )
    secret = repository / "removed-secret.txt"
    secret.write_text("SENTINEL_PRIVATE_SECRET", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "secret history"],
        check=True,
    )
    secret.unlink()
    (repository / "README.md").write_text("safe tree\n", encoding="utf-8")
    nested = repository / "src" / "nested.txt"
    nested.parent.mkdir()
    nested.write_text("nested tree\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "safe tree"],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(remote, "REPOSITORY_ROOT", repository)
    archive = tmp_path / "repo.tar"

    digest, size = remote._create_source_archive(archive, commit)

    assert digest == "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    assert size == archive.stat().st_size
    assert b"SENTINEL_PRIVATE_SECRET" not in archive.read_bytes()
    with tarfile.open(archive, "r:") as retained:
        assert [member.name for member in retained.getmembers()] == [
            "README.md",
            "src",
            "src/nested.txt",
        ]

    environment = repository / ".env.production"
    environment.write_text(
        "LAMBDA_" + "CLOUD_API_KEY=do-not-transfer\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "tracked environment"],
        check=True,
    )
    environment_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(remote.RemoteCaptureError, match="secret-bearing"):
        remote._create_source_archive(tmp_path / "blocked-env.tar", environment_commit)

    environment.unlink()
    private_material = repository / "apparently-safe.txt"
    private_material.write_text(
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nnot-a-real-key\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "tracked private marker"],
        check=True,
    )
    private_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    literal_marker_archive = tmp_path / "literal-marker.tar"
    marker_digest, marker_size = remote._create_source_archive(
        literal_marker_archive,
        private_commit,
    )
    assert marker_digest == (
        "sha256:" + hashlib.sha256(literal_marker_archive.read_bytes()).hexdigest()
    )
    assert marker_size == literal_marker_archive.stat().st_size

    private_payload = base64.b64encode(
        b"openssh-key-v1\x00" + b"\x00" * 256
    ).decode("ascii")
    private_material.write_text(
        "-----BEGIN OPENSSH "
        + "PRIVATE KEY-----\n"
        + private_payload
        + "\n-----END OPENSSH "
        + "PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "tracked private key"],
        check=True,
    )
    actual_private_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    blocked_key_archive = tmp_path / "blocked-key.tar"
    with pytest.raises(remote.RemoteCaptureError, match="private-key material"):
        remote._create_source_archive(blocked_key_archive, actual_private_commit)
    assert not blocked_key_archive.exists()

    private_material.unlink()
    (repository / ".gitattributes").write_text(
        "README.md export-ignore\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "archive mutation"],
        check=True,
    )
    attributes_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(remote.RemoteCaptureError, match="attributes alter"):
        remote._create_source_archive(
            tmp_path / "blocked-attributes.tar", attributes_commit
        )


def test_exact_source_archive_accepts_the_current_tracked_head(tmp_path: Path) -> None:
    commit = subprocess.run(
        ["git", "-C", str(remote.REPOSITORY_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    expected_archive = subprocess.run(
        [
            "git",
            "-C",
            str(remote.REPOSITORY_ROOT),
            "archive",
            "--format=tar",
            commit,
        ],
        check=True,
        capture_output=True,
    ).stdout
    expected_digest = "sha256:" + hashlib.sha256(expected_archive).hexdigest()
    destination = tmp_path / "current-head.tar"

    digest, size = remote._create_source_archive(
        destination,
        commit,
        expected_archive_sha256=expected_digest,
    )

    assert digest == expected_digest
    assert size == len(expected_archive)
    assert destination.read_bytes() == expected_archive


@pytest.mark.parametrize(
    ("entry_count", "blob_size", "message"),
    [
        (1, remote._MAX_SOURCE_ARCHIVE_BYTES, "archive bound"),
        (100_001, 0, "file count"),
    ],
)
def test_git_source_archive_preflight_rejects_sparse_or_many_entry_trees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_count: int,
    blob_size: int,
    message: str,
) -> None:
    listing = b"".join(
        f"100644 blob {'a' * 40} {blob_size}\tfile-{index}.txt\0".encode()
        for index in range(entry_count)
    )
    calls: list[list[str]] = []

    def run(arguments: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = [str(value) for value in arguments]  # type: ignore[arg-type]
        calls.append(argv)
        if "ls-tree" in argv:
            return subprocess.CompletedProcess(argv, 0, listing, b"")
        pytest.fail("bounded source preflight must reject before git archive")

    monkeypatch.setattr(remote, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(remote, "_git_checkout_present", lambda: True)
    monkeypatch.setattr(remote, "_run", run)

    with pytest.raises(remote.RemoteCaptureError, match=message):
        remote._create_source_archive(tmp_path / "rejected.tar", COMMIT)
    assert ["archive" for call in calls if "archive" in call] == []


def test_git_source_archive_stream_rejects_over_cap_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_popen = subprocess.Popen
    code = (
        "import sys\n"
        "for _ in range(2049):\n"
        "    sys.stdout.buffer.write(b'x' * 65536)\n"
        "    sys.stdout.buffer.flush()\n"
    )

    def over_cap_popen(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        return original_popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )

    monkeypatch.setattr(remote.subprocess, "Popen", over_cap_popen)
    destination = tmp_path / "over-cap.tar"
    with pytest.raises(remote.RemoteCaptureError, match="exceeds"):
        remote._stream_git_source_archive(destination, COMMIT)
    assert destination.is_file()
    assert destination.stat().st_size <= remote._MAX_SOURCE_ARCHIVE_BYTES


def test_git_source_archive_preflight_accounts_for_long_path_pax_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_path = "/".join(["long-name"] * 30) + "/payload.txt"
    listing = (
        f"100644 blob {'a' * 40} {remote._MAX_SOURCE_ARCHIVE_BYTES - 1024}\t"
        f"{long_path}\0"
    ).encode()

    def run(arguments: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv = [str(value) for value in arguments]  # type: ignore[arg-type]
        if "ls-tree" in argv:
            return subprocess.CompletedProcess(argv, 0, listing, b"")
        pytest.fail("PAX-aware source preflight must reject before Git streaming")

    monkeypatch.setattr(remote, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(remote, "_git_checkout_present", lambda: True)
    monkeypatch.setattr(remote, "_run", run)

    with pytest.raises(remote.RemoteCaptureError, match="archive bound"):
        remote._create_source_archive(tmp_path / "long-path.tar", COMMIT)


def test_git_source_archive_stream_preserves_preexisting_or_replaced_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "destination-target"
    target.write_bytes(b"must remain")
    destination = tmp_path / "source.tar"
    destination.symlink_to(target)

    with pytest.raises(remote.RemoteCaptureError):
        remote._stream_git_source_archive(destination, COMMIT)
    assert destination.is_symlink()
    assert target.read_bytes() == b"must remain"

    destination.unlink()
    original_popen = subprocess.Popen
    original_lstat = os.lstat
    replaced = False

    def small_popen(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        return original_popen(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'archive')"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )

    def replace_on_final_check(path: os.PathLike[str] | str) -> os.stat_result:
        nonlocal replaced
        result = original_lstat(path)
        if Path(path) == destination and not replaced:
            destination.unlink()
            destination.symlink_to(target)
            replaced = True
            return original_lstat(path)
        return result

    monkeypatch.setattr(remote.subprocess, "Popen", small_popen)
    monkeypatch.setattr(remote.os, "lstat", replace_on_final_check)
    with pytest.raises(remote.RemoteCaptureError, match="changed while it was written"):
        remote._stream_git_source_archive(destination, COMMIT)
    assert destination.is_symlink()
    assert target.read_bytes() == b"must remain"


def test_exact_tree_export_supports_pinned_tree_without_git_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_repository = tmp_path / "git-repository"
    git_repository.mkdir()
    subprocess.run(["git", "init", "-q", str(git_repository)], check=True)
    subprocess.run(
        ["git", "-C", str(git_repository), "config", "user.email", "test@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repository), "config", "user.name", "Inferdrome Test"],
        check=True,
    )
    (git_repository / "README.md").write_text("exported tree\n", encoding="utf-8")
    (git_repository / "src").mkdir()
    (git_repository / "src" / "nested.txt").write_text("nested\n", encoding="utf-8")
    executable = git_repository / "src" / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    subprocess.run(["git", "-C", str(git_repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(git_repository), "commit", "-qm", "exported tree"],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(git_repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    original_archive = tmp_path / "original-git-archive.tar"
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repository),
            "archive",
            "--format=tar",
            f"--output={original_archive}",
            commit,
        ],
        check=True,
    )
    monkeypatch.setattr(remote, "REPOSITORY_ROOT", git_repository)
    expected_digest = (
        "sha256:" + hashlib.sha256(original_archive.read_bytes()).hexdigest()
    )
    with pytest.raises(remote.RemoteCaptureError, match="independent digest pin"):
        remote._create_source_archive(
            tmp_path / "wrong-git-pin.tar",
            commit,
            expected_archive_sha256="sha256:" + "0" * 64,
        )
    git_archive = tmp_path / "git-path.tar"
    git_digest, git_size = remote._create_source_archive(
        git_archive,
        commit,
        expected_archive_sha256=expected_digest,
    )
    assert git_archive.read_bytes() == original_archive.read_bytes()
    assert git_digest == expected_digest
    assert git_size == original_archive.stat().st_size
    assert remote._require_checkout(commit) == commit
    with pytest.raises(remote.RemoteCaptureError, match="not --expected-commit"):
        remote._require_checkout("0" * 40)

    original_stream = remote._stream_git_source_archive

    publication_target = tmp_path / "git-publication-target"
    publication_target.write_bytes(b"must remain")
    publication_destination = tmp_path / "git-publication-race.tar"
    original_rename = remote._rename_no_replace

    def race_git_publication(source: Path, destination: Path) -> None:
        assert destination == publication_destination
        publication_destination.symlink_to(publication_target)
        original_rename(source, destination)

    monkeypatch.setattr(remote, "_rename_no_replace", race_git_publication)
    with pytest.raises(remote.RemoteCaptureError, match="already exists"):
        remote._create_source_archive(
            publication_destination,
            commit,
            expected_archive_sha256=expected_digest,
        )
    assert publication_destination.is_symlink()
    assert publication_target.read_bytes() == b"must remain"
    monkeypatch.setattr(remote, "_rename_no_replace", original_rename)

    def mutate_after_stream(
        destination: Path, selected_commit: str
    ) -> tuple[int, tuple[int, int], int, str]:
        result = original_stream(destination, selected_commit)
        offset = original_archive.read_bytes().index(b"exported tree")
        assert os.pwrite(result[0], b"replaced tree", offset) == len(b"replaced tree")
        return result

    monkeypatch.setattr(remote, "_stream_git_source_archive", mutate_after_stream)
    with pytest.raises(
        remote.RemoteCaptureError, match="changed while it was validated"
    ):
        remote._create_source_archive(tmp_path / "mutated-after-stream.tar", commit)
    monkeypatch.setattr(remote, "_stream_git_source_archive", original_stream)

    repository = tmp_path / "export"
    repository.mkdir()
    with tarfile.open(original_archive, mode="r:") as archive:
        archive.extractall(repository, filter="data")
    marker = {
        "repository_commit": commit,
        "schema_version": "inferdrome.source-tree-export.v1",
        "source_archive_sha256": expected_digest,
        "transport": "git-archive-exact-head-tree-v1",
    }
    (repository / ".inferdrome-source-export.json").write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (repository / remote._RETAINED_SOURCE_ARCHIVE_NAME).write_bytes(
        original_archive.read_bytes()
    )
    monkeypatch.setattr(remote, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(remote, "_git_checkout_present", lambda: False)
    assert remote._require_checkout(commit) == commit
    with pytest.raises(remote.RemoteCaptureError, match="not --expected-commit"):
        remote._require_checkout("0" * 40)

    archive = tmp_path / "repo.tar"
    with pytest.raises(
        remote.RemoteCaptureError, match="independent expected archive digest"
    ):
        remote._create_source_archive(archive, commit)

    marker = json.loads((repository / ".inferdrome-source-export.json").read_text())
    marker["source_archive_sha256"] = "sha256:" + "0" * 64
    (repository / ".inferdrome-source-export.json").write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(remote.RemoteCaptureError, match="independent archive digest"):
        remote._create_source_archive(
            tmp_path / "rewritten-marker.tar",
            commit,
            expected_archive_sha256=expected_digest,
        )
    marker["source_archive_sha256"] = expected_digest
    (repository / ".inferdrome-source-export.json").write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (repository / "README.md").write_text("modified exported tree\n", encoding="utf-8")
    with pytest.raises(remote.RemoteCaptureError, match="disagrees with retained"):
        remote._create_source_archive(
            tmp_path / "modified-tree.tar",
            commit,
            expected_archive_sha256=expected_digest,
        )
    (repository / "README.md").write_text("exported tree\n", encoding="utf-8")
    digest, size = remote._create_source_archive(
        archive,
        commit,
        expected_archive_sha256=expected_digest,
    )

    assert digest == expected_digest
    assert size == archive.stat().st_size
    assert archive.read_bytes() == original_archive.read_bytes()
    assert b".inferdrome-source-export.json" not in archive.read_bytes()

    destination_target = tmp_path / "destination-target"
    destination_target.write_bytes(b"must remain")
    destination_link = tmp_path / "destination-link.tar"
    destination_link.symlink_to(destination_target)
    with pytest.raises(remote.RemoteCaptureError, match="destination already exists"):
        remote._create_source_archive(
            destination_link,
            commit,
            expected_archive_sha256=expected_digest,
        )
    assert destination_link.is_symlink()
    assert destination_target.read_bytes() == b"must remain"

    replacement_destination = tmp_path / "replacement-destination.tar"
    original_writer = remote._write_bytes_exclusive
    replaced_staging: list[Path] = []

    def write_then_replace(path: Path, content: bytes) -> tuple[int, int]:
        result = original_writer(path, content)
        path.unlink()
        path.symlink_to(destination_target)
        replaced_staging.append(path)
        return result

    monkeypatch.setattr(remote, "_write_bytes_exclusive", write_then_replace)
    with pytest.raises(remote.RemoteCaptureError, match="could not be copied"):
        remote._create_source_archive(
            replacement_destination,
            commit,
            expected_archive_sha256=expected_digest,
        )
    assert not replacement_destination.exists()
    assert len(replaced_staging) == 1
    assert replaced_staging[0].is_symlink()
    assert destination_target.read_bytes() == b"must remain"
    monkeypatch.setattr(remote, "_write_bytes_exclusive", original_writer)

    retained_publication_destination = tmp_path / "retained-publication-race.tar"

    def race_retained_publication(source: Path, destination: Path) -> None:
        assert destination == retained_publication_destination
        retained_publication_destination.symlink_to(destination_target)
        original_rename(source, destination)

    monkeypatch.setattr(remote, "_rename_no_replace", race_retained_publication)
    with pytest.raises(remote.RemoteCaptureError, match="already exists"):
        remote._create_source_archive(
            retained_publication_destination,
            commit,
            expected_archive_sha256=expected_digest,
        )
    assert retained_publication_destination.is_symlink()
    assert destination_target.read_bytes() == b"must remain"
    monkeypatch.setattr(remote, "_rename_no_replace", original_rename)

    (repository / remote._RETAINED_SOURCE_ARCHIVE_NAME).unlink()
    with pytest.raises(remote.RemoteCaptureError, match="retained source archive"):
        remote._create_source_archive(
            tmp_path / "missing-retained.tar",
            commit,
            expected_archive_sha256=expected_digest,
        )
    (repository / remote._RETAINED_SOURCE_ARCHIVE_NAME).write_bytes(
        original_archive.read_bytes()
    )
    retained = repository / remote._RETAINED_SOURCE_ARCHIVE_NAME
    retained_target = tmp_path / "retained-target"
    retained_target.write_bytes(original_archive.read_bytes())
    retained.unlink()
    retained.symlink_to(retained_target)
    with pytest.raises(remote.RemoteCaptureError, match="retained source archive"):
        remote._create_source_archive(
            tmp_path / "linked-retained.tar",
            commit,
            expected_archive_sha256=expected_digest,
        )
    assert retained.is_symlink()
    retained.unlink()
    hardlink_target = tmp_path / "hardlink-target"
    hardlink_target.write_bytes(original_archive.read_bytes())
    retained.hardlink_to(hardlink_target)
    with pytest.raises(remote.RemoteCaptureError, match="retained source archive"):
        remote._create_source_archive(
            tmp_path / "hardlinked-retained.tar",
            commit,
            expected_archive_sha256=expected_digest,
        )
    retained.unlink()
    retained.write_bytes(b"x")
    with retained.open("r+b") as stream:
        stream.truncate(remote._MAX_SOURCE_ARCHIVE_BYTES + 1)
    with pytest.raises(remote.RemoteCaptureError, match="retained source archive"):
        remote._create_source_archive(
            tmp_path / "oversized-retained.tar",
            commit,
            expected_archive_sha256=expected_digest,
        )
    retained.unlink()
    retained.write_bytes(b"truncated")
    with pytest.raises(remote.RemoteCaptureError, match="independent digest"):
        remote._create_source_archive(
            tmp_path / "truncated-retained.tar",
            commit,
            expected_archive_sha256=expected_digest,
        )
    retained.write_bytes(original_archive.read_bytes())
    (repository / ".codex-venv").mkdir()
    with pytest.raises(remote.RemoteCaptureError, match="disagrees with retained"):
        remote._create_source_archive(
            tmp_path / "rejected-env.tar",
            commit,
            expected_archive_sha256=expected_digest,
        )


def test_remote_command_pins_commit_and_bounds_workload() -> None:
    script = remote._remote_capture_script(
        "/tmp/inferdrome-safe",
        COMMIT,
        SOURCE_ARCHIVE_SHA256,
        gpu_index=0,
        startup_timeout_seconds=900,
        remote_timeout_seconds=9_900,
    )

    assert COMMIT in script
    assert SOURCE_ARCHIVE_SHA256 in script
    assert "git clone" not in script
    assert "env -i" in script
    assert "timeout --foreground --signal=TERM --kill-after=60s 9900s" in script
    assert "--capture-root /tmp/inferdrome-safe/capture" in script
    assert "unset CUDA_VISIBLE_DEVICES NVIDIA_VISIBLE_DEVICES" in script
    assert (
        subprocess.run(
            ["bash", "-n"],
            input=script,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )
    _assert_embedded_python_compiles(script)


def test_qwen3_remote_command_requires_explicit_profile() -> None:
    script = remote._remote_capture_script(
        "/tmp/inferdrome-safe",
        COMMIT,
        SOURCE_ARCHIVE_SHA256,
        gpu_index=0,
        startup_timeout_seconds=300,
        remote_timeout_seconds=1_500,
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        qwen3_gpu_tier="a10-24gb-pcie",
    )

    assert "--managed-capability-profile" in script
    assert "--qwen3-gpu-tier" in script
    assert "a10-24gb-pcie" in script
    assert remote._QWEN3_PROFILE_ID in script
    assert "\n+  --managed-capability-profile" not in script
    assert "\n+  --qwen3-gpu-tier" not in script
    preflight = remote._remote_preflight_script(
        "/tmp/inferdrome-safe",
        gpu_index=0,
        expected_gpu_model="NVIDIA A10",
    )
    assert "nvidia-smi --id=0" in preflight
    assert "NVIDIA A10" in preflight
    assert "at least 40 GiB free" in preflight
    assert (
        subprocess.run(
            ["bash", "-n"],
            input=script,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )
    _assert_embedded_python_compiles(script)
    assert (
        subprocess.run(
            ["bash", "-n"],
            input=preflight,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def test_qwen3_capture_mode_enforces_lambda_rate_instance_and_cap() -> None:
    base = {
        "host_key_sha256": HOST_KEY_DIGEST,
        "lambda_billing_started_at": datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        "lambda_hourly_rate_usd": Decimal("1.29"),
        "lambda_instance_id": "b" * 32,
        "lambda_instance_type_name": "gpu_1x_a10",
        "managed_capability_profile": remote._QWEN3_PROFILE_ID,
        "max_cost_usd": Decimal("0.75"),
        "qwen3_gpu_tier": "a10-24gb-pcie",
        "identity_file": "/tmp/inferdrome-key",
        "remote_timeout_seconds": 1_500,
        "startup_timeout_seconds": 300,
    }
    remote._validate_capture_mode(SimpleNamespace(**base))

    for mutation, message in (
        ({"lambda_instance_id": None}, "instance-id"),
        ({"lambda_instance_type_name": None}, "instance-type-name"),
        ({"lambda_hourly_rate_usd": Decimal("1.30")}, "1.29"),
        ({"max_cost_usd": Decimal("0.76")}, "0.75"),
        ({"identity_file": None}, "identity"),
        ({"startup_timeout_seconds": 301}, "300-second"),
        ({"lambda_billing_started_at": None}, "billing-started-at"),
    ):
        values = {**base, **mutation}
        with pytest.raises(remote.RemoteCaptureError, match=message):
            remote._validate_capture_mode(SimpleNamespace(**values))

    with pytest.raises(remote.RemoteCaptureError, match="unsupported"):
        remote._validate_capture_mode(
            SimpleNamespace(
                **{
                    **base,
                    "managed_capability_profile": "unknown-profile",
                }
            )
        )


def test_qwen3_a100_capture_mode_freezes_exact_tier_rate_and_cap() -> None:
    base = {
        "host_key_sha256": HOST_KEY_DIGEST,
        "identity_file": "/tmp/inferdrome-key",
        "lambda_billing_started_at": datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        "lambda_hourly_rate_usd": Decimal("1.99"),
        "lambda_instance_id": "b" * 32,
        "lambda_instance_type_name": "gpu_1x_a100_runtime_api_value",
        "managed_capability_profile": remote._QWEN3_PROFILE_ID,
        "max_cost_usd": Decimal("1.25"),
        "qwen3_gpu_tier": "a100-40gb-pcie",
        "remote_timeout_seconds": 1_500,
        "startup_timeout_seconds": 300,
    }

    remote._validate_capture_mode(SimpleNamespace(**base))

    for mutation, message in (
        ({"lambda_hourly_rate_usd": Decimal("1.98")}, "1.99"),
        ({"max_cost_usd": Decimal("1.26")}, "1.25"),
        ({"qwen3_gpu_tier": "h100-80gb-pcie"}, "3.29"),
    ):
        with pytest.raises(remote.RemoteCaptureError, match=message):
            remote._validate_capture_mode(SimpleNamespace(**{**base, **mutation}))


def test_qwen3_a100_sxm4_capture_mode_freezes_exact_rate_and_cap() -> None:
    base = {
        "host_key_sha256": HOST_KEY_DIGEST,
        "identity_file": "/tmp/inferdrome-key",
        "lambda_billing_started_at": datetime(2026, 8, 23, 17, 0, tzinfo=UTC),
        "lambda_hourly_rate_usd": Decimal("1.99"),
        "lambda_instance_id": "b" * 32,
        "lambda_instance_type_name": "gpu_1x_a100_sxm4",
        "managed_capability_profile": remote._QWEN3_PROFILE_ID,
        "max_cost_usd": Decimal("1.25"),
        "qwen3_gpu_tier": "a100-40gb-sxm4",
        "remote_timeout_seconds": 1_500,
        "startup_timeout_seconds": 300,
    }

    remote._validate_capture_mode(SimpleNamespace(**base))

    for mutation, message in (
        ({"lambda_hourly_rate_usd": Decimal("2.00")}, "1.99"),
        ({"max_cost_usd": Decimal("1.26")}, "1.25"),
        ({"qwen3_gpu_tier": "h100-80gb-pcie"}, "3.29"),
    ):
        with pytest.raises(remote.RemoteCaptureError, match=message):
            remote._validate_capture_mode(SimpleNamespace(**{**base, **mutation}))


def test_qwen3_h100_capture_mode_freezes_exact_tier_rate_and_cap() -> None:
    base = {
        "host_key_sha256": HOST_KEY_DIGEST,
        "identity_file": "/tmp/inferdrome-key",
        "lambda_billing_started_at": datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        "lambda_hourly_rate_usd": Decimal("3.29"),
        "lambda_instance_id": "b" * 32,
        "lambda_instance_type_name": "gpu_1x_h100_runtime_api_value",
        "managed_capability_profile": remote._QWEN3_PROFILE_ID,
        "max_cost_usd": Decimal("2.25"),
        "qwen3_gpu_tier": "h100-80gb-pcie",
        "remote_timeout_seconds": 1_500,
        "startup_timeout_seconds": 300,
    }

    remote._validate_capture_mode(SimpleNamespace(**base))

    for mutation, message in (
        ({"lambda_hourly_rate_usd": Decimal("4.29")}, "3.29"),
        ({"max_cost_usd": Decimal("2.26")}, "2.25"),
        ({"qwen3_gpu_tier": "a100-40gb-pcie"}, "1.99"),
    ):
        with pytest.raises(remote.RemoteCaptureError, match=message):
            remote._validate_capture_mode(SimpleNamespace(**{**base, **mutation}))


def test_qwen3_phase_budget_fits_the_exact_cost_window() -> None:
    allowed_seconds = int(Decimal("0.75") / Decimal("1.29") * Decimal(3_600))

    assert allowed_seconds == 2_093
    assert sum(remote._QWEN3_PHASE_BUDGET_SECONDS.values()) == 2_078
    assert remote._QWEN3_POST_REMOTE_BUDGET_SECONDS == 298
    assert remote._QWEN3_TERMINATION_SAFETY_MARGIN_SECONDS == 300


def test_a100_phase_budget_fits_its_exact_cost_window() -> None:
    policy = remote.qwen3_gpu_tier_policy("a100-40gb-pcie")

    assert policy.allowed_seconds == 2_261
    assert sum(remote._QWEN3_PHASE_BUDGET_SECONDS.values()) == 2_078
    assert sum(remote._QWEN3_PHASE_BUDGET_SECONDS.values()) < policy.allowed_seconds


def test_h100_phase_budget_preserves_termination_slack() -> None:
    policy = remote.qwen3_gpu_tier_policy("h100-80gb-pcie")

    assert policy.allowed_seconds == 2_462
    assert sum(remote._QWEN3_PHASE_BUDGET_SECONDS.values()) == 2_078
    assert (
        policy.allowed_seconds - sum(remote._QWEN3_PHASE_BUDGET_SECONDS.values()) == 384
    )


def test_qwen3_transfer_metadata_rejects_oversized_archive(tmp_path: Path) -> None:
    metadata = tmp_path / "capture.tar.gz.metadata.json"
    _write_json(
        metadata,
        {
            "archive_name": "capture.tar.gz",
            "archive_sha256": "sha256:" + "a" * 64,
            "schema_version": "inferdrome.qwen3-transfer-metadata.v1",
            "size_bytes": remote._QWEN3_MAX_ARCHIVE_BYTES + 1,
        },
    )

    with pytest.raises(remote.RemoteCaptureError, match="invalid"):
        remote._read_transfer_metadata(metadata)


def test_qwen3_dry_run_discloses_termination_before_semantic_verification(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote,
        "_create_source_archive",
        lambda _path, _commit, **_kwargs: (SOURCE_ARCHIVE_SHA256, 1_024),
    )
    args = SimpleNamespace(
        destination="ubuntu@gpu.example.test",
        gpu_index=0,
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name="gpu_1x_a10",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("0.75"),
        qwen3_gpu_tier="a10-24gb-pcie",
        remote_timeout_seconds=1_500,
        startup_timeout_seconds=300,
    )

    remote._dry_run(args, COMMIT, None)

    plan = json.loads(capsys.readouterr().out)
    assert plan["expected_gpu_model"] == "NVIDIA A10"
    assert plan["expected_lambda_instance_type"] == "gpu_1x_a10"
    assert plan["qwen3_gpu_tier"] == "a10-24gb-pcie"
    assert plan["source_archive_sha256"] == SOURCE_ARCHIVE_SHA256
    assert plan["source_archive_bytes"] == 1_024
    assert sum(plan["phase_budget_seconds"].values()) == 2_078
    assert plan["managed_capability_profile"] == remote._QWEN3_PROFILE_ID
    watchdog_index = plan["steps"].index(
        "arm and validate the independent Lambda termination watchdog"
    )
    guarded_source_index = plan["steps"].index(
        "rebuild the checked source archive under watchdog protection"
    )
    termination_index = plan["steps"].index(
        "terminate and confirm the Lambda instance through its API"
    )
    verification_index = plan["steps"].index(
        "independently recalculate and verify every retrieved proof artifact"
    )
    assert watchdog_index < guarded_source_index
    assert termination_index < verification_index


def test_qwen3_a100_dry_run_binds_runtime_instance_type_and_exact_gpu(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote,
        "_create_source_archive",
        lambda _path, _commit, **_kwargs: (SOURCE_ARCHIVE_SHA256, 1_024),
    )
    args = SimpleNamespace(
        destination="ubuntu@gpu.example.test",
        gpu_index=0,
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.99"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name="gpu_1x_a100_api_runtime",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("1.25"),
        qwen3_gpu_tier="a100-40gb-pcie",
        remote_timeout_seconds=1_500,
        startup_timeout_seconds=300,
    )

    remote._dry_run(args, COMMIT, None)
    plan = json.loads(capsys.readouterr().out)

    assert plan["expected_gpu_model"] == "NVIDIA A100-PCIE-40GB"
    assert plan["expected_lambda_instance_type"] == "gpu_1x_a100_api_runtime"
    assert plan["qwen3_gpu_tier"] == "a100-40gb-pcie"
    assert plan["lambda_cost_guard"]["hourly_rate_usd"] == "1.99"
    assert plan["lambda_cost_guard"]["max_cost_usd"] == "1.25"


def test_qwen3_a100_sxm4_dry_run_binds_extension_instance_and_gpu(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote,
        "_create_source_archive",
        lambda _path, _commit, **_kwargs: (SOURCE_ARCHIVE_SHA256, 1_024),
    )
    args = SimpleNamespace(
        destination="ubuntu@gpu.example.test",
        gpu_index=0,
        lambda_billing_started_at=datetime(2026, 8, 23, 17, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.99"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name="gpu_1x_a100_sxm4",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("1.25"),
        qwen3_gpu_tier="a100-40gb-sxm4",
        remote_timeout_seconds=1_500,
        startup_timeout_seconds=300,
    )

    remote._dry_run(args, COMMIT, None)
    plan = json.loads(capsys.readouterr().out)

    assert plan["expected_gpu_model"] == "NVIDIA A100-SXM4-40GB"
    assert plan["expected_lambda_instance_type"] == "gpu_1x_a100_sxm4"
    assert plan["qwen3_gpu_tier"] == "a100-40gb-sxm4"
    assert plan["lambda_cost_guard"]["hourly_rate_usd"] == "1.99"
    assert plan["lambda_cost_guard"]["max_cost_usd"] == "1.25"


def test_qwen3_h100_dry_run_binds_runtime_instance_type_and_exact_gpu(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote,
        "_create_source_archive",
        lambda _path, _commit, **_kwargs: (SOURCE_ARCHIVE_SHA256, 1_024),
    )
    args = SimpleNamespace(
        destination="ubuntu@gpu.example.test",
        gpu_index=0,
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("3.29"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name="gpu_1x_h100_api_runtime",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("2.25"),
        qwen3_gpu_tier="h100-80gb-pcie",
        remote_timeout_seconds=1_500,
        startup_timeout_seconds=300,
    )

    remote._dry_run(args, COMMIT, None)
    plan = json.loads(capsys.readouterr().out)

    assert plan["expected_gpu_model"] == "NVIDIA H100 PCIe"
    assert plan["expected_lambda_instance_type"] == "gpu_1x_h100_api_runtime"
    assert plan["qwen3_gpu_tier"] == "h100-80gb-pcie"
    assert plan["lambda_cost_guard"]["hourly_rate_usd"] == "3.29"
    assert plan["lambda_cost_guard"]["max_cost_usd"] == "2.25"


def test_live_cost_window_clamps_remote_work_before_termination() -> None:
    now = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
    deadline = datetime(2026, 8, 20, 20, 20, tzinfo=UTC)

    assert (
        remote._effective_remote_timeout(
            1_500,
            termination_deadline=deadline,
            now=now,
        )
        == 902
    )
    with pytest.raises(remote.RemoteCaptureError, match="less than 300"):
        remote._effective_remote_timeout(
            1_500,
            termination_deadline=datetime(2026, 8, 20, 20, 5, tzinfo=UTC),
            now=now,
        )
    transfer_deadline = remote._transfer_deadline(
        termination_deadline=deadline,
        now=now,
        monotonic=100.0,
    )
    assert transfer_deadline == 1_277.0
    assert (
        remote._remaining_transfer_timeout(
            transfer_deadline,
            phase_limit_seconds=180,
            monotonic=1_150.0,
        )
        == 127
    )


def test_qwen3_fake_ssh_retrieval_stops_at_checksum_before_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_bytes = b"bounded fake archive bytes"
    archive_digest = hashlib.sha256(archive_bytes).hexdigest()
    calls: list[str] = []

    def fake_run(
        arguments: object,
        *,
        label: str,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = list(arguments)  # type: ignore[arg-type]
        calls.append(label)
        if label == "remote GPU preflight":
            assert "StrictHostKeyChecking=yes" in argv
            assert "IdentityAgent=none" in argv
            assert "NVIDIA A10" in argv[-1]
        elif label == "remote proof pack":
            assert remote._QWEN3_PROFILE_ID in argv[-1]
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    def download_metadata(
        _arguments: object,
        destination: Path,
        **kwargs: object,
    ) -> None:
        calls.append("bounded metadata retrieval")
        assert kwargs["minimum_size"] == 2
        assert kwargs["maximum_size"] == 4_096
        _write_json(
            destination,
            {
                "archive_name": "capture.tar.gz",
                "archive_sha256": f"sha256:{archive_digest}",
                "schema_version": "inferdrome.qwen3-transfer-metadata.v1",
                "size_bytes": len(archive_bytes),
            },
        )

    def download_archive(
        _arguments: object,
        destination: Path,
        **kwargs: object,
    ) -> None:
        calls.append("bounded archive retrieval")
        assert kwargs["expected_size"] == len(archive_bytes)
        assert kwargs["expected_sha256"] == f"sha256:{archive_digest}"
        destination.write_bytes(archive_bytes)

    monkeypatch.setattr(remote, "_run", fake_run)
    monkeypatch.setattr(remote, "_download_bounded_remote_file", download_metadata)
    monkeypatch.setattr(remote, "_download_exact_remote_file", download_archive)
    local_identity = remote.resolve_executable_identity(sys.executable)
    monkeypatch.setattr(remote, "_local_executable", lambda _name: local_identity)
    host_key_file = tmp_path / "known-hosts.pin"
    host_key_file.write_bytes(HOST_KEY_BYTES)
    args = SimpleNamespace(
        destination="ubuntu@gpu.example.test",
        gpu_index=0,
        host_key_file=str(host_key_file),
        host_key_sha256=HOST_KEY_DIGEST,
        lambda_instance_type_name="gpu_1x_a10",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        output_root=str(tmp_path / "retrieved"),
        port=22,
        qwen3_gpu_tier="a10-24gb-pcie",
        remote_timeout_seconds=1_500,
        startup_timeout_seconds=300,
    )
    source_archive = tmp_path / "repo.tar"
    source_archive.write_bytes(b"exact source")
    deadline = datetime(2099, 8, 20, 20, 20, tzinfo=UTC)

    result = remote._capture_over_ssh(
        args,
        COMMIT,
        None,
        source_archive,
        SOURCE_ARCHIVE_SHA256,
        termination_deadline=deadline,
    )

    assert (result / "capture.tar.gz").read_bytes() == archive_bytes
    assert not (result / "capture").exists()
    receipt = json.loads((result / "retrieval-receipt.json").read_text())
    assert receipt["gpu_tier_id"] == "a10-24gb-pcie"
    assert receipt["lambda_instance_type_name"] == "gpu_1x_a10"
    assert receipt["schema_version"] == "inferdrome.qwen3-gpu-retrieval.v2"
    assert receipt["semantic_verification"] == (
        "PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"
    )
    assert calls[-2:] == [
        "bounded metadata retrieval",
        "bounded archive retrieval",
    ]


def test_bounded_remote_download_discards_overflow(tmp_path: Path) -> None:
    destination = tmp_path / "bounded.bin"

    with pytest.raises(remote.RemoteCaptureError, match="maximum byte count"):
        remote._download_bounded_remote_file(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 4097)"],
            destination,
            minimum_size=2,
            maximum_size=4_096,
            timeout=5,
        )

    assert not destination.exists()


def test_qwen3_controller_orders_capture_termination_then_offline_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    deadline = datetime(2026, 8, 20, 20, 20, tzinfo=UTC)
    watchdog = SimpleNamespace(
        cost_window=SimpleNamespace(deadline=deadline),
        instance=SimpleNamespace(instance_id="b" * 32),
    )
    termination = SimpleNamespace(final_status="absent")
    captured = tmp_path / "capture-record"

    def create_source(path: Path, commit: str) -> tuple[str, int]:
        events.append("source-tree")
        assert commit == COMMIT
        path.write_bytes(b"exact tree")
        return SOURCE_ARCHIVE_SHA256, len(b"exact tree")

    def arm(_args: object) -> object:
        events.append("arm")
        return watchdog

    monkeypatch.setattr(remote, "_create_source_archive", create_source)
    monkeypatch.setattr(remote, "_arm_lambda_watchdog", arm)

    def capture_over_ssh(*_args: object, **kwargs: object) -> Path:
        events.append("capture-checksum")
        assert kwargs["termination_deadline"] == deadline
        return captured

    def terminate(handle: object) -> object:
        events.append("terminate")
        assert handle is watchdog
        return termination

    def finalize(*_args: object, **_kwargs: object) -> Path:
        events.append("offline-verify")
        return captured

    monkeypatch.setattr(remote, "_capture_over_ssh", capture_over_ssh)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        terminate,
    )
    monkeypatch.setattr(remote, "_finalize_qwen3_capture", finalize)
    args = SimpleNamespace(managed_capability_profile=remote._QWEN3_PROFILE_ID)

    assert remote._capture(args, COMMIT, None) == captured
    assert events == [
        "arm",
        "source-tree",
        "capture-checksum",
        "terminate",
        "offline-verify",
    ]


def test_live_source_failure_still_terminates_the_guarded_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    watchdog = SimpleNamespace(
        instance=SimpleNamespace(instance_id="b" * 32),
    )

    def arm(_args: object) -> object:
        events.append("arm")
        return watchdog

    def fail_source(_path: Path, _commit: str) -> tuple[str, int]:
        events.append("source-failed")
        raise remote.RemoteCaptureError("tracked secret rejected")

    def terminate(handle: object) -> object:
        events.append("terminate")
        assert handle is watchdog
        return SimpleNamespace(final_status="absent")

    monkeypatch.setattr(remote, "_arm_lambda_watchdog", arm)
    monkeypatch.setattr(remote, "_create_source_archive", fail_source)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        terminate,
    )

    with pytest.raises(remote.RemoteCaptureError, match="tracked secret rejected"):
        remote._capture(SimpleNamespace(), COMMIT, None)

    assert events == ["arm", "source-failed", "terminate"]


def test_qwen3_finalization_binds_termination_then_publishes_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_path = tmp_path / "retrieved"
    capture_path.mkdir()
    archive = capture_path / "capture.tar.gz"
    archive.write_bytes(b"qwen3 archive")
    archive_sha256 = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    (capture_path / "capture.tar.gz.sha256").write_text(
        f"{archive_sha256.removeprefix('sha256:')}  capture.tar.gz\n",
        encoding="ascii",
    )
    started = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
    cost_window = remote.lambda_gpu_guard.CostWindow(
        billing_started_at=started,
        deadline=datetime(2026, 8, 20, 20, 29, 53, tzinfo=UTC),
        cost_limit_deadline=datetime(2026, 8, 20, 20, 34, 53, tzinfo=UTC),
        allowed_seconds=2_093,
        termination_safety_margin_seconds=300,
        hourly_rate_usd=Decimal("1.29"),
        max_cost_usd=Decimal("0.75"),
    )
    termination = remote.lambda_gpu_guard.TerminationResult(
        instance_id="b" * 32,
        final_status="absent",
        request_sent=True,
        confirmed_at=datetime(2026, 8, 20, 20, 20, tzinfo=UTC),
    )
    guard_root = tmp_path / "guard"
    guard_root.mkdir()
    guard_receipt = guard_root / "termination-receipt.json"
    guard_receipt.write_text(
        json.dumps(
            {
                "cost_window": cost_window.public_record(),
                "record_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
                "schema_version": "inferdrome.lambda-termination-receipt.v2",
                "termination": termination.public_record(),
                "trigger": "controller-finally",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    verification = {
        "capture_manifest_sha256": "sha256:" + "d" * 64,
        "profile_id": remote._QWEN3_PROFILE_ID,
        "repository_commit": COMMIT,
        "run": {"run_id": "run-" + "e" * 32},
        "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
        "valid": True,
    }
    _write_json(
        capture_path / "retrieval-receipt.json",
        {
            "archive_sha256": archive_sha256,
            "billing_action_required": "PROVIDER_TERMINATION_PENDING",
            "managed_capability_profile": remote._QWEN3_PROFILE_ID,
            "repository_commit": COMMIT,
            "schema_version": "inferdrome.qwen3-gpu-retrieval.v1",
            "semantic_verification": ("PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"),
            "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
            "ssh_host_identity_sha256": "sha256:" + "9" * 64,
            "verified_at": "2026-08-20T20:19:00Z",
        },
    )
    monkeypatch.setattr(
        remote.qwen3_gpu_capture,
        "verify_capture_archive",
        lambda *_args, **_kwargs: {
            "archive_sha256": archive_sha256,
            "capture_manifest_sha256": verification["capture_manifest_sha256"],
            "verification": verification,
        },
    )

    def extract(
        _archive: Path,
        destination: Path,
        *,
        expected_archive_sha256: str | None = None,
    ) -> Path:
        assert expected_archive_sha256 == archive_sha256
        extracted = destination / "capture"
        (extracted / "runs").mkdir(parents=True)
        return extracted

    monkeypatch.setattr(remote.real_gpu_capture, "extract_capture_archive", extract)
    monkeypatch.setattr(
        remote.qwen3_gpu_capture,
        "verify_capture",
        lambda *_args, **_kwargs: verification,
    )
    instance = remote.lambda_gpu_guard.LambdaInstance(
        instance_id="b" * 32,
        ip="203.0.113.10",
        hostname="gpu.example.test",
        status="active",
        hourly_rate_usd=Decimal("1.29"),
        instance_type_name="gpu_1x_a10",
    )
    watchdog = SimpleNamespace(
        cost_window=cost_window,
        instance=instance,
        receipt_path=guard_receipt,
    )

    assert (
        remote._finalize_qwen3_capture(
            capture_path,
            commit=COMMIT,
            watchdog=watchdog,
            termination=termination,
        )
        == capture_path
    )
    assert (
        remote._finalize_qwen3_capture(
            capture_path,
            commit=COMMIT,
            watchdog=watchdog,
            termination=termination,
        )
        == capture_path
    )
    semantic = json.loads(
        (capture_path / "semantic-verification.json").read_text(encoding="utf-8")
    )
    assert semantic["semantic_verification"] == ("VALID_AFTER_PROVIDER_TERMINATION")
    assert semantic["provider_termination"] == termination.public_record()
    assert semantic["provider_instance"] == instance.public_record()
    assert (capture_path / "lambda-termination-receipt.json").read_bytes() == (
        guard_receipt.read_bytes()
    )


def test_qwen3_a100_finalization_publishes_tier_bound_v2_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_path = tmp_path / "retrieved"
    capture_path.mkdir()
    archive = capture_path / "capture.tar.gz"
    archive.write_bytes(b"qwen3 a100 archive")
    archive_sha256 = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    (capture_path / "capture.tar.gz.sha256").write_text(
        f"{archive_sha256.removeprefix('sha256:')}  capture.tar.gz\n",
        encoding="ascii",
    )
    instance_type = "gpu_1x_a100_api_runtime"
    _write_json(
        capture_path / "retrieval-receipt.json",
        {
            "archive_sha256": archive_sha256,
            "billing_action_required": "PROVIDER_TERMINATION_PENDING",
            "gpu_tier_id": "a100-40gb-pcie",
            "lambda_instance_type_name": instance_type,
            "managed_capability_profile": remote._QWEN3_PROFILE_ID,
            "repository_commit": COMMIT,
            "schema_version": "inferdrome.qwen3-gpu-retrieval.v2",
            "semantic_verification": ("PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"),
            "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
            "ssh_host_identity_sha256": "sha256:" + "9" * 64,
            "verified_at": "2026-08-20T20:19:00Z",
        },
    )
    cost_window = remote.lambda_gpu_guard.CostWindow(
        billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        deadline=datetime(2026, 8, 20, 20, 32, 41, tzinfo=UTC),
        cost_limit_deadline=datetime(2026, 8, 20, 20, 37, 41, tzinfo=UTC),
        allowed_seconds=2_261,
        termination_safety_margin_seconds=300,
        hourly_rate_usd=Decimal("1.99"),
        max_cost_usd=Decimal("1.25"),
    )
    termination = remote.lambda_gpu_guard.TerminationResult(
        instance_id="b" * 32,
        final_status="absent",
        request_sent=True,
        confirmed_at=datetime(2026, 8, 20, 20, 20, tzinfo=UTC),
    )
    guard_root = tmp_path / "guard"
    guard_root.mkdir()
    guard_receipt = guard_root / "termination-receipt.json"
    _write_json(
        guard_receipt,
        {
            "cost_window": cost_window.public_record(),
            "record_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
            "schema_version": "inferdrome.lambda-termination-receipt.v2",
            "termination": termination.public_record(),
            "trigger": "controller-finally",
        },
    )
    gpu_target = remote.qwen3_gpu_tier_policy("a100-40gb-pcie").public_target()
    verification = {
        "capture_manifest_sha256": "sha256:" + "d" * 64,
        "gpu_target": gpu_target,
        "gpu_tier_id": "a100-40gb-pcie",
        "profile_id": remote._QWEN3_PROFILE_ID,
        "repository_commit": COMMIT,
        "run": {"run_id": "run-" + "e" * 32},
        "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
        "valid": True,
    }

    def verify_archive(*_args: object, **kwargs: object) -> dict[str, object]:
        assert kwargs["expected_gpu_tier_id"] == "a100-40gb-pcie"
        return {
            "archive_sha256": archive_sha256,
            "capture_manifest_sha256": verification["capture_manifest_sha256"],
            "verification": verification,
        }

    monkeypatch.setattr(
        remote.qwen3_gpu_capture,
        "verify_capture_archive",
        verify_archive,
    )

    def extract(
        _archive: Path,
        destination: Path,
        *,
        expected_archive_sha256: str | None = None,
    ) -> Path:
        assert expected_archive_sha256 == archive_sha256
        extracted = destination / "capture"
        (extracted / "runs").mkdir(parents=True)
        return extracted

    monkeypatch.setattr(remote.real_gpu_capture, "extract_capture_archive", extract)

    def verify_extracted(*_args: object, **kwargs: object) -> dict[str, object]:
        assert kwargs["expected_gpu_tier_id"] == "a100-40gb-pcie"
        return verification

    monkeypatch.setattr(
        remote.qwen3_gpu_capture,
        "verify_capture",
        verify_extracted,
    )
    instance = remote.lambda_gpu_guard.LambdaInstance(
        instance_id="b" * 32,
        ip="203.0.113.10",
        hostname="gpu.example.test",
        status="active",
        hourly_rate_usd=Decimal("1.99"),
        instance_type_name=instance_type,
    )
    watchdog = SimpleNamespace(
        cost_window=cost_window,
        instance=instance,
        receipt_path=guard_receipt,
    )

    assert (
        remote._finalize_qwen3_capture(
            capture_path,
            commit=COMMIT,
            watchdog=watchdog,
            termination=termination,
        )
        == capture_path
    )
    semantic = json.loads(
        (capture_path / "semantic-verification.json").read_text(encoding="utf-8")
    )

    assert semantic["schema_version"] == ("inferdrome.qwen3-offline-verification.v2")
    assert semantic["gpu_tier_id"] == "a100-40gb-pcie"
    assert semantic["gpu_target"] == gpu_target
    assert semantic["lambda_instance_type_name"] == instance_type


def test_qwen3_offline_resume_uses_retained_guard_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
    cost_window = remote.lambda_gpu_guard.CostWindow(
        billing_started_at=started,
        deadline=datetime(2026, 8, 20, 20, 29, 53, tzinfo=UTC),
        cost_limit_deadline=datetime(2026, 8, 20, 20, 34, 53, tzinfo=UTC),
        allowed_seconds=2_093,
        termination_safety_margin_seconds=300,
        hourly_rate_usd=Decimal("1.29"),
        max_cost_usd=Decimal("0.75"),
    )
    instance = remote.lambda_gpu_guard.LambdaInstance(
        instance_id="b" * 32,
        ip="203.0.113.10",
        hostname="gpu.example.test",
        status="active",
        hourly_rate_usd=Decimal("1.29"),
        instance_type_name="gpu_1x_a10",
    )
    termination = remote.lambda_gpu_guard.TerminationResult(
        instance_id=instance.instance_id,
        final_status="terminated",
        request_sent=True,
        confirmed_at=datetime(2026, 8, 20, 20, 20, tzinfo=UTC),
    )
    guard_root = tmp_path / "guard"
    _write_json(
        guard_root / "guard-armed.json",
        {
            "cost_window": cost_window.public_record(),
            "instance": instance.public_record(),
            "record_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
            "schema_version": "inferdrome.lambda-guard-armed.v2",
            "watchdog_ready": True,
        },
    )
    termination_receipt = {
        "cost_window": cost_window.public_record(),
        "record_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
        "schema_version": "inferdrome.lambda-termination-receipt.v2",
        "termination": termination.public_record(),
        "trigger": "cost-deadline",
    }
    _write_json(guard_root / "termination-receipt.json", termination_receipt)
    capture_path = tmp_path / "retrieved"
    _write_json(
        capture_path / "retrieval-receipt.json",
        {
            "archive_sha256": "sha256:" + "8" * 64,
            "billing_action_required": "PROVIDER_TERMINATION_PENDING",
            "managed_capability_profile": remote._QWEN3_PROFILE_ID,
            "repository_commit": COMMIT,
            "schema_version": "inferdrome.qwen3-gpu-retrieval.v1",
            "semantic_verification": ("PENDING_UNTIL_PROVIDER_TERMINATION_CONFIRMED"),
            "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
            "ssh_host_identity_sha256": "sha256:" + "9" * 64,
            "verified_at": "2026-08-20T20:19:00Z",
        },
    )
    observed: dict[str, object] = {}

    def finalize(
        selected: Path,
        *,
        commit: str,
        evidence: remote._TerminationEvidence,
    ) -> Path:
        observed.update(
            {
                "capture_path": selected,
                "commit": commit,
                "cost_window": evidence.cost_window,
                "instance": evidence.instance,
                "receipt": evidence.receipt,
                "termination": evidence.termination,
                "trigger": evidence.trigger,
            }
        )
        return selected

    monkeypatch.setattr(remote, "_finalize_qwen3_capture_with_evidence", finalize)

    assert (
        remote._resume_qwen3_finalization(
            capture_path,
            commit=COMMIT,
            guard_state_directory=guard_root,
        )
        == capture_path.absolute()
    )
    assert observed == {
        "capture_path": capture_path.absolute(),
        "commit": COMMIT,
        "cost_window": cost_window.public_record(),
        "instance": instance.public_record(),
        "receipt": (guard_root / "termination-receipt.json").read_bytes(),
        "termination": termination.public_record(),
        "trigger": "cost-deadline",
    }


def test_remote_preflight_requires_build_tools_and_python_headers() -> None:
    script = remote._remote_preflight_script("/tmp/inferdrome-safe")

    assert "bash curl python3.12 nvidia-smi" in script
    assert "command -v git" not in script
    assert "command -v ninja" not in script
    assert "Python.h" in script
    assert "at least 40 GiB free" not in script
    assert "import ensurepip" in script
    assert "Python 3.12 development headers" in script


def test_a100_remote_preflight_requires_exact_40gb_pcie_name() -> None:
    script = remote._remote_preflight_script(
        "/tmp/inferdrome-safe",
        expected_gpu_model="NVIDIA A100-PCIE-40GB",
    )

    assert "NVIDIA A100-PCIE-40GB" in script
    assert "NVIDIA A100-SXM4-40GB" not in script
    assert "NVIDIA A100-SXM4-80GB" not in script
    assert "at least 40 GiB free" in script


def test_h100_remote_preflight_requires_exact_pcie_product_name() -> None:
    script = remote._remote_preflight_script(
        "/tmp/inferdrome-safe",
        expected_gpu_model="NVIDIA H100 PCIe",
    )

    assert "NVIDIA H100 PCIe" in script
    assert "NVIDIA H100 80GB HBM3" not in script
    assert "NVIDIA H100 NVL" not in script
    assert "at least 40 GiB free" in script


def test_lambda_guard_requires_actual_billing_start() -> None:
    args = SimpleNamespace(
        lambda_billing_started_at=None,
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        max_cost_usd=Decimal("2.58"),
    )

    with pytest.raises(remote.RemoteCaptureError, match="billing-started-at"):
        remote._lambda_guard_requested(args)


def test_lambda_guard_binds_explicit_instance_to_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def arm(reference: str, **kwargs: object) -> SimpleNamespace:
        observed["reference"] = reference
        observed["expected_endpoint"] = kwargs["expected_endpoint"]
        return SimpleNamespace(
            cost_window=SimpleNamespace(
                deadline=datetime(2026, 8, 18, 22, 0, tzinfo=UTC),
            ),
            state_directory=Path("/tmp/inferdrome-test-guard"),
        )

    monkeypatch.setattr(remote.lambda_gpu_guard, "arm_watchdog", arm)
    args = SimpleNamespace(
        destination="ubuntu@capture.example.test",
        lambda_billing_started_at=datetime(2026, 8, 18, 20, 0, tzinfo=UTC),
        lambda_guard_state_root="/tmp/inferdrome-test-guards",
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        max_cost_usd=Decimal("2.58"),
    )

    remote._arm_lambda_watchdog(args)

    assert observed == {
        "expected_endpoint": "capture.example.test",
        "reference": "b" * 32,
    }


def test_qwen3_guard_terminates_target_when_another_instance_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = "b" * 32
    other_id = "c" * 32
    target = SimpleNamespace(
        instance_id=target_id,
        instance_type_name="gpu_1x_a10",
        status="active",
    )
    other = SimpleNamespace(
        instance_id=other_id,
        instance_type_name="gpu_1x_a10",
        status="active",
    )
    handle = SimpleNamespace(
        client=SimpleNamespace(list_instances=lambda: (target, other)),
        cost_window=SimpleNamespace(deadline=datetime(2026, 8, 20, 20, 20, tzinfo=UTC)),
        instance=target,
        state_directory=Path("/tmp/inferdrome-test-guard"),
    )
    terminated: list[tuple[object, str]] = []
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "arm_watchdog",
        lambda *_args, **_kwargs: handle,
    )
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        lambda selected, *, trigger: (
            terminated.append((selected, trigger))
            or SimpleNamespace(final_status="terminated")
        ),
    )
    args = SimpleNamespace(
        destination="ubuntu@capture.example.test",
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_guard_state_root="/tmp/inferdrome-test-guards",
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id=target_id,
        lambda_instance_type_name="gpu_1x_a10",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("0.75"),
        qwen3_gpu_tier="a10-24gb-pcie",
    )

    with pytest.raises(remote.RemoteCaptureError, match="target terminated"):
        remote._arm_lambda_watchdog(args)

    assert terminated == [
        (handle, "campaign-single-instance-check-failed"),
    ]


def test_qwen3_guard_terminates_same_price_wrong_instance_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = SimpleNamespace(
        instance_id="b" * 32,
        instance_type_name="gpu_1x_a6000",
        status="active",
    )
    handle = SimpleNamespace(
        client=SimpleNamespace(list_instances=lambda: (target,)),
        cost_window=SimpleNamespace(deadline=datetime(2026, 8, 20, 20, 20, tzinfo=UTC)),
        instance=target,
        state_directory=Path("/tmp/inferdrome-test-guard"),
    )
    terminated: list[object] = []
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "arm_watchdog",
        lambda *_args, **_kwargs: handle,
    )
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        lambda selected, *, trigger: (
            terminated.append((selected, trigger))
            or SimpleNamespace(final_status="terminated")
        ),
    )
    args = SimpleNamespace(
        destination="ubuntu@capture.example.test",
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_guard_state_root="/tmp/inferdrome-test-guards",
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id=target.instance_id,
        lambda_instance_type_name="gpu_1x_a10",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("0.75"),
        qwen3_gpu_tier="a10-24gb-pcie",
    )

    with pytest.raises(remote.RemoteCaptureError, match="target terminated"):
        remote._arm_lambda_watchdog(args)

    assert terminated == [(handle, "campaign-single-instance-check-failed")]


def test_campaign_validation_failure_retains_unresolved_guard_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = SimpleNamespace(
        instance_id="b" * 32,
        instance_type_name="gpu_1x_a10",
        status="active",
    )
    other = SimpleNamespace(
        instance_id="c" * 32,
        instance_type_name="gpu_1x_a10",
        status="active",
    )

    class AliveWatchdog:
        def poll(self) -> None:
            return None

    state_directory = tmp_path / "guard"
    state_directory.mkdir()
    handle = SimpleNamespace(
        client=SimpleNamespace(list_instances=lambda: (target, other)),
        cost_window=SimpleNamespace(deadline=datetime(2026, 8, 20, 20, 20, tzinfo=UTC)),
        instance=target,
        process=AliveWatchdog(),
        state_directory=state_directory,
    )
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "arm_watchdog",
        lambda *_args, **_kwargs: handle,
    )
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            remote.lambda_gpu_guard.LambdaGuardError("synthetic provider failure")
        ),
    )
    args = SimpleNamespace(
        destination="ubuntu@capture.example.test",
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_guard_state_root=str(tmp_path),
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id=target.instance_id,
        lambda_instance_type_name="gpu_1x_a10",
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("0.75"),
        qwen3_gpu_tier="a10-24gb-pcie",
    )

    with pytest.raises(
        remote.RemoteCaptureError,
        match="unresolved state was retained",
    ):
        remote._arm_lambda_watchdog(args)

    marker = json.loads(
        (state_directory / "termination-unresolved.json").read_text(encoding="utf-8")
    )
    assert marker["status"] == "UNRESOLVED"
    assert marker["trigger"] == "campaign-single-instance-check-failed"
    assert marker["watchdog_armed"] is True


def test_qwen3_a100_guard_accepts_only_the_runtime_bound_instance_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = SimpleNamespace(
        instance_id="b" * 32,
        instance_type_name="gpu_1x_a100_api_runtime",
        status="active",
    )
    handle = SimpleNamespace(
        client=SimpleNamespace(list_instances=lambda: (target,)),
        cost_window=SimpleNamespace(deadline=datetime(2026, 8, 20, 20, 20, tzinfo=UTC)),
        instance=target,
        state_directory=Path("/tmp/inferdrome-test-guard"),
    )
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "arm_watchdog",
        lambda *_args, **_kwargs: handle,
    )
    args = SimpleNamespace(
        destination="ubuntu@capture.example.test",
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_guard_state_root="/tmp/inferdrome-test-guards",
        lambda_hourly_rate_usd=Decimal("1.99"),
        lambda_instance_id=target.instance_id,
        lambda_instance_type_name=target.instance_type_name,
        managed_capability_profile=remote._QWEN3_PROFILE_ID,
        max_cost_usd=Decimal("1.25"),
        qwen3_gpu_tier="a100-40gb-pcie",
    )

    assert remote._arm_lambda_watchdog(args) is handle


def test_capture_terminates_guarded_instance_even_after_capture_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchdog = SimpleNamespace(
        instance=SimpleNamespace(instance_id="b" * 32),
    )
    observed: list[object] = []
    monkeypatch.setattr(remote, "_arm_lambda_watchdog", lambda _args: watchdog)

    def fail_capture(*_args: object) -> Path:
        raise remote.RemoteCaptureError("proof failed")

    monkeypatch.setattr(remote, "_capture_over_ssh", fail_capture)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        lambda handle: (
            observed.append(handle) or SimpleNamespace(final_status="absent")
        ),
    )

    with pytest.raises(remote.RemoteCaptureError, match="proof failed"):
        remote._capture_with_source(
            SimpleNamespace(),
            COMMIT,
            None,
            Path("/unused/repo.tar"),
            SOURCE_ARCHIVE_SHA256,
        )

    assert observed == [watchdog]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX signals")
@pytest.mark.parametrize(
    "managed_profile",
    [None, remote._QWEN3_PROFILE_ID],
    ids=["retrospective-guarded", "qwen-guarded"],
)
def test_sigterm_enters_immediate_guard_cleanup_for_nonprospective_modes(
    monkeypatch: pytest.MonkeyPatch,
    managed_profile: str | None,
) -> None:
    ready_read, ready_write = os.pipe()
    cleanup_read, cleanup_write = os.pipe()
    watchdog = SimpleNamespace(
        cost_window=None,
        instance=SimpleNamespace(instance_id="b" * 32),
    )
    monkeypatch.setattr(remote, "_arm_lambda_watchdog", lambda _args: watchdog)

    def create_source(path: Path, _commit: str) -> tuple[str, int]:
        path.write_bytes(b"synthetic source")
        return SOURCE_ARCHIVE_SHA256, len(b"synthetic source")

    def block_capture(*_args: object, **_kwargs: object) -> Path:
        os.write(ready_write, b"r")
        while True:
            signal.pause()

    def terminate(_watchdog: object) -> SimpleNamespace:
        os.write(cleanup_write, b"c")
        return SimpleNamespace(final_status="absent")

    monkeypatch.setattr(remote, "_create_source_archive", create_source)
    monkeypatch.setattr(remote, "_capture_over_ssh", block_capture)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        terminate,
    )
    args = SimpleNamespace(
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name=(
            "gpu_1x_a10" if managed_profile is not None else None
        ),
        managed_capability_profile=managed_profile,
        max_cost_usd=Decimal("0.75"),
        prospective=False,
    )

    pid = os.fork()
    if pid == 0:
        os.close(ready_read)
        os.close(cleanup_read)
        try:
            remote._capture(args, COMMIT, None)
        except KeyboardInterrupt:
            os._exit(130)
        except BaseException:
            os._exit(2)
        os._exit(0)

    os.close(ready_write)
    os.close(cleanup_write)
    reaped = False
    try:
        ready, _, _ = select.select([ready_read], [], [], 3)
        assert ready and os.read(ready_read, 1) == b"r"
        os.kill(pid, signal.SIGTERM)
        status = _waitpid_bounded(pid)
        reaped = True
        cleanup, _, _ = select.select([cleanup_read], [], [], 1)
        assert cleanup and os.read(cleanup_read, 1) == b"c"
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 130
    finally:
        os.close(ready_read)
        os.close(cleanup_read)
        if not reaped:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX signals")
def test_sigterm_during_guard_arming_is_deferred_until_cleanup_owns_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_read, ready_write = os.pipe()
    cleanup_read, cleanup_write = os.pipe()
    watchdog = SimpleNamespace(instance=SimpleNamespace(instance_id="b" * 32))

    def arm(_args: object) -> SimpleNamespace:
        os.write(ready_write, b"r")
        time.sleep(0.15)
        return watchdog

    def terminate(_watchdog: object) -> SimpleNamespace:
        os.write(cleanup_write, b"c")
        return SimpleNamespace(final_status="absent")

    monkeypatch.setattr(remote, "_arm_lambda_watchdog", arm)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        terminate,
    )
    args = SimpleNamespace(
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name=None,
        managed_capability_profile=None,
        max_cost_usd=Decimal("0.75"),
        prospective=False,
    )

    pid = os.fork()
    if pid == 0:
        os.close(ready_read)
        os.close(cleanup_read)
        try:
            remote._capture(args, COMMIT, None)
        except KeyboardInterrupt:
            os._exit(130)
        except BaseException:
            os._exit(2)
        os._exit(0)

    os.close(ready_write)
    os.close(cleanup_write)
    reaped = False
    try:
        ready, _, _ = select.select([ready_read], [], [], 3)
        assert ready and os.read(ready_read, 1) == b"r"
        os.kill(pid, signal.SIGTERM)
        status = _waitpid_bounded(pid)
        reaped = True
        cleanup, _, _ = select.select([cleanup_read], [], [], 1)
        assert cleanup and os.read(cleanup_read, 1) == b"c"
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 130
    finally:
        os.close(ready_read)
        os.close(cleanup_read)
        if not reaped:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX signals")
def test_repeated_sigterm_cannot_interrupt_guard_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_read, ready_write = os.pipe()
    cleanup_read, cleanup_write = os.pipe()
    watchdog = SimpleNamespace(instance=SimpleNamespace(instance_id="b" * 32))
    monkeypatch.setattr(remote, "_arm_lambda_watchdog", lambda _args: watchdog)

    def create_source(path: Path, _commit: str) -> tuple[str, int]:
        path.write_bytes(b"synthetic source")
        return SOURCE_ARCHIVE_SHA256, len(b"synthetic source")

    def block_capture(*_args: object, **_kwargs: object) -> Path:
        os.write(ready_write, b"r")
        while True:
            signal.pause()

    def terminate(_watchdog: object) -> SimpleNamespace:
        os.write(cleanup_write, b"s")
        signal.pause()
        os.write(cleanup_write, b"d")
        return SimpleNamespace(final_status="absent")

    monkeypatch.setattr(remote, "_create_source_archive", create_source)
    monkeypatch.setattr(remote, "_capture_over_ssh", block_capture)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        terminate,
    )
    args = SimpleNamespace(
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name=None,
        managed_capability_profile=None,
        max_cost_usd=Decimal("0.75"),
        prospective=False,
    )

    pid = os.fork()
    if pid == 0:
        os.close(ready_read)
        os.close(cleanup_read)
        try:
            remote._capture(args, COMMIT, None)
        except KeyboardInterrupt:
            os._exit(130)
        except BaseException:
            os._exit(2)
        os._exit(0)

    os.close(ready_write)
    os.close(cleanup_write)
    reaped = False
    try:
        ready, _, _ = select.select([ready_read], [], [], 3)
        assert ready and os.read(ready_read, 1) == b"r"
        os.kill(pid, signal.SIGTERM)
        started, _, _ = select.select([cleanup_read], [], [], 2)
        assert started and os.read(cleanup_read, 1) == b"s"
        os.kill(pid, signal.SIGTERM)
        finished, _, _ = select.select([cleanup_read], [], [], 2)
        assert finished and os.read(cleanup_read, 1) == b"d"
        status = _waitpid_bounded(pid)
        reaped = True
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 130
    finally:
        os.close(ready_read)
        os.close(cleanup_read)
        if not reaped:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX signals")
def test_sigterm_at_cleanup_entry_cannot_bypass_guard_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_read, cleanup_write = os.pipe()
    watchdog = SimpleNamespace(
        cost_window=None,
        instance=SimpleNamespace(instance_id="b" * 32),
    )
    monkeypatch.setattr(remote, "_arm_lambda_watchdog", lambda _args: watchdog)

    def create_source(path: Path, _commit: str) -> tuple[str, int]:
        path.write_bytes(b"synthetic source")
        return SOURCE_ARCHIVE_SHA256, len(b"synthetic source")

    def terminate(_watchdog: object) -> SimpleNamespace:
        os.write(cleanup_write, b"c")
        return SimpleNamespace(final_status="absent")

    monkeypatch.setattr(remote, "_create_source_archive", create_source)
    monkeypatch.setattr(
        remote,
        "_capture_over_ssh",
        lambda *_args, **_kwargs: Path("/unused/capture"),
    )
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        terminate,
    )
    args = SimpleNamespace(
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name=None,
        managed_capability_profile=None,
        max_cost_usd=Decimal("0.75"),
        prospective=False,
    )
    source_lines = Path(remote.__file__).read_text(encoding="utf-8").splitlines()
    first_line = remote._capture_with_source.__code__.co_firstlineno
    cleanup_entry_line = next(
        line_number
        for line_number in range(first_line, len(source_lines) + 1)
        if "capture_failed = sys.exc_info()" in source_lines[line_number - 1]
    )

    pid = os.fork()
    if pid == 0:
        os.close(cleanup_read)

        def inject_at_cleanup_entry(
            frame: object,
            event: str,
            _argument: object,
        ) -> object:
            if (
                event == "line"
                and getattr(frame, "f_code", None)
                is remote._capture_with_source.__code__
                and getattr(frame, "f_lineno", None) == cleanup_entry_line
            ):
                sys.settrace(None)
                os.kill(os.getpid(), signal.SIGTERM)
            return inject_at_cleanup_entry

        sys.settrace(inject_at_cleanup_entry)
        try:
            remote._capture(args, COMMIT, None)
        except KeyboardInterrupt:
            os._exit(130)
        except BaseException:
            os._exit(2)
        os._exit(0)

    os.close(cleanup_write)
    reaped = False
    try:
        status = _waitpid_bounded(pid)
        reaped = True
        cleanup, _, _ = select.select([cleanup_read], [], [], 1)
        assert cleanup and os.read(cleanup_read, 1) == b"c"
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 130
    finally:
        os.close(cleanup_read)
        if not reaped:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX signals")
def test_repeated_sigterm_during_handler_restore_cannot_interrupt_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_read, cleanup_write = os.pipe()
    watchdog = SimpleNamespace(
        cost_window=None,
        instance=SimpleNamespace(instance_id="b" * 32),
    )
    monkeypatch.setattr(remote, "_arm_lambda_watchdog", lambda _args: watchdog)

    def create_source(path: Path, _commit: str) -> tuple[str, int]:
        path.write_bytes(b"synthetic source")
        return SOURCE_ARCHIVE_SHA256, len(b"synthetic source")

    def interrupt_capture(*_args: object, **_kwargs: object) -> Path:
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM did not interrupt capture")

    def terminate(_watchdog: object) -> SimpleNamespace:
        os.kill(os.getpid(), signal.SIGTERM)
        os.write(cleanup_write, b"c")
        return SimpleNamespace(final_status="absent")

    monkeypatch.setattr(remote, "_create_source_archive", create_source)
    monkeypatch.setattr(remote, "_capture_over_ssh", interrupt_capture)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        terminate,
    )
    args = SimpleNamespace(
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name=None,
        managed_capability_profile=None,
        max_cost_usd=Decimal("0.75"),
        prospective=False,
    )
    source_lines = Path(remote.__file__).read_text(encoding="utf-8").splitlines()
    guarded_code = remote._guarded_interruptible.__wrapped__.__code__
    restore_line = next(
        line_number
        for line_number in range(guarded_code.co_firstlineno, len(source_lines) + 1)
        if "signal.signal(selected_signal, handler)" in source_lines[line_number - 1]
    )

    pid = os.fork()
    if pid == 0:
        os.close(cleanup_read)

        def inject_during_restore(
            frame: object,
            event: str,
            _argument: object,
        ) -> object:
            if (
                event == "line"
                and getattr(frame, "f_code", None) is guarded_code
                and getattr(frame, "f_lineno", None) == restore_line
            ):
                sys.settrace(None)
                os.kill(os.getpid(), signal.SIGTERM)
            return inject_during_restore

        sys.settrace(inject_during_restore)
        try:
            remote._capture(args, COMMIT, None)
        except KeyboardInterrupt:
            os._exit(130)
        except BaseException:
            os._exit(2)
        os._exit(0)

    os.close(cleanup_write)
    reaped = False
    try:
        status = _waitpid_bounded(pid)
        reaped = True
        cleanup, _, _ = select.select([cleanup_read], [], [], 1)
        assert cleanup and os.read(cleanup_read, 1) == b"c"
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 130
    finally:
        os.close(cleanup_read)
        if not reaped:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX signals")
def test_sigterm_with_unconfirmed_termination_retains_unresolved_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_read, ready_write = os.pipe()
    state_directory = tmp_path / "guard"
    state_directory.mkdir()

    class AliveWatchdog:
        def poll(self) -> None:
            return None

    watchdog = SimpleNamespace(
        instance=SimpleNamespace(instance_id="b" * 32),
        process=AliveWatchdog(),
        state_directory=state_directory,
    )
    monkeypatch.setattr(remote, "_arm_lambda_watchdog", lambda _args: watchdog)

    def create_source(path: Path, _commit: str) -> tuple[str, int]:
        path.write_bytes(b"synthetic source")
        return SOURCE_ARCHIVE_SHA256, len(b"synthetic source")

    monkeypatch.setattr(remote, "_create_source_archive", create_source)

    def block_capture(*_args: object, **_kwargs: object) -> Path:
        os.write(ready_write, b"r")
        while True:
            signal.pause()

    monkeypatch.setattr(remote, "_capture_over_ssh", block_capture)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        lambda _watchdog: (_ for _ in ()).throw(
            remote.lambda_gpu_guard.LambdaGuardError("synthetic provider failure")
        ),
    )
    args = SimpleNamespace(
        lambda_billing_started_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
        lambda_hourly_rate_usd=Decimal("1.29"),
        lambda_instance_id="b" * 32,
        lambda_instance_type_name=None,
        managed_capability_profile=None,
        max_cost_usd=Decimal("0.75"),
        prospective=False,
    )

    pid = os.fork()
    if pid == 0:
        os.close(ready_read)
        try:
            remote._capture(args, COMMIT, None)
        except KeyboardInterrupt:
            os._exit(130)
        except BaseException:
            os._exit(2)
        os._exit(0)

    os.close(ready_write)
    reaped = False
    try:
        ready, _, _ = select.select([ready_read], [], [], 3)
        assert ready and os.read(ready_read, 1) == b"r"
        os.kill(pid, signal.SIGTERM)
        status = _waitpid_bounded(pid)
        reaped = True
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 130
    finally:
        os.close(ready_read)
        if not reaped:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)

    marker = json.loads(
        (state_directory / "termination-unresolved.json").read_text(encoding="utf-8")
    )
    assert marker["status"] == "UNRESOLVED"
    assert marker["watchdog_armed"] is True


def test_unconfirmed_termination_retains_explicit_armed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "guard"
    state_directory.mkdir()

    class AliveWatchdog:
        def poll(self) -> None:
            return None

    watchdog = SimpleNamespace(
        instance=SimpleNamespace(instance_id="b" * 32),
        process=AliveWatchdog(),
        state_directory=state_directory,
    )
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        lambda _watchdog: (_ for _ in ()).throw(
            remote.lambda_gpu_guard.LambdaGuardError("synthetic provider failure")
        ),
    )

    with pytest.raises(remote.RemoteCaptureError, match="was not confirmed"):
        remote._terminate_guarded_capture(
            watchdog,
            capture_failed=False,
            capture_label="capture",
        )

    marker = json.loads(
        (state_directory / "termination-unresolved.json").read_text(encoding="utf-8")
    )
    assert marker["status"] == "UNRESOLVED"
    assert marker["watchdog_armed"] is True
    assert marker["instance_id"] == "b" * 32


def test_unresolved_state_publication_failure_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchdog = SimpleNamespace(instance=SimpleNamespace(instance_id="b" * 32))
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        lambda _watchdog: (_ for _ in ()).throw(
            remote.lambda_gpu_guard.LambdaGuardError("synthetic provider failure")
        ),
    )
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "retain_unresolved_termination",
        lambda _watchdog: (_ for _ in ()).throw(
            remote.lambda_gpu_guard.LambdaGuardError("synthetic state failure")
        ),
    )

    with pytest.raises(
        remote.RemoteCaptureError,
        match="explicit unresolved state could not be retained",
    ):
        remote._terminate_guarded_capture(
            watchdog,
            capture_failed=True,
            capture_label="capture",
        )


def test_guard_failure_does_not_mask_the_capture_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "guard"
    state_directory.mkdir()

    class AliveWatchdog:
        def poll(self) -> None:
            return None

    watchdog = SimpleNamespace(
        instance=SimpleNamespace(instance_id="b" * 32),
        process=AliveWatchdog(),
        state_directory=state_directory,
    )
    monkeypatch.setattr(remote, "_arm_lambda_watchdog", lambda _args: watchdog)

    def fail_capture(*_args: object) -> Path:
        raise remote.RemoteCaptureError("original proof failure")

    def fail_termination(_handle: object) -> None:
        raise remote.lambda_gpu_guard.LambdaGuardError("provider unavailable")

    monkeypatch.setattr(remote, "_capture_over_ssh", fail_capture)
    monkeypatch.setattr(
        remote.lambda_gpu_guard,
        "terminate_guarded_instance",
        fail_termination,
    )

    with pytest.raises(remote.RemoteCaptureError, match="original proof failure"):
        remote._capture_with_source(
            SimpleNamespace(),
            COMMIT,
            None,
            Path("/unused/repo.tar"),
            SOURCE_ARCHIVE_SHA256,
        )
    assert (state_directory / "termination-unresolved.json").is_file()


def test_completed_capture_manifest_anchors_receipts_and_support(
    tmp_path: Path,
) -> None:
    root = tmp_path / "capture"
    root.mkdir()
    host_digest = _capture_tree(root)

    manifest_path = capture.write_capture_manifest(root, COMMIT)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["repository_commit"] == COMMIT
    assert manifest["support"]["host_preparation"]["sha256"] == host_digest
    assert manifest["single"]["receipt_path"].endswith("demo-receipt.json")
    assert manifest["comparison"]["receipt_path"].endswith(
        "comparison-demo-receipt.json"
    )
    assert stat.S_IMODE(manifest_path.stat().st_mode) & 0o222 == 0


def test_capture_manifest_rejects_receipt_from_another_commit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "capture"
    root.mkdir()
    _capture_tree(root, receipt_commit="b" * 40)

    with pytest.raises(capture.CaptureError, match="capture commit"):
        capture.write_capture_manifest(root, COMMIT)


def test_manifest_verification_rechecks_support_and_dispatches_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    root.mkdir()
    _capture_tree(root)
    capture.write_capture_manifest(root, COMMIT)
    calls: list[str] = []

    def single(*_args: object, **_kwargs: object) -> dict[str, str]:
        calls.append("single")
        return {"bundle_digest": f"sha256:{'c' * 64}", "run_id": f"run-{'c' * 32}"}

    def comparison(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("comparison")
        return {"status": "COMPARABLE", "run_ids": []}

    monkeypatch.setattr(capture, "_verify_single_receipt", single)
    monkeypatch.setattr(capture, "_verify_comparison_receipt", comparison)

    result = capture.verify_capture(root, expected_repository_commit=COMMIT)

    assert result["valid"] is True
    assert result["repository_commit"] == COMMIT
    assert calls == ["single", "comparison"]

    with pytest.raises(capture.CaptureError, match="not the expected commit"):
        capture.verify_capture(root, expected_repository_commit="d" * 40)


def test_failure_receipt_is_explicitly_not_evidence(tmp_path: Path) -> None:
    root = tmp_path / "capture"

    path = capture.write_failure_receipt(
        root,
        COMMIT,
        failed_step="comparison-proof",
        exit_code=143,
    )
    value = json.loads(path.read_text(encoding="utf-8"))

    assert value["proof_status"] == "INCOMPLETE_NOT_EVIDENCE"
    assert value["process_exit_code"] == 143
    assert stat.S_IMODE(path.stat().st_mode) & 0o222 == 0

    verified = capture.verify_failure_capture(
        root,
        expected_repository_commit=COMMIT,
    )
    assert verified == value


def test_failure_receipt_verification_rejects_commit_drift(tmp_path: Path) -> None:
    root = tmp_path / "capture"
    capture.write_failure_receipt(
        root,
        COMMIT,
        failed_step="single-proof",
        exit_code=1,
    )

    with pytest.raises(capture.CaptureError, match="not the expected commit"):
        capture.verify_failure_capture(
            root,
            expected_repository_commit="b" * 40,
        )


def _archive_with_member(path: Path, member: tarfile.TarInfo, content: bytes) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        root = tarfile.TarInfo("capture")
        root.type = tarfile.DIRTYPE
        root.mode = 0o700
        archive.addfile(root)
        if member.isfile():
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        else:
            archive.addfile(member)


def test_capture_archive_extracts_only_bounded_regular_tree(tmp_path: Path) -> None:
    archive = tmp_path / "capture.tar.gz"
    member = tarfile.TarInfo("capture/capture-failure.json")
    member.mode = 0o444
    _archive_with_member(archive, member, b"{}\n")

    extracted = capture.extract_capture_archive(archive, tmp_path / "retrieved")

    assert (extracted / "capture-failure.json").read_bytes() == b"{}\n"
    assert stat.S_IMODE(extracted.stat().st_mode) == 0o700
    assert stat.S_IMODE((extracted / "capture-failure.json").stat().st_mode) == 0o444


def test_capture_archive_preserves_sealed_bundle_modes(tmp_path: Path) -> None:
    archive = tmp_path / "capture.tar.gz"
    with tarfile.open(archive, mode="w:gz") as retained:
        for name, mode in (
            ("capture", 0o700),
            ("capture/single", 0o700),
            ("capture/single/example", 0o700),
            ("capture/single/example/runs", 0o700),
            ("capture/single/example/runs/run-" + "a" * 32, 0o700),
            (
                "capture/single/example/runs/run-" + "a" * 32 + "/bundle",
                0o500,
            ),
        ):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = mode
            retained.addfile(member)
        descriptor = tarfile.TarInfo(
            "capture/single/example/runs/run-" + "a" * 32 + "/bundle/bundle.json"
        )
        descriptor.mode = 0o400
        descriptor.size = 3
        retained.addfile(descriptor, io.BytesIO(b"{}\n"))

    extracted = capture.extract_capture_archive(archive, tmp_path / "retrieved")
    bundle = extracted / "single" / "example" / "runs" / ("run-" + "a" * 32) / "bundle"

    assert stat.S_IMODE(bundle.stat().st_mode) == 0o500
    assert stat.S_IMODE((bundle / "bundle.json").stat().st_mode) == 0o400


def test_capture_archive_verification_uses_isolated_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "capture.tar.gz"
    member = tarfile.TarInfo("capture/capture-manifest.json")
    member.mode = 0o444
    _archive_with_member(archive, member, b"{}\n")
    expected_archive_sha256 = capture.archive_sha256(archive)
    observed: dict[str, Path] = {}

    def verify(
        root: Path,
        *,
        expected_repository_commit: str | None = None,
    ) -> dict[str, object]:
        observed["root"] = root
        assert expected_repository_commit == COMMIT
        assert root.parent.name.startswith("inferdrome-capture-verification-")
        return {"valid": True}

    monkeypatch.setattr(capture, "verify_capture", verify)

    result = capture.verify_capture_archive(
        archive,
        expected_archive_sha256=expected_archive_sha256,
        expected_repository_commit=COMMIT,
    )

    assert result == {
        "archive_sha256": expected_archive_sha256,
        "capture_manifest_sha256": (
            "sha256:ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"
        ),
        "verification": {"valid": True},
    }
    assert not observed["root"].exists()


def test_capture_archive_verification_rejects_digest_mismatch(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "capture.tar.gz"
    member = tarfile.TarInfo("capture/capture-manifest.json")
    member.mode = 0o444
    _archive_with_member(archive, member, b"{}\n")

    with pytest.raises(capture.CaptureError, match="SHA-256 verification"):
        capture.verify_capture_archive(
            archive,
            expected_archive_sha256=f"sha256:{'0' * 64}",
            expected_repository_commit=COMMIT,
        )


@pytest.mark.parametrize(
    "member_names",
    (
        ("capture/README", "capture/readme"),
        ("capture/e\u0301", "capture/é"),
    ),
)
def test_capture_archive_rejects_case_or_unicode_colliding_members(
    tmp_path: Path,
    member_names: tuple[str, str],
) -> None:
    archive = tmp_path / "colliding-members.tar.gz"
    with tarfile.open(archive, mode="w:gz") as retained:
        root = tarfile.TarInfo("capture")
        root.type = tarfile.DIRTYPE
        root.mode = 0o700
        retained.addfile(root)
        for name in member_names:
            member = tarfile.TarInfo(name)
            member.mode = 0o400
            member.size = 1
            retained.addfile(member, io.BytesIO(b"x"))

    with pytest.raises(capture.CaptureError, match="unsafe member"):
        capture.extract_capture_archive(archive, tmp_path / "retrieved")


def test_capture_archive_digest_and_extraction_share_the_open_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "capture.tar.gz"
    member = tarfile.TarInfo("capture/verified")
    member.mode = 0o400
    _archive_with_member(archive, member, b"good")
    expected_archive_sha256 = capture.archive_sha256(archive)
    replacement = tmp_path / "replacement.tar.gz"
    replacement_member = tarfile.TarInfo("capture/replaced")
    replacement_member.mode = 0o400
    _archive_with_member(replacement, replacement_member, b"bad")

    original_snapshot = capture._snapshot_archive

    def snapshot_then_replace(
        stream: BinaryIO,
        metadata: os.stat_result,
    ) -> tuple[BinaryIO, str]:
        snapshot, digest = original_snapshot(stream, metadata)
        replacement.replace(archive)
        return snapshot, digest

    monkeypatch.setattr(capture, "_snapshot_archive", snapshot_then_replace)

    extracted = capture.extract_capture_archive(
        archive,
        tmp_path / "retrieved",
        expected_archive_sha256=expected_archive_sha256,
    )

    assert (extracted / "verified").read_bytes() == b"good"
    assert not (extracted / "replaced").exists()


def test_capture_archive_snapshot_survives_same_inode_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "capture.tar.gz"
    member = tarfile.TarInfo("capture/verified")
    member.mode = 0o400
    _archive_with_member(archive, member, b"good")
    expected_archive_sha256 = capture.archive_sha256(archive)

    original_snapshot = capture._snapshot_archive

    def snapshot_then_mutate(
        stream: BinaryIO,
        metadata: os.stat_result,
    ) -> tuple[BinaryIO, str]:
        snapshot, digest = original_snapshot(stream, metadata)
        with archive.open("r+b") as mutable:
            mutable.seek(0)
            mutable.write(b"same inode mutation")
        return snapshot, digest

    monkeypatch.setattr(capture, "_snapshot_archive", snapshot_then_mutate)

    extracted = capture.extract_capture_archive(
        archive,
        tmp_path / "retrieved",
        expected_archive_sha256=expected_archive_sha256,
    )

    assert (extracted / "verified").read_bytes() == b"good"


def test_capture_archive_rejects_too_many_members_before_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "directory-tarbomb.tar.gz"
    with tarfile.open(archive, mode="w:gz") as retained:
        for name in ("capture", "capture/one", "capture/two"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o700
            retained.addfile(member)
    monkeypatch.setattr(capture, "_MAX_CAPTURE_MEMBERS", 2)
    destination = tmp_path / "retrieved"

    with pytest.raises(capture.CaptureError, match="safety limits"):
        capture.extract_capture_archive(archive, destination)

    assert not (destination / "capture").exists()


def test_capture_archive_bounds_implicit_directories_before_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "implicit-directory-tarbomb.tar.gz"
    member = tarfile.TarInfo("capture/one/two/value.json")
    member.mode = 0o400
    _archive_with_member(archive, member, b"{}\n")
    monkeypatch.setattr(capture, "_MAX_CAPTURE_DIRECTORIES", 2)
    destination = tmp_path / "retrieved"

    with pytest.raises(capture.CaptureError, match="safety limits"):
        capture.extract_capture_archive(archive, destination)

    assert not (destination / "capture").exists()


def test_capture_tree_rejects_too_many_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "capture"
    (root / "one" / "two").mkdir(parents=True)
    monkeypatch.setattr(capture, "_MAX_CAPTURE_DIRECTORIES", 2)

    with pytest.raises(capture.CaptureError, match="safety limits"):
        capture._validate_capture_tree(root)


@pytest.mark.parametrize("kind", ["traversal", "symlink"])
def test_capture_archive_rejects_unsafe_members(tmp_path: Path, kind: str) -> None:
    archive = tmp_path / f"{kind}.tar.gz"
    if kind == "traversal":
        member = tarfile.TarInfo("capture/../outside")
    else:
        member = tarfile.TarInfo("capture/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/tmp/outside"
    _archive_with_member(archive, member, b"unsafe")

    with pytest.raises(capture.CaptureError, match="unsafe member"):
        capture.extract_capture_archive(archive, tmp_path / f"extract-{kind}")
