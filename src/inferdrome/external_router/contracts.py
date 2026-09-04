"""Strict, additive contracts for attached external-router evidence.

The first concrete profile deliberately accepts only an Inferdrome-owned
``llm-d-attached-v1`` envelope.  It records supplied facts from an external
router without claiming a native llm-d event API, importing llm-d, or changing
any router behaviour.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.external_router.canonical import (
    canonical_json_bytes,
    router_identity_digest,
    topology_identity_digest,
)

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
OpaqueId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]
Version = Annotated[
    str,
    Field(
        min_length=5,
        max_length=96,
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$",
    ),
]
MonotonicNs = Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]
FreshnessNs = Annotated[int, Field(ge=1, le=60_000_000_000)]
Epoch = Annotated[int, Field(ge=0, le=2_147_483_647)]

TelemetrySignal = Literal["HEALTH", "LOAD"]
TelemetryState = Literal["ADMISSIBLE", "STALE", "UNAVAILABLE"]
UnavailableTelemetryReason = Literal["MISSING", "UNSUPPORTED"]
FactState = Literal["OBSERVED", "UNAVAILABLE"]
UnavailableFactReason = Literal["MISSING", "UNSUPPORTED"]
EndpointOutcomeStatus = Literal["SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"]
EvidenceAdmissibility = Literal["ADMISSIBLE", "UNAVAILABLE"]
EvidenceUnavailableReason = Literal[
    "STALE_TELEMETRY",
    "UNAVAILABLE_TELEMETRY",
    "UNAVAILABLE_POLICY",
    "UNAVAILABLE_REASON",
    "UNAVAILABLE_SELECTION",
    "UNAVAILABLE_OUTCOME",
]

_SIGNALS: tuple[TelemetrySignal, ...] = ("HEALTH", "LOAD")
_UNAVAILABLE_REASON_ORDER: tuple[EvidenceUnavailableReason, ...] = (
    "STALE_TELEMETRY",
    "UNAVAILABLE_TELEMETRY",
    "UNAVAILABLE_POLICY",
    "UNAVAILABLE_REASON",
    "UNAVAILABLE_SELECTION",
    "UNAVAILABLE_OUTCOME",
)


class ExternalRouterModel(FrozenModel):
    """Strict immutable model whose errors do not carry sensitive payloads."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )


class AdapterIdentity(ExternalRouterModel):
    """Identity of this local, attached evidence adapter."""

    adapter_id: Literal["inferdrome.external-router-adapter-v1"]
    adapter_version: Literal["1.0.0"]


class RouterIdentity(ExternalRouterModel):
    """Immutable external-router facts; raw config bytes are never retained."""

    profile_id: Literal["llm-d-attached-v1"]
    profile_version: Literal["1.0.0"]
    router_implementation: Literal["llm-d"]
    router_version: Version
    config_sha256: Digest
    identity_sha256: Digest

    @model_validator(mode="after")
    def _identity_matches_facts(self) -> RouterIdentity:
        projection = self.model_dump(mode="json", exclude={"identity_sha256"})
        if self.identity_sha256 != router_identity_digest(projection):
            raise ValueError("router identity digest does not bind router facts")
        return self


class EndpointIdentity(ExternalRouterModel):
    """A logical endpoint identity with no origin, address, or credential."""

    endpoint_id: OpaqueId
    endpoint_identity_sha256: Digest


class TopologyReceipt(ExternalRouterModel):
    """The bounded two-endpoint logical topology supplied by the operator."""

    topology_id: OpaqueId
    endpoints: tuple[EndpointIdentity, EndpointIdentity]
    identity_sha256: Digest

    @model_validator(mode="after")
    def _topology_is_unique_and_bound(self) -> TopologyReceipt:
        endpoint_ids = tuple(endpoint.endpoint_id for endpoint in self.endpoints)
        endpoint_digests = tuple(
            endpoint.endpoint_identity_sha256 for endpoint in self.endpoints
        )
        if len(set(endpoint_ids)) != len(endpoint_ids):
            raise ValueError("topology endpoint identifiers are not unique")
        if len(set(endpoint_digests)) != len(endpoint_digests):
            raise ValueError("topology endpoint identities are not unique")
        projection = self.model_dump(mode="json", exclude={"identity_sha256"})
        if self.identity_sha256 != topology_identity_digest(projection):
            raise ValueError("topology identity digest does not bind topology facts")
        return self


