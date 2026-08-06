"""Safe, read-only bundle recalculation for CLI inspection commands."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from inferdrome.bundle.reader import (
    BundleReader,
    strict_json_value,
    strict_jsonl_lines,
)
from inferdrome.bundle.verifier import VerificationReport, verify_bundle
from inferdrome.domain.evidence import ArtifactRole
from inferdrome.domain.execution import ExecutionRecord
from inferdrome.domain.metrics import MetricDefinitions
from inferdrome.domain.request_record import RequestRecord
from inferdrome.errors import ReductionError, VerificationError
from inferdrome.metrics import ReductionResult, reduce_measurements


@dataclass(frozen=True)
class BundleAnalysis:
    verification: VerificationReport
    reduction: ReductionResult


def recalculate_bundle(
    bundle_path: Path,
    *,
    expected_bundle_digest: str | None = None,
) -> BundleAnalysis:
    """Verify and independently recalculate one immutable bundle."""

    initial = verify_bundle(
        bundle_path,
        expected_bundle_digest=expected_bundle_digest,
    )
    reader = BundleReader(initial.bundle_path, require_immutable=True)
    role_paths = {
        artifact.role: artifact.path for artifact in initial.descriptor.artifacts
    }

    execution_bytes = reader.read_bytes(role_paths[ArtifactRole.EXECUTION])
    definition_bytes = reader.read_bytes(
        role_paths[ArtifactRole.METRIC_DEFINITIONS]
    )
    record_bytes = reader.read_bytes(role_paths[ArtifactRole.REQUEST_RECORDS])
    strict_json_value(execution_bytes, label="execution record")
    strict_json_value(definition_bytes, label="metric definitions")
    record_lines = strict_jsonl_lines(
        record_bytes,
        label="canonical request records",
        max_line_bytes=reader.limits.max_jsonl_line_bytes,
    )
    try:
        execution = ExecutionRecord.model_validate_json(execution_bytes)
        definitions = MetricDefinitions.model_validate_json(definition_bytes)
        records = tuple(
            RequestRecord.model_validate_json(line) for line in record_lines
        )
    except ValidationError:
        raise VerificationError(
            "bundle recalculation inputs failed contract validation"
        ) from None
    try:
        reduction = reduce_measurements(
            execution,
            records,
            metric_definitions=definitions,
        )
    except ReductionError as error:
        raise VerificationError("bundle recalculation failed") from error
    stored_measurements = reader.read_bytes(
        role_paths[ArtifactRole.MEASUREMENTS]
    )
    if reduction.measurements_bytes != stored_measurements:
        raise VerificationError("bundle measurements disagree with recalculation")
    reader.assert_unchanged()

    final = verify_bundle(
        initial.bundle_path,
        expected_bundle_digest=initial.bundle_digest,
    )
    return BundleAnalysis(verification=final, reduction=reduction)
