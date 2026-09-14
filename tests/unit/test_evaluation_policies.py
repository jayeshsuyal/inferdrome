"""Policy semantics, retained telemetry, and deterministic replay boundaries."""

from __future__ import annotations

from collections import Counter
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from inferdrome.evaluation.contracts import EndpointId, EvaluationError
from inferdrome.evaluation.policies import (
    MAX_LOAD_COUNT,
    MAX_SAFE_INTEGER,
    POLICY_IDS,
    SAMPLE_STATUSES,
    EndpointSnapshot,
    HealthObservation,
    LoadObservation,
    PolicyId,
    RouterSnapshot,
    RoutingPolicy,
    SampleStatus,
)

RR: PolicyId = "evaluation_round_robin_v1"
LRL: PolicyId = "evaluation_least_reported_load_v1"
FALLBACK: PolicyId = "evaluation_freshness_fallback_v1"
CLOSED: PolicyId = "evaluation_fail_closed_v1"


def health(
    at: int = 0,
    *,
    sequence: int = 1,
    status: SampleStatus = "VALID",
    healthy: bool = True,
) -> HealthObservation:
    return HealthObservation(
        sequence, at, at, at, status, healthy if status == "VALID" else None
    )


def load(
    score: int = 1,
    *,
    at: int = 0,
    sequence: int = 1,
    status: SampleStatus = "VALID",
) -> LoadObservation:
    return LoadObservation(
        sequence,
        at,
        at,
        at,
        status,
        score if status == "VALID" else None,
        0 if status == "VALID" else None,
    )


DEFAULT_HEALTH = health()
DEFAULT_LOAD_A = load(1)
DEFAULT_LOAD_B = load(2)


def snapshot(
    *,
    health_a: HealthObservation | None = DEFAULT_HEALTH,
    health_b: HealthObservation | None = DEFAULT_HEALTH,
    load_a: LoadObservation | None = DEFAULT_LOAD_A,
    load_b: LoadObservation | None = DEFAULT_LOAD_B,
    attempt_a: LoadObservation | None = None,
    attempt_b: LoadObservation | None = None,
) -> RouterSnapshot:
    return RouterSnapshot(
        (
            EndpointSnapshot("endpoint-a", health_a, load_a, attempt_a or load_a),
            EndpointSnapshot("endpoint-b", health_b, load_b, attempt_b or load_b),
        )
    )


def policy(
    policy_id: PolicyId, *, health_age: int = 100, load_age: int = 10
) -> RoutingPolicy:
    return RoutingPolicy(
        policy_id, health_freshness_ns=health_age, load_freshness_ns=load_age
    )


@pytest.mark.parametrize("policy_id", POLICY_IDS)
@pytest.mark.parametrize("missing", ["neither", "a", "b", "both"])
def test_missing_required_load_has_explicit_policy_semantics(
    policy_id: PolicyId, missing: str
) -> None:
    state = snapshot(
        load_a=None if missing in {"a", "both"} else load(2),
        load_b=None if missing in {"b", "both"} else load(1),
    )
    decision = policy(policy_id).decide(state, request_index=0, decision_ns=0)
    if policy_id == RR:
        assert decision.selected_endpoint_id == "endpoint-a"
        assert decision.mode == "ROUND_ROBIN"
    elif missing == "neither":
        assert decision.selected_endpoint_id == "endpoint-b"
        assert decision.mode == "LOAD"
    elif policy_id == FALLBACK:
        assert decision.selected_endpoint_id == "endpoint-a"
        assert decision.reason == "FALLBACK_LOAD_MISSING"
        assert decision.mode == "FALLBACK"
    else:
        assert decision.selected_endpoint_id is None
        assert decision.reason == "REQUIRED_LOAD_MISSING"
        assert decision.mode == "REJECT"
    assert decision.eligible_endpoint_ids == ("endpoint-a", "endpoint-b")


