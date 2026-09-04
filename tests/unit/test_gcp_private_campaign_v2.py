"""Local/fake acceptance tests for the additive two-A100 pre-campaign closure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import inferdrome.deployment.gcp_private_campaign_v2 as campaign_v2
from inferdrome.deployment.gcp_private_campaign_v2 import (
    GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION,
    GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION,
    GCP_PRIVATE_CAMPAIGN_CLEANUP_AUTHORIZATION_SCHEMA_VERSION,
    GCP_PRIVATE_CAMPAIGN_CLEANUP_CONFIRMATION,
    GCP_PRIVATE_CAMPAIGN_MAX_EVENTS,
    GCP_PRIVATE_CAMPAIGN_PROPOSAL_SCHEMA_VERSION,
    GCP_PRIVATE_CAMPAIGN_TOPOLOGY_SCHEMA_VERSION,
    FakeGcpPrivateCampaignTransport,
    GcpPrivateCampaignApproval,
    GcpPrivateCampaignBootImage,
    GcpPrivateCampaignCleanupAuthorization,
    GcpPrivateCampaignError,
    GcpPrivateCampaignGuestServiceAccount,
    GcpPrivateCampaignIapConnectivity,
    GcpPrivateCampaignJournal,
    GcpPrivateCampaignLifecycleController,
    GcpPrivateCampaignOwnershipLabels,
    GcpPrivateCampaignProposal,
    GcpPrivateCampaignProposalPayload,
    GcpPrivateCampaignQuote,
    GcpPrivateCampaignRequestIds,
    GcpPrivateCampaignRoutingBinding,
    GcpPrivateCampaignTopology,
    _CleanupTransportFacade,
    build_gcp_private_campaign_create_request,
    gcp_private_campaign_boot_image_identity,
    gcp_private_campaign_proposal_id,
    issue_gcp_private_campaign_proposal,
    issue_gcp_private_campaign_startup_payload,
    verify_gcp_private_campaign_evidence_destination,
)
from inferdrome.qwen3_campaign import qwen3_workload_sha256
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.contracts import (
    ImageIdentity,
    fixed_policy_ids,
    fixed_r1_input_digests,
    fixed_selected_workload_sha256,
)


@dataclass(frozen=True)
class _Clock:
    instant: datetime = datetime(2026, 9, 2, tzinfo=UTC)
    monotonic_value: int = 0

    def now(self) -> datetime:
        return self.instant

    def monotonic_ns(self) -> int:
        return self.monotonic_value


@dataclass
class _SequenceClock:
    instants: tuple[datetime, ...]
    position: int = 0

    def now(self) -> datetime:
        index = min(self.position, len(self.instants) - 1)
        self.position += 1
        return self.instants[index]

    def monotonic_ns(self) -> int:
        return 0


def _reissued_proposal(
    proposal: GcpPrivateCampaignProposal,
    *,
    cleanup_horizon_seconds: int | None = None,
    max_runtime_seconds: int | None = None,
    quote_overrides: dict[str, object] | None = None,
    routing_overrides: dict[str, object] | None = None,
) -> GcpPrivateCampaignProposal:
    payload = proposal.model_dump(mode="python")
    payload.pop("proposal_id")
    if cleanup_horizon_seconds is not None:
        payload["cleanup_horizon_seconds"] = cleanup_horizon_seconds
    if max_runtime_seconds is not None:
        payload["max_runtime_seconds"] = max_runtime_seconds
    if quote_overrides is not None:
        payload["quote"].update(quote_overrides)
    if routing_overrides is not None:
        payload["routing"].update(routing_overrides)
    return issue_gcp_private_campaign_proposal(
        GcpPrivateCampaignProposalPayload.model_validate(payload)
    )


def _proposal() -> tuple[GcpPrivateCampaignProposal, object]:
    runner = ImageIdentity(reference="example/inferdrome-runner@sha256:" + ("1" * 64))
    serving = ImageIdentity(reference="example/inferdrome-serving@sha256:" + ("2" * 64))
    startup = issue_gcp_private_campaign_startup_payload(
        source_commit="1" * 40, runner_image=runner, serving_image=serving
    )
    inputs = fixed_r1_input_digests()
    payload = GcpPrivateCampaignProposalPayload(
        schema_version=GCP_PRIVATE_CAMPAIGN_PROPOSAL_SCHEMA_VERSION,
        proposal_kind="gcp_private_two_a100_precampaign",
        source_commit="1" * 40,
        topology=GcpPrivateCampaignTopology(
            schema_version=GCP_PRIVATE_CAMPAIGN_TOPOLOGY_SCHEMA_VERSION,
            provider="gcp-compute-engine",
            project_id="inferdrome-lab",
            region="us-central1",
            zone="us-central1-a",
            machine_type="a2-highgpu-2g",
            accelerator_model="NVIDIA A100-SXM4-40GB",
            accelerator_provider_type="nvidia-tesla-a100",
            accelerator_count=2,
            topology_kind="same_host_two_independent_engines",
            runner_separate_from_serving=True,
            runner_colocation="SAME_VM_SEPARATE_CPU_CONTAINER",
            runner_gpu_access="NONE",
            runner_cloud_credentials="NONE",
            runner_docker_socket="ABSENT",
            runner_provider_mutation_authority="NONE",
            serving_engine_count=2,
            one_engine_per_endpoint=True,
            persistent_disk_count=0,
            private_network="projects/inferdrome-lab/global/networks/labnet",
            private_subnetwork=(
                "projects/inferdrome-lab/regions/us-central1/subnetworks/labnet"
            ),
        ),
        ownership_labels=GcpPrivateCampaignOwnershipLabels(
            inferdrome="inferdrome",
            managed_by="inferdrome_gcp_private_campaign_v2",
            role="two-engine-precampaign",
            controller_id="pcctl-12345678",
            ownership_nonce="0123456789abcdef",
            run_lease_id="lease-0123456789ab",
        ),
        instance_name="inferdrome-pc-12345678",
        boot_disk_name="inferdrome-pc-12345678-boot",
        boot_image=GcpPrivateCampaignBootImage(
            image_ref="projects/inferdrome-lab/global/images/precampaign-image",
            provider_image_id=123,
            boot_image_identity=gcp_private_campaign_boot_image_identity(
                image_ref="projects/inferdrome-lab/global/images/precampaign-image",
                provider_image_id=123,
            ),
        ),
        guest_service_account=GcpPrivateCampaignGuestServiceAccount(
            service_account_email=(
                "precampaign-guest@inferdrome-lab.iam.gserviceaccount.com"
            ),
            service_account_mode="USER_MANAGED_LEAST_PRIVILEGE_V1",
            oauth_scopes=(),
            inherited_project_ssh_keys_blocked=True,
            os_login_disabled=True,
        ),
        iap_connectivity=GcpPrivateCampaignIapConnectivity(
            connectivity_kind="IAP_TCP_FORWARDING_V1",
            controller_principal=(
                "precampaign-controller@inferdrome-lab.iam.gserviceaccount.com"
            ),
            firewall_rule_ref=(
                "projects/inferdrome-lab/global/firewalls/inferdrome-pc-iap"
            ),
            firewall_provider_id=456,
            iap_source_cidr="35.235.240.0/20",
            instance_network_tag="inferdrome-pc-0123456789abcdef",
            local_tunnel_ports=(18000, 18001, 18002),
            tunnel_binary="gcloud",
            ssh_transport_forbidden=True,
        ),
        preloaded_artifacts=startup.preloaded_artifacts,
        runner_image=runner,
        serving_image=serving,
        model=startup.engines[0].model,
        runtime=startup.engines[0].runtime,
        startup_payload_digest=startup.startup_payload_id,
        routing=GcpPrivateCampaignRoutingBinding(
            execution_id="routing-execution-v1",
            source_commit="1" * 40,
            campaign_id="routing-campaign-v1",
            plan_sha256=inputs["plan"],
            trace_sha256=inputs["trace"],
            fault_schedule_sha256=inputs["fault"],
            trial_plan_sha256=inputs["trial"],
            policy_set_sha256=sha256_digest(
                canonical_json_bytes(list(fixed_policy_ids()))
            ),
            workload_sha256=qwen3_workload_sha256(),
            selected_workload_sha256=fixed_selected_workload_sha256(),
            request_denominator=6,
            evidence_destination_sha256="sha256:" + ("4" * 64),
        ),
        quote=GcpPrivateCampaignQuote(
            quote_id="quote-12345678",
            quoted_at="2026-09-02T00:00:00Z",
            expires_at="2026-09-02T01:00:00Z",
            currency="USD",
            rate_microusd_per_hour=1,
            usd_cap_microusd=2,
            estimate_only=True,
        ),
        max_runtime_seconds=300,
        cleanup_horizon_seconds=3600,
        request_ids=GcpPrivateCampaignRequestIds(
            create_request_id="123e4567-e89b-12d3-a456-426614174000",
            delete_request_id="223e4567-e89b-12d3-a456-426614174000",
            boot_disk_delete_request_id="323e4567-e89b-12d3-a456-426614174000",
        ),
    )
    return issue_gcp_private_campaign_proposal(payload), startup


def _seed_exact_cleanup_journal(
    journal: GcpPrivateCampaignJournal,
    proposal: GcpPrivateCampaignProposal,
    startup: object,
) -> object:
    """Create the smallest durable post-create history for cleanup tests."""

    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    now = _Clock().now()
    journal.append(
        proposal,
        state="CREATE_INTENT_DURABLE",
        occurred_at=now,
        detail_digest=request.create_request_digest,
    )
    journal.append(proposal, state="CREATE_INTENT", occurred_at=now)
    journal.append(proposal, state="CREATE_SUBMITTED", occurred_at=now)
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
    return request


def test_startup_rejects_an_image_identity_shared_by_runner_and_engines() -> None:
    image = ImageIdentity(reference="example/inferdrome@sha256:" + ("1" * 64))

    with pytest.raises(ValueError, match="runner and serving image content digests"):
        issue_gcp_private_campaign_startup_payload(
            source_commit="1" * 40, runner_image=image, serving_image=image
        )


def _same_content_images() -> tuple[ImageIdentity, ImageIdentity]:
    digest = "sha256:" + ("d" * 64)
    return (
        ImageIdentity(reference=f"registry.example/runner@{digest}"),
        ImageIdentity(reference=f"registry.example/serving@{digest}"),
    )


def test_preloaded_artifacts_reject_same_content_digest_across_repositories() -> None:
    _, startup = _proposal()
    runner, serving = _same_content_images()
    payload = startup.preloaded_artifacts.model_dump(mode="python")
    payload.update(runner_image=runner, serving_image=serving)

    with pytest.raises(ValueError, match="runner and serving image content digests"):
        campaign_v2.GcpPrivateCampaignPreloadedArtifacts(**payload)


def test_startup_rejects_same_content_digest_across_role_repositories() -> None:
    _, startup = _proposal()
    runner, serving = _same_content_images()
    preloaded = startup.preloaded_artifacts.model_copy(
        update={"runner_image": runner, "serving_image": serving}
    )
    engines = tuple(
        engine.model_copy(update={"serving_image": serving})
        for engine in startup.engines
    )
    observer = startup.runner.model_copy(update={"runner_image": runner})
    payload = startup.model_dump(mode="python")
    payload.pop("startup_payload_id")
    payload.update(
        runner_image=runner,
        preloaded_artifacts=preloaded,
        engines=engines,
        runner=observer,
    )

    with pytest.raises(ValueError, match="runner and serving image content digests"):
        campaign_v2.GcpPrivateCampaignStartupPayloadPayload(**payload)


def test_proposal_rejects_same_content_digest_across_role_repositories() -> None:
    proposal, startup = _proposal()
    runner, serving = _same_content_images()
    preloaded = startup.preloaded_artifacts.model_copy(
        update={"runner_image": runner, "serving_image": serving}
    )
    payload = proposal.model_dump(mode="python")
    payload.pop("proposal_id")
    payload.update(
        runner_image=runner,
        serving_image=serving,
        preloaded_artifacts=preloaded,
    )

    with pytest.raises(ValueError, match="runner and serving image content digests"):
        GcpPrivateCampaignProposalPayload(**payload)


def _self_consistent_unvalidated_proposal(
    proposal: GcpPrivateCampaignProposal, *, routing_overrides: dict[str, object]
) -> GcpPrivateCampaignProposal:
    """Model a hostile but digest-self-consistent JSON input at the CLI edge."""

    candidate = proposal.model_copy(
        update={"routing": proposal.routing.model_copy(update=routing_overrides)}
    )
    return candidate.model_copy(
        update={"proposal_id": gcp_private_campaign_proposal_id(candidate)}
    )


def _approval(proposal: GcpPrivateCampaignProposal) -> GcpPrivateCampaignApproval:
    return GcpPrivateCampaignApproval(
        schema_version=GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION,
        approval_kind="exact_human_campaign_approval",
        confirmation=GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION,
        human_approval_record_id="approval-local-test",
        approved_at="2026-09-02T00:00:00Z",
        expires_at="2026-09-02T00:30:00Z",
        proposal=proposal,
        proposal_digest=proposal.proposal_id,
    )


def _cleanup_authorization(
    proposal: GcpPrivateCampaignProposal,
) -> GcpPrivateCampaignCleanupAuthorization:
    return GcpPrivateCampaignCleanupAuthorization(
        schema_version=GCP_PRIVATE_CAMPAIGN_CLEANUP_AUTHORIZATION_SCHEMA_VERSION,
        authorization_kind="exact_cleanup_recovery",
        confirmation=GCP_PRIVATE_CAMPAIGN_CLEANUP_CONFIRMATION,
        cleanup_record_id="cleanup-local-test",
        authorized_at="2026-09-02T00:30:00Z",
        proposal=proposal,
        proposal_digest=proposal.proposal_id,
        cleanup_request_id=proposal.request_ids.delete_request_id,
    )


def _journal_root(tmp_path: Path) -> Path:
    root = tmp_path / "journal"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def test_fake_lifecycle_handoffs_only_after_two_endpoint_readiness_and_cleans(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    fake = FakeGcpPrivateCampaignTransport()
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=lambda: fake,
        clock=_Clock(),
    )

    receipt = controller.execute(
        proposal=proposal, approval=_approval(proposal), startup_payload=startup
    )

    assert receipt.collection_mode == "CREATE_NO_REPLACE_RETRIEVAL"
    assert fake.create_calls == 1
    assert fake.handoff_calls == 1
    assert fake.delete_calls == 1
    assert not fake.created
    assert not fake.boot_disk_present
    events = controller._journal.load(proposal)
    assert [event.state for event in events] == [
        "CREATE_INTENT_DURABLE",
        "CREATE_INTENT",
        "CREATE_SUBMITTED",
        "CREATED",
        "INSTANCE_IDENTITY_BOUND",
        "BOOT_DISK_IDENTITY_BOUND",
        "PROVIDER_BACKSTOP_VERIFIED",
        "READINESS_VERIFIED",
        "CAMPAIGN_HANDED_OFF",
        "EVIDENCE_RETRIEVED",
        "CLEANUP_INTENT",
        "INSTANCE_DELETE_SUBMITTED",
        "CLEANUP_CONFIRMED",
    ]
    journal_bytes = next(_journal_root(tmp_path).glob("*.events.jsonl")).read_bytes()
    assert b"10.23.0.17" not in journal_bytes
    assert b"123456789" not in journal_bytes
    assert b"prompt" not in journal_bytes


def test_invalid_or_expired_approval_never_initializes_transport(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    calls = 0

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal calls
        calls += 1
        return FakeGcpPrivateCampaignTransport()

    expired = GcpPrivateCampaignApproval(
        schema_version=GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_VERSION,
        approval_kind="exact_human_campaign_approval",
        confirmation=GCP_PRIVATE_CAMPAIGN_APPROVAL_CONFIRMATION,
        human_approval_record_id="approval-expired",
        approved_at="2026-09-02T00:00:00Z",
        expires_at="2026-09-02T00:00:01Z",
        proposal=proposal,
        proposal_digest=proposal.proposal_id,
    )
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=factory,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 0, 2, tzinfo=UTC)),
    )

    with pytest.raises(GcpPrivateCampaignError, match="APPROVAL_EXPIRED"):
        controller.execute(proposal=proposal, approval=expired, startup_payload=startup)

    assert calls == 0
    assert not list(_journal_root(tmp_path).glob("*.events.jsonl"))


@pytest.mark.parametrize(
    "routing_overrides",
    (
        {"policy_set_sha256": "sha256:" + ("e" * 64)},
        {"workload_sha256": "sha256:" + ("d" * 64)},
    ),
)
def test_fixed_r1_policy_and_workload_are_revalidated_before_factory(
    tmp_path: Path, routing_overrides: dict[str, object]
) -> None:
    proposal, startup = _proposal()
    hostile = _self_consistent_unvalidated_proposal(
        proposal, routing_overrides=routing_overrides
    )
    approval = _approval(proposal).model_copy(
        update={"proposal": hostile, "proposal_digest": hostile.proposal_id}
    )
    calls = 0

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal calls
        calls += 1
        return FakeGcpPrivateCampaignTransport()

    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=factory,
        clock=_Clock(),
    )
    with pytest.raises(GcpPrivateCampaignError, match="LOCAL_CONTRACT_INVALID"):
        controller.execute(proposal=hostile, approval=approval, startup_payload=startup)
    assert calls == 0
    assert not list(_journal_root(tmp_path).glob("*.events.jsonl"))


def test_evidence_destination_is_opened_and_bound_before_factory(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    calls = 0

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal calls
        calls += 1
        return FakeGcpPrivateCampaignTransport()

    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=factory,
        launch_preflight=lambda value: verify_gcp_private_campaign_evidence_destination(
            value, evidence_root=evidence_root
        ),
        clock=_Clock(),
    )
    with pytest.raises(GcpPrivateCampaignError, match="EVIDENCE_DESTINATION_MISMATCH"):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )
    assert calls == 0
    assert not list(_journal_root(tmp_path).glob("*.events.jsonl"))


def test_evidence_destination_preflight_refuses_a_symlinked_root(
    tmp_path: Path,
) -> None:
    proposal, _ = _proposal()
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "evidence-link"
    link.symlink_to(target, target_is_directory=True)
    rebound = _reissued_proposal(
        proposal,
        routing_overrides={
            "evidence_destination_sha256": sha256_digest(
                str(link.absolute()).encode("utf-8")
            )
        },
    )
    with pytest.raises(
        GcpPrivateCampaignError, match="EVIDENCE_DESTINATION_UNAVAILABLE"
    ):
        verify_gcp_private_campaign_evidence_destination(rebound, evidence_root=link)


def test_evidence_destination_preflight_requires_a_writable_private_root(
    tmp_path: Path,
) -> None:
    proposal, _ = _proposal()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    evidence_root.chmod(0o500)
    rebound = _reissued_proposal(
        proposal,
        routing_overrides={
            "evidence_destination_sha256": sha256_digest(
                str(evidence_root.absolute()).encode("utf-8")
            )
        },
    )

    with pytest.raises(
        GcpPrivateCampaignError, match="EVIDENCE_DESTINATION_UNAVAILABLE"
    ):
        verify_gcp_private_campaign_evidence_destination(
            rebound, evidence_root=evidence_root
        )


def test_preflight_evidence_descriptor_is_bound_before_create(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    rebound = _reissued_proposal(
        proposal,
        routing_overrides={
            "evidence_destination_sha256": sha256_digest(
                str(evidence_root.absolute()).encode("utf-8")
            )
        },
    )
    fake = FakeGcpPrivateCampaignTransport()
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=lambda: fake,
        launch_preflight=lambda value: verify_gcp_private_campaign_evidence_destination(
            value, evidence_root=evidence_root
        ),
        clock=_Clock(),
    )

    controller.execute(
        proposal=rebound, approval=_approval(rebound), startup_payload=startup
    )

    metadata = evidence_root.stat()
    assert fake.bound_evidence_root_identity == (metadata.st_dev, metadata.st_ino)


def test_backstop_and_create_intent_are_durable_before_factory(tmp_path: Path) -> None:
    proposal, startup = _proposal()
    root = _journal_root(tmp_path)
    journal = GcpPrivateCampaignJournal(root)
    fake = FakeGcpPrivateCampaignTransport()

    def factory() -> FakeGcpPrivateCampaignTransport:
        events = journal.load(proposal)
        assert [event.state for event in events] == [
            "CREATE_INTENT_DURABLE",
            "CREATE_INTENT",
        ]
        return fake

    controller = GcpPrivateCampaignLifecycleController(
        journal=journal, transport_factory=factory, clock=_Clock()
    )

    controller.execute(
        proposal=proposal, approval=_approval(proposal), startup_payload=startup
    )

    assert fake.create_calls == 1


def test_lost_create_and_delete_responses_reconcile_exact_request_ids(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    fake = FakeGcpPrivateCampaignTransport(
        lose_create_response_once=True, lose_delete_response_once=True
    )
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=lambda: fake,
        clock=_Clock(),
    )

    controller.execute(
        proposal=proposal, approval=_approval(proposal), startup_payload=startup
    )

    assert fake.create_calls == 1
    assert fake.delete_calls == 1
    states = [event.state for event in controller._journal.load(proposal)]
    assert "CREATE_RECONCILING" in states
    assert "INSTANCE_DELETE_RECONCILING" in states
    assert states[-1] == "CLEANUP_CONFIRMED"


def test_residual_boot_disk_is_deleted_exactly_or_marked_unconfirmed(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    fake = FakeGcpPrivateCampaignTransport(retain_boot_disk_after_instance_delete=True)
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=lambda: fake,
        clock=_Clock(),
    )
    controller.execute(
        proposal=proposal, approval=_approval(proposal), startup_payload=startup
    )
    assert not fake.boot_disk_present
    assert controller._journal.load(proposal)[-1].state == "CLEANUP_CONFIRMED"

    proposal, startup = _proposal()
    failed = FakeGcpPrivateCampaignTransport(
        retain_boot_disk_after_instance_delete=True, cleanup_failure=True
    )
    failed_controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path / "failed")),
        transport_factory=lambda: failed,
        clock=_Clock(),
    )
    with pytest.raises(GcpPrivateCampaignError, match="CLEANUP_UNCONFIRMED"):
        failed_controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )
    assert failed_controller._journal.load(proposal)[-1].state == "CLEANUP_UNCONFIRMED"


def test_journal_failure_after_cleanup_still_closes_supervised_transport(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()

    class ClosingTransport(FakeGcpPrivateCampaignTransport):
        closed: bool = False

        def close(self) -> None:
            self.closed = True

    def crash_after_cleanup_confirmation(point: str) -> None:
        if point == "after-cleanup_confirmed":
            raise RuntimeError("journal callback failed")

    fake = ClosingTransport()
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(
            _journal_root(tmp_path), crash_hook=crash_after_cleanup_confirmation
        ),
        transport_factory=lambda: fake,
        clock=_Clock(),
    )

    with pytest.raises(GcpPrivateCampaignError, match="CLEANUP_JOURNAL_RECORD_FAILED"):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )

    assert fake.closed is True


def test_principal_bind_journal_failure_still_closes_supervised_transport(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()

    class ClosingTransport(FakeGcpPrivateCampaignTransport):
        closed: bool = False

        def close(self) -> None:
            self.closed = True

    def crash_after_blocked(point: str) -> None:
        if point == "after-blocked":
            raise RuntimeError("journal callback failed")

    fake = ClosingTransport(expected_controller_principal="different@example.test")
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(
            _journal_root(tmp_path), crash_hook=crash_after_blocked
        ),
        transport_factory=lambda: fake,
        clock=_Clock(),
    )

    with pytest.raises(RuntimeError, match="journal callback failed"):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )

    assert fake.closed is True


def test_cleanup_recovery_is_valid_after_launch_approval_expiry(tmp_path: Path) -> None:
    proposal, startup = _proposal()
    root = _journal_root(tmp_path)
    journal = GcpPrivateCampaignJournal(root)
    journal.append(proposal, state="CREATE_INTENT_DURABLE", occurred_at=_Clock().now())
    journal.append(proposal, state="CREATE_INTENT", occurred_at=_Clock().now())
    journal.append(proposal, state="CREATE_SUBMITTED", occurred_at=_Clock().now())
    journal.append(proposal, state="CREATED", occurred_at=_Clock().now())
    journal.append(
        proposal,
        state="INSTANCE_IDENTITY_BOUND",
        occurred_at=_Clock().now(),
        detail_digest=sha256_digest(b"123456789"),
    )
    journal.append(
        proposal,
        state="BOOT_DISK_IDENTITY_BOUND",
        occurred_at=_Clock().now(),
        detail_digest=sha256_digest(b"246813579"),
    )
    fake = FakeGcpPrivateCampaignTransport(created=True, boot_disk_present=True)
    controller = GcpPrivateCampaignLifecycleController(
        journal=journal,
        transport_factory=lambda: fake,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 31, tzinfo=UTC)),
    )

    outcome = controller.recover_exact_cleanup(
        proposal=proposal,
        authorization=_cleanup_authorization(proposal),
        startup_payload=startup,
    )

    assert outcome.confirmed
    assert fake.create_calls == 0
    assert fake.delete_calls == 1


def test_cleanup_retries_keep_provider_cleanup_callable_after_journal_budget(
    tmp_path: Path,
) -> None:
    """A transient failure cannot spend the finite journal before cleanup."""

    proposal, startup = _proposal()
    journal = GcpPrivateCampaignJournal(_journal_root(tmp_path))
    _seed_exact_cleanup_journal(journal, proposal, startup)

    class EventuallyDeleting(FakeGcpPrivateCampaignTransport):
        attempts: int = 0

        def delete_exact_instance(self, *args: object, **kwargs: object) -> object:
            self.attempts += 1
            if self.attempts <= GCP_PRIVATE_CAMPAIGN_MAX_EVENTS + 8:
                raise GcpPrivateCampaignError("TRANSIENT_DELETE_FAILURE")
            return super().delete_exact_instance(*args, **kwargs)  # type: ignore[arg-type]

    fake = EventuallyDeleting(created=True, boot_disk_present=True)
    controller = GcpPrivateCampaignLifecycleController(
        journal=journal,
        transport_factory=lambda: fake,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 31, tzinfo=UTC)),
    )
    authorization = _cleanup_authorization(proposal)

    for _ in range(GCP_PRIVATE_CAMPAIGN_MAX_EVENTS + 8):
        outcome = controller.recover_exact_cleanup(
            proposal=proposal,
            authorization=authorization,
            startup_payload=startup,
        )
        assert outcome.confirmed is False
        assert journal.load(proposal)[-1].state == "CLEANUP_UNCONFIRMED"

    outcome = controller.recover_exact_cleanup(
        proposal=proposal,
        authorization=authorization,
        startup_payload=startup,
    )

    assert outcome.confirmed is True
    assert fake.attempts == GCP_PRIVATE_CAMPAIGN_MAX_EVENTS + 9
    assert fake.delete_calls == 1
    assert len(journal.load(proposal)) < GCP_PRIVATE_CAMPAIGN_MAX_EVENTS
    assert journal.load(proposal)[-1].state == "CLEANUP_CONFIRMED"


def test_cleanup_revokes_claim_and_fences_a_submitted_create_before_terminal_cleanup(
    tmp_path: Path,
) -> None:
    """A wedged provider call cannot hold the lifecycle journal lock forever."""

    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    journal = GcpPrivateCampaignJournal(_journal_root(tmp_path / "revoked"))
    now = _Clock().now()
    journal.append(
        proposal,
        state="CREATE_INTENT_DURABLE",
        occurred_at=now,
        detail_digest=request.create_request_digest,
    )
    journal.append(proposal, state="CREATE_INTENT", occurred_at=now)
    journal.claim_create_for_factory(
        proposal,
        request_digest=request.create_request_digest,
        occurred_at=now,
    )
    controller = GcpPrivateCampaignLifecycleController(
        journal=journal,
        transport_factory=FakeGcpPrivateCampaignTransport,
        clock=_Clock(),
    )

    # A cleanup intent observed after the factory claim revokes the remaining
    # create authority before the final insert seam can take its lock.
    controller._ensure_cleanup_intent(proposal)
    with (
        pytest.raises(GcpPrivateCampaignError, match="CREATE_CLAIM_REVOKED"),
        journal.create_insert_claim(
            proposal,
            request_digest=request.create_request_digest,
            occurred_at=now,
        ),
    ):
        pytest.fail("revoked create claim reached the insert seam")

    # A submitted create no longer holds the journal lock through the provider
    # call.  Ordinary cleanup must fail closed until the watchdog writes an
    # irreversible fence and proves the creator is gone.
    concurrent = GcpPrivateCampaignJournal(_journal_root(tmp_path / "concurrent"))
    concurrent.append(
        proposal,
        state="CREATE_INTENT_DURABLE",
        occurred_at=now,
        detail_digest=request.create_request_digest,
    )
    concurrent.append(proposal, state="CREATE_INTENT", occurred_at=now)
    concurrent.claim_create_for_factory(
        proposal,
        request_digest=request.create_request_digest,
        occurred_at=now,
    )
    concurrent.begin_create_submission(
        proposal,
        request_digest=request.create_request_digest,
        occurred_at=now,
    )
    with pytest.raises(GcpPrivateCampaignError, match="CREATE_FENCE_REQUIRED"):
        concurrent_controller = GcpPrivateCampaignLifecycleController(
            journal=concurrent,
            transport_factory=FakeGcpPrivateCampaignTransport,
            clock=_Clock(),
        )
        concurrent_controller._ensure_cleanup_intent(proposal)
    concurrent.fence_submitted_create(
        proposal,
        request_digest=request.create_request_digest,
        occurred_at=now,
    )
    with pytest.raises(GcpPrivateCampaignError, match="CREATE_CLAIM_REVOKED"):
        concurrent.assert_create_submission_live(
            proposal, request_digest=request.create_request_digest
        )
    # A returned/lost insert response cannot revive this creator through the
    # reconciliation edge after the watchdog has durably fenced it.
    with pytest.raises(GcpPrivateCampaignError, match="CREATE_CLAIM_REVOKED"):
        concurrent.begin_create_reconciliation(
            proposal,
            request_digest=request.create_request_digest,
            occurred_at=now,
        )
    concurrent.release_fenced_create_for_cleanup(proposal, occurred_at=now)
    assert [event.state for event in concurrent.load(proposal)] == [
        "CREATE_INTENT_DURABLE",
        "CREATE_INTENT",
        "CREATE_CLAIMED",
        "CREATE_SUBMITTED",
        "CREATE_FENCE_PENDING",
        "CLEANUP_INTENT",
    ]


def test_manual_recovery_never_confirms_temporary_absence_after_submitted_create(
    tmp_path: Path,
) -> None:
    """A delayed insert must not be mistaken for terminal provider absence."""

    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    journal = GcpPrivateCampaignJournal(_journal_root(tmp_path))
    now = _Clock().now()
    journal.append(
        proposal,
        state="CREATE_INTENT_DURABLE",
        occurred_at=now,
        detail_digest=request.create_request_digest,
    )
    journal.append(proposal, state="CREATE_INTENT", occurred_at=now)
    journal.append(proposal, state="CREATE_SUBMITTED", occurred_at=now)

    class DelayedMaterialization(FakeGcpPrivateCampaignTransport):
        remaining_invisible_reads = 1

        def observe_exact_instance(self, *args: object, **kwargs: object) -> object:
            if self.remaining_invisible_reads:
                self.remaining_invisible_reads -= 1
                raise campaign_v2.GcpPrivateCampaignTransportError("INSTANCE_NOT_FOUND")
            return super().observe_exact_instance(*args, **kwargs)  # type: ignore[arg-type]

    fake = DelayedMaterialization(created=True, boot_disk_present=True)
    controller = GcpPrivateCampaignLifecycleController(
        journal=journal,
        transport_factory=lambda: fake,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 31, tzinfo=UTC)),
    )

    first = controller.recover_exact_cleanup(
        proposal=proposal,
        authorization=_cleanup_authorization(proposal),
        startup_payload=startup,
    )
    assert first.confirmed is False
    assert first.cleanup_error == "CREATE_SUBMISSION_UNRECONCILED"
    assert journal.load(proposal)[-1].state == "CREATE_SUBMITTED"

    second = controller.recover_exact_cleanup(
        proposal=proposal,
        authorization=_cleanup_authorization(proposal),
        startup_payload=startup,
    )
    assert second.confirmed is True
    assert fake.delete_calls == 1
    assert journal.load(proposal)[-1].state == "CLEANUP_CONFIRMED"


@pytest.mark.parametrize("artifact_state", ("broken", "missing", "tampered"))
def test_recovery_defers_evidence_root_faults_until_after_exact_cleanup(
    tmp_path: Path, artifact_state: str
) -> None:
    proposal, startup = _proposal()
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    rebound = _reissued_proposal(
        proposal,
        routing_overrides={
            "evidence_destination_sha256": sha256_digest(
                str(evidence.absolute()).encode("utf-8")
            )
        },
    )
    journal = GcpPrivateCampaignJournal(_journal_root(tmp_path))
    for state in (
        "CREATE_INTENT_DURABLE",
        "CREATE_INTENT",
        "CREATE_SUBMITTED",
        "CREATED",
    ):
        journal.append(rebound, state=state, occurred_at=_Clock().now())
    journal.append(
        rebound,
        state="INSTANCE_IDENTITY_BOUND",
        occurred_at=_Clock().now(),
        detail_digest=sha256_digest(b"123456789"),
    )
    journal.append(
        rebound,
        state="BOOT_DISK_IDENTITY_BOUND",
        occurred_at=_Clock().now(),
        detail_digest=sha256_digest(b"246813579"),
    )
    if artifact_state in {"broken", "missing"}:
        evidence.rmdir()
    if artifact_state == "broken":
        evidence.write_bytes(b"not-a-directory")
    elif artifact_state == "tampered":
        artifact_name = campaign_v2.GcpPrivateCampaignReceiptStore._artifact_name(
            rebound
        )
        (evidence / artifact_name).write_bytes(b"{}")

    fake = FakeGcpPrivateCampaignTransport(created=True, boot_disk_present=True)
    controller = GcpPrivateCampaignLifecycleController(
        journal=journal,
        transport_factory=lambda: fake,
        receipt_root=evidence,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 31, tzinfo=UTC)),
    )

    outcome = controller.recover_exact_cleanup(
        proposal=rebound,
        authorization=_cleanup_authorization(rebound),
        startup_payload=startup,
    )

    assert outcome.confirmed is True
    assert outcome.retained_artifact_error is not None
    assert fake.delete_calls == 1
    assert fake.created is False


def test_journal_rejects_symlinked_history_without_provider_factory(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    root = _journal_root(tmp_path)
    events = root / "pcctl-12345678.gcp-private-campaign-v2.events.jsonl"
    outside = tmp_path / "outside"
    outside.write_text("{}\n", encoding="utf-8")
    events.symlink_to(outside)
    calls = 0

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal calls
        calls += 1
        return FakeGcpPrivateCampaignTransport()

    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(root),
        transport_factory=factory,
        clock=_Clock(),
    )
    with pytest.raises(GcpPrivateCampaignError, match="JOURNAL"):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )
    assert calls == 0
    assert outside.read_text(encoding="utf-8") == "{}\n"


def test_startup_or_approval_tamper_never_reaches_factory(tmp_path: Path) -> None:
    proposal, startup = _proposal()
    calls = 0

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal calls
        calls += 1
        return FakeGcpPrivateCampaignTransport()

    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=factory,
        clock=_Clock(),
    )
    tampered_approval = _approval(proposal).model_copy(
        update={"proposal_digest": "sha256:" + ("f" * 64)}
    )
    with pytest.raises(GcpPrivateCampaignError, match="LOCAL_CONTRACT_INVALID"):
        controller.execute(
            proposal=proposal,
            approval=tampered_approval,
            startup_payload=startup,
        )
    tampered_startup = startup.model_copy(
        update={"startup_payload_id": "sha256:" + ("e" * 64)}
    )
    with pytest.raises(GcpPrivateCampaignError, match="CREATE_REQUEST_INPUT_INVALID"):
        controller.execute(
            proposal=proposal,
            approval=_approval(proposal),
            startup_payload=tampered_startup,
        )
    assert calls == 0


def test_controller_principal_binding_follows_local_approval_and_blocks_mismatch(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    transport = FakeGcpPrivateCampaignTransport(
        expected_controller_principal=(
            "other-controller@inferdrome-lab.iam.gserviceaccount.com"
        )
    )
    factory_calls = 0

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal factory_calls
        factory_calls += 1
        return transport

    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=factory,
        clock=_Clock(),
    )
    invalid_approval = _approval(proposal).model_copy(
        update={"proposal_digest": "sha256:" + ("f" * 64)}
    )
    with pytest.raises(GcpPrivateCampaignError, match="LOCAL_CONTRACT_INVALID"):
        controller.execute(
            proposal=proposal,
            approval=invalid_approval,
            startup_payload=startup,
        )
    assert factory_calls == 0
    assert transport.bound_controller_principals == []

    with pytest.raises(GcpPrivateCampaignError, match="CONTROLLER_PRINCIPAL_MISMATCH"):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )
    assert factory_calls == 1
    assert transport.create_calls == 0


def test_cleanup_recovery_binds_the_same_approved_controller_principal(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    journal = GcpPrivateCampaignJournal(_journal_root(tmp_path))
    for state in (
        "CREATE_INTENT_DURABLE",
        "CREATE_INTENT",
        "CREATE_SUBMITTED",
        "CREATED",
    ):
        journal.append(proposal, state=state, occurred_at=_Clock().now())
    journal.append(
        proposal,
        state="INSTANCE_IDENTITY_BOUND",
        occurred_at=_Clock().now(),
        detail_digest=sha256_digest(b"123456789"),
    )
    journal.append(
        proposal,
        state="BOOT_DISK_IDENTITY_BOUND",
        occurred_at=_Clock().now(),
        detail_digest=sha256_digest(b"246813579"),
    )
    cleanup = FakeGcpPrivateCampaignTransport(
        created=True,
        boot_disk_present=True,
        expected_controller_principal=proposal.iap_connectivity.controller_principal,
    )
    controller = GcpPrivateCampaignLifecycleController(
        journal=journal,
        transport_factory=lambda: (_ for _ in ()).throw(
            AssertionError("recovery must use cleanup transport only")
        ),
        cleanup_transport_factory=lambda: _CleanupTransportFacade(cleanup),
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 31, tzinfo=UTC)),
    )

    outcome = controller.recover_exact_cleanup(
        proposal=proposal,
        authorization=_cleanup_authorization(proposal),
        startup_payload=startup,
    )

    assert outcome.confirmed is True
    assert cleanup.bound_controller_principals == [
        proposal.iap_connectivity.controller_principal
    ]


def test_durable_backstop_survives_crash_before_factory(tmp_path: Path) -> None:
    proposal, startup = _proposal()
    root = _journal_root(tmp_path)
    calls = 0

    class Crash(BaseException):
        pass

    def crash_hook(point: str) -> None:
        if point == "after-create_intent_durable":
            raise Crash

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal calls
        calls += 1
        return FakeGcpPrivateCampaignTransport()

    journal = GcpPrivateCampaignJournal(root, crash_hook=crash_hook)
    controller = GcpPrivateCampaignLifecycleController(
        journal=journal, transport_factory=factory, clock=_Clock()
    )
    with pytest.raises(Crash):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )
    assert [event.state for event in journal.load(proposal)] == [
        "CREATE_INTENT_DURABLE"
    ]
    assert calls == 0


def test_topology_drift_blocks_handoff_then_attempts_exact_cleanup(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    fake = FakeGcpPrivateCampaignTransport(topology_drift=True)
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=lambda: fake,
        clock=_Clock(),
    )

    with pytest.raises(GcpPrivateCampaignError, match="OBSERVED_TOPOLOGY_DRIFT"):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )

    assert fake.handoff_calls == 0
    assert fake.delete_calls == 1
    assert controller._journal.load(proposal)[-1].state == "CLEANUP_CONFIRMED"


def test_handoff_endpoint_hashes_must_match_the_admitted_config_before_retrieval(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()

    class WrongEndpointHandoff(FakeGcpPrivateCampaignTransport):
        def handoff_campaign(self, *args: object, **kwargs: object) -> object:
            receipt = super().handoff_campaign(*args, **kwargs)  # type: ignore[arg-type]
            return receipt.model_copy(
                update={"endpoint_a_origin_sha256": "sha256:" + ("f" * 64)}
            )

    fake = WrongEndpointHandoff()
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=lambda: fake,
        clock=_Clock(),
    )

    with pytest.raises(GcpPrivateCampaignError, match="CAMPAIGN_HANDOFF_MISMATCH"):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )

    assert fake.delete_calls == 1


def test_cleanup_refuses_same_name_replacement_with_a_new_provider_identity(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    fake = FakeGcpPrivateCampaignTransport(cleanup_provider_instance_id="987654321")
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=lambda: fake,
        clock=_Clock(),
    )

    with pytest.raises(GcpPrivateCampaignError, match="CLEANUP_UNCONFIRMED"):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )

    events = controller._journal.load(proposal)
    bound = next(event for event in events if event.state == "INSTANCE_IDENTITY_BOUND")
    assert bound.detail_digest == sha256_digest(b"123456789")
    assert fake.delete_calls == 0
    assert events[-1].state == "CLEANUP_UNCONFIRMED"


def test_readiness_same_name_replacement_never_reaches_handoff(tmp_path: Path) -> None:
    proposal, startup = _proposal()
    fake = FakeGcpPrivateCampaignTransport(readiness_provider_instance_id="987654321")
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=lambda: fake,
        clock=_Clock(),
    )

    with pytest.raises(GcpPrivateCampaignError, match="READINESS_RESOURCE_IDENTITY"):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )

    assert fake.handoff_calls == 0
    assert fake.delete_calls == 1


def test_recovery_without_durable_identity_uses_read_only_absence_only(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    journal = GcpPrivateCampaignJournal(_journal_root(tmp_path))
    for state in (
        "CREATE_INTENT_DURABLE",
        "CREATE_INTENT",
        "CREATE_SUBMITTED",
        "CREATED",
    ):
        journal.append(proposal, state=state, occurred_at=_Clock().now())
    fake = FakeGcpPrivateCampaignTransport(created=True, boot_disk_present=True)
    controller = GcpPrivateCampaignLifecycleController(
        journal=journal,
        transport_factory=lambda: fake,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 31, tzinfo=UTC)),
    )

    outcome = controller.recover_exact_cleanup(
        proposal=proposal,
        authorization=_cleanup_authorization(proposal),
        startup_payload=startup,
    )

    assert not outcome.confirmed
    assert fake.delete_calls == 0
    assert fake.boot_disk_delete_calls == 0
    assert journal.load(proposal)[-1].state == "CLEANUP_UNCONFIRMED"


def test_residual_disk_same_name_replacement_is_never_deleted(tmp_path: Path) -> None:
    proposal, startup = _proposal()
    fake = FakeGcpPrivateCampaignTransport(
        retain_boot_disk_after_instance_delete=True,
        cleanup_boot_disk_id="987654321",
    )
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=lambda: fake,
        clock=_Clock(),
    )

    with pytest.raises(GcpPrivateCampaignError, match="CLEANUP_UNCONFIRMED"):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )

    # The durable boot-disk identity is checked before the auto-delete
    # instance mutation, so a same-name replacement is never reached.
    assert fake.delete_calls == 0
    assert fake.boot_disk_delete_calls == 0


def test_quote_cap_rejects_an_underestimated_all_in_bound() -> None:
    proposal, _ = _proposal()
    payload = proposal.model_dump(mode="python")
    payload.pop("proposal_id")
    payload["quote"]["rate_microusd_per_hour"] = 3_600
    payload["quote"]["usd_cap_microusd"] = 1
    with pytest.raises(ValueError, match="quote cap"):
        GcpPrivateCampaignProposalPayload.model_validate(payload)


def test_future_launch_and_cleanup_authority_never_reaches_factory(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    calls = 0

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal calls
        calls += 1
        return FakeGcpPrivateCampaignTransport()

    future_approval = _approval(proposal).model_copy(
        update={"approved_at": "2026-09-02T00:01:00Z"}
    )
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path / "launch")),
        transport_factory=factory,
        clock=_Clock(),
    )
    with pytest.raises(GcpPrivateCampaignError, match="APPROVAL_NOT_YET_VALID"):
        controller.execute(
            proposal=proposal, approval=future_approval, startup_payload=startup
        )
    assert calls == 0

    journal = GcpPrivateCampaignJournal(_journal_root(tmp_path / "cleanup"))
    for state in (
        "CREATE_INTENT_DURABLE",
        "CREATE_INTENT",
        "CREATE_SUBMITTED",
        "CREATED",
    ):
        journal.append(proposal, state=state, occurred_at=_Clock().now())
    cleanup_controller = GcpPrivateCampaignLifecycleController(
        journal=journal, transport_factory=factory, clock=_Clock()
    )
    with pytest.raises(
        GcpPrivateCampaignError, match="CLEANUP_AUTHORIZATION_NOT_YET_VALID"
    ):
        cleanup_controller.recover_exact_cleanup(
            proposal=proposal,
            authorization=_cleanup_authorization(proposal),
            startup_payload=startup,
        )
    assert calls == 0


def test_future_quote_and_pre_factory_revalidation_block_provider_factory(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    future_quote = _reissued_proposal(
        proposal,
        quote_overrides={
            "quoted_at": "2026-09-02T00:01:00Z",
            "expires_at": "2026-09-02T01:00:00Z",
        },
    )
    calls = 0

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal calls
        calls += 1
        return FakeGcpPrivateCampaignTransport()

    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path / "quote")),
        transport_factory=factory,
        clock=_Clock(),
    )
    with pytest.raises(GcpPrivateCampaignError, match="QUOTE_NOT_YET_VALID"):
        controller.execute(
            proposal=future_quote,
            approval=_approval(future_quote),
            startup_payload=startup,
        )
    assert calls == 0

    revalidation = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path / "revalidation")),
        transport_factory=factory,
        clock=_SequenceClock(
            (
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 31, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 31, tzinfo=UTC),
            )
        ),
    )
    with pytest.raises(GcpPrivateCampaignError, match="APPROVAL_EXPIRED"):
        revalidation.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )
    assert calls == 0


def test_execution_deadline_blocks_create_but_exact_cleanup_survives_expiry(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    short = _reissued_proposal(proposal, max_runtime_seconds=60)
    calls = 0
    fake = FakeGcpPrivateCampaignTransport()

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal calls
        calls += 1
        return fake

    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path / "create")),
        transport_factory=factory,
        clock=_SequenceClock(
            (
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 1, tzinfo=UTC),
            )
        ),
    )
    with pytest.raises(GcpPrivateCampaignError, match="EXECUTION_DEADLINE_EXCEEDED"):
        controller.execute(
            proposal=short, approval=_approval(short), startup_payload=startup
        )
    assert calls == 1
    assert fake.create_calls == 0

    journal = GcpPrivateCampaignJournal(_journal_root(tmp_path / "cleanup"))
    for state in (
        "CREATE_INTENT_DURABLE",
        "CREATE_INTENT",
        "CREATE_SUBMITTED",
        "CREATED",
    ):
        journal.append(short, state=state, occurred_at=_Clock().now())
    journal.append(
        short,
        state="INSTANCE_IDENTITY_BOUND",
        occurred_at=_Clock().now(),
        detail_digest=sha256_digest(b"123456789"),
    )
    journal.append(
        short,
        state="BOOT_DISK_IDENTITY_BOUND",
        occurred_at=_Clock().now(),
        detail_digest=sha256_digest(b"246813579"),
    )
    cleanup_fake = FakeGcpPrivateCampaignTransport(created=True, boot_disk_present=True)
    cleanup = GcpPrivateCampaignLifecycleController(
        journal=journal,
        transport_factory=lambda: cleanup_fake,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 31, tzinfo=UTC)),
    )
    outcome = cleanup.recover_exact_cleanup(
        proposal=short,
        authorization=_cleanup_authorization(short),
        startup_payload=startup,
    )
    assert outcome.confirmed
    assert cleanup_fake.delete_calls == 1
    assert journal.load(short)[-1].state == "CLEANUP_CONFIRMED"


def test_unexpected_post_create_failure_is_sanitized_and_always_cleans(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()

    class FailingReadiness(FakeGcpPrivateCampaignTransport):
        def observe_readiness(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise RuntimeError("untrusted verifier detail")

    fake = FailingReadiness()
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=lambda: fake,
        clock=_Clock(),
    )

    with pytest.raises(GcpPrivateCampaignError, match="CAMPAIGN_OPERATION_FAILED"):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )
    assert fake.delete_calls == 1
    assert controller._journal.load(proposal)[-1].state == "CLEANUP_CONFIRMED"


@pytest.mark.parametrize("failure_kind", ("malformed", "generic"))
def test_post_dispatch_create_failures_attempt_exact_cleanup(
    tmp_path: Path, failure_kind: str
) -> None:
    proposal, startup = _proposal()

    class PostDispatchFailure(FakeGcpPrivateCampaignTransport):
        def create_instance(self, *args: object, **kwargs: object) -> object:
            super().create_instance(*args, **kwargs)  # type: ignore[arg-type]
            if failure_kind == "malformed":
                return object()
            raise RuntimeError("untrusted post-send detail")

    fake = PostDispatchFailure()
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=lambda: fake,
        clock=_Clock(),
    )

    with pytest.raises(GcpPrivateCampaignError, match="CAMPAIGN_OPERATION_FAILED"):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )

    states = [event.state for event in controller._journal.load(proposal)]
    assert "CLEANUP_INTENT" in states
    assert states[-1] == "CLEANUP_UNCONFIRMED"
    assert fake.delete_calls == 0


def test_receipt_publication_uses_held_evidence_descriptor_and_fails_closed_on_swap(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    rebound = _reissued_proposal(
        proposal,
        routing_overrides={
            "evidence_destination_sha256": sha256_digest(
                str(evidence.absolute()).encode("utf-8")
            )
        },
    )
    moved = tmp_path / "evidence-original"

    class ReplaceAfterRetrieve(FakeGcpPrivateCampaignTransport):
        def retrieve_evidence(self, *args: object, **kwargs: object) -> object:
            receipt = super().retrieve_evidence(*args, **kwargs)  # type: ignore[arg-type]
            evidence.rename(moved)
            evidence.mkdir(mode=0o700)
            return receipt

    fake = ReplaceAfterRetrieve()
    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=lambda: fake,
        receipt_root=evidence,
        launch_preflight=lambda value: verify_gcp_private_campaign_evidence_destination(
            value, evidence_root=evidence
        ),
        clock=_Clock(),
    )

    with pytest.raises(GcpPrivateCampaignError, match="RECEIPT_ROOT_CHANGED"):
        controller.execute(
            proposal=rebound, approval=_approval(rebound), startup_payload=startup
        )

    assert not list(evidence.glob("gcp-private-*.json"))
    assert list(moved.glob("gcp-private-readiness-*.json"))
    assert fake.delete_calls == 1


def test_crash_after_artifact_receipt_recovery_reverifies_durable_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal, startup = _proposal()
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    rebound = _reissued_proposal(
        proposal,
        routing_overrides={
            "evidence_destination_sha256": sha256_digest(
                str(evidence.absolute()).encode("utf-8")
            )
        },
    )

    class CrashAfterReceipt(BaseException):
        pass

    original_write = campaign_v2.GcpPrivateCampaignReceiptStore.write_artifact

    def write_then_crash(self: object, *args: object, **kwargs: object) -> object:
        original_write(self, *args, **kwargs)  # type: ignore[arg-type]
        raise CrashAfterReceipt

    monkeypatch.setattr(
        campaign_v2.GcpPrivateCampaignReceiptStore,
        "write_artifact",
        write_then_crash,
    )
    fake = FakeGcpPrivateCampaignTransport()
    journal = GcpPrivateCampaignJournal(_journal_root(tmp_path))
    controller = GcpPrivateCampaignLifecycleController(
        journal=journal,
        transport_factory=lambda: fake,
        receipt_root=evidence,
        launch_preflight=lambda value: verify_gcp_private_campaign_evidence_destination(
            value, evidence_root=evidence
        ),
        clock=_Clock(),
    )

    with pytest.raises(CrashAfterReceipt):
        controller.execute(
            proposal=rebound, approval=_approval(rebound), startup_payload=startup
        )

    assert journal.load(rebound)[-1].state == "CLEANUP_CONFIRMED"
    assert not any(
        event.state == "SEALED_ARTIFACT_RECEIPT_RECORDED"
        for event in journal.load(rebound)
    )
    artifact = next(evidence.glob("gcp-private-artifact-*.json"))
    readiness = next(evidence.glob("gcp-private-readiness-*.json"))
    receipt_bytes = artifact.read_bytes() + readiness.read_bytes()
    assert b"10.23.0.17" not in receipt_bytes
    assert b"123456789" not in receipt_bytes

    monkeypatch.setattr(
        campaign_v2.GcpPrivateCampaignReceiptStore,
        "write_artifact",
        original_write,
    )
    verified_paths: list[Path] = []

    def fake_verify(path: Path, *, expected_digest: str) -> SimpleNamespace:
        verified_paths.append(path)
        return SimpleNamespace(report=SimpleNamespace(retained_digest=expected_digest))

    monkeypatch.setattr(campaign_v2, "verify_execution_package", fake_verify)
    recovery = GcpPrivateCampaignLifecycleController(
        journal=journal,
        transport_factory=lambda: fake,
        receipt_root=evidence,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 31, tzinfo=UTC)),
    )
    outcome = recovery.recover_exact_cleanup(
        proposal=rebound,
        authorization=_cleanup_authorization(rebound),
        startup_payload=startup,
    )

    assert outcome.confirmed
    assert verified_paths == [
        evidence / f"routing-execution-{rebound.proposal_id[7:39]}"
    ]

    # A durable receipt is evidence, not a trust marker.  A later
    # cleanup-only recovery must reopen and reject a same-user tamper even
    # after the original run recorded confirmed absence.
    artifact.write_bytes(b"{}")
    tampered_outcome = recovery.recover_exact_cleanup(
        proposal=rebound,
        authorization=_cleanup_authorization(rebound),
        startup_payload=startup,
    )
    assert tampered_outcome.confirmed is True
    assert tampered_outcome.retained_artifact_error is not None


def test_recovery_uses_a_no_create_transport_surface(tmp_path: Path) -> None:
    proposal, startup = _proposal()
    journal = GcpPrivateCampaignJournal(_journal_root(tmp_path))
    for state in (
        "CREATE_INTENT_DURABLE",
        "CREATE_INTENT",
        "CREATE_SUBMITTED",
        "CREATED",
    ):
        journal.append(proposal, state=state, occurred_at=_Clock().now())
    journal.append(
        proposal,
        state="INSTANCE_IDENTITY_BOUND",
        occurred_at=_Clock().now(),
        detail_digest=sha256_digest(b"123456789"),
    )
    journal.append(
        proposal,
        state="BOOT_DISK_IDENTITY_BOUND",
        occurred_at=_Clock().now(),
        detail_digest=sha256_digest(b"246813579"),
    )
    fake = FakeGcpPrivateCampaignTransport(created=True, boot_disk_present=True)
    cleanup_only = _CleanupTransportFacade(fake)
    factory_calls = 0

    def forbidden_full_factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("recovery must not construct a full transport")

    controller = GcpPrivateCampaignLifecycleController(
        journal=journal,
        transport_factory=forbidden_full_factory,
        cleanup_transport_factory=lambda: cleanup_only,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 31, tzinfo=UTC)),
    )
    outcome = controller.recover_exact_cleanup(
        proposal=proposal,
        authorization=_cleanup_authorization(proposal),
        startup_payload=startup,
    )

    assert outcome.confirmed
    assert factory_calls == 0
    assert not hasattr(cleanup_only, "create_instance")


def test_post_factory_expiry_and_exact_expiry_never_create(tmp_path: Path) -> None:
    proposal, startup = _proposal()
    exact_expiry = _approval(proposal).model_copy(
        update={"expires_at": "2026-09-02T00:01:00Z"}
    )
    factory_calls = 0

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal factory_calls
        factory_calls += 1
        return FakeGcpPrivateCampaignTransport()

    exact_controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path / "exact")),
        transport_factory=factory,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 1, tzinfo=UTC)),
    )
    with pytest.raises(GcpPrivateCampaignError, match="APPROVAL_EXPIRED"):
        exact_controller.execute(
            proposal=proposal, approval=exact_expiry, startup_payload=startup
        )
    assert factory_calls == 0

    exact_quote = _reissued_proposal(
        proposal,
        quote_overrides={
            "quoted_at": "2026-09-02T00:00:00Z",
            "expires_at": "2026-09-02T00:01:00Z",
        },
    )
    quote_controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path / "quote")),
        transport_factory=factory,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 1, tzinfo=UTC)),
    )
    with pytest.raises(GcpPrivateCampaignError, match="QUOTE_EXPIRED"):
        quote_controller.execute(
            proposal=exact_quote,
            approval=_approval(exact_quote),
            startup_payload=startup,
        )
    assert factory_calls == 0

    delayed_approval = _approval(proposal).model_copy(
        update={"expires_at": "2026-09-02T00:00:01Z"}
    )
    delayed_fake = FakeGcpPrivateCampaignTransport()
    delayed_controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path / "delayed")),
        transport_factory=lambda: delayed_fake,
        clock=_SequenceClock(
            (
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 2, 0, 1, tzinfo=UTC),
            )
        ),
    )
    with pytest.raises(GcpPrivateCampaignError, match="APPROVAL_EXPIRED"):
        delayed_controller.execute(
            proposal=proposal,
            approval=delayed_approval,
            startup_payload=startup,
        )
    assert delayed_fake.create_calls == 0
    assert delayed_controller._journal.load(proposal)[-1].state == "BLOCKED"


def test_cleanup_recovery_resumes_after_intent_and_post_mutation_crashes(
    tmp_path: Path,
) -> None:
    proposal, startup = _proposal()

    class Crash(BaseException):
        pass

    root = _journal_root(tmp_path / "intent")
    fake = FakeGcpPrivateCampaignTransport()
    journal = GcpPrivateCampaignJournal(
        root,
        crash_hook=lambda point: (
            (_ for _ in ()).throw(Crash) if point == "after-cleanup_intent" else None
        ),
    )
    controller = GcpPrivateCampaignLifecycleController(
        journal=journal, transport_factory=lambda: fake, clock=_Clock()
    )
    with pytest.raises(Crash):
        controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )
    resumed = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(root),
        transport_factory=lambda: fake,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 31, tzinfo=UTC)),
    ).recover_exact_cleanup(
        proposal=proposal,
        authorization=_cleanup_authorization(proposal),
        startup_payload=startup,
    )
    assert resumed.confirmed
    assert fake.delete_calls == 1

    class CrashAfterDelete(FakeGcpPrivateCampaignTransport):
        def delete_exact_instance(self, *args: object, **kwargs: object) -> object:
            super().delete_exact_instance(*args, **kwargs)  # type: ignore[arg-type]
            raise Crash

    post_root = _journal_root(tmp_path / "post-mutation")
    post_fake = CrashAfterDelete()
    post_controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(post_root),
        transport_factory=lambda: post_fake,
        clock=_Clock(),
    )
    with pytest.raises(Crash):
        post_controller.execute(
            proposal=proposal, approval=_approval(proposal), startup_payload=startup
        )
    post_outcome = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(post_root),
        transport_factory=lambda: post_fake,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 31, tzinfo=UTC)),
    ).recover_exact_cleanup(
        proposal=proposal,
        authorization=_cleanup_authorization(proposal),
        startup_payload=startup,
    )
    assert post_outcome.confirmed
    assert post_fake.delete_calls == 1


def test_journal_loss_blocks_cleanup_recovery_before_factory(tmp_path: Path) -> None:
    proposal, startup = _proposal()
    calls = 0

    def factory() -> FakeGcpPrivateCampaignTransport:
        nonlocal calls
        calls += 1
        return FakeGcpPrivateCampaignTransport()

    controller = GcpPrivateCampaignLifecycleController(
        journal=GcpPrivateCampaignJournal(_journal_root(tmp_path)),
        transport_factory=factory,
        clock=_Clock(instant=datetime(2026, 9, 2, 0, 31, tzinfo=UTC)),
    )
    with pytest.raises(GcpPrivateCampaignError, match="JOURNAL_MISSING"):
        controller.recover_exact_cleanup(
            proposal=proposal,
            authorization=_cleanup_authorization(proposal),
            startup_payload=startup,
        )
    assert calls == 0


def test_profile_rejects_wrong_machine_shape_before_provider_surface() -> None:
    with pytest.raises(ValueError, match="machine_type"):
        GcpPrivateCampaignTopology(
            schema_version=GCP_PRIVATE_CAMPAIGN_TOPOLOGY_SCHEMA_VERSION,
            provider="gcp-compute-engine",
            project_id="inferdrome-lab",
            region="us-central1",
            zone="us-central1-a",
            machine_type="a2-highgpu-1g",
            accelerator_model="NVIDIA A100-SXM4-40GB",
            accelerator_provider_type="nvidia-tesla-a100",
            accelerator_count=2,
            topology_kind="same_host_two_independent_engines",
            runner_separate_from_serving=True,
            runner_colocation="SAME_VM_SEPARATE_CPU_CONTAINER",
            runner_gpu_access="NONE",
            runner_cloud_credentials="NONE",
            runner_docker_socket="ABSENT",
            runner_provider_mutation_authority="NONE",
            serving_engine_count=2,
            one_engine_per_endpoint=True,
            persistent_disk_count=0,
            private_network="projects/inferdrome-lab/global/networks/labnet",
            private_subnetwork=(
                "projects/inferdrome-lab/regions/us-central1/subnetworks/labnet"
            ),
        )
