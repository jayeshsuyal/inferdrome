"""Routing-local canonical encoders and digest helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

import rfc8785

_PACKAGE_DIGEST_PREFIX = b"inferdrome:routing-campaign-package-v1\0"


def canonical_json_bytes(value: Any) -> bytes:
    """Return RFC 8785 canonical JSON bytes without extending frozen digests."""

    return rfc8785.dumps(value)


def canonical_jsonl_bytes(values: Iterable[Any]) -> bytes:
    """Return canonical JSON Lines with one LF per non-empty record."""

    records = tuple(canonical_json_bytes(value) for value in values)
    if not records:
        return b""
    return b"".join(record + b"\n" for record in records)


def sha256_digest(payload: bytes) -> str:
    """Return the ordinary exact-byte digest used by a manifest entry."""

    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def package_digest(manifest_bytes: bytes) -> str:
    """Return the retained domain-separated digest of canonical manifest bytes."""

    return (
        f"sha256:{hashlib.sha256(_PACKAGE_DIGEST_PREFIX + manifest_bytes).hexdigest()}"
    )