@pytest.mark.parametrize("policy_id", POLICY_IDS)
@pytest.mark.parametrize("age", [9, 10, 11])
def test_load_freshness_uses_inclusive_equality(policy_id: PolicyId, age: int) -> None:
    state = snapshot(health_a=health(age), health_b=health(age), load_b=load(2, at=age))
    decision = policy(policy_id).decide(state, request_index=0, decision_ns=age)
    assert decision.load_ages_ns == (age, 0)
    assert decision.load_states == ("FRESH" if age <= 10 else "STALE", "FRESH")
    if policy_id == RR:
        assert decision.mode == "ROUND_ROBIN"
    elif age <= 10:
        assert decision.mode == "LOAD"
    elif policy_id == LRL:
        assert decision.mode == "LOAD"
        assert decision.reason == "RETAINED_STALE_LOAD"
    elif policy_id == FALLBACK:
        assert decision.mode == "FALLBACK"
        assert decision.reason == "FALLBACK_LOAD_STALE"
    else:
        assert decision.mode == "REJECT"
        assert decision.reason == "REQUIRED_LOAD_STALE"


@pytest.mark.parametrize("policy_id", POLICY_IDS)
@pytest.mark.parametrize("age", [9, 10, 11])
def test_health_freshness_uses_its_separate_inclusive_bound(
    policy_id: PolicyId, age: int
) -> None:
    state = snapshot(
        health_b=health(age), load_a=load(0, at=age), load_b=load(1, at=age)
    )
    decision = policy(policy_id, health_age=10).decide(
        state, request_index=0, decision_ns=age
    )
    assert decision.eligible_endpoint_ids == (
        ("endpoint-a", "endpoint-b") if age <= 10 else ("endpoint-b",)
    )
    assert decision.selected_endpoint_id == (
        "endpoint-a" if age <= 10 else "endpoint-b"
    )


@pytest.mark.parametrize("policy_id", POLICY_IDS)
@pytest.mark.parametrize("health_status", SAMPLE_STATUSES)
@pytest.mark.parametrize("healthy", [False, True])
def test_only_latest_fresh_valid_healthy_response_is_eligible(
    policy_id: PolicyId, health_status: SampleStatus, healthy: bool
) -> None:
    state = snapshot(
        health_a=health(status=health_status, healthy=healthy),
        health_b=health(healthy=False),
    )
    decision = policy(policy_id).decide(state, request_index=0, decision_ns=0)
    if health_status == "VALID" and healthy:
        assert decision.eligible_endpoint_ids == ("endpoint-a",)
        assert decision.selected_endpoint_id == "endpoint-a"
    else:
        assert decision.eligible_endpoint_ids == ()
        assert decision.selected_endpoint_id is None
        assert decision.reason == "NO_HEALTHY_ENDPOINT"


@pytest.mark.parametrize("policy_id", POLICY_IDS)
@pytest.mark.parametrize("unhealthy_endpoint", ["endpoint-a", "endpoint-b"])
def test_excluded_endpoints_missing_load_cannot_veto_healthy_engine(
    policy_id: PolicyId, unhealthy_endpoint: EndpointId
) -> None:
    state = snapshot(
        health_a=None if unhealthy_endpoint == "endpoint-a" else health(),
        health_b=None if unhealthy_endpoint == "endpoint-b" else health(),
        load_a=None if unhealthy_endpoint == "endpoint-a" else load(100),
        load_b=None if unhealthy_endpoint == "endpoint-b" else load(100),
    )
    decision = policy(policy_id).decide(state, request_index=0, decision_ns=0)
    expected = "endpoint-b" if unhealthy_endpoint == "endpoint-a" else "endpoint-a"
    assert decision.selected_endpoint_id == expected
    assert decision.eligible_endpoint_ids == (expected,)


@pytest.mark.parametrize("policy_id", POLICY_IDS)
@pytest.mark.parametrize("load_state", ["fresh", "stale", "missing"])
def test_one_healthy_engine_still_obeys_each_policys_load_requirement(
    policy_id: PolicyId, load_state: str
) -> None:
    state = snapshot(
        health_a=health(20),
        health_b=health(20, healthy=False),
        load_a=None
        if load_state == "missing"
        else load(at=20 if load_state == "fresh" else 0),
        load_b=None,
    )
    decision = policy(policy_id).decide(state, request_index=0, decision_ns=20)
    rejected = (policy_id == LRL and load_state == "missing") or (
        policy_id == CLOSED and load_state != "fresh"
    )
    assert decision.selected_endpoint_id == (None if rejected else "endpoint-a")
    assert decision.eligible_endpoint_ids == ("endpoint-a",)


