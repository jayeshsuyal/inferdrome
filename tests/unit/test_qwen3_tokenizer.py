"""Adversarial checks for the pinned Qwen3 tokenizer identity boundary."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from inferdrome.errors import AdapterError
from inferdrome.qwen3_tokenizer import (
    QWEN3_TOKENIZER_CONFIG_SHA256,
    QWEN3_TOKENIZER_JSON_SHA256,
    QWEN3_TOKENIZERS_VERSION,
    _read_pinned_regular_file,
    expected_qwen3_tokenizer_file_verification,
    verify_qwen3_tokenizer_files,
)
from scripts.verify_qwen3_workload_tokenization import (
    _require_tokenizers_version,
)


def test_expected_tokenizer_verification_is_exact() -> None:
    proof = expected_qwen3_tokenizer_file_verification()

    assert proof.policy == "bounded-regular-files-no-follow-sha256-v1"
    assert proof.tokenizer_json_sha256 == QWEN3_TOKENIZER_JSON_SHA256
    assert proof.tokenizer_config_sha256 == QWEN3_TOKENIZER_CONFIG_SHA256


def test_tokenizer_verification_rejects_relative_and_wrong_files(
    tmp_path: Path,
) -> None:
    with pytest.raises(AdapterError, match="root must be absolute"):
        verify_qwen3_tokenizer_files(Path("relative-snapshot"))

    (tmp_path / "tokenizer.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "tokenizer_config.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(AdapterError, match="digest differs"):
        verify_qwen3_tokenizer_files(tmp_path)


def test_tokenizer_verification_rejects_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    (tmp_path / "tokenizer.json").symlink_to(target)
    (tmp_path / "tokenizer_config.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(AdapterError, match="file is unsafe"):
        verify_qwen3_tokenizer_files(tmp_path)


def test_tokenizer_reader_detects_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "tokenizer.json"
    content = b"a" * 70_000
    path.write_bytes(content)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b"b" * len(content))
    expected = "sha256:" + hashlib.sha256(content).hexdigest()
    original_read = os.read
    replaced = False

    def replacing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        result = original_read(descriptor, size)
        if not replaced:
            os.replace(replacement, path)
            replaced = True
        return result

    monkeypatch.setattr(os, "read", replacing_read)
    with pytest.raises(AdapterError, match="changed during reading"):
        _read_pinned_regular_file(
            path,
            expected_digest=expected,
            limit=100_000,
        )


def test_tokenizer_toolchain_version_is_frozen() -> None:
    _require_tokenizers_version(QWEN3_TOKENIZERS_VERSION)
    with pytest.raises(SystemExit, match="version differs"):
        _require_tokenizers_version("0.22.0")
