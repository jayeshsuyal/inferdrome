"""Offline command tests: preview and rejected execute stay before live factory."""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any, cast

from inferdrome.deployment.gcp_private_campaign_v2 import (
    GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION,
    GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION,
    GcpPrivateCampaignApproval,
    canonical_gcp_private_campaign_proposal_bytes,
    canonical_gcp_private_campaign_startup_bytes,
)
from tests.unit.test_gcp_private_campaign_v2 import _proposal


def _command() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    return runpy.run_path(str(root / "scripts/gcp_private_campaign_v2.py"))


def test_preview_is_offline_and_expired_execute_does_not_construct_live_adapter(
    tmp_path: Path, capsys: Any
) -> None:
    proposal, startup = _proposal()
    proposal_path = tmp_path / "proposal.json"
    startup_path = tmp_path / "startup.json"
    proposal_path.write_bytes(canonical_gcp_private_campaign_proposal_bytes(proposal))
    startup_path.write_bytes(canonical_gcp_private_campaign_startup_bytes(startup))
    command = _command()
    factory_calls = 0

    def factory(*, evidence_root: Path) -> object:
        nonlocal factory_calls
        del evidence_root
        factory_calls += 1
        raise AssertionError("live adapter factory must not run")

    command["create_google_private_campaign_transport"] = factory
    main = cast(Any, command["main"])

    assert (
        main(
            [
                "preview",
                "--proposal",
                str(proposal_path),
                "--startup",
                str(startup_path),
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["provider_call_performed"] is False
    assert preview["proposal_id"] == proposal.proposal_id
    assert factory_calls == 0

    approval = GcpPrivateCampaignApproval(
        schema_version=GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION,
        approval_kind="exact_human_campaign_approval",
        confirmation=GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION,
        human_approval_record_id="approval-expired-cli",
        approved_at="2026-09-02T00:00:00Z",
        expires_at="2026-09-02T00:00:01Z",
        proposal=proposal,
        proposal_digest=proposal.proposal_id,
    )
    approval_path = tmp_path / "approval.json"
    approval_path.write_bytes(
        json.dumps(
            approval.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    journal = tmp_path / "journal"
    evidence = tmp_path / "evidence"
    journal.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)

    assert (
        main(
            [
                "execute",
                "--proposal",
                str(proposal_path),
                "--startup",
                str(startup_path),
                "--approval",
                str(approval_path),
                "--journal-root",
                str(journal),
                "--evidence-root",
                str(evidence),
            ]
        )
        == 2
    )
    failure = json.loads(capsys.readouterr().out)
    assert failure == {"error": "APPROVAL_EXPIRED"}
    assert factory_calls == 0