@pytest.mark.parametrize("policy_id", [LRL, FALLBACK, CLOSED])
@pytest.mark.parametrize("failure", SAMPLE_STATUSES[1:])
def test_failed_latest_poll_preserves_fresh_valid_value_and_its_age(
    policy_id: PolicyId, failure: SampleStatus
) -> None:
    retained = load(1, at=10)
    failed_attempt = load(at=15, sequence=2, status=failure)
    state = snapshot(
        health_a=health(20),
        health_b=health(20),
        load_a=retained,
        load_b=load(2, at=20),
        attempt_a=failed_attempt,
    )
    decision = policy(policy_id).decide(state, request_index=0, decision_ns=20)
    assert decision.selected_endpoint_id == "endpoint-a"
    assert decision.mode == "LOAD"
    assert decision.reason == "RETAINED_FRESH_LOAD"
    assert decision.load_ages_ns == (10, 0)
    assert decision.load_sequence_ids == (1, 1)
    assert decision.snapshot.endpoints[0].last_load_attempt == failed_attempt
    assert decision.snapshot.endpoints[0].load == retained


@pytest.mark.parametrize("policy_id", POLICY_IDS)
def test_late_load_delivery_does_not_restart_freshness(policy_id: PolicyId) -> None:
    old = LoadObservation(1, 0, 90, 95, "VALID", 0, 0)
    state = snapshot(
        health_a=health(100),
        health_b=health(100),
        load_a=old,
        load_b=load(10, at=100),
    )
    decision = policy(policy_id).decide(state, request_index=0, decision_ns=100)
    assert decision.load_ages_ns == (100, 0)
    assert decision.load_states == ("STALE", "FRESH")
    expected_mode = {
        RR: "ROUND_ROBIN",
        LRL: "LOAD",
        FALLBACK: "FALLBACK",
        CLOSED: "REJECT",
    }
    assert decision.mode == expected_mode[policy_id]


@pytest.mark.parametrize("policy_id", [LRL, FALLBACK, CLOSED])
def test_lowest_load_adds_running_and_waiting_and_ties_rotate(
    policy_id: PolicyId,
) -> None:
    equal = snapshot(
        load_a=LoadObservation(1, 0, 0, 0, "VALID", 1, 4),
        load_b=LoadObservation(1, 0, 0, 0, "VALID", 3, 2),
    )
    selected = policy(policy_id)
    assert [
        selected.decide(equal, request_index=i, decision_ns=0).selected_endpoint_id
        for i in range(4)
    ] == ["endpoint-a", "endpoint-b", "endpoint-a", "endpoint-b"]
    non_tie = snapshot(load_a=load(10, sequence=2), load_b=load(1, sequence=2))
    assert (
        selected.decide(non_tie, request_index=4, decision_ns=0).selected_endpoint_id
        == "endpoint-b"
    )
    equal_again = snapshot(load_a=load(1, sequence=3), load_b=load(1, sequence=3))
    assert (
        selected.decide(
            equal_again, request_index=5, decision_ns=0
        ).selected_endpoint_id
        == "endpoint-a"
    )


@pytest.mark.parametrize("policy_id", [RR, FALLBACK])
def test_balancing_is_bounded_and_deterministic_for_a_stable_healthy_pair(
    policy_id: PolicyId,
) -> None:
    state = snapshot(load_a=None, load_b=None)
    selected = policy(policy_id)
    counts: Counter[EndpointId | None] = Counter()
    for index in range(1000):
        decision = selected.decide(state, request_index=index, decision_ns=0)
        counts[decision.selected_endpoint_id] += 1
        assert abs(counts["endpoint-a"] - counts["endpoint-b"]) <= 1
    assert counts == {"endpoint-a": 500, "endpoint-b": 500}


def test_fallback_cursor_persists_across_episodes_separately_from_load_ties() -> None:
    selected = policy(FALLBACK)
    states = [
        snapshot(),
        snapshot(load_a=load(at=12, sequence=2), load_b=load(at=12, sequence=2)),
        snapshot(load_a=load(at=12, sequence=2), load_b=load(at=12, sequence=2)),
        snapshot(load_a=load(at=24, sequence=3), load_b=load(at=24, sequence=3)),
    ]
    decisions = [
        selected.decide(state, request_index=index, decision_ns=timestamp)
        for index, (state, timestamp) in enumerate(
            zip(states, (11, 12, 23, 24), strict=True)
        )
    ]
    assert [decision.mode for decision in decisions] == [
        "FALLBACK",
        "LOAD",
        "FALLBACK",
        "LOAD",
    ]
    assert [decision.selected_endpoint_id for decision in decisions] == [
        "endpoint-a",
        "endpoint-a",
        "endpoint-b",
        "endpoint-b",
    ]


