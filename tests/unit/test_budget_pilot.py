"""Pure contract and reserve checks for the fixed four-cell budget pilot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from inferdrome.evaluation.budget_pilot import (
    BudgetPilotProtocol,
    compile_budget_pilot,
    load_budget_pilot_protocol_bytes,
    load_budget_pilot_recipe_bytes,
)
from inferdrome.evaluation.cli import main
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.direct_process_lifecycle import (
    REAL_GPU_STARTUP_TIMEOUT_NS,
    direct_process_lifecycle_reservation,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "examples" / "vast-budget-pilot" / "protocol.json"
RECIPE = ROOT / "examples" / "vast-budget-pilot" / "recipe.json"


def _compiled():
    protocol = load_budget_pilot_protocol_bytes(PROTOCOL.read_bytes())
    recipe = load_budget_pilot_recipe_bytes(RECIPE.read_bytes())
    return compile_budget_pilot(protocol, recipe)


def test_compiles_exact_four_cell_matrix_and_budget() -> None:
    plan = _compiled()
    assert plan.trial_count == 4
    assert plan.planned_foreground_requests == 384
    assert plan.planned_background_requests == 16
    assert plan.trial_execution_worst_case_duration_ns == 292_000_000_000
    assert plan.execution_worst_case_duration_ns == 2_932_000_000_000
    assert plan.worst_case_duration_ns == 2_942_000_000_000
    assert plan.reserved_output_bytes == 23_068_672
    assert [
        (row["condition"], row["policy_id"]) for row in plan.to_dict()["trials"]
    ] == [
        ("HEALTHY", "evaluation_round_robin_v1"),
        ("HEALTHY", "evaluation_freshness_fallback_v1"),
        ("STALE_LOAD", "evaluation_round_robin_v1"),
        ("STALE_LOAD", "evaluation_freshness_fallback_v1"),
    ]


def test_lifecycle_reserve_has_offline_live_source_parity() -> None:
    plan = _compiled()
    live = direct_process_lifecycle_reservation(
        startup_timeout_ns=REAL_GPU_STARTUP_TIMEOUT_NS
    )
    assert plan.lifecycle_prepare_ns_per_trial == live.required_prepare_timeout_ns
    assert plan.lifecycle_cleanup_ns_per_trial == live.required_cleanup_timeout_ns
    assert (live.required_prepare_timeout_ns, live.required_cleanup_timeout_ns) == (
        420_000_000_000,
        240_000_000_000,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        (
            "policy_order",
            ["evaluation_round_robin_v1", "evaluation_fail_closed_v1"],
        ),
        ("condition_order", ["STALE_LOAD", "HEALTHY"]),
        ("max_session_duration_ns", 2_942_000_000_001),
        ("max_session_output_bytes", 23_068_673),
    ),
)
def test_protocol_or_exact_bound_drift_fails_closed(field: str, value: object) -> None:
    raw = json.loads(PROTOCOL.read_bytes())
    raw[field] = value
    if field in {"policy_order", "condition_order"}:
        with pytest.raises(ValidationError):
            BudgetPilotProtocol.model_validate(raw)
        return
    protocol = BudgetPilotProtocol.model_validate_json(canonical_json_bytes(raw))
    recipe_raw = json.loads(RECIPE.read_bytes())
    recipe_raw["protocol_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(protocol.model_dump(mode="json"))
        ).hexdigest()
    )
    recipe = load_budget_pilot_recipe_bytes(canonical_json_bytes(recipe_raw))
    with pytest.raises(EvaluationError, match="session bounds are not exact"):
        compile_budget_pilot(protocol, recipe)


def test_recipe_workload_drift_fails_closed_after_rebinding() -> None:
    protocol = load_budget_pilot_protocol_bytes(PROTOCOL.read_bytes())
    raw = json.loads(RECIPE.read_bytes())
    raw["foreground"]["max_tokens"] += 1
    raw["protocol_sha256"] = _compiled().protocol_sha256
    recipe = load_budget_pilot_recipe_bytes(canonical_json_bytes(raw))
    with pytest.raises(EvaluationError, match="changes the fixed workload"):
        compile_budget_pilot(protocol, recipe)


def test_cli_writes_same_pure_preflight(tmp_path: Path) -> None:
    output = tmp_path / "plan.json"
    assert (
        main(
            [
                "vast-budget-pilot-preflight",
                "--protocol",
                str(PROTOCOL),
                "--recipe",
                str(RECIPE),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    value = json.loads(output.read_bytes())
    assert value["trial_count"] == 4
    assert value["provider_action_performed"] is False
    assert value["evidence_eligible"] is False
