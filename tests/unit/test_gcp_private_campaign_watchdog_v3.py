"""Acceptance tests for the independent two-A100 private-campaign watchdog."""

from __future__ import annotations

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
        value.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
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
    journal: GcpPrivateCampaignJournal, proposal: GcpPrivateCampaignProposal
) -> None:
    now = datetime.now(UTC)
    journal.append(proposal, state="CREATE_INTENT_DURABLE", occurred_at=now)
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


def _request(
    proposal: GcpPrivateCampaignProposal, startup: object
) -> GcpPrivateCampaignCreateRequest:
    return build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )


def _append_exact_binding(
    journal: GcpPrivateCampaignJournal,
    proposal: GcpPrivateCampaignProposal,
) -> None:
    now = datetime.now(UTC)
    journal.append(proposal, state="CREATE_INTENT_DURABLE", occurred_at=now)
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


def test_watchdog_worker_recovers_post_insert_before_identity_binding(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal)
    _write_fake_state(
        watchdog_root, proposal, instance_present=True, boot_disk_present=True
    )
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
        _parent_process_id=2_000_000_000,
    )

    capability = watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )

    states = _await_terminal(watchdog_root, proposal)
    state = read_gcp_private_campaign_file_fake_worker_state(
        watchdog_root=watchdog_root,
        controller_id=proposal.ownership_labels.controller_id,
    )
    assert capability._worker_pid != os.getpid()
    assert states[-1] == "CLEANUP_CONFIRMED"
    assert "PREBIND_ABSENCE_PENDING" not in states
    assert state.instance_present is False
    assert state.boot_disk_present is False
    assert state.delete_calls == 1
    assert state.boot_disk_delete_calls == 1
    assert journal.load(proposal)[-1].state == "CLEANUP_CONFIRMED"


def test_watchdog_requires_two_exact_absence_observations(tmp_path: Path) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal)
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
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )

    states = _await_terminal(watchdog_root, proposal)
    assert states[-1] == "CLEANUP_CONFIRMED"
    assert states.count("PREBIND_ABSENCE_PENDING") == 1
    assert journal.load(proposal)[-1].state == "CLEANUP_CONFIRMED"


def test_hung_parent_deadline_and_cleanup_retry_are_reconciled(tmp_path: Path) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal)
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
    )
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=1)

    watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
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
    _append_create_intent(journal, proposal)
    _write_fake_state(
        watchdog_root,
        proposal,
        instance_present=True,
        boot_disk_present=True,
        invisible_observations_remaining=1,
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
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )

    states = _await_terminal(watchdog_root, proposal)
    state = read_gcp_private_campaign_file_fake_worker_state(
        watchdog_root=watchdog_root,
        controller_id=proposal.ownership_labels.controller_id,
    )
    assert states.count("RECOVERY_ATTEMPT") == 1
    assert "PREBIND_ABSENCE_PENDING" not in states
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
    _append_create_intent(journal, proposal)
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


def test_watchdog_journal_restart_resumes_only_after_parent_death(
    tmp_path: Path,
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal)
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
    states = _await_terminal(watchdog_root, proposal)
    assert restarted_pid != os.getpid()
    assert states.count("WORKER_READY") == 2
    assert states[-1] == "CLEANUP_CONFIRMED"


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
    _append_exact_binding(journal, proposal)
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
    capability = watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
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
    assert capability._worker_pid != os.getpid()
    assert watchdog._handle is not None
    watchdog._kill_process(watchdog._handle.process)


def test_dead_worker_blocks_google_factory_before_sdk_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal, startup, approval, cleanup = _fresh_inputs()
    journal_root = _private_root(tmp_path, "journal")
    watchdog_root = _private_root(tmp_path, "watchdog")
    journal = GcpPrivateCampaignJournal(journal_root)
    _append_create_intent(journal, proposal)
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
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )
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
    _append_create_intent(journal, proposal)
    _write_fake_state(
        watchdog_root, proposal, instance_present=False, boot_disk_present=False
    )
    watchdog = GcpPrivateCampaignWatchdog(
        watchdog_root=watchdog_root,
        journal_root=journal_root,
        worker_backend="file_fake_cleanup_v1",
    )
    capability = watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
        execution_deadline=journal.execution_deadline(proposal),
        now=datetime.now(UTC),
    )
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
    _append_create_intent(journal, proposal)
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
    capability = watchdog.activate(
        proposal=proposal,
        approval=approval,
        cleanup_authorization=cleanup,
        request=_request(proposal, startup),
        execution_deadline=now + timedelta(seconds=5),
        now=now,
    )
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
