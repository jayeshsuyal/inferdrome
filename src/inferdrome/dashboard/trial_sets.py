"""Bounded trial-set discovery and verified run-level dashboard projections."""

import base64
import binascii
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from inferdrome.dashboard.models import (
    PageView,
    RejectedTrialSet,
    RunDetail,
    TrialMetricVariationView,
    TrialRunPointView,
    TrialSetDetail,
    TrialSetIndexResponse,
    TrialSetMemberView,
    TrialSetSummary,
)
from inferdrome.dashboard.projection import (
    display_measurement,
    metric_label,
    project_run_detail,
)
from inferdrome.dashboard.work import DashboardLimits, DashboardWorkController
from inferdrome.domain.metrics import Unit
from inferdrome.errors import (
    DashboardError,
    DashboardPaginationError,
    DashboardTrialSetNotFound,
    InferdromeError,
    WorkLimitError,
)
from inferdrome.immutable import is_internal_staging_entry
from inferdrome.limits import WorkBudget, collect_bounded
from inferdrome.trials import (
    TrialMetricVariation,
    VerifiedTrialSet,
    trial_metric_variations,
    verify_trial_set,
)

_TRIAL_SET_ID = re.compile(r"^trial-set-[0-9a-f]{32}$")
_SAFE_ENTRY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_DISCOVERED_ENTRIES = 200
_DEFAULT_PAGE_LIMIT = 100
_MAX_PAGE_LIMIT = 200
_MAX_CURSOR_LENGTH = 128


@dataclass(frozen=True)
class _Candidate:
    entry: str
    path: Path


@dataclass(frozen=True)
class _VerifiedTrialSetSnapshot:
    snapshot_id: str
    generated_at: datetime
    entries: tuple[TrialSetSummary | RejectedTrialSet, ...]
    details: tuple[tuple[str, TrialSetDetail], ...]


def _entry_label(name: str) -> str:
    return name if _SAFE_ENTRY.fullmatch(name) else "<unsafe-entry>"


