"""Canonical bytes and local digest domains for routing-execution-v1."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

import rfc8785

_PACKAGE_DIGEST_PREFIX = b"inferdrome:routing-execution-package-v1\0"
_INPUT_TRANSFER_DIGEST_PREFIX = b"inferdrome:routing-execution-input-transfer-v1\0"


def canonical_json_bytes(value: Any) -> bytes:
    """Encode one JSON value with RFC 8785 without extending frozen domains."""

    return rfc8785.dumps(value)


def canonical_jsonl_bytes(values: Iterable[Any]) -> bytes:
    """Encode a bounded sequence of canonical JSON Lines."""

    records = tuple(canonical_json_bytes(value) for value in values)
    return b"" if not records else b"".join(record + b"\n" for record in records)


def sha256_digest(payload: bytes) -> str:
    """Return an ordinary exact-byte SHA-256 identity."""

    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def package_digest(manifest_bytes: bytes) -> str:
    """Return the retained, domain-separated package identity."""

    return (
        f"sha256:{hashlib.sha256(_PACKAGE_DIGEST_PREFIX + manifest_bytes).hexdigest()}"
    )


def input_transfer_digest(config_projection: Any, selected_workload_sha256: str) -> str:
    """Bind a config projection to the exact selected workload identity.

    The caller replaces the declaration field with a fixed zero digest first,
    avoiding a self-referential declaration while retaining every other config
    fact in the binding.
    """

    payload = (
        _INPUT_TRANSFER_DIGEST_PREFIX
        + canonical_json_bytes(config_projection)
        + b"\0"
        + selected_workload_sha256.encode()
    )
    return sha256_digest(payload)
