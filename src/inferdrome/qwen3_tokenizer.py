"""Fail-closed identity checks for the pinned Qwen3-8B tokenizer files."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from inferdrome.domain.base import FrozenModel
from inferdrome.errors import AdapterError

QWEN3_TOKENIZERS_VERSION: Final = "0.22.1"
QWEN3_TOKENIZER_JSON_SHA256: Final = (
    "sha256:aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"
)
QWEN3_TOKENIZER_CONFIG_SHA256: Final = (
    "sha256:d5d09f07b48c3086c508b30d1c9114bd1189145b74e982a265350c923acd8101"
)

_TOKENIZER_JSON_LIMIT = 67_108_864
_TOKENIZER_CONFIG_LIMIT = 1_048_576


class Qwen3TokenizerFileVerification(FrozenModel):
    policy: Literal["bounded-regular-files-no-follow-sha256-v1"]
    tokenizer_config_sha256: Literal[
        "sha256:d5d09f07b48c3086c508b30d1c9114bd1189145b74e982a265350c923acd8101"
    ]
    tokenizer_json_sha256: Literal[
        "sha256:aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"
    ]


@dataclass(frozen=True)
class VerifiedQwen3TokenizerFiles:
    verification: Qwen3TokenizerFileVerification
    tokenizer_config_json: bytes
    tokenizer_json: bytes


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int


def _identity(value: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        mode=value.st_mode,
        links=value.st_nlink,
        size=value.st_size,
        modified_ns=value.st_mtime_ns,
        changed_ns=value.st_ctime_ns,
    )


def _read_pinned_regular_file(
    path: Path,
    *,
    expected_digest: str,
    limit: int,
) -> bytes:
    label = path.name
    try:
        before = _identity(os.lstat(path))
    except OSError:
        raise AdapterError(f"Qwen3 tokenizer file is unavailable: {label}") from None
    if (
        not stat.S_ISREG(before.mode)
        or before.links != 1
        or not 1 <= before.size <= limit
    ):
        raise AdapterError(f"Qwen3 tokenizer file is unsafe: {label}")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise AdapterError(
            f"Qwen3 tokenizer file cannot be opened safely: {label}"
        ) from None
    try:
        if _identity(os.fstat(descriptor)) != before:
            raise AdapterError(f"Qwen3 tokenizer file changed before reading: {label}")
        remaining = before.size
        content = bytearray()
        while remaining:
            try:
                chunk = os.read(descriptor, min(remaining, 1_048_576))
            except OSError:
                raise AdapterError(
                    f"Qwen3 tokenizer file changed during reading: {label}"
                ) from None
            if not chunk:
                raise AdapterError(
                    f"Qwen3 tokenizer file was truncated during reading: {label}"
                )
            content.extend(chunk)
            remaining -= len(chunk)
        try:
            extra = os.read(descriptor, 1)
        except OSError:
            raise AdapterError(
                f"Qwen3 tokenizer file changed during reading: {label}"
            ) from None
        if extra:
            raise AdapterError(f"Qwen3 tokenizer file grew during reading: {label}")
        try:
            descriptor_after = _identity(os.fstat(descriptor))
            path_after = _identity(os.lstat(path))
        except OSError:
            raise AdapterError(
                f"Qwen3 tokenizer file changed during reading: {label}"
            ) from None
        if descriptor_after != before or path_after != before:
            raise AdapterError(f"Qwen3 tokenizer file changed during reading: {label}")
    finally:
        os.close(descriptor)

    result = bytes(content)
    observed_digest = "sha256:" + hashlib.sha256(result).hexdigest()
    if observed_digest != expected_digest:
        raise AdapterError(f"Qwen3 tokenizer file digest differs: {label}")
    return result


def expected_qwen3_tokenizer_file_verification() -> Qwen3TokenizerFileVerification:
    return Qwen3TokenizerFileVerification(
        policy="bounded-regular-files-no-follow-sha256-v1",
        tokenizer_config_sha256=QWEN3_TOKENIZER_CONFIG_SHA256,
        tokenizer_json_sha256=QWEN3_TOKENIZER_JSON_SHA256,
    )


def load_verified_qwen3_tokenizer_files(
    tokenizer_root: Path,
) -> VerifiedQwen3TokenizerFiles:
    """Read exact tokenizer files without following links or accepting races."""

    if not tokenizer_root.is_absolute():
        raise AdapterError("Qwen3 tokenizer root must be absolute")
    try:
        root_stat = os.lstat(tokenizer_root)
    except OSError:
        raise AdapterError("Qwen3 tokenizer root is unavailable") from None
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise AdapterError("Qwen3 tokenizer root must be a real directory")

    tokenizer_json = _read_pinned_regular_file(
        tokenizer_root / "tokenizer.json",
        expected_digest=QWEN3_TOKENIZER_JSON_SHA256,
        limit=_TOKENIZER_JSON_LIMIT,
    )
    tokenizer_config = _read_pinned_regular_file(
        tokenizer_root / "tokenizer_config.json",
        expected_digest=QWEN3_TOKENIZER_CONFIG_SHA256,
        limit=_TOKENIZER_CONFIG_LIMIT,
    )
    return VerifiedQwen3TokenizerFiles(
        verification=expected_qwen3_tokenizer_file_verification(),
        tokenizer_config_json=tokenizer_config,
        tokenizer_json=tokenizer_json,
    )


def verify_qwen3_tokenizer_files(
    tokenizer_root: Path,
) -> Qwen3TokenizerFileVerification:
    return load_verified_qwen3_tokenizer_files(tokenizer_root).verification
