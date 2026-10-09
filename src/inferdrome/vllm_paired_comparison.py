"""Offline exploratory paired comparisons over fully rechecked timed trials.

Artifact consistency is verified. Execution declarations, reset state, independent
blocks, source authorship and GPU provenance are not authenticated by this tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from fractions import Fraction
from pathlib import Path
from typing import Any

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_paired_protocol import (
    make_protocol,
    paired_statistics,
    validate_protocol,
)
from inferdrome.vllm_request_identity import _read_json, verify_links
from inferdrome.vllm_timed_result import verify_timed_result

SCHEMA = "inferdrome.vllm-router-paired-comparison.v1"
INPUT_SCHEMA = "inferdrome.vllm-router-paired-inputs.v1"
_INPUT_FIELDS = {
    "trial_id",
    "timing",
    "result",
    "ledger_rows",
    "token_certificate",
    "execution",
}
_EXECUTION_FIELDS = {
    "source_revision",
    "environment_sha256",
    "reset_procedure_sha256",
    "reset_completed",
}


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _fields(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} fields invalid")
    return value


def compare(
    protocol: dict[str, Any], plans: list[dict[str, Any]], inputs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Recheck all supplied trials; never remove failing blocks to obtain a signal."""
    try:
        return _compare(protocol, plans, inputs)
    except (
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise ValueError("malformed paired comparison input") from error


def _compare(
    protocol: dict[str, Any], plans: list[dict[str, Any]], inputs: list[dict[str, Any]]
) -> dict[str, Any]:
    validate_protocol(protocol, plans)
    if not isinstance(inputs, list):
        raise ValueError("trial inputs must be a list")
    planned = {trial["trial_id"]: trial for trial in protocol["trials"]}
    seen: set[str] = set()
    seen_results: set[str] = set()
    seen_requests: set[str] = set()
    previous_sequence = 0
    previous_end = 0
    reasons: list[dict[str, str]] = []
    trials: list[dict[str, Any]] = []
    counts: dict[tuple[int, str, str], int] = {}

    for item in inputs:
        _fields(item, _INPUT_FIELDS, "trial input")
        trial_id = item["trial_id"]
        if not isinstance(trial_id, str) or trial_id not in planned or trial_id in seen:
            raise ValueError("duplicate or unplanned trial")
        expected = planned[trial_id]
        if expected["sequence"] <= previous_sequence:
            raise ValueError("trial inputs differ from the frozen execution order")
        previous_sequence = expected["sequence"]
        seen.add(trial_id)
        plan = plans[expected["block"] - 1]
        timing = item["timing"]
        if (
            not isinstance(timing, dict)
            or timing.get("timing_sha256") != expected["timing_sha256"]
        ):
            raise ValueError("trial timing differs from protocol")
        execution = _fields(
            item["execution"], _EXECUTION_FIELDS, "execution declaration"
        )
        for field in _EXECUTION_FIELDS - {"reset_completed"}:
            if execution[field] != protocol[field]:
                raise ValueError("execution declaration differs from protocol")
        if type(execution["reset_completed"]) is not bool:
            raise ValueError("reset_completed must be boolean")
        result = item["result"]
        certificate = item["token_certificate"]
        timing_check = verify_timed_result(
            plan, timing, result, token_certificate=certificate
        )
        if not isinstance(item["ledger_rows"], list):
            raise ValueError("router ledger must be a list")
        link_check = verify_links(result, item["ledger_rows"])
        measurement = result["measurement"]
        if (
            measurement["policy"] != expected["policy"]
            or measurement["model"] != protocol["model"]
        ):
            raise ValueError("trial policy or model differs from protocol")
        if result["result_sha256"] in seen_results:
            raise ValueError("reused trial result")
        seen_results.add(result["result_sha256"])
        request_ids = {link["request_id"] for link in result["request_links"]}
        if seen_requests & request_ids:
            raise ValueError("request identity reused across trials")
        seen_requests.update(request_ids)
        started = measurement["started_unix_ns"]
        if (
            not isinstance(started, str)
            or re.fullmatch(r"[1-9][0-9]{0,19}", started) is None
        ):
            raise ValueError("trial start must be a positive decimal nanosecond string")
        start_ns = int(started)
        rows = measurement["rows"]
        trial_reasons: list[str] = []
        if start_ns < previous_end:
            trial_reasons.append("REPORTED_TRIAL_WINDOWS_OVERLAP_OR_REORDERED")
        previous_end = max(
            previous_end,
            start_ns + max(plan["duration_ns"], *(row["terminal_ns"] for row in rows)),
        )
        if not execution["reset_completed"]:
            trial_reasons.append("RESET_NOT_DECLARED_COMPLETE")
        if not timing_check["measurement_comparison_valid"]:
            trial_reasons.append("MEASUREMENT_NOT_COMPARISON_VALID")
        if (
            link_check["matched"] != len(rows)
            or link_check["not_dispatched"]
            or not link_check["correlation_valid"]
        ):
            trial_reasons.append("INCOMPLETE_ROUTER_RECEIPTS")
        # Rejection from capacity/queue pressure is measured work. Other failures
        # do not support a clean routing comparison in this narrow search oracle.
        clean_outcomes = all(
            (
                link["client_outcome"] == "completed"
                and link["router_outcome"] == "completed"
            )
            or (
                link["client_outcome"] == "rejected"
                and link["router_outcome"]
                in {"rejected_capacity", "rejected_queue_timeout"}
            )
            for link in link_check["links"]
        )
        if not clean_outcomes:
            trial_reasons.append("OPERATIONAL_ERROR_OR_OUTCOME_DISAGREEMENT")
        client_timing = measurement["summary"]["client_timing"]
        for metric, limit, reason in (
            (
                "scheduling_lag",
                "max_scheduling_lag_p95_ns",
                "CLIENT_SCHEDULING_LAG_EXCEEDS_LIMIT",
            ),
            (
                "client_queue_delay",
                "max_client_queue_p95_ns",
                "CLIENT_QUEUE_DELAY_EXCEEDS_LIMIT",
            ),
        ):
            observed = client_timing[metric]
            if (
                observed["count"] != len(rows)
                or observed["p95_ns"] is None
                or observed["p95_ns"] > protocol[limit]
            ):
                trial_reasons.append(reason)
        all_offered = measurement["summary"]["all_offered"]
        counts[(expected["block"], expected["condition"], expected["policy"])] = (
            all_offered["slo_good"]
        )
        reasons.extend(
            {"trial_id": trial_id, "reason": reason} for reason in trial_reasons
        )
        trials.append(
            {
                **expected,
                "slo_good": all_offered["slo_good"],
                "offered": all_offered["offered"],
                "goodput_denominator_ns": plan["duration_ns"],
                "slo_goodput_rps": all_offered["slo_goodput_rps"],
                "client_timing": client_timing,
                "measurement_comparison_valid": timing_check[
                    "measurement_comparison_valid"
                ],
                "eligible_for_search": not trial_reasons,
                "sources": {
                    "correlated_result_sha256": result["result_sha256"],
                    "measurement_sha256": measurement["result_sha256"],
                    "token_certificate_sha256": measurement["token_certificate_sha256"],
                    "timing_check_sha256": timing_check["result_sha256"],
                    "request_link_check_sha256": link_check["result_sha256"],
                    "ledger_rows_sha256": link_check["ledger_rows_sha256"],
                    "ledger_rows_encoding": link_check["ledger_rows_encoding"],
                    "execution_declaration_sha256": _digest(execution),
                },
            }
        )

    missing = [
        trial["trial_id"]
        for trial in protocol["trials"]
        if trial["trial_id"] not in seen
    ]
    reasons.extend(
        {"trial_id": trial_id, "reason": "MISSING_TRIAL"} for trial_id in missing
    )
    blocks: list[dict[str, Any]] = []
    deltas: dict[str, list[Fraction]] = {"baseline": [], "candidate": []}
    for block, plan in enumerate(plans, 1):
        cells: dict[str, Any] = {}
        for condition in deltas:
            a = counts.get((block, condition, protocol["policy_a"]))
            b = counts.get((block, condition, protocol["policy_b"]))
            if a is None or b is None:
                cells[condition] = None
                continue
            delta = Fraction((a - b) * 1_000_000_000, plan["duration_ns"])
            deltas[condition].append(delta)
            cells[condition] = {
                "policy_a_slo_good": a,
                "policy_b_slo_good": b,
                "a_minus_b_slo_good": a - b,
                "a_minus_b_goodput_rps": float(delta),
            }
        blocks.append(
            {
                "block": block,
                "seed": plan["seed"],
                "goodput_denominator_ns": plan["duration_ns"],
                **cells,
            }
        )
    statistics = (
        None
        if reasons
        else paired_statistics(
            deltas["baseline"], deltas["candidate"], protocol["minimum_effect_microrps"]
        )
    )
    status = (
        "INELIGIBLE"
        if reasons
        else (
            "REVERSAL_CANDIDATE"
            if statistics is not None and statistics["reversal_signal"]
            else "INCONCLUSIVE"
        )
    )
    report = {
        "schema": SCHEMA,
        "scope": "EXPLORATORY_SEARCH_ONLY",
        "status": status,
        "evidence_class": protocol["evidence_class"],
        "evidence_eligible": False,
        "independent_execution": "UNVERIFIED",
        "held_out_confirmation": "NOT_IMPLEMENTED",
        "protocol_sha256": protocol["protocol_sha256"],
        "declared_source_revision": protocol["source_revision"],
        "declared_environment_sha256": protocol["environment_sha256"],
        "declared_reset_procedure_sha256": protocol["reset_procedure_sha256"],
        "execution_declarations_scope": "OPERATOR_SUPPLIED_NOT_AUTHENTICATED",
        "chronology_scope": "REPORTED_CLIENT_CLOCKS_NOT_TRUSTED_CHRONOLOGY",
        "planned_trials": len(planned),
        "supplied_trials": len(inputs),
        "missing_trials": missing,
        "ineligibility_reasons": reasons,
        "trials": trials,
        "blocks": blocks,
        "statistics": statistics,
        "limitations": [
            "A block is one replicate; requests within a block "
            "are not independent replicates.",
            "Planned timing is not observed router arrival timing; "
            "p95 limits do not bound every request.",
            "Hashes verify consistency, not GPU execution, source authorship, "
            "reset state or independence.",
            "The protocol hash does not prove it was fixed before observing results.",
            "The directional noise screen is exploratory; "
            "adaptive search multiplicity is uncontrolled.",
            "No confidence interval, causal attribution, held-out confirmation "
            "or customer acceptance is established.",
            "INCONCLUSIVE does not establish equivalence or absence of a reversal.",
        ],
    }
    report["result_sha256"] = _digest(report)
    return report


def _path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("artifact path must be a nonempty string")
    return root / value


def _json(root: Path, value: object) -> Any:
    return _read_json(_path(root, value).read_bytes())


def _load_inputs(
    path: Path, protocol: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = _fields(
        _read_json(path.read_bytes()),
        {"schema", "protocol_sha256", "plans", "trials"},
        "input manifest",
    )
    if (
        manifest["schema"] != INPUT_SCHEMA
        or manifest["protocol_sha256"] != protocol["protocol_sha256"]
    ):
        raise ValueError("input manifest protocol mismatch")
    if not isinstance(manifest["plans"], list) or not isinstance(
        manifest["trials"], list
    ):
        raise ValueError("input manifest plans and trials must be lists")
    plans = [_json(path.parent, value) for value in manifest["plans"]]
    inputs = []
    for item in manifest["trials"]:
        _fields(
            item,
            {
                "trial_id",
                "timing",
                "result",
                "ledger",
                "token_certificate",
                "execution",
            },
            "input manifest trial",
        )
        raw_ledger = _path(path.parent, item["ledger"]).read_bytes()
        inputs.append(
            {
                "trial_id": item["trial_id"],
                "timing": _json(path.parent, item["timing"]),
                "result": _json(path.parent, item["result"]),
                "ledger_rows": [_read_json(line) for line in raw_ledger.splitlines()],
                "token_certificate": None
                if item["token_certificate"] is None
                else _json(path.parent, item["token_certificate"]),
                "execution": item["execution"],
            }
        )
    return plans, inputs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare and inspect exploratory paired routing comparisons offline"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    for name in ("compare", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--protocol", type=Path, required=True)
        command.add_argument("--inputs", type=Path, required=True)
        command.add_argument(
            "--output" if name == "compare" else "--report", type=Path, required=True
        )
    args = parser.parse_args()
    if args.command == "prepare":
        config = _read_json(args.config.read_bytes())
        _fields(
            config,
            {
                "plans",
                "policy_a",
                "policy_b",
                "candidate_parameters",
                "order_seed",
                "minimum_effect_microrps",
                "max_scheduling_lag_p95_ns",
                "max_client_queue_p95_ns",
                "model",
                "source_revision",
                "environment_sha256",
                "reset_procedure_sha256",
            },
            "protocol configuration",
        )
        if not isinstance(config["plans"], list):
            raise ValueError("configuration plans must be a list")
        plans = [_json(args.config.parent, value) for value in config["plans"]]
        result = make_protocol(
            plans, **{key: value for key, value in config.items() if key != "plans"}
        )
    else:
        protocol = _read_json(args.protocol.read_bytes())
        if not isinstance(protocol, dict):
            raise ValueError("protocol must be an object")
        plans, inputs = _load_inputs(args.inputs, protocol)
        result = compare(protocol, plans, inputs)
        if args.command == "verify":
            if canonical_json_bytes(
                _read_json(args.report.read_bytes())
            ) != canonical_json_bytes(result):
                raise ValueError(
                    "paired comparison report differs from regenerated report"
                )
            if result["status"] == "INELIGIBLE":
                raise SystemExit(2)
            return
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    if result.get("status") == "INELIGIBLE":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
