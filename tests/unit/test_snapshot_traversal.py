"""SYNTHETIC_ONLY snapshot traversal failures cannot produce partial identities."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Literal

import pytest

from inferdrome.errors import AdapterError
from inferdrome.execution import managed_vllm

_EXISTING_DIGEST = (
    "sha256:b2ec63bc388bd22de8153ec68f2d6f0a2cd91e1cbe268fd63aa90887dbad3998"
)


def snapshot(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    (root / "config.json").write_bytes(b'{"SYNTHETIC_ONLY":true}\n')
    (root / "nested").mkdir()
    (root / "nested" / "weights.safetensors").write_bytes(b"SYNTHETIC_ONLY weights\n")
    return root


@pytest.mark.parametrize("kind", ["model", "tokenizer"])
@pytest.mark.parametrize("scan", [1, 2])
def test_scan_failure_before_or_after_hash_cannot_return_partial_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["model", "tokenizer"],
    scan: int,
) -> None:
    root = snapshot(tmp_path)
    original = os.scandir
    count = 0

    def blocked(path):
        nonlocal count
        if Path(path) == root / "nested":
            count += 1
            if count == scan:
                raise PermissionError(
                    errno.EACCES, "private traversal sentinel", str(path)
                )
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(managed_vllm.os, "scandir", blocked)
        with pytest.raises(
            AdapterError, match="snapshot tree cannot be inspected safely"
        ) as error:
            managed_vllm.snapshot_directory_identity(root, kind=kind, revision="a" * 40)
    assert count == scan
    assert "sentinel" not in str(error.value) and str(root) not in str(error.value)


def test_readable_tree_hash_bytes_and_dot_cache_exclusion_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = snapshot(tmp_path)
    original = os.scandir
    (root / ".cache").mkdir()
    (root / ".cache" / "excluded").write_bytes(b"cache is outside the hash domain")

    def excluded(path):
        if Path(path) == root / ".cache":
            raise AssertionError("excluded cache must not be traversed")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(managed_vllm.os, "scandir", excluded)
        identity = managed_vllm.snapshot_directory_identity(
            root, kind="model", revision="a" * 40
        )
    # Captured from the unchanged helper before adding the fail-closed onerror.
    assert identity.sha256 == _EXISTING_DIGEST
    assert identity.file_count == 2
    assert identity.hash_policy == "regular-files-excluding-dot-cache-v1"


def test_unreadable_directory_rejects_snapshot_identity_on_permission_enforcing_hosts(
    tmp_path: Path,
) -> None:
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("requires a non-root host that enforces directory read permissions")
    root = snapshot(tmp_path)
    blocked = root / "nested"
    original_mode = blocked.stat().st_mode & 0o777
    blocked.chmod(0)
    try:
        try:
            with os.scandir(blocked) as entries:
                list(entries)
        except PermissionError:
            pass
        else:
            pytest.skip("host permits scanning mode-000 directories")
        with pytest.raises(
            AdapterError, match="snapshot tree cannot be inspected safely"
        ):
            managed_vllm.snapshot_directory_identity(
                root, kind="model", revision="a" * 40
            )
    finally:
        blocked.chmod(original_mode)
