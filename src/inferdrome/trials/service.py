"""Fail-closed storage and analysis for immutable repeated-trial groupings."""

import hmac
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from inferdrome.bundle import BundleAnalysis, recalculate_bundle
from inferdrome.domain.digests import (
    DigestDomain,
    canonical_json_bytes,
    digest_bytes,
)
from inferdrome.domain.ids import RunId, new_trial_set_id
from inferdrome.domain.metrics import Measurement
from inferdrome.domain.states import EvidenceEligibility
from inferdrome.domain.trial_set import TrialSet, TrialSetMember
from inferdrome.errors import TrialSetError
from inferdrome.immutable import publish_immutable_directory

_TRIAL_SET_FILENAME = "trial-set.json"
_MAX_TRIAL_SET_BYTES = 262_144
_ROUNDING_QUANTUM = Decimal("0.000001")
_STATISTICS_PRECISION = 80


@dataclass(frozen=True)
class TrialSetComparisonAuthority:
    scope: Literal[
        "CONTROLLED_OUTCOME_ELIGIBLE",
        "DESCRIPTIVE_ONLY_NON_AUTHORITATIVE",
    ]
    exitspec_contract_digest: str | None
    issues: tuple[
        Literal[
            "EVIDENCE_NOT_CUSTOMER_ELIGIBLE",
            "EXITSPEC_CONTRACT_IDENTITY_MISSING",
            "EXITSPEC_CONTRACT_IDENTITY_MISMATCH",
        ],
        ...,
    ]


@dataclass(frozen=True)
class VerifiedTrialSet:
    path: Path
    descriptor: TrialSet
    trial_set_digest: str
    members: tuple[BundleAnalysis, ...]

    @property
    def comparison_authority(self) -> TrialSetComparisonAuthority:
        """Compute fail-closed comparison authority from verified members."""

        return _comparison_authority(self.members)


@dataclass(frozen=True)
class TrialRunValue:
    run_id: str
    value: str | None
    sample_count: int | None


@dataclass(frozen=True)
class TrialMetricVariation:
    metric: str
    aggregation: str
    unit: str
    values: tuple[TrialRunValue, ...]
    available_run_count: int
    minimum: str | None
    maximum: str | None
    median: str | None
    mean: str | None
    span: str | None
    sample_standard_deviation: str | None


def _canonical_trial_set_bytes(descriptor: TrialSet) -> bytes:
    return canonical_json_bytes(
        descriptor.model_dump(mode="json", by_alias=True, exclude_none=False)
    )