class RequestBinding(ExternalRouterModel):
    """Correlation facts without user input, prompt, or output retention."""

    request_id: OpaqueId
    router_request_id: OpaqueId
    correlation_id: OpaqueId
    router_identity_sha256: Digest
    topology_identity_sha256: Digest


class CandidateEndpoint(ExternalRouterModel):
    """An endpoint presented to the external router for this request."""

    endpoint_id: OpaqueId
    endpoint_identity_sha256: Digest


class TelemetryObservation(ExternalRouterModel):
    """One externally sampled telemetry state bound to an endpoint and epoch."""

    observation_id: OpaqueId
    observer_id: OpaqueId
    endpoint_id: OpaqueId
    endpoint_identity_sha256: Digest
    signal: TelemetrySignal
    state: TelemetryState
    clock_domain_id: OpaqueId
    epoch: Epoch
    sampled_at_monotonic_ns: MonotonicNs | None = None
    age_ns: MonotonicNs | None = None
    freshness_bound_ns: FreshnessNs | None = None
    unavailable_reason: UnavailableTelemetryReason | None = None

    @model_validator(mode="after")
    def _state_has_only_admissible_facts(self) -> TelemetryObservation:
        has_timing = (
            self.sampled_at_monotonic_ns is not None
            and self.age_ns is not None
            and self.freshness_bound_ns is not None
        )
        if self.state == "UNAVAILABLE":
            has_any_timing = any(
                value is not None
                for value in (
                    self.sampled_at_monotonic_ns,
                    self.age_ns,
                    self.freshness_bound_ns,
                )
            )
            if has_any_timing or self.unavailable_reason is None:
                raise ValueError(
                    "unavailable telemetry must omit timing and name cause"
                )
            return self
        if not has_timing or self.unavailable_reason is not None:
            raise ValueError("observed telemetry timing is incomplete or ambiguous")
        assert self.age_ns is not None
        assert self.freshness_bound_ns is not None
        if self.state == "ADMISSIBLE" and self.age_ns > self.freshness_bound_ns:
            raise ValueError("admissible telemetry exceeds its freshness bound")
        if self.state == "STALE" and self.age_ns <= self.freshness_bound_ns:
            raise ValueError("stale telemetry does not exceed its freshness bound")
        return self


class ReportedTextFact(ExternalRouterModel):
    """A router-reported policy or reason, never an Inferdrome default."""

    state: FactState
    value: OpaqueId | None = None
    unavailable_reason: UnavailableFactReason | None = None

    @model_validator(mode="after")
    def _reported_or_explicitly_unavailable(self) -> ReportedTextFact:
        if self.state == "OBSERVED":
            if self.value is None or self.unavailable_reason is not None:
                raise ValueError("observed router fact is incomplete")
        elif self.value is not None or self.unavailable_reason is None:
            raise ValueError("unavailable router fact must not invent a value")
        return self


class ReportedSelection(ExternalRouterModel):
    """A router-reported selection, separate from evidence admissibility."""

    state: FactState
    endpoint_id: OpaqueId | None = None
    unavailable_reason: UnavailableFactReason | None = None

    @model_validator(mode="after")
    def _reported_or_explicitly_unavailable(self) -> ReportedSelection:
        if self.state == "OBSERVED":
            if self.endpoint_id is None or self.unavailable_reason is not None:
                raise ValueError("observed router selection is incomplete")
        elif self.endpoint_id is not None or self.unavailable_reason is None:
            raise ValueError("unavailable router selection must not invent endpoint")
        return self


