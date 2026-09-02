"""Actual local socket integration for the two-endpoint PR-B bridge."""

from __future__ import annotations

from pathlib import Path

from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.cli import _demo_config
from inferdrome.routing_execution.executor import ManualMonotonicClock, run_execution
from inferdrome.routing_execution.loopback import LoopbackPair
from inferdrome.routing_execution.verifier import verify_execution_package
from tests.routing_execution_support import workload_bytes


def test_two_real_loopback_endpoints_execute_full_fault_campaign(
    tmp_path: Path,
) -> None:
    workload = workload_bytes()
    with LoopbackPair() as endpoints:
        config = _demo_config(
            origin_a=endpoints.endpoint_a.origin,
            origin_b=endpoints.endpoint_b.origin,
            selected_sha256=sha256_digest(workload),
            source_commit="0" * 40,
        )
        config_path = tmp_path / "deployment-config.json"
        workload_path = tmp_path / "workload.jsonl"
        config_path.write_bytes(canonical_json_bytes(config))
        workload_path.write_bytes(workload)
        sealed = run_execution(
            config_path,
            workload_path,
            tmp_path / "package",
            clock=ManualMonotonicClock(),
        )
        assert endpoints.endpoint_a.model_count == endpoints.endpoint_b.model_count == 3
        assert (
            endpoints.endpoint_a.health_count == endpoints.endpoint_b.health_count == 18
        )
        assert (
            endpoints.endpoint_a.metrics_count
            == endpoints.endpoint_b.metrics_count
            == 6
        )
        assert endpoints.endpoint_a.request_count == 4
        assert endpoints.endpoint_b.request_count == 10

    verified = verify_execution_package(sealed.path)
    receipt = verified.producer_receipt
    assert len(receipt.reset_receipts) == len(receipt.fault_receipts) == 3
    assert all(row.load_collection_paused for row in receipt.fault_receipts)
    assert all(row.health_collection_continues for row in receipt.fault_receipts)
    terminal_ids = {row.terminal_outcome_id for row in receipt.terminal_outcomes}
    assert {row.terminal_outcome_id for row in receipt.route_decisions} == terminal_ids
    assert all(summary.request_denominator == 6 for summary in receipt.trial_summaries)
