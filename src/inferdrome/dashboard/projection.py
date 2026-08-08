"""Verified bundle-to-dashboard projections with no raw-content exposure."""

import hashlib
import math
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ValidationError

from inferdrome.bundle import BundleAnalysis, recalculate_bundle, verify_bundle
from inferdrome.bundle.reader import BundleReader, strict_json_value, strict_jsonl_lines
from inferdrome.dashboard.models import (
    ArtifactView,
    ComparisonContractView,
    ContextFieldView,
    DigestView,
    DistributionBin,
    DistributionView,
    EnvironmentFieldView,
    ExecutionView,
    MetricView,
    RunDetail,
    RunSummary,
    SensitivityView,
    UnavailableMetricView,
    VerificationView,
)
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.environment import EnvironmentFieldName, EnvironmentManifest
from inferdrome.domain.evidence import ArtifactRole
from inferdrome.domain.execution import ExecutionRecord
from inferdrome.domain.experiment import (
    AttachedVllmTarget,
    ConcurrentTraffic,
    ExperimentSpec,
)
from inferdrome.domain.metrics import Aggregation, Measurement, MetricId, Unit
from inferdrome.domain.request_record import RequestRecord, RequestStatus
from inferdrome.errors import VerificationError

_METRIC_LABELS: dict[tuple[MetricId, Aggregation], str] = {
    (MetricId.MEASURED_REQUEST_COUNT, Aggregation.COUNT): "Measured requests",
    (MetricId.SUCCESSFUL_REQUEST_COUNT, Aggregation.COUNT): "Successful requests",
    (MetricId.FAILED_REQUEST_COUNT, Aggregation.COUNT): "Failed requests",
    (MetricId.ERROR_RATE, Aggregation.RATIO): "Error rate",
    (MetricId.TTFT_NS, Aggregation.MEAN): "TTFT mean",
    (MetricId.TTFT_NS, Aggregation.P50): "TTFT P50",
    (MetricId.TTFT_NS, Aggregation.P95): "TTFT P95",
    (MetricId.TTFT_NS, Aggregation.P99): "TTFT P99",
    (MetricId.LAST_CHOICES_EVENT_SPAN_NS, Aggregation.MEAN): "Choices span mean",
    (MetricId.LAST_CHOICES_EVENT_SPAN_NS, Aggregation.P50): "Choices span P50",
    (MetricId.LAST_CHOICES_EVENT_SPAN_NS, Aggregation.P95): "Choices span P95",
    (MetricId.LAST_CHOICES_EVENT_SPAN_NS, Aggregation.P99): "Choices span P99",
    (
        MetricId.ATTEMPTED_REQUEST_THROUGHPUT,
        Aggregation.RATE,
    ): "Attempted request throughput",
    (
        MetricId.SUCCESSFUL_REQUEST_THROUGHPUT,
        Aggregation.RATE,
    ): "Successful request throughput",
    (MetricId.OUTPUT_TOKEN_THROUGHPUT, Aggregation.RATE): "Output throughput",
}

_ENVIRONMENT_LABELS: dict[EnvironmentFieldName, str] = {
    EnvironmentFieldName.CLIENT_OS: "Client OS",
    EnvironmentFieldName.CLIENT_ARCH: "Client architecture",
    EnvironmentFieldName.CLIENT_PYTHON_VERSION: "Client Python",
    EnvironmentFieldName.PRODUCER_VERSION: "Producer version",
    EnvironmentFieldName.PRODUCER_DISTRIBUTION_SHA256: "Producer distribution",
    EnvironmentFieldName.TARGET_ENGINE_VERSION: "Target engine version",
    EnvironmentFieldName.TARGET_MODEL_REVISION: "Model revision",
    EnvironmentFieldName.TARGET_TOKENIZER_REVISION: "Tokenizer revision",
    EnvironmentFieldName.SERVER_MODEL_ID: "Server model ID",
    EnvironmentFieldName.GPU_MODEL: "GPU model",
    EnvironmentFieldName.GPU_COUNT: "GPU count",
    EnvironmentFieldName.CUDA_VERSION: "CUDA version",
    EnvironmentFieldName.DRIVER_VERSION: "Driver version",
}