@pytest.mark.parametrize("policy_id", POLICY_IDS)
def test_rejection_does_not_advance_any_selection_cursor(policy_id: PolicyId) -> None:
    selected = policy(policy_id)
    shared_load = None if policy_id == FALLBACK else load()
    initial = snapshot(load_a=shared_load, load_b=shared_load)
    assert (
        selected.decide(initial, request_index=0, decision_ns=0).selected_endpoint_id
        == "endpoint-a"
    )
    no_health = snapshot(
        health_a=health(1, sequence=2, status="TIMEOUT"),
        health_b=health(1, sequence=2, status="MALFORMED"),
        load_a=shared_load,
        load_b=shared_load,
    )
    assert (
        selected.decide(no_health, request_index=1, decision_ns=1).selected_endpoint_id
        is None
    )
    restored = snapshot(
        health_a=health(2, sequence=3),
        health_b=health(2, sequence=3),
        load_a=shared_load,
        load_b=shared_load,
    )
    assert (
        selected.decide(restored, request_index=2, decision_ns=2).selected_endpoint_id
        == "endpoint-b"
    )


def test_round_robin_skips_unhealthy_and_returns_to_next_endpoint_on_recovery() -> None:
    selected = policy(RR)
    only_a = snapshot(health_b=health(healthy=False))
    assert [
        selected.decide(only_a, request_index=i, decision_ns=0).selected_endpoint_id
        for i in range(3)
    ] == ["endpoint-a"] * 3
    restored = snapshot(health_b=health(sequence=2))
    assert (
        selected.decide(restored, request_index=3, decision_ns=0).selected_endpoint_id
        == "endpoint-b"
    )


@pytest.mark.parametrize("policy_id", POLICY_IDS)
def test_decision_snapshot_and_settings_allow_independent_replay(
    policy_id: PolicyId,
) -> None:
    initial = snapshot(load_a=load(), load_b=load())
    first = policy(policy_id)
    decisions = [
        first.decide(initial, request_index=i, decision_ns=i) for i in range(15)
    ]
    replay = policy(policy_id)
    reproduced = [
        replay.decide(
            row.snapshot, request_index=row.request_index, decision_ns=row.decision_ns
        )
        for row in decisions
    ]
    assert decisions == reproduced
    assert decisions[0].snapshot is initial
    assert asdict(decisions[0])["snapshot"] == asdict(initial)
    assert set(asdict(initial)) == {"endpoints"}
    assert (
        policy(policy_id).decide(initial, request_index=0, decision_ns=0)
        == decisions[0]
    )


@pytest.mark.parametrize("policy_id", POLICY_IDS)
@pytest.mark.parametrize("future_channel", ["health", "load", "attempt"])
def test_any_future_snapshot_observation_is_rejected_even_if_endpoint_is_excluded(
    policy_id: PolicyId,
    future_channel: str,
) -> None:
    state = snapshot(
        health_a=health(21, healthy=False)
        if future_channel == "health"
        else health(healthy=False),
        load_a=load(at=21) if future_channel == "load" else load(),
        attempt_a=load(at=21, sequence=2, status="MALFORMED")
        if future_channel == "attempt"
        else None,
    )
    with pytest.raises(EvaluationError, match="future observation"):
        policy(policy_id).decide(state, request_index=0, decision_ns=20)


def test_regressing_decision_time_and_rewritten_sequences_do_not_advance_cursor() -> (
    None
):
    selected = policy(RR)
    initial = snapshot()
    assert (
        selected.decide(initial, request_index=0, decision_ns=5).selected_endpoint_id
        == "endpoint-a"
    )
    with pytest.raises(EvaluationError, match="decision time regressed"):
        selected.decide(initial, request_index=1, decision_ns=4)
    rewritten = snapshot(load_a=load(99))
    with pytest.raises(EvaluationError, match="sequence was rewritten"):
        selected.decide(rewritten, request_index=1, decision_ns=5)
    assert (
        selected.decide(initial, request_index=1, decision_ns=5).selected_endpoint_id
        == "endpoint-b"
    )


