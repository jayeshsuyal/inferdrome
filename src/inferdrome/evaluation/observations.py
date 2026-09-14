"""Bounded vLLM load parsing and router-only observation publication.

The parser targets the vLLM 0.26.0 running/waiting gauges for one model and
engine. Router publication has no independent-observer handle or sample buffer.
All timestamps come from the caller's shared trial-relative monotonic clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol

import aiohttp

from inferdrome.evaluation.contracts import (
    EndpointId,
    EvaluationConfig,
    EvaluationError,
)
from inferdrome.evaluation.policies import (
    MAX_LOAD_COUNT,
    MAX_SAFE_INTEGER,
    EndpointSnapshot,
    HealthObservation,
    LoadObservation,
    RouterSnapshot,
)

MAX_METRICS_BODY_BYTES = 1_048_576
MAX_METRIC_LINE_BYTES = 16_384
MAX_METRIC_LINES = 16_384
MAX_LABEL_VALUE_BYTES = 256
_ENDPOINT_IDS: tuple[EndpointId, EndpointId] = ("endpoint-a", "endpoint-b")
_GAUGES = ("vllm:num_requests_running", "vllm:num_requests_waiting")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_ENGINE = re.compile(r"[0-9]{1,10}")
_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_SAMPLE = re.compile(
    r"(vllm:num_requests_(?:running|waiting))\{([^{}]*)\}[ \t]+([^ \t]+)[ \t]*"
)
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]{0,63})[ \t]*=[ \t]*"([^"\\]*)"')
_NUMBER = re.compile(r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


class MissingLoad(EvaluationError):
    """At least one required gauge was absent from an otherwise admitted body."""


class MalformedLoad(EvaluationError):
    """A load body or matching gauge violates the narrow parsing contract."""


def _labels(text: str, model: str, engine: str) -> None:
    pairs = text.split(",")
    if len(pairs) != 2:
        raise MalformedLoad("load labels violate the selected identity")
    labels: dict[str, str] = {}
    for pair in pairs:
        match = _LABEL.fullmatch(pair.strip(" \t"))
        if match is None:
            raise MalformedLoad("load labels violate the selected identity")
        name, value = match.groups()
        if name in labels or len(value.encode("utf-8")) > MAX_LABEL_VALUE_BYTES:
            raise MalformedLoad("load labels violate the selected identity")
        labels[name] = value
    if labels != {"model_name": model, "engine": engine}:
        raise MalformedLoad("load labels violate the selected identity")


def _count(text: str) -> int:
    if len(text) > 64 or _NUMBER.fullmatch(text) is None:
        raise MalformedLoad("load count violates its numeric contract")
    try:
        value = Decimal(text)
        if (
            not value.is_finite()
            or not 0 <= value <= MAX_LOAD_COUNT
            or value != value.to_integral_value()
        ):
            raise MalformedLoad("load count violates its numeric contract")
        return int(value)
    except InvalidOperation:
        raise MalformedLoad("load count violates its numeric contract") from None


def parse_load(content: bytes, *, model: str, engine: str = "0") -> tuple[int, int]:
    """Require one running/waiting sample each, with exactly model_name and engine.

    Unrelated metrics and comments are ignored within body/line limits. Matching
    samples with another identity or dimensions are malformed, never summed or
    silently selected. Server sample timestamps are rejected: freshness derives
    from local acquisition start. Only the configured safe label alphabet is
    supported; no general Prometheus expression or escape parser is provided.
    """
    if (
        not isinstance(content, bytes)
        or len(content) > MAX_METRICS_BODY_BYTES
        or not isinstance(model, str)
        or _MODEL.fullmatch(model) is None
        or not isinstance(engine, str)
        or _ENGINE.fullmatch(engine) is None
        or int(engine) > MAX_LOAD_COUNT
    ):
        raise MalformedLoad("load input violates its parsing bounds")
    lines = content.split(b"\n", MAX_METRIC_LINES)
    if len(lines) > MAX_METRIC_LINES:
        raise MalformedLoad("load input violates its parsing bounds")
    found: dict[str, int] = {}
    for raw_line in lines:
        if len(raw_line) > MAX_METRIC_LINE_BYTES:
            raise MalformedLoad("load input violates its parsing bounds")
        try:
            line = raw_line.removesuffix(b"\r").decode("utf-8")
        except UnicodeError:
            raise MalformedLoad("load input is not valid text") from None
        if "\r" in line or "\x00" in line:
            raise MalformedLoad("load input is not valid text")
        line = line.strip(" \t")
        if not line or line.startswith("#"):
            continue
        name = _NAME.match(line)
        if name is None or name.group() not in _GAUGES:
            continue
        sample = _SAMPLE.fullmatch(line)
        if sample is None:
            raise MalformedLoad("load sample violates its exposition contract")
        gauge, labels, number = sample.groups()
        if gauge in found:
            raise MalformedLoad("load sample is duplicated")
        _labels(labels, model, engine)
        found[gauge] = _count(number)
    if len(found) != len(_GAUGES):
        raise MissingLoad("required load gauges are missing")
    return found[_GAUGES[0]], found[_GAUGES[1]]


@dataclass(frozen=True)
class _Cursor:
    sequence: int
    started_ns: int
    completed_ns: int
    published_ns: int

    @classmethod
    def from_sample(cls, sample: HealthObservation | LoadObservation) -> _Cursor:
        return cls(
            sample.sequence, sample.started_ns, sample.completed_ns, sample.published_ns
        )


@dataclass
class _Published:
    health: HealthObservation | None = None
    load: LoadObservation | None = None
    last_load_attempt: LoadObservation | None = None
    health_cursor: _Cursor | None = None
    load_cursor: _Cursor | None = None


class RouterObservations:
    """Mutable two-endpoint publisher that exposes only immutable router snapshots.

    One freeze is permitted per instance. Health remains live. Suppressed load
    values are discarded, retaining only sequence/acquisition-order metadata.
    Restore never republishes a buffered sample or modifies sample timestamps;
    the target requires a newly started poll at/after the actual restore cutoff.
    """

    def __init__(self) -> None:
        self._published = {endpoint: _Published() for endpoint in _ENDPOINT_IDS}
        self._target: EndpointId | None = None
        self._frozen = False
        self._freeze_used = False
        self._restore_cutoff: int | None = None
        self._transition_ns = 0
        self._latest_publication_ns = 0

    @property
    def frozen(self) -> bool:
        return self._frozen

    def _endpoint(self, endpoint: EndpointId) -> _Published:
        if endpoint not in _ENDPOINT_IDS:
            raise EvaluationError("observation endpoint is unsupported")
        return self._published[endpoint]

    def _ordered(
        self, sample: HealthObservation | LoadObservation, previous: _Cursor | None
    ) -> _Cursor:
        if sample.published_ns < self._transition_ns or (
            previous is not None
            and (
                sample.sequence <= previous.sequence
                or sample.started_ns < previous.started_ns
                or sample.completed_ns < previous.completed_ns
                or sample.published_ns < previous.published_ns
            )
        ):
            raise EvaluationError("observation sequence or timestamps regress")
        return _Cursor.from_sample(sample)

    def publish_health(self, endpoint: EndpointId, sample: HealthObservation) -> None:
        published = self._endpoint(endpoint)
        if type(sample) is not HealthObservation:
            raise EvaluationError("health observation has the wrong type")
        cursor = self._ordered(sample, published.health_cursor)
        published.health = sample
        published.health_cursor = cursor
        self._latest_publication_ns = max(
            self._latest_publication_ns, sample.published_ns
        )

    def publish_load(self, endpoint: EndpointId, sample: LoadObservation) -> bool:
        published = self._endpoint(endpoint)
        if type(sample) is not LoadObservation:
            raise EvaluationError("load observation has the wrong type")
        cursor = self._ordered(sample, published.load_cursor)
        published.load_cursor = cursor
        self._latest_publication_ns = max(
            self._latest_publication_ns, sample.published_ns
        )
        if endpoint == self._target and (
            self._frozen
            or (
                self._restore_cutoff is not None
                and sample.started_ns < self._restore_cutoff
            )
        ):
            return False
        published.last_load_attempt = sample
        if sample.status == "VALID":
            published.load = sample
        return True

    def snapshot(self) -> RouterSnapshot:
        def endpoint_snapshot(endpoint: EndpointId) -> EndpointSnapshot:
            state = self._published[endpoint]
            return EndpointSnapshot(
                endpoint, state.health, state.load, state.last_load_attempt
            )

        return RouterSnapshot(
            (endpoint_snapshot("endpoint-a"), endpoint_snapshot("endpoint-b"))
        )

    def _transition_time(self, now_ns: int) -> None:
        if (
            type(now_ns) is not int
            or not 0 <= now_ns <= MAX_SAFE_INTEGER
            or now_ns < max(self._transition_ns, self._latest_publication_ns)
        ):
            raise EvaluationError("observation transition timestamp is invalid")

    def freeze(self, endpoint: EndpointId, now_ns: int) -> None:
        self._endpoint(endpoint)
        self._transition_time(now_ns)
        if self._freeze_used:
            raise EvaluationError("observation freeze was already used")
        self._target = endpoint
        self._frozen = self._freeze_used = True
        self._transition_ns = now_ns

    def restore(self, now_ns: int) -> bool:
        """Restore once; repeated cleanup calls return False without moving cutoff."""
        if not self._frozen:
            return False
        self._transition_time(now_ns)
        self._frozen = False
        self._restore_cutoff = self._transition_ns = now_ns
        return True


class ProbeError(EvaluationError):
    """Sanitized HTTP acquisition failure; no raw URL/body/error text is retained."""


class ProbeLimitError(ProbeError):
    """The HTTP response exceeded the declared byte ceiling."""


@dataclass(frozen=True)
class ProbeResponse:
    status: int
    body: bytes = field(repr=False)


class ProbeTransport(Protocol):
    async def get(self, origin: str, path: str) -> ProbeResponse: ...

    async def close(self) -> None: ...


class AiohttpProbeTransport:
    """Allowlisted /health and /metrics reads, with caller-owned deadlines/cleanup.

    At most six pooled connections support separate endpoint/channel probes. No
    ambient credentials, proxies, cookies, redirects, retries, or decompression.
    The caller must bound task creation and poll duration, including the pinned
    aiohttp malformed-chunk payload limitation documented for evaluation v1.
    """

    def __init__(self, config: EvaluationConfig, *, max_response_bytes: int) -> None:
        if (
            type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= MAX_METRICS_BODY_BYTES
        ):
            raise ProbeError("probe response byte bound is invalid")
        self._origins = frozenset(endpoint.origin for endpoint in config.endpoints)
        self._max_response_bytes = max_response_bytes
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=6),
            timeout=aiohttp.ClientTimeout(total=None),
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            read_bufsize=16_384,
            max_line_size=4096,
            max_field_size=4096,
            max_headers=64,
            headers={"Accept": "text/plain", "Accept-Encoding": "identity"},
        )

    async def get(self, origin: str, path: str) -> ProbeResponse:
        if (
            not isinstance(origin, str)
            or origin not in self._origins
            or path not in ("/health", "/metrics")
        ):
            raise ProbeError("probe target is outside its allowlist")
        try:
            async with self._session.get(
                origin + path, allow_redirects=False
            ) as response:
                if response.status != 200:
                    response.close()
                    return ProbeResponse(response.status, b"")
                encodings = response.headers.getall("Content-Encoding", [])
                if encodings and encodings != ["identity"]:
                    raise ProbeError("probe response encoding is unsupported")
                if (
                    response.content_length is not None
                    and response.content_length > self._max_response_bytes
                ):
                    raise ProbeLimitError("probe response exceeds its byte bound")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16_384):
                    if len(body) + len(chunk) > self._max_response_bytes:
                        raise ProbeLimitError("probe response exceeds its byte bound")
                    body.extend(chunk)
                return ProbeResponse(response.status, bytes(body))
        except ProbeError:
            raise
        except (aiohttp.ClientError, OSError, ValueError):
            raise ProbeError("probe HTTP acquisition failed") from None

    async def close(self) -> None:
        await self._session.close()

    @property
    def closed(self) -> bool:
        return self._session.closed
