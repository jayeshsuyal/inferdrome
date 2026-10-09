"""Opt-in request correlation for live router studies; identity, not causality."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from inferdrome.routing_execution.canonical import canonical_json_bytes

REQUEST_ID_HEADER = "X-Inferdrome-Request-ID"
CORRELATED_SCHEMA = "inferdrome.vllm-router-correlated-result.v1"
VERIFICATION_SCHEMA = "inferdrome.vllm-router-request-links.v1"
MEASUREMENT_SCHEMAS = {
    "inferdrome.vllm-router-study-result.v1",
    "inferdrome.vllm-router-capacity-result.v2",
    "inferdrome.vllm-router-timed-result.v1",
}
SUPPORTED_POLICIES = {
    "round_robin",
    "least_busy",
    "cache_only",
    "cache_plus_load",
    "cache_saturation",
}
ROUTER_OUTCOMES = {
    "error",
    "completed",
    "rejected_accounting",
    "rejected_capacity",
    "rejected_queue_timeout",
    "rejected_body",
    "rejected_input",
    "rejected_request_id",
    "disconnected",
    "cancelled",
    "timeout",
    "upstream_error",
    "upstream_http_error",
    "upstream_protocol_error",
    "stream_limit",
}


def valid_request_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _read_json(raw: bytes) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("nonfinite JSON value")

    return json.loads(
        raw, object_pairs_hook=unique_object, parse_constant=reject_constant
    )


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _ledger_digest(rows: list[dict[str, Any]]) -> str:
    # The existing ledger has absolute monotonic nanoseconds. Long host uptimes
    # can exceed RFC 8785's integer domain; preserve those integers exactly.
    raw = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _check_digest(value: dict[str, Any], label: str) -> None:
    if value.get("result_sha256") != _digest(
        {key: item for key, item in value.items() if key != "result_sha256"}
    ):
        raise ValueError(f"{label} digest mismatch")


def correlated_result(
    measurement: dict[str, Any], request_links: list[dict[str, Any]]
) -> dict[str, Any]:
    """Wrap an unchanged measurement artifact, including all scheduled offers."""
    result = {
        "schema": CORRELATED_SCHEMA,
        "measurement": measurement,
        "request_links": request_links,
    }
    result["result_sha256"] = _digest(result)
    return result


def validate_correlated_result(
    result: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(result, dict) or set(result) != {
        "schema",
        "measurement",
        "request_links",
        "result_sha256",
    }:
        raise ValueError(
            "correlated result fields invalid; legacy results have no links"
        )
    if result["schema"] != CORRELATED_SCHEMA:
        raise ValueError("unsupported correlated result schema")
    _check_digest(result, "correlated result")
    measurement = result["measurement"]
    if (
        not isinstance(measurement, dict)
        or not isinstance(measurement.get("schema"), str)
        or measurement["schema"] not in MEASUREMENT_SCHEMAS
    ):
        raise ValueError("unsupported embedded measurement schema")
    _check_digest(measurement, "measurement")
    if (
        not isinstance(measurement.get("policy"), str)
        or measurement["policy"] not in SUPPORTED_POLICIES
    ):
        raise ValueError("measurement policy invalid")
    rows = measurement.get("rows")
    links = result["request_links"]
    if (
        not isinstance(rows, list)
        or not rows
        or not isinstance(links, list)
        or len(links) != len(rows)
        or not isinstance(measurement.get("comparison_valid"), bool)
    ):
        raise ValueError("client/link population invalid")
    seen: set[str] = set()
    for index, (row, link) in enumerate(zip(rows, links, strict=True)):
        if (
            not isinstance(row, dict)
            or type(row.get("index")) is not int
            or row["index"] != index
            or not isinstance(link, dict)
            or set(link) != {"index", "request_id", "response_request_id"}
            or type(link["index"]) is not int
            or link["index"] != index
            or not valid_request_id(link["request_id"])
            or link["request_id"] in seen
        ):
            raise ValueError("client/link index or request identity invalid")
        seen.add(link["request_id"])
        if not isinstance(row.get("outcome"), str) or not row["outcome"]:
            raise ValueError("client outcome invalid")
        for key in ("dispatch_ns", "response_headers_ns"):
            if key not in row or (
                row[key] is not None and (type(row[key]) is not int or row[key] < 0)
            ):
                raise ValueError("client dispatch/header observation invalid")
        response_id = link["response_request_id"]
        if row["response_headers_ns"] is not None:
            if row["dispatch_ns"] is None:
                raise ValueError("undispatched request has response headers")
            if not valid_request_id(response_id) or response_id != link["request_id"]:
                raise ValueError("response request identity missing or mismatched")
        elif response_id is not None:
            raise ValueError("response identity has no observed response headers")
    return measurement, rows, links


def verify_links(
    result: dict[str, Any], ledger_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Join by identity only. Missing dispatch receipts remain explicitly unresolved."""
    measurement, rows, links = validate_correlated_result(result)
    offered_ids = {link["request_id"] for link in links}
    ledger_by_id: dict[str, dict[str, Any]] = {}
    for row in ledger_rows:
        if not isinstance(row, dict) or not valid_request_id(row.get("request_id")):
            raise ValueError("router ledger request identity invalid")
        request_id = row["request_id"]
        if request_id in ledger_by_id:
            raise ValueError("duplicate router ledger request identity")
        if request_id not in offered_ids:
            raise ValueError("orphan router ledger request identity")
        if row.get("policy") != measurement.get("policy"):
            raise ValueError("router ledger policy mismatch")
        if (
            not isinstance(row.get("outcome"), str)
            or row["outcome"] not in ROUTER_OUTCOMES
        ):
            raise ValueError("router ledger outcome invalid")
        replica = row.get("replica")
        if (
            "replica" not in row
            or (row["outcome"] == "completed" and replica is None)
            or (
                replica is not None
                and (type(replica) is not int or replica not in (0, 1))
            )
        ):
            raise ValueError("router ledger replica invalid")
        ledger_by_id[request_id] = row
    joined: list[dict[str, Any]] = []
    unresolved = 0
    for row, link in zip(rows, links, strict=True):
        receipt = ledger_by_id.get(link["request_id"])
        if row["dispatch_ns"] is None:
            if receipt is not None:
                raise ValueError("undispatched request has a router ledger row")
            state = "NOT_DISPATCHED"
        elif receipt is None:
            state = "DISPATCHED_WITHOUT_ROUTER_RECORD"
            unresolved += 1
        else:
            state = "MATCHED"
        joined.append(
            {
                "index": row["index"],
                "request_id": link["request_id"],
                "state": state,
                "client_outcome": row.get("outcome"),
                "router_outcome": receipt["outcome"] if receipt is not None else None,
                "replica": receipt.get("replica") if receipt is not None else None,
            }
        )
    report = {
        "schema": VERIFICATION_SCHEMA,
        "scope": "REQUEST_IDENTITY_ONLY",
        "status": "VERIFIED" if unresolved == 0 else "INCOMPLETE",
        "correlation_valid": unresolved == 0,
        "measurement_comparison_valid": measurement["comparison_valid"],
        "correlated_result_sha256": result["result_sha256"],
        "measurement_sha256": measurement["result_sha256"],
        "ledger_rows_sha256": _ledger_digest(ledger_rows),
        "ledger_rows_encoding": "PYTHON_JSON_SORTED_COMPACT_UTF8_V1",
        "offered": len(rows),
        "matched": len(ledger_rows),
        "not_dispatched": sum(item["state"] == "NOT_DISPATCHED" for item in joined),
        "unresolved_dispatches": unresolved,
        "links": joined,
    }
    report["result_sha256"] = _digest(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify live-study request links offline"
    )
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = _read_json(args.result.read_bytes())
    raw_ledger = args.ledger.read_bytes()
    report = verify_links(
        result, [_read_json(line) for line in raw_ledger.splitlines()]
    )
    report["ledger_file_sha256"] = "sha256:" + hashlib.sha256(raw_ledger).hexdigest()
    report["result_sha256"] = _digest(
        {key: value for key, value in report.items() if key != "result_sha256"}
    )
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    if not report["correlation_valid"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
