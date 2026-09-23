"""Read-only run index over the retrieved GPU evidence store.

`list_runs` enumerates the sealed retrieval receipts and workload manifests that
already exist under a retrieved-evidence root and returns typed, structured
summaries. It performs no execution, verification, mutation, or provider call,
and unrecognized or in-progress directories are skipped rather than raised on.
The typed output is what an MCP tool will hand an agent verbatim.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from inferdrome.domain.base import FrozenModel

_RETRIEVAL_RECEIPT = "retrieval-receipt.json"
_WORKLOAD_MANIFEST = ("capture", "support", "workload-manifest.json")
_ARCHIVE = "capture.tar.gz"
_ARCHIVE_METADATA = "capture.tar.gz.metadata.json"
_ARCHIVE_SHA256_SIDECAR = "capture.tar.gz.sha256"
_MAX_JSON_BYTES = 1_048_576
_HASH_CHUNK_BYTES = 1_048_576

RunStatus = Literal["RETRIEVED", "FAILED", "UNVERIFIED"]
VerificationStatus = Literal[
    "VERIFIED",
    "DIGEST_MISMATCH",
    "RECORDED_DIGESTS_DISAGREE",
    "NO_RECORDED_DIGEST",
    "ARCHIVE_ABSENT",
]


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


class RunDetail(RunSummary):
    """A run summary plus the extra detail an agent needs to interpret it."""

    archive_size_bytes: int | None = None
    workload_line_count: int | None = None
    gpu_model: str | None = None
    gpu_count: int | None = None
    driver_version: str | None = None


class EvidenceVerification(FrozenModel):
    """The recomputed integrity check for one run's sealed archive.

    `status` is recomputed on every call from the archive bytes on disk; it is
    never a cached verdict. `recomputed_sha256` is what the retrieved archive
    hashes to now; `recorded_sha256` is the digest the receipt/metadata sealed.
    """

    run_id: str
    status: VerificationStatus
    recomputed_sha256: str | None = None
    recorded_sha256: str | None = None
    archive_size_bytes: int | None = None


def _run_dir(evidence_root: Path, run_id: str) -> Path:
    if "/" in run_id or "\\" in run_id or run_id in {"", ".", ".."}:
        raise KeyError(run_id)
    run_dir = evidence_root / run_id
    if not _is_candidate_run_dir(run_dir):
        raise KeyError(run_id)
    return run_dir


def _environment_fields(run_dir: Path) -> Sequence[Mapping[str, object]]:
    for candidate in sorted(run_dir.glob("capture/**/bundle/environment.json")):
        document = _read_json_object(candidate)
        fields = document.get("fields") if document is not None else None
        if isinstance(fields, list):
            return [item for item in fields if isinstance(item, dict)]
    return ()


def _field_value(fields: Sequence[Mapping[str, object]], name: str) -> object:
    for item in fields:
        if item.get("name") == name:
            return item.get("value")
    return None


def get_run(evidence_root: Path, run_id: str) -> RunDetail:
    """Return the full typed detail for one run, read from sealed evidence.

    Raises KeyError if `run_id` is not a recognizable run directory under the
    evidence root. Performs no verification (see `verify_evidence`), execution,
    or provider call.
    """

    run_dir = _run_dir(evidence_root, run_id)
    summary = _summary(run_dir)
    if summary is None:
        raise KeyError(run_id)
    metadata = _read_json_object(run_dir / _ARCHIVE_METADATA)
    workload = _read_json_object(run_dir.joinpath(*_WORKLOAD_MANIFEST))
    fields = _environment_fields(run_dir)
    gpu_count = _field_value(fields, "gpu.count")
    line_count = workload.get("line_count") if workload is not None else None
    size_bytes = metadata.get("size_bytes") if metadata is not None else None
    return RunDetail(
        **summary.model_dump(),
        archive_size_bytes=size_bytes if isinstance(size_bytes, int) else None,
        workload_line_count=line_count if isinstance(line_count, int) else None,
        gpu_model=_as_str(_field_value(fields, "gpu.model")),
        gpu_count=gpu_count if isinstance(gpu_count, int) else None,
        driver_version=_as_str(_field_value(fields, "driver.version")),
    )


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _normalize_digest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("sha256:"):
        text = text[len("sha256:") :]
    # A `.sha256` sidecar is "<hex>  <filename>"; keep only the hex.
    text = text.split()[0] if text else text
    lowered = text.lower()
    if len(lowered) == 64 and all(c in "0123456789abcdef" for c in lowered):
        return lowered
    return None


def _recorded_archive_digests(run_dir: Path) -> set[str]:
    recorded: set[str] = set()
    metadata = _read_json_object(run_dir / _ARCHIVE_METADATA)
    if metadata is not None:
        digest = _normalize_digest(metadata.get("archive_sha256"))
        if digest is not None:
            recorded.add(digest)
    receipt = _read_json_object(run_dir / _RETRIEVAL_RECEIPT)
    if receipt is not None:
        digest = _normalize_digest(receipt.get("archive_sha256"))
        if digest is not None:
            recorded.add(digest)
    sidecar = run_dir / _ARCHIVE_SHA256_SIDECAR
    try:
        if sidecar.is_file() and sidecar.stat().st_size <= _MAX_JSON_BYTES:
            digest = _normalize_digest(sidecar.read_text())
            if digest is not None:
                recorded.add(digest)
    except OSError:
        pass
    return recorded


class ConfigDifference(FrozenModel):
    """One configuration dimension that differs between two runs."""

    dimension: str
    baseline: str | None
    candidate: str | None


class MetricDelta(FrozenModel):
    """A paired metric present in both runs, candidate minus baseline."""

    metric: str
    aggregation: str
    unit: str
    baseline_value: float
    candidate_value: float
    absolute_delta: float
    percent_change: float | None


class RunComparison(FrozenModel):
    """A structured, guard-checked comparison of two runs.

    `comparability` is `INCOMPARABLE` when any controlled dimension differs
    (model, revision, profile, GPU, workload size); the differing dimensions are
    listed in `differences`. Metric deltas are always returned, but a consumer
    must heed `comparability` before attributing them: an INCOMPARABLE delta may
    reflect the config difference, not the engine.
    """

    baseline_run_id: str
    candidate_run_id: str
    comparability: Literal["COMPARABLE", "INCOMPARABLE"]
    differences: tuple[ConfigDifference, ...]
    metric_deltas: tuple[MetricDelta, ...]


_COMPARABILITY_DIMENSIONS = (
    "model_id",
    "model_revision",
    "managed_capability_profile",
    "gpu_model",
    "workload_line_count",
)


def _numeric(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _measurements(run_dir: Path) -> dict[tuple[str, str], tuple[float, str]]:
    for candidate in sorted(
        run_dir.glob("capture/**/bundle/derived/measurements.json")
    ):
        document = _read_json_object(candidate)
        rows = document.get("measurements") if document is not None else None
        if not isinstance(rows, list):
            continue
        result: dict[tuple[str, str], tuple[float, str]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            metric = row.get("metric")
            aggregation = row.get("aggregation")
            unit = row.get("unit")
            number = _numeric(row.get("value"))
            if (
                isinstance(metric, str)
                and isinstance(aggregation, str)
                and isinstance(unit, str)
                and number is not None
            ):
                result[(metric, aggregation)] = (number, unit)
        return result
    return {}


def compare_runs(
    evidence_root: Path, baseline_run_id: str, candidate_run_id: str
) -> RunComparison:
    """Compare two runs' config and reduced metrics with a comparability guard.

    Reads sealed evidence only. Raises KeyError if either run is unknown. The
    comparison never emits a winner or verdict; it reports the differences and
    the paired metric deltas and leaves interpretation to the caller.
    """

    baseline = get_run(evidence_root, baseline_run_id)
    candidate = get_run(evidence_root, candidate_run_id)
    differences: list[ConfigDifference] = []
    for dimension in _COMPARABILITY_DIMENSIONS:
        left = getattr(baseline, dimension)
        right = getattr(candidate, dimension)
        if left != right:
            differences.append(
                ConfigDifference(
                    dimension=dimension,
                    baseline=None if left is None else str(left),
                    candidate=None if right is None else str(right),
                )
            )
    baseline_metrics = _measurements(evidence_root / baseline_run_id)
    candidate_metrics = _measurements(evidence_root / candidate_run_id)
    deltas: list[MetricDelta] = []
    for key in sorted(baseline_metrics.keys() & candidate_metrics.keys()):
        base_value, unit = baseline_metrics[key]
        cand_value, _ = candidate_metrics[key]
        deltas.append(
            MetricDelta(
                metric=key[0],
                aggregation=key[1],
                unit=unit,
                baseline_value=base_value,
                candidate_value=cand_value,
                absolute_delta=cand_value - base_value,
                percent_change=(
                    (cand_value - base_value) / base_value * 100.0
                    if base_value != 0.0
                    else None
                ),
            )
        )
    return RunComparison(
        baseline_run_id=baseline_run_id,
        candidate_run_id=candidate_run_id,
        comparability="COMPARABLE" if not differences else "INCOMPARABLE",
        differences=tuple(differences),
        metric_deltas=tuple(deltas),
    )


def verify_evidence(evidence_root: Path, run_id: str) -> EvidenceVerification:
    """Recompute one run's archive digest and compare it to the sealed record.

    The result is derived fresh from the archive bytes on every call, never from
    a stored verdict. Raises KeyError for an unknown run.
    """

    run_dir = _run_dir(evidence_root, run_id)
    archive = run_dir / _ARCHIVE
    recorded = _recorded_archive_digests(run_dir)
    if not archive.is_file():
        return EvidenceVerification(run_id=run_id, status="ARCHIVE_ABSENT")
    size_bytes = archive.stat().st_size
    if len(recorded) > 1:
        return EvidenceVerification(
            run_id=run_id,
            status="RECORDED_DIGESTS_DISAGREE",
            archive_size_bytes=size_bytes,
        )
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    recomputed = digest.hexdigest()
    if not recorded:
        return EvidenceVerification(
            run_id=run_id,
            status="NO_RECORDED_DIGEST",
            recomputed_sha256=recomputed,
            archive_size_bytes=size_bytes,
        )
    recorded_digest = next(iter(recorded))
    return EvidenceVerification(
        run_id=run_id,
        status="VERIFIED" if recomputed == recorded_digest else "DIGEST_MISMATCH",
        recomputed_sha256=recomputed,
        recorded_sha256=recorded_digest,
        archive_size_bytes=size_bytes,
    )
