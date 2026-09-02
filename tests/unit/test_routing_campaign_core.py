"""Scenario and determinism tests for the synthetic routing campaign."""

from __future__ import annotations

import json
from pathlib import Path

from inferdrome.routing_campaign import run_campaign, verify_campaign

_ROOT = Path(__file__).resolve().parents[2]
_INPUTS = _ROOT / "campaigns" / "routing-campaign-v1"


def _run(output: Path):
    return run_campaign(
        _INPUTS / "stale-load-fresh-health.plan.json",
        _INPUTS / "stale-load-fresh-health.trace.jsonl",
        _INPUTS / "stale-load-fresh-health.fault-schedule.json",
        _INPUTS / "trial-plan.json",
        output,
    )


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_repeat_is_byte_identical_and_replay_closes_every_terminal(
    tmp_path: Path,
) -> None:
    first = _run(tmp_path / "first")
    second = _run(tmp_path / "second")

    assert first.retained_digest == second.retained_digest
    first_files = sorted(
        path.relative_to(first.path) for path in first.path.rglob("*") if path.is_file()
    )
    second_files = sorted(
        path.relative_to(second.path)
        for path in second.path.rglob("*")
        if path.is_file()
    )
    assert first_files == second_files
    for relative_path in first_files:
        assert (first.path / relative_path).read_bytes() == (
            second.path / relative_path
        ).read_bytes()

    report = verify_campaign(first.path, expected_digest=first.retained_digest)
    assert report.trial_count == 3
    assert report.planned_request_count == 18
    assert report.trial_ids == (
        "trial-fail-closed-v1",
        "trial-fail-open-v1",
        "trial-typed-v1",
    )


def test_stale_load_fresh_health_scenario_has_exact_policy_populations(
    tmp_path: Path,
) -> None:
    sealed = _run(tmp_path / "campaign")
    summary = json.loads(
        (sealed.path / "derived" / "campaign-summary.json").read_text(encoding="utf-8")
    )
    populations = {
        item["policy_id"]: item["terminal_population"]
        for item in summary["trial_summaries"]
    }
    assert populations == {
        "fail_closed_required_load_v1": {
            "CANCELLED": 0,
            "FAILED": 0,
            "NO_SAFE_ROUTE": 4,
            "SUCCEEDED": 2,
            "TIMED_OUT": 0,
        },
        "explicit_fail_open_stale_load_v1": {
            "CANCELLED": 0,
            "FAILED": 0,
            "NO_SAFE_ROUTE": 0,
            "SUCCEEDED": 2,
            "TIMED_OUT": 4,
        },
        "typed_admissible_state_only_v1": {
            "CANCELLED": 0,
            "FAILED": 0,
            "NO_SAFE_ROUTE": 0,
            "SUCCEEDED": 6,
            "TIMED_OUT": 0,
        },
    }

    closed = _jsonl(
        sealed.path / "trials" / "trial-fail-closed-v1" / "route-decisions.jsonl"
    )
    open_rows = _jsonl(
        sealed.path / "trials" / "trial-fail-open-v1" / "route-decisions.jsonl"
    )
    typed = _jsonl(sealed.path / "trials" / "trial-typed-v1" / "route-decisions.jsonl")
    for rows in (closed, open_rows, typed):
        stale = rows[2]
        candidate_b = stale["candidates"][1]
        assert candidate_b["health"]["age_ms"] == 0
        assert candidate_b["health"]["admissibility"] == "ADMISSIBLE"
        assert candidate_b["load"]["age_ms"] == 10
        assert candidate_b["load"]["admissibility"] == "INADMISSIBLE"
    assert closed[2]["selected_endpoint_id"] is None
    assert closed[2]["fallback_reason"] == "REQUIRED_LOAD_STALE"
    assert open_rows[2]["selected_endpoint_id"] == "endpoint-b"
    assert open_rows[2]["fallback_reason"] == "STALE_LOAD_FAIL_OPEN"
    assert typed[2]["selected_endpoint_id"] == "endpoint-a"
    assert typed[2]["fallback_reason"] == "HEALTH_ONLY_TIE_BREAK"


def test_cold_reset_receipts_are_trial_scoped_and_clear_state(tmp_path: Path) -> None:
    sealed = _run(tmp_path / "campaign")
    reset_rows = []
    for trial_id in (
        "trial-fail-closed-v1",
        "trial-fail-open-v1",
        "trial-typed-v1",
    ):
        reset_rows.append(
            json.loads(
                (sealed.path / "trials" / trial_id / "reset-receipt.json").read_text(
                    encoding="utf-8"
                )
            )
        )
    assert {row["virtual_time_ms"] for row in reset_rows} == {0}
    assert all(row["queue_cleared"] for row in reset_rows)
    assert all(row["load_state_cleared"] for row in reset_rows)
    assert all(row["kv_state_cleared"] for row in reset_rows)
    endpoint_a_instances = {
        row["endpoint_instance_ids"]["endpoint-a"] for row in reset_rows
    }
    assert len(endpoint_a_instances) == 3
