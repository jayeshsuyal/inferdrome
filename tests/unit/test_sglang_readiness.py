"""SYNTHETIC_ONLY fake/loopback probes; SGLang runtime remains UNVERIFIED."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.observations import (
    ProbeError,
    ProbeLimitError,
    ProbeResponse,
)
from inferdrome.evaluation.sglang_metrics import (
    MalformedSGLangMetrics,
    MissingSGLangMetrics,
)
from inferdrome.evaluation.sglang_readiness import (
    AiohttpSGLangProbeTransport,
    SGLangReadinessAcquisition,
    acquire_sglang_metrics,
    acquire_sglang_readiness,
    reset_sglang_cache,
)
from tests.unit.test_sglang_serving_profile import config, model_info

_DEADLINE = 1_000_000_000
_FLUSHED = (
    b"Cache flushed.\nPlease check backend logs for more details. "
    b"(When there are running or waiting requests, the operation will not be "
    b"performed.)\n"
)


def metrics(running: int = 0, queued: int = 0) -> bytes:
    labels = (
        'model_name="test/private-model",engine_type="unified",'
        'tp_rank="0",pp_rank="0",moe_ep_rank="0"'
    )
    return (
        f"sglang:num_running_reqs{{{labels}}} {running}\n"
        f"sglang:num_queue_reqs{{{labels}}} {queued}\n"
    ).encode()


class FakeProbes:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.responses: dict[str, list[ProbeResponse]] = {
            "/health": [ProbeResponse(200, b"")],
            "/health_generate": [ProbeResponse(200, b"")],
            "/model_info": [ProbeResponse(200, model_info())],
            "/metrics": [ProbeResponse(200, metrics())],
            "/flush_cache": [ProbeResponse(200, _FLUSHED)],
        }
        self.tick = 0
        self.latency = 1
        self.closed = False

    def now(self) -> int:
        return self.tick

    async def _request(self, method: str, origin: str, path: str) -> ProbeResponse:
        assert origin == config().origin
        self.calls.append((method, path))
        self.tick += self.latency
        responses = self.responses[path]
        return responses.pop(0) if len(responses) > 1 else responses[0]

    async def get(self, origin: str, path: str) -> ProbeResponse:
        return await self._request("GET", origin, path)

    async def post(self, origin: str, path: str) -> ProbeResponse:
        return await self._request("POST", origin, path)

    async def close(self) -> None:
        self.closed = True


async def ready(transport: FakeProbes) -> SGLangReadinessAcquisition:
    return await acquire_sglang_readiness(
        config(), transport, deadline_ns=_DEADLINE, now_ns=transport.now
    )


async def reset(
    transport: FakeProbes,
    readiness: SGLangReadinessAcquisition,
    *,
    drained: bool = True,
    max_age_ns: int = 100,
) -> object:
    return await reset_sglang_cache(
        config(),
        transport,
        readiness=readiness,
        locally_owned_requests_drained=drained,
        max_age_ns=max_age_ns,
        deadline_ns=_DEADLINE,
        now_ns=transport.now,
    )


def test_readiness_and_reset_record_actual_status_and_sequential_readbacks() -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        receipt = await ready(transport)
        assert receipt.generation_status == 200
        assert receipt.started_ns == 0 and receipt.completed_ns == 3
        assert receipt.readiness.identity == "SERVER_DECLARED_MATCH"
        assert transport.calls == [
            ("GET", "/health"),
            ("GET", "/health_generate"),
            ("GET", "/model_info"),
        ]
        result = await reset_sglang_cache(
            config(),
            transport,
            readiness=receipt,
            locally_owned_requests_drained=True,
            max_age_ns=100,
            deadline_ns=_DEADLINE,
            now_ns=transport.now,
        )
        assert transport.calls[3:] == [
            ("GET", "/metrics"),
            ("POST", "/flush_cache"),
            ("GET", "/health"),
            ("GET", "/model_info"),
            ("GET", "/metrics"),
        ]
        assert result.before.completed_ns == 4
        assert result.flush_completed_ns == 5
        assert result.after.completed_ns == 8
        assert result.before.counts.reported_running_requests == 0
        assert result.after.counts.reported_queued_requests == 0
        assert result.disposition == "SERVER_ACCEPTED"
        assert result.scheduler_source_age == "UNKNOWN"
        assert result.runtime_verification == "UNVERIFIED"
        assert result.evidence_eligible is False
        assert not transport.closed  # Lifecycle owner, not helpers, owns closure.

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/health", "/health_generate", "/model_info"])
@pytest.mark.parametrize("status", [199, 204, 302, 400, 503, True])
def test_readiness_requires_200_and_never_retries(path: str, status: int) -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        transport.responses[path] = [ProbeResponse(status, b"private error sentinel")]
        with pytest.raises(ProbeError) as error:
            await ready(transport)
        assert "sentinel" not in str(error.value)
        assert transport.calls[-1] == ("GET", path)
        assert transport.calls.count(("GET", path)) == 1

    asyncio.run(scenario())


def test_readiness_checks_model_before_returning_a_receipt() -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        transport.responses["/model_info"] = [
            ProbeResponse(200, model_info(served_model_name="private/wrong"))
        ]
        with pytest.raises(EvaluationError, match="readiness responses are invalid"):
            await ready(transport)

    asyncio.run(scenario())


@pytest.mark.parametrize("drained", [False, 0, 1, "true", None])
def test_reset_requires_explicit_owner_drain_before_any_probe(drained: bool) -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        receipt = await ready(transport)
        transport.calls.clear()
        with pytest.raises(EvaluationError, match="locally owned requests drained"):
            await reset(transport, receipt, drained=drained)
        assert transport.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["config", "status", "future", "unordered"])
def test_reset_requires_matching_prior_generation_readiness(change: str) -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        receipt = await ready(transport)
        if change == "config":
            receipt = replace(
                receipt,
                readiness=replace(
                    receipt.readiness, config_sha256="sha256:" + "0" * 64
                ),
            )
        elif change == "status":
            receipt = replace(receipt, generation_status=204)
        elif change == "future":
            receipt = replace(receipt, completed_ns=transport.now() + 1)
        else:
            receipt = replace(receipt, started_ns=receipt.completed_ns + 1)
        transport.calls.clear()
        with pytest.raises(EvaluationError, match="readiness binding is invalid"):
            await reset(transport, receipt)
        assert transport.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b"", MissingSGLangMetrics),
        (b"vllm:num_requests_running 0\n", MissingSGLangMetrics),
        (
            metrics().replace(b"test/private-model", b"wrong/model"),
            MalformedSGLangMetrics,
        ),
        (metrics().replace(b" 0\n", b" NaN\n"), MalformedSGLangMetrics),
        (metrics(1, 0), EvaluationError),
        (metrics(0, 1), EvaluationError),
    ],
)
def test_reset_does_not_flush_missing_malformed_or_reported_busy_metrics(
    body: bytes, error: type[Exception]
) -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        receipt = await ready(transport)
        transport.responses["/metrics"] = [ProbeResponse(200, body)]
        with pytest.raises(error):
            await reset(transport, receipt)
        assert ("POST", "/flush_cache") not in transport.calls

    asyncio.run(scenario())


def test_slow_acquisition_is_stale_and_never_becomes_idle_proof() -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        receipt = await ready(transport)
        transport.latency = 101
        with pytest.raises(EvaluationError, match="acquisition is stale"):
            await reset(transport, receipt, max_age_ns=100)
        assert ("POST", "/flush_cache") not in transport.calls

    asyncio.run(scenario())


@pytest.mark.parametrize("status,body", [(400, _FLUSHED), (200, b""), (200, b"ok")])
def test_reset_requires_pinned_server_success_without_repeating_flush(
    status: int, body: bytes
) -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        receipt = await ready(transport)
        transport.responses["/flush_cache"] = [ProbeResponse(status, body)]
        with pytest.raises(EvaluationError):
            await reset(transport, receipt)
        assert transport.calls.count(("POST", "/flush_cache")) == 1
        assert transport.calls[-1] == ("POST", "/flush_cache")

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["health", "identity", "busy", "missing"])
def test_failed_postflush_readback_returns_no_reset_receipt(failure: str) -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        receipt = await ready(transport)
        if failure == "health":
            transport.responses["/health"] = [ProbeResponse(503, b"")]
        elif failure == "identity":
            transport.responses["/model_info"] = [
                ProbeResponse(200, model_info(weight_version="changed"))
            ]
        else:
            transport.responses["/metrics"] = [
                ProbeResponse(200, metrics()),
                ProbeResponse(200, metrics(0, 1) if failure == "busy" else b""),
            ]
        with pytest.raises(EvaluationError):
            await reset(transport, receipt)
        assert transport.calls.count(("POST", "/flush_cache")) == 1
        assert transport.calls.count(("GET", "/health_generate")) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("deadline", [0, 1, 2, 3])
def test_shared_deadline_prevents_late_readiness_success(deadline: int) -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        with pytest.raises(ProbeError, match="deadline exceeded"):
            await acquire_sglang_readiness(
                config(), transport, deadline_ns=deadline, now_ns=transport.now
            )
        assert len(transport.calls) == deadline

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [True, False])
def test_deadline_or_caller_cancellation_interrupts_pending_acquisition(
    cancel: bool,
) -> None:
    class BlockingProbes(FakeProbes):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.cancelled = False

        async def get(self, origin: str, path: str) -> ProbeResponse:
            self.calls.append(("GET", path))
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

    async def scenario() -> None:
        transport = BlockingProbes()
        task = asyncio.create_task(
            acquire_sglang_readiness(
                config(),
                transport,
                deadline_ns=time.monotonic_ns()
                + (1_000_000_000 if cancel else 5_000_000),
                now_ns=time.monotonic_ns,
            )
        )
        await transport.started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ProbeError, match="deadline exceeded"):
                await task
        assert transport.cancelled
        assert transport.calls == [("GET", "/health")]
        assert not transport.closed

    asyncio.run(scenario())


def test_cancellation_during_uncertain_flush_never_retries_or_returns_receipt() -> None:
    class BlockingFlush(FakeProbes):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.cancelled = False

        async def post(self, origin: str, path: str) -> ProbeResponse:
            self.calls.append(("POST", path))
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

    async def scenario() -> None:
        transport = BlockingFlush()
        receipt = await ready(transport)
        task = asyncio.create_task(reset(transport, receipt))
        await transport.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.cancelled
        assert transport.calls[-1] == ("POST", "/flush_cache")
        assert transport.calls.count(("POST", "/flush_cache")) == 1
        assert transport.calls.count(("GET", "/health_generate")) == 1

    asyncio.run(scenario())


def test_flush_at_deadline_does_not_begin_postflush_readbacks() -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        receipt = await ready(transport)
        with pytest.raises(ProbeError, match="deadline exceeded"):
            await reset_sglang_cache(
                config(),
                transport,
                readiness=receipt,
                locally_owned_requests_drained=True,
                max_age_ns=100,
                deadline_ns=5,
                now_ns=transport.now,
            )
        assert transport.calls[-1] == ("POST", "/flush_cache")
        assert transport.calls.count(("POST", "/flush_cache")) == 1

    asyncio.run(scenario())


def test_regressing_injected_clock_cannot_complete_a_metrics_sample() -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        transport.tick, transport.latency = 10, -1
        with pytest.raises(ProbeError, match="clock regressed"):
            await acquire_sglang_metrics(
                config(), transport, deadline_ns=_DEADLINE, now_ns=transport.now
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/health", "/health_generate", "/model_info"])
def test_injected_readiness_response_still_obeys_body_bounds(path: str) -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        size = 65_537 if path == "/model_info" else 4097
        transport.responses[path] = [ProbeResponse(200, b"x" * size)]
        with pytest.raises(ProbeLimitError):
            await ready(transport)

    asyncio.run(scenario())


@pytest.mark.parametrize("origin", ["http://10.0.0.1:8001", "http://192.168.1.1:8001"])
def test_probe_boundary_rejects_nonloopback_profile_origins(origin: str) -> None:
    async def scenario() -> None:
        transport = FakeProbes()
        with pytest.raises(ProbeError, match="literal loopback"):
            await acquire_sglang_metrics(
                config(origin=origin),
                transport,
                deadline_ns=_DEADLINE,
                now_ns=transport.now,
            )
        assert transport.calls == []
        with pytest.raises(ProbeError, match="literal loopback"):
            AiohttpSGLangProbeTransport((config(origin=origin),))

    asyncio.run(scenario())


@asynccontextmanager
async def _server(
    response: bytes, requests: list[bytes]
) -> AsyncIterator[tuple[AiohttpSGLangProbeTransport, str]]:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    transport = AiohttpSGLangProbeTransport((config(origin=origin),))
    try:
        yield transport, origin
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()
        assert transport.closed


def test_loopback_uses_bodyless_post_without_environment_proxy_auth_or_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    async def scenario() -> None:
        requests: list[bytes] = []
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n"
            b"Set-Cookie: secret=value\r\nConnection: close\r\n\r\n"
        )
        async with _server(response, requests) as (transport, origin):
            assert (await transport.get(origin, "/health")).status == 200
            assert (await transport.post(origin, "/flush_cache")).status == 200
        assert len(requests) == 2
        assert requests[0].startswith(b"GET /health HTTP/1.1\r\n")
        assert requests[1].startswith(b"POST /flush_cache HTTP/1.1\r\n")
        for request in requests:
            assert b"authorization:" not in request.lower()
            assert b"cookie:" not in request.lower()
            assert b"Accept-Encoding: identity" in request
        assert b"Content-Length: 0" in requests[1]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response,error",
    [
        (b"Content-Length: 4097\r\n\r\n", ProbeLimitError),
        (
            b"Transfer-Encoding: chunked\r\n\r\n1001\r\n"
            + b"a" * 4097
            + b"\r\n0\r\n\r\n",
            ProbeLimitError,
        ),
        (b"Content-Encoding: gzip\r\nContent-Length: 0\r\n\r\n", ProbeError),
        (b"Content-Length: 4\r\n\r\na", ProbeError),
    ],
)
def test_loopback_response_bounds_encoding_and_partial_body(
    response: bytes, error: type[Exception]
) -> None:
    async def scenario() -> None:
        requests: list[bytes] = []
        async with _server(b"HTTP/1.1 200 OK\r\n" + response, requests) as (
            transport,
            origin,
        ):
            with pytest.raises(error):
                await transport.get(origin, "/health")
        assert len(requests) == 1

    asyncio.run(scenario())


def test_loopback_redirect_is_not_followed_and_error_body_is_discarded() -> None:
    async def scenario() -> None:
        requests: list[bytes] = []
        response = (
            b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/private\r\n"
            b"Content-Length: 999999999\r\n\r\nprivate-error"
        )
        async with _server(response, requests) as (transport, origin):
            result = await transport.get(origin, "/health")
            assert result == ProbeResponse(302, b"")
        assert len(requests) == 1

    asyncio.run(scenario())


def test_loopback_disconnect_does_not_trigger_implicit_get_retry() -> None:
    async def scenario() -> None:
        requests: list[bytes] = []
        async with _server(b"", requests) as (transport, origin):
            with pytest.raises(ProbeError, match="HTTP acquisition failed"):
                await transport.get(origin, "/health")
        assert len(requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/flush_cache"),
        ("POST", "/health"),
        ("GET", "/metrics?x=1"),
        ("GET", "/v1/models"),
    ],
)
def test_loopback_operation_allowlist_prevents_request(method: str, path: str) -> None:
    async def scenario() -> None:
        requests: list[bytes] = []
        async with _server(b"", requests) as (transport, origin):
            operation = transport.get if method == "GET" else transport.post
            with pytest.raises(ProbeError, match="allowlist"):
                await operation(origin, path)
            with pytest.raises(ProbeError, match="allowlist"):
                await transport.get("http://127.0.0.1:1", "/health")
        assert requests == []

    asyncio.run(scenario())


def test_transport_refuses_duplicate_and_unbounded_origins() -> None:
    for configs in ((), (config(), config()), (config(), config(), config())):
        with pytest.raises(ProbeError):
            AiohttpSGLangProbeTransport(configs)
