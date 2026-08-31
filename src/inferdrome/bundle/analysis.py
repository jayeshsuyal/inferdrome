"""Safe, read-only bundle recalculation for CLI inspection commands."""

import hmac
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
from inferdrome.domain.experiment import ExperimentSpec
from inferdrome.domain.ids import sha256_digest
from inferdrome.domain.metrics import MetricDefinitions
from inferdrome.domain.request_record import RequestRecord
from inferdrome.domain.states import RunState
from inferdrome.errors import ReductionError, VerificationError
from inferdrome.limits import WorkBudget
from inferdrome.metrics import ReductionResult, reduce_measurements
from inferdrome.workspace import RunWorkspace


@dataclass(frozen=True)
class BundleAnalysis:
    verification: VerificationReport
    reduction: ReductionResult


def recalculate_bundle(
    bundle_path: Path,
    *,
    expected_bundle_digest: str | None = None,
    work_budget: WorkBudget | None = None,
) -> BundleAnalysis:
    """Verify and independently recalculate one immutable bundle."""

    initial = verify_bundle(
        bundle_path,
        expected_bundle_digest=expected_bundle_digest,
        work_budget=work_budget,
    )
    reader = BundleReader(
        initial.bundle_path,
        require_immutable=True,
        work_budget=work_budget,
    )
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
    try:
        execution = ExecutionRecord.model_validate_json(execution_bytes)
        definitions = MetricDefinitions.model_validate_json(definition_bytes)
    except (RecursionError, ValueError, ValidationError):
        raise VerificationError(
            "bundle recalculation inputs failed contract validation"
        ) from None
    record_lines = strict_jsonl_lines(
        record_bytes,
        label="canonical request records",
        max_line_bytes=reader.limits.max_jsonl_line_bytes,
        max_records=reader.limits.max_jsonl_records,
        expected_records=execution.configured_traffic.measured_requests,
    )
    try:
        records = tuple(
            RequestRecord.model_validate_json(line) for line in record_lines
        )
    except (RecursionError, ValueError, ValidationError):
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
        work_budget=work_budget,
    )
    return BundleAnalysis(verification=final, reduction=reduction)


def verify_bundle_matches_workspace(
    workspace: RunWorkspace,
    analysis: BundleAnalysis | None = None,
    *,
    work_budget: WorkBudget | None = None,
) -> BundleAnalysis:
    """Bind a valid sealed bundle to the exact frozen workspace that produced it."""

    workspace.verify_frozen_inputs(work_budget=work_budget)
    if workspace.current_state(work_budget=work_budget).state is not RunState.COMPLETE:
        raise VerificationError("bundle workspace is not COMPLETE")
    selected = analysis or recalculate_bundle(
        workspace.path / "bundle",
        work_budget=work_budget,
    )
    verification = selected.verification
    expected_bundle_path = (workspace.path / "bundle").absolute()
    if (
        verification.bundle_path != expected_bundle_path
        or verification.run_id != workspace.run_id
    ):
        raise VerificationError("bundle is not attached to this run workspace")

    metadata = workspace.metadata
    bundle_descriptor = verification.descriptor
    if (
        metadata.run_id != bundle_descriptor.run_id
        or not hmac.compare_digest(
            metadata.source_spec_digest,
            bundle_descriptor.digests.source_spec_digest,
        )
        or not hmac.compare_digest(
            metadata.execution_fingerprint,
            bundle_descriptor.digests.execution_fingerprint,
        )
        or not hmac.compare_digest(
            metadata.request_plan_digest,
            bundle_descriptor.digests.request_plan_digest,
        )
    ):
        raise VerificationError("workspace resolution metadata disagrees with bundle")

    reader = BundleReader(
        verification.bundle_path,
        require_immutable=True,
        work_budget=work_budget,
    )
    role_paths = {
        artifact.role: artifact.path for artifact in bundle_descriptor.artifacts
    }
    comparisons = (
        (
            "inputs/experiment.original.yaml",
            ArtifactRole.ORIGINAL_SPEC,
        ),
        (
            "inputs/experiment.resolved.json",
            ArtifactRole.RESOLVED_SPEC,
        ),
        (
            "inputs/request-plan.json",
            ArtifactRole.REQUEST_PLAN,
        ),
    )
    for workspace_path, role in comparisons:
        workspace_bytes = workspace.read_frozen_input(
            workspace_path,
            work_budget=work_budget,
        )
        bundle_bytes = reader.read_bytes(role_paths[role])
        if workspace_bytes != bundle_bytes:
            raise VerificationError(
                "bundle bytes disagree with frozen workspace inputs"
            )

    resolved_bytes = workspace.read_frozen_input(
        "inputs/experiment.resolved.json",
        work_budget=work_budget,
    )
    try:
        resolved = ExperimentSpec.model_validate_json(resolved_bytes)
    except ValidationError:
        raise VerificationError(
            "workspace resolved input failed contract validation"
        ) from None
    workload_bytes = workspace.read_frozen_input(
        "inputs/workload.source.jsonl",
        work_budget=work_budget,
    )
    if not hmac.compare_digest(
        sha256_digest(workload_bytes),
        resolved.workload.sha256,
    ):
        raise VerificationError("workspace workload digest disagrees")
    reader.assert_unchanged()
    workspace.verify_frozen_inputs(work_budget=work_budget)
    return selected
