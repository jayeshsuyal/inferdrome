"""Tiny synthetic model inventory and fake downloader IO; no network/model run."""

from __future__ import annotations

import ast
import hashlib
import os
import shutil
import signal
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from inferdrome.deployment import vast_model_stage as stage
from inferdrome.deployment.vast_transfer import TransferCommand
from inferdrome.qwen3_campaign import QWEN3_8B_REVISION

# Reusable by the root's fake bootstrap journey. These bytes are deliberately
# not a usable model; only filenames resemble the production frozen inventory.
TINY_FILES = {
    ".gitattributes": b"synthetic attributes\n",
    "config.json": b'{"synthetic_config":true}',
    "tokenizer.json": b'{"synthetic_tokenizer":true}',
    "model-00001-of-00005.safetensors": b"synthetic weights, never loaded",
    "model.safetensors.index.json": b'{"synthetic_index":true}',
}


def tiny_model_manifest() -> dict[str, Any]:
    return {
        "files": [
            {
                "path": name,
                "size_bytes": len(content),
                "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
            for name, content in TINY_FILES.items()
        ]
    }


@pytest.fixture
def destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "model"
    target.mkdir(mode=0o700)
    monkeypatch.setattr(stage, "qwen3_model_manifest", tiny_model_manifest)
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=10 * stage.HEADROOM_BYTES),
    )
    return target


class FakeDownloader:
    def __init__(
        self,
        change: Callable[[TransferCommand], None] | None = None,
    ) -> None:
        self.calls: list[TransferCommand] = []
        self.change = change

    def __call__(self, command: TransferCommand, seconds: float) -> None:
        assert 0 < seconds <= 30
        assert all(not old.directory.exists() for old in self.calls)
        self.calls.append(command)
        name = command.data_path.name
        command.data_path.write_bytes(TINY_FILES[name])
        # A real local_dir download also leaves private metadata. It must stay
        # in the per-file scratch, never be adopted into the model inventory.
        metadata = command.directory / ".cache"
        metadata.mkdir()
        (metadata / "synthetic-metadata").write_bytes(b"discard me")
        if self.change is not None:
            self.change(command)


def test_success_downloads_each_exact_file_sequentially_and_discards_scratch(
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "HTTPS_PROXY",
        "SSH_AUTH_SOCK",
    ):
        monkeypatch.setenv(variable, "ambient credential must not propagate")
    downloader = FakeDownloader()
    stage.stage_pinned_model(destination, seconds=30, runner=downloader)
    assert [call.data_path.name for call in downloader.calls] == list(TINY_FILES)
    assert {path.name for path in destination.iterdir()} == set(TINY_FILES)
    for name, content in TINY_FILES.items():
        path = destination / name
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_nlink == 1
    for call in downloader.calls:
        assert call.argv[:3] == (stage.DOWNLOADER, "-I", "-c")
        assert call.argv[4:] == (
            call.data_path.name,
            "b968826d9c46dd6066d109eabc6255188de91218",
            str(call.directory),
        )
        assert call.argv[5] == QWEN3_8B_REVISION
        assert call.maximum_bytes == max(len(TINY_FILES[call.data_path.name]), 131_072)
        assert call.direction == "send"
        assert call.environment == {
            "PATH": "/usr/bin:/bin",
            "HOME": str(call.directory),
            "TMPDIR": str(call.directory),
            "LANG": "C.UTF-8",
            "HF_HOME": str(call.directory / ".hf"),
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_DISABLE_PROGRESS_BARS": "1",
            "HF_HUB_DISABLE_XET": "1",
            "HF_HUB_ETAG_TIMEOUT": "20",
            "HF_HUB_DOWNLOAD_TIMEOUT": "20",
        }
        assert "ambient credential" not in repr(call)
        assert not call.directory.exists()
    assert not list(destination.parent.glob("download-*"))


def test_worker_has_exact_public_pinned_download_and_no_ambient_http_client(
    destination: Path,
) -> None:
    downloader = FakeDownloader()
    stage.stage_pinned_model(destination, seconds=30, runner=downloader)
    # Parse the actual submitted program without importing or invoking the real
    # downloader, HTTP client, model hub, registry or an SSH process.
    tree = ast.parse(downloader.calls[0].argv[3])
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    downloads = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "hf_hub_download"
    ]
    assert len(downloads) == 1
    options = {keyword.arg: keyword.value for keyword in downloads[0].keywords}
    for name, expected in {
        "repo_id": "Qwen/Qwen3-8B",
        "endpoint": "https://huggingface.co",
        "token": False,
        "force_download": True,
        "etag_timeout": 20,
    }.items():
        assert ast.literal_eval(options[name]) == expected
    for name, variable in {
        "filename": "name",
        "revision": "revision",
        "local_dir": "directory",
    }.items():
        value = options[name]
        assert isinstance(value, ast.Name)
        assert value.id == variable
    clients = [
        node
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
        and node.func.attr == "Client"
    ]
    assert len(clients) == 1
    client_options = {
        keyword.arg: ast.literal_eval(keyword.value) for keyword in clients[0].keywords
    }
    assert client_options == {
        "trust_env": False,
        "follow_redirects": True,
        "timeout": 20,
    }
    constants = {
        node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
    }
    assert "huggingface-hub" in constants and "1.3.2" in constants


