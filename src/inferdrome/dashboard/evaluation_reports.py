"""Bounded, read-only catalog of pinned report bytes; no execution or replay."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from time import monotonic
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from inferdrome.dashboard.evaluation_projection import (
    evaluation_report_id,
    project_report,
)
from inferdrome.dashboard.evaluation_report_models import (
    Digest,
    EvaluationReportDetail,
    EvaluationReportIndex,
    RejectedEvaluationReport,
    ReportKind,
)
from inferdrome.dashboard.work import DashboardLimits, DashboardWorkController
from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.domain.base import FrozenModel
from inferdrome.errors import DashboardError, InferdromeError, WorkLimitError
from inferdrome.evaluation.study_files import MAX_METADATA_BYTES, StudyDirectory
from inferdrome.limits import WorkLimits
from inferdrome.parsing import StructuredDataLimits, validate_json_structure
from inferdrome.routing_execution.canonical import sha256_digest

CATALOG_LIMIT = 64 * 1024
INDEX_LIMIT = 64 * 1024
MAX_ENTRIES = 8
SNAPSHOT_WORK = WorkLimits(
    max_units=8, max_bytes=8 * MAX_METADATA_BYTES + CATALOG_LIMIT, max_seconds=30.0
)


class EvaluationReportNotFound(DashboardError):
    """No valid currently configured report has the requested public identity."""


class _CatalogEntry(FrozenModel):
    kind: ReportKind
    report_path: Annotated[str, Field(min_length=1, max_length=4096)]
    expected_sha256: Digest

    @field_validator("report_path")
    @classmethod
    def private_report_path(cls, value: str) -> str:
        path = Path(value)
        if (
            not path.is_absolute()
            or path.name != "report.json"
            or ".." in path.parts
            or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("invalid report location")
        return value


def _directory_identity(path: Path) -> tuple[int, int]:
    info = os.lstat(path)
    return info.st_dev, info.st_ino


def _read_catalog(path: Path) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid catalog location")
    identity = _directory_identity(path.parent)
    with closing(SafeDirFD.open(path.parent)) as root:
        if os.fstat(root.fd).st_mode & 0o077:
            raise ValueError("catalog directory must be private")
        if (root.device, root.inode) != identity:
            raise ValueError("catalog directory changed")
        descriptor = root.open_child(path.name, os.O_RDONLY | os.O_NONBLOCK)
        try:
            before = root.validated_regular_child(path.name, descriptor=descriptor)
            if before.st_mode & 0o077 or not 1 <= before.st_size <= CATALOG_LIMIT:
                raise ValueError("catalog ownership or size")
            data = bytearray()
            while len(data) <= before.st_size:
                chunk = os.read(descriptor, min(65536, before.st_size + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            after = root.validated_regular_child(path.name, descriptor=descriptor)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or len(data) != before.st_size:
                raise ValueError("catalog changed")
            if _directory_identity(path.parent) != identity:
                raise ValueError("catalog directory changed")
            return bytes(data)
        finally:
            os.close(descriptor)


def _catalog_entries(content: bytes) -> list[Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate catalog field")
            value[key] = item
        return value

    def number(_value: str) -> Any:
        raise ValueError("catalog has noninteger number")

    text = content.decode("utf-8")
    validate_json_structure(
        text,
        limits=StructuredDataLimits(
            max_depth=8, max_tokens=1024, max_integer_digits=16
        ),
    )
    value = json.loads(
        text, object_pairs_hook=pairs, parse_float=number, parse_constant=number
    )
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "entries"}
        or value["schema_version"]
        != "inferdrome.dashboard-evaluation-reports-catalog.v1"
        or type(value["entries"]) is not list
        or len(value["entries"]) > MAX_ENTRIES
    ):
        raise ValueError("catalog contract")
    return value["entries"]


def _report_bytes(entry: _CatalogEntry) -> bytes:
    path = Path(entry.report_path)
    identity = _directory_identity(path.parent)
    with StudyDirectory.open(path.parent, budget=MAX_METADATA_BYTES) as directory:
        content = directory.read("report.json", limit=MAX_METADATA_BYTES)
        if _directory_identity(path.parent) != identity:
            raise ValueError("report directory changed")
        return content


class EvaluationReportsIndex:
    """Recheck current pinned bytes on every read; no stale-success cache."""

    def __init__(
        self,
        catalog_path: Path | None = None,
        *,
        clock: Callable[[], float] = monotonic,
        work_limits: WorkLimits = SNAPSHOT_WORK,
    ) -> None:
        if (
            work_limits.max_units > SNAPSHOT_WORK.max_units
            or work_limits.max_bytes > SNAPSHOT_WORK.max_bytes
            or work_limits.max_seconds > SNAPSHOT_WORK.max_seconds
        ):
            raise ValueError("evaluation report work limits exceed protocol")
        self.catalog_path = catalog_path
        self.work_limits = work_limits
        self._controller = DashboardWorkController(
            DashboardLimits(
                max_concurrent_snapshot_builds=1,
                max_aggregate_active_units=8,
                max_aggregate_active_bytes=SNAPSHOT_WORK.max_bytes,
            ),
            clock=clock,
        )

    def _scan(self) -> tuple[EvaluationReportIndex, tuple[EvaluationReportDetail, ...]]:
        # Importing the reader never invokes the source writers or runtime.
        from inferdrome.evaluation.report_reader import load_evaluation_report_bytes

        with self._controller.session(self.work_limits) as budget:
            if self.catalog_path is None:
                return EvaluationReportIndex(reports=(), rejected=()), ()
            budget.reserve(bytes_=CATALOG_LIMIT)
            try:
                entries = _catalog_entries(_read_catalog(self.catalog_path))
            except (OSError, ValueError, InferdromeError, RecursionError):
                raise DashboardError(
                    "evaluation report catalog is unavailable"
                ) from None
            budget.checkpoint()
            parsed: list[_CatalogEntry | None] = []
            for raw in entries:
                try:
                    parsed.append(_CatalogEntry.model_validate(raw))
                except (ValueError, TypeError):
                    parsed.append(None)
            ids = Counter(
                evaluation_report_id(item.kind, item.expected_sha256)
                for item in parsed
                if item
            )
            paths = Counter(item.report_path for item in parsed if item)
            details: list[EvaluationReportDetail] = []
            rejected: list[RejectedEvaluationReport] = []
            total_encoded = 0
            for index, entry in enumerate(parsed, 1):
                budget.reserve(units=1, bytes_=MAX_METADATA_BYTES)
                code: (
                    Literal[
                        "CONFIGURATION_INVALID",
                        "REPORT_UNAVAILABLE",
                        "DIGEST_MISMATCH",
                        "REPORT_INVALID",
                        "PROJECTION_LIMIT",
                    ]
                    | None
                ) = None
                if (
                    entry is None
                    or ids[evaluation_report_id(entry.kind, entry.expected_sha256)] != 1
                    or paths[entry.report_path] != 1
                ):
                    code = "CONFIGURATION_INVALID"
                else:
                    try:
                        content = _report_bytes(entry)
                    except (OSError, ValueError, InferdromeError):
                        code = "REPORT_UNAVAILABLE"
                    else:
                        budget.checkpoint()
                        if sha256_digest(content) != entry.expected_sha256:
                            code = "DIGEST_MISMATCH"
                        else:
                            try:
                                report = load_evaluation_report_bytes(
                                    content, kind=entry.kind
                                )
                                detail = project_report(
                                    report.model_dump(mode="json"),
                                    kind=entry.kind,
                                    digest=entry.expected_sha256,
                                    entry=index,
                                )
                                encoded_size = len(
                                    detail.model_dump_json().encode("utf-8")
                                )
                                if (
                                    encoded_size > MAX_METADATA_BYTES
                                    or total_encoded + encoded_size
                                    > MAX_ENTRIES * MAX_METADATA_BYTES
                                ):
                                    code = "PROJECTION_LIMIT"
                                else:
                                    details.append(detail)
                                    total_encoded += encoded_size
                                del report
                            except (
                                ValueError,
                                TypeError,
                                KeyError,
                                AssertionError,
                                RecursionError,
                                InferdromeError,
                            ):
                                code = "REPORT_INVALID"
                        del content
                if code is not None:
                    rejected.append(RejectedEvaluationReport(entry=index, code=code))
                budget.checkpoint()
            result = EvaluationReportIndex(
                reports=tuple(detail.summary for detail in details),
                rejected=tuple(rejected),
            )
            if len(result.model_dump_json().encode("utf-8")) > INDEX_LIMIT:
                raise DashboardError("evaluation report index limit exceeded")
            return result, tuple(details)

    def refresh(self) -> EvaluationReportIndex:
        try:
            return self._scan()[0]
        except WorkLimitError:
            raise DashboardError(
                "evaluation report work is temporarily unavailable"
            ) from None

    def get_report(self, report_id: str) -> EvaluationReportDetail:
        if re.fullmatch(r"ev-[0-9a-f]{64}", report_id) is None:
            raise EvaluationReportNotFound("evaluation report not found")
        try:
            _, details = self._scan()
        except WorkLimitError:
            raise DashboardError(
                "evaluation report work is temporarily unavailable"
            ) from None
        for detail in details:
            if detail.summary.report_id == report_id:
                return detail
        raise EvaluationReportNotFound("evaluation report not found")
