"""Narrow load identities, router publication isolation, and bounded HTTP probes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from typing import Any

import aiohttp
import pytest
from multidict import CIMultiDict

from inferdrome.evaluation.contracts import EvaluationError, load_config_bytes
from inferdrome.evaluation.observations import (
    MAX_LABEL_VALUE_BYTES,
    MAX_LOAD_COUNT,
    MAX_METRIC_LINE_BYTES,
    MAX_METRIC_LINES,
    MAX_METRICS_BODY_BYTES,
    AiohttpProbeTransport,
    MalformedLoad,
    MissingLoad,
    ProbeError,
    ProbeLimitError,
    ProbeResponse,
    RouterObservations,
    parse_load,
)
from inferdrome.evaluation.policies import (
    HealthObservation,
    LoadObservation,
    SampleStatus,
)

_MODEL = "Private/Model-sentinel_47"
_LABELS = f'engine="0",model_name="{_MODEL}"'
_RUNNING = "vllm:num_requests_running"
_WAITING = "vllm:num_requests_waiting"


def _gauge(name: str, value: str = "1", labels: str = _LABELS) -> bytes:
    return f"{name}{{{labels}}} {value}\n".encode()


def _body(running: str = "1", waiting: str = "2") -> bytes:
    return _gauge(_RUNNING, running) + _gauge(_WAITING, waiting)


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("0", 0),
        ("1", 1),
        ("1.0", 1),
        ("+2", 2),
        (".0", 0),
        ("1.e2", 100),
        ("1.0e+02", 100),
        (str(MAX_LOAD_COUNT), MAX_LOAD_COUNT),
    ],
)
def test_load_counts_accept_only_exact_bounded_integers(
    number: str, expected: int
) -> None:
    assert parse_load(_body(number, number), model=_MODEL) == (expected, expected)


def test_pinned_vllm_gauges_ignore_other_metrics_and_accept_label_order() -> None:
    body = (
        b"# HELP vllm:num_requests_running Running requests\r\n"
        b"# TYPE vllm:num_requests_running gauge\r\n"
        + _gauge(_RUNNING, "3.0", f'model_name="{_MODEL}",engine="0"')
        + b'vllm:num_requests_waiting_by_reason{reason="capacity",engine="8"} 99\n'
        + b'unrelated_metric{private="ignored"} 0.5\n'
        + _gauge(_WAITING, "4e0")
        + b"\n# EOF\n"
    )
    assert parse_load(body, model=_MODEL) == (3, 4)
    selected_engine = body.replace(b'engine="0"', b'engine="1"')
    assert parse_load(selected_engine, model=_MODEL, engine="1") == (3, 4)


@pytest.mark.parametrize(
    "number",
    [
        "-1",
        "-0",
        "NaN",
        "+Inf",
        "Inf",
        "-Inf",
        "1.5",
        "true",
        "1_000",
        "0x1",
        "1e-99999",
        "1e99999",
        "2147483648",
        "9" * 65,
        "1e9999999999999999999999999999999999",
        "1 1234567890",
        "1 # exemplar",
    ],
)
def test_invalid_numbers_or_server_timestamps_are_malformed(number: str) -> None:
    with pytest.raises(MalformedLoad):
        parse_load(_body(number), model=_MODEL)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"# comments only\n",
        b"unrelated_metric 42\n",
        _gauge(_RUNNING),
        _gauge(_WAITING),
        b'vllm:num_requests_waiting_by_reason{reason="unknown"} 100\n',
    ],
)
def test_absent_required_gauges_are_missing(body: bytes) -> None:
    with pytest.raises(MissingLoad):
        parse_load(body, model=_MODEL)


@pytest.mark.parametrize(
    "labels",
    [
        f'model_name="{_MODEL}"',
        'engine="0"',
        f'engine="0",engine="0",model_name="{_MODEL}"',
        f'model_name="{_MODEL}",model_name="{_MODEL}"',
        'engine="0",model_name="wrong-private-model"',
        f'engine="1",model_name="{_MODEL}"',
        f'engine="0",model_name="{_MODEL}",replica="extra"',
        f'engine="0",model_name="{_MODEL}",',
        f'engine=0,model_name="{_MODEL}"',
        f'engine="\\n",model_name="{_MODEL}"',
        'engine="0",model_name="' + "x" * (MAX_LABEL_VALUE_BYTES + 1) + '"',
    ],
)
def test_target_identity_is_exact_and_unambiguous(labels: str) -> None:
    with pytest.raises(MalformedLoad):
        parse_load(_gauge(_RUNNING, labels=labels) + _gauge(_WAITING), model=_MODEL)


@pytest.mark.parametrize(
    "extra",
    [
        _gauge(_RUNNING),
        _gauge(_WAITING),
        _gauge(_RUNNING, labels='engine="0",model_name="other"'),
    ],
)
def test_duplicate_or_other_identity_target_rows_are_not_summed(extra: bytes) -> None:
    with pytest.raises(MalformedLoad):
        parse_load(_body() + extra, model=_MODEL)


@pytest.mark.parametrize(
    "body",
    [
        f"{_RUNNING} 1\n".encode() + _gauge(_WAITING),
        f"{_RUNNING}{{{_LABELS}}}\n".encode() + _gauge(_WAITING),
        _body() + b"# invalid utf8 \xff\n",
        _body() + b"# invalid\x00text\n",
        _body() + b"# internal\rcarriage\n",
        b"#" * (MAX_METRIC_LINE_BYTES + 1),
        b"\n" * MAX_METRIC_LINES,
        b"\n" * (MAX_METRICS_BODY_BYTES + 1),
    ],
)
def test_body_line_text_and_sample_bounds_apply_before_selection(body: bytes) -> None:
    with pytest.raises(MalformedLoad):
        parse_load(body, model=_MODEL)


@pytest.mark.parametrize(
    ("model", "engine"),
    [
        ("private-secret\n", "0"),
        ("x" * 201, "0"),
        (_MODEL, "-1"),
        (_MODEL, "engine-secret"),
        (_MODEL, "2147483648"),
        (_MODEL, "0" * 11),
    ],
)
def test_parser_error_messages_do_not_echo_selected_or_raw_data(
    model: str,
    engine: str,
) -> None:
    with pytest.raises(MalformedLoad) as error:
        parse_load(_body(), model=model, engine=engine)
    assert "private" not in str(error.value).lower()
    assert _MODEL not in repr(error.value)
    assert "engine-secret" not in repr(error.value)


def _load(
    sequence: int,
    started: int,
    *,
    score: int = 1,
    status: SampleStatus = "VALID",
    completed: int | None = None,
    published: int | None = None,
) -> LoadObservation:
    return LoadObservation(
        sequence,
        started,
        started + 1 if completed is None else completed,
        started + 2 if published is None else published,
        status,
        score if status == "VALID" else None,
        0 if status == "VALID" else None,
    )


def _health(
    sequence: int, started: int, *, healthy: bool = True, status: SampleStatus = "VALID"
) -> HealthObservation:
    return HealthObservation(
        sequence,
        started,
        started + 1,
        started + 2,
        status,
        healthy if status == "VALID" else None,
    )


def test_empty_snapshots_are_immutable_and_do_not_expose_acquisition_clients() -> None:
    observations = RouterObservations()
    snapshot = observations.snapshot()
    assert tuple(endpoint.endpoint_id for endpoint in snapshot.endpoints) == (
        "endpoint-a",
        "endpoint-b",
    )
    assert all(
        endpoint.health is endpoint.load is endpoint.last_load_attempt is None
        for endpoint in snapshot.endpoints
    )
    with pytest.raises(FrozenInstanceError):
        snapshot.endpoints[0].load = _load(0, 0)  # type: ignore[misc]
    observations.publish_load("endpoint-a", _load(0, 0))
    assert snapshot.endpoints[0].load is None
    assert observations.snapshot().endpoints[0].load == _load(0, 0)


@pytest.mark.parametrize(
    "status",
    [
        "MISSING",
        "MALFORMED",
        "HTTP_ERROR",
        "TIMEOUT",
        "TRANSPORT_ERROR",
        "CANCELLED",
    ],
)
def test_bad_latest_load_retains_valid_pair_and_original_timestamps(
    status: SampleStatus,
) -> None:
    observations = RouterObservations()
    original = _load(1, 0, score=7)
    observations.publish_load("endpoint-a", original)
    failure = _load(2, 10, status=status)
    assert observations.publish_load("endpoint-a", failure)
    snapshot = observations.snapshot().endpoints[0]
    assert snapshot.load == original
    assert snapshot.last_load_attempt == failure
    assert snapshot.load.started_ns == 0


@pytest.mark.parametrize("status", ["VALID", "TIMEOUT", "MALFORMED"])
def test_health_failure_or_unhealthy_result_replaces_previous_healthy_flag(
    status: SampleStatus,
) -> None:
    observations = RouterObservations()
    observations.publish_health("endpoint-a", _health(1, 0))
    latest = _health(2, 5, healthy=False, status=status)
    observations.publish_health("endpoint-a", latest)
    assert observations.snapshot().endpoints[0].health == latest
    assert not observations.snapshot().endpoints[0].health.healthy


def test_freeze_only_target_load_while_health_and_other_endpoint_continue() -> None:
    observations = RouterObservations()
    original = _load(1, 0)
    for endpoint in ("endpoint-a", "endpoint-b"):
        observations.publish_load(endpoint, original)
        observations.publish_health(endpoint, _health(1, 0))
    observations.freeze("endpoint-a", 10)
    assert observations.frozen
    assert not observations.publish_load("endpoint-a", _load(2, 11, score=999))
    assert observations.publish_load("endpoint-b", _load(2, 11, score=9))
    observations.publish_health("endpoint-a", _health(2, 11, healthy=False))
    target, other = observations.snapshot().endpoints
    assert target.load == target.last_load_attempt == original
    assert target.health == _health(2, 11, healthy=False)
    assert other.load == _load(2, 11, score=9)


def test_restore_discards_backlog_and_requires_poll_started_at_cutoff() -> None:
    observations = RouterObservations()
    original = _load(1, 0)
    observations.publish_load("endpoint-a", original)
    observations.freeze("endpoint-a", 10)
    assert not observations.publish_load("endpoint-a", _load(2, 11, score=123))
    assert observations.restore(20)
    assert not observations.frozen
    assert observations.snapshot().endpoints[0].load == original
    # An in-flight poll finishing after restore cannot restore freshness.
    assert not observations.publish_load(
        "endpoint-a",
        _load(3, 19, score=999, completed=21, published=22),
    )
    assert observations.snapshot().endpoints[0].last_load_attempt == original
    fresh = _load(4, 20, score=4, completed=23, published=24)
    assert observations.publish_load("endpoint-a", fresh)
    assert observations.snapshot().endpoints[0].load == fresh
    # Cleanup retries must not move the actual cutoff to the later call time.
    assert not observations.restore(100)
    assert observations.publish_load("endpoint-a", _load(5, 30))


def test_suppressed_failure_attempts_and_pre_restore_polls_still_consume_sequence() -> (
    None
):
    observations = RouterObservations()
    observations.publish_load("endpoint-a", _load(1, 0))
    observations.freeze("endpoint-a", 10)
    assert not observations.publish_load("endpoint-a", _load(2, 11, status="TIMEOUT"))
    with pytest.raises(EvaluationError, match="regress"):
        observations.publish_load("endpoint-a", _load(2, 15))
    observations.restore(20)
    assert not observations.publish_load(
        "endpoint-a",
        _load(3, 19, completed=21, published=22),
    )
    with pytest.raises(EvaluationError, match="regress"):
        observations.publish_load("endpoint-a", _load(3, 25))
    assert observations.publish_load("endpoint-a", _load(4, 25))


@pytest.mark.parametrize(
    "candidate",
    [
        _load(1, 30),
        _load(0, 30),
        _load(2, 9, completed=21, published=31),
        _load(2, 10, completed=19, published=31),
        _load(2, 10, completed=20, published=29),
    ],
)
def test_load_sequences_and_all_acquisition_times_cannot_regress(
    candidate: LoadObservation,
) -> None:
    observations = RouterObservations()
    original = _load(1, 10, completed=20, published=30)
    observations.publish_load("endpoint-a", original)
    with pytest.raises(EvaluationError, match="regress"):
        observations.publish_load("endpoint-a", candidate)
    assert observations.snapshot().endpoints[0].last_load_attempt == original


def test_sequences_are_independent_per_endpoint_and_health_load_channel() -> None:
    observations = RouterObservations()
    for endpoint in ("endpoint-a", "endpoint-b"):
        observations.publish_load(endpoint, _load(1, 10))
        observations.publish_health(endpoint, _health(1, 10))
    with pytest.raises(EvaluationError, match="regress"):
        observations.publish_health("endpoint-a", _health(1, 20))
    with pytest.raises(EvaluationError, match="regress"):
        observations.publish_health("endpoint-b", _health(2, 9))
    observations.publish_health("endpoint-a", _health(2, 20))


def test_freeze_once_restore_cleanup_idempotence_and_transition_order() -> None:
    observations = RouterObservations()
    assert not observations.restore(100)
    observations.publish_load("endpoint-a", _load(1, 0))
    with pytest.raises(EvaluationError, match="timestamp"):
        observations.freeze("endpoint-a", 1)
    observations.freeze("endpoint-a", 10)
    with pytest.raises(EvaluationError, match="already used"):
        observations.freeze("endpoint-b", 10)
    with pytest.raises(EvaluationError, match="timestamp"):
        observations.restore(9)
    with pytest.raises(EvaluationError, match="regress"):
        observations.publish_load("endpoint-a", _load(2, 5))
    assert observations.frozen
    assert observations.restore(20)
    assert not observations.restore(0)
    with pytest.raises(EvaluationError, match="already used"):
        observations.freeze("endpoint-a", 30)


@pytest.mark.parametrize("now", [-1, True, 0.5, 2**53])
def test_transition_time_type_and_storage_bounds(now: object) -> None:
    with pytest.raises(EvaluationError):
        RouterObservations().freeze("endpoint-a", now)  # type: ignore[arg-type]


@pytest.mark.parametrize("operation", ["freeze", "publish_health", "publish_load"])
def test_unknown_endpoint_operations_fail_without_mutation(operation: str) -> None:
    observations = RouterObservations()
    original = observations.snapshot()
    argument = {
        "freeze": 0,
        "publish_health": _health(1, 0),
        "publish_load": _load(1, 0),
    }[operation]
    with pytest.raises(EvaluationError, match="endpoint"):
        getattr(observations, operation)("private-endpoint-sentinel", argument)
    assert observations.snapshot() == original


class _Body:
    def __init__(
        self, chunks: tuple[bytes, ...], error: Exception | None = None
    ) -> None:
        self.chunks = chunks
        self.error = error
        self.reads = 0

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        assert size <= 16_384
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.error is not None:
            raise self.error


class _Response:
    def __init__(
        self,
        *,
        status: int = 200,
        chunks: tuple[bytes, ...] = (b"ok",),
        content_length: int | None = None,
        encoding: tuple[str, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.content_length = content_length
        self.headers = CIMultiDict(("Content-Encoding", value) for value in encoding)
        self.content = _Body(chunks, error)
        self.closed = False

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, response: _Response, settings: dict[str, Any]) -> None:
        self.response = response
        self.settings = settings
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def get(self, target: str, **kwargs: object) -> _Response:
        self.requests.append((target, kwargs))
        return self.response

    async def close(self) -> None:
        await self.settings["connector"].close()
        self.closed = True


def _probe_config() -> Any:
    return load_config_bytes(
        json.dumps(
            {
                "schema_version": "inferdrome.evaluation-config.v1",
                "source_commit": "1" * 40,
                "model": _MODEL,
                "endpoints": [
                    {"endpoint_id": "endpoint-a", "origin": "http://127.0.0.1:19001"},
                    {"endpoint_id": "endpoint-b", "origin": "http://127.0.0.1:19002"},
                ],
                "bounds": {},
                "offers": [
                    {"endpoint_id": "endpoint-a", "scheduled_ns": 0, "prompt": "raw"}
                ],
            }
        ).encode()
    )


def _probe(
    monkeypatch: pytest.MonkeyPatch, response: _Response, *, bound: int = 32
) -> tuple[AiohttpProbeTransport, _Session]:
    sessions: list[_Session] = []

    def session(**kwargs: Any) -> _Session:
        created = _Session(response, kwargs)
        sessions.append(created)
        return created

    monkeypatch.setattr(
        "inferdrome.evaluation.observations.aiohttp.ClientSession", session
    )
    transport = AiohttpProbeTransport(_probe_config(), max_response_bytes=bound)
    return transport, sessions[0]


def test_probe_collects_bounded_body_and_disables_ambient_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        response = _Response(chunks=(b"a", b"b"))
        transport, session = _probe(monkeypatch, response, bound=2)
        try:
            assert await transport.get("http://127.0.0.1:19001", "/metrics") == (
                ProbeResponse(200, b"ab")
            )
            assert session.requests == [
                (
                    "http://127.0.0.1:19001/metrics",
                    {"allow_redirects": False},
                )
            ]
            assert session.settings["trust_env"] is False
            assert session.settings["auto_decompress"] is False
            assert isinstance(session.settings["cookie_jar"], aiohttp.DummyCookieJar)
            assert session.settings["connector"].limit == 6
            assert session.settings["headers"] == {
                "Accept": "text/plain",
                "Accept-Encoding": "identity",
            }
            assert response.closed
            assert "ab" not in repr(ProbeResponse(200, b"ab"))
        finally:
            await transport.close()
        assert transport.closed

    asyncio.run(exercise())


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_probe_http_failures_skip_all_raw_body_reads(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    async def exercise() -> None:
        response = _Response(
            status=status,
            chunks=(b"private-server-body",),
            error=AssertionError("error body must not be read"),
        )
        transport, session = _probe(monkeypatch, response)
        try:
            assert await transport.get("http://127.0.0.1:19002", "/health") == (
                ProbeResponse(status, b"")
            )
            assert response.content.reads == 0
            assert response.closed
            assert len(session.requests) == 1
        finally:
            await transport.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("declared", [True, False])
def test_probe_rejects_response_above_cap_without_accumulating_more(
    monkeypatch: pytest.MonkeyPatch,
    declared: bool,
) -> None:
    async def exercise() -> None:
        response = _Response(
            content_length=5 if declared else None,
            chunks=(b"12", b"345", b"never-read"),
        )
        transport, _session = _probe(monkeypatch, response, bound=4)
        try:
            with pytest.raises(ProbeLimitError):
                await transport.get("http://127.0.0.1:19001", "/metrics")
            assert response.content.reads == (0 if declared else 2)
            assert response.closed
        finally:
            await transport.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("encoding", [("gzip",), ("identity", "gzip")])
def test_probe_rejects_encoded_or_ambiguous_body(
    monkeypatch: pytest.MonkeyPatch,
    encoding: tuple[str, ...],
) -> None:
    async def exercise() -> None:
        response = _Response(encoding=encoding)
        transport, _session = _probe(monkeypatch, response)
        try:
            with pytest.raises(ProbeError, match="encoding"):
                await transport.get("http://127.0.0.1:19001", "/metrics")
            assert response.content.reads == 0
            assert response.closed
        finally:
            await transport.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("origin", "path"),
    [
        ("http://127.0.0.1:19003", "/metrics"),
        ("http://127.0.0.1:19001", "/metrics?private=query"),
        ("http://127.0.0.1:19001", "/v1/chat/completions"),
    ],
)
def test_probe_target_allowlist_checked_before_http(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    path: str,
) -> None:
    async def exercise() -> None:
        transport, session = _probe(monkeypatch, _Response())
        try:
            with pytest.raises(ProbeError):
                await transport.get(origin, path)
            assert session.requests == []
        finally:
            await transport.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure",
    [
        aiohttp.ClientPayloadError("private-server-secret"),
        OSError("private-origin-secret"),
    ],
)
def test_probe_transport_errors_are_sanitized_and_context_closes(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    async def exercise() -> None:
        response = _Response(chunks=(), error=failure)
        transport, _session = _probe(monkeypatch, response)
        try:
            with pytest.raises(ProbeError) as error:
                await transport.get("http://127.0.0.1:19001", "/metrics")
            assert "private" not in repr(error.value)
            assert error.value.__cause__ is None
            assert response.closed
        finally:
            await transport.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("bound", [0, -1, True, 1.5, MAX_METRICS_BODY_BYTES + 1])
def test_probe_invalid_response_cap_fails_before_client_creation(bound: Any) -> None:
    with pytest.raises(ProbeError, match="bound"):
        AiohttpProbeTransport(_probe_config(), max_response_bytes=bound)