def _read_model[ModelT: BaseModel](
    reader: BundleReader,
    path: str,
    model: type[ModelT],
    *,
    label: str,
) -> ModelT:
    content = reader.read_bytes(path)
    strict_json_value(content, label=label)
    try:
        return model.model_validate_json(content)
    except ValidationError:
        raise VerificationError(f"{label} failed contract validation") from None


def _read_records(reader: BundleReader, path: str) -> tuple[RequestRecord, ...]:
    content = reader.read_bytes(path)
    lines = strict_jsonl_lines(
        content,
        label="canonical request records",
        max_line_bytes=reader.limits.max_jsonl_line_bytes,
    )
    try:
        return tuple(RequestRecord.model_validate_json(line) for line in lines)
    except ValidationError:
        raise VerificationError(
            "canonical request records failed contract validation"
        ) from None


def _duration_ns(started_at: datetime, ended_at: datetime) -> int:
    elapsed = ended_at - started_at
    return (
        elapsed.days * 86_400_000_000_000
        + elapsed.seconds * 1_000_000_000
        + elapsed.microseconds * 1_000
    )


def _decimal_text(value: int | str) -> str:
    return str(value)


def _format_decimal(value: Decimal, places: int = 2) -> str:
    return f"{value:.{places}f}"


def display_measurement(value: int | str, unit: Unit) -> str:
    exact = Decimal(str(value))
    if unit is Unit.COUNT:
        return f"{int(exact):,}"
    if unit is Unit.RATIO:
        return f"{_format_decimal(exact * 100)}%"
    if unit is Unit.NANOSECONDS:
        return f"{_format_decimal(exact / Decimal(1_000_000))} ms"
    if unit is Unit.REQUESTS_PER_SECOND:
        return f"{_format_decimal(exact)} req/s"
    if unit is Unit.TOKENS_PER_SECOND:
        return f"{_format_decimal(exact)} tok/s"
    return str(value)


def metric_label(metric: str, aggregation: str) -> str:
    """Return the frozen dashboard label for one metric series."""

    try:
        return _METRIC_LABELS[(MetricId(metric), Aggregation(aggregation))]
    except (KeyError, ValueError):
        raise VerificationError("trial-set metric identity is unsupported") from None


def _metric_view(measurement: Measurement) -> MetricView:
    key = f"{measurement.metric.value}:{measurement.aggregation.value}"
    return MetricView(
        key=key,
        metric=measurement.metric.value,
        aggregation=measurement.aggregation.value,
        label=_METRIC_LABELS[(measurement.metric, measurement.aggregation)],
        value=_decimal_text(measurement.value),
        display_value=display_measurement(measurement.value, measurement.unit),
        unit=measurement.unit.value,
        sample_count=measurement.sample_count,
        population=measurement.population.value,
        definition_id=measurement.definition_id.value,
        quantile_method=(
            measurement.quantile_method.value
            if measurement.quantile_method is not None
            else None
        ),
        rounding_policy=measurement.rounding_policy.value,
    )


