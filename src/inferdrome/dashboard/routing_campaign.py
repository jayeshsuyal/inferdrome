"""Verified, bounded, read-only projection of one sealed R1 routing campaign."""

import base64
import binascii
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore, RLock

from inferdrome.dashboard.models import PageView
from inferdrome.dashboard.routing_campaign_models import (
    RejectedRoutingCampaign,
    RoutingCampaignDetail,
    RoutingCampaignIndexResponse,
    RoutingCampaignSummary,
    RoutingCampaignTrialView,
    RoutingCandidateView,
    RoutingEndpointInstanceView,
    RoutingFaultTimelineView,
    RoutingObserverEpochView,
    RoutingRequestView,
    RoutingResetView,
    RoutingTelemetryView,
    RoutingTerminalOutcomeView,
    RoutingTerminalPopulationView,
)
from inferdrome.errors import (
    DashboardError,
    DashboardPaginationError,
    DashboardRoutingCampaignNotFound,
    InferdromeError,
    WorkLimitError,
)
from inferdrome.limits import WorkBudget, WorkLimits
from inferdrome.routing_campaign.contracts import (
    CandidateState,
    EndpointId,
    Observation,
    TerminalStatus,
)
from inferdrome.routing_campaign.package import VerifiedCampaign, load_verified_campaign

_CAMPAIGN_ID = "routing-campaign-v1"
_MAX_PAGE_LIMIT = 25
_MAX_CURSOR_LENGTH = 128
_PACKAGE_MAX_BYTES = 67_108_864
_SNAPSHOT_WORK = WorkLimits(max_units=1, max_bytes=_PACKAGE_MAX_BYTES, max_seconds=30)
_TERMINAL_STATUS_ORDER: tuple[TerminalStatus, ...] = (
    "SUCCEEDED",
    "TIMED_OUT",
    "FAILED",
    "CANCELLED",
    "NO_SAFE_ROUTE",
)
_ENDPOINT_ID_ORDER: tuple[EndpointId, ...] = ("endpoint-a", "endpoint-b")


@dataclass(frozen=True)
class _VerifiedRoutingCampaignSnapshot:
    snapshot_id: str
    entries: tuple[RoutingCampaignSummary | RejectedRoutingCampaign, ...]
    details: tuple[tuple[str, RoutingCampaignDetail], ...]


def _entry_label(_: Path) -> str:
    """Keep a configured package location out of browser-facing rejections."""

    return "<configured-root>"


def _encode_cursor(offset: int, snapshot_id: str) -> str:
    encoded = base64.urlsafe_b64encode(f"v1:{offset}:{snapshot_id}".encode("ascii"))
    return encoded.decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[int, str | None]:
    if cursor is None:
        return 0, None
    if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
        raise DashboardPaginationError("routing-campaign cursor is invalid")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            padded,
            altchars=b"-_",
            validate=True,
        ).decode("ascii")
        version, raw_offset, snapshot_id = decoded.split(":")
        offset = int(raw_offset)
    except (ValueError, UnicodeError, binascii.Error):
        raise DashboardPaginationError("routing-campaign cursor is invalid") from None
    if (
        version != "v1"
        or str(offset) != raw_offset
        or not 0 <= offset <= 1
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot_id)
    ):
        raise DashboardPaginationError("routing-campaign cursor is invalid")
    return offset, snapshot_id


