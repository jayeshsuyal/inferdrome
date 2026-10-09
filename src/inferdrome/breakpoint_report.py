"""Allowlisted Markdown views regenerated from complete Breakpoint evidence."""

from __future__ import annotations

import argparse
import html
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from inferdrome import vllm_bounded_search as search
from inferdrome import vllm_heldout_confirmation as confirmation
from inferdrome import vllm_witness_reducer as reduction
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_paired_comparison import _load_inputs
from inferdrome.vllm_request_identity import _read_json


@dataclass(frozen=True)
class _VerifiedReport:
    stage: str
    report: dict[str, Any]
    plan: dict[str, Any]
    plans: list[dict[str, Any]]
    options: dict[str, Any]
    comparison: dict[str, Any] | None
    parameters: dict[str, Any] | None


def _object(path: Path) -> dict[str, Any]:
    value = _read_json(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("artifact must be a JSON object")
    return value


def _previous(path: Path | None) -> dict[str, Any] | None:
    return None if path is None else _object(path)


def _selected_comparison(report: dict[str, Any]) -> dict[str, Any] | None:
    selected = report["selected_candidate"]
    if selected is None:
        return None
    for candidate in report["candidate_results"]:
        if candidate["candidate_id"] == selected["candidate_id"]:
            comparison: dict[str, Any] = candidate["comparison"]
            return comparison
    raise ValueError("selected candidate comparison is unavailable")


def _regenerate(args: argparse.Namespace) -> _VerifiedReport:
    if args.stage == "search":
        plan = _object(args.search_plan)
        plans, observations = search._load_search_inputs(args.inputs, plan)
        report = search.evaluate(
            plan, plans, observations, previous_report=_previous(args.previous_report)
        )
        selected = report["selected_candidate"]
        result = _VerifiedReport(
            "search",
            report,
            plan,
            plans,
            plan["comparison_options"],
            _selected_comparison(report),
            None if selected is None else selected["parameters"],
        )
    elif args.stage == "reduce":
        source = reduction._load_source(args.source)
        plan = _object(args.plan)
        observations = reduction._load_reduction_inputs(args.inputs, plan, source)
        report = reduction.reduce(
            plan, source, observations, previous_report=_previous(args.previous_report)
        )
        comparison = _selected_comparison(source["report"])
        for attempt in report["attempts"]:
            if attempt["proposal_id"] == report["incumbent"]["proposal_id"]:
                comparison = attempt["comparison"]
        result = _VerifiedReport(
            "reduce",
            report,
            plan,
            source["plans"],
            plan["comparison_options"],
            comparison,
            plan["root_parameters"],
        )
    else:
        source = confirmation._load_source(args.source)
        plan = _object(args.plan)
        plans, inputs = _load_inputs(args.inputs, plan["protocol"])
        report = confirmation.evaluate(plan, source, plans, inputs)
        result = _VerifiedReport(
            "confirm",
            report,
            plan,
            plans,
            plan["protocol"],
            report["comparison"],
            plan["protocol"]["candidate_parameters"],
        )
    if canonical_json_bytes(_object(args.report)) != canonical_json_bytes(report):
        raise ValueError("saved report differs from regenerated raw evidence")
    return result


def _literal(value: object) -> str:
    # Even a future validator accepting user strings cannot introduce markup,
    # links, raw HTML, table cells, or terminal controls through these scalars.
    text = "".join(
        f"\\u{ord(character):04x}"
        if ord(character) < 32 or 127 <= ord(character) < 160
        else character
        for character in str(value)
    )
    escaped = html.escape(text, quote=True).replace("`", "&#96;").replace("|", "&#124;")
    return f"`{escaped}`"


def _metric_rows(comparison: dict[str, Any] | None) -> list[str]:
    if comparison is None or comparison["statistics"] is None:
        return ["Directional statistics are unavailable for this result.", ""]
    statistics = comparison["statistics"]
    rows = [
        "| Timing | Exact mean A - B goodput (requests/s) | Margin exceedances "
        "| Exact p | Direction passes |",
        "|---|---:|---:|---:|---|",
    ]
    for label, key in (("Baseline", "baseline"), ("Candidate", "candidate")):
        values = statistics[key]
        passes = "yes" if values["direction_supported"] else "no"
        mean = values["exact_rps"]["mean"]
        exact_mean = f"{mean['numerator']}/{mean['denominator']}"
        pvalue = values["pvalue"]
        exact_pvalue = f"{pvalue['numerator']}/{pvalue['denominator']}"
        rows.append(
            f"| {label} | {_literal(exact_mean)} | "
            f"{values['exceedances']}/{values['block_count']} | "
            f"{_literal(exact_pvalue)} | {passes} |"
        )
    rows.extend(
        [
            "",
            "Baseline tests A above B; candidate tests B above A. Each direction uses "
            "the fixed practical margin, an exact one-sided binomial threshold of "
            "1/40, and an observed-mean guard. The block is the replication unit.",
            "",
        ]
    )
    return rows


def _result_rows(value: _VerifiedReport) -> list[str]:
    report = value.report
    rows = ["## Result", ""]
    if value.stage == "search":
        coverage = report["coverage"]
        rows.extend(
            [
                f"- Evaluated candidates: {report['evaluated_candidates']} / "
                f"{coverage['scheduled_candidates']} scheduled; "
                f"{report['budget']['max_candidates']} declared candidate limit.",
                f"- Ineligible candidates: {coverage['ineligible_candidates']}; "
                f"inconclusive candidates: {coverage['inconclusive_candidates']}.",
                f"- Reserved trial slots: {report['budget']['trial_slots_reserved']} / "
                f"{report['budget']['max_trial_slots']} declared limit.",
            ]
        )
        selected = report["selected_candidate"]
        if selected is not None:
            distance = selected["distance"]
            rows.extend(
                [
                    f"- Selected candidate: {_literal(selected['candidate_id'])}.",
                    "- Planned advance: maximum "
                    f"{_literal(distance['max_advance_ns'])} ns; "
                    f"total {_literal(distance['total_advance_ns'])} ns; "
                    f"{distance['moved_offers']} moved offers across seed blocks.",
                ]
            )
    elif value.stage == "reduce":
        coverage = report["coverage"]
        rows.extend(
            [
                f"- Retained timing groups: {coverage['retained_groups']} / "
                f"{coverage['source_groups']} original groups.",
                f"- Completed comparisons: {report['completed_comparisons']} / "
                f"{report['budget']['max_comparisons']} declared limit; "
                f"accepted reductions: {coverage['accepted_reductions']}.",
                f"- Ineligible comparisons: {coverage['ineligible_comparisons']}; "
                f"inconclusive comparisons: {coverage['inconclusive_comparisons']}.",
                f"- Reserved trial slots: {report['budget']['trial_slots_reserved']} / "
                f"{report['budget']['max_trial_slots']} declared limit.",
            ]
        )
    else:
        rows.extend(
            [
                "- One frozen candidate and one held-out batch.",
                f"- Fresh held-out seed blocks: {value.plan['planned_blocks']}.",
                f"- Retained timing groups: "
                f"{len(value.plan['source_incumbent']['retained_groups'])}.",
            ]
        )
    if report.get("next_action") is not None:
        rows.append(
            "- Next step: supply the next protocol's separately collected evidence."
        )
    if value.comparison is not None:
        rows.append(
            f"- Trial artifacts in the displayed comparison: "
            f"{value.comparison['supplied_trials']} / "
            f"{value.comparison['planned_trials']} planned."
        )
    rows.append("")
    rows.extend(_metric_rows(value.comparison))
    return rows


def _references(value: _VerifiedReport) -> list[str]:
    report = value.report
    plan_fields = {
        "search": "search_plan_sha256",
        "reduce": "reduction_plan_sha256",
        "confirm": "confirmation_plan_sha256",
    }
    rows = [
        "## Artifact references",
        "",
        f"- Plan digest: {_literal(value.plan[plan_fields[value.stage]])}.",
        f"- Report digest: {_literal(report['result_sha256'])}.",
        f"- Declared source revision: {_literal(value.options['source_revision'])}.",
        "- Declared environment digest: "
        f"{_literal(value.options['environment_sha256'])}.",
        "- Declared reset procedure digest: "
        f"{_literal(value.options['reset_procedure_sha256'])}.",
    ]
    for label, key in (
        ("Source search report digest", "source_report_sha256"),
        ("Source reduction report digest", "source_reduction_report_sha256"),
        ("Previous report digest", "previous_report_sha256"),
    ):
        if report.get(key) is not None:
            rows.append(f"- {label}: {_literal(report[key])}.")
    if value.comparison is not None:
        rows.extend(
            [
                f"- Displayed comparison digest: "
                f"{_literal(value.comparison['result_sha256'])}.",
                f"- Displayed protocol digest: "
                f"{_literal(value.comparison['protocol_sha256'])}.",
            ]
        )
    rows.extend(
        [
            "",
            "These references bind artifact contents. Source, environment and reset "
            "values are operator declarations; their authenticity is unverified.",
            "",
        ]
    )
    return rows


def _render(value: _VerifiedReport) -> str:
    report = value.report
    options = value.options
    plan = value.plans[0]
    meanings = {
        "AWAITING_EVIDENCE": "The next planned comparison needs evidence.",
        "CANDIDATE_FOUND": "The exploratory screen selected a timing candidate.",
        "GRID_EXHAUSTED": "The supplied search exhausted its grid without a selection.",
        "BUDGET_EXHAUSTED": "The declared comparison budget is exhausted.",
        "REDUCTION_COMPLETE": "The declared removal procedure has completed.",
        "HELD_OUT_CRITERIA_MET": "The held-out evidence meets both fixed criteria.",
        "HELD_OUT_CRITERIA_NOT_MET": (
            "The held-out evidence does not meet both criteria."
        ),
        "INELIGIBLE": "The supplied evidence cannot support the planned comparison.",
    }
    rows = [
        "# Breakpoint results",
        "",
        f"- Stage: {_literal(value.stage)}.",
        f"- Status: {_literal(report['status'])}.",
        f"- Evidence class: {_literal(report['evidence_class'])}.",
        "- Evidence eligible: false.",
        "",
        meanings[report["status"]],
        "",
        "The saved JSON report was regenerated from its supplied raw artifacts and "
        "matched exactly before this summary was written. Retain those artifacts "
        "for replay; this Markdown file is a presentation of that verification.",
        "",
    ]
    if report["evidence_class"] == "SYNTHETIC_ONLY":
        rows.extend(
            [
                "**SYNTHETIC_ONLY: this result demonstrates the software workflow and "
                "does not establish a vLLM or GPU performance finding.**",
                "",
            ]
        )
    rows.extend(
        [
            "## Method",
            "",
            f"- Policy A: {_literal(options['policy_a'])}; "
            f"policy B: {_literal(options['policy_b'])}.",
            f"- Seed blocks: {len(value.plans)}; "
            "four matched trials per comparison block.",
            f"- Offers per trial: {plan['offered_count']}; "
            f"goodput denominator: {_literal(plan['duration_ns'])} ns.",
            f"- First-content SLO: {_literal(plan['first_content_slo_ns'])} ns; "
            f"completion SLO: {_literal(plan['completion_slo_ns'])} ns.",
            f"- Client p95 limits: scheduling lag "
            f"{_literal(options['max_scheduling_lag_p95_ns'])} ns; queue delay "
            f"{_literal(options['max_client_queue_p95_ns'])} ns.",
            f"- Practical margin: {_literal(options['minimum_effect_microrps'])} "
            "microrequests/s.",
            "- Every offer stays in accounting. Timing changes preserve the workload "
            "population, token budgets, SLOs and measurement window.",
        ]
    )
    if value.parameters is not None:
        parameters = value.parameters
        rows.append(
            f"- Timing recipe: group size {parameters['group_size']}; retained spacing "
            f"{parameters['retained_spacing_bps']} basis points; maximum advance "
            f"{parameters['max_advance_ns']} ns."
        )
    rows.append("")
    rows.extend(_result_rows(value))
    rows.extend(_references(value))
    rows.extend(
        [
            "## Limits",
            "",
            "- Independent execution, advance registration, reset state and GPU "
            "provenance remain unverified. Reported clocks establish consistency only.",
            "- Directional criteria assume independent blocks and a null directional "
            "margin-exceedance probability at most one half. They do not establish "
            "population-mean significance or causality.",
            "- Adaptive search and reduction do not establish global or statistical "
            "minimality. Changed arrivals may change runtime cache and queue state.",
            "- A negative, exhausted or ineligible outcome does not establish "
            "equivalence or absence of a reversal.",
        ]
    )
    if value.stage == "confirm":
        rows.append(
            "- Confirmation covers one fixed batch under its stated assumptions. "
            "Fresh supplied identities do not prove unseen outcomes; undisclosed "
            "experiments, retries and selective publication remain uncontrolled."
        )
    else:
        rows.append(
            "- This stage is exploratory. Search multiplicity is uncontrolled; "
            "confirmation requires a separate linked held-out artifact."
        )
    if report.get("previous_report_sha256") is not None:
        rows.append(
            "- Resume verification regenerates the immediate previous prefix; "
            "earlier ancestor report files are not traversed."
        )
    rows.extend(
        [
            "- This view omits raw prompts, request identifiers, model identifiers, "
            "endpoint URLs and artifact paths. Keep raw archives private.",
            "",
        ]
    )
    return "\n".join(rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="inferdrome breakpoint summarize",
        description=(
            "Regenerate a saved report before writing an allowlisted Markdown view"
        ),
    )
    stages = parser.add_subparsers(dest="stage", required=True)
    for stage in ("search", "reduce", "confirm"):
        command = stages.add_parser(stage)
        if stage == "search":
            command.add_argument("--search-plan", type=Path, required=True)
        else:
            command.add_argument("--plan", type=Path, required=True)
            command.add_argument("--source", type=Path, required=True)
        command.add_argument("--inputs", type=Path, required=True)
        if stage != "confirm":
            command.add_argument("--previous-report", type=Path)
        command.add_argument("--report", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    verified = _regenerate(args)
    rendered = _render(verified)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(rendered)
    return 2 if verified.report["status"] in {"AWAITING_EVIDENCE", "INELIGIBLE"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
