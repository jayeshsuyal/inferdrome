"""Bounded offline reduction of a verified search candidate's timing support.

The reducer restores whole original compression groups, never deletes requests,
never runs trials, and never promotes exploratory screening to confirmation.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import _artifact_json_types
from inferdrome.vllm_bounded_search import (
    _digest,
    _load_search_inputs,
    _prior_digest,
    _protocol,
    evaluate,
)
from inferdrome.vllm_paired_comparison import _fields, _json, _load_inputs, compare
from inferdrome.vllm_reduced_protocol import (
    REDUCTION_SCOPE,
    _active_groups,
    group_universe,
    make_reduced_protocol,
)
from inferdrome.vllm_request_identity import _read_json

PLAN_SCHEMA = "inferdrome.vllm-router-reduction-plan.v1"
SCHEMA = "inferdrome.vllm-router-witness-reduction.v1"
SOURCE_SCHEMA = "inferdrome.vllm-router-reduction-source.v1"
INPUT_SCHEMA = "inferdrome.vllm-router-reduction-inputs.v1"
ALGORITHM = "ordered-complement-removal.v1"
_MALFORMED = (KeyError, TypeError, AttributeError, OverflowError, RecursionError)


def _verify_source(source: dict[str, Any]) -> dict[str, Any]:
    _fields(
        source,
        {"search_plan", "plans", "observations", "report", "previous_report"},
        "reduction source",
    )
    if source["previous_report"] is not None and not isinstance(
        source["previous_report"], dict
    ):
        raise ValueError("source previous report must be an object or null")
    expected = evaluate(
        source["search_plan"],
        source["plans"],
        source["observations"],
        previous_report=source["previous_report"],
    )
    if canonical_json_bytes(source["report"]) != canonical_json_bytes(expected):
        raise ValueError("source search report differs from regenerated raw evidence")
    if expected["status"] != "CANDIDATE_FOUND":
        raise ValueError("reduction requires a screened-in search candidate")
    return expected


def _make_plan(
    source: dict[str, Any],
    verified: dict[str, Any],
    max_comparisons: int,
    max_trial_slots: int,
) -> dict[str, Any]:
    if type(max_comparisons) is not int or not 1 <= max_comparisons <= 128:
        raise ValueError("max comparisons must be an integer in 1..128")
    if type(max_trial_slots) is not int or not 1 <= max_trial_slots <= 16_384:
        raise ValueError("max trial slots must be an integer in 1..16384")
    selected = verified["selected_candidate"]
    slots = 4 * len(source["plans"])
    result = {
        "schema": PLAN_SCHEMA,
        "scope": "EXPLORATORY_SEARCH_ONLY",
        "evidence_class": verified["evidence_class"],
        "source_search_plan_sha256": source["search_plan"]["search_plan_sha256"],
        "source_report_sha256": verified["result_sha256"],
        "source_candidate_id": selected["candidate_id"],
        "source_comparison_sha256": selected["comparison_sha256"],
        "source_protocol_sha256": selected["protocol_sha256"],
        "base_plan_sha256s": [plan["plan_sha256"] for plan in source["plans"]],
        "root_parameters": selected["parameters"],
        "comparison_options": source["search_plan"]["comparison_options"],
        "source_groups": group_universe(source["plans"], selected["parameters"]),
        "max_comparisons": max_comparisons,
        "max_trial_slots": max_trial_slots,
        "trial_slots_per_comparison": slots,
        "comparison_limit": min(max_comparisons, max_trial_slots // slots),
        "algorithm": ALGORITHM,
        "reduction_scope": REDUCTION_SCOPE,
    }
    result["reduction_plan_sha256"] = _digest(result)
    return result


def make_reduction_plan(
    source: dict[str, Any], *, max_comparisons: int, max_trial_slots: int
) -> dict[str, Any]:
    """Bind the reducer and its budget to a search hit rechecked from raw inputs."""
    try:
        return _make_plan(
            source, _verify_source(source), max_comparisons, max_trial_slots
        )
    except _MALFORMED as error:
        raise ValueError("malformed reduction source") from error


def _validate_plan(plan: dict[str, Any], source: dict[str, Any]) -> None:
    verified = _verify_source(source)
    _artifact_json_types(plan)
    expected = _make_plan(
        source, verified, plan["max_comparisons"], plan["max_trial_slots"]
    )
    if canonical_json_bytes(plan) != canonical_json_bytes(expected):
        raise ValueError("reduction plan differs from its verified source and recipe")


def _track(
    inputs: list[dict[str, Any]],
    protocol: dict[str, Any],
    plans: list[dict[str, Any]],
    seen_requests: set[str],
    seen_results: set[str],
    previous_end: int,
) -> tuple[int, bool]:
    """Account for all validated artifacts, including ineligible attempts."""
    trials = {trial["trial_id"]: trial for trial in protocol["trials"]}
    starts = []
    ends = []
    for item in inputs:
        result = item["result"]
        if result["result_sha256"] in seen_results:
            raise ValueError("result reused across source or reduction proposals")
        seen_results.add(result["result_sha256"])
        request_ids = {link["request_id"] for link in result["request_links"]}
        if seen_requests & request_ids:
            raise ValueError("request identity reused across source or proposals")
        seen_requests.update(request_ids)
        measurement = result["measurement"]
        start = int(measurement["started_unix_ns"])
        trial_plan = plans[trials[item["trial_id"]]["block"] - 1]
        starts.append(start)
        ends.append(
            start
            + max(
                trial_plan["duration_ns"],
                *(row["terminal_ns"] for row in measurement["rows"]),
            )
        )
    return max([previous_end, *ends]), bool(starts and min(starts) < previous_end)


def reduce(
    plan: dict[str, Any],
    source: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    previous_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay deterministic proposals, raw comparisons and an optional prior prefix."""
    try:
        _validate_plan(plan, source)
        if not isinstance(observations, list):
            raise ValueError("reduction observations must be a list")
        previous_sha = None
        if previous_report is not None:
            if not isinstance(previous_report, dict):
                raise ValueError("previous reduction report must be an object")
            count = previous_report.get("completed_comparisons")
            if type(count) is not int or not 0 <= count < len(observations):
                raise ValueError("resume must append proposals to the previous prefix")
            parent = previous_report["previous_report_sha256"]
            _prior_digest(parent)
            expected = _reduce(plan, source, observations[:count], parent)
            if canonical_json_bytes(previous_report) != canonical_json_bytes(expected):
                raise ValueError("previous snapshot differs from regenerated prefix")
            previous_sha = previous_report["result_sha256"]
        return _reduce(plan, source, observations, previous_sha)
    except _MALFORMED as error:
        raise ValueError("malformed witness reduction input") from error


