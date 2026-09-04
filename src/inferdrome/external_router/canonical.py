"""Canonical bytes and isolated digest domains for attached-router evidence."""

from __future__ import annotations

import hashlib
from typing import Any

import rfc8785

_ROUTER_IDENTITY_PREFIX = b"inferdrome:external-router-identity-v1\0"
_TOPOLOGY_IDENTITY_PREFIX = b"inferdrome:external-router-topology-v1\0"
_EVIDENCE_PREFIX = b"inferdrome:external-router-evidence-v1\0"


def canonical_json_bytes(value: Any) -> bytes:
    """Encode one value as RFC 8785 canonical JSON bytes."""

    return rfc8785.dumps(value)


def sha256_digest(payload: bytes) -> str:
    """Return an exact-byte SHA-256 identity."""

    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def router_identity_digest(value: Any) -> str:
    """Return a domain-separated digest of one router identity projection."""

    return sha256_digest(_ROUTER_IDENTITY_PREFIX + canonical_json_bytes(value))


def topology_identity_digest(value: Any) -> str:
    """Return a domain-separated digest of one logical topology projection."""

    return sha256_digest(_TOPOLOGY_IDENTITY_PREFIX + canonical_json_bytes(value))


def evidence_digest(canonical_evidence_bytes: bytes) -> str:
    """Return the retained digest of exact canonical evidence bytes."""

    return sha256_digest(_EVIDENCE_PREFIX + canonical_evidence_bytes)
