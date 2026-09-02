"""Deterministic execution behavior, terminal closure, and no-retry semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from inferdrome.routing_execution.executor import (
    ManualMonotonicClock,
    run_execution,
)
from inferdrome.routing_execution.verifier import verify_execution_package
from tests.routing_execution_support import StaticEndpointTransport, write_inputs


def _run_static(
    root: Path, *, mode: str = "normal", cancel: bool = False
) -> tuple[object, list[StaticEndpointTransport]]:
    config_path, workload_path, _, _ = write_inputs(root)
    transports: list[StaticEndpointTransport] = []

    def factory() -> StaticEndpointTransport:
        transport = StaticEndpointTransport(mode=mode)
        transports.append(transport)
        return transport

    sealed = run_execution(
        config_path,
        workload_path,
        root / "package",
        transport_factory=factory,
        clock=ManualMonotonicClock(),
        cancel_requested=(lambda: cancel),
    )
    return sealed, transports


def test_stale_load_fresh_health_policy_projection_and_complete_bijection(
    tmp_path: Path,
) -> None:
    sealed, transports = _run_static(tmp_path)
    report = verify_execution_package(sealed.path)
    receipt = report.producer_receipt
    assert len(receipt.route_decisions) == len(receipt.terminal_outcomes) == 18
    assert len(receipt.telemetry_observations) == 3 * 6 * 2 * 4
    assert len(transports) == 3
    assert all(transport.closed for transport in transports)

    by_policy = {
        summary.policy_id: summary.trial_id for summary in receipt.trial_summaries
    }
    for policy_id, trial_id in by_policy.items():
        stale_decisions = [
            row
            for row in receipt.route_decisions
            if row.trial_id == trial_id and row.sequence_index >= 2
        ]
        assert len(stale_decisions) == 4
        for decision in stale_decisions:
            assert all(
                candidate.health.admissibility == "ADMISSIBLE"
                and candidate.load.state == "STALE"
                and candidate.load.admissibility == "INADMISSIBLE"
                for candidate in decision.candidates
            )
        if policy_id == "fail_closed_required_load_v1":
            assert all(row.selected_endpoint_id is None for row in stale_decisions)
            assert all(
                row.fallback_reason == "REQUIRED_LOAD_STALE" for row in stale_decisions
            )
        elif policy_id == "explicit_fail_open_stale_load_v1":
            assert all(
                row.selected_endpoint_id == "endpoint-b" for row in stale_decisions
            )
            assert all(
                row.fallback_reason == "STALE_LOAD_FAIL_OPEN" for row in stale_decisions
            )
        else:
            assert all(
                row.selected_endpoint_id == "endpoint-a" for row in stale_decisions
            )
            assert all(
                row.fallback_reason == "HEALTH_ONLY_TIE_BREAK"
                for row in stale_decisions
            )


def test_timeout_and_cancellation_are_terminalized_without_retry(
    tmp_path: Path,
) -> None:
    timeout_root = tmp_path / "timeout"
    timeout_root.mkdir(mode=0o700)
    sealed, timeout_transports = _run_static(timeout_root, mode="timeout")
    receipt = verify_execution_package(sealed.path).producer_receipt
    selected = [row for row in receipt.terminal_outcomes if row.attempt_count == 1]
    assert selected and all(row.status == "TIMED_OUT" for row in selected)
    assert sum(len(row.request_bodies) for row in timeout_transports) == len(selected)

    cancel_root = tmp_path / "cancel"
    cancel_root.mkdir(mode=0o700)
    cancelled, cancel_transports = _run_static(cancel_root, cancel=True)
    cancelled_receipt = verify_execution_package(cancelled.path).producer_receipt
    selected_cancelled = [
        row
        for row in cancelled_receipt.terminal_outcomes
        if row.selected_endpoint_id is not None
    ]
    assert selected_cancelled and all(
        row.status == "CANCELLED" for row in selected_cancelled
    )
    assert all(row.attempt_count == 0 for row in selected_cancelled)
    assert all(row.request_sha256 is None for row in selected_cancelled)
    assert sum(len(row.request_bodies) for row in cancel_transports) == 0


def test_fixed_clock_and_transport_produce_byte_stable_packages(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir(mode=0o700)
    second_root.mkdir(mode=0o700)
    first, _ = _run_static(first_root)
    second, _ = _run_static(second_root)
    assert first.retained_digest == second.retained_digest
    assert (first.path / "producer-receipt.json").read_bytes() == (
        second.path / "producer-receipt.json"
    ).read_bytes()


@pytest.mark.parametrize(
    "mode", ("malformed_completion", "empty_choices", "empty_choice_content")
)
def test_malformed_openai_completion_is_a_failed_single_attempt(
    tmp_path: Path, mode: str
) -> None:
    sealed, transports = _run_static(tmp_path, mode=mode)
    receipt = verify_execution_package(sealed.path).producer_receipt
    attempted = [row for row in receipt.terminal_outcomes if row.attempt_count == 1]
    assert attempted
    assert all(row.status == "FAILED" for row in attempted)
    assert all(row.reason == "MALFORMED_RESPONSE" for row in attempted)
    assert sum(len(row.request_bodies) for row in transports) == len(attempted)


@pytest.mark.parametrize(
    "mode", ("malformed_metrics", "ambiguous_metrics", "wrong_model_metrics")
)
def test_inadmissible_target_metric_fails_closed_without_aggregation(
    tmp_path: Path, mode: str
) -> None:
    sealed, _ = _run_static(tmp_path, mode=mode)
    receipt = verify_execution_package(sealed.path).producer_receipt
    load = [row for row in receipt.telemetry_observations if row.signal == "LOAD"]
    assert load and all(row.value == "UNAVAILABLE" for row in load)
    assert all(row.selected_endpoint_id is None for row in receipt.route_decisions)
    assert all(row.status == "NO_SAFE_ROUTE" for row in receipt.terminal_outcomes)
