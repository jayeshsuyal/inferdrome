"""Public summaries project freshly verified artifacts without private strings."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from test_vllm_bounded_search import _fixture as _search_fixture
from test_vllm_bounded_search import _observation as _search_observation
from test_vllm_heldout_confirmation import (
    _confirmation_plan,
    _confirmation_source,
    _heldout_plans,
    _inputs,
    _write_inputs,
    _write_source,
)
from test_vllm_witness_reducer import _plan as _reduction_plan
from test_vllm_witness_reducer import _rehash

from inferdrome import vllm_bounded_search as bounded
from inferdrome import vllm_heldout_confirmation as confirmation
from inferdrome import vllm_witness_reducer as reducer
from inferdrome.cli import main
from inferdrome.vllm_search_plan import make_search_plan


@lru_cache
def _template(
    signal: str = "reversal", hostile: bool = False, kind: str = "search_incumbent"
) -> dict[str, Any]:
    source = _confirmation_source(kind)
    if hostile:
        _, plans, options = _search_fixture(blocks=8)
        options["comparison_options"]["model"] = (
            "![PRIVATE_MODEL_SENTINEL](https://private.invalid/secret) "
            "<script>secret</script>"
        )
        search_plan = make_search_plan(plans, **options)
        observations = [_search_observation(search_plan, plans, 0, reversal=True)]
        search = {
            "search_plan": search_plan,
            "plans": plans,
            "observations": observations,
            "report": bounded.evaluate(search_plan, plans, observations),
            "previous_report": None,
        }
        plan = _reduction_plan(search, max_trial_slots=31)
        source = {
            "search_source": search,
            "reduction_plan": plan,
            "observations": [],
            "report": reducer.reduce(plan, search, []),
            "previous_report": None,
        }
    plans = _heldout_plans()
    plan = _confirmation_plan(source, plans)
    inputs = _inputs(source, plan, plans, signal=signal)
    if signal == "missing":
        inputs.pop()
    return {
        "source": source,
        "plans": plans,
        "plan": plan,
        "inputs": inputs,
        "report": confirmation.evaluate(plan, source, plans, inputs),
    }


def _bundle(
    tmp_path: Path,
    *,
    signal: str = "reversal",
    hostile: bool = False,
    kind: str = "search_incumbent",
) -> dict[str, Any]:
    bundle = copy.deepcopy(_template(signal, hostile, kind))
    source_path = _write_source(tmp_path, bundle["source"])
    inputs_path, _ = _write_inputs(
        tmp_path, bundle["plan"], bundle["plans"], bundle["inputs"]
    )
    plan_path = tmp_path / "confirmation-plan.json"
    report_path = tmp_path / "confirmation-report.json"
    plan_path.write_text(json.dumps(bundle["plan"]))
    report_path.write_text(json.dumps(bundle["report"]))
    bundle["flags"] = {
        "search": [
            "--search-plan",
            str(tmp_path / "search-plan.json"),
            "--inputs",
            str(tmp_path / "inputs.json"),
            "--report",
            str(tmp_path / "source-report.json"),
        ],
        "reduce": [
            "--plan",
            str(tmp_path / "reduction-plan.json"),
            "--source",
            str(tmp_path / "source.json"),
            "--inputs",
            str(tmp_path / "reduction-inputs.json"),
            "--report",
            str(tmp_path / "reduction-report.json"),
        ],
        "confirm": [
            "--plan",
            str(plan_path),
            "--source",
            str(source_path),
            "--inputs",
            str(inputs_path),
            "--report",
            str(report_path),
        ],
    }
    return bundle


def _summarize(bundle: dict[str, Any], stage: str, output: Path) -> int:
    return main(
        [
            "breakpoint",
            "summarize",
            stage,
            *bundle["flags"][stage],
            "--output",
            str(output),
        ]
    )


@pytest.mark.parametrize(
    ("stage", "status"),
    [
        ("search", "CANDIDATE_FOUND"),
        ("reduce", "BUDGET_EXHAUSTED"),
        ("confirm", "HELD_OUT_CRITERIA_MET"),
    ],
)
def test_summary_reverifies_all_stages_and_exposes_revisions_and_claim_boundaries(
    tmp_path: Path, stage: str, status: str
) -> None:
    bundle = _bundle(tmp_path)
    output = tmp_path / "summary.md"
    assert _summarize(bundle, stage, output) == 0
    rendered = output.read_text()
    assert rendered.startswith("# Breakpoint results")
    assert status in rendered
    assert "SYNTHETIC_ONLY" in rendered
    assert "Evidence eligible: false" in rendered
    for heading in ("## Method", "## Result", "## Artifact references", "## Limits"):
        assert heading in rendered
    assert "a" * 40 in rendered
    for secret in (
        "PRIVATE_SEARCH_LEDGER_SENTINEL",
        str(tmp_path),
        "http://127.0.0.1:8090",
    ):
        assert secret not in rendered
    result = {
        "search": bundle["source"]["search_source"]["report"],
        "reduce": bundle["source"]["report"],
        "confirm": bundle["report"],
    }[stage]
    assert result["result_sha256"] in rendered


def test_confirmation_summary_presents_verified_effects_and_trial_population(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    output = tmp_path / "summary.md"
    assert _summarize(bundle, "confirm", output) == 0
    rendered = output.read_text()
    assert "cache_only" in rendered and "least_busy" in rendered
    assert "32" in rendered
    assert "`2/1`" in rendered and "`-2/1`" in rendered
    assert "`1/256`" in rendered
    assert bundle["plan"]["confirmation_plan_sha256"] in rendered
    assert bundle["plan"]["protocol"]["protocol_sha256"] in rendered


@pytest.mark.parametrize("stage", ["search", "reduce", "confirm"])
def test_valid_arbitrary_model_and_private_ledger_text_are_not_published(
    tmp_path: Path, stage: str
) -> None:
    bundle = _bundle(tmp_path, hostile=True)
    output = tmp_path / "summary.md"
    assert _summarize(bundle, stage, output) == 0
    rendered = output.read_text()
    for secret in (
        "PRIVATE_MODEL_SENTINEL",
        "PRIVATE_SEARCH_LEDGER_SENTINEL",
        "private.invalid",
        "<script>",
        "127.0.0.1",
        str(tmp_path),
    ):
        assert secret not in rendered


@pytest.mark.parametrize("stage", ["search", "reduce", "confirm"])
def test_rehashed_saved_report_tampering_is_refused_before_publication(
    tmp_path: Path, stage: str
) -> None:
    bundle = _bundle(tmp_path)
    flags = bundle["flags"][stage]
    path = Path(flags[flags.index("--report") + 1])
    report = json.loads(path.read_text())
    report["evidence_eligible"] = True
    _rehash(report)
    path.write_text(json.dumps(report))
    output = tmp_path / "must-not-exist.md"
    assert _summarize(bundle, stage, output) == 1
    assert not output.exists()


@pytest.mark.parametrize("stage", ["search", "reduce", "confirm"])
def test_raw_source_corruption_is_refused_with_saved_reports_untouched(
    tmp_path: Path, stage: str
) -> None:
    bundle = _bundle(tmp_path)
    path = tmp_path / "candidate-0-trial-0-ledger.json"
    ledger = [json.loads(row) for row in path.read_text().splitlines()]
    ledger[0]["private_debug"] = "silently edited raw receipt"
    path.write_text("".join(json.dumps(row) + "\n" for row in ledger))
    output = tmp_path / "must-not-exist.md"
    assert _summarize(bundle, stage, output) == 1
    assert not output.exists()


def test_existing_summary_is_preserved_on_exclusive_publication_failure(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    output = tmp_path / "summary.md"
    output.write_bytes(b"previous reviewed public summary\n")
    assert _summarize(bundle, "confirm", output) == 1
    assert output.read_bytes() == b"previous reviewed public summary\n"


@pytest.mark.parametrize(
    ("signal", "status", "exit_code"),
    [("weak", "HELD_OUT_CRITERIA_NOT_MET", 0), ("missing", "INELIGIBLE", 2)],
)
def test_nonpositive_outcomes_keep_their_status_and_exit_codes(
    tmp_path: Path, signal: str, status: str, exit_code: int
) -> None:
    bundle = _bundle(tmp_path, signal=signal)
    output = tmp_path / "summary.md"
    assert _summarize(bundle, "confirm", output) == exit_code
    assert status in output.read_text()
    assert "Evidence eligible: false" in output.read_text()


@pytest.mark.parametrize("stage", ["search", "reduce"])
def test_awaiting_evidence_summary_preserves_status_and_exit_code(
    tmp_path: Path, stage: str
) -> None:
    bundle = _bundle(tmp_path)
    source = bundle["source"]["search_source"]
    if stage == "search":
        path = tmp_path / "inputs.json"
        manifest = json.loads(path.read_text())
        manifest["candidates"] = []
        path.write_text(json.dumps(manifest))
        report = bounded.evaluate(source["search_plan"], source["plans"], [])
        (tmp_path / "source-report.json").write_text(json.dumps(report))
    else:
        plan = _reduction_plan(source)
        (tmp_path / "reduction-plan.json").write_text(json.dumps(plan))
        path = tmp_path / "reduction-inputs.json"
        manifest = json.loads(path.read_text())
        manifest["reduction_plan_sha256"] = plan["reduction_plan_sha256"]
        path.write_text(json.dumps(manifest))
        report = reducer.reduce(plan, source, [])
        (tmp_path / "reduction-report.json").write_text(json.dumps(report))
    output = tmp_path / "summary.md"
    assert _summarize(bundle, stage, output) == 2
    assert "AWAITING_EVIDENCE" in output.read_text()


def test_reduction_summary_displays_the_accepted_incumbent_comparison(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, kind="budget")
    output = tmp_path / "summary.md"
    assert _summarize(bundle, "reduce", output) == 0
    rendered = output.read_text()
    report = bundle["source"]["report"]
    incumbent = report["incumbent"]
    comparison = next(
        attempt["comparison"]
        for attempt in report["attempts"]
        if attempt["proposal_id"] == incumbent["proposal_id"]
    )
    assert incumbent["source"] == "REDUCTION"
    assert comparison["result_sha256"] in rendered
    assert (
        bundle["source"]["search_source"]["report"]["selected_candidate"][
            "comparison_sha256"
        ]
        not in rendered
    )
    assert (
        f"Retained timing groups: {len(incumbent['retained_groups'])} / 6" in rendered
    )


@pytest.mark.parametrize("stage", ["search", "reduce"])
def test_explicit_null_previous_report_is_rejected_without_publication(
    tmp_path: Path, stage: str
) -> None:
    bundle = _bundle(tmp_path)
    previous = tmp_path / "previous.json"
    previous.write_text("null")
    bundle["flags"][stage].extend(["--previous-report", str(previous)])
    output = tmp_path / "must-not-exist.md"
    assert _summarize(bundle, stage, output) == 1
    assert not output.exists()