def _real_directory(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _regular_file(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _encode_cursor(offset: int, snapshot_id: str) -> str:
    encoded = base64.urlsafe_b64encode(
        f"v1:{offset}:{snapshot_id}".encode("ascii")
    )
    return encoded.decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[int, str | None]:
    if cursor is None:
        return 0, None
    if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
        raise DashboardPaginationError("trial-set cursor is invalid")
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
        raise DashboardPaginationError("trial-set cursor is invalid") from None
    if (
        version != "v1"
        or str(offset) != raw_offset
        or not 0 <= offset <= _MAX_DISCOVERED_ENTRIES
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot_id)
    ):
        raise DashboardPaginationError("trial-set cursor is invalid")
    return offset, snapshot_id


def _snapshot_id(
    entries: tuple[TrialSetSummary | RejectedTrialSet, ...],
) -> str:
    digest = hashlib.sha256(b"inferdrome.dashboard.trial-set-snapshot.v1\0")
    for entry in entries:
        if isinstance(entry, TrialSetSummary):
            identity = f"trial:{entry.trial_set_id}:{entry.trial_set_digest}"
        else:
            identity = f"rejected:{entry.entry}:{entry.code}"
        digest.update(identity.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _display_optional(value: str | None, unit: Unit) -> str | None:
    return None if value is None else display_measurement(value, unit)


def _variation_view(
    variation: TrialMetricVariation,
    verified: VerifiedTrialSet,
) -> TrialMetricVariationView:
    unit = Unit(variation.unit)
    points = tuple(
        TrialRunPointView(
            repetition_index=member.repetition_index,
            run_id=point.run_id,
            value=point.value,
            display_value=_display_optional(point.value, unit),
            sample_count=point.sample_count,
        )
        for member, point in zip(
            verified.descriptor.members,
            variation.values,
            strict=True,
        )
    )
    return TrialMetricVariationView(
        key=f"{variation.metric}:{variation.aggregation}",
        metric=variation.metric,
        aggregation=variation.aggregation,
        label=metric_label(variation.metric, variation.aggregation),
        unit=variation.unit,
        total_run_count=len(verified.members),
        available_run_count=variation.available_run_count,
        minimum=variation.minimum,
        maximum=variation.maximum,
        median=variation.median,
        mean=variation.mean,
        span=variation.span,
        sample_standard_deviation=variation.sample_standard_deviation,
        minimum_display_value=_display_optional(variation.minimum, unit),
        maximum_display_value=_display_optional(variation.maximum, unit),
        median_display_value=_display_optional(variation.median, unit),
        mean_display_value=_display_optional(variation.mean, unit),
        span_display_value=_display_optional(variation.span, unit),
        sample_standard_deviation_display_value=_display_optional(
            variation.sample_standard_deviation,
            unit,
        ),
        points=points,
    )


def _environment_drift_fields(details: tuple[RunDetail, ...]) -> tuple[str, ...]:
    if not details:
        return ()
    normalized: list[dict[str, tuple[object, str]]] = []
    for detail in details:
        normalized.append(
            {
                field.name: (field.value, field.provenance)
                for field in detail.environment
            }
        )
    baseline = normalized[0]
    changed = {
        name
        for current in normalized[1:]
        for name in set(baseline) | set(current)
        if baseline.get(name) != current.get(name)
    }
    return tuple(sorted(changed))


def project_trial_set(
    verified: VerifiedTrialSet,
    *,
    work_budget: WorkBudget | None = None,
) -> TrialSetDetail:
    """Project one verified grouping without pooling member request records."""

    details = tuple(
        project_run_detail(member, work_budget=work_budget)
        for member in verified.members
    )
    descriptor = verified.descriptor
    drift_fields = _environment_drift_fields(details)
    summaries = tuple(detail.summary for detail in details)
    summary = TrialSetSummary(
        trial_set_id=descriptor.trial_set_id,
        experiment_id=descriptor.experiment_id,
        title=descriptor.title,
        created_at=descriptor.created_at,
        member_count=len(summaries),
        earliest_run_at=min(item.started_at for item in summaries),
        latest_run_at=max(item.ended_at for item in summaries),
        model=summaries[0].model,
        execution_fingerprint=descriptor.execution_fingerprint,
        trial_set_digest=verified.trial_set_digest,
        evidence_eligibilities=tuple(
            sorted({item.evidence_eligibility for item in summaries})
        ),
        environment_status=(
            "DRIFT_DETECTED" if drift_fields else "CONSISTENT"
        ),
    )
    return TrialSetDetail(
        summary=summary,
        hypothesis=descriptor.hypothesis,
        membership_policy=descriptor.membership_policy,
        metric_definitions_digest=descriptor.metric_definitions_digest,
        reducer_version=descriptor.reducer_version,
        members=tuple(
            TrialSetMemberView(
                repetition_index=member.repetition_index,
                run=detail.summary,
            )
            for member, detail in zip(
                descriptor.members,
                details,
                strict=True,
            )
        ),
        variations=tuple(
            _variation_view(variation, verified)
            for variation in trial_metric_variations(verified)
        ),
        environment_drift_fields=drift_fields,
    )


class TrialSetDashboardIndex:
    """Read-only index over immutable trial-set descriptors and member bundles."""

    def __init__(
        self,
        trial_sets_root: Path,
        runs_root: Path,
        *,
        limits: DashboardLimits | None = None,
        work_controller: DashboardWorkController | None = None,
    ) -> None:
        self.trial_sets_root = trial_sets_root.absolute()
        self.runs_root = runs_root.absolute()
        self.limits = limits or DashboardLimits()
        self._work_controller = work_controller or DashboardWorkController(self.limits)
        self._details: dict[str, TrialSetDetail] = {}
        self._cache_by_digest: dict[str, TrialSetDetail] = {}
        self._snapshot: _VerifiedTrialSetSnapshot | None = None
        self._lock = RLock()

    def _candidates(
        self,
    ) -> tuple[tuple[_Candidate, ...], tuple[RejectedTrialSet, ...]]:
        if not self.trial_sets_root.exists():
            return (), ()
        if not _real_directory(self.trial_sets_root):
            raise DashboardError("trial-sets root must be a real directory")
        if _regular_file(self.trial_sets_root / "trial-set.json"):
            return (
                (
                    _Candidate(
                        entry=_entry_label(self.trial_sets_root.name),
                        path=self.trial_sets_root,
                    ),
                ),
                (),
            )
        try:
            with os.scandir(self.trial_sets_root) as iterator:
                entries = sorted(
                    (
                        entry
                        for entry in collect_bounded(
                            iterator,
                            limit=self.limits.max_trial_set_entries,
                            error=lambda: DashboardError(
                                "trial-sets root exceeds the entry limit"
                            ),
                        )
                        if not is_internal_staging_entry(entry.name)
                    ),
                    key=lambda item: item.name,
                )
        except OSError:
            raise DashboardError("trial-sets root could not be scanned") from None
        candidates: list[_Candidate] = []
        rejected: list[RejectedTrialSet] = []
        for entry in entries:
            label = _entry_label(entry.name)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                rejected.append(
                    RejectedTrialSet(entry=label, code="UNSAFE_ENTRY")
                )
                continue
            if stat.S_ISLNK(metadata.st_mode):
                rejected.append(
                    RejectedTrialSet(entry=label, code="UNSAFE_ENTRY")
                )
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                continue
            path = Path(entry.path)
            if _regular_file(path / "trial-set.json"):
                candidates.append(_Candidate(entry=label, path=path))
            elif _TRIAL_SET_ID.fullmatch(entry.name):
                rejected.append(
                    RejectedTrialSet(
                        entry=label,
                        code="DECLARATION_UNAVAILABLE",
                    )
                )
        return tuple(candidates), tuple(rejected)

    def refresh(
        self,
        *,
        cursor: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> TrialSetIndexResponse:
        if isinstance(limit, bool) or not 1 <= limit <= _MAX_PAGE_LIMIT:
            raise DashboardPaginationError("trial-set page limit is invalid")
        offset, expected_snapshot_id = _decode_cursor(cursor)
        if expected_snapshot_id is not None:
            with self._lock:
                snapshot = self._snapshot
                if snapshot is None or snapshot.snapshot_id != expected_snapshot_id:
                    raise DashboardPaginationError(
                        "trial-set cursor refers to a stale snapshot"
                    )
                return self._page(snapshot, offset=offset, limit=limit)

        with self._work_controller.session(
            self.limits.trial_set_snapshot_work
        ) as budget:
            with self._lock:
                candidate_cache = dict(self._cache_by_digest)
            try:
                candidates, discovery_rejections = self._candidates()
                budget.checkpoint()
            except WorkLimitError:
                raise DashboardError(
                    "trial-set snapshot exceeded its work limits"
                ) from None
            rejected = list(discovery_rejections)
            detail_by_id: dict[str, TrialSetDetail] = {}
            source_by_id: dict[str, str] = {}
            duplicate_ids: set[str] = set()

            for candidate in candidates:
                try:
                    budget.reserve(units=1)
                    verified = verify_trial_set(
                        candidate.path,
                        runs_root=self.runs_root,
                        work_budget=budget,
                    )
                    detail = candidate_cache.get(verified.trial_set_digest)
                    if detail is None:
                        detail = project_trial_set(
                            verified,
                            work_budget=budget,
                        )
                        candidate_cache[verified.trial_set_digest] = detail
                    if (
                        detail.summary.trial_set_digest
                        != verified.trial_set_digest
                    ):
                        raise DashboardError("trial-set cache disagrees")
                    budget.checkpoint()
                except WorkLimitError:
                    raise DashboardError(
                        "trial-set snapshot exceeded its work limits"
                    ) from None
                except (
                    InferdromeError,
                    OSError,
                    RecursionError,
                    ValueError,
                ) as error:
                    if "member" in str(error):
                        rejected.append(
                            RejectedTrialSet(
                                entry=candidate.entry,
                                code="MEMBER_UNAVAILABLE",
                            )
                        )
                    else:
                        rejected.append(
                            RejectedTrialSet(
                                entry=candidate.entry,
                                code="VERIFICATION_FAILED",
                            )
                        )
                    continue

                trial_set_id = detail.summary.trial_set_id
                if trial_set_id in duplicate_ids:
                    rejected.append(
                        RejectedTrialSet(
                            entry=candidate.entry,
                            code="DUPLICATE_TRIAL_SET_ID",
                        )
                    )
                    continue
                if trial_set_id in detail_by_id:
                    duplicate_ids.add(trial_set_id)
                    first_entry = source_by_id.pop(trial_set_id)
                    detail_by_id.pop(trial_set_id)
                    rejected.extend(
                        (
                            RejectedTrialSet(
                                entry=first_entry,
                                code="DUPLICATE_TRIAL_SET_ID",
                            ),
                            RejectedTrialSet(
                                entry=candidate.entry,
                                code="DUPLICATE_TRIAL_SET_ID",
                            ),
                        )
                    )
                    continue
                detail_by_id[trial_set_id] = detail
                source_by_id[trial_set_id] = candidate.entry

            summaries = tuple(
                detail.summary
                for detail in sorted(
                    detail_by_id.values(),
                    key=lambda item: (
                        item.summary.latest_run_at,
                        item.summary.trial_set_id,
                    ),
                    reverse=True,
                )
            )
            entries: tuple[TrialSetSummary | RejectedTrialSet, ...] = (
                *summaries,
                *tuple(rejected),
            )
            snapshot_id = _snapshot_id(entries)
            try:
                budget.checkpoint()
            except WorkLimitError:
                raise DashboardError(
                    "trial-set snapshot exceeded its work limits"
                ) from None
            snapshot = _VerifiedTrialSetSnapshot(
                snapshot_id=snapshot_id,
                generated_at=datetime.now(UTC),
                entries=entries,
                details=tuple(detail_by_id.items()),
            )
            with self._lock:
                self._cache_by_digest = {
                    detail.summary.trial_set_digest: detail
                    for detail in detail_by_id.values()
                }
                self._snapshot = snapshot
                self._details = dict(snapshot.details)
            return self._page(snapshot, offset=offset, limit=limit)

    @staticmethod
    def _page(
        snapshot: _VerifiedTrialSetSnapshot,
        *,
        offset: int,
        limit: int,
    ) -> TrialSetIndexResponse:
        if offset > len(snapshot.entries):
            raise DashboardPaginationError(
                "trial-set cursor is outside the current snapshot"
            )
        page_entries = snapshot.entries[offset : offset + limit]
        next_offset = offset + len(page_entries)
        has_more = next_offset < len(snapshot.entries)
        return TrialSetIndexResponse(
            generated_at=snapshot.generated_at,
            trial_sets=tuple(
                item for item in page_entries if isinstance(item, TrialSetSummary)
            ),
            rejected=tuple(
                item for item in page_entries if isinstance(item, RejectedTrialSet)
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

    def get(self, trial_set_id: str) -> TrialSetDetail:
        if not _TRIAL_SET_ID.fullmatch(trial_set_id):
            raise DashboardTrialSetNotFound(
                "trial set is not present in the verified index"
            )
        with self._lock:
            has_snapshot = self._snapshot is not None
        if not has_snapshot:
            self.refresh()
        with self._lock:
            detail = self._details.get(trial_set_id)
            if detail is None:
                raise DashboardTrialSetNotFound(
                    "trial set is not present in the verified index"
                )
            return detail
