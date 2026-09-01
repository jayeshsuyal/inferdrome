"""Additive v0.2 read-only GCP quote, capacity, cost, and cleanup guards.

The frozen v1 quote and capacity records remain their own inputs.  This module
only canonicalizes and binds them into a stricter local-only v0.2 guard; it has
no provider transport, credential, SDK, or mutation capability.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, Self

from pydantic import Field, ValidationError, model_validator

from inferdrome.deployment.gcp import GcpTimestamp
from inferdrome.deployment.gcp_lifecycle import (
    GcpExecutionError,
    GcpExecutionModel,
    GcpExecutionPreflight,
    GcpLeaseRecord,
    GcpQuoteComponent,
    _parse_timestamp,
    _preflight_json,
    _timestamp,
    gcp_capacity_digest,
    gcp_cost_quote_digest,
    gcp_execution_request_digest,
)
from inferdrome.deployment.spec import CostCeiling
from inferdrome.domain.digests import DigestDomain, canonical_json_bytes, digest_bytes
from inferdrome.domain.ids import Sha256Digest

GCP_READ_ONLY_QUOTE_BASIS_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-read-only-quote-basis.v2"
)
GCP_COST_CLEANUP_GUARD_SCHEMA_VERSION: Final = (
    "inferdrome.gcp-cost-cleanup-guard.v2"
)
GCP_READ_ONLY_QUOTE_BASIS_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-read-only-quote-basis:v2"
)
GCP_COST_CLEANUP_GUARD_SCHEMA_ID: Final = (
    "urn:inferdrome:gcp-cost-cleanup-guard:v2"
)


class GcpCostGuardError(GcpExecutionError):
    """A bounded, non-secret cost/capability protection failure."""

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,47}", code) is None:
            code = "COST_GUARD_ERROR"
        self.code = code
        super().__init__(code)


def _model_value(model: GcpExecutionModel) -> dict[str, Any]:
    value = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    if not isinstance(value, dict):
        raise TypeError("GCP cost guard contract must serialize as an object")
    return value


def gcp_hard_usd_ceiling_microusd(ceiling: CostCeiling) -> int:
    """Return the exact fixed-point ceiling without a floating-point path."""

    if (
        ceiling.currency != "USD"
        or ceiling.hard_limit is not True
        or ceiling.estimate_basis != "controller_estimate"
    ):
        raise GcpCostGuardError("COST_CEILING_NOT_HARD_USD")
    try:
        value = (Decimal(ceiling.max_cost_usd) * Decimal(1_000_000)).to_integral_exact()
        result = int(value)
    except (InvalidOperation, ValueError):
        raise GcpCostGuardError("COST_CEILING_FIXED_POINT_INVALID") from None
    if result < 0 or result > 10**12:
        raise GcpCostGuardError("COST_CEILING_OUT_OF_BOUNDS")
    return result


class GcpQuoteRateComponent(GcpExecutionModel):
    """A rational USD rate whose full-period ceiling is exact and inspectable."""

    component_id: Literal["compute", "gpu", "boot_disk", "network"]
    rate_numerator_microusd: int = Field(strict=True, ge=0, le=10**12)
    rate_denominator_seconds: int = Field(strict=True, ge=1, le=86_400)
    maximum_microusd: int = Field(strict=True, ge=0, le=10**12)


class GcpReadOnlyQuoteBasisPayload(GcpExecutionModel):
    """The exact rate/currency basis derived solely from a v1 quote input."""

    schema_version: Literal["inferdrome.gcp-read-only-quote-basis.v2"]
    basis_kind: Literal["read_only_rational_rate_basis"]
    quote_digest: Sha256Digest
    plan_id: Sha256Digest
    controller_id: str = Field(pattern=r"^ctl-[a-z0-9]{8,24}$")
    request_digest: Sha256Digest
    currency: Literal["USD"]
    rate_unit: Literal["microusd_per_second_rational"]
    billable_duration_seconds: int = Field(strict=True, ge=1, le=86_400)
    component_rates: tuple[GcpQuoteRateComponent, ...] = Field(
        min_length=4, max_length=4
    )
    safety_margin_microusd: int = Field(strict=True, ge=0, le=10**12)
    worst_case_microusd: int = Field(strict=True, ge=0, le=10**12)
    quote_authority: Literal["operator_supplied_read_only_quote"]
    pricing_status: Literal["operator_supplied_estimate_not_invoice_truth"]
    invoice_truth: Literal["unavailable_external_provider_invoice"]
    issued_at: GcpTimestamp
    valid_until: GcpTimestamp

    @model_validator(mode="after")
    def validate_basis(self) -> Self:
        component_ids = [component.component_id for component in self.component_rates]
        if set(component_ids) != {"compute", "gpu", "boot_disk", "network"}:
            raise ValueError("rate basis must cover every billable component")
        if len(set(component_ids)) != len(component_ids):
            raise ValueError("rate basis components must be unique")
        maximum = 0
        for component in self.component_rates:
            if component.rate_denominator_seconds != self.billable_duration_seconds:
                raise ValueError("rate denominator must equal the billable duration")
            expected = (
                component.rate_numerator_microusd
                * self.billable_duration_seconds
                + component.rate_denominator_seconds
                - 1
            ) // component.rate_denominator_seconds
            if component.maximum_microusd != expected:
                raise ValueError("rate component maximum is inconsistent")
            maximum += component.maximum_microusd
        if maximum + self.safety_margin_microusd != self.worst_case_microusd:
            raise ValueError("rate basis worst case is inconsistent")
        if _parse_timestamp(self.valid_until) <= _parse_timestamp(self.issued_at):
            raise ValueError("rate basis validity must be positive")
        return self


class GcpReadOnlyQuoteBasis(GcpReadOnlyQuoteBasisPayload):
    """An immutable rate basis with a domain-separated identity."""

    basis_id: Sha256Digest

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.basis_id != gcp_read_only_quote_basis_id(self):
            raise ValueError("rate basis identity does not match its payload")
        return self


class GcpCostCleanupGuardPayload(GcpExecutionModel):
    """Read-only quote/capability guard with no launch authority."""

    schema_version: Literal["inferdrome.gcp-cost-cleanup-guard.v2"]
    guard_kind: Literal["read_only_cost_capacity_cleanup_guard"]
    provider: Literal["gcp-compute-engine"]
    plan_id: Sha256Digest
    controller_id: str = Field(pattern=r"^ctl-[a-z0-9]{8,24}$")
    request_digest: Sha256Digest
    quote_digest: Sha256Digest
    capacity_digest: Sha256Digest
    rate_basis_digest: Sha256Digest
    currency: Literal["USD"]
    max_runtime_seconds: int = Field(strict=True, ge=1, le=86_400)
    controller_deadline_at: GcpTimestamp
    watchdog_deadline_at: GcpTimestamp
    hard_cost_ceiling: CostCeiling
    hard_ceiling_microusd: int = Field(strict=True, ge=0, le=10**12)
    estimated_max_microusd: int = Field(strict=True, ge=0, le=10**12)
    max_cleanup_attempts: int = Field(strict=True, ge=1, le=3)
    cleanup_timeout_seconds: int = Field(strict=True, ge=1, le=3_600)
    orphan_policy: Literal["block_next_run"]
    orphan_discovery_scope: Literal[
        "exact_label_scoped_inventory"
    ]
    cleanup_confirmation_scope: Literal[
        "exact_instance_and_boot_disk_absence"
    ]
    quote_authority: Literal["read_only_quote_only"]
    capacity_authority: Literal["read_only_capacity_only"]
    provider_billing_enforcement: Literal[False]
    invoice_truth: Literal["unavailable_external_provider_invoice"]

    @model_validator(mode="after")
    def validate_guard(self) -> Self:
        ceiling = gcp_hard_usd_ceiling_microusd(self.hard_cost_ceiling)
        if self.hard_ceiling_microusd != ceiling:
            raise ValueError("guard hard ceiling fixed point is inconsistent")
        if self.estimated_max_microusd > self.hard_ceiling_microusd:
            raise ValueError("guard estimate exceeds its hard ceiling")
        if _parse_timestamp(self.watchdog_deadline_at) < _parse_timestamp(
            self.controller_deadline_at
        ):
            raise ValueError("guard watchdog deadline precedes controller deadline")
        return self


class GcpCostCleanupGuard(GcpCostCleanupGuardPayload):
    """A content-addressed local-only cost/cleanup guard, not an authorization proof."""

    guard_id: Sha256Digest

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.guard_id != gcp_cost_cleanup_guard_id(self):
            raise ValueError("cost cleanup guard identity does not match its payload")
        return self


def canonical_gcp_read_only_quote_basis_payload_bytes(
    basis: GcpReadOnlyQuoteBasis | GcpReadOnlyQuoteBasisPayload,
) -> bytes:
    value = _model_value(basis)
    value.pop("basis_id", None)
    return canonical_json_bytes(value)


def gcp_read_only_quote_basis_id(
    basis: GcpReadOnlyQuoteBasis | GcpReadOnlyQuoteBasisPayload,
) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_READ_ONLY_QUOTE_BASIS,
        canonical_gcp_read_only_quote_basis_payload_bytes(basis),
    )


def canonical_gcp_read_only_quote_basis_bytes(basis: GcpReadOnlyQuoteBasis) -> bytes:
    return canonical_json_bytes(_model_value(basis))


def gcp_read_only_quote_basis_digest(basis: GcpReadOnlyQuoteBasis) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_READ_ONLY_QUOTE_BASIS,
        canonical_gcp_read_only_quote_basis_bytes(basis),
    )


def canonical_gcp_cost_cleanup_guard_payload_bytes(
    guard: GcpCostCleanupGuard | GcpCostCleanupGuardPayload,
) -> bytes:
    value = _model_value(guard)
    value.pop("guard_id", None)
    return canonical_json_bytes(value)


def gcp_cost_cleanup_guard_id(
    guard: GcpCostCleanupGuard | GcpCostCleanupGuardPayload,
) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_COST_CLEANUP_GUARD,
        canonical_gcp_cost_cleanup_guard_payload_bytes(guard),
    )


def canonical_gcp_cost_cleanup_guard_bytes(guard: GcpCostCleanupGuard) -> bytes:
    return canonical_json_bytes(_model_value(guard))


def gcp_cost_cleanup_guard_digest(guard: GcpCostCleanupGuard) -> Sha256Digest:
    return digest_bytes(
        DigestDomain.GCP_COST_CLEANUP_GUARD,
        canonical_gcp_cost_cleanup_guard_bytes(guard),
    )


def parse_gcp_read_only_quote_basis_json(payload: str | bytes) -> GcpReadOnlyQuoteBasis:
    try:
        _preflight_json(payload, kind="GCP read-only quote basis")
        return GcpReadOnlyQuoteBasis.model_validate_json(payload)
    except (ValidationError, ValueError):
        raise GcpCostGuardError("RATE_BASIS_INVALID") from None


def parse_gcp_cost_cleanup_guard_json(payload: str | bytes) -> GcpCostCleanupGuard:
    try:
        _preflight_json(payload, kind="GCP cost cleanup guard")
        return GcpCostCleanupGuard.model_validate_json(payload)
    except (ValidationError, ValueError):
        raise GcpCostGuardError("COST_GUARD_INVALID") from None


def _strict_basis(basis: GcpReadOnlyQuoteBasis) -> GcpReadOnlyQuoteBasis:
    raw = canonical_gcp_read_only_quote_basis_bytes(basis)
    try:
        parsed = GcpReadOnlyQuoteBasis.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise GcpCostGuardError("RATE_BASIS_INVALID") from None
    if canonical_gcp_read_only_quote_basis_bytes(parsed) != raw:
        raise GcpCostGuardError("RATE_BASIS_NONCANONICAL")
    return parsed


def _strict_guard(guard: GcpCostCleanupGuard) -> GcpCostCleanupGuard:
    raw = canonical_gcp_cost_cleanup_guard_bytes(guard)
    try:
        parsed = GcpCostCleanupGuard.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise GcpCostGuardError("COST_GUARD_INVALID") from None
    if canonical_gcp_cost_cleanup_guard_bytes(parsed) != raw:
        raise GcpCostGuardError("COST_GUARD_NONCANONICAL")
    return parsed


def _expected_rates(
    quote_components: tuple[GcpQuoteComponent, ...], *, billable_duration_seconds: int
) -> tuple[GcpQuoteRateComponent, ...]:
    return tuple(
        GcpQuoteRateComponent(
            component_id=component.component_id,
            rate_numerator_microusd=component.maximum_microusd,
            rate_denominator_seconds=billable_duration_seconds,
            maximum_microusd=component.maximum_microusd,
        )
        for component in sorted(
            quote_components, key=lambda component: component.component_id
        )
    )


def issue_gcp_read_only_quote_basis(
    *, preflight: GcpExecutionPreflight
) -> GcpReadOnlyQuoteBasis:
    """Bind an exact rational USD rate basis from a read-only quote input."""

    quote = preflight.quote
    payload = GcpReadOnlyQuoteBasisPayload(
        schema_version=GCP_READ_ONLY_QUOTE_BASIS_SCHEMA_VERSION,
        basis_kind="read_only_rational_rate_basis",
        quote_digest=gcp_cost_quote_digest(quote),
        plan_id=quote.plan_id,
        controller_id=quote.controller_id,
        request_digest=quote.request_digest,
        currency=quote.currency,
        rate_unit="microusd_per_second_rational",
        billable_duration_seconds=quote.billable_duration_seconds,
        component_rates=_expected_rates(
            quote.components,
            billable_duration_seconds=quote.billable_duration_seconds,
        ),
        safety_margin_microusd=quote.safety_margin_microusd,
        worst_case_microusd=quote.worst_case_microusd,
        quote_authority="operator_supplied_read_only_quote",
        pricing_status=quote.pricing_status,
        invoice_truth=quote.invoice_truth,
        issued_at=quote.issued_at,
        valid_until=quote.valid_until,
    )
    value = _model_value(payload)
    value["basis_id"] = gcp_read_only_quote_basis_id(payload)
    return GcpReadOnlyQuoteBasis.model_validate_json(
        canonical_json_bytes(value)
    )


def validate_gcp_read_only_quote_basis(
    basis: GcpReadOnlyQuoteBasis,
    *,
    preflight: GcpExecutionPreflight,
    now: datetime,
    enforce_freshness: bool = True,
) -> GcpReadOnlyQuoteBasis:
    """Fail closed if a rate basis is not exact to the current quote input."""

    basis = _strict_basis(basis)
    quote = preflight.quote
    expected = {
        "quote_digest": gcp_cost_quote_digest(quote),
        "plan_id": quote.plan_id,
        "controller_id": quote.controller_id,
        "request_digest": quote.request_digest,
        "currency": quote.currency,
        "billable_duration_seconds": quote.billable_duration_seconds,
        "component_rates": _expected_rates(
            quote.components,
            billable_duration_seconds=quote.billable_duration_seconds,
        ),
        "safety_margin_microusd": quote.safety_margin_microusd,
        "worst_case_microusd": quote.worst_case_microusd,
        "issued_at": quote.issued_at,
        "valid_until": quote.valid_until,
    }
    if any(getattr(basis, key) != value for key, value in expected.items()):
        raise GcpCostGuardError("RATE_BASIS_BINDING_MISMATCH")
    if enforce_freshness:
        now_value = _parse_timestamp(_timestamp(now))
        if not (
            _parse_timestamp(basis.issued_at)
            <= now_value
            <= _parse_timestamp(basis.valid_until)
        ):
            raise GcpCostGuardError("RATE_BASIS_EXPIRED")
    return basis


def gcp_watchdog_deadline_at(preflight: GcpExecutionPreflight) -> GcpTimestamp:
    """Derive the durable watchdog horizon from the immutable quote window.

    This is a frozen-v1 preflight observation used to bind the read-only quote
    and cleanup tail.  It does not extend, replace, or dynamically govern the
    v2 provider runtime after activation: the versioned activation contract
    derives that bounded runtime and its watchdog-cleanup horizon separately.
    """

    try:
        cleanup_tail_seconds = (
            preflight.quote.billable_duration_seconds
            - preflight.request.provider_max_runtime_seconds
        )
        if cleanup_tail_seconds < 0:
            raise ValueError("quote duration is shorter than provider runtime")
        deadline = _parse_timestamp(preflight.arm.expires_at) + timedelta(
            seconds=cleanup_tail_seconds
        )
        return _timestamp(deadline)
    except (ValueError, TypeError):
        raise GcpCostGuardError("WATCHDOG_DEADLINE_INVALID") from None


def issue_gcp_cost_cleanup_guard(
    *,
    preflight: GcpExecutionPreflight,
    quote_basis: GcpReadOnlyQuoteBasis,
    max_cleanup_attempts: int,
    cleanup_timeout_seconds: int,
    now: datetime,
) -> GcpCostCleanupGuard:
    """Create a local guard from read-only inputs, never launch authority."""

    quote_basis = validate_gcp_read_only_quote_basis(
        quote_basis, preflight=preflight, now=now
    )
    request = preflight.request
    quote = preflight.quote
    capacity = preflight.capacity
    payload = GcpCostCleanupGuardPayload(
        schema_version=GCP_COST_CLEANUP_GUARD_SCHEMA_VERSION,
        guard_kind="read_only_cost_capacity_cleanup_guard",
        provider="gcp-compute-engine",
        plan_id=preflight.arm.plan_id,
        controller_id=preflight.arm.controller_id,
        request_digest=gcp_execution_request_digest(request),
        quote_digest=gcp_cost_quote_digest(quote),
        capacity_digest=gcp_capacity_digest(capacity),
        rate_basis_digest=gcp_read_only_quote_basis_digest(quote_basis),
        currency="USD",
        max_runtime_seconds=request.provider_max_runtime_seconds,
        controller_deadline_at=preflight.arm.expires_at,
        watchdog_deadline_at=gcp_watchdog_deadline_at(preflight),
        hard_cost_ceiling=preflight.arm.cost_ceiling,
        hard_ceiling_microusd=gcp_hard_usd_ceiling_microusd(preflight.arm.cost_ceiling),
        estimated_max_microusd=quote.worst_case_microusd,
        max_cleanup_attempts=max_cleanup_attempts,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
        orphan_policy="block_next_run",
        orphan_discovery_scope="exact_label_scoped_inventory",
        cleanup_confirmation_scope="exact_instance_and_boot_disk_absence",
        quote_authority="read_only_quote_only",
        capacity_authority="read_only_capacity_only",
        provider_billing_enforcement=False,
        invoice_truth="unavailable_external_provider_invoice",
    )
    value = _model_value(payload)
    value["guard_id"] = gcp_cost_cleanup_guard_id(payload)
    return GcpCostCleanupGuard.model_validate_json(
        canonical_json_bytes(value)
    )


def validate_gcp_cost_cleanup_guard(
    guard: GcpCostCleanupGuard,
    *,
    preflight: GcpExecutionPreflight,
    quote_basis: GcpReadOnlyQuoteBasis,
    now: datetime,
    record: GcpLeaseRecord | None = None,
    enforce_freshness: bool = True,
    enforce_controller_deadline: bool = True,
) -> GcpCostCleanupGuard:
    """Validate that pure pricing/capacity input cannot authorize a launch."""

    guard = _strict_guard(guard)
    quote_basis = validate_gcp_read_only_quote_basis(
        quote_basis,
        preflight=preflight,
        now=now,
        enforce_freshness=enforce_freshness,
    )
    request = preflight.request
    quote = preflight.quote
    capacity = preflight.capacity
    expected = {
        "plan_id": preflight.arm.plan_id,
        "controller_id": preflight.arm.controller_id,
        "request_digest": gcp_execution_request_digest(request),
        "quote_digest": gcp_cost_quote_digest(quote),
        "capacity_digest": gcp_capacity_digest(capacity),
        "rate_basis_digest": gcp_read_only_quote_basis_digest(quote_basis),
        "max_runtime_seconds": request.provider_max_runtime_seconds,
        "controller_deadline_at": preflight.arm.expires_at,
        "watchdog_deadline_at": gcp_watchdog_deadline_at(preflight),
        "hard_cost_ceiling": preflight.arm.cost_ceiling,
        "hard_ceiling_microusd": gcp_hard_usd_ceiling_microusd(
            preflight.arm.cost_ceiling
        ),
        "estimated_max_microusd": quote.worst_case_microusd,
    }
    if any(getattr(guard, key) != value for key, value in expected.items()):
        raise GcpCostGuardError("COST_GUARD_BINDING_MISMATCH")
    if enforce_freshness and enforce_controller_deadline:
        now_value = _parse_timestamp(_timestamp(now))
        if now_value >= _parse_timestamp(guard.controller_deadline_at):
            raise GcpCostGuardError("COST_GUARD_DEADLINE_EXPIRED")
    if record is not None and (
        record.plan_id != guard.plan_id
        or record.request_digest != guard.request_digest
        or record.quote_digest != guard.quote_digest
        or record.capacity_digest != guard.capacity_digest
        or record.estimated_max_microusd != guard.estimated_max_microusd
        or record.max_cleanup_attempts != guard.max_cleanup_attempts
        or record.cleanup_timeout_seconds != guard.cleanup_timeout_seconds
    ):
        raise GcpCostGuardError("COST_GUARD_LEASE_MISMATCH")
    return guard


def gcp_cost_guard_contract_schemas() -> dict[str, dict[str, Any]]:
    """Return additive v0.2 schemas for read-only cost guard inputs."""

    models: tuple[tuple[str, type[GcpExecutionModel], str], ...] = (
        (
            "gcp-read-only-quote-basis.schema.json",
            GcpReadOnlyQuoteBasis,
            GCP_READ_ONLY_QUOTE_BASIS_SCHEMA_ID,
        ),
        (
            "gcp-cost-cleanup-guard.schema.json",
            GcpCostCleanupGuard,
            GCP_COST_CLEANUP_GUARD_SCHEMA_ID,
        ),
    )
    output: dict[str, dict[str, Any]] = {}
    for filename, model, schema_id in models:
        schema = model.model_json_schema()
        schema["$id"] = schema_id
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        output[filename] = schema
    return output