def _reduce(
    plan: dict[str, Any],
    source: dict[str, Any],
    observations: list[dict[str, Any]],
    previous_sha: str | None,
    load_next: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    if len(observations) > plan["comparison_limit"]:
        raise ValueError("submitted proposals exceed the frozen reduction budget")
    plans = source["plans"]
    seen_requests: set[str] = set()
    seen_results: set[str] = set()
    previous_end = 0
    for candidate, observation in zip(
        source["search_plan"]["candidates"], source["observations"], strict=False
    ):
        protocol = _protocol(source["search_plan"], plans, candidate)
        previous_end, _ = _track(
            observation["inputs"],
            protocol,
            plans,
            seen_requests,
            seen_results,
            previous_end,
        )
    incumbent = {
        "retained_groups": list(plan["source_groups"]),
        "comparison_sha256": plan["source_comparison_sha256"],
        "protocol_sha256": plan["source_protocol_sha256"],
        "source": "SEARCH",
        "proposal_id": None,
    }
    active = [set(_active_groups(base, plan["root_parameters"])) for base in plans]
    attempts: list[dict[str, Any]] = []
    structural_skips: list[dict[str, Any]] = []
    tested: set[tuple[str, ...]] = set()
    cached_skips = 0
    granularity = 2
    cursor = 0
    next_action = None
    while True:
        groups = incumbent["retained_groups"]
        size = len(groups)
        if size <= 1:
            status = "REDUCTION_COMPLETE"
            break
        granularity = min(granularity, size)
        if cursor == granularity:
            if granularity == size:
                status = "REDUCTION_COMPLETE"
                break
            granularity = min(size, granularity * 2)
            cursor = 0
            continue
        removed = groups[
            size * cursor // granularity : size * (cursor + 1) // granularity
        ]
        removed_set = set(removed)
        retained = [group for group in groups if group not in removed_set]
        cursor += 1
        proposal = {
            "retained_groups": retained,
            "removed_groups": removed,
            "parent_retained_groups": list(groups),
            "granularity": granularity,
        }
        empty_blocks = [
            index + 1
            for index, local in enumerate(active)
            if not local.intersection(retained)
        ]
        if empty_blocks:
            structural_skips.append(
                {
                    **proposal,
                    "reason": "NO_MOVED_OFFERS_IN_BLOCK",
                    "blocks": empty_blocks,
                }
            )
            continue
        key = tuple(retained)
        if key in tested:
            cached_skips += 1
            continue
        if len(attempts) == plan["comparison_limit"]:
            status = "BUDGET_EXHAUSTED"
            break
        protocol = make_reduced_protocol(
            plans,
            source_parameters=plan["root_parameters"],
            retained_groups=retained,
            **plan["comparison_options"],
        )
        proposal_id = f"r{len(attempts) + 1:03d}"
        if len(attempts) == len(observations) and load_next is not None:
            loaded = load_next(proposal_id, protocol)
            if loaded is not None:
                observations.append(loaded)
        if len(attempts) == len(observations):
            next_action = {
                "type": "COLLECT_REDUCTION",
                "proposal_id": proposal_id,
                **proposal,
                "protocol": protocol,
            }
            status = "AWAITING_EVIDENCE"
            break
        observation = observations[len(attempts)]
        _fields(observation, {"proposal_id", "inputs"}, "reduction observation")
        if observation["proposal_id"] != proposal_id:
            raise ValueError(
                "proposal observations must follow the deterministic prefix"
            )
        comparison = compare(protocol, plans, observation["inputs"])
        previous_end, overlaps = _track(
            observation["inputs"],
            protocol,
            plans,
            seen_requests,
            seen_results,
            previous_end,
        )
        reasons = (
            ["CROSS_PROPOSAL_OR_SOURCE_REPORTED_WINDOWS_OVERLAP"] if overlaps else []
        )
        attempt_status = "INELIGIBLE" if reasons else comparison["status"]
        accepted = attempt_status == "REVERSAL_CANDIDATE"
        attempts.append(
            {
                "proposal_id": proposal_id,
                **proposal,
                "protocol_sha256": protocol["protocol_sha256"],
                "comparison": comparison,
                "comparison_status": comparison["status"],
                "attempt_status": attempt_status,
                "accepted": accepted,
                "additional_ineligibility_reasons": reasons,
            }
        )
        tested.add(key)
        if accepted:
            incumbent = {
                "retained_groups": retained,
                "comparison_sha256": comparison["result_sha256"],
                "protocol_sha256": protocol["protocol_sha256"],
                "source": "REDUCTION",
                "proposal_id": proposal_id,
            }
            granularity = max(2, granularity - 1)
            cursor = 0
    if len(attempts) != len(observations):
        raise ValueError("evidence supplied after reduction completed")
    count = len(attempts)
    slots = plan["trial_slots_per_comparison"]
    report = {
        "schema": SCHEMA,
        "scope": "EXPLORATORY_SEARCH_ONLY",
        "status": status,
        "stop_reason": {
            "REDUCTION_COMPLETE": (
                "NO_FURTHER_SCREENED_IN_REMOVAL_UNDER_DECLARED_ALGORITHM"
            ),
            "BUDGET_EXHAUSTED": "NO_FURTHER_COMPLETE_COMPARISON_FITS_DECLARED_BUDGET",
            "AWAITING_EVIDENCE": "NEXT_REDUCTION_REQUIRES_RAW_EVIDENCE",
        }[status],
        "evidence_class": plan["evidence_class"],
        "evidence_eligible": False,
        "reduction_plan_sha256": plan["reduction_plan_sha256"],
        "source_report_sha256": plan["source_report_sha256"],
        "algorithm": ALGORITHM,
        "reduction_scope": REDUCTION_SCOPE,
        "previous_report_sha256": previous_sha,
        "resume_scope": "IMMEDIATE_PREVIOUS_PREFIX_REGENERATED_ANCESTORS_NOT_TRAVERSED"
        if previous_sha is not None
        else "NO_PREVIOUS_SNAPSHOT_SUPPLIED",
        "completed_comparisons": count,
        "trial_artifacts_supplied": sum(len(item["inputs"]) for item in observations),
        "attempts": attempts,
        "incumbent": incumbent,
        "next_action": next_action,
        "budget": {
            "max_comparisons": plan["max_comparisons"],
            "max_trial_slots": plan["max_trial_slots"],
            "trial_slots_per_comparison": slots,
            "trial_slots_reserved": count * slots,
            "remaining_comparison_slots": plan["comparison_limit"] - count,
            "remaining_trial_slots": plan["max_trial_slots"] - count * slots,
        },
        "structural_skips": structural_skips,
        "cached_skips": cached_skips,
        "coverage": {
            "source_groups": len(plan["source_groups"]),
            "retained_groups": len(incumbent["retained_groups"]),
            "accepted_reductions": sum(row["accepted"] for row in attempts),
            "ineligible_comparisons": sum(
                row["attempt_status"] == "INELIGIBLE" for row in attempts
            ),
            "inconclusive_comparisons": sum(
                row["attempt_status"] == "INCONCLUSIVE" for row in attempts
            ),
        },
        "independent_execution": "UNVERIFIED",
        "held_out_confirmation": "NOT_IMPLEMENTED",
        "search_multiplicity": "UNCONTROLLED",
        "chronology_scope": "REPORTED_CLIENT_CLOCKS_NOT_TRUSTED_CHRONOLOGY",
        "limitations": [
            "Only timing support is reduced: original group membership, every offer, "
            "prompt, token budget, epoch, SLO and measurement denominator stay fixed.",
            "Restoring arrivals changes the runtime history; cache and queue state "
            "equivalence and setup-history minimization are not established.",
            "Every accepted mask needs a new complete paired comparison. All supplied "
            "source and proposal identities must be distinct, "
            "including failed attempts.",
            "Inconclusive or ineligible attempts are not evidence against a reversal. "
            "Previously tested masks are not retried by this algorithm.",
            "Completion only exhausts this ordered removal procedure; it proves "
            "neither global nor statistical minimality, nor absence of a reversal.",
            "The shared mask names original epoch-local group ordinals; group sizes "
            "and moved offers may differ across frozen seed blocks.",
            "Trial slots are reserved accounting units, not observed execution or GPU "
            "cost. This controller does not launch trials or enforce runner budgets.",
            "Hashes and reported clocks do not authenticate execution, independence, "
            "provenance, resets or preregistration.",
            "Adaptive search multiplicity remains uncontrolled; no held-out "
            "confirmation is implemented.",
            "Resume regenerates the immediate previous prefix; ancestor files "
            "are not traversed.",
        ],
    }
    report["result_sha256"] = _digest(report)
    return report


def _load_source(path: Path) -> dict[str, Any]:
    manifest = _fields(
        _read_json(path.read_bytes()),
        {"schema", "search_plan", "inputs", "report", "previous_report"},
        "reduction source manifest",
    )
    if manifest["schema"] != SOURCE_SCHEMA:
        raise ValueError("reduction source manifest schema mismatch")
    search_plan = _json(path.parent, manifest["search_plan"])
    if not isinstance(manifest["inputs"], str) or not manifest["inputs"]:
        raise ValueError("source inputs path must be a nonempty string")
    plans, observations = _load_search_inputs(
        path.parent / manifest["inputs"], search_plan
    )
    return {
        "search_plan": search_plan,
        "plans": plans,
        "observations": observations,
        "report": _json(path.parent, manifest["report"]),
        "previous_report": None
        if manifest["previous_report"] is None
        else _json(path.parent, manifest["previous_report"]),
    }


def _load_reduction_inputs(
    path: Path, plan: dict[str, Any], source: dict[str, Any]
) -> list[dict[str, Any]]:
    manifest = _fields(
        _read_json(path.read_bytes()),
        {"schema", "reduction_plan_sha256", "proposals"},
        "reduction input manifest",
    )
    if (
        manifest["schema"] != INPUT_SCHEMA
        or manifest["reduction_plan_sha256"] != plan["reduction_plan_sha256"]
    ):
        raise ValueError("reduction input manifest plan mismatch")
    if not isinstance(manifest["proposals"], list):
        raise ValueError("reduction proposals must be a list")
    _validate_plan(plan, source)
    if len(manifest["proposals"]) > plan["comparison_limit"]:
        raise ValueError("reduction input manifest exceeds budget")
    observations: list[dict[str, Any]] = []

    def load_next(proposal_id: str, protocol: dict[str, Any]) -> dict[str, Any] | None:
        if len(observations) == len(manifest["proposals"]):
            return None
        entry = manifest["proposals"][len(observations)]
        _fields(entry, {"proposal_id", "inputs"}, "proposal input manifest")
        if entry["proposal_id"] != proposal_id:
            raise ValueError("proposal manifests must follow the deterministic prefix")
        if not isinstance(entry["inputs"], str) or not entry["inputs"]:
            raise ValueError("proposal inputs path must be a nonempty string")
        plans, inputs = _load_inputs(path.parent / entry["inputs"], protocol)
        if canonical_json_bytes(plans) != canonical_json_bytes(source["plans"]):
            raise ValueError("proposal plans differ from frozen source plans")
        return {"proposal_id": entry["proposal_id"], "inputs": inputs}

    # Load adaptive protocols in one replay, without rechecking each earlier
    # comparison once per later manifest entry. No saved report is trusted.
    _reduce(plan, source, observations, None, load_next)
    if len(observations) != len(manifest["proposals"]):
        raise ValueError("proposal manifests supplied after reduction completed")
    return observations


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reduce a verified timing search candidate"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--max-comparisons", type=int, required=True)
    prepare.add_argument("--max-trial-slots", type=int, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    for name in ("report", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--inputs", type=Path, required=True)
        command.add_argument("--previous-report", type=Path)
        command.add_argument(
            "--report" if name == "verify" else "--output", type=Path, required=True
        )
    args = parser.parse_args()
    source = _load_source(args.source)
    if args.command == "prepare":
        result = make_reduction_plan(
            source,
            max_comparisons=args.max_comparisons,
            max_trial_slots=args.max_trial_slots,
        )
    else:
        plan = _read_json(args.plan.read_bytes())
        observations = _load_reduction_inputs(args.inputs, plan, source)
        previous = (
            None
            if args.previous_report is None
            else _read_json(args.previous_report.read_bytes())
        )
        if args.previous_report is not None and not isinstance(previous, dict):
            raise ValueError("previous reduction report must be an object")
        result = reduce(plan, source, observations, previous_report=previous)
        if args.command == "verify":
            if canonical_json_bytes(
                _read_json(args.report.read_bytes())
            ) != canonical_json_bytes(result):
                raise ValueError(
                    "reduction report differs from regenerated raw evidence"
                )
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
