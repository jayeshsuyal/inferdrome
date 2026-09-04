"""Explicit read-only contracts for the causal qualification projection."""

from typing import Literal

from inferdrome.dashboard.models import PageView
from inferdrome.domain.base import FrozenModel

RoutingQualificationId = Literal["stale-telemetry-qualification-v1"]
RoutingQualificationPolicyId = Literal[
    "fail_closed_required_load_v1",
    "explicit_fail_open_stale_load_v1",
    "typed_admissible_state_only_v1",
]
RoutingQualificationTerminalStatus = Literal[
    "SUCCEEDED",
    "TIMED_OUT",
    "FAILED",
    "CANCELLED",
    "NO_SAFE_ROUTE",
]
RoutingQualificationEndpointId = Literal["endpoint-a", "endpoint-b"]
RoutingQualificationAdmissibility = Literal["ADMISSIBLE", "INADMISSIBLE"]


class RoutingQualificationSummary(FrozenModel):
    """Verified identity and scope of one bounded qualification descriptor."""

    qualification_id: RoutingQualificationId
    retained_digest: str
    source_campaign_id: Literal["routing-campaign-v1"]
    source_package_retained_digest: str
    source_execution_mode: Literal["SYNTHETIC_CPU_ONLY"]
    repetitions_per_mode: Literal[1]
    population_accounting: Literal["SEPARATE_PER_TRIAL_NO_POOLING"]
    verified_by_source_replay: Literal[True] = True
    verified_descriptor_binding: Literal[True] = True


class RoutingQualificationFaultView(FrozenModel):
    """The fixed stale-load/fresh-health causal observation in virtual time."""

    load_observer_pause_at_ms: Literal[15]
    health_collection_continues: Literal[True]
    focal_decision_time_ms: Literal[20]
    health_age_ms: Literal[0]
    load_age_ms: Literal[10]
    freshness_bound_ms: Literal[5]


class RoutingQualificationEndpointStateView(FrozenModel):
    """Only the focal health/load state the declared policies received."""

    endpoint_id: RoutingQualificationEndpointId
    health_epoch: int
    health_age_ms: Literal[0]
    health_admissibility: Literal["ADMISSIBLE"]
    load_epoch: int
    load_age_ms: Literal[10]
    load_admissibility: Literal["INADMISSIBLE"]


class RoutingQualificationPopulationEntry(FrozenModel):
    status: RoutingQualificationTerminalStatus
    count: int


class RoutingQualificationResetView(FrozenModel):
    """Cold-reset identity retained separately for one policy trial."""

    virtual_time_ms: Literal[0]
    endpoint_a_instance_id: str
    endpoint_b_instance_id: str
    observer_epochs: tuple[int, int, int]
    queue_cleared: Literal[True]
    load_state_cleared: Literal[True]
    kv_state_cleared: Literal[True]


class RoutingQualificationTrialView(FrozenModel):
    """One declared mode, focal decision, and non-pooled terminal population."""

    policy_id: RoutingQualificationPolicyId
    repetition_index: Literal[0]
    trial_id: str
    request_denominator: Literal[6]
    reset: RoutingQualificationResetView
    focal_request_id: Literal["request-002"]
    focal_decision_id: str
    focal_endpoint_states: tuple[
        RoutingQualificationEndpointStateView,
        RoutingQualificationEndpointStateView,
    ]
    selected_endpoint_id: RoutingQualificationEndpointId | None
    fallback_reason: Literal[
        "REQUIRED_LOAD_STALE",
        "STALE_LOAD_FAIL_OPEN",
        "HEALTH_ONLY_TIE_BREAK",
    ]
    terminal_status: RoutingQualificationTerminalStatus
    terminal_reason: str
    reset_receipt_sha256: str
    state_observations_sha256: str
    route_decisions_sha256: str
    terminal_outcomes_sha256: str
    terminal_population: tuple[
        RoutingQualificationPopulationEntry,
        RoutingQualificationPopulationEntry,
        RoutingQualificationPopulationEntry,
        RoutingQualificationPopulationEntry,
        RoutingQualificationPopulationEntry,
    ]
    terminal_population_total: Literal[6]


class RoutingQualificationDetail(FrozenModel):
    """Verified causal narrative plus links back to the full R1 receipts."""

    projection_version: Literal["inferdrome.routing-qualification-dashboard.v1"] = (
        "inferdrome.routing-qualification-dashboard.v1"
    )
    summary: RoutingQualificationSummary
    fault_timeline: RoutingQualificationFaultView
    trials: tuple[
        RoutingQualificationTrialView,
        RoutingQualificationTrialView,
        RoutingQualificationTrialView,
    ]
    source_receipts_path: Literal["/routing-campaigns/routing-campaign-v1"] = (
        "/routing-campaigns/routing-campaign-v1"
    )
    descriptor_download_path: Literal[
        "/api/v1/routing-qualifications/stale-telemetry-qualification-v1/evidence"
    ] = "/api/v1/routing-qualifications/stale-telemetry-qualification-v1/evidence"
    interpretation_boundary: Literal["MEASUREMENT_EVIDENCE_ONLY"] = (
        "MEASUREMENT_EVIDENCE_ONLY"
    )


class RejectedRoutingQualification(FrozenModel):
    """A withheld configured qualification without path or verifier disclosure."""

    entry: str
    status: Literal["REJECTED"] = "REJECTED"
    code: Literal["CONFIGURATION_INVALID", "UNSAFE_ENTRY", "VERIFICATION_FAILED"]
    message: str = "Routing qualification could not be verified."


class RoutingQualificationIndexResponse(FrozenModel):
    projection_version: Literal["inferdrome.routing-qualification-dashboard.v1"] = (
        "inferdrome.routing-qualification-dashboard.v1"
    )
    routing_qualifications: tuple[RoutingQualificationSummary, ...]
    rejected: tuple[RejectedRoutingQualification, ...]
    page: PageView
