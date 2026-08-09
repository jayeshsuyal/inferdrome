"""Bounded discovery and verified projections for controlled comparisons."""

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
from typing import Literal

from inferdrome.comparisons import (
    ComparisonResultDeclaration,
    VerifiedComparisonPlan,
    VerifiedComparisonResult,
    inspect_comparison_execution,
    inspect_comparison_result_declaration,
    verify_comparison_plan,
    verify_comparison_result,
)
from inferdrome.dashboard.models import (
    ControlledComparisonDetail,
    ControlledComparisonExecutionView,
    ControlledComparisonIndexResponse,
    ControlledComparisonPlanView,
    ControlledComparisonRunProgress,
    ControlledComparisonSummary,
    PageView,
    RejectedControlledComparison,
)
from inferdrome.dashboard.projection import display_measurement, metric_label
from inferdrome.dashboard.trial_sets import project_trial_set
from inferdrome.errors import (
    DashboardControlledComparisonNotFound,
    DashboardError,
    DashboardPaginationError,
    InferdromeError,
)
from inferdrome.immutable import is_internal_staging_entry

_PLAN_ID = re.compile(r"^comparison-plan-[0-9a-f]{32}$")
_RESULT_ID = re.compile(r"^comparison-result-[0-9a-f]{32}$")
_SAFE_ENTRY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_DISCOVERED_ENTRIES = 200
_DEFAULT_PAGE_LIMIT = 100
_MAX_PAGE_LIMIT = 200
_MAX_CURSOR_LENGTH = 128

_RejectionCode = Literal[
    "UNSAFE_ENTRY",
    "DECLARATION_UNAVAILABLE",
    "PLAN_VERIFICATION_FAILED",
    "RESULT_VERIFICATION_FAILED",
    "DUPLICATE_RESULT_FOR_PLAN",
    "ORPHAN_RESULT",
]
_ResultStatus = Literal[
    "COMPARABLE",
    "INCOMPARABLE",
    "NO_RESULT",
    "WITHHELD",
]
_ResultIssue = Literal[
    "RESULT_VERIFICATION_FAILED",
    "DUPLICATE_RESULT_FOR_PLAN",
]


@dataclass(frozen=True)
class _Candidate:
    entry: str
    path: Path


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


def _failure_fingerprint(path: Path, code: str) -> str:
    digest = hashlib.sha256(b"inferdrome.dashboard.rejected-comparison.v1\0")
    digest.update(code.encode("ascii"))
    digest.update(b"\0")
    try:
        metadata = os.lstat(path)
    except OSError:
        digest.update(b"unavailable")
    else:
        digest.update(
            ":".join(
                str(value)
                for value in (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mode,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
            ).encode("ascii")
        )
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            for filename in ("comparison-plan.json", "comparison-result.json"):
                try:
                    child = os.lstat(path / filename)
                except OSError:
                    continue
                digest.update(
                    f":{filename}:{child.st_dev}:{child.st_ino}:{child.st_size}:"
                    f"{child.st_mode}:{child.st_mtime_ns}:{child.st_ctime_ns}".encode(
                        "ascii"
                    )
                )
    return digest.hexdigest()


def _rejected(
    candidate: _Candidate,
    code: _RejectionCode,
) -> RejectedControlledComparison:
    return RejectedControlledComparison(
        entry=candidate.entry,
        code=code,
        failure_fingerprint=_failure_fingerprint(candidate.path, code),
    )


def _encode_cursor(offset: int, snapshot_id: str) -> str:
    encoded = base64.urlsafe_b64encode(f"v1:{offset}:{snapshot_id}".encode("ascii"))
    return encoded.decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[int, str | None]:
    if cursor is None:
        return 0, None
    if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
        raise DashboardPaginationError("comparison cursor is invalid")
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
        raise DashboardPaginationError("comparison cursor is invalid") from None
    if (
        version != "v1"
        or str(offset) != raw_offset
        or not 0 <= offset <= _MAX_DISCOVERED_ENTRIES * 2
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot_id)
    ):
        raise DashboardPaginationError("comparison cursor is invalid")
    return offset, snapshot_id


