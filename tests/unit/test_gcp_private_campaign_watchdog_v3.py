"""Acceptance tests for the independent two-A100 private-campaign watchdog."""

from __future__ import annotations

import multiprocessing
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from inferdrome.deployment.gcp_private_campaign_google import (
    create_google_private_campaign_transport,
)
from inferdrome.deployment.gcp_private_campaign_v2 import (
    GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION,
    GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION,
    GCP_PRIVATE_CAMPAIGN_CLEANUP_AUTHORIZATION_SCHEMA_VERSION,
    GCP_PRIVATE_CAMPAIGN_CLEANUP_CONFIRMATION,
    FakeGcpPrivateCampaignTransport,
    GcpPrivateCampaignApproval,
    GcpPrivateCampaignCleanupAuthorization,
    GcpPrivateCampaignCreateRequest,
    GcpPrivateCampaignError,
    GcpPrivateCampaignJournal,
    GcpPrivateCampaignJournalIdentity,
    GcpPrivateCampaignLifecycleController,
    GcpPrivateCampaignProposal,
    GcpPrivateCampaignProposalPayload,
    build_gcp_private_campaign_create_request,
    issue_gcp_private_campaign_proposal,
)
from inferdrome.deployment.gcp_private_campaign_watchdog_v3 import (
    GcpPrivateCampaignFileFakeWorkerState,
    GcpPrivateCampaignWatchdog,
    _WatchdogStore,
    read_gcp_private_campaign_file_fake_worker_state,
    write_gcp_private_campaign_file_fake_worker_state,
)
from inferdrome.routing_execution.canonical import sha256_digest
from tests.unit.test_gcp_private_campaign_v2 import _proposal


def _timestamp(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _fresh_inputs() -> tuple[
    GcpPrivateCampaignProposal,
    object,
    GcpPrivateCampaignApproval,
    GcpPrivateCampaignCleanupAuthorization,
]:
    now = datetime.now(UTC).replace(microsecond=0)
    proposal, startup = _proposal()
    payload = proposal.model_dump(mode="python")
    payload.pop("proposal_id")
    payload["quote"] = proposal.quote.model_copy(
        update={
            "quoted_at": _timestamp(now - timedelta(minutes=2)),
            "expires_at": _timestamp(now + timedelta(minutes=15)),
        }
    )
    fresh = issue_gcp_private_campaign_proposal(
        GcpPrivateCampaignProposalPayload.model_validate(payload)
    )
    approval = GcpPrivateCampaignApproval(
        schema_version=GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION,
        approval_kind="exact_human_campaign_approval",
        confirmation=GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION,
        human_approval_record_id="watchdog-live-test",
        approved_at=_timestamp(now - timedelta(minutes=1)),
        expires_at=_timestamp(now + timedelta(minutes=10)),
        proposal=fresh,
        proposal_digest=fresh.proposal_id,
    )
    cleanup = GcpPrivateCampaignCleanupAuthorization(
        schema_version=GCP_PRIVATE_CAMPAIGN_CLEANUP_AUTHORIZATION_SCHEMA_VERSION,
        authorization_kind="exact_cleanup_recovery",
        confirmation=GCP_PRIVATE_CAMPAIGN_CLEANUP_CONFIRMATION,
        cleanup_record_id="watchdog-cleanup-test",
        authorized_at=_timestamp(now - timedelta(minutes=1)),
        proposal=fresh,
        proposal_digest=fresh.proposal_id,
        cleanup_request_id=fresh.request_ids.delete_request_id,
    )
    return fresh, startup, approval, cleanup


def _private_root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return root


def _append_create_intent(
    journal: GcpPrivateCampaignJournal,
    proposal: GcpPrivateCampaignProposal,
    startup: object,
) -> None:
    now = datetime.now(UTC)
    journal.append(
        proposal,
        state="CREATE_INTENT_DURABLE",
        occurred_at=now,
        detail_digest=_request(proposal, startup).create_request_digest,
    )
    journal.append(proposal, state="CREATE_INTENT", occurred_at=now)


def _write_fake_state(
    root: Path,
    proposal: GcpPrivateCampaignProposal,
    *,
    instance_present: bool,
    boot_disk_present: bool,
    invisible_observations_remaining: int = 0,
    delete_failures_remaining: int = 0,
    replacement_provider_instance_id: str | None = None,
    ambiguous_residuals: bool = False,
    create_request_digest_mismatch: bool = False,
) -> None:
    write_gcp_private_campaign_file_fake_worker_state(
        watchdog_root=root,
        controller_id=proposal.ownership_labels.controller_id,
        state=GcpPrivateCampaignFileFakeWorkerState(
            schema_version="inferdrome.gcp-private-watchdog-file-fake.v1",
            proposal_id=proposal.proposal_id,
            instance_present=instance_present,
            boot_disk_present=boot_disk_present,
            invisible_observations_remaining=invisible_observations_remaining,
            delete_failures_remaining=delete_failures_remaining,
            replacement_provider_instance_id=replacement_provider_instance_id,
            ambiguous_residuals=ambiguous_residuals,
            create_request_digest_mismatch=create_request_digest_mismatch,
        ),
    )


def _await_terminal(
    root: Path, proposal: GcpPrivateCampaignProposal, *, timeout_seconds: float = 5.0
) -> tuple[str, ...]:
    store = _WatchdogStore(root)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        record = store.read_activation(proposal.ownership_labels.controller_id)
        events = store.load_events(record)
        if events and events[-1].state in {
            "CLEANUP_CONFIRMED",
            "CLEANUP_UNCONFIRMED",
            "STOPPED",
        }:
            return tuple(event.state for event in events)
        time.sleep(0.05)
    pytest.fail("watchdog worker did not reach a terminal local state")


def _await_state(
    root: Path,
    proposal: GcpPrivateCampaignProposal,
    state: str,
    *,
    timeout_seconds: float = 3.0,
) -> tuple[str, ...]:
    store = _WatchdogStore(root)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        record = store.read_activation(proposal.ownership_labels.controller_id)
        events = store.load_events(record)
        states = tuple(event.state for event in events)
        if state in states:
            return states
        time.sleep(0.05)
    pytest.fail(f"watchdog worker did not record {state}")


def _request(
    proposal: GcpPrivateCampaignProposal, startup: object
) -> GcpPrivateCampaignCreateRequest:
    return build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )


