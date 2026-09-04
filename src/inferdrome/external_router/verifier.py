"""Offline canonical verification for attached external-router evidence."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from inferdrome.errors import VerificationError
from inferdrome.external_router.canonical import evidence_digest
from inferdrome.external_router.contracts import (
    ExternalRouterEvidence,
    external_router_evidence_bytes,
)
from inferdrome.external_router.llmd import (
    ExternalRouterAdapterError,
    decode_attached_record,
)


@dataclass(frozen=True)
class VerifiedExternalRouterEvidence:
    """Offline-verified evidence plus its exact retained identity."""

    evidence: ExternalRouterEvidence
    retained_digest: str


def verify_external_router_evidence(
    content: bytes,
    *,
    expected_digest: str | None = None,
) -> VerifiedExternalRouterEvidence:
    """Verify strict semantics, canonical bytes, and an optional retained digest."""

    try:
        decode_attached_record(content)
        evidence = ExternalRouterEvidence.model_validate_json(content)
    except (ExternalRouterAdapterError, ValidationError):
        raise VerificationError(
            "external router evidence violates its contract"
        ) from None
    canonical = external_router_evidence_bytes(evidence)
    if canonical != content:
        raise VerificationError("external router evidence is not canonical JSON")
    retained_digest = evidence_digest(canonical)
    if expected_digest is not None and expected_digest != retained_digest:
        raise VerificationError("external router evidence digest disagrees")
    return VerifiedExternalRouterEvidence(
        evidence=evidence,
        retained_digest=retained_digest,
    )
