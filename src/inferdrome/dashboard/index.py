"""Bounded discovery and digest-keyed caching for dashboard run bundles."""

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

from inferdrome.bundle import verify_bundle
from inferdrome.dashboard.comparison import compare_runs
from inferdrome.dashboard.models import (
    ComparisonResponse,
    PageView,
    RejectedRun,
    RunDetail,
    RunIndexResponse,
    RunSummary,
    TrialSetDetail,
    TrialSetIndexResponse,
)
from inferdrome.dashboard.projection import load_run_detail
from inferdrome.dashboard.trial_sets import TrialSetDashboardIndex
from inferdrome.errors import (
    DashboardError,
    DashboardPaginationError,
    DashboardRunNotFound,
    InferdromeError,
)

_RUN_ID = re.compile(r"^run-[0-9a-f]{32}$")
_SAFE_ENTRY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_DISCOVERED_ENTRIES = 1_000
_DEFAULT_PAGE_LIMIT = 100
_MAX_PAGE_LIMIT = 200
_MAX_CURSOR_LENGTH = 128


@dataclass(frozen=True)
class _Candidate:
    entry: str
    bundle_path: Path


def _entry_label(name: str) -> str:
    return name if _SAFE_ENTRY.fullmatch(name) else "<unsafe-entry>"


def _directory_without_follow(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _file_without_follow(path: Path) -> bool:
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
        raise DashboardPaginationError("dashboard cursor is invalid")
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
        raise DashboardPaginationError("dashboard cursor is invalid") from None
    if (
        version != "v1"
        or str(offset) != raw_offset
        or not 0 <= offset <= _MAX_DISCOVERED_ENTRIES
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot_id)
    ):
        raise DashboardPaginationError("dashboard cursor is invalid")
    return offset, snapshot_id


