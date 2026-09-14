"""Closed, bounded offline validation against one expected compiled trial.

Digest and consistency checks do not attest execution or runtime identity. Raw
model names, origins, prompts, arbitrary strings and server errors are never
accepted as report fields or copied into validation exceptions.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, cast, get_args

from inferdrome.evaluation.contracts import EvaluationConfig, EvaluationError, Outcome
from inferdrome.evaluation.fault_config import RoutingFaultConfig
from inferdrome.evaluation.faults import FaultEvent, ObservationRecord
from inferdrome.evaluation.healthy_config import HealthyRoutingConfig
from inferdrome.evaluation.policies import (
    MAX_LOAD_COUNT,
    MAX_SAFE_INTEGER,
    SAMPLE_STATUSES,
    EndpointSnapshot,
    HealthObservation,
    LoadObservation,
    PolicyDecision,
    RouterSnapshot,
    RoutingPolicy,
)
from inferdrome.evaluation.runner import EvaluationResult, RequestRecord
from inferdrome.parsing import StructuredDataLimits, validate_json_structure
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

TrialConfig = HealthyRoutingConfig | RoutingFaultConfig
MAX_RESULT_BYTES = 64 * 1024 * 1024
OUTCOMES: tuple[str, ...] = get_args(Outcome)
_ENDPOINTS = ("endpoint-a", "endpoint-b")
_EVIDENCE = ("LOCAL_MEASUREMENT_ONLY", "SYNTHETIC_ONLY")
_RESULT_LIMITS = StructuredDataLimits(
    max_depth=32, max_tokens=4_000_000, max_integer_digits=16
)


class StudyValidationError(EvaluationError):
    """Sanitized offline measurement contract failure."""


def _require(condition: bool, category: str) -> None:
    if not condition:
        raise StudyValidationError(f"study result violates {category}")


def _closed(value: Any, names: set[str]) -> dict[str, Any]:
    _require(type(value) is dict and set(value) == names, "closed fields")
    return cast(dict[str, Any], value)


def _array(value: Any, bound: int) -> list[Any] | tuple[Any, ...]:
    _require(type(value) in (list, tuple) and len(value) <= bound, "array bounds")
    return cast(list[Any] | tuple[Any, ...], value)


def _integer(value: Any, ceiling: int = MAX_SAFE_INTEGER) -> int:
    _require(type(value) is int and 0 <= value <= ceiling, "integer bounds")
    return cast(int, value)


def _optional_integer(value: Any, ceiling: int = MAX_SAFE_INTEGER) -> int | None:
    return None if value is None else _integer(value, ceiling)


def _literal(value: Any, choices: tuple[str, ...]) -> str:
    _require(type(value) is str and value in choices, "literal fields")
    return cast(str, value)


def _equal(left: Any, right: Any, category: str) -> None:
    _require(canonical_json_bytes(left) == canonical_json_bytes(right), category)


def _names(cls: Any) -> set[str]:
    return {field.name for field in fields(cls)}


@dataclass(frozen=True)
class ValidatedTrialResult:
    config_sha256: str
    result_sha256: str
    policy_id: str
    status: str
    evidence_class: str
    foreground: EvaluationResult
    background: EvaluationResult | None
    observations: tuple[ObservationRecord, ...]
    decisions: tuple[PolicyDecision, ...]
    events: tuple[FaultEvent, ...]
    recovery: dict[str, Any] | None
    elapsed_ns: int


def _record(
    raw: Any,
    config: EvaluationConfig,
    index: int,
    elapsed: int,
    *,
    routing: str,
    stop_at_ns: int | None,
) -> RequestRecord:
    row = _closed(raw, _names(RequestRecord))
    _require(_integer(row["request_index"]) == index, "request index coverage")
    scheduled = config.offers[index].scheduled_ns
    _require(_integer(row["scheduled_ns"]) == scheduled, "offered schedule")
    end = config.bounds.duration_ns + config.bounds.drain_ns
    cutoff = min(scheduled + config.bounds.request_timeout_ns, end)
    if stop_at_ns is not None:
        cutoff = min(cutoff, stop_at_ns)
    for name in (
        "arrival_observed_ns",
        "dispatch_ns",
        "dispatch_lag_ns",
        "response_headers_ns",
        "first_body_byte_ns",
        "first_content_ns",
        "protocol_done_ns",
    ):
        _optional_integer(row[name])
    terminal = _integer(row["terminal_ns"], elapsed)
    outcome = _literal(row["outcome"], OUTCOMES)
    if routing == "FIXED_ASSIGNMENT":
        _require(outcome != "REJECTED_ROUTE", "fixed route outcome")
        if row["endpoint_id"] is not None:
            _require(
                row["endpoint_id"] == config.offers[index].endpoint_id,
                "fixed assignment",
            )
    arrival, dispatch = row["arrival_observed_ns"], row["dispatch_ns"]
    if arrival is not None:
        _require(scheduled <= arrival <= terminal, "arrival ordering")
    if dispatch is not None:
        _require(
            arrival is not None and arrival <= dispatch <= terminal, "dispatch ordering"
        )
        _require(row["dispatch_lag_ns"] == dispatch - scheduled, "dispatch lag")
        _literal(row["endpoint_id"], _ENDPOINTS)
        _require(dispatch < cutoff, "dispatch cutoff")
    else:
        _require(row["dispatch_lag_ns"] is None, "absent dispatch lag")
        _require(
            row["endpoint_id"] is None or row["endpoint_id"] in _ENDPOINTS,
            "undispatched endpoint",
        )
        _require(
            all(
                row[name] is None
                for name in (
                    "response_headers_ns",
                    "first_body_byte_ns",
                    "first_content_ns",
                    "protocol_done_ns",
                    "http_status",
                    "finish_reason",
                    "prompt_tokens",
                    "completion_tokens",
                )
            ),
            "undispatched progress",
        )
    _require(_integer(row["attempts"], 1) == int(dispatch is not None), "attempt count")
    headers, body = row["response_headers_ns"], row["first_body_byte_ns"]
    content, done = row["first_content_ns"], row["protocol_done_ns"]
    status = _optional_integer(row["http_status"], 599)
    _require((headers is None) == (status is None), "HTTP header status")
    if headers is not None:
        _require(
            dispatch is not None
            and dispatch <= headers <= terminal
            and status is not None
            and status >= 100,
            "HTTP timing",
        )
    if body is not None:
        _require(
            headers is not None and headers <= body <= terminal and status == 200,
            "body timing",
        )
    times = tuple(
        _integer(value, terminal)
        for value in _array(
            row["content_event_times_ns"], config.bounds.max_content_events
        )
    )
    _require(list(times) == sorted(times), "content event ordering")
    _require(
        (not times and content is None) or (bool(times) and content == times[0]),
        "first content identity",
    )
    if content is not None:
        _require(body is not None and body <= content, "first content ordering")
    _require(row["finish_reason"] in (None, "stop", "length"), "finish reason")
    if done is not None:
        _require(
            bool(times)
            and times[-1] <= done <= terminal
            and row["finish_reason"] is not None,
            "protocol completion",
        )
    prompt = _optional_integer(row["prompt_tokens"])
    completion = _optional_integer(row["completion_tokens"])
    provenance = _literal(
        row["usage_provenance"], ("UNAVAILABLE", "SERVER_REPORTED_STREAM_USAGE")
    )
    _require((prompt is None) == (completion is None), "usage pair")
    _require((provenance == "UNAVAILABLE") == (completion is None), "usage provenance")
    if completion is not None:
        _require(
            row["finish_reason"] is not None
            and status == 200
            and (not times or completion > 0),
            "usage consistency",
        )
    if outcome == "SUCCESS":
        _require(
            dispatch is not None and status == 200 and done is not None,
            "successful protocol",
        )
    if outcome in (
        "HTTP_ERROR",
        "STREAM_ERROR",
        "STREAM_LIMIT",
        "INCOMPLETE_STREAM",
        "TRANSPORT_ERROR",
        "SUCCESS",
    ):
        _require(dispatch is not None, "attempted terminal outcome")
    if outcome == "HTTP_ERROR":
        _require(status is not None and status != 200, "HTTP failure status")
    if outcome in ("STREAM_ERROR", "STREAM_LIMIT", "INCOMPLETE_STREAM"):
        _require(status == 200, "stream failure status")
    if outcome in ("REJECTED_CAPACITY", "REJECTED_ROUTE"):
        _require(
            arrival is not None and dispatch is None and row["endpoint_id"] is None,
            "rejection semantics",
        )
    _require(
        stop_at_ns is None or terminal < stop_at_ns or outcome == "CANCELLED",
        "scheduled population stop",
    )
    if outcome == "TIMEOUT":
        deadline = scheduled + config.bounds.request_timeout_ns
        _require(deadline <= end and terminal >= deadline, "request timeout boundary")
    elif outcome == "DRAIN_TIMEOUT":
        _require(
            terminal >= end and scheduled + config.bounds.request_timeout_ns > end,
            "drain boundary",
        )
    elif outcome != "CANCELLED":
        _require(terminal < cutoff, "terminal cutoff")
    return RequestRecord(**{**row, "content_event_times_ns": times})


def _population(
    raw: Any,
    config: EvaluationConfig,
    *,
    routing: str,
    evidence_class: str,
    stop_at_ns: int | None = None,
) -> EvaluationResult:
    aggregate_fields = {
        "outcomes",
        "offered_count",
        "arrivals_observed_count",
        "dispatched_count",
    }
    value = _closed(raw, _names(EvaluationResult) | aggregate_fields)
    fixed = {
        "config_sha256": sha256_digest(
            canonical_json_bytes(config.model_dump(mode="json"))
        ),
        "declared_source_commit": config.source_commit,
        "model_sha256": sha256_digest(config.model.encode()),
        "bounds": config.bounds.model_dump(),
        "request_settings": {
            "max_tokens": config.max_tokens,
            "temperature": 0,
            "enable_thinking": False,
            "n": 1,
            "stream": True,
            "include_usage": True,
        },
        "routing": routing,
        "schema_version": "inferdrome.evaluation-result.v1",
        "evidence_class": evidence_class,
        "evidence_eligible": False,
        "endpoint_identity": "UNVERIFIED",
        "exact_token_timing": "UNAVAILABLE",
        "wire_first_response_byte": "UNAVAILABLE",
        "cleanup": "CLIENT_TASKS_AND_CONNECTIONS_CLOSED",
    }
    for name, expected in fixed.items():
        _equal(value[name], expected, "population identity/settings")
    elapsed = _integer(value["elapsed_ns"])
    rows = _array(value["records"], len(config.offers))
    _require(len(rows) == len(config.offers), "offered record coverage")
    records = tuple(
        _record(row, config, index, elapsed, routing=routing, stop_at_ns=stop_at_ns)
        for index, row in enumerate(rows)
    )
    _require(type(value["cancelled"]) is bool, "cancellation flag")
    _require(
        value["cancelled"] or all(row.outcome != "CANCELLED" for row in records),
        "cancelled population",
    )
    peak_active = _integer(
        value["peak_active"], min(config.bounds.concurrency, len(rows))
    )
    _integer(value["peak_queue"], min(config.bounds.max_queue, len(rows)))
    _require(not any(row.attempts for row in records) or peak_active > 0, "active peak")
    active = 0
    # Half-open intervals provide a lower bound even when same-clock task
    # completion and dispatch ordering cannot be reconstructed from the trace.
    intervals = sorted(
        item
        for row in records
        if row.dispatch_ns is not None and row.dispatch_ns < row.terminal_ns
        for item in ((row.dispatch_ns, 1), (row.terminal_ns, -1))
    )
    for _, change in intervals:
        active += change
        _require(active <= peak_active, "observed active overlap")
    expected_aggregates = {
        "outcomes": dict(Counter(row.outcome for row in records)),
        "offered_count": len(records),
        "arrivals_observed_count": sum(
            row.arrival_observed_ns is not None for row in records
        ),
        "dispatched_count": sum(row.attempts for row in records),
    }
    for name, expected in expected_aggregates.items():
        _equal(value[name], expected, "recomputed population aggregates")
    return EvaluationResult(
        **{
            key: item
            for key, item in value.items()
            if key not in aggregate_fields and key != "records"
        },
        records=records,
    )


def _events(
    raw: Any, config: TrialConfig, status: str, elapsed: int
) -> tuple[FaultEvent, ...]:
    permitted: tuple[str, ...] = ("WARMUP_PASSED", "WARMUP_FAILED")
    if isinstance(config, RoutingFaultConfig):
        permitted += (
            "FREEZE_STARTED",
            "BACKGROUND_STARTED",
            "TELEMETRY_RESTORED",
            "BACKGROUND_STOP_REQUESTED",
            "RESTORED_DURING_CLEANUP",
        )
    events: list[FaultEvent] = []
    seen: set[str] = set()
    for item in _array(raw, 7):
        row = _closed(item, _names(FaultEvent))
        name = _literal(row["kind"], permitted)
        observed = _integer(row["observed_ns"], elapsed)
        _require(
            name not in seen and (not events or observed >= events[-1].observed_ns),
            "event order/uniqueness",
        )
        seen.add(name)
        events.append(FaultEvent(name, observed))
    _require(not {"WARMUP_PASSED", "WARMUP_FAILED"} <= seen, "warmup event exclusivity")
    if status == "COMPLETED":
        _require(
            "WARMUP_PASSED" in seen and "WARMUP_FAILED" not in seen, "completed warmup"
        )
        if isinstance(config, RoutingFaultConfig):
            _require(
                seen
                == {
                    "WARMUP_PASSED",
                    "FREEZE_STARTED",
                    "BACKGROUND_STARTED",
                    "TELEMETRY_RESTORED",
                    "BACKGROUND_STOP_REQUESTED",
                },
                "completed fault phases",
            )
    if status == "WARMUP_FAILED":
        _require(seen == {"WARMUP_FAILED"}, "failed warmup events")
    indexes = {event.kind: i for i, event in enumerate(events)}
    timing = {event.kind: event.observed_ns for event in events}
    if "WARMUP_PASSED" in seen or "WARMUP_FAILED" in seen:
        first = events[0]
        _require(
            first.kind in ("WARMUP_PASSED", "WARMUP_FAILED")
            and first.observed_ns >= config.telemetry.warmup_ns,
            "warmup schedule",
        )
    if isinstance(config, RoutingFaultConfig):
        if "FREEZE_STARTED" in seen:
            _require(
                "WARMUP_PASSED" in seen
                and indexes["WARMUP_PASSED"] < indexes["FREEZE_STARTED"]
                and timing["FREEZE_STARTED"] >= config.fault.freeze_start_ns,
                "freeze onset",
            )
        for name in (
            "BACKGROUND_STARTED",
            "TELEMETRY_RESTORED",
            "RESTORED_DURING_CLEANUP",
        ):
            if name in seen:
                _require(
                    "FREEZE_STARTED" in seen
                    and indexes[name] > indexes["FREEZE_STARTED"],
                    "fault lifecycle",
                )
        _require(
            not {"TELEMETRY_RESTORED", "RESTORED_DURING_CLEANUP"} <= seen,
            "restoration uniqueness",
        )
        if "FREEZE_STARTED" in seen:
            _require(
                bool({"TELEMETRY_RESTORED", "RESTORED_DURING_CLEANUP"} & seen),
                "restoration cleanup",
            )
        if "TELEMETRY_RESTORED" in seen:
            _require(
                "BACKGROUND_STARTED" in seen
                and indexes["BACKGROUND_STARTED"] < indexes["TELEMETRY_RESTORED"]
                and timing["TELEMETRY_RESTORED"] >= config.fault.restore_ns,
                "restore schedule",
            )
        if "BACKGROUND_STOP_REQUESTED" in seen:
            _require(
                "TELEMETRY_RESTORED" in seen
                and indexes["BACKGROUND_STOP_REQUESTED"] > indexes["TELEMETRY_RESTORED"]
                and timing["BACKGROUND_STOP_REQUESTED"]
                >= config.fault.background_stop_ns,
                "background stop schedule",
            )
    return tuple(events)


def _observations(
    raw: Any, config: TrialConfig, events: tuple[FaultEvent, ...], elapsed: int
) -> tuple[ObservationRecord, ...]:
    result: list[ObservationRecord] = []
    previous: dict[tuple[str, str], ObservationRecord] = {}
    timing = {event.kind: event.observed_ns for event in events}
    freeze = timing.get("FREEZE_STARTED")
    restore = timing.get("TELEMETRY_RESTORED", timing.get("RESTORED_DURING_CLEANUP"))
    for item in _array(raw, config.telemetry.max_observations):
        row = _closed(item, _names(ObservationRecord))
        endpoint = _literal(row["endpoint_id"], _ENDPOINTS)
        channel = _literal(
            row["channel"], ("HEALTH", "ROUTER_LOAD", "INDEPENDENT_LOAD")
        )
        status = _literal(row["status"], SAMPLE_STATUSES)
        seq = _integer(row["sequence"], config.telemetry.max_observations)
        started, completed, published = (
            _integer(row[name], elapsed)
            for name in ("started_ns", "completed_ns", "published_ns")
        )
        _require(started <= completed <= published, "observation timestamp order")
        _require(
            started
            < config.foreground.bounds.duration_ns + config.foreground.bounds.drain_ns,
            "acquisition before polling horizon",
        )
        _require(
            not result or published >= result[-1].published_ns,
            "observation trace order",
        )
        old = previous.get((endpoint, channel))
        _require(
            seq == (0 if old is None else old.sequence + 1),
            "observation sequence coverage",
        )
        _require(
            old is None or started >= old.completed_ns, "non-overlapping acquisitions"
        )
        _require(
            old is None
            or started
            >= (old.published_ns // config.telemetry.interval_ns + 1)
            * config.telemetry.interval_ns,
            "observation polling cadence",
        )
        count = _optional_integer(row["response_body_bytes"])
        _require(
            status != "VALID" or count is not None, "valid response byte provenance"
        )
        if count is not None and count > config.telemetry.max_response_bytes:
            _require(status == "MALFORMED", "oversize response classification")
        publication = row["published_to_router"]
        if channel == "INDEPENDENT_LOAD":
            _require(
                publication is None and row["healthy"] is None, "observer separation"
            )
        elif channel == "HEALTH":
            _require(
                publication is True and row["running"] is row["waiting"] is None,
                "health publication",
            )
            _require(
                (status == "VALID" and row["healthy"] is True)
                or (status != "VALID" and row["healthy"] is None),
                "health status",
            )
        else:
            _require(
                type(publication) is bool and row["healthy"] is None,
                "router load publication",
            )
            target = (
                isinstance(config, RoutingFaultConfig)
                and endpoint == config.fault.target_endpoint_id
            )
            if not target or freeze is None or published < freeze:
                _require(publication is True, "unfrozen publication")
            elif restore is None or published < restore:
                if published > freeze:
                    _require(publication is False, "frozen publication")
            elif published == restore and freeze < restore and started < restore:
                _require(publication is False, "old acquisition at restoration")
            elif published > restore:
                _require(
                    publication == (started >= restore), "restored acquisition cutoff"
                )
        if channel != "HEALTH":
            if status == "VALID" and publication is not False:
                _integer(row["running"], MAX_LOAD_COUNT)
                _integer(row["waiting"], MAX_LOAD_COUNT)
            else:
                _require(
                    row["running"] is row["waiting"] is None,
                    "failed/suppressed load values",
                )
        observation = ObservationRecord(**row)
        previous[(endpoint, channel)] = observation
        result.append(observation)
    return tuple(result)


def _sample(raw: Any, health: bool) -> HealthObservation | LoadObservation | None:
    if raw is None:
        return None
    cls = HealthObservation if health else LoadObservation
    row = _closed(raw, _names(cls))
    return cls(**row)


def _snapshot(raw: Any) -> RouterSnapshot:
    value = _closed(raw, {"endpoints"})
    endpoints: list[EndpointSnapshot] = []
    for item in _array(value["endpoints"], 2):
        row = _closed(item, _names(EndpointSnapshot))
        endpoints.append(
            EndpointSnapshot(
                row["endpoint_id"],
                cast(HealthObservation | None, _sample(row["health"], True)),
                cast(LoadObservation | None, _sample(row["load"], False)),
                cast(LoadObservation | None, _sample(row["last_load_attempt"], False)),
            )
        )
    _require(len(endpoints) == 2, "two endpoint snapshot")
    return RouterSnapshot((endpoints[0], endpoints[1]))


def _visible_snapshot(states: dict[str, dict[str, Any]]) -> RouterSnapshot:
    return _snapshot({"endpoints": [states[endpoint] for endpoint in _ENDPOINTS]})


def _publish(states: dict[str, dict[str, Any]], row: ObservationRecord) -> None:
    if row.published_to_router is not True:
        return
    common = {
        name: getattr(row, name)
        for name in ("sequence", "started_ns", "completed_ns", "published_ns", "status")
    }
    target = states[row.endpoint_id]
    if row.channel == "HEALTH":
        target["health"] = {**common, "healthy": row.healthy}
    else:
        sample = {**common, "running": row.running, "waiting": row.waiting}
        target["last_load_attempt"] = sample
        if row.status == "VALID":
            target["load"] = sample


def _warmup_provenance(
    observations: tuple[ObservationRecord, ...],
    config: TrialConfig,
    event: FaultEvent,
) -> int:
    """Replay one coherent trace prefix, including failures replacing health.

    Equal-clock publications may surround the synchronous warmup check. Load
    failures retain the last valid sample; health failures replace health.
    """
    latest: dict[tuple[str, str], ObservationRecord] = {}

    def publish(row: ObservationRecord) -> None:
        if row.channel == "HEALTH" or (
            row.status == "VALID" and row.published_to_router is not False
        ):
            latest[row.endpoint_id, row.channel] = row

    def ready() -> bool:
        for endpoint in _ENDPOINTS:
            for channel in ("HEALTH", "ROUTER_LOAD", "INDEPENDENT_LOAD"):
                row = latest.get((endpoint, channel))
                freshness = (
                    config.telemetry.health_freshness_ns
                    if channel == "HEALTH"
                    else config.telemetry.load_freshness_ns
                )
                if (
                    row is None
                    or row.status != "VALID"
                    or (channel == "HEALTH" and row.healthy is not True)
                    or not 0 <= event.observed_ns - row.started_ns <= freshness
                ):
                    return False
        return True

    cursor = 0
    while (
        cursor < len(observations)
        and observations[cursor].published_ns < event.observed_ns
    ):
        publish(observations[cursor])
        cursor += 1
    expected = event.kind == "WARMUP_PASSED"
    while ready() != expected:
        _require(
            cursor < len(observations)
            and observations[cursor].published_ns == event.observed_ns,
            "warmup observation provenance",
        )
        publish(observations[cursor])
        cursor += 1
    return cursor


def _population_start(population: EvaluationResult, start: int | None) -> None:
    if start is None:
        _require(
            population.cancelled
            and all(
                row.outcome == "CANCELLED"
                and row.arrival_observed_ns is None
                and row.endpoint_id is None
                and row.attempts == 0
                for row in population.records
            ),
            "unstarted population",
        )
    else:
        _require(
            all(
                row.arrival_observed_ns is None or row.arrival_observed_ns >= start
                for row in population.records
            ),
            "population after actual start",
        )


def _decisions(
    raw: Any,
    config: TrialConfig,
    foreground: EvaluationResult,
    observations: tuple[ObservationRecord, ...],
    minimum_observation_prefix: int,
) -> tuple[PolicyDecision, ...]:
    policy = RoutingPolicy(
        config.policy_id,
        health_freshness_ns=config.telemetry.health_freshness_ns,
        load_freshness_ns=config.telemetry.load_freshness_ns,
    )
    states = {
        endpoint: {
            "endpoint_id": endpoint,
            "health": None,
            "load": None,
            "last_load_attempt": None,
        }
        for endpoint in _ENDPOINTS
    }
    for observation in observations[:minimum_observation_prefix]:
        _publish(states, observation)
    cursor = minimum_observation_prefix
    decisions: list[PolicyDecision] = []
    seen: set[int] = set()
    for item in _array(raw, len(config.foreground.offers)):
        row = _closed(item, _names(PolicyDecision))
        index = _integer(row["request_index"], len(config.foreground.offers) - 1)
        decision_ns = _integer(row["decision_ns"], foreground.elapsed_ns)
        record = foreground.records[index]
        _require(
            index not in seen
            and record.arrival_observed_ns is not None
            and record.arrival_observed_ns <= decision_ns <= record.terminal_ns,
            "decision request/timing",
        )
        _require(
            not decisions or decision_ns >= decisions[-1].decision_ns,
            "decision chronological order",
        )
        seen.add(index)
        snapshot = _snapshot(row["snapshot"])
        while (
            cursor < len(observations)
            and observations[cursor].published_ns < decision_ns
        ):
            _publish(states, observations[cursor])
            cursor += 1
        # Equal-clock observations may precede or follow the synchronous decision.
        # The recorded snapshot must match some prefix of the actual ordered trace.
        while (
            _visible_snapshot(states) != snapshot
            and cursor < len(observations)
            and (observations[cursor].published_ns == decision_ns)
        ):
            _publish(states, observations[cursor])
            cursor += 1
        _require(_visible_snapshot(states) == snapshot, "snapshot trace provenance")
        expected = policy.decide(snapshot, request_index=index, decision_ns=decision_ns)
        _equal(row, asdict(expected), "replayed policy decision")
        _require(
            record.endpoint_id == expected.selected_endpoint_id,
            "decision endpoint binding",
        )
        if record.dispatch_ns is not None:
            _require(decision_ns <= record.dispatch_ns, "decision before dispatch")
        if expected.selected_endpoint_id is None:
            _require(
                record.outcome
                in ("REJECTED_ROUTE", "TIMEOUT", "DRAIN_TIMEOUT", "CANCELLED"),
                "rejected decision outcome",
            )
        decisions.append(expected)
    for request in foreground.records:
        if (
            request.attempts
            or request.outcome == "REJECTED_ROUTE"
            or request.endpoint_id is not None
        ):
            _require(request.request_index in seen, "required route decision")
        if request.outcome == "REJECTED_CAPACITY":
            _require(request.request_index not in seen, "capacity before routing")
    return tuple(decisions)


def _recovery(
    config: RoutingFaultConfig,
    events: tuple[FaultEvent, ...],
    observations: tuple[ObservationRecord, ...],
    decisions: tuple[PolicyDecision, ...],
    foreground: EvaluationResult,
    background: EvaluationResult,
) -> dict[str, Any]:
    timing = {event.kind: event.observed_ns for event in events}
    restored = timing.get("TELEMETRY_RESTORED")
    target = config.fault.target_endpoint_id
    index = 0 if target == "endpoint-a" else 1
    publications = [
        row.published_ns
        for row in observations
        if restored is not None
        and row.channel == "ROUTER_LOAD"
        and row.endpoint_id == target
        and row.published_to_router
        and row.status == "VALID"
        and row.started_ns >= restored
    ]
    usable = [
        decision
        for decision in decisions
        if restored is not None
        and decision.mode == "LOAD"
        and target in decision.eligible_endpoint_ids
        and decision.load_states[index] == "FRESH"
        and (sample := decision.snapshot.endpoints[index].load) is not None
        and sample.started_ns >= restored
    ]
    dispatches = [
        dispatch
        for decision in usable
        if (dispatch := foreground.records[decision.request_index].dispatch_ns)
        is not None
    ]
    background_dispatches = [
        row.dispatch_ns for row in background.records if row.dispatch_ns is not None
    ]
    return {
        "freeze_started_ns": timing.get("FREEZE_STARTED"),
        "telemetry_restored_ns": restored,
        "first_restored_publication_ns": min(publications, default=None),
        "first_decision_using_restored_load_ns": usable[0].decision_ns
        if usable
        else None,
        "first_dispatch_using_restored_load_ns": min(dispatches, default=None),
        "first_background_dispatch_ns": min(background_dispatches, default=None),
        "background_active_at_restore": None
        if restored is None
        else any(
            row.dispatch_ns is not None
            and row.dispatch_ns <= restored < row.terminal_ns
            for row in background.records
        ),
        "load_recovery_applicable": config.policy_id != "evaluation_round_robin_v1",
        "missing_timestamps": "UNOBSERVED_OR_CENSORED_NOT_ZERO",
    }


def validate_trial_result(
    raw_result: Any, expected_config: TrialConfig
) -> ValidatedTrialResult:
    """Validate one result and bind every population/decision to its expected config."""
    try:
        return _validate_trial_result(raw_result, expected_config)
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
        OverflowError,
    ):
        raise StudyValidationError(
            "study trial result failed closed validation"
        ) from None


def _validate_trial_result(
    raw_result: Any, config: TrialConfig
) -> ValidatedTrialResult:
    fault = isinstance(config, RoutingFaultConfig)
    _require(
        type(config) in (HealthyRoutingConfig, RoutingFaultConfig),
        "expected config type",
    )
    names = {
        "config_sha256",
        "policy_id",
        "status",
        "foreground",
        "telemetry_bounds",
        "decisions",
        "observations",
        "events",
        "elapsed_ns",
        "schema_version",
        "evidence_class",
        "evidence_eligible",
        "endpoint_identity",
        "observation_clock",
        "observer_isolation",
        "cleanup",
    }
    if fault:
        names |= {"background", "fault_schedule", "recovery", "actual_overload"}
    value = _closed(raw_result, names)
    expected_digest = sha256_digest(
        canonical_json_bytes(config.model_dump(mode="json"))
    )
    fixed = {
        "config_sha256": expected_digest,
        "policy_id": config.policy_id,
        "telemetry_bounds": config.telemetry.model_dump(),
        "schema_version": "inferdrome.evaluation-routing-result.v1"
        if fault
        else "inferdrome.evaluation-healthy-result.v1",
        "evidence_eligible": False,
        "endpoint_identity": "UNVERIFIED",
        "observation_clock": "SHARED_TRIAL_MONOTONIC_ACQUISITION_START",
        "observer_isolation": "SEPARATE_CLIENT_AND_DATAFLOW_NOT_SECURITY_SANDBOX",
        "cleanup": "PUBLICATION_RESTORED_CLIENT_TASKS_AND_CONNECTIONS_CLOSED"
        if fault
        else "CLIENT_TASKS_AND_CONNECTIONS_CLOSED",
    }
    for name, expected in fixed.items():
        _equal(value[name], expected, "trial identity/settings")
    evidence = _literal(value["evidence_class"], _EVIDENCE)
    status = _literal(value["status"], ("COMPLETED", "WARMUP_FAILED", "CANCELLED"))
    elapsed = _integer(value["elapsed_ns"])
    foreground = _population(
        value["foreground"],
        config.foreground,
        routing="INJECTED",
        evidence_class=evidence,
    )
    _require(foreground.elapsed_ns <= elapsed, "shared trial epoch")
    background = None
    if isinstance(config, RoutingFaultConfig):
        _equal(value["fault_schedule"], config.fault.model_dump(), "fault schedule")
        _equal(
            value["actual_overload"],
            "NOT_ESTABLISHED_BY_FAULT_SCHEDULE",
            "overload claim",
        )
        background = _population(
            value["background"],
            config.background,
            routing="FIXED_ASSIGNMENT",
            evidence_class=evidence,
            stop_at_ns=config.fault.background_stop_ns,
        )
        _require(background.elapsed_ns <= elapsed, "background epoch")
    events = _events(value["events"], config, status, elapsed)
    observations = _observations(value["observations"], config, events, elapsed)
    if status == "COMPLETED":
        _require(not foreground.cancelled, "completed foreground status")
    timing = {event.kind: event.observed_ns for event in events}
    _population_start(foreground, timing.get("WARMUP_PASSED"))
    if background is not None:
        _population_start(background, timing.get("BACKGROUND_STARTED"))
    warmup_prefix = 0
    for event in events:
        if event.kind in ("WARMUP_PASSED", "WARMUP_FAILED"):
            warmup_prefix = _warmup_provenance(observations, config, event)
    decisions = _decisions(
        value["decisions"], config, foreground, observations, warmup_prefix
    )
    recovery = None
    if isinstance(config, RoutingFaultConfig):
        assert background is not None
        recovery = _recovery(
            config, events, observations, decisions, foreground, background
        )
        _equal(value["recovery"], recovery, "recomputed fault recovery")
    return ValidatedTrialResult(
        expected_digest,
        sha256_digest(canonical_json_bytes(value)),
        config.policy_id,
        status,
        evidence,
        foreground,
        background,
        observations,
        decisions,
        events,
        recovery,
        elapsed,
    )


def load_trial_result_bytes(
    content: bytes, expected_config: TrialConfig
) -> ValidatedTrialResult:
    """Reject oversized or ambiguous JSON without reading files."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for name, item in items:
            _require(name not in value, "duplicate JSON keys")
            value[name] = item
        return value

    def reject_number(_value: str) -> Any:
        raise StudyValidationError("study result contains a noninteger JSON number")

    try:
        _require(
            type(content) is bytes and 1 <= len(content) <= MAX_RESULT_BYTES,
            "result byte bound",
        )
        text = content.decode("utf-8")
        validate_json_structure(text, limits=_RESULT_LIMITS)
        raw = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=reject_number,
            parse_float=reject_number,
        )
        return replace(
            validate_trial_result(raw, expected_config),
            result_sha256=sha256_digest(content),
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StudyValidationError(
            "study trial JSON failed closed validation"
        ) from None
