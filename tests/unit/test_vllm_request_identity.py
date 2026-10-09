"""Adversarial offline checks for identity joins, without GPU or network access."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

from inferdrome import vllm_request_identity as identity
from inferdrome.routing_execution.canonical import canonical_json_bytes

SCHEMAS = (
    "inferdrome.vllm-router-study-result.v1",
    "inferdrome.vllm-router-capacity-result.v2",
)
IDS = ("1" * 32, "2" * 32, "3" * 32)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _rehash(value: dict) -> None:
    value["result_sha256"] = _digest(
        {key: item for key, item in value.items() if key != "result_sha256"}
    )


def _fixture(schema: str = SCHEMAS[0]) -> tuple[dict, list[dict]]:
    rows = [
        {
            "index": index,
            "scheduled_ns": 100 + index,
            "epoch": 0,
            "traffic_class": "repeated_prefix",
            "tenant": "fixture",
            "dispatch_ns": 200 + index if index < 2 else None,
            "response_headers_ns": 300 + index if index < 2 else None,
            "first_body_byte_ns": 400 + index if index < 2 else None,
            "first_content_ns": 500 + index if index < 2 else None,
            "terminal_ns": 600 + index,
            "max_content_gap_ns": None,
            "outcome": "completed" if index < 2 else "not_dispatched",
            "http_status": 200 if index < 2 else None,
            "prompt_tokens": 286 if index < 2 else None,
            "completion_tokens": 128 if index < 2 else None,
        }
        for index in range(3)
    ]
    if schema == SCHEMAS[1]:
        for row in rows:
            row.update(ready_ns=150 + row["index"], document_id=row["index"])
    measurement = {
        "schema": schema,
        "policy": "cache_only",
        "model": "fixture-model",
        "evidence_class": "SYNTHETIC_ONLY",
        "status": "COMPLETED",
        "comparison_valid": False,
        "rows": rows,
    }
    _rehash(measurement)
    links = [
        {
            "index": index,
            "request_id": request_id,
            "response_request_id": request_id if index < 2 else None,
        }
        for index, request_id in enumerate(IDS)
    ]
    ledger = [
        {
            "request_id": IDS[index],
            "policy": "cache_only",
            "outcome": "completed",
            "replica": index,
        }
        for index in range(2)
    ]
    return identity.correlated_result(measurement, links), ledger


@pytest.mark.parametrize("schema", SCHEMAS)
def test_wrapper_preserves_measurement_without_upgrading_validity(schema: str) -> None:
    result, ledger = _fixture(schema)
    measurement = copy.deepcopy(result["measurement"])
    before = canonical_json_bytes(measurement)
    result = identity.correlated_result(measurement, result["request_links"])
    report = identity.verify_links(result, ledger)

    assert canonical_json_bytes(result["measurement"]) == before
    assert report["measurement_sha256"] == result["measurement"]["result_sha256"]
    assert report["correlated_result_sha256"] == result["result_sha256"]
    assert report["status"] == "VERIFIED"
    assert report["correlation_valid"] is True
    assert report["measurement_comparison_valid"] is False
    assert report["scope"] == "REQUEST_IDENTITY_ONLY"
    assert report["result_sha256"] == _digest(
        {key: value for key, value in report.items() if key != "result_sha256"}
    )


def test_out_of_order_receipts_join_by_id_and_preserve_undispatched_offer() -> None:
    result, ledger = _fixture()
    ledger[0]["outcome"] = "cancelled"
    ledger.reverse()

    report = identity.verify_links(result, ledger)

    assert report["offered"] == 3
    assert report["matched"] == 2
    assert report["not_dispatched"] == 1
    assert report["unresolved_dispatches"] == 0
    assert [row["request_id"] for row in report["links"]] == list(IDS)
    assert [row["replica"] for row in report["links"]] == [0, 1, None]
    assert report["links"][0]["client_outcome"] == "completed"
    assert report["links"][0]["router_outcome"] == "cancelled"
    assert report["links"][2] == {
        "index": 2,
        "request_id": IDS[2],
        "state": "NOT_DISPATCHED",
        "client_outcome": "not_dispatched",
        "router_outcome": None,
        "replica": None,
    }


def test_missing_dispatched_receipt_is_incomplete_even_after_response() -> None:
    result, ledger = _fixture()
    report = identity.verify_links(result, ledger[1:])

    assert report["status"] == "INCOMPLETE"
    assert report["correlation_valid"] is False
    assert report["unresolved_dispatches"] == 1
    assert report["matched"] == 1
    assert report["links"][0]["state"] == "DISPATCHED_WITHOUT_ROUTER_RECORD"
    assert report["links"][0]["router_outcome"] is None
    assert report["links"][1]["state"] == "MATCHED"


def test_duplicate_client_identity_is_rejected_after_rehashing() -> None:
    result, ledger = _fixture()
    result["request_links"][1].update(request_id=IDS[0], response_request_id=IDS[0])
    _rehash(result)
    with pytest.raises(ValueError, match="identity invalid"):
        identity.verify_links(result, ledger)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate", "duplicate router ledger"),
        ("orphan", "orphan router ledger"),
        ("policy", "policy mismatch"),
        ("not_dispatched", "undispatched request"),
        ("invalid_outcome", "outcome invalid"),
        ("bool_replica", "replica invalid"),
    ],
)
def test_invalid_router_receipts_are_rejected(mutation: str, message: str) -> None:
    result, ledger = _fixture()
    if mutation == "duplicate":
        ledger.append(copy.deepcopy(ledger[0]))
    elif mutation == "orphan":
        ledger[0]["request_id"] = "f" * 32
    elif mutation == "policy":
        ledger[0]["policy"] = "least_busy"
    elif mutation == "not_dispatched":
        ledger.append({**ledger[0], "request_id": IDS[2]})
    elif mutation == "invalid_outcome":
        ledger[0]["outcome"] = "imaginary_success"
    elif mutation == "bool_replica":
        ledger[0]["replica"] = False

    with pytest.raises(ValueError, match=message):
        identity.verify_links(result, ledger)


@pytest.mark.parametrize("missing_from", ["measurement", "ledger", "both"])
def test_missing_policy_cannot_match_by_shared_absence(missing_from: str) -> None:
    result, ledger = _fixture()
    if missing_from in {"measurement", "both"}:
        del result["measurement"]["policy"]
    if missing_from in {"ledger", "both"}:
        for row in ledger:
            del row["policy"]
    _rehash(result["measurement"])
    _rehash(result)

    with pytest.raises(ValueError, match="policy"):
        identity.verify_links(result, ledger)


@pytest.mark.parametrize("policy", ["invented_policy", ["cache_only"]])
def test_identically_invalid_policies_do_not_make_valid_evidence(
    policy: object,
) -> None:
    result, ledger = _fixture()
    result["measurement"]["policy"] = policy
    for row in ledger:
        row["policy"] = copy.deepcopy(policy)
    _rehash(result["measurement"])
    _rehash(result)

    with pytest.raises(ValueError, match="measurement policy invalid"):
        identity.verify_links(result, ledger)


@pytest.mark.parametrize("mutation", ["missing", "empty", "list"])
def test_client_outcome_must_be_present_and_a_nonempty_string(mutation: str) -> None:
    result, ledger = _fixture()
    row = result["measurement"]["rows"][0]
    if mutation == "missing":
        del row["outcome"]
    elif mutation == "empty":
        row["outcome"] = ""
    else:
        row["outcome"] = ["completed"]
    _rehash(result["measurement"])
    _rehash(result)

    with pytest.raises(ValueError, match="client outcome invalid"):
        identity.verify_links(result, ledger)


@pytest.mark.parametrize("mutation", ["missing", "null"])
def test_completed_router_receipt_requires_selected_replica(mutation: str) -> None:
    result, ledger = _fixture()
    if mutation == "missing":
        del ledger[0]["replica"]
    else:
        ledger[0]["replica"] = None

    with pytest.raises(ValueError, match="router ledger replica invalid"):
        identity.verify_links(result, ledger)


def test_rejected_router_receipt_can_have_explicit_null_replica() -> None:
    result, ledger = _fixture()
    ledger[0].update(outcome="rejected_input", replica=None)
    assert identity.verify_links(result, ledger)["status"] == "VERIFIED"

    del ledger[0]["replica"]
    with pytest.raises(ValueError, match="router ledger replica invalid"):
        identity.verify_links(result, ledger)


@pytest.mark.parametrize("mutation", ["schema_list", "measurement_list"])
def test_malformed_embedded_measurement_is_a_validation_error(mutation: str) -> None:
    result, ledger = _fixture()
    if mutation == "schema_list":
        result["measurement"]["schema"] = [SCHEMAS[0]]
        _rehash(result["measurement"])
    else:
        result["measurement"] = [result["measurement"]]
    _rehash(result)

    with pytest.raises(ValueError, match="embedded measurement schema"):
        identity.verify_links(result, ledger)


def test_router_outcome_list_is_a_validation_error() -> None:
    result, ledger = _fixture()
    ledger[0]["outcome"] = ["completed"]
    with pytest.raises(ValueError, match="router ledger outcome invalid"):
        identity.verify_links(result, ledger)


@pytest.mark.parametrize("result", [None, [], "correlated result", 1])
def test_nondictionary_root_is_a_validation_error(result: object) -> None:
    with pytest.raises(ValueError, match="correlated result fields invalid"):
        identity.verify_links(result, [])


@pytest.mark.parametrize("response_id", [None, "f" * 32, "invalid", "A" * 32])
def test_observed_response_headers_require_matching_identity(
    response_id: str | None,
) -> None:
    result, ledger = _fixture()
    result["request_links"][0]["response_request_id"] = response_id
    _rehash(result)

    with pytest.raises(ValueError, match="response request identity"):
        identity.verify_links(result, ledger)


def test_no_headers_allows_null_and_preserves_different_terminal_outcomes() -> None:
    result, ledger = _fixture()
    row = result["measurement"]["rows"][0]
    row.update(response_headers_ns=None, outcome="cancelled", http_status=None)
    result["request_links"][0]["response_request_id"] = None
    _rehash(result["measurement"])
    _rehash(result)

    report = identity.verify_links(result, ledger)

    assert report["status"] == "VERIFIED"
    assert report["links"][0]["client_outcome"] == "cancelled"
    assert report["links"][0]["router_outcome"] == "completed"
    assert report["measurement_comparison_valid"] is False


def test_response_identity_cannot_be_claimed_without_observed_headers() -> None:
    result, ledger = _fixture()
    result["measurement"]["rows"][0]["response_headers_ns"] = None
    _rehash(result["measurement"])
    _rehash(result)
    with pytest.raises(ValueError, match="no observed response headers"):
        identity.verify_links(result, ledger)


@pytest.mark.parametrize("target", ["wrapper", "measurement"])
def test_digest_tampering_is_rejected_at_each_layer(target: str) -> None:
    result, ledger = _fixture()
    if target == "wrapper":
        result["request_links"][2]["request_id"] = "f" * 32
    else:
        result["measurement"]["comparison_valid"] = True
        _rehash(result)  # An honest outer hash cannot repair a stale inner hash.
    with pytest.raises(ValueError, match="digest mismatch"):
        identity.verify_links(result, ledger)


@pytest.mark.parametrize("schema", SCHEMAS)
def test_legacy_measurement_cannot_be_misrepresented_as_correlated(schema: str) -> None:
    result, ledger = _fixture(schema)
    with pytest.raises(ValueError, match="legacy results have no links"):
        identity.verify_links(result["measurement"], ledger)


@pytest.mark.parametrize("target", ["row", "link"])
def test_bool_indexes_are_invalid_even_when_equal_to_integer_zero(target: str) -> None:
    result, ledger = _fixture()
    if target == "row":
        result["measurement"]["rows"][0]["index"] = False
        _rehash(result["measurement"])
    else:
        result["request_links"][0]["index"] = False
    _rehash(result)
    with pytest.raises(ValueError, match="index or request identity invalid"):
        identity.verify_links(result, ledger)


def _cli_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, missing_receipt: bool = False
) -> tuple[Path, bytes, list[dict]]:
    result, ledger = _fixture()
    if missing_receipt:
        ledger = ledger[1:]
    # Spaces and CRLF ensure file provenance uses exact bytes, not reserialized rows.
    raw_ledger = (
        b"\r\n".join(json.dumps(row, indent=None).encode() for row in reversed(ledger))
        + b"\r\n"
    )
    result_path = tmp_path / "client.json"
    ledger_path = tmp_path / "router.jsonl"
    output_path = tmp_path / "links.json"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    ledger_path.write_bytes(raw_ledger)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify-request-links",
            "--result",
            str(result_path),
            "--ledger",
            str(ledger_path),
            "--output",
            str(output_path),
        ],
    )
    return output_path, raw_ledger, list(reversed(ledger))


def test_cli_records_exact_ledger_bytes_and_refuses_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path, raw_ledger, rows = _cli_inputs(tmp_path, monkeypatch)
    identity.main()
    original_output = output_path.read_bytes()
    report = json.loads(original_output)
    assert report["status"] == "VERIFIED"
    assert report["ledger_file_sha256"] == (
        "sha256:" + hashlib.sha256(raw_ledger).hexdigest()
    )
    assert report["ledger_rows_sha256"] == _digest(rows)
    assert report["ledger_file_sha256"] != report["ledger_rows_sha256"]
    assert report["result_sha256"] == _digest(
        {key: value for key, value in report.items() if key != "result_sha256"}
    )

    with pytest.raises(FileExistsError):
        identity.main()
    assert output_path.read_bytes() == original_output


def test_cli_writes_inspectable_incomplete_report_and_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path, raw_ledger, _ = _cli_inputs(
        tmp_path, monkeypatch, missing_receipt=True
    )
    with pytest.raises(SystemExit) as exited:
        identity.main()
    assert exited.value.code == 2
    report = json.loads(output_path.read_bytes())
    assert report["status"] == "INCOMPLETE"
    assert report["correlation_valid"] is False
    assert report["unresolved_dispatches"] == 1
    assert report["ledger_file_sha256"] == (
        "sha256:" + hashlib.sha256(raw_ledger).hexdigest()
    )


def test_cli_preserves_ledger_integers_beyond_rfc8785_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path, _, rows = _cli_inputs(tmp_path, monkeypatch)
    rows[0].update(arrived_ns=2**63 - 1, first_byte_ms=0.000001)
    raw = b"\n".join(json.dumps(row).encode() for row in rows) + b"\n"
    (tmp_path / "router.jsonl").write_bytes(raw)
    identity.main()
    report = json.loads(output_path.read_bytes())
    assert report["status"] == "VERIFIED"
    assert report["ledger_rows_encoding"] == "PYTHON_JSON_SORTED_COMPACT_UTF8_V1"
    encoded = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    assert str(2**63 - 1).encode() in encoded
    assert (
        report["ledger_rows_sha256"] == "sha256:" + hashlib.sha256(encoded).hexdigest()
    )
    assert report["ledger_file_sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    reordered = [dict(reversed(list(row.items()))) for row in rows]
    assert identity._ledger_digest(reordered) == report["ledger_rows_sha256"]
    rows[0]["arrived_ns"] -= 1
    assert identity._ledger_digest(rows) != report["ledger_rows_sha256"]


@pytest.mark.parametrize("location", ["result_root", "result_nested", "ledger"])
@pytest.mark.parametrize("matching_value", [True, False])
def test_cli_rejects_duplicate_fields_even_with_matching_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    matching_value: bool,
) -> None:
    output_path, _, _ = _cli_inputs(tmp_path, monkeypatch)
    path = tmp_path / ("router.jsonl" if location == "ledger" else "client.json")
    key, value = (
        ("schema", identity.CORRELATED_SCHEMA)
        if location == "result_root"
        else ("policy", "cache_only")
    )
    original_field = f'"{key}": "{value}"'.encode()
    duplicate_value = value if matching_value else "ignored-if-last-field-wins"
    duplicate_field = f'"{key}": "{duplicate_value}", '.encode()
    raw = path.read_bytes()
    assert original_field in raw
    path.write_bytes(raw.replace(original_field, duplicate_field + original_field, 1))

    # Even the unequal first value would disappear under ordinary last-field-wins
    # parsing, leaving the original hashed measurement and wrapper unchanged.
    with pytest.raises(ValueError, match="duplicate JSON field"):
        identity.main()
    assert not output_path.exists()


@pytest.mark.parametrize("location", ["result", "ledger"])
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_cli_rejects_nonfinite_json_before_writing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    constant: str,
) -> None:
    output_path, _, _ = _cli_inputs(tmp_path, monkeypatch)
    path = tmp_path / ("client.json" if location == "result" else "router.jsonl")
    raw = path.read_bytes()
    if location == "result":
        original = b'"max_content_gap_ns": null'
        replacement = f'"max_content_gap_ns": {constant}'.encode()
    else:
        original = b"{"
        replacement = f'{{"untrusted_number": {constant}, '.encode()
    assert original in raw
    path.write_bytes(raw.replace(original, replacement, 1))

    with pytest.raises(ValueError, match="nonfinite JSON value"):
        identity.main()
    assert not output_path.exists()