@pytest.mark.parametrize("channel", ["health", "load", "attempt"])
def test_observation_sequences_cannot_regress_or_disappear(channel: str) -> None:
    selected = policy(RR)
    initial = snapshot(
        health_a=health(sequence=2),
        load_a=load(sequence=2),
        attempt_a=load(sequence=3, status="TIMEOUT"),
    )
    selected.decide(initial, request_index=0, decision_ns=0)
    regressed = snapshot(
        health_a=health(sequence=1 if channel == "health" else 2),
        load_a=load(sequence=1 if channel == "load" else 2),
        attempt_a=load(sequence=2 if channel == "attempt" else 3, status="TIMEOUT")
        if channel != "attempt"
        else load(sequence=2),
    )
    with pytest.raises(EvaluationError, match="sequence regressed"):
        selected.decide(regressed, request_index=1, decision_ns=0)
    with pytest.raises(EvaluationError, match="sequence regressed"):
        selected.decide(
            snapshot(health_a=None, load_a=None), request_index=1, decision_ns=0
        )


def test_newer_sequence_cannot_move_observation_time_backwards() -> None:
    selected = policy(RR)
    selected.decide(snapshot(health_a=health(10)), request_index=0, decision_ns=10)
    with pytest.raises(EvaluationError, match="timestamps regressed"):
        selected.decide(
            snapshot(health_a=health(9, sequence=2)), request_index=1, decision_ns=10
        )


def test_load_cannot_appear_retroactively_after_a_later_failed_attempt() -> None:
    selected = policy(RR)
    selected.decide(
        snapshot(load_a=load(sequence=1), attempt_a=load(sequence=3, status="TIMEOUT")),
        request_index=0,
        decision_ns=0,
    )
    with pytest.raises(EvaluationError, match="after a later attempt"):
        selected.decide(
            snapshot(
                load_a=load(sequence=2), attempt_a=load(sequence=4, status="TIMEOUT")
            ),
            request_index=1,
            decision_ns=0,
        )


@pytest.mark.parametrize(
    "invalid", [-1, True, 0.0, float("inf"), "SECRET", MAX_SAFE_INTEGER + 1]
)
@pytest.mark.parametrize(
    "field", ["sequence", "started_ns", "completed_ns", "published_ns"]
)
@pytest.mark.parametrize("kind", ["health", "load"])
def test_observation_integers_are_strict_and_bounded(
    invalid: object,
    field: str,
    kind: str,
) -> None:
    sample = health() if kind == "health" else load()
    with pytest.raises(EvaluationError) as error:
        replace(sample, **{field: invalid})
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("times", [(2, 1, 3), (1, 3, 2)])
def test_observation_times_must_be_ordered(times: tuple[int, int, int]) -> None:
    with pytest.raises(EvaluationError, match="unordered"):
        HealthObservation(1, *times, "VALID", True)
    with pytest.raises(EvaluationError, match="unordered"):
        LoadObservation(1, *times, "VALID", 1, 0)


@pytest.mark.parametrize("invalid", [None, True, -1, 1.2, "1", MAX_LOAD_COUNT + 1])
@pytest.mark.parametrize("field", ["running", "waiting"])
def test_load_counts_are_strict_nonnegative_bounded_integers(
    invalid: object, field: str
) -> None:
    with pytest.raises(EvaluationError):
        replace(load(), **{field: invalid})


def test_largest_allowed_values_and_zero_are_accepted() -> None:
    state = LoadObservation(
        MAX_SAFE_INTEGER,
        MAX_SAFE_INTEGER,
        MAX_SAFE_INTEGER,
        MAX_SAFE_INTEGER,
        "VALID",
        MAX_LOAD_COUNT,
        MAX_LOAD_COUNT,
    )
    assert state.score == 2 * MAX_LOAD_COUNT
    assert load(0).score == 0
    assert load(status="MISSING").score is None
    observed_health = HealthObservation(
        MAX_SAFE_INTEGER,
        MAX_SAFE_INTEGER,
        MAX_SAFE_INTEGER,
        MAX_SAFE_INTEGER,
        "VALID",
        True,
    )
    selected = policy(CLOSED, health_age=0, load_age=0)
    decision = selected.decide(
        snapshot(
            health_a=observed_health,
            health_b=observed_health,
            load_a=state,
            load_b=state,
        ),
        request_index=MAX_SAFE_INTEGER,
        decision_ns=MAX_SAFE_INTEGER,
    )
    assert decision.selected_endpoint_id == "endpoint-a"


