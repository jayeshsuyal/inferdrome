"""Four bounded routing baselines using only router-visible observations.

These policies are independent of the frozen routing-execution policies. Health
always gates eligibility. Load freshness uses acquisition start, so slow delivery
or republication cannot make an old observation fresh. A failed latest load poll
does not invalidate a retained valid observation or reset its age.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from inferdrome.evaluation.contracts import EndpointId, EvaluationError

PolicyId = Literal[
    "evaluation_round_robin_v1",
    "evaluation_least_reported_load_v1",
    "evaluation_freshness_fallback_v1",
    "evaluation_fail_closed_v1",
]
SampleStatus = Literal[
    "VALID",
    "MISSING",
    "MALFORMED",
    "HTTP_ERROR",
    "TIMEOUT",
    "TRANSPORT_ERROR",
    "CANCELLED",
]
LoadState = Literal["FRESH", "STALE", "MISSING"]
DecisionMode = Literal["ROUND_ROBIN", "LOAD", "FALLBACK", "REJECT"]
DecisionReason = Literal[
    "NO_HEALTHY_ENDPOINT",
    "REQUIRED_LOAD_MISSING",
    "REQUIRED_LOAD_STALE",
    "ROUND_ROBIN",
    "FRESH_LOAD",
    "RETAINED_FRESH_LOAD",
    "RETAINED_STALE_LOAD",
    "FALLBACK_LOAD_MISSING",
    "FALLBACK_LOAD_STALE",
]

POLICY_IDS: tuple[PolicyId, ...] = (
    "evaluation_round_robin_v1",
    "evaluation_least_reported_load_v1",
    "evaluation_freshness_fallback_v1",
    "evaluation_fail_closed_v1",
)
SAMPLE_STATUSES: tuple[SampleStatus, ...] = (
    "VALID",
    "MISSING",
    "MALFORMED",
    "HTTP_ERROR",
    "TIMEOUT",
    "TRANSPORT_ERROR",
    "CANCELLED",
)
MAX_SAFE_INTEGER = 2**53 - 1
MAX_LOAD_COUNT = 2**31 - 1
_ENDPOINTS: tuple[EndpointId, EndpointId] = ("endpoint-a", "endpoint-b")


def _bounded_integer(value: object, ceiling: int = MAX_SAFE_INTEGER) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= ceiling
    ):
        raise EvaluationError("routing integer violates its bound")
    return value


def _observation_contract(
    sequence: int,
    started_ns: int,
    completed_ns: int,
    published_ns: int,
    status: SampleStatus,
) -> None:
    for value in (sequence, started_ns, completed_ns, published_ns):
        _bounded_integer(value)
    if not started_ns <= completed_ns <= published_ns:
        raise EvaluationError("routing observation timestamps are unordered")
    if not isinstance(status, str) or status not in SAMPLE_STATUSES:
        raise EvaluationError("routing observation status is unsupported")


@dataclass(frozen=True, slots=True)
class HealthObservation:
    sequence: int
    started_ns: int
    completed_ns: int
    published_ns: int
    status: SampleStatus
    healthy: bool | None

    def __post_init__(self) -> None:
        _observation_contract(
            self.sequence,
            self.started_ns,
            self.completed_ns,
            self.published_ns,
            self.status,
        )
        if (self.status == "VALID" and not isinstance(self.healthy, bool)) or (
            self.status != "VALID" and self.healthy is not None
        ):
            raise EvaluationError("routing health value contradicts its status")


@dataclass(frozen=True, slots=True)
class LoadObservation:
    sequence: int
    started_ns: int
    completed_ns: int
    published_ns: int
    status: SampleStatus
    running: int | None
    waiting: int | None

    def __post_init__(self) -> None:
        _observation_contract(
            self.sequence,
            self.started_ns,
            self.completed_ns,
            self.published_ns,
            self.status,
        )
        if self.status == "VALID":
            _bounded_integer(self.running, MAX_LOAD_COUNT)
            _bounded_integer(self.waiting, MAX_LOAD_COUNT)
        elif self.running is not None or self.waiting is not None:
            raise EvaluationError("routing load value contradicts its status")

    @property
    def score(self) -> int | None:
        if self.status != "VALID":
            return None
        assert self.running is not None and self.waiting is not None
        return self.running + self.waiting


@dataclass(frozen=True, slots=True)
class EndpointSnapshot:
    endpoint_id: EndpointId
    health: HealthObservation | None
    load: LoadObservation | None
    last_load_attempt: LoadObservation | None

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint_id, str) or self.endpoint_id not in _ENDPOINTS:
            raise EvaluationError("routing endpoint identifier is unsupported")
        if self.health is not None and type(self.health) is not HealthObservation:
            raise EvaluationError("routing health observation has an invalid type")
        for load in (self.load, self.last_load_attempt):
            if load is not None and type(load) is not LoadObservation:
                raise EvaluationError("routing load observation has an invalid type")
        if self.load is not None:
            if self.load.status != "VALID":
                raise EvaluationError("routing retained load must be valid")
            latest = self.last_load_attempt
            if latest is None or latest.sequence < self.load.sequence:
                raise EvaluationError(
                    "routing latest load attempt precedes retained load"
                )
            if (
                latest.started_ns < self.load.started_ns
                or latest.completed_ns < self.load.completed_ns
                or latest.published_ns < self.load.published_ns
                or (latest.sequence == self.load.sequence and latest != self.load)
            ):
                raise EvaluationError("routing load history is inconsistent")
        if (
            self.last_load_attempt is not None
            and self.last_load_attempt.status == "VALID"
            and self.last_load_attempt != self.load
        ):
            raise EvaluationError("routing latest valid load must be retained")


@dataclass(frozen=True, slots=True)
class RouterSnapshot:
    endpoints: tuple[EndpointSnapshot, EndpointSnapshot]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.endpoints, tuple)
            or len(self.endpoints) != 2
            or any(
                type(endpoint) is not EndpointSnapshot for endpoint in self.endpoints
            )
            or tuple(endpoint.endpoint_id for endpoint in self.endpoints) != _ENDPOINTS
        ):
            raise EvaluationError("routing snapshot requires ordered endpoints A and B")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    request_index: int
    decision_ns: int
    policy_id: PolicyId
    selected_endpoint_id: EndpointId | None
    eligible_endpoint_ids: tuple[EndpointId, ...]
    mode: DecisionMode
    reason: DecisionReason
    load_ages_ns: tuple[int | None, int | None]
    load_states: tuple[LoadState, LoadState]
    load_sequence_ids: tuple[int | None, int | None]
    snapshot: RouterSnapshot


def _validate_progress(
    previous: HealthObservation | LoadObservation | None,
    current: HealthObservation | LoadObservation | None,
) -> None:
    if previous is None:
        return
    if current is None or current.sequence < previous.sequence:
        raise EvaluationError("routing observation sequence regressed")
    if current.sequence == previous.sequence:
        if current != previous:
            raise EvaluationError("routing observation sequence was rewritten")
    elif (
        current.started_ns < previous.started_ns
        or current.completed_ns < previous.completed_ns
        or current.published_ns < previous.published_ns
    ):
        raise EvaluationError("routing observation timestamps regressed")


def _ring_choice(
    candidates: tuple[EndpointId, ...], cursor: int
) -> tuple[EndpointId, int]:
    for offset in range(2):
        position = (cursor + offset) % 2
        selected = _ENDPOINTS[position]
        if selected in candidates:
            return selected, (position + 1) % 2
    raise AssertionError("a routing ring choice requires an eligible endpoint")


class RoutingPolicy:
    """One trial's deterministic policy state; no I/O or observer references.

    Health and load are fresh at equality with their separate age bounds. Only
    eligible endpoints' load is required. Round-robin advances after any selection;
    load ties advance a separate cursor only on a tie. Freshness fallback advances
    its own cursor only on fallback selections, including across fallback episodes.
    Rejections advance no cursor. All state has constant size.
    """

    __slots__ = (
        "_fallback_cursor",
        "_health_freshness_ns",
        "_last_decision_ns",
        "_load_freshness_ns",
        "_previous_snapshot",
        "_round_robin_cursor",
        "_tie_cursor",
        "policy_id",
    )

    def __init__(
        self,
        policy_id: PolicyId,
        *,
        health_freshness_ns: int,
        load_freshness_ns: int,
    ) -> None:
        if not isinstance(policy_id, str) or policy_id not in POLICY_IDS:
            raise EvaluationError("routing policy identifier is unsupported")
        self.policy_id = policy_id
        self._health_freshness_ns = _bounded_integer(health_freshness_ns)
        self._load_freshness_ns = _bounded_integer(load_freshness_ns)
        self._round_robin_cursor = 0
        self._tie_cursor = 0
        self._fallback_cursor = 0
        self._last_decision_ns: int | None = None
        self._previous_snapshot: RouterSnapshot | None = None

    def _validate_snapshot(self, snapshot: RouterSnapshot, decision_ns: int) -> None:
        if type(snapshot) is not RouterSnapshot:
            raise EvaluationError("routing snapshot has an invalid type")
        if self._last_decision_ns is not None and decision_ns < self._last_decision_ns:
            raise EvaluationError("routing decision time regressed")
        for endpoint in snapshot.endpoints:
            for sample in (endpoint.health, endpoint.load, endpoint.last_load_attempt):
                if sample is not None and sample.published_ns > decision_ns:
                    raise EvaluationError(
                        "routing snapshot contains a future observation"
                    )
        if self._previous_snapshot is None:
            return
        for previous, current in zip(
            self._previous_snapshot.endpoints, snapshot.endpoints, strict=True
        ):
            _validate_progress(previous.health, current.health)
            _validate_progress(previous.load, current.load)
            _validate_progress(previous.last_load_attempt, current.last_load_attempt)
            if (
                current.load != previous.load
                and current.load is not None
                and previous.last_load_attempt is not None
                and current.load.sequence <= previous.last_load_attempt.sequence
            ):
                raise EvaluationError("routing load appeared after a later attempt")

    def decide(
        self, snapshot: RouterSnapshot, *, request_index: int, decision_ns: int
    ) -> PolicyDecision:
        _bounded_integer(request_index)
        _bounded_integer(decision_ns)
        self._validate_snapshot(snapshot, decision_ns)
        eligible: list[EndpointId] = []
        ages: list[int | None] = []
        states: list[LoadState] = []
        sequences: list[int | None] = []
        scores: dict[EndpointId, int] = {}
        for endpoint in snapshot.endpoints:
            health = endpoint.health
            if (
                health is not None
                and health.status == "VALID"
                and health.healthy is True
                and decision_ns - health.started_ns <= self._health_freshness_ns
            ):
                eligible.append(endpoint.endpoint_id)
            load = endpoint.load
            age = None if load is None else decision_ns - load.started_ns
            ages.append(age)
            sequences.append(None if load is None else load.sequence)
            states.append(
                "MISSING"
                if age is None
                else "FRESH"
                if age <= self._load_freshness_ns
                else "STALE"
            )
            if load is not None:
                assert load.score is not None
                scores[endpoint.endpoint_id] = load.score
        selected, mode, reason = self._select(
            tuple(eligible), scores, (states[0], states[1]), snapshot
        )
        decision = PolicyDecision(
            request_index=request_index,
            decision_ns=decision_ns,
            policy_id=self.policy_id,
            selected_endpoint_id=selected,
            eligible_endpoint_ids=tuple(eligible),
            mode=mode,
            reason=reason,
            load_ages_ns=(ages[0], ages[1]),
            load_states=(states[0], states[1]),
            load_sequence_ids=(sequences[0], sequences[1]),
            snapshot=snapshot,
        )
        self._last_decision_ns = decision_ns
        self._previous_snapshot = snapshot
        return decision

    def _select(
        self,
        eligible: tuple[EndpointId, ...],
        scores: dict[EndpointId, int],
        states: tuple[LoadState, LoadState],
        snapshot: RouterSnapshot,
    ) -> tuple[EndpointId | None, DecisionMode, DecisionReason]:
        if not eligible:
            return None, "REJECT", "NO_HEALTHY_ENDPOINT"
        if self.policy_id == "evaluation_round_robin_v1":
            selected, self._round_robin_cursor = _ring_choice(
                eligible, self._round_robin_cursor
            )
            return selected, "ROUND_ROBIN", "ROUND_ROBIN"
        required_states = [
            state
            for endpoint, state in zip(_ENDPOINTS, states, strict=True)
            if endpoint in eligible
        ]
        missing = "MISSING" in required_states
        stale = "STALE" in required_states
        if self.policy_id == "evaluation_freshness_fallback_v1" and (missing or stale):
            selected, self._fallback_cursor = _ring_choice(
                eligible, self._fallback_cursor
            )
            reason: DecisionReason = (
                "FALLBACK_LOAD_MISSING" if missing else "FALLBACK_LOAD_STALE"
            )
            return selected, "FALLBACK", reason
        if missing:
            return None, "REJECT", "REQUIRED_LOAD_MISSING"
        if stale and self.policy_id == "evaluation_fail_closed_v1":
            return None, "REJECT", "REQUIRED_LOAD_STALE"
        minimum = min(scores[endpoint] for endpoint in eligible)
        tied = tuple(endpoint for endpoint in eligible if scores[endpoint] == minimum)
        selected = tied[0]
        if len(tied) > 1:
            selected, self._tie_cursor = _ring_choice(tied, self._tie_cursor)
        retained_after_failure = any(
            endpoint.endpoint_id in eligible
            and endpoint.last_load_attempt is not None
            and endpoint.last_load_attempt.status != "VALID"
            for endpoint in snapshot.endpoints
        )
        return (
            selected,
            "LOAD",
            (
                "RETAINED_STALE_LOAD"
                if stale
                else "RETAINED_FRESH_LOAD"
                if retained_after_failure
                else "FRESH_LOAD"
            ),
        )