def _append_exact_binding(
    journal: GcpPrivateCampaignJournal,
    proposal: GcpPrivateCampaignProposal,
    startup: object,
) -> None:
    now = datetime.now(UTC)
    journal.append(
        proposal,
        state="CREATE_INTENT_DURABLE",
        occurred_at=now,
        detail_digest=_request(proposal, startup).create_request_digest,
    )
    journal.append(proposal, state="CREATE_INTENT", occurred_at=now)
    journal.append(proposal, state="CREATE_RECONCILING", occurred_at=now)
    journal.append(proposal, state="CREATED", occurred_at=now)
    journal.append(
        proposal,
        state="INSTANCE_IDENTITY_BOUND",
        occurred_at=now,
        detail_digest=sha256_digest(b"123456789"),
    )
    journal.append(
        proposal,
        state="BOOT_DISK_IDENTITY_BOUND",
        occurred_at=now,
        detail_digest=sha256_digest(b"246813579"),
    )
    journal.append(
        proposal,
        state="PROVIDER_BACKSTOP_VERIFIED",
        occurred_at=now,
        detail_digest="sha256:" + ("c" * 64),
    )


def _stalled_creator_process(
    journal_root_text: str,
    watchdog_root_text: str,
    ready_marker_text: str,
) -> None:
    """Local-only creator deliberately stopped inside the insert seam."""

    journal_root = Path(journal_root_text)
    watchdog_root = Path(watchdog_root_text)
    ready_marker = Path(ready_marker_text)
    late_marker = ready_marker.with_suffix(".late")
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    _write_fake_state(
        watchdog_root, proposal, instance_present=True, boot_disk_present=True
    )
    request = _request(proposal, startup)
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
        worker_poll_seconds=0.02,
    )
    now = datetime.now(UTC)
    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=request,
        journal_identity=journal.identity(proposal),
        # This test exercises parent-death fencing, not expiry.  Leave enough
        # activation margin that CI process startup cannot accidentally turn
        # it into a wall-clock deadline test.
        execution_deadline=now + timedelta(seconds=30),
        now=now,
    )
    journal.claim_create_for_factory(
        proposal,
        request_digest=request.create_request_digest,
        occurred_at=now,
    )
    # This is the exact durable point reached before an outbound insert.  The
    # process intentionally never returns, so the detached worker must fence
    # this one PID and complete exact cleanup without a parent lock release.
    journal.begin_create_submission(
        proposal,
        request_digest=request.create_request_digest,
        occurred_at=now,
    )
    ready_marker.write_text(proposal.proposal_id, encoding="utf-8")
    time.sleep(30)
    late_marker.write_text("late-insert", encoding="utf-8")