def _snapshot_id(entries: tuple[RunSummary | RejectedRun, ...]) -> str:
    digest = hashlib.sha256(b"inferdrome.dashboard.run-index-snapshot.v1\0")
    for entry in entries:
        if isinstance(entry, RunSummary):
            identity = f"run:{entry.run_id}:{entry.bundle_digest}"
        else:
            identity = f"rejected:{entry.entry}:{entry.code}"
        digest.update(identity.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


class DashboardIndex:
    """Read-only index that resolves URLs exclusively through verified run IDs."""

    def __init__(
        self,
        runs_root: Path,
        *,
        trial_sets_root: Path | None = None,
    ) -> None:
        self.runs_root = runs_root.absolute()
        selected_trial_sets_root = (
            trial_sets_root.absolute()
            if trial_sets_root is not None
            else self.runs_root.parent / "trial-sets"
        )
        self._trial_sets = TrialSetDashboardIndex(
            selected_trial_sets_root,
            self.runs_root,
        )
        self._cache_by_digest: dict[str, RunDetail] = {}
        self._runs: dict[str, RunDetail] = {}
        self._lock = RLock()

    def _candidates(self) -> tuple[tuple[_Candidate, ...], tuple[RejectedRun, ...]]:
        if not self.runs_root.exists():
            return (), ()
        if not _directory_without_follow(self.runs_root):
            raise DashboardError("runs root must be a real directory")

        if _file_without_follow(self.runs_root / "bundle.json"):
            return (
                (
                    _Candidate(
                        entry=_entry_label(self.runs_root.name),
                        bundle_path=self.runs_root,
                    ),
                ),
                (),
            )

        try:
            with os.scandir(self.runs_root) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError:
            raise DashboardError("runs root could not be scanned") from None
        if len(entries) > _MAX_DISCOVERED_ENTRIES:
            raise DashboardError("runs root exceeds the dashboard entry limit")

        candidates: list[_Candidate] = []
        rejected: list[RejectedRun] = []
        for entry in entries:
            label = _entry_label(entry.name)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                rejected.append(RejectedRun(entry=label, code="UNSAFE_ENTRY"))
                continue
            if stat.S_ISLNK(metadata.st_mode):
                rejected.append(RejectedRun(entry=label, code="UNSAFE_ENTRY"))
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                continue

            child = Path(entry.path)
            direct_descriptor = child / "bundle.json"
            workspace_bundle = child / "bundle"
            if _file_without_follow(direct_descriptor):
                candidates.append(_Candidate(entry=label, bundle_path=child))
            elif _directory_without_follow(workspace_bundle):
                candidates.append(_Candidate(entry=label, bundle_path=workspace_bundle))
            elif _RUN_ID.fullmatch(entry.name):
                rejected.append(
                    RejectedRun(entry=label, code="BUNDLE_UNAVAILABLE")
                )
        return tuple(candidates), tuple(rejected)

    def refresh(
        self,
        *,
        cursor: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> RunIndexResponse:
        """Rescan and reverify the bounded root before publishing a snapshot."""

        if isinstance(limit, bool) or not 1 <= limit <= _MAX_PAGE_LIMIT:
            raise DashboardPaginationError("dashboard page limit is invalid")
        offset, expected_snapshot_id = _decode_cursor(cursor)
        with self._lock:
            candidates, discovery_rejections = self._candidates()
            rejected = list(discovery_rejections)
            run_by_id: dict[str, RunDetail] = {}
            source_by_id: dict[str, str] = {}
            duplicate_ids: set[str] = set()

            for candidate in candidates:
                try:
                    report = verify_bundle(candidate.bundle_path)
                    detail = self._cache_by_digest.get(report.bundle_digest)
                    if detail is None:
                        detail = load_run_detail(candidate.bundle_path)
                        self._cache_by_digest[report.bundle_digest] = detail
                    if (
                        detail.summary.bundle_digest != report.bundle_digest
                        or detail.summary.run_id != report.run_id
                    ):
                        raise DashboardError("verified dashboard cache disagrees")
                except (InferdromeError, OSError, ValueError):
                    rejected.append(
                        RejectedRun(
                            entry=candidate.entry,
                            code="VERIFICATION_FAILED",
                        )
                    )
                    continue

                run_id = detail.summary.run_id
                if run_id in duplicate_ids:
                    rejected.append(
                        RejectedRun(
                            entry=candidate.entry,
                            code="DUPLICATE_RUN_ID",
                        )
                    )
                    continue
                if run_id in run_by_id:
                    duplicate_ids.add(run_id)
                    first_entry = source_by_id.pop(run_id)
                    run_by_id.pop(run_id)
                    rejected.extend(
                        (
                            RejectedRun(
                                entry=first_entry,
                                code="DUPLICATE_RUN_ID",
                            ),
                            RejectedRun(
                                entry=candidate.entry,
                                code="DUPLICATE_RUN_ID",
                            ),
                        )
                    )
                    continue
                run_by_id[run_id] = detail
                source_by_id[run_id] = candidate.entry

            self._runs = run_by_id
            self._cache_by_digest = {
                detail.summary.bundle_digest: detail
                for detail in run_by_id.values()
            }
            summaries = tuple(
                detail.summary
                for detail in sorted(
                    run_by_id.values(),
                    key=lambda item: (item.summary.started_at, item.summary.run_id),
                    reverse=True,
                )
            )
            entries: tuple[RunSummary | RejectedRun, ...] = (
                *summaries,
                *tuple(rejected),
            )
            snapshot_id = _snapshot_id(entries)
            if (
                expected_snapshot_id is not None
                and expected_snapshot_id != snapshot_id
            ):
                raise DashboardPaginationError(
                    "dashboard cursor refers to a stale snapshot"
                )
            if offset > len(entries):
                raise DashboardPaginationError(
                    "dashboard cursor is outside the current snapshot"
                )
            page_entries = entries[offset : offset + limit]
            next_offset = offset + len(page_entries)
            has_more = next_offset < len(entries)
            return RunIndexResponse(
                generated_at=datetime.now(UTC),
                runs=tuple(
                    item for item in page_entries if isinstance(item, RunSummary)
                ),
                rejected=tuple(
                    item for item in page_entries if isinstance(item, RejectedRun)
                ),
                page=PageView(
                    limit=limit,
                    returned=len(page_entries),
                    total=len(entries),
                    has_more=has_more,
                    next_cursor=(
                        _encode_cursor(next_offset, snapshot_id)
                        if has_more
                        else None
                    ),
                ),
            )

    def get_run(self, run_id: str) -> RunDetail:
        if not _RUN_ID.fullmatch(run_id):
            raise DashboardRunNotFound("run is not present in the verified index")
        self.refresh()
        with self._lock:
            detail = self._runs.get(run_id)
            if detail is None:
                raise DashboardRunNotFound(
                    "run is not present in the verified index"
                )
            return detail

    def compare(
        self,
        baseline_run_id: str,
        candidate_run_id: str,
    ) -> ComparisonResponse:
        if not _RUN_ID.fullmatch(baseline_run_id) or not _RUN_ID.fullmatch(
            candidate_run_id
        ):
            raise DashboardRunNotFound("run is not present in the verified index")
        self.refresh()
        with self._lock:
            baseline = self._runs.get(baseline_run_id)
            candidate = self._runs.get(candidate_run_id)
            if baseline is None or candidate is None:
                raise DashboardRunNotFound(
                    "run is not present in the verified index"
                )
            return compare_runs(baseline, candidate)

    def list_trial_sets(
        self,
        *,
        cursor: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> TrialSetIndexResponse:
        return self._trial_sets.refresh(cursor=cursor, limit=limit)

    def get_trial_set(self, trial_set_id: str) -> TrialSetDetail:
        return self._trial_sets.get(trial_set_id)
