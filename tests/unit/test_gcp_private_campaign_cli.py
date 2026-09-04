"""Offline command tests: preview and rejected execute stay before live factory."""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any, cast

from inferdrome.deployment.gcp_private_campaign_v2 import (
    GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION,
    GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION,
    GCP_PRIVATE_CAMPAIGN_CLEANUP_AUTHORIZATION_SCHEMA_VERSION,
    GCP_PRIVATE_CAMPAIGN_CLEANUP_CONFIRMATION,
    GcpPrivateCampaignApproval,
    GcpPrivateCampaignCleanupAuthorization,
    canonical_gcp_private_campaign_proposal_bytes,
    canonical_gcp_private_campaign_startup_bytes,
)
from tests.unit.test_gcp_private_campaign_v2 import _proposal


def _command() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    return runpy.run_path(str(root / "scripts/gcp_private_campaign_v2.py"))


def test_preview_is_offline_and_expired_execute_does_not_construct_live_adapter(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    proposal, startup = _proposal()
    proposal_path = tmp_path / "proposal.json"
    startup_path = tmp_path / "startup.json"
    proposal_path.write_bytes(canonical_gcp_private_campaign_proposal_bytes(proposal))
    startup_path.write_bytes(canonical_gcp_private_campaign_startup_bytes(startup))
    command = _command()
    factory_calls = 0

    def factory(*, evidence_root: Path, watchdog_capability: object) -> object:
        nonlocal factory_calls
        del evidence_root, watchdog_capability
        factory_calls += 1
        raise AssertionError("live adapter factory must not run")

    main = cast(Any, command["main"])
    # ``runpy.run_path`` returns a copy of the globals mapping.  Patch the
    # function's actual global namespace so this test proves the live factory
    # seam instead of mutating an inert dictionary.
    monkeypatch.setitem(
        main.__globals__, "create_google_private_campaign_transport", factory
    )

    controller_calls = 0

    def forbidden_controller(_: object) -> object:
        nonlocal controller_calls
        controller_calls += 1
        raise AssertionError("controller must stay behind approval validation")

    monkeypatch.setitem(main.__globals__, "_controller", forbidden_controller)

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
    cleanup = GcpPrivateCampaignCleanupAuthorization(
        schema_version=GCP_PRIVATE_CAMPAIGN_CLEANUP_AUTHORIZATION_SCHEMA_VERSION,
        authorization_kind="exact_cleanup_recovery",
        confirmation=GCP_PRIVATE_CAMPAIGN_CLEANUP_CONFIRMATION,
        cleanup_record_id="cleanup-expired-cli",
        authorized_at="2026-09-02T00:00:00Z",
        proposal=proposal,
        proposal_digest=proposal.proposal_id,
        cleanup_request_id=proposal.request_ids.delete_request_id,
    )
    cleanup_path = tmp_path / "cleanup.json"
    cleanup_path.write_bytes(
        json.dumps(
            cleanup.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    journal = tmp_path / "journal"
    evidence = tmp_path / "evidence"
    watchdog = tmp_path / "watchdog"
    journal.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)
    watchdog.mkdir(mode=0o700)

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
                "--cleanup-authorization",
                str(cleanup_path),
                "--journal-root",
                str(journal),
                "--evidence-root",
                str(evidence),
                "--watchdog-root",
                str(watchdog),
            ]
        )
        == 2
    )
    failure = json.loads(capsys.readouterr().out)
    assert failure == {"error": "APPROVAL_EXPIRED"}
    assert controller_calls == 0
    assert factory_calls == 0
