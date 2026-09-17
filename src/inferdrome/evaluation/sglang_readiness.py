"""Bounded SGLang 0.5.18 probes for the caller-owned serving lifecycle.

No process ownership, retries, readiness loop, warmup or request draining lives
here. The owner supplies its absolute monotonic deadline and closes transport.
HTTP contracts are pinned in docs/SGLANG_SERVING_PREPARATION.md. Successful
readbacks remain server declarations, never GPU or cache-state qualification.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit

import aiohttp

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.observations import (
    MAX_METRICS_BODY_BYTES,
    ProbeError,
    ProbeLimitError,
    ProbeResponse,
)
from inferdrome.evaluation.policies import MAX_SAFE_INTEGER
from inferdrome.evaluation.sglang_metrics import (
    SGLangSchedulerSample,
    parse_sglang_metrics,
)
from inferdrome.evaluation.sglang_profile import (
    SglangReadiness,
    SglangServingConfig,
    build_sglang_serving_profile,
    validate_sglang_readiness,
)

_GET_LIMITS = {
    "/health": 4096,
    "/health_generate": 4096,
    "/model_info": 65_536,
    "/metrics": MAX_METRICS_BODY_BYTES,
}
_FLUSH_LIMIT = 4096
# Pinned http_server.py:892-907 returns this body only for ret.success.
_FLUSH_SUCCESS = (
    b"Cache flushed.\nPlease check backend logs for more details. "
    b"(When there are running or waiting requests, the operation will not be "
    b"performed.)\n"
)


class SGLangProbeTransport(Protocol):
    async def get(self, origin: str, path: str) -> ProbeResponse: ...

    async def post(self, origin: str, path: str) -> ProbeResponse: ...

    async def close(self) -> None: ...


def _local_config(config: SglangServingConfig) -> SglangServingConfig:
    config = build_sglang_serving_profile(config).config
    if urlsplit(config.origin).hostname != "127.0.0.1":
        raise ProbeError("SGLang probes require literal loopback origins")
    return config


class AiohttpSGLangProbeTransport:
    """1-2 owned loopback origins, five fixed paths and bounded response bytes.

    No ambient authentication, proxies, cookies, redirects, retries or automatic
    decompression. GET cannot reset state; POST accepts only a bodyless flush.
    All calls require an enclosing owner deadline; the acquisition functions
    below supply one. The owner bounds concurrency and closes this client.
    """

    def __init__(self, configs: tuple[SglangServingConfig, ...]) -> None:
        if type(configs) is not tuple or not 1 <= len(configs) <= 2:
            raise ProbeError("SGLang probe origin count is invalid")
        origins = tuple(_local_config(config).origin for config in configs)
        if len(set(origins)) != len(origins):
            raise ProbeError("SGLang probe origins must be distinct")
        self._origins = frozenset(origins)
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=2),
            timeout=aiohttp.ClientTimeout(total=None),
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            read_bufsize=16_384,
            max_line_size=4096,
            max_field_size=4096,
            max_headers=64,
            headers={"Accept": "*/*", "Accept-Encoding": "identity"},
        )
        # aiohttp 3.13 otherwise retries a disconnected idempotent GET once.
        # The lifecycle owns readiness retries and the one absolute deadline.
        self._session._retry_connection = False

    async def get(self, origin: str, path: str) -> ProbeResponse:
        return await self._request("GET", origin, path)

    async def post(self, origin: str, path: str) -> ProbeResponse:
        return await self._request("POST", origin, path)

    async def _request(self, method: str, origin: str, path: str) -> ProbeResponse:
        allowed = isinstance(path, str) and (
            path in _GET_LIMITS if method == "GET" else path == "/flush_cache"
        )
        if not isinstance(origin, str) or origin not in self._origins or not allowed:
            raise ProbeError("SGLang probe target is outside its allowlist")
        limit = _GET_LIMITS[path] if method == "GET" else _FLUSH_LIMIT
        try:
            async with self._session.request(
                method, origin + path, allow_redirects=False
            ) as response:
                if response.status != 200:
                    response.close()
                    return ProbeResponse(response.status, b"")
                encodings = response.headers.getall("Content-Encoding", [])
                if encodings and encodings != ["identity"]:
                    raise ProbeError("SGLang probe response encoding is unsupported")
                if (
                    response.content_length is not None
                    and response.content_length > limit
                ):
                    raise ProbeLimitError("SGLang probe response exceeds its bound")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16_384):
                    if len(body) + len(chunk) > limit:
                        raise ProbeLimitError("SGLang probe response exceeds its bound")
                    body.extend(chunk)
                return ProbeResponse(response.status, bytes(body))
        except ProbeError:
            raise
        except (aiohttp.ClientError, OSError, ValueError):
            raise ProbeError("SGLang probe HTTP acquisition failed") from None

    async def close(self) -> None:
        await self._session.close()

    @property
    def closed(self) -> bool:
        return self._session.closed


def _timestamp(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
        raise ProbeError("SGLang probe clock violates its bound")
    return value


def _completed(now_ns: Callable[[], int], *, started: int, deadline_ns: int) -> int:
    completed = _timestamp(now_ns())
    if completed < started:
        raise ProbeError("SGLang probe clock regressed")
    if completed >= _timestamp(deadline_ns):
        raise ProbeError("SGLang probe deadline exceeded")
    return completed


async def _acquire(
    transport: SGLangProbeTransport,
    config: SglangServingConfig,
    path: str,
    *,
    deadline_ns: int,
    now_ns: Callable[[], int],
    method: Literal["GET", "POST"] = "GET",
) -> ProbeResponse:
    started = _timestamp(now_ns())
    remaining = _timestamp(deadline_ns) - started
    if remaining <= 0:
        raise ProbeError("SGLang probe deadline exceeded")
    try:
        async with asyncio.timeout(remaining / 1_000_000_000):
            response = (
                await transport.get(config.origin, path)
                if method == "GET"
                else await transport.post(config.origin, path)
            )
    except TimeoutError:
        raise ProbeError("SGLang probe deadline exceeded") from None
    _completed(now_ns, started=started, deadline_ns=deadline_ns)
    # Injected transports must satisfy the same body and status bounds.
    if (
        type(response) is not ProbeResponse
        or type(response.status) is not int
        or response.status != 200
        or type(response.body) is not bytes
    ):
        raise ProbeError("SGLang probe did not return a valid HTTP 200 response")
    limit = _GET_LIMITS[path] if method == "GET" else _FLUSH_LIMIT
    if len(response.body) > limit:
        raise ProbeLimitError("SGLang probe response exceeds its bound")
    return response


@dataclass(frozen=True, slots=True)
class SGLangReadinessAcquisition:
    readiness: SglangReadiness
    generation_status: int
    started_ns: int
    completed_ns: int


async def acquire_sglang_readiness(
    config: SglangServingConfig,
    transport: SGLangProbeTransport,
    *,
    deadline_ns: int,
    now_ns: Callable[[], int],
) -> SGLangReadinessAcquisition:
    """One health/generation/model attempt, before warmup/drain/cache reset."""
    config = _local_config(config)
    started = _timestamp(now_ns())
    responses = []
    for path in ("/health", "/health_generate", "/model_info"):
        responses.append(
            await _acquire(
                transport, config, path, deadline_ns=deadline_ns, now_ns=now_ns
            )
        )
    health, generation, info = responses
    readiness = validate_sglang_readiness(
        config,
        health_status=health.status,
        generation_status=generation.status,
        model_info_status=info.status,
        model_info=info.body,
    )
    completed = _completed(now_ns, started=started, deadline_ns=deadline_ns)
    return SGLangReadinessAcquisition(readiness, generation.status, started, completed)


async def acquire_sglang_metrics(
    config: SglangServingConfig,
    transport: SGLangProbeTransport,
    *,
    deadline_ns: int,
    now_ns: Callable[[], int],
) -> SGLangSchedulerSample:
    """One bounded acquisition; neither a retry nor scheduler-age proof."""
    config = _local_config(config)
    started = _timestamp(now_ns())
    response = await _acquire(
        transport, config, "/metrics", deadline_ns=deadline_ns, now_ns=now_ns
    )
    sample = parse_sglang_metrics(
        response.body,
        model=config.served_model_name,
        started_ns=started,
        completed_ns=_timestamp(now_ns()),
    )
    _completed(now_ns, started=sample.completed_ns, deadline_ns=deadline_ns)
    return sample


def _reported_idle(
    sample: SGLangSchedulerSample, *, now_ns: int, max_age_ns: int
) -> None:
    counts = sample.available_at(now_ns=now_ns, max_age_ns=max_age_ns)
    if counts is None:
        raise EvaluationError("SGLang reset metrics acquisition is stale")
    if counts.reported_running_requests or counts.reported_queued_requests:
        raise EvaluationError("SGLang reset requires reported idle scheduler gauges")


@dataclass(frozen=True, slots=True)
class SGLangCacheReset:
    """Server accepted flush plus readbacks; no actual-idle/cache attestation."""

    readiness: SglangReadiness
    before: SGLangSchedulerSample
    after: SGLangSchedulerSample
    flush_completed_ns: int
    disposition: Literal["SERVER_ACCEPTED"] = "SERVER_ACCEPTED"
    scheduler_source_age: Literal["UNKNOWN"] = "UNKNOWN"
    runtime_verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    evidence_eligible: Literal[False] = False


async def reset_sglang_cache(
    config: SglangServingConfig,
    transport: SGLangProbeTransport,
    *,
    readiness: SGLangReadinessAcquisition,
    locally_owned_requests_drained: bool,
    max_age_ns: int,
    deadline_ns: int,
    now_ns: Callable[[], int],
) -> SGLangCacheReset:
    """Flush once after owner warmup/drain, then non-generating readbacks.

    The drain flag is an owner assertion about its own request tasks. Freshly
    acquired zero gauges do not prove server idle: their source age is unknown.
    The pinned server's successful flush response is the authoritative server
    acceptance. An error/cancellation returns no reset receipt and never retries
    an uncertain mutation. The owner must prevent new offers for this sequence.
    """
    config = _local_config(config)
    profile = build_sglang_serving_profile(config)
    if locally_owned_requests_drained is not True:
        raise EvaluationError("SGLang reset requires locally owned requests drained")
    _timestamp(max_age_ns)
    if max_age_ns == 0:
        raise EvaluationError("SGLang reset metric age bound must be positive")
    if (
        type(readiness) is not SGLangReadinessAcquisition
        or type(readiness.readiness) is not SglangReadiness
        or readiness.readiness != SglangReadiness(profile.config_sha256)
        or type(readiness.generation_status) is not int
        or readiness.generation_status != 200
        or not (
            _timestamp(readiness.started_ns)
            <= _timestamp(readiness.completed_ns)
            <= _timestamp(now_ns())
        )
    ):
        raise EvaluationError("SGLang reset readiness binding is invalid")
    before = await acquire_sglang_metrics(
        config, transport, deadline_ns=deadline_ns, now_ns=now_ns
    )
    _reported_idle(before, now_ns=now_ns(), max_age_ns=max_age_ns)
    response = await _acquire(
        transport,
        config,
        "/flush_cache",
        method="POST",
        deadline_ns=deadline_ns,
        now_ns=now_ns,
    )
    if response.body != _FLUSH_SUCCESS:
        raise EvaluationError("SGLang flush response violates the pinned contract")
    flush_completed = _timestamp(now_ns())
    health = await _acquire(
        transport, config, "/health", deadline_ns=deadline_ns, now_ns=now_ns
    )
    info = await _acquire(
        transport, config, "/model_info", deadline_ns=deadline_ns, now_ns=now_ns
    )
    # Generation status is the actual earlier probe result, never a new probe
    # after reset or a fabricated replacement for a missing readiness response.
    checked = validate_sglang_readiness(
        config,
        health_status=health.status,
        generation_status=readiness.generation_status,
        model_info_status=info.status,
        model_info=info.body,
    )
    after = await acquire_sglang_metrics(
        config, transport, deadline_ns=deadline_ns, now_ns=now_ns
    )
    _reported_idle(after, now_ns=now_ns(), max_age_ns=max_age_ns)
    _completed(now_ns, started=after.completed_ns, deadline_ns=deadline_ns)
    return SGLangCacheReset(checked, before, after, flush_completed)
