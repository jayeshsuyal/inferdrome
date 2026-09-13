"""One-root verified projection of sealed routing-execution evidence.

This reader intentionally consumes only the verifier's in-memory result.  It
never turns a configured filesystem path into a browser identifier and never
reopens a package after the verifier has completed its immutable double scan.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore, RLock
from typing import Literal, cast

from inferdrome.dashboard.models import PageView
from inferdrome.dashboard.routing_execution_models import (
    RejectedRoutingExecution,
    RoutingExecutionArtifactProvenanceView,
    RoutingExecutionCampaignView,
    RoutingExecutionCandidateView,
    RoutingExecutionDetail,
    RoutingExecutionEndpointIdentityView,
    RoutingExecutionEvidenceView,
    RoutingExecutionFaultPlanView,
    RoutingExecutionFaultReceiptView,
    RoutingExecutionIndexResponse,
    RoutingExecutionInputTransferView,
    RoutingExecutionModelView,
    RoutingExecutionObserverEpochView,
    RoutingExecutionRequestView,
    RoutingExecutionResetView,
    RoutingExecutionRoutingInputsView,
    RoutingExecutionRuntimeView,
    RoutingExecutionSummary,
    RoutingExecutionTelemetryPlanView,
    RoutingExecutionTelemetryView,
    RoutingExecutionTerminalPopulationView,
    RoutingExecutionTerminalView,
    RoutingExecutionTopologyView,
    RoutingExecutionTrialView,
    RoutingExecutionWorkloadView,
    VastRoutingExecutionEvidenceView,
    VastRoutingExecutionTopologyView,
)
from inferdrome.errors import (
    DashboardError,
    DashboardPaginationError,
    DashboardRoutingExecutionNotFound,
    InferdromeError,
    WorkLimitError,
)
from inferdrome.limits import WorkBudget, WorkLimits
from inferdrome.routing_execution.contracts import (
    CandidateState,
    RouteDecisionReceipt,
    TelemetryObservation,
    TerminalOutcomeReceipt,
    TerminalStatus,
)
from inferdrome.routing_execution.manual_host_contracts import ExecutionManifest
from inferdrome.routing_execution.package import (
    VerifiedExecutionPackage,
    verify_execution_package,
)

_EXECUTION_ID = "routing-execution-v1"
_MAX_PAGE_LIMIT = 25
_PACKAGE_MAX_BYTES = 33_554_432
_SNAPSHOT_WORK = WorkLimits(max_units=1, max_bytes=_PACKAGE_MAX_BYTES, max_seconds=30)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TERMINAL_STATUS_ORDER: tuple[
    TerminalStatus,
    TerminalStatus,
    TerminalStatus,
    TerminalStatus,
    TerminalStatus,
] = (
    "SUCCEEDED",
    "TIMED_OUT",
    "FAILED",
    "CANCELLED",
    "NO_SAFE_ROUTE",
)


@dataclass(frozen=True)
class _VerifiedRoutingExecutionSnapshot:
    entries: tuple[RoutingExecutionSummary | RejectedRoutingExecution, ...]
    detail: RoutingExecutionDetail | None


def _entry_label(_: Path | None) -> Literal["<configured-root>"]:
    """Do not expose a configured local package location to the browser."""

    return "<configured-root>"


def _endpoint(
    value: object,
) -> RoutingExecutionEndpointIdentityView:
    """Copy only the logical endpoint alias from verified sealed evidence."""

    endpoint_id = getattr(value, "endpoint_id", None)
    if endpoint_id not in {"endpoint-a", "endpoint-b"}:
        raise DashboardError("verified routing execution endpoint disagrees")
    return RoutingExecutionEndpointIdentityView(
        endpoint_id=endpoint_id,
    )


def _topology(
    manifest: ExecutionManifest,
) -> RoutingExecutionTopologyView | VastRoutingExecutionTopologyView:
    if manifest.mode == "VAST_MANUAL_CONTAINER":
        vast_topology = manifest.topology
        if (
            vast_topology.provider != "VAST_AI"
            or vast_topology.provisioning != "OPERATOR_SUPPLIED_CONTAINER"
        ):
            raise DashboardError("manual-container topology declaration disagrees")
        return VastRoutingExecutionTopologyView(
            profile_id=vast_topology.profile_id,
            accelerator_model=vast_topology.accelerator_model,
            accelerator_count=vast_topology.accelerator_count,
            container_count=vast_topology.container_count,
            serving_engine_count=vast_topology.serving_engine_count,
            one_engine_per_endpoint=vast_topology.one_engine_per_endpoint,
            tensor_parallel_size=vast_topology.tensor_parallel_size,
            declared_provider="VAST_AI",
            declared_provisioning="OPERATOR_SUPPLIED_CONTAINER",
            identity_assertion=vast_topology.identity_assertion,
            lifecycle_protection=vast_topology.lifecycle_protection,
            isolation_boundary=vast_topology.isolation_boundary,
            observer_gpu_isolation=vast_topology.observer_gpu_isolation,
        )
    topology = manifest.topology
    if manifest.mode == "LAMBDA_MANUAL_HOST":
        provider = getattr(topology, "provider", None)
        provisioning = getattr(topology, "provisioning", None)
        assertion = getattr(topology, "identity_assertion", None)
        lifecycle = getattr(topology, "lifecycle_protection", None)
        if (
            provider != "LAMBDA"
            or provisioning != "OPERATOR_SUPPLIED_VM"
            or assertion != "OPERATOR_DECLARED_NOT_OBSERVED"
            or lifecycle != "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY"
        ):
            raise DashboardError("manual-host topology declaration disagrees")
        return RoutingExecutionTopologyView(
            accelerator_model=topology.accelerator_model,
            accelerator_count=topology.accelerator_count,
            runner_separate_from_serving=topology.runner_separate_from_serving,
            serving_engine_count=topology.serving_engine_count,
            one_engine_per_endpoint=topology.one_engine_per_endpoint,
            declared_provider="LAMBDA",
            declared_provisioning="OPERATOR_SUPPLIED_VM",
            identity_assertion="OPERATOR_DECLARED_NOT_OBSERVED",
            lifecycle_protection="UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY",
        )
    return RoutingExecutionTopologyView(
        accelerator_model=topology.accelerator_model,
        accelerator_count=topology.accelerator_count,
        runner_separate_from_serving=topology.runner_separate_from_serving,
        serving_engine_count=topology.serving_engine_count,
        one_engine_per_endpoint=topology.one_engine_per_endpoint,
        declared_provider=None,
        declared_provisioning=None,
        identity_assertion="NOT_RETAINED_BY_V1",
        lifecycle_protection="NOT_RETAINED_BY_V1",
    )


def _summary(verified: VerifiedExecutionPackage) -> RoutingExecutionSummary:
    manifest = verified.executed_manifest
    policies = tuple(manifest.routing_inputs.policies)
    if (
        manifest.execution_id != _EXECUTION_ID
        or verified.report.execution_id != _EXECUTION_ID
        or len(policies) != 3
        or manifest.planned_requests_per_trial != 6
        or manifest.planned_terminal_denominator != 18
    ):
        raise DashboardError("verified routing execution summary disagrees")
    return RoutingExecutionSummary(
        execution_id=manifest.execution_id,
        retained_digest=verified.report.retained_digest,
        mode=manifest.mode,
        source_commit=manifest.source_commit,
        model=RoutingExecutionModelView(
            model_id=manifest.model.model_id,
            model_revision=manifest.model.model_revision,
            tokenizer_revision=manifest.model.tokenizer_revision,
        ),
        runtime=RoutingExecutionRuntimeView(
            runtime_name=manifest.runtime.runtime_name,
            runtime_version=manifest.runtime.runtime_version,
            adapter_id=manifest.runtime.adapter_id,
            adapter_version=manifest.runtime.adapter_version,
        ),
        topology=_topology(manifest),
        policy_ids=policies,
        trial_count=3,
        request_denominator_per_trial=6,
        terminal_denominator=18,
    )


def _telemetry(value: TelemetryObservation) -> RoutingExecutionTelemetryView:
    """Project a received observation without its raw payload digest/body."""

    return RoutingExecutionTelemetryView(
        signal=value.signal,
        observer_id=value.observer_id,
        endpoint_id=value.endpoint_id,
        epoch=value.epoch,
        sampled_at_monotonic_ns=value.sampled_at_monotonic_ns,
        decision_at_monotonic_ns=value.decision_at_monotonic_ns,
        age_ns=value.age_ns,
        freshness_bound_ns=value.freshness_bound_ns,
        state=value.state,
        admissibility=value.admissibility,
        value=value.value,
        source=value.source,
    )


def _candidate(value: CandidateState) -> RoutingExecutionCandidateView:
    return RoutingExecutionCandidateView(
        endpoint_id=value.endpoint_id,
        eligible=value.eligible,
        health=_telemetry(value.health),
        load=_telemetry(value.load),
        gpu_dcgm=_telemetry(value.gpu_dcgm),
        kv_cache=_telemetry(value.kv_cache),
    )


def _terminal(value: TerminalOutcomeReceipt) -> RoutingExecutionTerminalView:
    return RoutingExecutionTerminalView(
        terminal_outcome_id=value.terminal_outcome_id,
        decision_id=value.decision_id,
        selected_endpoint_id=value.selected_endpoint_id,
        status=value.status,
        reason=value.reason,
        started_at_monotonic_ns=value.started_at_monotonic_ns,
        ended_at_monotonic_ns=value.ended_at_monotonic_ns,
        http_status=value.http_status,
        attempt_count=value.attempt_count,
    )


def _project(verified: VerifiedExecutionPackage) -> RoutingExecutionDetail:
    """Project only verifier-returned records, never a package pathname."""

    manifest = verified.executed_manifest
    receipt = verified.producer_receipt
    summary = _summary(verified)
    endpoint_view_values = tuple(_endpoint(endpoint) for endpoint in manifest.endpoints)
    if tuple(item.endpoint_id for item in endpoint_view_values) != (
        "endpoint-a",
        "endpoint-b",
    ):
        raise DashboardError("verified routing execution endpoint order disagrees")
    endpoint_views = cast(
        tuple[
            RoutingExecutionEndpointIdentityView,
            RoutingExecutionEndpointIdentityView,
        ],
        endpoint_view_values,
    )

    resets = {row.trial_id: row for row in receipt.reset_receipts}
    faults = {row.trial_id: row for row in receipt.fault_receipts}
    decisions_by_trial: dict[str, list[RouteDecisionReceipt]] = {}
    for decision in receipt.route_decisions:
        decisions_by_trial.setdefault(decision.trial_id, []).append(decision)
    terminals_by_id = {
        terminal.terminal_outcome_id: terminal for terminal in receipt.terminal_outcomes
    }
    if len(terminals_by_id) != len(receipt.terminal_outcomes):
        raise DashboardError("verified routing execution terminal identities disagree")

    trials: list[RoutingExecutionTrialView] = []
    for policy_id in summary.policy_ids:
        matching_summaries = [
            item for item in receipt.trial_summaries if item.policy_id == policy_id
        ]
        if len(matching_summaries) != 1:
            raise DashboardError("verified routing execution policy trial disagrees")
        trial_summary = matching_summaries[0]
        trial_id = trial_summary.trial_id
        reset = resets.get(trial_id)
        fault = faults.get(trial_id)
        decisions = decisions_by_trial.get(trial_id, [])
        if (
            reset is None
            or fault is None
            or len(decisions) != 6
            or tuple(sorted(item.sequence_index for item in decisions))
            != (0, 1, 2, 3, 4, 5)
        ):
            raise DashboardError("verified routing execution trial inventory disagrees")
        requests: list[RoutingExecutionRequestView] = []
        for decision in sorted(decisions, key=lambda item: item.sequence_index):
            terminal = terminals_by_id.get(decision.terminal_outcome_id)
            if (
                terminal is None
                or terminal.decision_id != decision.decision_id
                or terminal.request_id != decision.request_id
                or terminal.sequence_index != decision.sequence_index
            ):
                raise DashboardError(
                    "verified routing execution decision closure disagrees"
                )
            requests.append(
                RoutingExecutionRequestView(
                    request_id=decision.request_id,
                    sequence_index=decision.sequence_index,
                    decision_id=decision.decision_id,
                    decision_at_monotonic_ns=decision.decision_at_monotonic_ns,
                    candidates=cast(
                        tuple[
                            RoutingExecutionCandidateView,
                            RoutingExecutionCandidateView,
                        ],
                        tuple(_candidate(item) for item in decision.candidates),
                    ),
                    selected_endpoint_id=decision.selected_endpoint_id,
                    claims_used=decision.claims_used,
                    claims_permitted_stale=decision.claims_permitted_stale,
                    claims_discarded=decision.claims_discarded,
                    fallback_reason=decision.fallback_reason,
                    terminal=_terminal(terminal),
                )
            )
        population = tuple(
            RoutingExecutionTerminalPopulationView(
                status=status, count=trial_summary.terminal_population[status]
            )
            for status in _TERMINAL_STATUS_ORDER
        )
        if sum(item.count for item in population) != 6:
            raise DashboardError("verified routing execution population disagrees")
        if len(requests) != 6 or len(population) != 5:
            raise DashboardError(
                "verified routing execution trial projection disagrees"
            )
        runtime_identities = tuple(
            _endpoint(item) for item in reset.endpoint_runtime_identities
        )
        if len(runtime_identities) != 2:
            raise DashboardError("verified routing execution reset identities disagree")
        trials.append(
            RoutingExecutionTrialView(
                trial_id=trial_id,
                policy_id=policy_id,
                reset=RoutingExecutionResetView(
                    reset_at_monotonic_ns=reset.reset_at_monotonic_ns,
                    observer_epochs=tuple(
                        RoutingExecutionObserverEpochView(signal=signal, epoch=epoch)
                        for signal, epoch in sorted(reset.observer_epochs.items())
                    ),
                    runner_connection_state_cleared=(
                        reset.runner_connection_state_cleared
                    ),
                    runner_telemetry_state_cleared=(
                        reset.runner_telemetry_state_cleared
                    ),
                    endpoint_engine_reset_assertion=(
                        reset.endpoint_engine_reset_assertion
                    ),
                    endpoint_runtime_identities=runtime_identities,
                ),
                fault=RoutingExecutionFaultReceiptView(
                    fault_id=fault.fault_id,
                    activated_at_sequence_index=fault.activated_at_sequence_index,
                    activated_at_monotonic_ns=fault.activated_at_monotonic_ns,
                    load_collection_paused=fault.load_collection_paused,
                    health_collection_continues=fault.health_collection_continues,
                ),
                requests=cast(
                    tuple[
                        RoutingExecutionRequestView,
                        RoutingExecutionRequestView,
                        RoutingExecutionRequestView,
                        RoutingExecutionRequestView,
                        RoutingExecutionRequestView,
                        RoutingExecutionRequestView,
                    ],
                    tuple(requests),
                ),
                terminal_population=population,
                terminal_population_total=6,
            )
        )
    if len(trials) != 3:
        raise DashboardError("verified routing execution trial count disagrees")
    transfer = verified.input_transfer
    input_transfer = RoutingExecutionInputTransferView(
        config_sha256=transfer.config_sha256,
        selected_workload_sha256=transfer.selected_workload_sha256,
        workload_size_bytes=transfer.workload_size_bytes,
        declared_input_transfer_sha256=transfer.declared_input_transfer_sha256,
        verified_before_transport=transfer.verified_before_transport,
    )
    evidence: RoutingExecutionEvidenceView | VastRoutingExecutionEvidenceView
    if manifest.mode == "VAST_MANUAL_CONTAINER":
        provenance = manifest.artifact_provenance
        if provenance.source_commit != manifest.source_commit:
            raise DashboardError("manual-container artifact provenance disagrees")
        evidence = VastRoutingExecutionEvidenceView(
            container_image=manifest.container_image.reference,
            artifact_provenance=RoutingExecutionArtifactProvenanceView(
                container_image_assertion=provenance.container_image_assertion,
                source_commit=provenance.source_commit,
                observer_artifact_sha256=provenance.observer_artifact_sha256,
                supervisor_artifact_sha256=provenance.supervisor_artifact_sha256,
                model_manifest_sha256=provenance.model_manifest_sha256,
                model_snapshot_sha256=provenance.model_snapshot_sha256,
                runtime_observation_sha256=provenance.runtime_observation_sha256,
                runtime_assertion=provenance.runtime_assertion,
            ),
            endpoints=endpoint_views,
            input_transfer_receipt_sha256=manifest.input_transfer_receipt_sha256,
            input_transfer=input_transfer,
        )
    else:
        evidence = RoutingExecutionEvidenceView(
            runner_image=manifest.runner_image.reference,
            serving_image=manifest.serving_image.reference,
            endpoints=endpoint_views,
            input_transfer_receipt_sha256=manifest.input_transfer_receipt_sha256,
            input_transfer=input_transfer,
        )
    return RoutingExecutionDetail(
        summary=summary,
        evidence=evidence,
        campaign=RoutingExecutionCampaignView(
            routing_inputs=RoutingExecutionRoutingInputsView(
                campaign_id=manifest.routing_inputs.campaign_id,
                plan_sha256=manifest.routing_inputs.plan_sha256,
                trace_sha256=manifest.routing_inputs.trace_sha256,
                fault_schedule_sha256=manifest.routing_inputs.fault_schedule_sha256,
                trial_plan_sha256=manifest.routing_inputs.trial_plan_sha256,
                policies=cast(
                    tuple[str, str, str], tuple(manifest.routing_inputs.policies)
                ),
            ),
            workload=RoutingExecutionWorkloadView(
                workload_id=manifest.workload.workload_id,
                workload_sha256=manifest.workload.workload_sha256,
                selected_workload_sha256=(
                    manifest.workload.selected_workload_sha256
                ),
                selected_request_ids=cast(
                    tuple[str, str, str, str, str, str],
                    tuple(manifest.workload.selected_request_ids),
                ),
                request_denominator=manifest.workload.request_denominator,
            ),
            telemetry=RoutingExecutionTelemetryPlanView(
                clock_domain=manifest.telemetry.clock_domain,
                health_freshness_ms=manifest.telemetry.health_freshness_ms,
                load_freshness_ms=manifest.telemetry.load_freshness_ms,
                gpu_freshness_ms=manifest.telemetry.gpu_freshness_ms,
                load_metric_name=manifest.telemetry.load_metric_name,
            ),
            fault=RoutingExecutionFaultPlanView(
                fault_id=manifest.fault.fault_id,
                load_collection_pause_after_sequence_index=(
                    manifest.fault.load_collection_pause_after_sequence_index
                ),
                health_collection_continues=(
                    manifest.fault.health_collection_continues
                ),
                inter_request_interval_ms=manifest.fault.inter_request_interval_ms,
            ),
        ),
        trials=cast(
            tuple[
                RoutingExecutionTrialView,
                RoutingExecutionTrialView,
                RoutingExecutionTrialView,
            ],
            tuple(trials),
        ),
    )


class RoutingExecutionDashboardIndex:
    """Serve one configured package only after strict offline verification."""

    def __init__(
        self,
        *,
        routing_execution_root: Path | None = None,
        expected_execution_digest: str | None = None,
    ) -> None:
        self.routing_execution_root = (
            routing_execution_root.absolute()
            if routing_execution_root is not None
            else None
        )
        self.expected_execution_digest = expected_execution_digest
        self._cache_by_digest: dict[str, RoutingExecutionDetail] = {}
        self._snapshot: _VerifiedRoutingExecutionSnapshot | None = None
        self._lock = RLock()
        self._build_slot = BoundedSemaphore(value=1)

    def _configuration_error(self) -> RejectedRoutingExecution | None:
        root = self.routing_execution_root
        digest = self.expected_execution_digest
        if root is None and digest is None:
            return None
        if (
            root is None
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
        ):
            return RejectedRoutingExecution(
                entry=_entry_label(root), code="CONFIGURATION_INVALID"
            )
        return None

    @staticmethod
    def _is_safe_directory(root: Path) -> bool:
        try:
            metadata = os.lstat(root)
        except OSError:
            return False
        return not stat.S_ISLNK(metadata.st_mode) and stat.S_ISDIR(metadata.st_mode)

    def _verify_configured_root(self) -> _VerifiedRoutingExecutionSnapshot:
        configuration_error = self._configuration_error()
        if configuration_error is not None:
            return _VerifiedRoutingExecutionSnapshot((configuration_error,), None)
        root = self.routing_execution_root
        digest = self.expected_execution_digest
        if root is None and digest is None:
            return _VerifiedRoutingExecutionSnapshot((), None)
        if root is None or digest is None:
            return _VerifiedRoutingExecutionSnapshot(
                (
                    RejectedRoutingExecution(
                        entry=_entry_label(root), code="CONFIGURATION_INVALID"
                    ),
                ),
                None,
            )
        if (
            not self._is_safe_directory(root)
            or root.name.startswith(".routing-execution-stage-")
        ):
            return _VerifiedRoutingExecutionSnapshot(
                (
                    RejectedRoutingExecution(
                        entry=_entry_label(root), code="UNSAFE_ENTRY"
                    ),
                ),
                None,
            )
        budget = WorkBudget(_SNAPSHOT_WORK)
        try:
            budget.reserve(units=1, bytes_=_PACKAGE_MAX_BYTES)
            verified = verify_execution_package(
                root, expected_digest=digest, require_immutable=True
            )
            budget.checkpoint()
            with self._lock:
                detail = self._cache_by_digest.get(verified.report.retained_digest)
            if detail is None:
                detail = _project(verified)
            if (
                detail.summary.execution_id != verified.report.execution_id
                or detail.summary.retained_digest != verified.report.retained_digest
            ):
                raise DashboardError("verified routing execution cache disagrees")
        except (
            DashboardError,
            InferdromeError,
            OSError,
            RecursionError,
            ValueError,
            AssertionError,
            WorkLimitError,
        ):
            return _VerifiedRoutingExecutionSnapshot(
                (
                    RejectedRoutingExecution(
                        entry=_entry_label(root), code="VERIFICATION_FAILED"
                    ),
                ),
                None,
            )
        return _VerifiedRoutingExecutionSnapshot((detail.summary,), detail)

    def refresh(self, *, limit: int = _MAX_PAGE_LIMIT) -> RoutingExecutionIndexResponse:
        """Verify the one configured package before rendering an index entry."""

        if isinstance(limit, bool) or not 1 <= limit <= _MAX_PAGE_LIMIT:
            raise DashboardPaginationError("routing-execution page limit is invalid")
        if not self._build_slot.acquire(blocking=False):
            raise DashboardError(
                "routing-execution snapshot concurrency limit exceeded"
            )
        try:
            snapshot = self._verify_configured_root()
            with self._lock:
                self._snapshot = snapshot
                self._cache_by_digest = (
                    {snapshot.detail.summary.retained_digest: snapshot.detail}
                    if snapshot.detail is not None
                    else {}
                )
            return RoutingExecutionIndexResponse(
                routing_executions=tuple(
                    item
                    for item in snapshot.entries
                    if isinstance(item, RoutingExecutionSummary)
                ),
                rejected=tuple(
                    item
                    for item in snapshot.entries
                    if isinstance(item, RejectedRoutingExecution)
                ),
                page=PageView(
                    limit=limit,
                    returned=len(snapshot.entries),
                    total=len(snapshot.entries),
                    has_more=False,
                    next_cursor=None,
                ),
            )
        finally:
            self._build_slot.release()

    def get_execution(self, execution_id: str) -> RoutingExecutionDetail:
        if execution_id != _EXECUTION_ID:
            raise DashboardRoutingExecutionNotFound(
                "routing execution is not present in the verified index"
            )
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            self.refresh()
            with self._lock:
                snapshot = self._snapshot
        if snapshot is None or snapshot.detail is None:
            raise DashboardRoutingExecutionNotFound(
                "routing execution is not present in the verified index"
            )
        return snapshot.detail