def test_watchdog_fences_a_stalled_creator_before_cleanup(
    tmp_path: Path,
) -> None:
    """The detached worker fences a blocked creator without any provider call."""

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("requires a local fork process for exact-PID fencing")
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    ready_marker = tmp_path / "creator-ready"
    process = multiprocessing.get_context("fork").Process(
        target=_stalled_creator_process,
        args=(
            os.fspath(journal_root),
            os.fspath(watchdog_root),
            os.fspath(ready_marker),
        ),
    )
    process.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not ready_marker.exists():
            time.sleep(0.02)
        assert ready_marker.exists()
        process.join(timeout=5)
        assert process.is_alive() is False
        assert process.exitcode is not None and process.exitcode != 0

        store = _WatchdogStore(watchdog_root)
        record = store.read_activation("pcctl-12345678")
        states = _await_terminal(
            watchdog_root, record.binding.proposal, timeout_seconds=8
        )
        assert "RECOVERY_ATTEMPT" in states
        assert states[-1] == "CLEANUP_CONFIRMED"
        assert (
            GcpPrivateCampaignJournal(journal_root)
            .load(record.binding.proposal)[4]
            .state
            == "CREATE_FENCE_PENDING"
        )
        state = read_gcp_private_campaign_file_fake_worker_state(
            watchdog_root=watchdog_root,
            controller_id=record.binding.proposal.ownership_labels.controller_id,
        )
        assert state.instance_present is False
        assert state.boot_disk_present is False
        assert ready_marker.with_suffix(".late").exists() is False
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=1)


def test_watchdog_worker_recovers_post_insert_before_identity_binding(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    _write_fake_state(
        watchdog_root, proposal, instance_present=True, boot_disk_present=True
    )
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
        _parent_process_id=2_000_000_000,
    )

    activation = watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )

    states = _await_terminal(watchdog_root, proposal)
    state = read_gcp_private_campaign_file_fake_worker_state(
        watchdog_root=watchdog_root,
        controller_id=proposal.ownership_labels.controller_id,
    )
    assert activation._worker_pid != os.getpid()
    assert states[-1] == "CLEANUP_CONFIRMED"
    assert "PREBIND_ABSENCE_PENDING" not in states
    assert state.instance_present is False
    assert state.boot_disk_present is False
    assert state.delete_calls == 1
    assert state.boot_disk_delete_calls == 1
    assert journal.load(proposal)[-1].state == "CLEANUP_CONFIRMED"


def test_prebind_absence_stays_unconfirmed_without_authoritative_create_record(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    _write_fake_state(
        watchdog_root, proposal, instance_present=False, boot_disk_present=False
    )
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
        _parent_process_id=2_000_000_000,
    )
    request = _request(proposal, startup)

    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=request,
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )

    states = _await_state(watchdog_root, proposal, "PREBIND_ABSENCE_PENDING")
    assert states.count("PREBIND_ABSENCE_PENDING") == 1
    time.sleep(1.2)
    events = journal.load(proposal)
    assert events[-1].state == "CLEANUP_INTENT"
    assert all(event.state != "CLEANUP_CONFIRMED" for event in events)
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_dead_parent_cleanup_retry_is_reconciled_without_wall_clock_deadline_race(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    _write_fake_state(
        watchdog_root,
        proposal,
        instance_present=True,
        boot_disk_present=True,
        delete_failures_remaining=8,
    )
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
        _parent_process_id=2_000_000_000,
    )
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=60)

    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
        journal_identity=journal.identity(proposal),
        execution_deadline=deadline,
        now=now,
    )

    states = _await_terminal(watchdog_root, proposal)
    state = read_gcp_private_campaign_file_fake_worker_state(
        watchdog_root=watchdog_root,
        controller_id=proposal.ownership_labels.controller_id,
    )
    assert states.count("RECOVERY_ATTEMPT") == 1
    assert states[-1] == "CLEANUP_CONFIRMED"
    assert state.delete_attempts == 9
    assert state.delete_calls == 1
    assert len(states) < 64
    assert journal.load(proposal)[-1].state == "CLEANUP_CONFIRMED"


