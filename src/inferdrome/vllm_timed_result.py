"""Offline timing and client-accounting checks; no routing winner is inferred."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import TIMED_RESULT_SCHEMA, validate_timing
from inferdrome.vllm_request_identity import _read_json, validate_correlated_result
from inferdrome.vllm_router_study import (
    CAPACITY_SCHEMA,
    Offer,
    RequestResult,
    summarize,
    validate_capacity_certificate,
    validate_token_certificate,
)

VERIFICATION_SCHEMA = "inferdrome.vllm-router-timed-result-check.v1"
_OUTCOMES = {
    "error",
    "completed",
    "rejected",
    "http_error",
    "protocol_error",
    "prompt_length_mismatch",
    "output_length_mismatch",
    "timeout",
    "cancelled",
    "transport_error",
}
_FIELDS = {
    "schema",
    "plan_sha256",
    "trace_sha256",
    "token_certificate_sha256",
    "policy",
    "model",
    "router_origin",
    "started_unix_ns",
    "evidence_class",
    "router_accounting_valid",
    "status",
    "comparison_valid",
    "router_stats_after",
    "rows",
    "summary",
    "base_plan_schema",
    "base_plan_sha256",
    "base_trace_sha256",
    "token_certificate_scope",
    "arrival_timing_scope",
    "result_sha256",
}


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _client_row(
    raw: dict[str, Any], offer: Offer, plan: dict[str, Any]
) -> RequestResult:
    if set(raw) != {field.name for field in fields(RequestResult)}:
        raise ValueError("timed client row fields mismatch")
    expected = {
        "index": offer.index,
        "scheduled_ns": offer.scheduled_ns,
        "epoch": offer.epoch,
        "traffic_class": offer.traffic_class,
        "tenant": offer.tenant,
        "document_id": offer.document_id,
    }
    if any(
        type(raw[key]) is not type(value) for key, value in expected.items()
    ) or canonical_json_bytes(
        {key: raw[key] for key in expected}
    ) != canonical_json_bytes(expected):
        raise ValueError("timed client row differs from planned offer")
    row = RequestResult(**raw)
    stamps = [
        row.ready_ns,
        row.dispatch_ns,
        row.response_headers_ns,
        row.first_body_byte_ns,
        row.first_content_ns,
        row.terminal_ns,
    ]
    if type(row.terminal_ns) is not int or any(
        value is not None and (type(value) is not int or value < 0)
        for value in [
            *stamps,
            row.max_content_gap_ns,
            row.prompt_tokens,
            row.completion_tokens,
        ]
    ):
        raise ValueError("timed client timestamp or token count invalid")
    observed = [value for value in stamps if value is not None]
    if observed != sorted(observed) or (
        row.ready_ns is not None and row.ready_ns < row.scheduled_ns
    ):
        raise ValueError("timed client timestamp order invalid")
    if (
        (row.dispatch_ns is not None and row.ready_ns is None)
        or (row.first_body_byte_ns is not None and row.response_headers_ns is None)
        or (row.first_content_ns is not None and row.first_body_byte_ns is None)
        or (
            row.http_status is not None
            and (type(row.http_status) is not int or not 100 <= row.http_status <= 599)
        )
        or ((row.http_status is None) != (row.response_headers_ns is None))
        or row.outcome not in _OUTCOMES
        or (row.outcome == "rejected" and row.http_status != 503)
        or (row.outcome == "http_error" and row.http_status in (None, 200, 503))
        or (
            row.outcome
            in {"protocol_error", "prompt_length_mismatch", "output_length_mismatch"}
            and row.http_status != 200
        )
    ):
        raise ValueError("timed client observations invalid")
    expected_tokens = (
        plan["prompt_tokens_by_index"][offer.index]
        if plan["schema"] == CAPACITY_SCHEMA
        else plan["expected_prompt_tokens"]
    )
    if row.outcome == "completed" and (
        row.http_status != 200
        or row.first_content_ns is None
        or row.prompt_tokens != expected_tokens
        or row.completion_tokens != plan["max_tokens"]
    ):
        raise ValueError("timed completed request token or HTTP mismatch")
    return row


def verify_timed_result(
    plan: dict[str, Any],
    timing: dict[str, Any],
    result: dict[str, Any],
    *,
    token_certificate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reproduce timing and client reductions, separately from ledger correlation."""
    offers = validate_timing(plan, timing)
    measurement, raw_rows, _ = validate_correlated_result(result)
    if set(measurement) != _FIELDS:
        raise ValueError("timed measurement fields invalid")
    expected = {
        "schema": TIMED_RESULT_SCHEMA,
        "plan_sha256": timing["timing_sha256"],
        "trace_sha256": timing["transformed_trace_sha256"],
        "base_plan_schema": plan["schema"],
        "base_plan_sha256": plan["plan_sha256"],
        "base_trace_sha256": plan["trace_sha256"],
        "token_certificate_scope": "BASE_WORKLOAD_UNCHANGED_TIMING_ONLY",
        "arrival_timing_scope": "PLANNED_OFFERS_NOT_OBSERVED_ARRIVALS",
        "evidence_class": "SYNTHETIC_ONLY"
        if plan["phase"] == "fixture"
        else "LOCAL_MEASUREMENT_ONLY",
    }
    if any(measurement.get(key) != value for key, value in expected.items()):
        raise ValueError("timed measurement provenance mismatch")
    if token_certificate is not None:
        if plan["schema"] == CAPACITY_SCHEMA:
            validate_capacity_certificate(plan, token_certificate)
        else:
            validate_token_certificate(plan, token_certificate)
    elif plan["phase"] != "fixture":
        raise ValueError("timed measurement requires the base token certificate")
    if measurement["token_certificate_sha256"] != (
        token_certificate["certificate_sha256"]
        if token_certificate is not None
        else None
    ):
        raise ValueError("timed measurement token certificate mismatch")
    if len(raw_rows) != len(offers):
        raise ValueError("timed client population mismatch")
    rows = [
        _client_row(raw, offer, plan)
        for raw, offer in zip(raw_rows, offers, strict=True)
    ]
    summary = summarize(plan, rows, include_client_timing=True)
    if canonical_json_bytes(measurement["summary"]) != canonical_json_bytes(summary):
        raise ValueError("timed client summary mismatch")
    after = measurement["router_stats_after"]
    expected_stats = {
        "policy": measurement["policy"],
        "offered": len(offers),
        "terminal": len(offers),
        "ledger_rows": len(offers),
        "in_flight": 0,
        "pending": 0,
        "active": 0,
        "busy": [0, 0],
        "accounting_failed": False,
    }
    accounting_valid = isinstance(after, dict) and canonical_json_bytes(
        {key: after.get(key) for key in expected_stats}
    ) == canonical_json_bytes(expected_stats)
    if (
        type(measurement["router_accounting_valid"]) is not bool
        or measurement["router_accounting_valid"] != accounting_valid
        or measurement["status"] not in ("COMPLETED", "INTERRUPTED", "DRAIN_TIMEOUT")
        or (accounting_valid and any(row.dispatch_ns is None for row in rows))
        or (
            measurement["status"] == "COMPLETED"
            and any(row.outcome == "cancelled" for row in rows)
        )
    ):
        raise ValueError("timed client accounting status mismatch")
    comparison_valid = (
        measurement["status"] == "COMPLETED"
        and accounting_valid
        and not any(
            row.outcome in {"prompt_length_mismatch", "output_length_mismatch"}
            for row in rows
        )
    )
    if measurement["comparison_valid"] != comparison_valid:
        raise ValueError("timed client comparison flag mismatch")
    report = {
        "schema": VERIFICATION_SCHEMA,
        "scope": "TIMING_AND_CLIENT_ACCOUNTING_ONLY",
        "status": "VERIFIED",
        "base_plan_sha256": plan["plan_sha256"],
        "timing_sha256": timing["timing_sha256"],
        "population_sha256": timing["population_sha256"],
        "transformed_trace_sha256": timing["transformed_trace_sha256"],
        "correlated_result_sha256": result["result_sha256"],
        "measurement_sha256": measurement["result_sha256"],
        "offered": len(offers),
        "goodput_denominator_ns": plan["duration_ns"],
        "measurement_comparison_valid": comparison_valid,
        "router_ledger_verification": "REQUIRES_SEPARATE_REQUEST_LINK_CHECK",
    }
    report["result_sha256"] = _digest(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify timed client results offline")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--timing", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--token-certificate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify_timed_result(
        _read_json(args.plan.read_bytes()),
        _read_json(args.timing.read_bytes()),
        _read_json(args.result.read_bytes()),
        token_certificate=(
            _read_json(args.token_certificate.read_bytes())
            if args.token_certificate is not None
            else None
        ),
    )
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")


if __name__ == "__main__":
    main()