def _snapshot_id(
    entries: tuple[RoutingCampaignSummary | RejectedRoutingCampaign, ...],
) -> str:
    digest = hashlib.sha256(b"inferdrome.routing-campaign-dashboard.snapshot.v1\0")
    for entry in entries:
        if isinstance(entry, RoutingCampaignSummary):
            identity = f"campaign:{entry.campaign_id}:{entry.retained_digest}"
        else:
            identity = f"rejected:{entry.entry}:{entry.code}"
        digest.update(identity.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _telemetry(observation: Observation) -> RoutingTelemetryView:
    """Allowlist one already verified R1 observation into the dashboard shape."""

    # The source object is an immutable R1 Observation. Attribute reads retain
    # its declared literals without exposing an arbitrary model dump.
    return RoutingTelemetryView(
        signal=observation.signal,
        observer_id=observation.observer_id,
        endpoint_id=observation.endpoint_id,
        epoch=observation.epoch,
        observed_at_ms=observation.observed_at_ms,
        decision_time_ms=observation.decision_time_ms,
        age_ms=observation.age_ms,
        freshness_bound_ms=observation.freshness_bound_ms,
        value=observation.value,
        admissibility=observation.admissibility,
    )


def _candidate(candidate: CandidateState) -> RoutingCandidateView:
    return RoutingCandidateView(
        endpoint_id=candidate.endpoint_id,
        eligible=candidate.eligible,
        health=_telemetry(candidate.health),
        load=_telemetry(candidate.load),
        kv=_telemetry(candidate.kv),
    )


def _summary(verified: VerifiedCampaign) -> RoutingCampaignSummary:
    return RoutingCampaignSummary(
        campaign_id=verified.plan.campaign_id,
        retained_digest=verified.report.retained_digest,
        execution_mode=verified.plan.execution_mode,
        trial_count=verified.report.trial_count,
        planned_request_count=verified.report.planned_request_count,
        policy_ids=verified.plan.policies,
    )


def _project_campaign(verified: VerifiedCampaign) -> RoutingCampaignDetail:
    """Project only in-memory records returned from a stable verifier snapshot."""

    trials: list[RoutingCampaignTrialView] = []
    for trial in verified.trial_plan.trials:
        try:
            reset = verified.resets[trial.trial_id]
            decisions = verified.decisions[trial.trial_id]
            terminals = verified.terminals[trial.trial_id]
            trial_summary = verified.summaries[trial.trial_id]
        except KeyError:
            raise DashboardError("verified routing campaign records disagree") from None
        requests: list[RoutingRequestView] = []
        for decision, terminal in zip(decisions, terminals, strict=True):
            if (
                decision.request_id != terminal.request_id
                or decision.sequence_index != terminal.sequence_index
                or decision.decision_id != terminal.decision_id
                or decision.terminal_outcome_id != terminal.terminal_outcome_id
            ):
                raise DashboardError("verified routing campaign closure disagrees")
            requests.append(
                RoutingRequestView(
                    request_id=decision.request_id,
                    sequence_index=decision.sequence_index,
                    decision_id=decision.decision_id,
                    decision_time_ms=decision.decision_time_ms,
                    candidates=tuple(_candidate(item) for item in decision.candidates),
                    selected_endpoint_id=decision.selected_endpoint_id,
                    claims_used=decision.claims_used,
                    claims_permitted_stale=decision.claims_permitted_stale,
                    claims_discarded=decision.claims_discarded,
                    fallback_reason=decision.fallback_reason,
                    terminal=RoutingTerminalOutcomeView(
                        terminal_outcome_id=terminal.terminal_outcome_id,
                        decision_id=terminal.decision_id,
                        status=terminal.status,
                        reason=terminal.reason,
                        started_at_ms=terminal.started_at_ms,
                        ended_at_ms=terminal.ended_at_ms,
                    ),
                )
            )
        terminal_population = tuple(
            RoutingTerminalPopulationView(
                status=status,
                count=trial_summary.terminal_population[status],
            )
            for status in _TERMINAL_STATUS_ORDER
        )
        terminal_population_total = sum(item.count for item in terminal_population)
        if terminal_population_total != len(requests):
            raise DashboardError("verified routing terminal population disagrees")
        trials.append(
            RoutingCampaignTrialView(
                trial_id=trial.trial_id,
                policy_id=trial.policy_id,
                reset=RoutingResetView(
                    virtual_time_ms=reset.virtual_time_ms,
                    endpoint_instances=tuple(
                        RoutingEndpointInstanceView(
                            endpoint_id=endpoint_id,
                            instance_id=reset.endpoint_instance_ids[endpoint_id],
                        )
                        for endpoint_id in _ENDPOINT_ID_ORDER
                    ),
                    observer_epochs=tuple(
                        RoutingObserverEpochView(
                            observer_id=observer_id,
                            epoch=epoch,
                        )
                        for observer_id, epoch in sorted(reset.observer_epochs.items())
                    ),
                    queue_cleared=reset.queue_cleared,
                    load_state_cleared=reset.load_state_cleared,
                    kv_state_cleared=reset.kv_state_cleared,
                ),
                requests=tuple(requests),
                terminal_population=terminal_population,
                terminal_population_total=terminal_population_total,
            )
        )
    return RoutingCampaignDetail(
        summary=_summary(verified),
        fault_timeline=RoutingFaultTimelineView(
            load_collection_paused_at_ms=(
                verified.fault_schedule.load_observer_pause_at_ms
            ),
            health_collection_continues=verified.fault_schedule.health_continues,
            load_freshness_bound_ms=verified.plan.load_observer.freshness_bound_ms,
            health_freshness_bound_ms=(
                verified.plan.health_observer.freshness_bound_ms
            ),
        ),
        trials=tuple(trials),
    )


class RoutingCampaignDashboardIndex:
    """Serve at most one explicitly configured, verified R1 campaign package."""

    def __init__(self, routing_campaigns_root: Path | None = None) -> None:
        self.routing_campaigns_root = (
            routing_campaigns_root.absolute()
            if routing_campaigns_root is not None
            else None
        )
        self._cache_by_digest: dict[str, RoutingCampaignDetail] = {}
        self._details: dict[str, RoutingCampaignDetail] = {}
        self._snapshot: _VerifiedRoutingCampaignSnapshot | None = None
        self._lock = RLock()
        self._build_slot = BoundedSemaphore(value=1)

    def _new_snapshot(
        self,
        entries: tuple[RoutingCampaignSummary | RejectedRoutingCampaign, ...],
        details: tuple[tuple[str, RoutingCampaignDetail], ...],
    ) -> _VerifiedRoutingCampaignSnapshot:
        return _VerifiedRoutingCampaignSnapshot(
            snapshot_id=_snapshot_id(entries),
            entries=entries,
            details=details,
        )

    def _verify_configured_root(
        self,
    ) -> tuple[
        tuple[RoutingCampaignSummary | RejectedRoutingCampaign, ...],
        tuple[tuple[str, RoutingCampaignDetail], ...],
    ]:
        root = self.routing_campaigns_root
        if root is None:
            return (), ()
        label = _entry_label(root)
        try:
            metadata = os.lstat(root)
        except FileNotFoundError:
            return (), ()
        except OSError:
            return (
                (RejectedRoutingCampaign(entry=label, code="UNSAFE_ENTRY"),),
                (),
            )
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return (
                (RejectedRoutingCampaign(entry=label, code="UNSAFE_ENTRY"),),
                (),
            )
        if root.name.startswith(".routing-campaign-stage-"):
            return (
                (RejectedRoutingCampaign(entry=label, code="UNSAFE_ENTRY"),),
                (),
            )
        budget = WorkBudget(_SNAPSHOT_WORK)
        try:
            budget.reserve(units=1, bytes_=_PACKAGE_MAX_BYTES)
            verified = load_verified_campaign(root, require_immutable=True)
            budget.checkpoint()
            with self._lock:
                detail = self._cache_by_digest.get(verified.report.retained_digest)
            if detail is None:
                detail = _project_campaign(verified)
            if (
                detail.summary.campaign_id != verified.plan.campaign_id
                or detail.summary.retained_digest != verified.report.retained_digest
            ):
                raise DashboardError("verified routing campaign cache disagrees")
        except (
            DashboardError,
            InferdromeError,
            OSError,
            RecursionError,
            ValueError,
            WorkLimitError,
        ):
            return (
                (RejectedRoutingCampaign(entry=label, code="VERIFICATION_FAILED"),),
                (),
            )
        return (detail.summary,), ((detail.summary.campaign_id, detail),)

    def refresh(
        self,
        *,
        cursor: str | None = None,
        limit: int = 25,
    ) -> RoutingCampaignIndexResponse:
        """Verify the configured root before returning any campaign projection."""

        if isinstance(limit, bool) or not 1 <= limit <= _MAX_PAGE_LIMIT:
            raise DashboardPaginationError("routing-campaign page limit is invalid")
        offset, expected_snapshot_id = _decode_cursor(cursor)
        if expected_snapshot_id is not None:
            with self._lock:
                snapshot = self._snapshot
                if snapshot is None or snapshot.snapshot_id != expected_snapshot_id:
                    raise DashboardPaginationError(
                        "routing-campaign cursor refers to a stale snapshot"
                    )
                return self._page(snapshot, offset=offset, limit=limit)
        if not self._build_slot.acquire(blocking=False):
            raise DashboardError("routing-campaign snapshot concurrency limit exceeded")
        try:
            entries, details = self._verify_configured_root()
            snapshot = self._new_snapshot(entries, details)
            with self._lock:
                self._cache_by_digest = {
                    detail.summary.retained_digest: detail
                    for _, detail in snapshot.details
                }
                self._snapshot = snapshot
                self._details = dict(snapshot.details)
            return self._page(snapshot, offset=offset, limit=limit)
        finally:
            self._build_slot.release()

    @staticmethod
    def _page(
        snapshot: _VerifiedRoutingCampaignSnapshot,
        *,
        offset: int,
        limit: int,
    ) -> RoutingCampaignIndexResponse:
        if offset > len(snapshot.entries):
            raise DashboardPaginationError(
                "routing-campaign cursor is outside the current snapshot"
            )
        page_entries = snapshot.entries[offset : offset + limit]
        next_offset = offset + len(page_entries)
        has_more = next_offset < len(snapshot.entries)
        return RoutingCampaignIndexResponse(
            routing_campaigns=tuple(
                item
                for item in page_entries
                if isinstance(item, RoutingCampaignSummary)
            ),
            rejected=tuple(
                item
                for item in page_entries
                if isinstance(item, RejectedRoutingCampaign)
            ),
            page=PageView(
                limit=limit,
                returned=len(page_entries),
                total=len(snapshot.entries),
                has_more=has_more,
                next_cursor=(
                    _encode_cursor(next_offset, snapshot.snapshot_id)
                    if has_more
                    else None
                ),
            ),
        )

    def get_campaign(self, campaign_id: str) -> RoutingCampaignDetail:
        if campaign_id != _CAMPAIGN_ID:
            raise DashboardRoutingCampaignNotFound(
                "routing campaign is not present in the verified index"
            )
        with self._lock:
            has_snapshot = self._snapshot is not None
        if not has_snapshot:
            self.refresh()
        with self._lock:
            detail = self._details.get(campaign_id)
        if detail is None:
            raise DashboardRoutingCampaignNotFound(
                "routing campaign is not present in the verified index"
            )
        return detail