def test_false_absence_never_settles_a_real_instance(tmp_path: Path) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    _write_fake_state(
        watchdog_root,
        proposal,
        instance_present=True,
        boot_disk_present=True,
        # Both the direct observation and the immediately following absence
        # confirmation report not-found once.  The later visible resource
        # must be reconciled/deleted rather than terminal-confirmed early.
        invisible_observations_remaining=2,
    )
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
        _parent_process_id=2_000_000_000,
    )

    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )

    states = _await_terminal(watchdog_root, proposal)
    state = read_gcp_private_campaign_file_fake_worker_state(
        watchdog_root=watchdog_root,
        controller_id=proposal.ownership_labels.controller_id,
    )
    assert states.count("RECOVERY_ATTEMPT") == 1
    assert states.count("PREBIND_ABSENCE_PENDING") == 1
    assert state.invisible_observations_remaining == 0
    assert state.delete_attempts == 1
    assert state.delete_calls == 1
    assert journal.load(proposal)[-1].state == "CLEANUP_CONFIRMED"


def test_prebind_same_name_replacement_without_request_anchor_never_deletes(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    _write_fake_state(
        watchdog_root,
        proposal,
        instance_present=True,
        boot_disk_present=True,
        create_request_digest_mismatch=True,
    )
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
        _parent_process_id=2_000_000_000,
    )
    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        record = _WatchdogStore(watchdog_root).read_activation(
            proposal.ownership_labels.controller_id
        )
        if any(
            event.state == "RECOVERY_ATTEMPT"
            for event in _WatchdogStore(watchdog_root).load_events(record)
        ):
            break
        time.sleep(0.05)
    else:
        pytest.fail("worker did not attempt pre-bind reconciliation")
    state = read_gcp_private_campaign_file_fake_worker_state(
        watchdog_root=watchdog_root,
        controller_id=proposal.ownership_labels.controller_id,
    )
    assert state.instance_present is True
    assert state.delete_calls == 0
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_watchdog_journal_restart_preserves_prebind_absence_as_unconfirmed(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    _write_fake_state(
        watchdog_root, proposal, instance_present=False, boot_disk_present=False
    )
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
        _parent_process_id=2_000_000_000,
    )
    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )

    store = _WatchdogStore(watchdog_root)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        record = store.read_activation(proposal.ownership_labels.controller_id)
        events = store.load_events(record)
        if events and events[-1].state == "PREBIND_ABSENCE_PENDING":
            break
        time.sleep(0.05)
    else:
        pytest.fail("worker did not persist the first exact-absence vote")
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)
    restarted_pid = watchdog.resume_cleanup_only(
        controller_id=proposal.ownership_labels.controller_id
    )
    # The restart must ACK the durable cleanup-only state, but without a
    # provider create-operation identity it may never promote prior absence to
    # terminal cleanup proof.  This is deliberately a live recovery worker,
    # not a new create authority.
    time.sleep(1.2)
    record = store.read_activation(proposal.ownership_labels.controller_id)
    states = tuple(event.state for event in store.load_events(record))
    assert restarted_pid != os.getpid()
    assert states.count("WORKER_READY") == 2
    assert "CLEANUP_CONFIRMED" not in states
    assert journal.load(proposal)[-1].state == "CLEANUP_INTENT"
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


