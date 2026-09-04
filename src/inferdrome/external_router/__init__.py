"""Attached external-router evidence contracts.

This additive namespace validates records supplied by an independently operated
router.  It does not embed, contact, or control that router.
"""

from inferdrome.external_router.llmd import (
    CapturedExternalRouterEvidence,
    ExternalRouterAdapterError,
    adapt_llmd_attached_record,
)
from inferdrome.external_router.verifier import (
    VerifiedExternalRouterEvidence,
    verify_external_router_evidence,
)

__all__ = [
    "CapturedExternalRouterEvidence",
    "ExternalRouterAdapterError",
    "VerifiedExternalRouterEvidence",
    "adapt_llmd_attached_record",
    "verify_external_router_evidence",
]