def _is_real_directory(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _is_regular_file(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _node_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validated_run_id(value: str) -> str:
    try:
        return TypeAdapter(RunId).validate_python(value, strict=True)
    except ValidationError:
        raise TrialSetError("trial-set member run ID is invalid") from None


def _bundle_path_for_run(runs_root: Path, run_id: str) -> Path:
    selected_id = _validated_run_id(run_id)
    root = runs_root.absolute()
    if not _is_real_directory(root):
        raise TrialSetError("runs root must be a real directory")
    run_path = root / selected_id
    if not _is_real_directory(run_path):
        raise TrialSetError("trial-set member workspace is unavailable or unsafe")
    workspace_bundle = run_path / "bundle"
    if _is_real_directory(workspace_bundle):
        return workspace_bundle
    if _is_regular_file(run_path / "bundle.json"):
        return run_path
    raise TrialSetError("trial-set member bundle is unavailable")


def _verified_members(
    descriptor: TrialSet,
    runs_root: Path,
) -> tuple[BundleAnalysis, ...]:
    analyses: list[BundleAnalysis] = []
    for member in descriptor.members:
        try:
            analysis = recalculate_bundle(
                _bundle_path_for_run(runs_root, member.run_id),
                expected_bundle_digest=member.bundle_digest,
            )
        except TrialSetError:
            raise
        except Exception as error:
            raise TrialSetError(
                "trial-set member bundle failed verification"
            ) from error
        verification = analysis.verification
        bundle_descriptor = verification.descriptor
        if verification.run_id != member.run_id:
            raise TrialSetError("trial-set member run identity disagrees")
        if bundle_descriptor.experiment_id != descriptor.experiment_id:
            raise TrialSetError("trial-set members must share one experiment ID")
        if (
            bundle_descriptor.digests.execution_fingerprint
            != descriptor.execution_fingerprint
        ):
            raise TrialSetError(
                "trial-set members must share one execution fingerprint"
            )
        if (
            bundle_descriptor.digests.metric_definitions_digest
            != descriptor.metric_definitions_digest
        ):
            raise TrialSetError(
                "trial-set members must share one metric-definition set"
            )
        if (
            analysis.reduction.measurements.reducer_version
            != descriptor.reducer_version
        ):
            raise TrialSetError("trial-set members must share one reducer version")
        analyses.append(analysis)
    return tuple(analyses)


def _comparison_authority(
    analyses: tuple[BundleAnalysis, ...],
) -> TrialSetComparisonAuthority:
    issues: list[
        Literal[
            "EVIDENCE_NOT_CUSTOMER_ELIGIBLE",
            "EXITSPEC_CONTRACT_IDENTITY_MISSING",
            "EXITSPEC_CONTRACT_IDENTITY_MISMATCH",
        ]
    ] = []
    if any(
        analysis.verification.descriptor.evidence_eligibility
        is not EvidenceEligibility.CUSTOMER_ELIGIBLE
        for analysis in analyses
    ):
        issues.append("EVIDENCE_NOT_CUSTOMER_ELIGIBLE")

    contract_digests = tuple(
        analysis.verification.descriptor.digests.exitspec_contract_digest
        for analysis in analyses
    )
    if any(digest is None for digest in contract_digests):
        issues.append("EXITSPEC_CONTRACT_IDENTITY_MISSING")
    non_null_digests = {digest for digest in contract_digests if digest is not None}
    if len(non_null_digests) > 1:
        issues.append("EXITSPEC_CONTRACT_IDENTITY_MISMATCH")
    shared_digest = (
        next(iter(non_null_digests))
        if len(non_null_digests) == 1
        and all(digest is not None for digest in contract_digests)
        else None
    )
    return TrialSetComparisonAuthority(
        scope=(
            "CONTROLLED_OUTCOME_ELIGIBLE"
            if not issues
            else "DESCRIPTIVE_ONLY_NON_AUTHORITATIVE"
        ),
        exitspec_contract_digest=shared_digest,
        issues=tuple(issues),
    )


def _load_descriptor(path: Path) -> tuple[TrialSet, bytes]:
    trial_path = path.absolute()
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        initial_path_directory = os.stat(trial_path, follow_symlinks=False)
        if not stat.S_ISDIR(initial_path_directory.st_mode):
            raise TrialSetError("trial-set path must be a real directory")
        directory_descriptor = os.open(trial_path, directory_flags)
    except OSError:
        raise TrialSetError("trial-set path must be a real directory") from None
    content = bytearray()
    try:
        opened_directory = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(opened_directory.st_mode)
            or _node_identity(initial_path_directory)
            != _node_identity(opened_directory)
        ):
            raise TrialSetError("trial-set directory changed while being opened")
        if stat.S_IMODE(opened_directory.st_mode) & 0o222:
            raise TrialSetError("trial-set directory must be read-only")
        if os.listdir(directory_descriptor) != [_TRIAL_SET_FILENAME]:
            raise TrialSetError("trial-set directory contains undeclared entries")

        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        initial_path_file = os.stat(
            _TRIAL_SET_FILENAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(initial_path_file.st_mode)
            or initial_path_file.st_nlink != 1
            or stat.S_IMODE(initial_path_file.st_mode) & 0o222
            or initial_path_file.st_size > _MAX_TRIAL_SET_BYTES
        ):
            raise TrialSetError(
                "trial-set descriptor is not one bounded read-only file"
            )
        file_descriptor = os.open(
            _TRIAL_SET_FILENAME,
            file_flags,
            dir_fd=directory_descriptor,
        )
        try:
            opened_file = os.fstat(file_descriptor)
            if _node_identity(initial_path_file) != _node_identity(opened_file):
                raise TrialSetError(
                    "trial-set descriptor changed while being opened"
                )
            remaining = opened_file.st_size
            while remaining:
                chunk = os.read(file_descriptor, min(65_536, remaining))
                if not chunk:
                    raise TrialSetError("trial-set descriptor was truncated")
                content.extend(chunk)
                remaining -= len(chunk)
            if os.read(file_descriptor, 1):
                raise TrialSetError("trial-set descriptor grew during its read")
            final_open_file = os.fstat(file_descriptor)
            final_path_file = os.stat(
                _TRIAL_SET_FILENAME,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                _node_identity(opened_file) != _node_identity(final_open_file)
                or _node_identity(opened_file) != _node_identity(final_path_file)
            ):
                raise TrialSetError(
                    "trial-set descriptor changed during its read"
                )
        finally:
            os.close(file_descriptor)

        final_open_directory = os.fstat(directory_descriptor)
        final_path_directory = os.stat(trial_path, follow_symlinks=False)
        if (
            os.listdir(directory_descriptor) != [_TRIAL_SET_FILENAME]
            or _node_identity(opened_directory)
            != _node_identity(final_open_directory)
            or _node_identity(opened_directory)
            != _node_identity(final_path_directory)
        ):
            raise TrialSetError("trial-set directory changed during its read")
    except OSError:
        raise TrialSetError("trial-set descriptor changed during its read") from None
    finally:
        os.close(directory_descriptor)
    raw_content = bytes(content)
    try:
        descriptor = TrialSet.model_validate_json(raw_content)
    except ValidationError:
        raise TrialSetError("trial-set descriptor failed contract validation") from None
    if trial_path.name != descriptor.trial_set_id:
        raise TrialSetError("trial-set directory and identifier disagree")
    if raw_content != _canonical_trial_set_bytes(descriptor):
        raise TrialSetError("trial-set descriptor is not canonical JSON")
    return descriptor, raw_content


def verify_trial_set(
    path: Path,
    *,
    runs_root: Path,
    expected_trial_set_digest: str | None = None,
) -> VerifiedTrialSet:
    """Verify one immutable grouping and independently recalculate every member."""

    descriptor, initial_bytes = _load_descriptor(path)
    trial_set_digest = digest_bytes(DigestDomain.TRIAL_SET, initial_bytes)
    if expected_trial_set_digest is not None and not hmac.compare_digest(
        expected_trial_set_digest,
        trial_set_digest,
    ):
        raise TrialSetError("trial-set digest does not match the expected value")
    members = _verified_members(descriptor, runs_root)
    final_descriptor, final_bytes = _load_descriptor(path)
    if final_descriptor != descriptor or final_bytes != initial_bytes:
        raise TrialSetError("trial-set descriptor changed during verification")
    return VerifiedTrialSet(
        path=path.absolute(),
        descriptor=descriptor,
        trial_set_digest=trial_set_digest,
        members=members,
    )


def create_trial_set(
    *,
    runs_root: Path,
    trial_sets_root: Path,
    run_ids: Sequence[str],
    title: str,
    hypothesis: str | None = None,
    trial_set_id: str | None = None,
    created_at: datetime | None = None,
) -> VerifiedTrialSet:
    """Create one immutable same-configuration grouping from completed runs."""

    if not 2 <= len(run_ids) <= 100:
        raise TrialSetError("a trial set requires between 2 and 100 runs")
    selected_ids = tuple(_validated_run_id(run_id) for run_id in run_ids)
    if len(set(selected_ids)) != len(selected_ids):
        raise TrialSetError("trial-set run IDs must be unique")

    analyses: list[BundleAnalysis] = []
    for run_id in selected_ids:
        try:
            analyses.append(
                recalculate_bundle(_bundle_path_for_run(runs_root, run_id))
            )
        except TrialSetError:
            raise
        except Exception as error:
            raise TrialSetError(
                "trial-set member bundle failed verification"
            ) from error
    first = analyses[0].verification
    try:
        descriptor = TrialSet(
            schema_version="inferdrome.trial-set.v1",
            trial_set_id=trial_set_id or new_trial_set_id(),
            experiment_id=first.descriptor.experiment_id,
            title=title,
            hypothesis=hypothesis,
            created_at=created_at or datetime.now(UTC),
            design_status="RETROSPECTIVE",
            membership_policy="same_execution_fingerprint_v1",
            request_population_policy="separate_per_run_v1",
            statistical_unit="run",
            weighting="equal_per_run",
            execution_fingerprint=(
                first.descriptor.digests.execution_fingerprint
            ),
            metric_definitions_digest=(
                first.descriptor.digests.metric_definitions_digest
            ),
            reducer_version=analyses[0].reduction.measurements.reducer_version,
            members=tuple(
                TrialSetMember(
                    repetition_index=index,
                    run_id=analysis.verification.run_id,
                    bundle_digest=analysis.verification.bundle_digest,
                )
                for index, analysis in enumerate(analyses)
            ),
        )
    except ValidationError:
        raise TrialSetError("trial-set metadata failed contract validation") from None
    _verified_members(descriptor, runs_root)
    content = _canonical_trial_set_bytes(descriptor)

    root = trial_sets_root.absolute()
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        raise TrialSetError("trial-sets root could not be created") from None
    if not _is_real_directory(root):
        raise TrialSetError("trial-sets root must be a real directory")
    try:
        destination = publish_immutable_directory(
            root=root,
            artifact_id=descriptor.trial_set_id,
            filename=_TRIAL_SET_FILENAME,
            content=content,
        )
    except FileExistsError:
        raise TrialSetError("trial-set ID is already reserved") from None
    except (OSError, ValueError) as error:
        raise TrialSetError("trial-set publication failed closed") from error
    return verify_trial_set(destination, runs_root=runs_root)


def _decimal_text(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = _STATISTICS_PRECISION
        context.rounding = ROUND_HALF_EVEN
        rounded = value.quantize(_ROUNDING_QUANTUM)
        normalized = rounded.normalize()
    if rounded == 0:
        return "0"
    return format(normalized, "f")


def _measurement_key(measurement: Measurement) -> tuple[str, str]:
    return measurement.metric.value, measurement.aggregation.value


def trial_metric_variations(
    verified: VerifiedTrialSet,
) -> tuple[TrialMetricVariation, ...]:
    """Summarize per-run values without pooling underlying request records."""

    ordered_keys: list[tuple[str, str]] = []
    measurements_by_run: list[dict[tuple[str, str], Measurement]] = []
    unit_by_key: dict[tuple[str, str], str] = {}
    for analysis in verified.members:
        by_key: dict[tuple[str, str], Measurement] = {}
        for measurement in analysis.reduction.measurements.measurements:
            key = _measurement_key(measurement)
            if key not in unit_by_key:
                ordered_keys.append(key)
                unit_by_key[key] = measurement.unit.value
            elif unit_by_key[key] != measurement.unit.value:
                raise TrialSetError("trial-set metric units disagree")
            by_key[key] = measurement
        measurements_by_run.append(by_key)

    variations: list[TrialMetricVariation] = []
    run_ids = tuple(member.run_id for member in verified.descriptor.members)
    for key in ordered_keys:
        run_values: list[TrialRunValue] = []
        numeric_values: list[Decimal] = []
        for run_id, by_key in zip(run_ids, measurements_by_run, strict=True):
            selected_measurement = by_key.get(key)
            if selected_measurement is None:
                run_values.append(
                    TrialRunValue(run_id=run_id, value=None, sample_count=None)
                )
                continue
            value = Decimal(str(selected_measurement.value))
            numeric_values.append(value)
            run_values.append(
                TrialRunValue(
                    run_id=run_id,
                    value=str(selected_measurement.value),
                    sample_count=selected_measurement.sample_count,
                )
            )

        numeric_values.sort()
        minimum: Decimal | None
        maximum: Decimal | None
        median: Decimal | None
        mean: Decimal | None
        span: Decimal | None
        sample_standard_deviation: Decimal | None
        with localcontext() as context:
            context.prec = _STATISTICS_PRECISION
            context.rounding = ROUND_HALF_EVEN
            if numeric_values:
                minimum = numeric_values[0]
                maximum = numeric_values[-1]
                midpoint = len(numeric_values) // 2
                median = (
                    numeric_values[midpoint]
                    if len(numeric_values) % 2
                    else (
                        numeric_values[midpoint - 1] + numeric_values[midpoint]
                    )
                    / 2
                )
                mean = sum(numeric_values, start=Decimal(0)) / len(
                    numeric_values
                )
                span = maximum - minimum
                if len(numeric_values) >= 2:
                    sample_variance = (
                        sum(
                            ((value - mean) ** 2 for value in numeric_values),
                            start=Decimal(0),
                        )
                        / (len(numeric_values) - 1)
                    )
                    sample_standard_deviation = sample_variance.sqrt()
                else:
                    sample_standard_deviation = None
            else:
                minimum = None
                maximum = None
                median = None
                mean = None
                span = None
                sample_standard_deviation = None
        variations.append(
            TrialMetricVariation(
                metric=key[0],
                aggregation=key[1],
                unit=unit_by_key[key],
                values=tuple(run_values),
                available_run_count=len(numeric_values),
                minimum=None if minimum is None else _decimal_text(minimum),
                maximum=None if maximum is None else _decimal_text(maximum),
                median=None if median is None else _decimal_text(median),
                mean=None if mean is None else _decimal_text(mean),
                span=None if span is None else _decimal_text(span),
                sample_standard_deviation=(
                    None
                    if sample_standard_deviation is None
                    else _decimal_text(sample_standard_deviation)
                ),
            )
        )
    return tuple(variations)
