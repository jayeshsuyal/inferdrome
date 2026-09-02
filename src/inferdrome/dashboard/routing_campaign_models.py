"""Explicit read-only contracts for verified routing-campaign projections."""

from typing import Literal

from inferdrome.dashboard.models import PageView
from inferdrome.domain.base import FrozenModel

RoutingCampaignId = Literal["routing-campaign-v1"]
RoutingTerminalStatus = Literal[
    "SUCCEEDED",
    "TIMED_OUT",
    "FAILED",
    "CANCELLED",
    "NO_SAFE_ROUTE",
]
RoutingAdmissibility = Literal["ADMISSIBLE", "INADMISSIBLE"]
RoutingEndpointId = Literal["endpoint-a", "endpoint-b"]


class RoutingCampaignSummary(FrozenModel):
    """Verified campaign facts suitable for the bounded campaign index."""

    campaign_id: RoutingCampaignId
    retained_digest: str
    execution_mode: Literal["SYNTHETIC_CPU_ONLY"]
    trial_count: int
    planned_request_count: int
    policy_ids: tuple[str, ...]
    verified_by_replay: Literal[True] = True


class RoutingFaultTimelineView(FrozenModel):
    """The declared stale-load/fresh-health fault boundary in virtual time."""

    load_collection_paused_at_ms: int
    health_collection_continues: Literal[True]
    load_freshness_bound_ms: int
    health_freshness_bound_ms: int


class RoutingEndpointInstanceView(FrozenModel):
    endpoint_id: RoutingEndpointId
    instance_id: str


class RoutingObserverEpochView(FrozenModel):
    observer_id: str
    epoch: int


class RoutingResetView(FrozenModel):
    virtual_time_ms: int
    endpoint_instances: tuple[RoutingEndpointInstanceView, ...]
    observer_epochs: tuple[RoutingObserverEpochView, ...]
    queue_cleared: Literal[True]
    load_state_cleared: Literal[True]
    kv_state_cleared: Literal[True]


class RoutingTelemetryView(FrozenModel):
    signal: Literal["HEALTH", "LOAD", "KV"]
    observer_id: str
    endpoint_id: RoutingEndpointId
    epoch: int
    observed_at_ms: int
    decision_time_ms: int
    age_ms: int
    freshness_bound_ms: int
    value: str | int
    admissibility: RoutingAdmissibility


class RoutingCandidateView(FrozenModel):
    endpoint_id: RoutingEndpointId
    eligible: bool
    health: RoutingTelemetryView
    load: RoutingTelemetryView
    kv: RoutingTelemetryView


class RoutingTerminalOutcomeView(FrozenModel):
    terminal_outcome_id: str
    decision_id: str
    status: RoutingTerminalStatus
    reason: str
    started_at_ms: int
    ended_at_ms: int


class RoutingRequestView(FrozenModel):
    request_id: str
    sequence_index: int
    decision_id: str
    decision_time_ms: int
    candidates: tuple[RoutingCandidateView, ...]
    selected_endpoint_id: RoutingEndpointId | None
    claims_used: tuple[str, ...]
    claims_permitted_stale: tuple[str, ...]
    claims_discarded: tuple[str, ...]
    fallback_reason: Literal[
        "NONE",
        "REQUIRED_LOAD_STALE",
        "STALE_LOAD_FAIL_OPEN",
        "HEALTH_ONLY_TIE_BREAK",
    ]
    terminal: RoutingTerminalOutcomeView


class RoutingTerminalPopulationView(FrozenModel):
    status: RoutingTerminalStatus
    count: int


class RoutingCampaignTrialView(FrozenModel):
    trial_id: str
    policy_id: str
    reset: RoutingResetView
    requests: tuple[RoutingRequestView, ...]
    terminal_population: tuple[RoutingTerminalPopulationView, ...]
    terminal_population_total: int


class RoutingCampaignDetail(FrozenModel):
    """Allowlisted R1 receipt projection; no raw package contents are exposed."""

    projection_version: Literal["inferdrome.routing-campaign-dashboard.v1"] = (
        "inferdrome.routing-campaign-dashboard.v1"
    )
    summary: RoutingCampaignSummary
    fault_timeline: RoutingFaultTimelineView
    trials: tuple[RoutingCampaignTrialView, ...]
    interpretation_boundary: Literal["MEASUREMENT_EVIDENCE_ONLY"] = (
        "MEASUREMENT_EVIDENCE_ONLY"
    )


class RejectedRoutingCampaign(FrozenModel):
    """A withheld configured package with no verifier or path disclosure."""

    entry: str
    status: Literal["REJECTED"] = "REJECTED"
    code: Literal["VERIFICATION_FAILED", "UNSAFE_ENTRY"]
    message: str = "Routing campaign could not be verified."


class RoutingCampaignIndexResponse(FrozenModel):
    projection_version: Literal["inferdrome.routing-campaign-dashboard.v1"] = (
        "inferdrome.routing-campaign-dashboard.v1"
    )
    routing_campaigns: tuple[RoutingCampaignSummary, ...]
    rejected: tuple[RejectedRoutingCampaign, ...]
    page: PageView
