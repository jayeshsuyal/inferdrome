"""Offline command tests: v0.2 execute stays before every live factory edge."""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any, cast

import pytest

import inferdrome.deployment.gcp_private_campaign_google as campaign_google
from inferdrome.deployment.gcp_private_campaign_v2 import (
    GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION,
    GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION,
    GcpPrivateCampaignApproval,
    GcpPrivateCampaignTransportError,
    canonical_gcp_private_campaign_proposal_bytes,
    canonical_gcp_private_campaign_startup_bytes,
)
from tests.unit.test_gcp_private_campaign_v2 import _proposal


def _command() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    return runpy.run_path(str(root / "scripts/gcp_private_campaign_v2.py"))


def test_preview_is_offline_and_execute_is_watchdog_fail_closed(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal, startup = _proposal()
    proposal_path = tmp_path / "proposal.json"
    startup_path = tmp_path / "startup.json"
    proposal_path.write_bytes(canonical_gcp_private_campaign_proposal_bytes(proposal))
    startup_path.write_bytes(canonical_gcp_private_campaign_startup_bytes(startup))
    main = cast(Any, _command()["main"])
    main_globals = cast(dict[str, Any], main.__globals__)
    approval_parse_calls = 0
    controller_calls = 0
    factory_calls = 0
    sdk_calls = 0
    insert_calls = 0

    def forbidden_approval_parse(*args: object, **kwargs: object) -> object:
        nonlocal approval_parse_calls
        del args, kwargs
        approval_parse_calls += 1
        raise AssertionError("execute must not parse the supplied approval")

    def forbidden_controller(*args: object, **kwargs: object) -> object:
        nonlocal controller_calls
        del args, kwargs
        controller_calls += 1
        raise AssertionError("execute must not construct a lifecycle controller")

    def forbidden_factory(*args: object, **kwargs: object) -> object:
        nonlocal factory_calls
        del args, kwargs
        factory_calls += 1
        raise AssertionError("execute must not reach the create-capable factory")

    def forbidden_sdk() -> object:
        nonlocal sdk_calls
        sdk_calls += 1
        raise AssertionError("execute must not import the optional Google SDK")

    def forbidden_insert(*args: object, **kwargs: object) -> object:
        nonlocal insert_calls
        del args, kwargs
        insert_calls += 1
        raise AssertionError("execute must not reach InstancesClient.insert")

    monkeypatch.setitem(main_globals, "_controller", forbidden_controller)
    monkeypatch.setattr(campaign_google, "_google_sdk", forbidden_sdk)
    monkeypatch.setattr(
        campaign_google.GoogleGcpPrivateCampaignTransport,
        "create_instance",
        forbidden_insert,
    )

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
    assert controller_calls == sdk_calls == insert_calls == 0

    approval = GcpPrivateCampaignApproval(
        schema_version=GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION,
        approval_kind="exact_human_campaign_approval",
        confirmation=GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION,
        human_approval_record_id="approval-valid-cli",
        approved_at="2026-09-02T00:00:00Z",
        expires_at="2026-09-02T00:10:00Z",
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

    # Patch only after the offline preview: executing with a syntactically
    # valid approval must still stop before approval parsing or any live seam.
    monkeypatch.setitem(main_globals, "_parse", forbidden_approval_parse)
    monkeypatch.setattr(
        campaign_google,
        "create_google_private_campaign_transport",
        forbidden_factory,
    )

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
    assert failure == {
        "cleanup_status": "NOT_REQUIRED",
        "error": "LIVE_EXECUTION_WATCHDOG_UNAVAILABLE",
        "provider_call_performed": False,
    }
    assert (
        approval_parse_calls
        == controller_calls
        == factory_calls
        == sdk_calls
        == insert_calls
        == 0
    )
    assert not list(journal.iterdir())


def test_create_capable_google_factory_fails_before_optional_sdk_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk_calls = 0

    def forbidden_sdk() -> object:
        nonlocal sdk_calls
        sdk_calls += 1
        raise AssertionError("watchdog-disabled factory must not import an SDK")

    monkeypatch.setattr(campaign_google, "_google_sdk", forbidden_sdk)

    with pytest.raises(
        GcpPrivateCampaignTransportError,
        match="LIVE_EXECUTION_WATCHDOG_UNAVAILABLE",
    ):
        campaign_google.create_google_private_campaign_transport(evidence_root=tmp_path)

    assert sdk_calls == 0