@pytest.mark.parametrize(
    ("replacement_provider_instance_id", "ambiguous_residuals"),
    (("987654321", False), (None, True)),
)
def test_replacement_or_ambiguous_owned_resource_is_never_deleted(
    tmp_path: Path,
    replacement_provider_instance_id: str | None,
    ambiguous_residuals: bool,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_exact_binding(journal, proposal, startup)
    _write_fake_state(
        watchdog_root,
        proposal,
        instance_present=True,
        boot_disk_present=True,
        replacement_provider_instance_id=replacement_provider_instance_id,
        ambiguous_residuals=ambiguous_residuals,
    )
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
        _parent_process_id=2_000_000_000,
    )
    activation = watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        record = _WatchdogStore(watchdog_root).read_activation(
            proposal.ownership_labels.controller_id
        )
        if any(
            event.state == "RECOVERY_ATTEMPT"
            for event in _WatchdogStore(watchdog_root).load_events(record)
        ):
            break
        time.sleep(0.05)
    else:
        pytest.fail("worker did not attempt bounded cleanup")
    state = read_gcp_private_campaign_file_fake_worker_state(
        watchdog_root=watchdog_root,
        controller_id=proposal.ownership_labels.controller_id,
    )
    assert state.delete_calls == 0
    assert state.boot_disk_delete_calls == 0
    assert state.instance_present is True
    # A valid post-create history can arm only the cleanup actor.  It cannot
    # be repurposed into an SDK/client/insert capability.
    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_CREATE_INTENT_INVALID"):
        watchdog.take_create_capability()
    assert activation._worker_pid != os.getpid()
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_cleanup_ready_history_cannot_mint_a_live_create_capability(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_exact_binding(journal, proposal, startup)
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
    )

    activation = watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )

    assert activation._worker_pid != os.getpid()
    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_CREATE_INTENT_INVALID"):
        watchdog.take_create_capability()
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_cleanup_ready_claimed_history_cannot_mint_a_second_create_capability(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    request = _request(proposal, startup)
    journal.claim_create_for_factory(
        proposal,
        request_digest=request.create_request_digest,
        occurred_at=datetime.now(UTC),
    )
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
    )

    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=request,
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )

    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_CREATE_INTENT_INVALID"):
        watchdog.take_create_capability()
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_dead_worker_blocks_google_factory_before_sdk_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    _write_fake_state(
        watchdog_root, proposal, instance_present=False, boot_disk_present=False
    )
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
    )
    request = _request(proposal, startup)
    capability = watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=request,
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )
    capability = watchdog.take_create_capability()
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)
    sdk_calls = 0

    def forbidden_sdk() -> object:
        nonlocal sdk_calls
        sdk_calls += 1
        raise AssertionError("SDK import must stay behind watchdog liveness")

    monkeypatch.setattr(
        "inferdrome.deployment.gcp_private_campaign_google._google_sdk", forbidden_sdk
    )
    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_WORKER_NOT_ACTIVE"):
        create_google_private_campaign_transport(
            evidence_root=_private_root(tmp_path, "evidence"),
            watchdog_capability=capability,
        )
    assert sdk_calls == 0


@pytest.mark.parametrize("mutation", ("missing", "tampered"))
def test_missing_or_tampered_activation_blocks_sdk_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    _write_fake_state(
        watchdog_root, proposal, instance_present=False, boot_disk_present=False
    )
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
    )
    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )
    capability = watchdog.take_create_capability()
    activation = watchdog_root / (
        f"{proposal.ownership_labels.controller_id}."
        "gcp-private-watchdog-v3.activation.json"
    )
    if mutation == "missing":
        activation.unlink()
    else:
        activation.write_bytes(b"{}")
    sdk_calls = 0

    def forbidden_sdk() -> object:
        nonlocal sdk_calls
        sdk_calls += 1
        raise AssertionError("SDK import must stay behind activation validation")

    monkeypatch.setattr(
        "inferdrome.deployment.gcp_private_campaign_google._google_sdk", forbidden_sdk
    )
    with pytest.raises(GcpPrivateCampaignError):
        create_google_private_campaign_transport(
            evidence_root=_private_root(tmp_path, f"evidence-{mutation}"),
            watchdog_capability=capability,
        )
    assert sdk_calls == 0
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_stale_activation_blocks_sdk_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    _write_fake_state(
        watchdog_root, proposal, instance_present=False, boot_disk_present=False
    )
    now = datetime.now(UTC)
    clock_now = now

    def fake_now() -> datetime:
        return clock_now

    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
        _now=fake_now,
    )
    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
        journal_identity=journal.identity(proposal),
        execution_deadline=now + timedelta(seconds=5),
        now=now,
    )
    capability = watchdog.take_create_capability()
    clock_now = now + timedelta(seconds=6)
    sdk_calls = 0

    def forbidden_sdk() -> object:
        nonlocal sdk_calls
        sdk_calls += 1
        raise AssertionError("SDK import must stay behind activation freshness")

    monkeypatch.setattr(
        "inferdrome.deployment.gcp_private_campaign_google._google_sdk", forbidden_sdk
    )
    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_ACTIVATION_EXPIRED"):
        create_google_private_campaign_transport(
            evidence_root=_private_root(tmp_path, "evidence-stale"),
            watchdog_capability=capability,
        )
    assert sdk_calls == 0
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_missing_capability_blocks_sdk_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk_calls = 0

    def forbidden_sdk() -> object:
        nonlocal sdk_calls
        sdk_calls += 1
        raise AssertionError("SDK import must stay behind capability validation")

    monkeypatch.setattr(
        "inferdrome.deployment.gcp_private_campaign_google._google_sdk", forbidden_sdk
    )
    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_CAPABILITY_REQUIRED"):
        create_google_private_campaign_transport(
            evidence_root=_private_root(tmp_path, "evidence-missing"),
            watchdog_capability=object(),  # type: ignore[arg-type]
        )
    assert sdk_calls == 0


