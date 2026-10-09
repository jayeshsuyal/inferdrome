"""One frozen held-out comparison of a rechecked terminal timing incumbent.

The wrapper verifies artifact consistency and applies fixed-batch criteria.
Independent execution, prior registration and unseen outcomes remain unverified.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import _artifact_json_types
from inferdrome.vllm_bounded_search import _digest, _protocol
from inferdrome.vllm_paired_comparison import _fields, _json, _load_inputs, compare
from inferdrome.vllm_paired_protocol import _common_plan
from inferdrome.vllm_reduced_protocol import make_reduced_protocol, reduced_timings
from inferdrome.vllm_request_identity import _read_json
from inferdrome.vllm_witness_reducer import (
    _load_reduction_inputs,
    _track,
    reduce,
)
from inferdrome.vllm_witness_reducer import (
    _load_source as _load_search_source,
)

PLAN_SCHEMA = "inferdrome.vllm-router-confirmation-plan.v1"
SCHEMA = "inferdrome.vllm-router-heldout-confirmation.v1"
SOURCE_SCHEMA = "inferdrome.vllm-router-confirmation-source.v1"
SCOPE = "FIXED_BATCH_HELD_OUT_CONFIRMATION"
DECISION_RULE = "EXACT_ONE_SIDED_BINOMIAL_MARGIN_EXCEEDANCE_TWO_DIRECTIONS_V1"
_MALFORMED = (KeyError, TypeError, AttributeError, OverflowError, RecursionError)


def _verify_source(source: dict[str, Any]) -> dict[str, Any]:
    _fields(
        source,
        {
            "search_source",
            "reduction_plan",
            "observations",
            "report",
            "previous_report",
        },
        "confirmation source",
    )
    if source["previous_report"] is not None and not isinstance(
        source["previous_report"], dict
    ):
        raise ValueError("source previous reduction report must be an object or null")
    expected = reduce(
        source["reduction_plan"],
        source["search_source"],
        source["observations"],
        previous_report=source["previous_report"],
    )
    if canonical_json_bytes(source["report"]) != canonical_json_bytes(expected):
        raise ValueError(
            "source reduction report differs from regenerated raw evidence"
        )
    if expected["status"] not in {"REDUCTION_COMPLETE", "BUDGET_EXHAUSTED"}:
        raise ValueError("confirmation requires a terminal reduction incumbent")
    return expected


def _make_plan(
    source: dict[str, Any],
    verified: dict[str, Any],
    plans: list[dict[str, Any]],
    order_seed: int,
) -> dict[str, Any]:
    if not isinstance(plans, list) or not 8 <= len(plans) <= 32 or len(plans) % 4:
        raise ValueError("confirmation requires eight to thirty-two blocks in fours")
    discovery = source["search_source"]["plans"]
    reduction_plan = source["reduction_plan"]
    options = {**reduction_plan["comparison_options"], "order_seed": order_seed}
    # The unchanged paired validator validates each plan's deterministic
    # recipe and exact scalar types before its hashes or seed are trusted here.
    protocol = make_reduced_protocol(
        plans,
        source_parameters=reduction_plan["root_parameters"],
        retained_groups=verified["incumbent"]["retained_groups"],
        **options,
    )
    common = _common_plan(discovery[0])
    if any(_common_plan(plan) != common for plan in plans):
        raise ValueError("held-out workload contract differs from discovery")
    for field in ("seed", "trace_sha256", "plan_sha256"):
        held_out = [plan[field] for plan in plans]
        old = {plan[field] for plan in discovery}
        if len(set(held_out)) != len(held_out) or old.intersection(held_out):
            raise ValueError(f"held-out {field} identities must be distinct and fresh")
    result = {
        "schema": PLAN_SCHEMA,
        "scope": SCOPE,
        "evidence_class": verified["evidence_class"],
        "decision_rule": DECISION_RULE,
        "source_reduction_plan_sha256": reduction_plan["reduction_plan_sha256"],
        "source_reduction_report_sha256": verified["result_sha256"],
        "source_search_report_sha256": verified["source_report_sha256"],
        "source_reduction_status": verified["status"],
        "source_incumbent": dict(verified["incumbent"]),
        "discovery_seeds": [plan["seed"] for plan in discovery],
        "discovery_trace_sha256s": [plan["trace_sha256"] for plan in discovery],
        "discovery_base_plan_sha256s": [plan["plan_sha256"] for plan in discovery],
        "held_out_seeds": [plan["seed"] for plan in plans],
        "held_out_trace_sha256s": [plan["trace_sha256"] for plan in plans],
        "held_out_base_plan_sha256s": [plan["plan_sha256"] for plan in plans],
        "comparison_count": 1,
        "planned_blocks": len(plans),
        "planned_trials": len(protocol["trials"]),
        "protocol": protocol,
    }
    result["confirmation_plan_sha256"] = _digest(result)
    return result


def make_confirmation_plan(
    source: dict[str, Any], plans: list[dict[str, Any]], *, order_seed: int
) -> dict[str, Any]:
    """Freeze one incumbent, its unchanged criteria, and fresh workload plans."""
    try:
        return _make_plan(source, _verify_source(source), plans, order_seed)
    except _MALFORMED as error:
        raise ValueError("malformed confirmation plan input") from error


def _validate_plan(
    plan: dict[str, Any], source: dict[str, Any], plans: list[dict[str, Any]]
) -> dict[str, Any]:
    verified = _verify_source(source)
    _artifact_json_types(plan)
    expected = _make_plan(source, verified, plans, plan["protocol"]["order_seed"])
    if canonical_json_bytes(plan) != canonical_json_bytes(expected):
        raise ValueError("confirmation plan differs from its source and fixed recipe")
    return verified


def validate_confirmation_plan(
    plan: dict[str, Any], source: dict[str, Any], plans: list[dict[str, Any]]
) -> None:
    """Regenerate the complete plan, including the terminal incumbent and trials."""
    try:
        _validate_plan(plan, source, plans)
    except _MALFORMED as error:
        raise ValueError("malformed confirmation plan") from error


def _track_source(
    source: dict[str, Any],
    verified: dict[str, Any],
    seen_requests: set[str],
    seen_results: set[str],
) -> int:
    search = source["search_source"]
    discovery = search["plans"]
    previous_end = 0
    for candidate, observation in zip(
        search["search_plan"]["candidates"], search["observations"], strict=False
    ):
        protocol = _protocol(search["search_plan"], discovery, candidate)
        previous_end, _ = _track(
            observation["inputs"],
            protocol,
            discovery,
            seen_requests,
            seen_results,
            previous_end,
        )
    reduction_plan = source["reduction_plan"]
    for attempt, observation in zip(
        verified["attempts"], source["observations"], strict=True
    ):
        protocol = make_reduced_protocol(
            discovery,
            source_parameters=reduction_plan["root_parameters"],
            retained_groups=attempt["retained_groups"],
            **reduction_plan["comparison_options"],
        )
        previous_end, _ = _track(
            observation["inputs"],
            protocol,
            discovery,
            seen_requests,
            seen_results,
            previous_end,
        )
    return previous_end


def evaluate(
    plan: dict[str, Any],
    source: dict[str, Any],
    plans: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Recheck raw source and held-out trials; retain every planned block."""
    try:
        return _evaluate(plan, source, plans, inputs)
    except _MALFORMED as error:
        raise ValueError("malformed held-out confirmation input") from error


