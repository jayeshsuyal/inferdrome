"""The local-fixture llm-d attachment profile.

``llm-d-attached-v1`` is an Inferdrome input envelope, not an assertion that
llm-d exposes this event format.  The adapter only checks supplied local bytes;
it has no transport, Kubernetes, cloud, or subprocess dependency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from inferdrome.external_router.canonical import (
    canonical_json_bytes,
    evidence_digest,
    sha256_digest,
)
from inferdrome.external_router.contracts import (
    ExternalRouterEvidence,
    LlmdAttachedRecord,
    external_router_evidence_bytes,
)

MAX_ATTACHED_RECORD_BYTES = 1_048_576
MAX_ROUTER_CONFIG_BYTES = 1_048_576


class ExternalRouterAdapterError(ValueError):
    """An attached external-router record cannot be safely captured."""


@dataclass(frozen=True)
class CapturedExternalRouterEvidence:
    """A validated canonical receipt plus its externally retained digest."""

    evidence: ExternalRouterEvidence
    canonical_bytes: bytes
    retained_digest: str


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode_attached_record(content: bytes) -> dict[str, Any]:
    """Decode one bounded JSON object while rejecting ambiguous JSON syntax."""

    if (
        not isinstance(content, bytes)
        or not 1 <= len(content) <= MAX_ATTACHED_RECORD_BYTES
    ):
        raise ExternalRouterAdapterError("attached router record has invalid size")
    try:
        decoded = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ExternalRouterAdapterError(
            "attached router record is invalid JSON"
        ) from None
    if not isinstance(decoded, dict):
        raise ExternalRouterAdapterError("attached router record must be a JSON object")
    return decoded


def _validated_record(content: bytes) -> LlmdAttachedRecord:
    try:
        # Decode once ourselves first to reject duplicate keys and non-finite
        # constants.  Pydantic's JSON path then preserves JSON arrays as the
        # strict tuple contracts used by the canonical schemas.
        decode_attached_record(content)
        return LlmdAttachedRecord.model_validate_json(content)
    except ValidationError:
        raise ExternalRouterAdapterError(
            "attached router record violates the llm-d profile"
        ) from None


def adapt_llmd_attached_record(
    record_bytes: bytes,
    *,
    router_config_bytes: bytes,
) -> CapturedExternalRouterEvidence:
    """Validate local profile bytes and emit canonical external-router evidence.

    Raw router configuration is accepted solely to recompute its supplied
    identity.  It is never parsed, retained, serialized, or included in errors.
    """

    if (
        not isinstance(router_config_bytes, bytes)
        or not 1 <= len(router_config_bytes) <= MAX_ROUTER_CONFIG_BYTES
    ):
        raise ExternalRouterAdapterError("router configuration has invalid size")
    record = _validated_record(record_bytes)
    if record.router.config_sha256 != sha256_digest(router_config_bytes):
        raise ExternalRouterAdapterError("router configuration identity disagrees")
    payload = record.model_dump(mode="json")
    payload["schema_version"] = "inferdrome.external-router-evidence.v1"
    reasons = record.computed_unavailable_reasons()
    payload["evidence_admissibility"] = (
        "ADMISSIBLE" if not reasons else "UNAVAILABLE"
    )
    payload["evidence_unavailable_reasons"] = reasons
    try:
        evidence = ExternalRouterEvidence.model_validate_json(
            canonical_json_bytes(payload)
        )
    except ValidationError:
        raise ExternalRouterAdapterError(
            "attached router evidence assessment is invalid"
        ) from None
    canonical = external_router_evidence_bytes(evidence)
    return CapturedExternalRouterEvidence(
        evidence=evidence,
        canonical_bytes=canonical,
        retained_digest=evidence_digest(canonical),
    )