class RouterDecision(ExternalRouterModel):
    """One external router decision bound to its external correlation."""

    decision_id: OpaqueId
    correlation_id: OpaqueId
    candidate_endpoint_ids: tuple[OpaqueId, ...]
    selection: ReportedSelection
    policy: ReportedTextFact
    reason: ReportedTextFact

    @model_validator(mode="after")
    def _candidate_inventory_is_bounded(self) -> RouterDecision:
        if not 1 <= len(self.candidate_endpoint_ids) <= 2:
            raise ValueError("router candidate inventory is outside the profile bound")
        if len(set(self.candidate_endpoint_ids)) != len(self.candidate_endpoint_ids):
            raise ValueError("router candidate identifiers are not unique")
        return self


class EndpointOutcome(ExternalRouterModel):
    """One terminal endpoint outcome bound to the external correlation ID."""

    outcome_id: OpaqueId
    correlation_id: OpaqueId
    state: FactState
    endpoint_id: OpaqueId | None = None
    status: EndpointOutcomeStatus | None = None
    unavailable_reason: UnavailableFactReason | None = None

    @model_validator(mode="after")
    def _outcome_has_no_implicit_endpoint(self) -> EndpointOutcome:
        if self.state == "OBSERVED":
            if (
                self.endpoint_id is None
                or self.status is None
                or self.unavailable_reason is not None
            ):
                raise ValueError("observed endpoint outcome is incomplete")
        elif (
            self.endpoint_id is not None
            or self.status is not None
            or self.unavailable_reason is None
        ):
            raise ValueError("unavailable endpoint outcome must not invent a terminal")
        return self


