"""Strict local contracts for the attached external-router adapter."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from inferdrome.errors import VerificationError
from inferdrome.external_router.llmd import (
    adapt_llmd_attached_record,
)
from inferdrome.external_router.schema import check_schemas, schema_bytes
from inferdrome.external_router.verifier import verify_external_router_evidence
from tests.external_router_support import (
    fixture_record,
    fixture_record_bytes,
    fixture_router_config,
)


def _capture(record: dict[str, object] | None = None):
    return adapt_llmd_attached_record(
        fixture_record_bytes(record),
        router_config_bytes=fixture_router_config(),
    )


def test_llmd_fixture_is_canonical_repeatable_and_offline_verifiable() -> None:
    first = _capture()
    second = _capture()

    assert first.canonical_bytes == second.canonical_bytes
    assert first.retained_digest == second.retained_digest
    verified = verify_external_router_evidence(
        first.canonical_bytes,
        expected_digest=first.retained_digest,
    )
    assert verified.evidence.evidence_admissibility == "ADMISSIBLE"
    assert verified.evidence.evidence_unavailable_reasons == ()
    assert verified.retained_digest == first.retained_digest


def test_schema_snapshots_are_current_closed_and_draft_valid() -> None:
    check_schemas()
    for content in schema_bytes().values():
        schema = json.loads(content)
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False


def test_stale_or_explicitly_unavailable_telemetry_is_not_admissible() -> None:
    stale = fixture_record()
    stale_load = stale["observations"][3]
    assert isinstance(stale_load, dict)
    stale_load.update(
        {
            "state": "STALE",
            "sampled_at_monotonic_ns": 9_994_000,
            "age_ns": 6_000,
            "freshness_bound_ns": 5_000,
        }
    )
    captured_stale = _capture(stale)
    assert captured_stale.evidence.evidence_admissibility == "UNAVAILABLE"
    assert captured_stale.evidence.evidence_unavailable_reasons == (
        "STALE_TELEMETRY",
    )

    missing = fixture_record()
    missing_load = missing["observations"][1]
    assert isinstance(missing_load, dict)
    missing_load.clear()
    missing_load.update(
        {
            "observation_id": "load-a",
            "observer_id": "load-observer",
            "endpoint_id": "endpoint-a",
            "endpoint_identity_sha256": "sha256:" + "a" * 64,
            "signal": "LOAD",
            "state": "UNAVAILABLE",
            "clock_domain_id": "runner-monotonic",
            "epoch": 4,
            "unavailable_reason": "MISSING",
        }
    )
    captured_missing = _capture(missing)
    assert captured_missing.evidence.evidence_admissibility == "UNAVAILABLE"
    assert captured_missing.evidence.evidence_unavailable_reasons == (
        "UNAVAILABLE_TELEMETRY",
    )


def test_router_policy_or_reason_is_never_inferred() -> None:
    record = fixture_record()
    policy = record["decision"]["policy"]
    reason = record["decision"]["reason"]
    assert isinstance(policy, dict)
    assert isinstance(reason, dict)
    policy.clear()
    policy.update({"state": "UNAVAILABLE", "unavailable_reason": "UNSUPPORTED"})
    reason.clear()
    reason.update({"state": "UNAVAILABLE", "unavailable_reason": "MISSING"})

    captured = _capture(record)
    assert captured.evidence.decision.policy.value is None
    assert captured.evidence.decision.reason.value is None
    assert captured.evidence.evidence_admissibility == "UNAVAILABLE"
    assert captured.evidence.evidence_unavailable_reasons == (
        "UNAVAILABLE_POLICY",
        "UNAVAILABLE_REASON",
    )


def test_canonical_evidence_rejects_tamper_and_raw_config_retention() -> None:
    captured = _capture()
    assert b'"scope":"local-only"' not in captured.canonical_bytes
    assert b"origin" not in captured.canonical_bytes
    assert b"prompt" not in captured.canonical_bytes
    assert b"response" not in captured.canonical_bytes

    replacement = captured.canonical_bytes.replace(
        b"lowest-queue", b"different-reason", 1
    )
    with pytest.raises(VerificationError, match="digest"):
        verify_external_router_evidence(
            replacement,
            expected_digest=captured.retained_digest,
        )