def test_activation_refuses_missing_controller_captured_intent_identity(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
    )

    # No controller could have captured this identity because no durable
    # CREATE_INTENT exists.  Activation must fail before it can spawn/ack a
    # worker or choose a fresh pathname baseline.
    with pytest.raises(GcpPrivateCampaignError, match="JOURNAL_IDENTITY_MISMATCH"):
        watchdog.activate(
            proposal=proposal,
            approval=approval,
            cleanup_authorization=cleanup,
            request=_request(proposal, startup),
            journal_identity=GcpPrivateCampaignJournalIdentity(
                root_device=0,
                root_inode=0,
                event_device=0,
                event_inode=0,
            ),
            execution_deadline=datetime.now(UTC) + timedelta(seconds=60),
            now=datetime.now(UTC),
        )


@pytest.mark.parametrize(
    "mutation",
    ("missing", "empty", "corrupt", "wrong_digest", "blocked", "replaced"),
)
def test_changed_core_create_intent_blocks_google_factory_before_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    request = _request(proposal, startup)
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="google_cleanup_only_v1",
    )
    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=request,
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )
    capability = watchdog.take_create_capability()
    history = journal_root / (
        f"{proposal.ownership_labels.controller_id}."
        "gcp-private-campaign-v2.events.jsonl"
    )
    if mutation == "missing":
        history.unlink()
    elif mutation == "empty":
        history.write_bytes(b"")
    elif mutation == "corrupt":
        history.write_bytes(b"{}\n")
    elif mutation == "wrong_digest":
        wrong_root = _private_root(tmp_path, "wrong-digest")
        wrong_journal = GcpPrivateCampaignJournal(wrong_root)
        wrong_journal.append(
            proposal,
            state="CREATE_INTENT_DURABLE",
            occurred_at=datetime.now(UTC),
            detail_digest="sha256:" + ("f" * 64),
        )
        wrong_journal.append(
            proposal, state="CREATE_INTENT", occurred_at=datetime.now(UTC)
        )
        history.write_bytes((wrong_root / history.name).read_bytes())
    elif mutation == "blocked":
        journal.append(proposal, state="BLOCKED", occurred_at=datetime.now(UTC))
    else:
        replacement_root = _private_root(tmp_path, "replacement")
        payload = proposal.model_dump(mode="python")
        payload.pop("proposal_id")
        payload["quote"] = proposal.quote.model_copy(
            update={"quote_id": "quote-87654321"}
        )
        replacement = issue_gcp_private_campaign_proposal(
            GcpPrivateCampaignProposalPayload.model_validate(payload)
        )
        replacement_journal = GcpPrivateCampaignJournal(replacement_root)
        _append_create_intent(replacement_journal, replacement, startup)
        replacement_history = replacement_root / history.name
        history.write_bytes(replacement_history.read_bytes())

    sdk_calls = 0

    def forbidden_sdk() -> object:
        nonlocal sdk_calls
        sdk_calls += 1
        raise AssertionError("SDK import must stay behind the core journal check")

    monkeypatch.setattr(
        "inferdrome.deployment.gcp_private_campaign_google._google_sdk", forbidden_sdk
    )
    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_CREATE_INTENT_INVALID"):
        create_google_private_campaign_transport(
            evidence_root=_private_root(tmp_path, f"evidence-{mutation}"),
            watchdog_capability=capability,
        )
    assert sdk_calls == 0
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_same_content_journal_file_replacement_blocks_factory_before_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    request = _request(proposal, startup)
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="google_cleanup_only_v1",
    )
    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=request,
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )
    capability = watchdog.take_create_capability()
    history = journal_root / (
        f"{proposal.ownership_labels.controller_id}."
        "gcp-private-campaign-v2.events.jsonl"
    )
    replacement = tmp_path / "same-content-history"
    replacement.write_bytes(history.read_bytes())
    replacement.chmod(0o600)
    os.replace(replacement, history)
    sdk_calls = 0

    def forbidden_sdk() -> object:
        nonlocal sdk_calls
        sdk_calls += 1
        raise AssertionError("SDK import must stay behind journal identity")

    monkeypatch.setattr(
        "inferdrome.deployment.gcp_private_campaign_google._google_sdk", forbidden_sdk
    )
    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_CREATE_INTENT_INVALID"):
        create_google_private_campaign_transport(
            evidence_root=_private_root(tmp_path, "evidence"),
            watchdog_capability=capability,
        )
    assert sdk_calls == 0
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_activation_rejects_a_copied_wrong_root_before_worker_ready(
    tmp_path: Path,
) -> None:
    """A watchdog cannot choose a copied journal as its own identity baseline."""

    proposal, startup, approval, cleanup = _fresh_inputs()
    intent_root = _private_root(tmp_path, "controller-intent")
    copied_root = _private_root(tmp_path, "copied-watchdog-root")
    watchdog_root = _private_root(tmp_path, "watchdog")
    controller_journal = GcpPrivateCampaignJournal(intent_root)
    _append_create_intent(controller_journal, proposal, startup)
    controller_identity = controller_journal.identity(proposal)
    history_name = (
        f"{proposal.ownership_labels.controller_id}."
        "gcp-private-campaign-v2.events.jsonl"
    )
    copied_history = copied_root / history_name
    copied_history.write_bytes((intent_root / history_name).read_bytes())
    copied_history.chmod(0o600)
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=copied_root,
        worker_backend="file_fake_cleanup_v1",
    )

    with pytest.raises(GcpPrivateCampaignError, match="JOURNAL_IDENTITY_MISMATCH"):
        watchdog.activate(
            proposal=proposal,
            approval=approval,
            cleanup_authorization=cleanup,
            request=_request(proposal, startup),
            journal_identity=controller_identity,
            execution_deadline=controller_journal.execution_deadline(proposal),
            now=datetime.now(UTC),
        )

    # The identity boundary is before activation persistence and worker spawn;
    # no READY receipt can make the copied path look self-authenticating.
    assert list(watchdog_root.iterdir()) == []


