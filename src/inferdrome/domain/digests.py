"""Domain-separated exact-byte and canonical-JSON digests."""

import hashlib
from enum import StrEnum
from typing import Any

import rfc8785


class DigestDomain(StrEnum):
    SOURCE_SPEC = "source-spec-v1"
    EXECUTION_FINGERPRINT = "execution-fingerprint-v1"
    REQUEST_PLAN = "request-plan-v1"
    METRIC_DEFINITIONS = "metric-definitions-v1"
    BUNDLE_MANIFEST = "bundle-manifest-v1"
    TRIAL_SET = "trial-set-v1"
    COMPARISON_PLAN = "comparison-plan-v1"
    COMPARISON_RESULT = "comparison-result-v1"
    DEPLOYMENT_SPEC = "deployment-spec-v1"
    LIFECYCLE_OUTCOME = "lifecycle-outcome-v1"
    DEPLOYMENT_RECEIPT = "deployment-receipt-v1"


def digest_bytes(domain: DigestDomain, payload: bytes) -> str:
    """Hash exact bytes with an explicit, versioned domain separator."""

    separator = f"inferdrome:{domain.value}\0".encode()
    return f"sha256:{hashlib.sha256(separator + payload).hexdigest()}"


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a JSON value using RFC 8785 canonical JSON."""

    return rfc8785.dumps(value)


def digest_canonical_json(domain: DigestDomain, value: Any) -> str:
    """Hash a canonical JSON value in one of Inferdrome's digest domains."""

    return digest_bytes(domain, canonical_json_bytes(value))
