"""The active v0.3 story is a closed, machine-readable claim boundary."""

from __future__ import annotations

import json

import pytest

from inferdrome.cli import main
from inferdrome.v0_3_capabilities import V0_3_CAPABILITY_CONTRACT


def test_v0_3_capability_contract_is_complete_and_non_verdicting() -> None:
    contract = V0_3_CAPABILITY_CONTRACT
    assert contract.schema_version == "inferdrome.v0_3_capabilities.v1"
    assert contract.product_role == "MEASUREMENT_EVIDENCE_ONLY"
    facts = {entry.capability_id: entry for entry in contract.capabilities}
    assert set(facts) == {
        "local_routing_execution",
        "external_router_evidence_adapter",
        "historical_a10_serving_evidence",
        "two_a100_multi_endpoint_campaign",
        "gcp_operation",
        "kubernetes_operation",
    }
    assert facts["local_routing_execution"].status == "PROVEN_LOCAL_SOCKET_LEVEL"
    assert (
        facts["external_router_evidence_adapter"].status
        == "LOCAL_FIXTURE_VALIDATED"
    )
    assert facts["historical_a10_serving_evidence"].status == "PRESERVED_EXTERNAL_ONLY"
    assert facts["two_a100_multi_endpoint_campaign"].status == "UNEXECUTED"
    assert facts["gcp_operation"].status == "LOCAL_FAKE_VALIDATED"
    assert facts["kubernetes_operation"].status == "NOT_CLAIMED"
    assert contract.authorities_not_provided == (
        "PRODUCTION_ROUTING",
        "POLICY_VERDICT",
        "PROMOTION_CONTROL",
        "ACCEPTANCE_VERDICT",
    )
    rendered = json.dumps(contract.model_dump(mode="json"), sort_keys=True)
    assert "PASS" not in rendered
    assert "FAIL" not in rendered
    assert "NOT_PROVEN" not in rendered


def test_capabilities_cli_renders_the_active_v0_3_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["capabilities"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == V0_3_CAPABILITY_CONTRACT.model_dump(
        mode="json"
    )
    assert captured.err == ""