@pytest.mark.parametrize("invalid", [None, 0, 1, "healthy", []])
def test_valid_health_requires_a_real_boolean(invalid: object) -> None:
    with pytest.raises(EvaluationError):
        replace(health(), healthy=invalid)


@pytest.mark.parametrize("status", SAMPLE_STATUSES[1:])
def test_nonvalid_observations_cannot_carry_values(status: SampleStatus) -> None:
    with pytest.raises(EvaluationError):
        replace(health(), status=status)
    with pytest.raises(EvaluationError):
        replace(load(), status=status)


@pytest.mark.parametrize("invalid", [None, 1, [], "SECRET"])
def test_unknown_status_and_policy_are_sanitized(invalid: object) -> None:
    with pytest.raises(EvaluationError) as error:
        replace(health(), status=invalid)
    assert "SECRET" not in str(error.value)
    with pytest.raises(EvaluationError) as error:
        policy(invalid)  # type: ignore[arg-type]
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize(
    "retained, latest",
    [
        (load(), None),
        (None, load()),
        (load(status="MALFORMED"), load(status="MALFORMED")),
        (load(sequence=2), load(sequence=1)),
        (load(sequence=1), load(sequence=1, status="MALFORMED")),
        (load(sequence=1), load(sequence=2)),
        (load(at=2), load(at=1, sequence=2, status="TIMEOUT")),
    ],
)
def test_retained_and_latest_load_history_must_be_consistent(
    retained: LoadObservation | None,
    latest: LoadObservation | None,
) -> None:
    with pytest.raises(EvaluationError):
        EndpointSnapshot("endpoint-a", health(), retained, latest)


def test_latest_failed_attempt_without_ever_valid_load_is_explicit_missing() -> None:
    state = snapshot(load_a=None, attempt_a=load(sequence=2, status="MALFORMED"))
    decision = policy(CLOSED).decide(state, request_index=0, decision_ns=0)
    assert decision.load_states == ("MISSING", "FRESH")
    assert decision.load_sequence_ids == (None, 1)
    assert decision.snapshot.endpoints[0].last_load_attempt is not None
    assert decision.snapshot.endpoints[0].last_load_attempt.status == "MALFORMED"
    assert decision.reason == "REQUIRED_LOAD_MISSING"


def test_snapshot_is_frozen_and_requires_exact_ordered_endpoint_pair() -> None:
    state = snapshot()
    with pytest.raises(FrozenInstanceError):
        state.endpoints = ()  # type: ignore[assignment,misc]
    with pytest.raises(FrozenInstanceError):
        state.endpoints[0].health = None  # type: ignore[misc]
    for invalid in (
        (),
        state.endpoints[::-1],
        (state.endpoints[0],) * 2,
        list(state.endpoints),
    ):
        with pytest.raises(EvaluationError):
            RouterSnapshot(invalid)  # type: ignore[arg-type]
    with pytest.raises(EvaluationError):
        EndpointSnapshot("SECRET", None, None, None)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid", [-1, True, 1.2, "SECRET", MAX_SAFE_INTEGER + 1])
@pytest.mark.parametrize("field", ["request_index", "decision_ns"])
def test_decision_integers_are_strict_and_bounded(invalid: object, field: str) -> None:
    arguments: dict[str, object] = {
        "request_index": 0,
        "decision_ns": 0,
        field: invalid,
    }
    with pytest.raises(EvaluationError):
        policy(RR).decide(snapshot(), **arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid", [-1, True, 1.2, "SECRET", MAX_SAFE_INTEGER + 1])
@pytest.mark.parametrize("field", ["health_freshness_ns", "load_freshness_ns"])
def test_policy_freshness_bounds_are_strict_integers(
    invalid: object, field: str
) -> None:
    arguments: dict[str, object] = {
        "health_freshness_ns": 1,
        "load_freshness_ns": 1,
        field: invalid,
    }
    with pytest.raises(EvaluationError):
        RoutingPolicy(RR, **arguments)  # type: ignore[arg-type]