def _evaluate(
    plan: dict[str, Any],
    source: dict[str, Any],
    plans: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    verified = _validate_plan(plan, source, plans)
    comparison = compare(plan["protocol"], plans, inputs)
    seen_requests: set[str] = set()
    seen_results: set[str] = set()
    previous_end = _track_source(source, verified, seen_requests, seen_results)
    _, overlaps = _track(
        inputs,
        plan["protocol"],
        plans,
        seen_requests,
        seen_results,
        previous_end,
    )
    reasons = ["SOURCE_AND_HELD_OUT_REPORTED_WINDOWS_OVERLAP"] if overlaps else []
    status = (
        "INELIGIBLE"
        if reasons or comparison["status"] == "INELIGIBLE"
        else (
            "HELD_OUT_CRITERIA_MET"
            if comparison["status"] == "REVERSAL_CANDIDATE"
            else "HELD_OUT_CRITERIA_NOT_MET"
        )
    )
    result = {
        "schema": SCHEMA,
        "scope": SCOPE,
        "status": status,
        "evidence_class": plan["evidence_class"],
        "evidence_eligible": False,
        "confirmation_plan_sha256": plan["confirmation_plan_sha256"],
        "source_reduction_report_sha256": plan["source_reduction_report_sha256"],
        "protocol_sha256": plan["protocol"]["protocol_sha256"],
        "decision_rule": DECISION_RULE,
        "comparison": comparison,
        "additional_ineligibility_reasons": reasons,
        "independent_execution": "UNVERIFIED",
        "preregistration": "UNVERIFIED",
        "execution_declarations_scope": "OPERATOR_SUPPLIED_NOT_AUTHENTICATED",
        "chronology_scope": "REPORTED_CLIENT_CLOCKS_NOT_TRUSTED_CHRONOLOGY",
        "multiplicity_scope": "TWO_FIXED_DIRECTIONS_FOR_ONE_FIXED_HELD_OUT_BATCH",
        "retry_enforcement": "NO_GLOBAL_REGISTRY_OR_CROSS_INVOCATION_ENFORCEMENT",
        "limitations": [
            "This report applies the existing two-direction criteria to one frozen "
            "candidate and one complete held-out batch; the nested exploratory "
            "comparison retains its original schema and claim boundaries.",
            "Fresh seed, trace and plan identities are checked against all supplied "
            "discovery plans. They do not prove outcomes were unseen, blocks were "
            "independent, or the plan was registered before collection.",
            "All supplied search and reduction attempts, including failed attempts, "
            "participate in result and request reuse checks. Undisclosed experiments "
            "or reuse outside this supplied source are not detectable.",
            "The fixed binomial criteria concern directional margin exceedance "
            "probability under independent blocks, not population-mean significance. "
            "The observed-mean guard does not establish mean significance.",
            "Requests are not independent replicates. The block is the replication "
            "unit; every planned block and every offer stays in accounting.",
            "The two-direction error allocation covers one predeclared held-out "
            "batch only under its assumptions. Repeated confirmations, optional "
            "stopping, discarded failures and adaptive reuse remain uncontrolled.",
            "The retained original epoch-local group ordinals define a timing rule "
            "on fresh workloads; concrete requests and runtime cache/queue state "
            "can differ. Minimality and causal attribution are not established.",
            "Hashes, declarations and reported clocks do not authenticate GPU "
            "execution, source authorship, reset state, independent execution or "
            "chronology. Scheduling gates bound p95, not every request.",
            "HELD_OUT_CRITERIA_NOT_MET does not establish equivalence or absence "
            "of a reversal. INELIGIBLE does not provide evidence against it.",
            "Synthetic evidence stays SYNTHETIC_ONLY; local measurement does not "
            "establish GPU provenance or customer acceptance. No trials are "
            "launched by this verifier.",
        ],
    }
    result["result_sha256"] = _digest(result)
    return result


def _load_source(path: Path) -> dict[str, Any]:
    manifest = _fields(
        _read_json(path.read_bytes()),
        {
            "schema",
            "search_source",
            "reduction_plan",
            "inputs",
            "report",
            "previous_report",
        },
        "confirmation source manifest",
    )
    if manifest["schema"] != SOURCE_SCHEMA:
        raise ValueError("confirmation source manifest schema mismatch")
    for field in ("search_source", "inputs"):
        if not isinstance(manifest[field], str) or not manifest[field]:
            raise ValueError(f"source {field} path must be a nonempty string")
    search = _load_search_source(path.parent / manifest["search_source"])
    reduction_plan = _json(path.parent, manifest["reduction_plan"])
    return {
        "search_source": search,
        "reduction_plan": reduction_plan,
        "observations": _load_reduction_inputs(
            path.parent / manifest["inputs"], reduction_plan, search
        ),
        "report": _json(path.parent, manifest["report"]),
        "previous_report": None
        if manifest["previous_report"] is None
        else _json(path.parent, manifest["previous_report"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Freeze and verify one held-out timing confirmation offline"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--plans", type=Path, nargs="+", required=True)
    prepare.add_argument("--order-seed", type=int, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    materialize = commands.add_parser("materialize")
    materialize.add_argument("--plan", type=Path, required=True)
    materialize.add_argument("--source", type=Path, required=True)
    materialize.add_argument("--plans", type=Path, nargs="+", required=True)
    materialize.add_argument("--output-dir", type=Path, required=True)
    for name in ("report", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--inputs", type=Path, required=True)
        command.add_argument(
            "--report" if name == "verify" else "--output", type=Path, required=True
        )
    args = parser.parse_args()
    source = _load_source(args.source)
    if args.command == "prepare":
        plans = [_read_json(path.read_bytes()) for path in args.plans]
        result = make_confirmation_plan(source, plans, order_seed=args.order_seed)
    elif args.command == "materialize":
        plan = _read_json(args.plan.read_bytes())
        plans = [_read_json(path.read_bytes()) for path in args.plans]
        validate_confirmation_plan(plan, source, plans)
        timings = reduced_timings(plans, plan["protocol"])
        artifacts = {"protocol.json": plan["protocol"]}
        for block, pair in enumerate(timings, start=1):
            for condition, timing in pair.items():
                artifacts[f"b{block:02d}-{condition}-timing.json"] = timing
        encoded = {
            name: canonical_json_bytes(value) + b"\n"
            for name, value in artifacts.items()
        }
        args.output_dir.mkdir(mode=0o700)
        for name, content in encoded.items():
            with (args.output_dir / name).open("xb") as stream:
                stream.write(content)
        return
    else:
        plan = _read_json(args.plan.read_bytes())
        plans, inputs = _load_inputs(args.inputs, plan["protocol"])
        result = evaluate(plan, source, plans, inputs)
        if args.command == "verify":
            if canonical_json_bytes(
                _read_json(args.report.read_bytes())
            ) != canonical_json_bytes(result):
                raise ValueError(
                    "confirmation report differs from regenerated raw evidence"
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