def _distribution(
    metric: str,
    label: str,
    values: tuple[int, ...],
) -> DistributionView:
    if not values:
        return DistributionView(
            metric=metric,
            label=label,
            unit="ns",
            sample_count=0,
            minimum=None,
            maximum=None,
            bins=(),
        )

    minimum = min(values)
    maximum = max(values)
    bins: tuple[DistributionBin, ...]
    if minimum == maximum:
        bins = (
            DistributionBin(
                lower_bound=minimum,
                upper_bound=maximum,
                count=len(values),
            ),
        )
    else:
        requested_bins = min(12, max(2, math.ceil(math.sqrt(len(values)))))
        width = max(1, math.ceil((maximum - minimum + 1) / requested_bins))
        bin_count = math.ceil((maximum - minimum + 1) / width)
        counts = [0] * bin_count
        for value in values:
            index = min((value - minimum) // width, bin_count - 1)
            counts[index] += 1
        bins = tuple(
            DistributionBin(
                lower_bound=minimum + index * width,
                upper_bound=min(maximum, minimum + (index + 1) * width - 1),
                count=count,
            )
            for index, count in enumerate(counts)
        )
    return DistributionView(
        metric=metric,
        label=label,
        unit="ns",
        sample_count=len(values),
        minimum=minimum,
        maximum=maximum,
        bins=bins,
    )


def _context(spec: ExperimentSpec) -> tuple[ContextFieldView, ...]:
    target = spec.target
    target_revision = (
        target.model_revision if isinstance(target, AttachedVllmTarget) else None
    )
    tokenizer_revision = (
        target.tokenizer_revision if isinstance(target, AttachedVllmTarget) else None
    )
    engine_version = (
        target.engine_version if isinstance(target, AttachedVllmTarget) else None
    )
    endpoint_identity = (
        "sha256:"
        + hashlib.sha256(
            b"inferdrome.dashboard.endpoint-identity.v1\0"
            + str(target.endpoint).encode("utf-8")
        ).hexdigest()
        if isinstance(target, AttachedVllmTarget)
        else None
    )
    traffic = spec.traffic
    concurrency = (
        traffic.concurrency if isinstance(traffic, ConcurrentTraffic) else None
    )
    request_rate = (
        None if isinstance(traffic, ConcurrentTraffic) else traffic.requests_per_second
    )
    return (
        ContextFieldView(
            key="experiment.id",
            label="Experiment ID",
            value=spec.experiment.id,
            group="experiment",
        ),
        ContextFieldView(
            key="experiment.title",
            label="Experiment title",
            value=spec.experiment.title,
            group="experiment",
        ),
        ContextFieldView(
            key="experiment.hypothesis",
            label="Hypothesis",
            value=spec.experiment.hypothesis,
            group="experiment",
        ),
        ContextFieldView(
            key="execution.max_runtime_seconds",
            label="Maximum runtime",
            value=str(spec.execution.max_runtime_seconds),
            group="execution",
        ),
        ContextFieldView(
            key="execution.max_measured_requests",
            label="Maximum measured requests",
            value=str(spec.execution.max_measured_requests),
            group="execution",
        ),
        ContextFieldView(
            key="target.engine",
            label="Target engine",
            value=target.engine,
            group="target",
        ),
        ContextFieldView(
            key="target.api",
            label="Target API",
            value=target.api,
            group="target",
        ),
        ContextFieldView(
            key="target.endpoint_identity",
            label="Endpoint identity digest",
            value=endpoint_identity,
            group="target",
        ),
        ContextFieldView(
            key="target.model",
            label="Model",
            value=target.model,
            group="target",
        ),
        ContextFieldView(
            key="target.model_revision",
            label="Model revision",
            value=target_revision,
            group="target",
        ),
        ContextFieldView(
            key="target.tokenizer_revision",
            label="Tokenizer revision",
            value=tokenizer_revision,
            group="target",
        ),
        ContextFieldView(
            key="target.engine_version",
            label="Engine version",
            value=engine_version,
            group="target",
        ),
        ContextFieldView(
            key="traffic.kind",
            label="Traffic model",
            value=traffic.kind,
            group="traffic",
        ),
        ContextFieldView(
            key="traffic.concurrency",
            label="Concurrency",
            value=str(concurrency) if concurrency is not None else None,
            group="traffic",
        ),
        ContextFieldView(
            key="traffic.requests_per_second",
            label="Request rate",
            value=request_rate,
            group="traffic",
        ),
        ContextFieldView(
            key="measurement.ttft_definition",
            label="TTFT definition",
            value=spec.measurement.ttft_definition,
            group="measurement",
        ),
        ContextFieldView(
            key="measurement.choices_span_definition",
            label="Choices span definition",
            value=spec.measurement.choices_span_definition,
            group="measurement",
        ),
        ContextFieldView(
            key="producer.name",
            label="Producer",
            value=spec.execution.producer_name,
            group="producer",
        ),
        ContextFieldView(
            key="producer.version",
            label="Producer version",
            value=spec.execution.producer_version,
            group="producer",
        ),
        ContextFieldView(
            key="producer.adapter",
            label="Adapter",
            value=spec.execution.adapter,
            group="producer",
        ),
        ContextFieldView(
            key="producer.adapter_version",
            label="Adapter version",
            value=spec.execution.adapter_version,
            group="producer",
        ),
    )


def _signature(model: BaseModel) -> str:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    return canonical_json_bytes(value).decode("utf-8")


def load_run_detail(bundle_path: Path) -> RunDetail:
    """Verify, recalculate, and project one immutable evidence bundle."""

    return project_run_detail(recalculate_bundle(bundle_path))


def project_run_detail(analysis: BundleAnalysis) -> RunDetail:
    """Project one already verified and recalculated bundle analysis."""

    report = analysis.verification
    descriptor = report.descriptor
    role_paths = {artifact.role: artifact.path for artifact in descriptor.artifacts}
    reader = BundleReader(report.bundle_path, require_immutable=True)

    resolved = _read_model(
        reader,
        role_paths[ArtifactRole.RESOLVED_SPEC],
        ExperimentSpec,
        label="resolved experiment",
    )
    execution = _read_model(
        reader,
        role_paths[ArtifactRole.EXECUTION],
        ExecutionRecord,
        label="execution record",
    )
    environment = _read_model(
        reader,
        role_paths[ArtifactRole.ENVIRONMENT],
        EnvironmentManifest,
        label="environment manifest",
    )
    records = _read_records(reader, role_paths[ArtifactRole.REQUEST_RECORDS])

    reader.assert_unchanged()
    verify_bundle(report.bundle_path, expected_bundle_digest=report.bundle_digest)

    metric_views = tuple(
        _metric_view(measurement)
        for measurement in analysis.reduction.measurements.measurements
    )
    metric_by_key = {metric.key: metric for metric in metric_views}

    def required_value(metric: MetricId, aggregation: Aggregation) -> MetricView:
        return metric_by_key[f"{metric.value}:{aggregation.value}"]

    def optional_int(metric: MetricId, aggregation: Aggregation) -> int | None:
        view = metric_by_key.get(f"{metric.value}:{aggregation.value}")
        return int(view.value) if view is not None else None

    measured = required_value(MetricId.MEASURED_REQUEST_COUNT, Aggregation.COUNT)
    successful = required_value(MetricId.SUCCESSFUL_REQUEST_COUNT, Aggregation.COUNT)
    failed = required_value(MetricId.FAILED_REQUEST_COUNT, Aggregation.COUNT)
    error_rate = required_value(MetricId.ERROR_RATE, Aggregation.RATIO)
    output_throughput = required_value(
        MetricId.OUTPUT_TOKEN_THROUGHPUT, Aggregation.RATE
    )
    headline_keys = (
        f"{MetricId.OUTPUT_TOKEN_THROUGHPUT.value}:{Aggregation.RATE.value}",
        f"{MetricId.TTFT_NS.value}:{Aggregation.P50.value}",
        f"{MetricId.TTFT_NS.value}:{Aggregation.P95.value}",
        f"{MetricId.ERROR_RATE.value}:{Aggregation.RATIO.value}",
    )
    headline_metrics = tuple(
        metric_by_key[key] for key in headline_keys if key in metric_by_key
    )
    duration_ns = _duration_ns(execution.started_at, execution.ended_at)

    summary = RunSummary(
        run_id=report.run_id,
        experiment_id=descriptor.experiment_id,
        title=resolved.experiment.title,
        model=resolved.target.model,
        producer_name=descriptor.producer.name,
        producer_version=descriptor.producer.version,
        adapter_name=descriptor.producer.adapter,
        adapter_version=descriptor.producer.adapter_version,
        execution_mode=descriptor.execution_mode.value,
        started_at=execution.started_at,
        ended_at=execution.ended_at,
        duration_ns=duration_ns,
        integrity_status="VALID",
        evidence_eligibility=descriptor.evidence_eligibility.value,
        environment_completeness=descriptor.environment_completeness.value,
        replayability=descriptor.replayability.value,
        bundle_digest=report.bundle_digest,
        measured_requests=int(measured.value),
        successful_requests=int(successful.value),
        failed_requests=int(failed.value),
        error_rate=error_rate.value,
        ttft_p50_ns=optional_int(MetricId.TTFT_NS, Aggregation.P50),
        ttft_p95_ns=optional_int(MetricId.TTFT_NS, Aggregation.P95),
        output_token_throughput_per_s=output_throughput.value,
        headline_metrics=headline_metrics,
    )

    traffic = execution.configured_traffic
    execution_view = ExecutionView(
        terminal_state=execution.terminal_state,
        started_at=execution.started_at,
        ended_at=execution.ended_at,
        duration_ns=duration_ns,
        measurement_window_ns=execution.measurement_window_ns,
        measurement_window_definition=execution.measurement_window_definition,
        traffic_kind=traffic.kind,
        concurrency=(
            traffic.concurrency if isinstance(traffic, ConcurrentTraffic) else None
        ),
        requests_per_second=(
            None
            if isinstance(traffic, ConcurrentTraffic)
            else traffic.requests_per_second
        ),
        max_concurrency=(
            None if isinstance(traffic, ConcurrentTraffic) else traffic.max_concurrency
        ),
        warmup_requests=traffic.warmup_requests,
        measured_requests=traffic.measured_requests,
        producer_exit_status=execution.producer_exit_status,
    )

    successful_records = tuple(
        record for record in records if record.outcome.status is RequestStatus.SUCCESS
    )
    ttft_values = tuple(
        record.timing.ttft_ns
        for record in successful_records
        if record.timing.ttft_ns is not None
    )
    choices_span_values = tuple(
        record.timing.ttft_ns + sum(record.timing.itl_ns)
        for record in successful_records
        if record.timing.ttft_ns is not None
    )

    artifact_views = tuple(
        ArtifactView(
            role=artifact.role.value,
            path=artifact.path,
            media_type=artifact.media_type.value,
            sensitivity=artifact.sensitivity.value,
            size_bytes=reader.files[artifact.path].size_bytes,
            content_exposed=False,
        )
        for artifact in descriptor.artifacts
    )
    environment_views = tuple(
        EnvironmentFieldView(
            name=field.name.value,
            label=_ENVIRONMENT_LABELS[field.name],
            value=field.value,
            provenance=field.provenance.value,
            evidence_path=field.evidence_path,
        )
        for field in environment.fields
    )
    unavailable = tuple(
        UnavailableMetricView(
            metric=item.metric.value,
            reason=item.reason.value,
            capability_matrix=item.capability_matrix,
        )
        for item in analysis.reduction.measurements.unavailable
    )

    return RunDetail(
        summary=summary,
        hypothesis=resolved.experiment.hypothesis,
        verification=VerificationView(
            bundle_digest=report.bundle_digest,
            artifact_count=report.artifact_count,
            total_bytes=report.total_bytes,
            integrity_status="VALID",
            evidence_eligibility=descriptor.evidence_eligibility.value,
            environment_completeness=descriptor.environment_completeness.value,
            replayability=descriptor.replayability.value,
            verified_by_recalculation=True,
        ),
        execution=execution_view,
        measurements=metric_views,
        distributions=(
            _distribution("ttft_ns", "TTFT distribution", ttft_values),
            _distribution(
                "last_choices_event_span_ns",
                "Choices span distribution",
                choices_span_values,
            ),
        ),
        context=_context(resolved),
        environment=environment_views,
        artifacts=artifact_views,
        unavailable=unavailable,
        digests=DigestView(
            source_spec_digest=descriptor.digests.source_spec_digest,
            execution_fingerprint=descriptor.digests.execution_fingerprint,
            request_plan_digest=descriptor.digests.request_plan_digest,
            metric_definitions_digest=descriptor.digests.metric_definitions_digest,
            exitspec_contract_digest=descriptor.digests.exitspec_contract_digest,
        ),
        sensitivity=SensitivityView(
            prompt_content_in_request_plan=(
                descriptor.sensitivity.prompt_content_in_request_plan
            ),
            canonical_response_content_included=(
                descriptor.sensitivity.canonical_response_content_included
            ),
            native_response_content_present=(
                descriptor.sensitivity.native_response_content_present
            ),
            secrets_permitted=False,
        ),
        comparison_contract=ComparisonContractView(
            execution_fingerprint=descriptor.digests.execution_fingerprint,
            metric_definitions_digest=descriptor.digests.metric_definitions_digest,
            reducer_version=analysis.reduction.measurements.reducer_version,
            execution_mode=descriptor.execution_mode.value,
            workload_sha256=resolved.workload.sha256,
            requested_output_tokens=resolved.workload.requested_output_tokens,
            temperature=resolved.workload.temperature,
            seed=resolved.workload.seed,
            traffic_signature=_signature(resolved.traffic),
            measurement_signature=_signature(resolved.measurement),
        ),
    )
