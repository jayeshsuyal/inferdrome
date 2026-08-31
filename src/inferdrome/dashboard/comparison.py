"""Conservative pairwise comparison over verified dashboard projections."""

from decimal import Decimal
from typing import Literal

from inferdrome.dashboard.models import (
    ComparisonResponse,
    ContextChangeView,
    MetricDeltaView,
    MetricView,
    RunDetail,
)
from inferdrome.dashboard.projection import display_measurement
from inferdrome.domain.metrics import Unit

_FINGERPRINT_CONTEXT_KEYS = frozenset(
    {
        "execution.max_runtime_seconds",
        "execution.max_measured_requests",
        "target.engine",
        "target.api",
        "target.endpoint_identity",
        "target.model",
        "target.model_revision",
        "target.tokenizer_revision",
        "target.engine_version",
        "producer.name",
        "producer.version",
        "producer.adapter",
        "producer.adapter_version",
    }
)


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _signed_display(value: Decimal, unit: Unit) -> str:
    exact = _decimal_text(value)
    rendered = display_measurement(exact, unit)
    if value > 0:
        return f"+{rendered}"
    return rendered


def _hard_incompatibilities(
    baseline: RunDetail,
    candidate: RunDetail,
) -> list[str]:
    reasons: list[str] = []
    if baseline.summary.run_id == candidate.summary.run_id:
        reasons.append("A run cannot be compared with itself.")

    customer_eligible = "CUSTOMER_ELIGIBLE"
    if any(
        detail.summary.evidence_eligibility != customer_eligible
        or detail.verification.evidence_eligibility != customer_eligible
        for detail in (baseline, candidate)
    ):
        reasons.append(
            "Evidence-authoritative comparison requires CUSTOMER_ELIGIBLE "
            "evidence for both runs."
        )

    baseline_exitspec = baseline.digests.exitspec_contract_digest
    candidate_exitspec = candidate.digests.exitspec_contract_digest
    if baseline_exitspec is None or candidate_exitspec is None:
        reasons.append(
            "Evidence-authoritative comparison requires both runs to declare "
            "an ExitSpec contract identity."
        )
    elif baseline_exitspec != candidate_exitspec:
        reasons.append("ExitSpec contract identities differ.")

    baseline_contract = baseline.comparison_contract
    candidate_contract = candidate.comparison_contract
    checks = (
        (
            baseline_contract.metric_definitions_digest,
            candidate_contract.metric_definitions_digest,
            "Metric definition sets differ.",
        ),
        (
            baseline_contract.reducer_version,
            candidate_contract.reducer_version,
            "Reducer versions differ.",
        ),
        (
            baseline_contract.execution_mode,
            candidate_contract.execution_mode,
            "Execution modes differ.",
        ),
        (
            baseline_contract.workload_sha256,
            candidate_contract.workload_sha256,
            "Workload content differs.",
        ),
        (
            baseline_contract.requested_output_tokens,
            candidate_contract.requested_output_tokens,
            "Requested output-token limits differ.",
        ),
        (
            baseline_contract.temperature,
            candidate_contract.temperature,
            "Sampling temperatures differ.",
        ),
        (
            baseline_contract.seed,
            candidate_contract.seed,
            "Sampling seeds differ.",
        ),
        (
            baseline_contract.traffic_signature,
            candidate_contract.traffic_signature,
            "Traffic contracts differ.",
        ),
        (
            baseline_contract.measurement_signature,
            candidate_contract.measurement_signature,
            "Measurement semantics differ.",
        ),
    )
    reasons.extend(reason for left, right, reason in checks if left != right)

    baseline_semantics = {
        metric.key: (
            metric.definition_id,
            metric.unit,
            metric.population,
            metric.quantile_method,
            metric.rounding_policy,
        )
        for metric in baseline.measurements
    }
    candidate_semantics = {
        metric.key: (
            metric.definition_id,
            metric.unit,
            metric.population,
            metric.quantile_method,
            metric.rounding_policy,
        )
        for metric in candidate.measurements
    }
    if baseline_semantics != candidate_semantics:
        reasons.append("Available measurement sets or semantics differ.")
    return reasons