def test_disk_budget_covers_two_snapshots_largest_scratch_and_headroom(
    destination: Path,
) -> None:
    sizes = [len(content) for content in TINY_FILES.values()]
    assert stage.required_free_bytes() == (
        2 * sum(sizes) + max(sizes) + stage.HEADROOM_BYTES
    )


def test_insufficient_disk_rejects_before_downloader(
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=stage.required_free_bytes() - 1),
    )
    downloader = FakeDownloader()
    with pytest.raises(stage.ModelStageFailure):
        stage.stage_pinned_model(destination, seconds=30, runner=downloader)
    assert not downloader.calls and not list(destination.iterdir())


@pytest.mark.parametrize("name", ["old", ".partial", ".gitattributes"])
def test_existing_file_refuses_adoption_and_preserves_original_bytes(
    destination: Path,
    name: str,
) -> None:
    (destination / name).write_bytes(b"prior bytes")
    downloader = FakeDownloader()
    with pytest.raises(stage.ModelStageFailure):
        stage.stage_pinned_model(destination, seconds=30, runner=downloader)
    assert not downloader.calls
    assert (destination / name).read_bytes() == b"prior bytes"


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "oversize",
        "short",
        "hash",
        "symlink",
        "hardlink",
        "directory",
        "runner",
    ],
)
def test_bad_download_is_never_adopted(
    destination: Path,
    failure: str,
) -> None:
    def change(command: TransferCommand) -> None:
        path = command.data_path
        if failure == "missing":
            path.unlink()
        elif failure == "oversize":
            path.write_bytes(TINY_FILES[path.name] + b"x")
        elif failure == "short":
            path.write_bytes(TINY_FILES[path.name][:-1])
        elif failure == "hash":
            path.write_bytes(b"x" * len(TINY_FILES[path.name]))
        elif failure == "symlink":
            path.unlink()
            target = command.directory / "other"
            target.write_bytes(TINY_FILES[path.name])
            path.symlink_to(target)
        elif failure == "hardlink":
            os.link(path, command.directory / "other")
        elif failure == "directory":
            path.unlink()
            path.mkdir()
        else:
            raise RuntimeError("a secret token and a sensitive download URL")

    downloader = FakeDownloader(change)
    with pytest.raises(stage.ModelStageFailure) as caught:
        stage.stage_pinned_model(destination, seconds=30, runner=downloader)
    assert "secret" not in str(caught.value) and "URL" not in str(caught.value)
    assert len(downloader.calls) == 1
    assert not list(destination.iterdir())
    assert not downloader.calls[0].directory.exists()


def test_failure_after_verified_file_cannot_be_adopted_or_retried_as_full_snapshot(
    destination: Path,
) -> None:
    failed_name = list(TINY_FILES)[1]

    def fail_second(command: TransferCommand) -> None:
        if command.data_path.name == failed_name:
            raise RuntimeError("synthetic failure")

    downloader = FakeDownloader(fail_second)
    with pytest.raises(stage.ModelStageFailure):
        stage.stage_pinned_model(destination, seconds=30, runner=downloader)
    assert len(downloader.calls) == 2
    assert {path.name for path in destination.iterdir()} == {next(iter(TINY_FILES))}
    assert not (destination / failed_name).exists()
    retry = FakeDownloader()
    with pytest.raises(stage.ModelStageFailure):
        stage.stage_pinned_model(destination, seconds=30, runner=retry)
    assert not retry.calls
    assert not list(destination.parent.glob("download-*"))


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="POSIX timer required")
def test_fake_stalled_downloader_is_interrupted_by_whole_call_timer(
    destination: Path,
) -> None:
    calls: list[TransferCommand] = []

    def stalled(command: TransferCommand, seconds: float) -> None:
        assert 0 < seconds <= 0.03
        calls.append(command)
        time.sleep(2)
        raise AssertionError("hard timer failed to interrupt the fake downloader")

    started = time.monotonic()
    with pytest.raises(stage.ModelStageFailure):
        stage.stage_pinned_model(destination, seconds=0.03, runner=stalled)
    assert time.monotonic() - started < 0.75
    assert len(calls) == 1 and not calls[0].directory.exists()
    assert not list(destination.iterdir())


def test_production_path_requires_guest_identity_before_any_runner(
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(os, "getgid", lambda: 0)
    downloader = FakeDownloader()
    monkeypatch.setattr(stage, "_run_bounded", downloader)
    with pytest.raises(stage.ModelStageFailure, match="GUEST_IDENTITY_REQUIRED"):
        stage.stage_pinned_model(destination, seconds=30)
    assert not downloader.calls
