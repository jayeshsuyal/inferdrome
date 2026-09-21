"""Read-only run index over the retrieved GPU evidence store.

`list_runs` enumerates the sealed retrieval receipts and workload manifests that
already exist under a retrieved-evidence root and returns typed, structured
summaries. It performs no execution, verification, mutation, or provider call,
and unrecognized or in-progress directories are skipped rather than raised on.
The typed output is what an MCP tool will hand an agent verbatim.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from inferdrome.domain.base import FrozenModel

_RETRIEVAL_RECEIPT = "retrieval-receipt.json"
_WORKLOAD_MANIFEST = ("capture", "support", "workload-manifest.json")
_MAX_JSON_BYTES = 1_048_576

RunStatus = Literal["RETRIEVED", "FAILED", "UNVERIFIED"]


class RunSummary(FrozenModel):
    """One retrieved run's identity and provenance, read from sealed evidence.

    Metadata fields are optional because a partially retrieved or failed run may
    lack a receipt or workload manifest; `metadata_complete` says whether both
    were present and read.
    """

    run_id: str
    status: RunStatus
    metadata_complete: bool
    model_id: str | None = None
    model_revision: str | None = None
    managed_capability_profile: str | None = None
    repository_commit: str | None = None
    archive_sha256: str | None = None
    source_archive_sha256: str | None = None
    semantic_verification: str | None = None
    verified_at: str | None = None


def _read_json_object(path: Path) -> Mapping[str, object] | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
            return None
        value = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _status(name: str) -> RunStatus:
    if name.endswith("-FAILED"):
        return "FAILED"
    if name.endswith("-UNVERIFIED"):
        return "UNVERIFIED"
    return "RETRIEVED"


def _is_candidate_run_dir(entry: Path) -> bool:
    name = entry.name
    # Skip hidden `.staging` retrievals and `reextract-*` debug directories:
    # they are transient or derived, not sealed runs to expose.
    return (
        entry.is_dir()
        and not name.startswith(".")
        and not name.startswith("reextract")
    )


def _str_field(mapping: Mapping[str, object] | None, key: str) -> str | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    return value if isinstance(value, str) else None


def _summary(run_dir: Path) -> RunSummary | None:
    receipt = _read_json_object(run_dir / _RETRIEVAL_RECEIPT)
    workload = _read_json_object(run_dir.joinpath(*_WORKLOAD_MANIFEST))
    if receipt is None and workload is None:
        return None
    return RunSummary(
        run_id=run_dir.name,
        status=_status(run_dir.name),
        metadata_complete=receipt is not None and workload is not None,
        model_id=_str_field(workload, "model_id"),
        model_revision=_str_field(workload, "model_revision"),
        managed_capability_profile=_str_field(
            receipt, "managed_capability_profile"
        ),
        repository_commit=_str_field(receipt, "repository_commit"),
        archive_sha256=_str_field(receipt, "archive_sha256"),
        source_archive_sha256=_str_field(receipt, "source_archive_sha256"),
        semantic_verification=_str_field(receipt, "semantic_verification"),
        verified_at=_str_field(receipt, "verified_at"),
    )


def list_runs(
    evidence_root: Path,
    *,
    model_id: str | None = None,
    status: RunStatus | None = None,
) -> tuple[RunSummary, ...]:
    """Return typed summaries of the runs under one retrieved-evidence root.

    Reads only sealed receipts/manifests already on disk; unrecognized or
    in-progress directories are skipped, never raised on. Optional `model_id`
    and `status` narrow the result. Ordered by `verified_at` then `run_id`, both
    descending, so the most recently verified runs come first.
    """

    if not evidence_root.is_dir():
        raise FileNotFoundError(
            f"evidence root is not a directory: {evidence_root}"
        )
    summaries: list[RunSummary] = []
    for entry in sorted(evidence_root.iterdir()):
        if not _is_candidate_run_dir(entry):
            continue
        summary = _summary(entry)
        if summary is None:
            continue
        if model_id is not None and summary.model_id != model_id:
            continue
        if status is not None and summary.status != status:
            continue
        summaries.append(summary)
    summaries.sort(key=lambda s: (s.verified_at or "", s.run_id), reverse=True)
    return tuple(summaries)
