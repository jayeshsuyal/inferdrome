"""Verified causal projection over one sealed R1 package and PR4 descriptor.

The qualification descriptor is never treated as standalone evidence.  Each
snapshot first independently replays its configured source package, then
rebinds the canonical descriptor to that exact in-memory source snapshot.
Only the allowlisted causal facts and the verified descriptor bytes are kept.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore, RLock
from typing import Literal

from inferdrome.dashboard.models import PageView
from inferdrome.dashboard.routing_qualification_models import (
    RejectedRoutingQualification,
    RoutingQualificationDetail,
    RoutingQualificationEndpointStateView,
    RoutingQualificationFaultView,
    RoutingQualificationIndexResponse,
    RoutingQualificationPopulationEntry,
    RoutingQualificationResetView,
    RoutingQualificationSummary,
    RoutingQualificationTrialView,
)
from inferdrome.errors import (
    DashboardError,
    DashboardPaginationError,
    DashboardRoutingQualificationNotFound,
    InferdromeError,
    WorkLimitError,
)
from inferdrome.limits import WorkBudget, WorkLimits
from inferdrome.routing_campaign import VerifiedCampaign, load_verified_campaign
from inferdrome.routing_qualification import (
    CapturedQualification,
    StaleTelemetryQualificationError,
    verify_qualification_against_verified,
)

_QUALIFICATION_ID = "stale-telemetry-qualification-v1"
_MAX_PAGE_LIMIT = 25
_QUALIFICATION_MAX_BYTES = 524_288
_SNAPSHOT_WORK = WorkLimits(
    max_units=2,
    max_bytes=67_108_864 + _QUALIFICATION_MAX_BYTES,
    max_seconds=30,
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FocalFallbackReason = Literal[
    "REQUIRED_LOAD_STALE", "STALE_LOAD_FAIL_OPEN", "HEALTH_ONLY_TIE_BREAK"
]
_ENDPOINT_ORDER = ("endpoint-a", "endpoint-b")
_FOCAL_FALLBACK_REASONS: tuple[_FocalFallbackReason, ...] = (
    "REQUIRED_LOAD_STALE",
    "STALE_LOAD_FAIL_OPEN",
    "HEALTH_ONLY_TIE_BREAK",
)


@dataclass(frozen=True)
class _VerifiedRoutingQualificationSnapshot:
    entries: tuple[RoutingQualificationSummary | RejectedRoutingQualification, ...]
    detail: RoutingQualificationDetail | None
    descriptor_bytes: bytes | None


def _entry_label(_: Path | None) -> str:
    """Keep configured local paths out of browser-facing rejections."""

    return "<configured-root>"


def _summary(captured: CapturedQualification) -> RoutingQualificationSummary:
    descriptor = captured.descriptor
    return RoutingQualificationSummary(
        qualification_id=descriptor.qualification_id,
        retained_digest=captured.retained_digest,
        source_campaign_id=descriptor.source_campaign_id,
        source_package_retained_digest=descriptor.source_package_retained_digest,
        source_execution_mode=descriptor.source_execution_mode,
        repetitions_per_mode=descriptor.repetitions_per_mode,
        population_accounting=descriptor.population_accounting,
    )


def _focal_endpoint_states(
    verified: VerifiedCampaign,
    trial_id: str,
) -> tuple[
    RoutingQualificationEndpointStateView, RoutingQualificationEndpointStateView
]:
    try:
        decision = verified.decisions[trial_id][2]
    except (KeyError, IndexError):
        raise DashboardError(
            "verified qualification focal decision is unavailable"
        ) from None
    if decision.request_id != "request-002" or decision.decision_time_ms != 20:
        raise DashboardError("verified qualification focal decision disagrees")
    if len(decision.candidates) != len(_ENDPOINT_ORDER):
        raise DashboardError("verified qualification candidate inventory disagrees")
    rows: list[RoutingQualificationEndpointStateView] = []
    for endpoint_id, candidate in zip(
        _ENDPOINT_ORDER, decision.candidates, strict=True
    ):
        if candidate.endpoint_id != endpoint_id:
            raise DashboardError("verified qualification candidate order disagrees")
        health = candidate.health
        load = candidate.load
        if (
            health.endpoint_id != endpoint_id
            or load.endpoint_id != endpoint_id
            or health.age_ms != 0
            or health.admissibility != "ADMISSIBLE"
            or load.age_ms != 10
            or load.admissibility != "INADMISSIBLE"
            or health.freshness_bound_ms != 5
            or load.freshness_bound_ms != 5
        ):
            raise DashboardError("verified qualification focal telemetry disagrees")
        rows.append(
            RoutingQualificationEndpointStateView(
                endpoint_id=endpoint_id,
                health_epoch=health.epoch,
                health_age_ms=0,
                health_admissibility=health.admissibility,
                load_epoch=load.epoch,
                load_age_ms=10,
                load_admissibility=load.admissibility,
            )
        )
    return rows[0], rows[1]


def _trial_view(
    captured: CapturedQualification,
    verified: VerifiedCampaign,
    index: int,
) -> RoutingQualificationTrialView:
    descriptor_trial = captured.descriptor.trials[index]
    try:
        source_trial = verified.trial_plan.trials[index]
        reset = verified.resets[source_trial.trial_id]
        decision = verified.decisions[source_trial.trial_id][2]
        terminal = verified.terminals[source_trial.trial_id][2]
    except (KeyError, IndexError):
        raise DashboardError("verified qualification trial records disagree") from None
    if (
        source_trial.trial_id != descriptor_trial.trial_id
        or source_trial.policy_id != descriptor_trial.policy_id
        or decision.request_id != "request-002"
        or decision.decision_id != terminal.decision_id
        or decision.terminal_outcome_id != terminal.terminal_outcome_id
        or decision.decision_time_ms != 20
        or terminal.started_at_ms != 20
        or reset.virtual_time_ms != 0
        or not reset.queue_cleared
        or not reset.load_state_cleared
        or not reset.kv_state_cleared
        or decision.fallback_reason not in _FOCAL_FALLBACK_REASONS
    ):
        raise DashboardError("verified qualification causal records disagree")
    try:
        endpoint_a_instance_id = reset.endpoint_instance_ids["endpoint-a"]
        endpoint_b_instance_id = reset.endpoint_instance_ids["endpoint-b"]
    except KeyError:
        raise DashboardError(
            "verified qualification reset identity disagrees"
        ) from None
    epoch_rows = tuple(epoch for _, epoch in sorted(reset.observer_epochs.items()))
    if len(epoch_rows) != 3:
        raise DashboardError("verified qualification observer epochs disagree")
    population_values = descriptor_trial.terminal_population.values
    populations = (
        RoutingQualificationPopulationEntry(
            status="SUCCEEDED", count=population_values["SUCCEEDED"]
        ),
        RoutingQualificationPopulationEntry(
            status="TIMED_OUT", count=population_values["TIMED_OUT"]
        ),
        RoutingQualificationPopulationEntry(
            status="FAILED", count=population_values["FAILED"]
        ),
        RoutingQualificationPopulationEntry(
            status="CANCELLED", count=population_values["CANCELLED"]
        ),
        RoutingQualificationPopulationEntry(
            status="NO_SAFE_ROUTE", count=population_values["NO_SAFE_ROUTE"]
        ),
    )
    if sum(item.count for item in populations) != descriptor_trial.request_denominator:
        raise DashboardError("verified qualification terminal population disagrees")
    return RoutingQualificationTrialView(
        policy_id=descriptor_trial.policy_id,
        repetition_index=descriptor_trial.repetition_index,
        trial_id=descriptor_trial.trial_id,
        request_denominator=descriptor_trial.request_denominator,
        reset=RoutingQualificationResetView(
            virtual_time_ms=reset.virtual_time_ms,
            endpoint_a_instance_id=endpoint_a_instance_id,
            endpoint_b_instance_id=endpoint_b_instance_id,
            observer_epochs=(epoch_rows[0], epoch_rows[1], epoch_rows[2]),
            queue_cleared=reset.queue_cleared,
            load_state_cleared=reset.load_state_cleared,
            kv_state_cleared=reset.kv_state_cleared,
        ),
        focal_request_id="request-002",
        focal_decision_id=decision.decision_id,
        focal_endpoint_states=_focal_endpoint_states(verified, source_trial.trial_id),
        selected_endpoint_id=decision.selected_endpoint_id,
        fallback_reason=decision.fallback_reason,
        terminal_status=terminal.status,
        terminal_reason=terminal.reason,
        reset_receipt_sha256=descriptor_trial.reset_receipt_sha256,
        state_observations_sha256=descriptor_trial.state_observations_sha256,
        route_decisions_sha256=descriptor_trial.route_decisions_sha256,
        terminal_outcomes_sha256=descriptor_trial.terminal_outcomes_sha256,
        terminal_population=populations,
        terminal_population_total=descriptor_trial.request_denominator,
    )


def _project(
    captured: CapturedQualification,
    verified: VerifiedCampaign,
) -> RoutingQualificationDetail:
    descriptor = captured.descriptor
    if (
        descriptor.qualification_id != _QUALIFICATION_ID
        or descriptor.source_campaign_id != verified.plan.campaign_id
        or descriptor.source_package_retained_digest != verified.report.retained_digest
        or len(descriptor.trials) != 3
    ):
        raise DashboardError("verified qualification identity disagrees")
    return RoutingQualificationDetail(
        summary=_summary(captured),
        fault_timeline=RoutingQualificationFaultView(
            load_observer_pause_at_ms=descriptor.fault_timeline.load_observer_pause_at_ms,
            health_collection_continues=descriptor.fault_timeline.health_continues,
            focal_decision_time_ms=(
                descriptor.fault_timeline.first_stale_load_fresh_health_decision_time_ms
            ),
            health_age_ms=descriptor.fault_timeline.health_age_ms,
            load_age_ms=descriptor.fault_timeline.load_age_ms,
            freshness_bound_ms=descriptor.fault_timeline.freshness_bound_ms,
        ),
        trials=(
            _trial_view(captured, verified, 0),
            _trial_view(captured, verified, 1),
            _trial_view(captured, verified, 2),
        ),
    )


class RoutingQualificationDashboardIndex:
    """Serve one causal descriptor only after source-and-overlay verification."""

    def __init__(
        self,
        *,
        routing_campaigns_root: Path | None = None,
        routing_qualifications_root: Path | None = None,
        expected_qualification_digest: str | None = None,
    ) -> None:
        self.routing_campaigns_root = (
            routing_campaigns_root.absolute()
            if routing_campaigns_root is not None
            else None
        )
        self.routing_qualifications_root = (
            routing_qualifications_root.absolute()
            if routing_qualifications_root is not None
            else None
        )
        self.expected_qualification_digest = expected_qualification_digest
        self._snapshot: _VerifiedRoutingQualificationSnapshot | None = None
        self._lock = RLock()
        self._build_slot = BoundedSemaphore(value=1)

    def _configuration_error(self) -> RejectedRoutingQualification | None:
        values = (
            self.routing_campaigns_root,
            self.routing_qualifications_root,
            self.expected_qualification_digest,
        )
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            return RejectedRoutingQualification(
                entry=_entry_label(self.routing_qualifications_root),
                code="CONFIGURATION_INVALID",
            )
        if not isinstance(
            self.expected_qualification_digest, str
        ) or not _DIGEST.fullmatch(self.expected_qualification_digest):
            return RejectedRoutingQualification(
                entry=_entry_label(self.routing_qualifications_root),
                code="CONFIGURATION_INVALID",
            )
        return None

    @staticmethod
    def _is_safe_directory(root: Path) -> bool:
        try:
            metadata = os.lstat(root)
        except OSError:
            return False
        return not stat.S_ISLNK(metadata.st_mode) and stat.S_ISDIR(metadata.st_mode)

    def _verify_configured_pair(self) -> _VerifiedRoutingQualificationSnapshot:
        configuration_error = self._configuration_error()
        if configuration_error is not None:
            return _VerifiedRoutingQualificationSnapshot(
                entries=(configuration_error,), detail=None, descriptor_bytes=None
            )
        campaign_root = self.routing_campaigns_root
        qualification_root = self.routing_qualifications_root
        expected_digest = self.expected_qualification_digest
        if campaign_root is None:
            return _VerifiedRoutingQualificationSnapshot((), None, None)
        if qualification_root is None or expected_digest is None:
            return _VerifiedRoutingQualificationSnapshot(
                entries=(
                    RejectedRoutingQualification(
                        entry=_entry_label(qualification_root),
                        code="CONFIGURATION_INVALID",
                    ),
                ),
                detail=None,
                descriptor_bytes=None,
            )
        if not self._is_safe_directory(campaign_root) or not self._is_safe_directory(
            qualification_root
        ):
            return _VerifiedRoutingQualificationSnapshot(
                entries=(
                    RejectedRoutingQualification(
                        entry=_entry_label(qualification_root),
                        code="UNSAFE_ENTRY",
                    ),
                ),
                detail=None,
                descriptor_bytes=None,
            )
        if qualification_root.name.startswith(".inferdrome-qualification-stage-"):
            return _VerifiedRoutingQualificationSnapshot(
                entries=(
                    RejectedRoutingQualification(
                        entry=_entry_label(qualification_root), code="UNSAFE_ENTRY"
                    ),
                ),
                detail=None,
                descriptor_bytes=None,
            )
        budget = WorkBudget(_SNAPSHOT_WORK)
        try:
            budget.reserve(units=2, bytes_=_SNAPSHOT_WORK.max_bytes)
            verified = load_verified_campaign(
                campaign_root,
                require_immutable=True,
            )
            captured = verify_qualification_against_verified(
                qualification_root,
                verified=verified,
                expected_descriptor_digest=expected_digest,
            )
            budget.checkpoint()
            detail = _project(captured, verified)
            return _VerifiedRoutingQualificationSnapshot(
                entries=(detail.summary,),
                detail=detail,
                descriptor_bytes=captured.canonical_bytes,
            )
        except (
            DashboardError,
            InferdromeError,
            StaleTelemetryQualificationError,
            OSError,
            RecursionError,
            ValueError,
            WorkLimitError,
        ):
            return _VerifiedRoutingQualificationSnapshot(
                entries=(
                    RejectedRoutingQualification(
                        entry=_entry_label(qualification_root),
                        code="VERIFICATION_FAILED",
                    ),
                ),
                detail=None,
                descriptor_bytes=None,
            )

    def refresh(
        self,
        *,
        cursor: str | None = None,
        limit: int = _MAX_PAGE_LIMIT,
    ) -> RoutingQualificationIndexResponse:
        """Reverify the configured pair before returning any causal facts."""

        if (
            cursor is not None
            or isinstance(limit, bool)
            or not 1 <= limit <= _MAX_PAGE_LIMIT
        ):
            raise DashboardPaginationError("routing-qualification page is invalid")
        if not self._build_slot.acquire(blocking=False):
            raise DashboardError(
                "routing-qualification snapshot concurrency limit exceeded"
            )
        try:
            snapshot = self._verify_configured_pair()
            with self._lock:
                self._snapshot = snapshot
            return RoutingQualificationIndexResponse(
                routing_qualifications=tuple(
                    entry
                    for entry in snapshot.entries
                    if isinstance(entry, RoutingQualificationSummary)
                ),
                rejected=tuple(
                    entry
                    for entry in snapshot.entries
                    if isinstance(entry, RejectedRoutingQualification)
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

    def _snapshot_for_detail(self) -> _VerifiedRoutingQualificationSnapshot:
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            self.refresh()
            with self._lock:
                snapshot = self._snapshot
        if snapshot is None:
            raise DashboardError("routing-qualification snapshot is unavailable")
        return snapshot

    def get_qualification(self, qualification_id: str) -> RoutingQualificationDetail:
        if qualification_id != _QUALIFICATION_ID:
            raise DashboardRoutingQualificationNotFound(
                "routing qualification is not present in the verified index"
            )
        detail = self._snapshot_for_detail().detail
        if detail is None:
            raise DashboardRoutingQualificationNotFound(
                "routing qualification is not present in the verified index"
            )
        return detail

    def get_evidence(self, qualification_id: str) -> tuple[bytes, str]:
        """Return only canonical descriptor bytes retained in a verified snapshot."""

        self.get_qualification(qualification_id)
        descriptor_bytes = self._snapshot_for_detail().descriptor_bytes
        if descriptor_bytes is None:
            raise DashboardRoutingQualificationNotFound(
                "routing qualification is not present in the verified index"
            )
        return descriptor_bytes, self.expected_qualification_digest or ""
