"""A bounded offline search controller over verified paired-comparison evidence.

Submission finalizes one candidate, even if its evidence is incomplete. Resume
appends candidates to a new snapshot; it does not repair or erase old attempts.
No serving process, cloud job or GPU experiment is launched here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_paired_comparison import _fields, _json, _load_inputs, compare
from inferdrome.vllm_paired_protocol import make_protocol
from inferdrome.vllm_request_identity import _read_json
from inferdrome.vllm_search_plan import (
    candidate_protocol,
    make_search_plan,
    validate_search_plan,
)

SCHEMA = "inferdrome.vllm-router-bounded-search.v1"
INPUT_SCHEMA = "inferdrome.vllm-router-search-inputs.v1"


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _prior_digest(value: object) -> None:
    if value is not None and (
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
    ):
        raise ValueError("prior snapshot digest is invalid")


def evaluate(
    search_plan: dict[str, Any],
    plans: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    *,
    previous_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recheck raw evidence in a frozen prefix; optionally verify its prior snapshot."""
    try:
        validate_search_plan(search_plan, plans)
        if not isinstance(observations, list):
            raise ValueError("search observations must be a list")
        previous_sha = None
        if previous_report is not None:
            if not isinstance(previous_report, dict):
                raise ValueError("previous report must be an object")
            count = previous_report.get("evaluated_candidates")
            if type(count) is not int or not 0 <= count < len(observations):
                raise ValueError("resume must append candidates to the previous prefix")
            parent = previous_report["previous_report_sha256"]
            _prior_digest(parent)
            regenerated = _evaluate(search_plan, plans, observations[:count], parent)
            if canonical_json_bytes(previous_report) != canonical_json_bytes(
                regenerated
            ):
                raise ValueError(
                    "previous snapshot differs from regenerated evidence prefix"
                )
            previous_sha = previous_report["result_sha256"]
        return _evaluate(search_plan, plans, observations, previous_sha)
    except (
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise ValueError("malformed bounded search input") from error


def _protocol(
    search_plan: dict[str, Any], plans: list[dict[str, Any]], candidate: dict[str, Any]
) -> dict[str, Any]:
    protocol = make_protocol(
        plans,
        candidate_parameters=candidate["parameters"],
        **search_plan["comparison_options"],
    )
    if protocol["protocol_sha256"] != candidate["protocol_sha256"]:
        raise ValueError("candidate protocol differs from search plan")
    return protocol


def _evaluate(
    search_plan: dict[str, Any],
    plans: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    previous_sha: str | None,
) -> dict[str, Any]:
    scheduled = search_plan["scheduled_candidate_count"]
    if len(observations) > scheduled:
        raise ValueError("submitted candidates exceed the frozen search budget")
    candidates = search_plan["candidates"]
    results: list[dict[str, Any]] = []
    seen_requests: set[str] = set()
    seen_results: set[str] = set()
    previous_end = 0
    selected = None
    for index, observation in enumerate(observations):
        if selected is not None:
            raise ValueError("evidence supplied after the first screened-in candidate")
        _fields(observation, {"candidate_id", "inputs"}, "candidate observation")
        candidate = candidates[index]
        if observation["candidate_id"] != candidate["candidate_id"]:
            raise ValueError(
                "candidate observations must follow the exact search prefix"
            )
        protocol = _protocol(search_plan, plans, candidate)
        comparison = compare(protocol, plans, observation["inputs"])
        search_reasons = []
        candidate_starts = []
        candidate_ends = []
        planned_trials = {trial["trial_id"]: trial for trial in protocol["trials"]}
        for item in observation["inputs"]:
            result = item["result"]
            if result["result_sha256"] in seen_results:
                raise ValueError("result reused across search candidates")
            seen_results.add(result["result_sha256"])
            request_ids = {link["request_id"] for link in result["request_links"]}
            if seen_requests & request_ids:
                raise ValueError("request identity reused across search candidates")
            seen_requests.update(request_ids)
            measurement = result["measurement"]
            start = int(measurement["started_unix_ns"])
            trial_plan = plans[planned_trials[item["trial_id"]]["block"] - 1]
            candidate_starts.append(start)
            candidate_ends.append(
                start
                + max(
                    trial_plan["duration_ns"],
                    *(row["terminal_ns"] for row in measurement["rows"]),
                )
            )
        if candidate_starts and min(candidate_starts) < previous_end:
            search_reasons.append("CROSS_CANDIDATE_REPORTED_WINDOWS_OVERLAP")
        previous_end = max([previous_end, *candidate_ends])
        search_status = "INELIGIBLE" if search_reasons else comparison["status"]
        summary = {
            "candidate_id": candidate["candidate_id"],
            "rank": candidate["rank"],
            "parameters": candidate["parameters"],
            "distance": candidate["distance"],
            "schedule_sha256": candidate["schedule_sha256"],
            "protocol_sha256": protocol["protocol_sha256"],
            "comparison_sha256": comparison["result_sha256"],
            "comparison_status": comparison["status"],
            "search_status": search_status,
            "search_ineligibility_reasons": search_reasons,
        }
        results.append({**summary, "comparison": comparison})
        if search_status == "REVERSAL_CANDIDATE":
            selected = summary

    count = len(observations)
    next_action = None
    if selected is not None:
        status = "CANDIDATE_FOUND"
        stop_reason = "FIRST_SCREENED_IN_CANDIDATE"
    elif count < scheduled:
        status = "AWAITING_EVIDENCE"
        stop_reason = "NEXT_CANDIDATE_REQUIRES_RAW_EVIDENCE"
        candidate = candidates[count]
        next_action = {
            "type": "COLLECT_CANDIDATE",
            "candidate_id": candidate["candidate_id"],
            "protocol": _protocol(search_plan, plans, candidate),
        }
    elif scheduled == len(candidates):
        status = "GRID_EXHAUSTED"
        stop_reason = "NO_SCREENED_IN_CANDIDATE_IN_SUBMITTED_GRID"
    else:
        status = "BUDGET_EXHAUSTED"
        stop_reason = "NO_FURTHER_COMPLETE_CANDIDATE_FITS_DECLARED_BUDGET"
    slots = search_plan["trial_slots_per_candidate"]
    report = {
        "schema": SCHEMA,
        "scope": "EXPLORATORY_SEARCH_ONLY",
        "status": status,
        "stop_reason": stop_reason,
        "evidence_class": search_plan["evidence_class"],
        "evidence_eligible": False,
        "search_plan_sha256": search_plan["search_plan_sha256"],
        "previous_report_sha256": previous_sha,
        "resume_scope": "IMMEDIATE_PREVIOUS_PREFIX_REGENERATED_ANCESTORS_NOT_TRAVERSED"
        if previous_sha is not None
        else "NO_PREVIOUS_SNAPSHOT_SUPPLIED",
        "evaluated_candidates": count,
        "trial_artifacts_supplied": sum(len(item["inputs"]) for item in observations),
        "budget": {
            "max_candidates": search_plan["max_candidates"],
            "max_trial_slots": search_plan["max_trial_slots"],
            "trial_slots_per_candidate": slots,
            "trial_slots_reserved": count * slots,
            "remaining_candidate_slots": search_plan["max_candidates"] - count,
            "remaining_trial_slots": search_plan["max_trial_slots"] - count * slots,
        },
        "coverage": {
            "grid_recipes": search_plan["grid_recipe_count"],
            "unique_candidates": len(candidates),
            "scheduled_candidates": scheduled,
            "unsubmitted_scheduled_candidates": scheduled - count,
            "unselected_grid_candidates": len(candidates) - scheduled,
            "ineligible_candidates": sum(
                row["search_status"] == "INELIGIBLE" for row in results
            ),
            "inconclusive_candidates": sum(
                row["search_status"] == "INCONCLUSIVE" for row in results
            ),
            "screened_in_candidates": sum(
                row["search_status"] == "REVERSAL_CANDIDATE" for row in results
            ),
            "earlier_unresolved_candidates": [
                row["candidate_id"]
                for row in results
                if row["search_status"] != "REVERSAL_CANDIDATE"
            ],
            "all_grid_candidates_evaluated": count == len(candidates),
        },
        "candidate_results": results,
        "selected_candidate": selected,
        "selection_scope": (
            "NEAREST_SCREENED_IN_AMONG_EVALUATED_SCHEDULES_UNDER_DECLARED_ORDER"
        ),
        "next_action": next_action,
        "independent_execution": "UNVERIFIED",
        "held_out_confirmation": "NOT_IMPLEMENTED",
        "search_multiplicity": "UNCONTROLLED",
        "chronology_scope": "REPORTED_CLIENT_CLOCKS_NOT_TRUSTED_CHRONOLOGY",
        "limitations": [
            "Nearest refers to the frozen lexicographic planned-arrival distance, "
            "not a universal workload distance.",
            "Inconclusive comparisons do not exclude smaller real reversals; "
            "ineligible comparisons leave evidence gaps.",
            "Exhausting the grid or budget does not prove that no reversal exists.",
            "Trial slots are reserved accounting units, "
            "not observed execution or GPU cost.",
            "This controller reads supplied artifacts; it does not launch trials "
            "or enforce an external runner budget.",
            "Hashes and reported clocks do not authenticate execution, independence, "
            "provenance, resets or preregistration.",
            "Search multiplicity is uncontrolled; a candidate is not a confirmed "
            "or globally minimal counterexample.",
            "Resume checks the immediate prior snapshot against supplied raw evidence; "
            "ancestors are not traversed.",
        ],
    }
    report["result_sha256"] = _digest(report)
    return report


def _load_search_inputs(
    path: Path, search_plan: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = _fields(
        _read_json(path.read_bytes()),
        {"schema", "search_plan_sha256", "plans", "candidates"},
        "search input manifest",
    )
    if (
        manifest["schema"] != INPUT_SCHEMA
        or manifest["search_plan_sha256"] != search_plan["search_plan_sha256"]
    ):
        raise ValueError("search input manifest plan mismatch")
    if not isinstance(manifest["plans"], list) or not isinstance(
        manifest["candidates"], list
    ):
        raise ValueError("search manifest plans and candidates must be lists")
    plans = [_json(path.parent, value) for value in manifest["plans"]]
    validate_search_plan(search_plan, plans)
    if len(manifest["candidates"]) > search_plan["scheduled_candidate_count"]:
        raise ValueError("search input manifest exceeds budget")
    observations = []
    for index, entry in enumerate(manifest["candidates"]):
        _fields(entry, {"candidate_id", "inputs"}, "candidate input manifest")
        candidate = search_plan["candidates"][index]
        if entry["candidate_id"] != candidate["candidate_id"]:
            raise ValueError("candidate input manifests must follow the search prefix")
        protocol = _protocol(search_plan, plans, candidate)
        if not isinstance(entry["inputs"], str) or not entry["inputs"]:
            raise ValueError("candidate input manifest path must be a nonempty string")
        candidate_plans, inputs = _load_inputs(path.parent / entry["inputs"], protocol)
        if canonical_json_bytes(candidate_plans) != canonical_json_bytes(plans):
            raise ValueError("candidate input plans differ from frozen search plans")
        observations.append({"candidate_id": entry["candidate_id"], "inputs": inputs})
    return plans, observations


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="inferdrome breakpoint search" if argv is not None else None,
        description="Prepare and resume a bounded offline timing search",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    candidate = sub.add_parser("candidate")
    candidate.add_argument("--search-plan", type=Path, required=True)
    candidate.add_argument("--plans", nargs="+", type=Path, required=True)
    candidate.add_argument("--candidate-id", required=True)
    candidate.add_argument("--output", type=Path, required=True)
    for name in ("report", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--search-plan", type=Path, required=True)
        command.add_argument("--inputs", type=Path, required=True)
        command.add_argument("--previous-report", type=Path)
        command.add_argument(
            "--report" if name == "verify" else "--output", type=Path, required=True
        )
    args = parser.parse_args(argv)
    if args.command == "prepare":
        config = _fields(
            _read_json(args.config.read_bytes()),
            {
                "plans",
                "comparison_options",
                "group_sizes",
                "retained_spacing_bps",
                "max_advances_ns",
                "max_candidates",
                "max_trial_slots",
            },
            "search configuration",
        )
        if not isinstance(config["plans"], list):
            raise ValueError("configuration plans must be a list")
        plans = [_json(args.config.parent, value) for value in config["plans"]]
        result = make_search_plan(
            plans, **{key: value for key, value in config.items() if key != "plans"}
        )
    else:
        search_plan = _read_json(args.search_plan.read_bytes())
        if not isinstance(search_plan, dict):
            raise ValueError("search plan must be an object")
        if args.command == "candidate":
            plans = [_read_json(path.read_bytes()) for path in args.plans]
            result = candidate_protocol(search_plan, plans, args.candidate_id)
        else:
            plans, observations = _load_search_inputs(args.inputs, search_plan)
            previous = (
                None
                if args.previous_report is None
                else _read_json(args.previous_report.read_bytes())
            )
            if args.previous_report is not None and not isinstance(previous, dict):
                raise ValueError("previous report must be an object")
            result = evaluate(
                search_plan, plans, observations, previous_report=previous
            )
            if args.command == "verify":
                if canonical_json_bytes(
                    _read_json(args.report.read_bytes())
                ) != canonical_json_bytes(result):
                    raise ValueError("search report differs from regenerated evidence")
                if result["status"] == "AWAITING_EVIDENCE":
                    raise SystemExit(2)
                return
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    if result.get("status") == "AWAITING_EVIDENCE":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