def _context_values(detail: RunDetail) -> dict[str, tuple[str, str | None, str]]:
    values: dict[str, tuple[str, str | None, str]] = {
        item.key: (item.label, item.value, item.group) for item in detail.context
    }
    values.update(
        {
            f"environment.{item.name}": (
                item.label,
                None if item.value is None else str(item.value),
                "environment",
            )
            for item in detail.environment
        }
    )
    values.update(
        {
            "evidence.eligibility": (
                "Evidence eligibility",
                detail.summary.evidence_eligibility,
                "evidence",
            ),
            "evidence.environment_completeness": (
                "Environment completeness",
                detail.summary.environment_completeness,
                "evidence",
            ),
            "evidence.replayability": (
                "Replayability",
                detail.summary.replayability,
                "evidence",
            ),
        }
    )
    return values


def _context_changes(
    baseline: RunDetail,
    candidate: RunDetail,
) -> tuple[ContextChangeView, ...]:
    baseline_values = _context_values(baseline)
    candidate_values = _context_values(candidate)
    changes: list[ContextChangeView] = []
    for key in sorted(set(baseline_values) | set(candidate_values)):
        baseline_item = baseline_values.get(key)
        candidate_item = candidate_values.get(key)
        baseline_value = baseline_item[1] if baseline_item is not None else None
        candidate_value = candidate_item[1] if candidate_item is not None else None
        if baseline_value == candidate_value:
            continue
        label = (
            baseline_item[0]
            if baseline_item is not None
            else candidate_item[0]
            if candidate_item is not None
            else key
        )
        group = (
            baseline_item[2]
            if baseline_item is not None
            else candidate_item[2]
            if candidate_item is not None
            else "context"
        )
        changes.append(
            ContextChangeView(
                key=key,
                label=label,
                baseline_value=baseline_value,
                candidate_value=candidate_value,
                group=group,
            )
        )
    return tuple(changes)


def _metric_deltas(
    baseline: tuple[MetricView, ...],
    candidate: tuple[MetricView, ...],
) -> tuple[MetricDeltaView, ...]:
    candidate_by_key = {metric.key: metric for metric in candidate}
    deltas: list[MetricDeltaView] = []
    for baseline_metric in baseline:
        candidate_metric = candidate_by_key[baseline_metric.key]
        baseline_value = Decimal(baseline_metric.value)
        candidate_value = Decimal(candidate_metric.value)
        absolute_delta = candidate_value - baseline_value
        percent_delta = (
            None
            if baseline_value == 0
            else _decimal_text((absolute_delta / baseline_value) * 100)
        )
        unit = Unit(baseline_metric.unit)
        deltas.append(
            MetricDeltaView(
                key=baseline_metric.key,
                metric=baseline_metric.metric,
                aggregation=baseline_metric.aggregation,
                label=baseline_metric.label,
                unit=baseline_metric.unit,
                baseline_value=baseline_metric.value,
                candidate_value=candidate_metric.value,
                absolute_delta=_decimal_text(absolute_delta),
                percent_delta=percent_delta,
                baseline_display_value=baseline_metric.display_value,
                candidate_display_value=candidate_metric.display_value,
                delta_display_value=_signed_display(absolute_delta, unit),
            )
        )
    return tuple(deltas)


def compare_runs(baseline: RunDetail, candidate: RunDetail) -> ComparisonResponse:
    """Compare two verified runs without inferring metric directionality."""

    reasons = _hard_incompatibilities(baseline, candidate)
    context_changes = _context_changes(baseline, candidate)
    fingerprint_changed = (
        baseline.comparison_contract.execution_fingerprint
        != candidate.comparison_contract.execution_fingerprint
    )
    declared_fingerprint_change = any(
        change.key in _FINGERPRINT_CONTEXT_KEYS for change in context_changes
    )
    if fingerprint_changed and not reasons and not declared_fingerprint_change:
        reasons.append(
            "Execution fingerprints differ without a declared context change."
        )
    if reasons:
        return ComparisonResponse(
            baseline_run_id=baseline.summary.run_id,
            candidate_run_id=candidate.summary.run_id,
            status="INCOMPARABLE",
            reasons=tuple(reasons),
            metric_deltas=(),
            context_changes=context_changes,
        )

    status: Literal["COMPARABLE", "COMPARABLE_WITH_CONTEXT_CHANGES"] = (
        "COMPARABLE_WITH_CONTEXT_CHANGES" if context_changes else "COMPARABLE"
    )
    return ComparisonResponse(
        baseline_run_id=baseline.summary.run_id,
        candidate_run_id=candidate.summary.run_id,
        status=status,
        reasons=(
            ("Verified measurement contracts align; contextual fields changed.",)
            if context_changes
            else ("Verified measurement and context contracts align.",)
        ),
        metric_deltas=_metric_deltas(baseline.measurements, candidate.measurements),
        context_changes=context_changes,
    )