def _snapshot_id(
    entries: tuple[ControlledComparisonSummary | RejectedControlledComparison, ...],
) -> str:
    digest = hashlib.sha256(b"inferdrome.dashboard.comparison-snapshot.v1\0")
    for entry in entries:
        if isinstance(entry, ControlledComparisonSummary):
            identity = (
                f"plan:{entry.comparison_plan_id}:{entry.comparison_plan_digest}:"
                f"{entry.comparison_result_digest or 'none'}:{entry.result_status}"
            )
        else:
            identity = (
                f"rejected:{entry.entry}:{entry.code}:{entry.failure_fingerprint}"
            )
        digest.update(identity.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _discover(
    root: Path,
    *,
    filename: str,
    identifier: re.Pattern[str],
    root_label: str,
) -> tuple[tuple[_Candidate, ...], tuple[RejectedControlledComparison, ...]]:
    if not root.exists():
        return (), ()
    if not _real_directory(root):
        raise DashboardError(f"{root_label} must be a real directory")
    if _regular_file(root / filename):
        return ((_Candidate(entry=_entry_label(root.name), path=root),), ())
    try:
        with os.scandir(root) as iterator:
            entries = sorted(
                (
                    entry
                    for entry in iterator
                    if not is_internal_staging_entry(entry.name)
                ),
                key=lambda item: item.name,
            )
    except OSError:
        raise DashboardError(f"{root_label} could not be scanned") from None
    if len(entries) > _MAX_DISCOVERED_ENTRIES:
        raise DashboardError(f"{root_label} exceeds the entry limit")

    candidates: list[_Candidate] = []
    rejected: list[RejectedControlledComparison] = []
    for entry in entries:
        candidate = _Candidate(
            entry=_entry_label(entry.name),
            path=Path(entry.path),
        )
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            rejected.append(_rejected(candidate, "UNSAFE_ENTRY"))
            continue
        if stat.S_ISLNK(metadata.st_mode):
            rejected.append(_rejected(candidate, "UNSAFE_ENTRY"))
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        if _regular_file(candidate.path / filename):
            candidates.append(candidate)
        elif identifier.fullmatch(entry.name):
            rejected.append(_rejected(candidate, "DECLARATION_UNAVAILABLE"))
    return tuple(candidates), tuple(rejected)


def _summary(
    plan: VerifiedComparisonPlan,
    result: VerifiedComparisonResult | None,
    *,
    result_withheld: bool = False,
) -> ControlledComparisonSummary:
    descriptor = plan.descriptor
    outcome = descriptor.primary_outcome
    estimate = None
    estimate_display = None
    result_id = None
    result_digest = None
    result_status: _ResultStatus
    if result is not None:
        result_id = result.descriptor.comparison_result_id
        result_digest = result.comparison_result_digest
        result_status = result.descriptor.status.value
        if result.descriptor.outcomes:
            estimate = result.descriptor.outcomes[0].estimate
            estimate_display = display_measurement(estimate, outcome.unit)
    elif result_withheld:
        result_status = "WITHHELD"
    else:
        result_status = "NO_RESULT"
    return ControlledComparisonSummary(
        comparison_plan_id=descriptor.comparison_plan_id,
        comparison_plan_digest=plan.comparison_plan_digest,
        experiment_id=descriptor.experiment_id,
        title=descriptor.title,
        created_at=descriptor.created_at,
        treatment_path=descriptor.independent_variable.path,
        baseline_value=descriptor.independent_variable.baseline_value,
        candidate_value=descriptor.independent_variable.candidate_value,
        planned_repetitions_per_arm=descriptor.planned_repetitions_per_arm,
        baseline_trial_set_id=descriptor.baseline_arm.planned_trial_set_id,
        candidate_trial_set_id=descriptor.candidate_arm.planned_trial_set_id,
        design_status=descriptor.design_status,
        predeclaration_assurance=descriptor.predeclaration_assurance,
        primary_outcome_key=f"{outcome.metric.value}:{outcome.aggregation.value}",
        primary_outcome_label=metric_label(
            outcome.metric.value,
            outcome.aggregation.value,
        ),
        primary_outcome_unit=outcome.unit.value,
        result_status=result_status,
        comparison_result_id=result_id,
        comparison_result_digest=result_digest,
        estimate=estimate,
        estimate_display_value=estimate_display,
    )


def _execution_view(
    plan: VerifiedComparisonPlan,
    result: VerifiedComparisonResult | None,
    runs_root: Path,
) -> ControlledComparisonExecutionView:
    schedule = plan.descriptor.ordered_schedule
    try:
        progress = inspect_comparison_execution(plan, runs_root)
    except (InferdromeError, OSError, ValueError):
        return ControlledComparisonExecutionView(
            status="BLOCKED",
            result_published=result is not None,
            completed_run_count=0,
            planned_run_count=len(schedule),
            next_sequence_index=None,
            exact_schedule_prefix=False,
            issue="PROGRESS_INSPECTION_FAILED",
            slots=tuple(
                ControlledComparisonRunProgress(
                    sequence_index=slot.sequence_index,
                    run_id=slot.run_id,
                    state="INVALID",
                    verified_bundle=False,
                )
                for slot in schedule
            ),
        )
    return ControlledComparisonExecutionView(
        status=progress.status,
        result_published=result is not None,
        completed_run_count=progress.completed_run_count,
        planned_run_count=progress.planned_run_count,
        next_sequence_index=progress.next_sequence_index,
        exact_schedule_prefix=progress.exact_schedule_prefix,
        issue=None,
        slots=tuple(
            ControlledComparisonRunProgress(
                sequence_index=slot.sequence_index,
                run_id=slot.run_id,
                state=slot.state,
                verified_bundle=slot.verified_bundle,
            )
            for slot in progress.slots
        ),
    )


def _detail(
    plan: VerifiedComparisonPlan,
    result: VerifiedComparisonResult | None,
    runs_root: Path,
    *,
    result_issue: _ResultIssue | None = None,
) -> ControlledComparisonDetail:
    expose_trial_set_measurements = (
        result is not None and result.descriptor.status.value == "COMPARABLE"
    )
    return ControlledComparisonDetail(
        summary=_summary(
            plan,
            result,
            result_withheld=result_issue is not None,
        ),
        plan=ControlledComparisonPlanView.model_validate(
            plan.descriptor.model_dump(
                mode="python",
                exclude={
                    "baseline_arm": {"resolved_experiment"},
                    "candidate_arm": {"resolved_experiment"},
                },
            )
        ),
        execution=_execution_view(plan, result, runs_root),
        result=None if result is None else result.descriptor,
        baseline_trial_set=(
            project_trial_set(result.baseline).summary
            if expose_trial_set_measurements and result is not None
            else None
        ),
        candidate_trial_set=(
            project_trial_set(result.candidate).summary
            if expose_trial_set_measurements and result is not None
            else None
        ),
        result_issue=result_issue,
    )


class ControlledComparisonDashboardIndex:
    """Read-only index over plans and independently recalculated results."""

    def __init__(
        self,
        comparison_plans_root: Path,
        comparison_results_root: Path,
        trial_sets_root: Path,
        runs_root: Path,
    ) -> None:
        self.comparison_plans_root = comparison_plans_root.absolute()
        self.comparison_results_root = comparison_results_root.absolute()
        self.trial_sets_root = trial_sets_root.absolute()
        self.runs_root = runs_root.absolute()
        self._lock = RLock()

    def _plan_candidates(
        self,
    ) -> tuple[tuple[_Candidate, ...], tuple[RejectedControlledComparison, ...]]:
        return _discover(
            self.comparison_plans_root,
            filename="comparison-plan.json",
            identifier=_PLAN_ID,
            root_label="comparison-plans root",
        )

    def _result_candidates(
        self,
    ) -> tuple[tuple[_Candidate, ...], tuple[RejectedControlledComparison, ...]]:
        return _discover(
            self.comparison_results_root,
            filename="comparison-result.json",
            identifier=_RESULT_ID,
            root_label="comparison-results root",
        )

    def _verified_result(
        self,
        declaration: ComparisonResultDeclaration,
    ) -> VerifiedComparisonResult:
        return verify_comparison_result(
            declaration.path,
            runs_root=self.runs_root,
            trial_sets_root=self.trial_sets_root,
            comparison_plans_root=self.comparison_plans_root,
            expected_comparison_result_digest=(declaration.comparison_result_digest),
        )

    def refresh(
        self,
        *,
        cursor: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> ControlledComparisonIndexResponse:
        if isinstance(limit, bool) or not 1 <= limit <= _MAX_PAGE_LIMIT:
            raise DashboardPaginationError("comparison page limit is invalid")
        offset, expected_snapshot_id = _decode_cursor(cursor)
        with self._lock:
            plan_candidates, plan_rejections = self._plan_candidates()
            result_candidates, result_rejections = self._result_candidates()
            rejected = [*plan_rejections, *result_rejections]

            plans: dict[str, VerifiedComparisonPlan] = {}
            for candidate in plan_candidates:
                try:
                    verified_plan = verify_comparison_plan(candidate.path)
                except (InferdromeError, OSError, ValueError):
                    rejected.append(_rejected(candidate, "PLAN_VERIFICATION_FAILED"))
                    continue
                plans[verified_plan.descriptor.comparison_plan_id] = verified_plan

            declarations_by_plan: dict[
                str, list[tuple[_Candidate, ComparisonResultDeclaration]]
            ] = {}
            for candidate in result_candidates:
                try:
                    declaration = inspect_comparison_result_declaration(candidate.path)
                except (InferdromeError, OSError, ValueError):
                    rejected.append(_rejected(candidate, "RESULT_VERIFICATION_FAILED"))
                    continue
                linked_plan = plans.get(declaration.descriptor.comparison_plan_id)
                if linked_plan is None:
                    rejected.append(_rejected(candidate, "ORPHAN_RESULT"))
                    continue
                declarations_by_plan.setdefault(
                    declaration.descriptor.comparison_plan_id,
                    [],
                ).append((candidate, declaration))

            summaries_by_plan: dict[str, ControlledComparisonSummary] = {}
            for plan_id, plan in plans.items():
                declarations = declarations_by_plan.get(plan_id, [])
                if len(declarations) > 1:
                    for candidate, _ in declarations:
                        rejected.append(
                            _rejected(candidate, "DUPLICATE_RESULT_FOR_PLAN")
                        )
                    summaries_by_plan[plan_id] = _summary(
                        plan,
                        None,
                        result_withheld=True,
                    )
                    continue
                if not declarations:
                    summaries_by_plan[plan_id] = _summary(plan, None)
                    continue
                candidate, declaration = declarations[0]
                if (
                    declaration.descriptor.comparison_plan_digest
                    != plan.comparison_plan_digest
                ):
                    rejected.append(_rejected(candidate, "RESULT_VERIFICATION_FAILED"))
                    summaries_by_plan[plan_id] = _summary(
                        plan,
                        None,
                        result_withheld=True,
                    )
                    continue
                try:
                    result = self._verified_result(declaration)
                except (InferdromeError, OSError, ValueError):
                    rejected.append(_rejected(candidate, "RESULT_VERIFICATION_FAILED"))
                    summaries_by_plan[plan_id] = _summary(
                        plan,
                        None,
                        result_withheld=True,
                    )
                    continue
                summaries_by_plan[plan_id] = _summary(plan, result)

            summaries = tuple(
                summary
                for summary in sorted(
                    summaries_by_plan.values(),
                    key=lambda item: (
                        item.created_at,
                        item.comparison_plan_id,
                    ),
                    reverse=True,
                )
            )
            entries: tuple[
                ControlledComparisonSummary | RejectedControlledComparison,
                ...,
            ] = (*summaries, *tuple(rejected))
            snapshot_id = _snapshot_id(entries)
            if expected_snapshot_id is not None and expected_snapshot_id != snapshot_id:
                raise DashboardPaginationError(
                    "comparison cursor refers to a stale snapshot"
                )
            if offset > len(entries):
                raise DashboardPaginationError(
                    "comparison cursor is outside the current snapshot"
                )
            page_entries = entries[offset : offset + limit]
            next_offset = offset + len(page_entries)
            has_more = next_offset < len(entries)
            return ControlledComparisonIndexResponse(
                generated_at=datetime.now(UTC),
                comparisons=tuple(
                    item
                    for item in page_entries
                    if isinstance(item, ControlledComparisonSummary)
                ),
                rejected=tuple(
                    item
                    for item in page_entries
                    if isinstance(item, RejectedControlledComparison)
                ),
                page=PageView(
                    limit=limit,
                    returned=len(page_entries),
                    total=len(entries),
                    has_more=has_more,
                    next_cursor=(
                        _encode_cursor(next_offset, snapshot_id) if has_more else None
                    ),
                ),
            )

    def get(self, comparison_plan_id: str) -> ControlledComparisonDetail:
        if not _PLAN_ID.fullmatch(comparison_plan_id):
            raise DashboardControlledComparisonNotFound(
                "comparison is not present in the verified index"
            )
        plan_path = self.comparison_plans_root / comparison_plan_id
        try:
            plan = verify_comparison_plan(plan_path)
        except (InferdromeError, OSError, ValueError):
            raise DashboardControlledComparisonNotFound(
                "comparison is not present in the verified index"
            ) from None

        result_candidates, _ = self._result_candidates()
        matching: list[tuple[_Candidate, ComparisonResultDeclaration]] = []
        for candidate in result_candidates:
            try:
                declaration = inspect_comparison_result_declaration(candidate.path)
            except (InferdromeError, OSError, ValueError):
                continue
            if declaration.descriptor.comparison_plan_id != comparison_plan_id:
                continue
            matching.append((candidate, declaration))
        if len(matching) > 1:
            return _detail(
                plan,
                None,
                self.runs_root,
                result_issue="DUPLICATE_RESULT_FOR_PLAN",
            )
        if not matching:
            return _detail(plan, None, self.runs_root)
        if (
            matching[0][1].descriptor.comparison_plan_digest
            != plan.comparison_plan_digest
        ):
            return _detail(
                plan,
                None,
                self.runs_root,
                result_issue="RESULT_VERIFICATION_FAILED",
            )
        try:
            result = self._verified_result(matching[0][1])
        except (InferdromeError, OSError, ValueError):
            return _detail(
                plan,
                None,
                self.runs_root,
                result_issue="RESULT_VERIFICATION_FAILED",
            )
        return _detail(plan, result, self.runs_root)