def test_same_content_journal_root_replacement_blocks_factory_before_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    request = _request(proposal, startup)
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="google_cleanup_only_v1",
    )
    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=request,
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )
    capability = watchdog.take_create_capability()
    history_name = (
        f"{proposal.ownership_labels.controller_id}."
        "gcp-private-campaign-v2.events.jsonl"
    )
    moved_root = tmp_path / "moved-original-journal"
    os.rename(journal_root, moved_root)
    journal_root.mkdir(mode=0o700)
    replacement = journal_root / history_name
    replacement.write_bytes((moved_root / history_name).read_bytes())
    replacement.chmod(0o600)
    sdk_calls = 0

    def forbidden_sdk() -> object:
        nonlocal sdk_calls
        sdk_calls += 1
        raise AssertionError("SDK import must stay behind journal root identity")

    monkeypatch.setattr(
        "inferdrome.deployment.gcp_private_campaign_google._google_sdk", forbidden_sdk
    )
    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_CREATE_INTENT_INVALID"):
        create_google_private_campaign_transport(
            evidence_root=_private_root(tmp_path, "evidence"),
            watchdog_capability=capability,
        )
    assert sdk_calls == 0
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_factory_to_insert_journal_swap_blocks_the_final_create_seam(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    request = _request(proposal, startup)
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="google_cleanup_only_v1",
    )
    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=request,
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )
    capability = watchdog.take_create_capability()
    capability.consume_for_factory()
    history = journal_root / (
        f"{proposal.ownership_labels.controller_id}."
        "gcp-private-campaign-v2.events.jsonl"
    )
    replacement = tmp_path / "factory-insert-swap"
    replacement.write_bytes(history.read_bytes())
    replacement.chmod(0o600)
    os.replace(replacement, history)

    with (
        pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_CREATE_INTENT_INVALID"),
        capability.insert_claim(request),
    ):
        pytest.fail("replaced journal reached the insert seam")
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_capability_is_one_use_and_bound_to_the_exact_create_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    request = _request(proposal, startup)
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="google_cleanup_only_v1",
    )
    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=request,
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )
    capability = watchdog.take_create_capability()
    sdk_calls = 0

    def fake_sdk() -> object:
        nonlocal sdk_calls
        sdk_calls += 1
        return object()

    monkeypatch.setattr(
        "inferdrome.deployment.gcp_private_campaign_google._google_sdk", fake_sdk
    )
    create_google_private_campaign_transport(
        evidence_root=_private_root(tmp_path, "evidence"),
        watchdog_capability=capability,
    )
    assert sdk_calls == 1
    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_CAPABILITY_REPLAYED"):
        create_google_private_campaign_transport(
            evidence_root=_private_root(tmp_path, "evidence-replay"),
            watchdog_capability=capability,
        )
    mismatched = request.model_copy(
        update={"create_request_digest": "sha256:" + ("f" * 64)}
    )
    with (
        pytest.raises(
            GcpPrivateCampaignError, match="WATCHDOG_CAPABILITY_BINDING_MISMATCH"
        ),
        capability.insert_claim(mismatched),
    ):
        pytest.fail("mismatched request reached the insert seam")
    assert sdk_calls == 1
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_file_fake_watchdog_never_authorizes_google_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal, startup)
    request = _request(proposal, startup)
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
    )
    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=request,
        journal_identity=journal.identity(proposal),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )
    capability = watchdog.take_create_capability()
    sdk_calls = 0

    def forbidden_sdk() -> object:
        nonlocal sdk_calls
        sdk_calls += 1
        raise AssertionError("file fake must not reach the Google SDK")

    monkeypatch.setattr(
        "inferdrome.deployment.gcp_private_campaign_google._google_sdk", forbidden_sdk
    )
    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_LIVE_BACKEND_REQUIRED"):
        create_google_private_campaign_transport(
            evidence_root=_private_root(tmp_path, "evidence"),
            watchdog_capability=capability,
        )
    assert sdk_calls == 0
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_watchdog_death_after_factory_stops_the_preinsert_guard(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()

    class Activation:
        active = True

        def assert_active(self) -> None:
            if not self.active:
                raise GcpPrivateCampaignError("WATCHDOG_WORKER_NOT_ACTIVE")

    class Watchdog:
        def __init__(self, activation: Activation) -> None:
            self._activation = activation

        def activate(self, **_: object) -> Activation:
            return self._activation

    class Transport(FakeGcpPrivateCampaignTransport):
        def __init__(self, activation: Activation) -> None:
            super().__init__()
            self._activation = activation

        def bind_pre_insert_guard(self, guard: object) -> None:
            super().bind_pre_insert_guard(guard)  # type: ignore[arg-type]
            self._activation.active = False

    activation = Activation()
    fake = Transport(activation)
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_private_root(tmp_path, "journal")),
        transport_factory=lambda: fake,
        watchdog=Watchdog(activation),
    )

    with pytest.raises(GcpPrivateCampaignError, match="WATCHDOG_WORKER_NOT_ACTIVE"):
        controller.execute(
            proposal=proposal,
            approval=approval,
            startup_payload=startup,
            cleanup_authorization=cleanup,
        )
    assert fake.create_calls == 0


def test_watchdog_missing_cleanup_authorization_stops_before_transport_factory(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, _ = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    factory_calls = 0

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal factory_calls
        factory_calls += 1
        return FakeGcpPrivateCampaignTransport()

    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(journal_root),
        transport_factory=factory,
        watchdog=GcpPrivateCampaignWatchdog(
            watchdog_root=watchdog_root,
            journal_root=journal_root,
            worker_backend="file_fake_cleanup_v1",
        ),
    )
    with pytest.raises(
        GcpPrivateCampaignError, match="WATCHDOG_CLEANUP_AUTHORIZATION_MISSING"
    ):
        controller.execute(
            proposal=proposal, approval=approval, startup_payload=startup
        )
    assert factory_calls == 0