class _AttachedRecord(ExternalRouterModel):
    """Shared operator-supplied facts before or after adapter assessment."""

    adapter: AdapterIdentity
    router: RouterIdentity
    topology: TopologyReceipt
    request: RequestBinding
    decision_at_monotonic_ns: MonotonicNs
    candidates: tuple[CandidateEndpoint, ...]
    observations: tuple[TelemetryObservation, ...]
    decision: RouterDecision
    outcome: EndpointOutcome

    @model_validator(mode="after")
    def _bind_all_external_facts(self) -> _AttachedRecord:
        if self.request.router_identity_sha256 != self.router.identity_sha256:
            raise ValueError("request router identity binding disagrees")
        if self.request.topology_identity_sha256 != self.topology.identity_sha256:
            raise ValueError("request topology identity binding disagrees")

        topology_by_id = {
            endpoint.endpoint_id: endpoint.endpoint_identity_sha256
            for endpoint in self.topology.endpoints
        }
        candidate_ids = tuple(candidate.endpoint_id for candidate in self.candidates)
        if not 1 <= len(candidate_ids) <= len(self.topology.endpoints):
            raise ValueError("candidate inventory is outside the topology bound")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("candidate endpoint identifiers are not unique")
        for candidate in self.candidates:
            if (
                topology_by_id.get(candidate.endpoint_id)
                != candidate.endpoint_identity_sha256
            ):
                raise ValueError("candidate endpoint is not bound to topology")

        if self.decision.candidate_endpoint_ids != candidate_ids:
            raise ValueError("decision candidate inventory disagrees with receipt")
        if self.decision.correlation_id != self.request.correlation_id:
            raise ValueError("router decision correlation binding disagrees")
        if (
            self.decision.selection.state == "OBSERVED"
            and self.decision.selection.endpoint_id not in candidate_ids
        ):
            raise ValueError("selected endpoint is not a declared candidate")

        expected_observations = {
            (candidate.endpoint_id, signal)
            for candidate in self.candidates
            for signal in _SIGNALS
        }
        observation_keys = tuple(
            (observation.endpoint_id, observation.signal)
            for observation in self.observations
        )
        if set(observation_keys) != expected_observations or len(
            observation_keys
        ) != len(expected_observations):
            raise ValueError("telemetry inventory is incomplete or duplicated")
        observation_ids = {
            observation.observation_id for observation in self.observations
        }
        if len(observation_ids) != len(self.observations):
            raise ValueError("telemetry observation identifiers are not unique")
        clock_domains = {
            observation.clock_domain_id for observation in self.observations
        }
        epochs = {observation.epoch for observation in self.observations}
        if len(clock_domains) != 1 or len(epochs) != 1:
            raise ValueError("telemetry clock domain or epoch disagrees")
        for observation in self.observations:
            if (
                topology_by_id.get(observation.endpoint_id)
                != observation.endpoint_identity_sha256
            ):
                raise ValueError("telemetry endpoint is not bound to topology")
            if observation.endpoint_id not in candidate_ids:
                raise ValueError("telemetry endpoint is not a declared candidate")
            if observation.state != "UNAVAILABLE":
                assert observation.sampled_at_monotonic_ns is not None
                assert observation.age_ns is not None
                if observation.sampled_at_monotonic_ns > self.decision_at_monotonic_ns:
                    raise ValueError("telemetry sample is after routing decision")
                if (
                    self.decision_at_monotonic_ns - observation.sampled_at_monotonic_ns
                    != observation.age_ns
                ):
                    raise ValueError("telemetry age does not bind decision timestamp")

        if self.outcome.correlation_id != self.request.correlation_id:
            raise ValueError("endpoint outcome correlation binding disagrees")
        if self.outcome.state == "OBSERVED":
            if self.decision.selection.state != "OBSERVED":
                raise ValueError("observed outcome has no observed selection")
            if self.outcome.endpoint_id != self.decision.selection.endpoint_id:
                raise ValueError("endpoint outcome does not bind selected endpoint")
        return self

    def computed_unavailable_reasons(self) -> tuple[EvidenceUnavailableReason, ...]:
        """Return the fixed, non-verdicting evidence-admissibility assessment."""

        reasons: set[EvidenceUnavailableReason] = set()
        if any(observation.state == "STALE" for observation in self.observations):
            reasons.add("STALE_TELEMETRY")
        if any(
            observation.state == "UNAVAILABLE" for observation in self.observations
        ):
            reasons.add("UNAVAILABLE_TELEMETRY")
        if self.decision.policy.state == "UNAVAILABLE":
            reasons.add("UNAVAILABLE_POLICY")
        if self.decision.reason.state == "UNAVAILABLE":
            reasons.add("UNAVAILABLE_REASON")
        if self.decision.selection.state == "UNAVAILABLE":
            reasons.add("UNAVAILABLE_SELECTION")
        if self.outcome.state == "UNAVAILABLE":
            reasons.add("UNAVAILABLE_OUTCOME")
        return tuple(
            reason for reason in _UNAVAILABLE_REASON_ORDER if reason in reasons
        )


class LlmdAttachedRecord(_AttachedRecord):
    """The local-fixture-only input envelope for the first llm-d profile."""

    schema_version: Literal["inferdrome.llm-d-attached-record.v1"]


class ExternalRouterEvidence(_AttachedRecord):
    """Canonical adapter output with a deterministic evidence admissibility state."""

    schema_version: Literal["inferdrome.external-router-evidence.v1"]
    evidence_admissibility: EvidenceAdmissibility
    evidence_unavailable_reasons: tuple[EvidenceUnavailableReason, ...]

    @model_validator(mode="after")
    def _assessment_is_computed_not_asserted(self) -> ExternalRouterEvidence:
        expected_reasons = self.computed_unavailable_reasons()
        expected_admissibility: EvidenceAdmissibility = (
            "ADMISSIBLE" if not expected_reasons else "UNAVAILABLE"
        )
        if self.evidence_admissibility != expected_admissibility:
            raise ValueError("evidence admissibility is not supported by observations")
        if self.evidence_unavailable_reasons != expected_reasons:
            raise ValueError("evidence unavailable reasons are not canonical")
        return self


def external_router_evidence_bytes(evidence: ExternalRouterEvidence) -> bytes:
    """Return the one canonical form accepted by the standalone verifier."""

    return canonical_json_bytes(evidence.model_dump(mode="json"))
