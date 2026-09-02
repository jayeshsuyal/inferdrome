"""Adversarial tests for the descriptor-anchored v2 journal primitives."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from inferdrome.deployment import gcp_securefs


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "journal"
    root.mkdir()
    return root


def test_safe_dir_fd_fails_closed_without_no_follow_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gcp_securefs, "_NOFOLLOW", None)

    with pytest.raises(
        gcp_securefs.SafeDirFSError,
        match="secure no-follow directory primitives unavailable",
    ):
        gcp_securefs.SafeDirFD.open(_root(tmp_path))


def test_safe_dir_fd_fails_closed_without_directory_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gcp_securefs, "_DIRECTORY", None)

    with pytest.raises(
        gcp_securefs.SafeDirFSError,
        match="secure no-follow directory primitives unavailable",
    ):
        gcp_securefs.SafeDirFD.open(_root(tmp_path))


def test_safe_dir_fd_fails_closed_without_dir_fd_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gcp_securefs.os, "supports_dir_fd", frozenset())

    with pytest.raises(
        gcp_securefs.SafeDirFSError,
        match="secure dir_fd primitives unavailable",
    ):
        gcp_securefs.SafeDirFD.open(_root(tmp_path))


def test_safe_dir_fd_accepts_only_fixed_system_temp_aliases(tmp_path: Path) -> None:
    """macOS `/var`/`/tmp` aliases do not weaken caller-link rejection."""

    for alias, target in gcp_securefs._TRUSTED_SYSTEM_DIRECTORY_ALIASES.items():
        try:
            relative = tmp_path.relative_to(target)
        except ValueError:
            continue
        aliased_root = Path(alias, relative)
        directory = gcp_securefs.SafeDirFD.open(aliased_root)
        try:
            assert directory.path == aliased_root
        finally:
            directory.close()
        break
    else:
        pytest.skip("temporary path is not below a macOS system alias target")


def test_validated_regular_child_rejects_hard_link_and_replacement(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    event = root / "event.jsonl"
    event.write_text("first\n", encoding="utf-8")
    alias = root / "event-alias.jsonl"
    os.link(event, alias)
    directory = gcp_securefs.SafeDirFD.open(root)
    descriptor: int | None = None
    try:
        with pytest.raises(gcp_securefs.SafeDirFSError, match="unsafe journal child"):
            directory.validated_regular_child(alias.name)

        alias.unlink()
        descriptor = directory.open_child(event.name, os.O_RDONLY)
        replacement = root / "replacement.jsonl"
        replacement.write_text("replacement\n", encoding="utf-8")
        os.replace(replacement, event)

        with pytest.raises(gcp_securefs.SafeDirFSError, match="journal child changed"):
            directory.validated_regular_child(event.name, descriptor=descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        directory.close()


def test_open_child_rejects_a_symlink_before_use(tmp_path: Path) -> None:
    root = _root(tmp_path)
    target = tmp_path / "outside"
    target.write_text("not a journal", encoding="utf-8")
    (root / "event.jsonl").symlink_to(target)
    directory = gcp_securefs.SafeDirFD.open(root)
    try:
        with pytest.raises(OSError):
            directory.open_child("event.jsonl", os.O_RDONLY)
    finally:
        directory.close()


def test_safe_dir_fd_rejects_a_symlinked_intermediate_root_component(
    tmp_path: Path,
) -> None:
    target_parent = tmp_path / "target-parent"
    target_root = target_parent / "journal"
    target_root.mkdir(parents=True)
    path_parent = tmp_path / "path-parent"
    path_parent.mkdir()
    (path_parent / "alias").symlink_to(target_parent, target_is_directory=True)

    with pytest.raises(OSError):
        gcp_securefs.SafeDirFD.open(path_parent / "alias" / "journal")
