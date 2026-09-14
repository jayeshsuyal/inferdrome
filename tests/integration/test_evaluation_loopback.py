"""Real local HTTP/SSE rehearsals; these are not GPU performance evidence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

import pytest

from inferdrome.evaluation.contracts import EvaluationConfig, load_config_bytes
from inferdrome.evaluation.runner import EvaluationResult, run_evaluation
from inferdrome.evaluation.transport import AiohttpTransport

_PROMPT = "PRIVATE_PROMPT_SENTINEL_12ef"
_MODEL = "PRIVATE_MODEL_SENTINEL_abc9"
_OUTPUT = "PRIVATE_OUTPUT_SENTINEL_34ea_🌊"
_SERVER_ERROR = "PRIVATE_SERVER_ERROR_SENTINEL_56fc"
_GUARD_SECONDS = 5


def _event(value: object) -> bytes:
    return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\r\n\r\n"


def _choice(content: str | None = None, finish: str | None = None) -> bytes:
    return _event(
        {
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": finish}
            ]
        }
    )


_METADATA = _event(
    {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
) + _choice("")
_CONTENT = _choice(_OUTPUT)
_FINISH = _choice(finish="stop")
_USAGE = _event(
    {
        "choices": [],
        "usage": {"prompt_tokens": 4, "completion_tokens": 7, "total_tokens": 11},
    }
)
_DONE = b"data: [DONE]\r\n\r\n"
_SUCCESS = _CONTENT + _FINISH + _USAGE + _DONE


@dataclass
class _Request:
    endpoint: int
    target: str
    headers: dict[str, str]
    body: dict[str, object]


_Handler = Callable[
    [_Request, asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]


@dataclass
class _Pair:
    origins: tuple[str, str]
    requests: list[_Request]
    handlers: set[asyncio.Task[None]]
    errors: list[Exception] = field(default_factory=list)


@asynccontextmanager
async def _servers(handler: _Handler) -> AsyncIterator[_Pair]:
    """Own both listeners, every accepted writer, and all bounded handler tasks."""
    tasks: set[asyncio.Task[None]] = set()
    writers: set[asyncio.StreamWriter] = set()
    requests: list[_Request] = []
    errors: list[Exception] = []
    servers: list[asyncio.Server] = []

    async def serve(
        endpoint: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writers.add(writer)
        try:
            async with asyncio.timeout(_GUARD_SECONDS):
                header = await reader.readuntil(b"\r\n\r\n")
                lines = header.decode("ascii").split("\r\n")
                method, target, version = lines[0].split(" ")
                assert method == "POST"
                assert version == "HTTP/1.1"
                headers = dict(
                    line.lower().split(": ", 1) for line in lines[1:] if line
                )
                length = int(headers["content-length"])
                assert 0 < length <= 262_144
                body = json.loads(await reader.readexactly(length))
                request = _Request(endpoint, target, headers, body)
                requests.append(request)
                await handler(request, reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            # Tests deliberately close/reset client and server connections.
            pass
        except Exception as error:
            errors.append(error)
        finally:
            writer.close()
            with suppress(ConnectionError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)
            writers.discard(writer)

    def accept(
        endpoint: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.create_task(serve(endpoint, reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        for endpoint in range(2):

            def accepted(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
                endpoint: int = endpoint,
            ) -> None:
                accept(endpoint, reader, writer)

            servers.append(
                await asyncio.start_server(
                    accepted,
                    "127.0.0.1",
                    0,
                    limit=65_536,
                )
            )
        origins = tuple(
            f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            for server in servers
        )
        assert len(origins) == 2
        yield _Pair((origins[0], origins[1]), requests, tasks, errors)
    finally:
        for server in servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in servers))
        for writer in tuple(writers):
            writer.close()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=1)
            for task in pending:
                task.cancel()
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                1,
            )
        assert not tasks, "loopback handler task leaked"
        assert not writers, "loopback accepted writer leaked"
        assert not errors, f"loopback handler failed: {type(errors[0]).__name__}"


def _config(
    pair: _Pair, *, offers: list[dict[str, object]] | None = None, **bounds: int
) -> EvaluationConfig:
    return load_config_bytes(
        json.dumps(
            {
                "schema_version": "inferdrome.evaluation-config.v1",
                "source_commit": "1" * 40,
                "model": _MODEL,
                "max_tokens": 32,
                "endpoints": [
                    {"endpoint_id": f"endpoint-{letter}", "origin": origin}
                    for letter, origin in zip("ab", pair.origins, strict=True)
                ],
                "bounds": {
                    "max_requests": 20,
                    "concurrency": 2,
                    "max_queue": 2,
                    "duration_ns": 2_000_000_000,
                    "request_timeout_ns": 2_000_000_000,
                    "drain_ns": 1_000_000_000,
                    **bounds,
                },
                "offers": offers
                or [
                    {"scheduled_ns": 0, "endpoint_id": "endpoint-a", "prompt": _PROMPT}
                ],
            }
        ).encode()
    )


async def _headers(
    writer: asyncio.StreamWriter,
    *,
    status: int = 200,
    content_type: str = "text/event-stream",
    extra: bytes = b"",
) -> None:
    writer.write(
        f"HTTP/1.1 {status} Test\r\nContent-Type: {content_type}\r\n".encode()
        + b"Connection: close\r\n"
        + extra
        + b"\r\n"
    )
    await writer.drain()


async def _response(
    writer: asyncio.StreamWriter,
    body: bytes = _SUCCESS,
    *,
    status: int = 200,
    content_type: str = "text/event-stream",
    extra: bytes = b"",
) -> None:
    await _headers(
        writer,
        status=status,
        content_type=content_type,
        extra=f"Content-Length: {len(body)}\r\n".encode() + extra,
    )
    writer.write(body)
    await writer.drain()


def _assert_sanitized(result: EvaluationResult, pair: _Pair) -> None:
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)
    for secret in (_PROMPT, _MODEL, _OUTPUT, _SERVER_ERROR, *pair.origins):
        assert secret not in serialized
    assert result.evidence_class == "LOCAL_MEASUREMENT_ONLY"
    assert not result.evidence_eligible
    assert result.exact_token_timing == "UNAVAILABLE"
    assert result.wire_first_response_byte == "UNAVAILABLE"
    assert result.cleanup == "CLIENT_TASKS_AND_CONNECTIONS_CLOSED"
    assert len(result.records) == result.to_dict()["offered_count"]
    assert len({record.request_index for record in result.records}) == len(
        result.records
    )


def test_real_concurrent_requests_overlap_and_send_explicit_streaming_policy() -> None:
    async def exercise() -> None:
        both_started = asyncio.Event()
        started: set[int] = set()

        async def handler(
            request: _Request,
            _reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            started.add(request.endpoint)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            await _response(writer)

        async with _servers(handler) as pair:
            config = _config(
                pair,
                offers=[
                    {
                        "scheduled_ns": 0,
                        "endpoint_id": f"endpoint-{letter}",
                        "prompt": _PROMPT,
                    }
                    for letter in "ab"
                ],
            )
            transport = AiohttpTransport(config)
            result = await asyncio.wait_for(
                run_evaluation(config, transport), _GUARD_SECONDS
            )
            assert transport.closed
            assert both_started.is_set()
            assert result.peak_active == 2
            assert result.peak_queue == 0
            assert [record.outcome for record in result.records] == ["SUCCESS"] * 2
            assert {request.endpoint for request in pair.requests} == {0, 1}
            for request in pair.requests:
                assert request.target == "/v1/chat/completions"
                assert request.headers["accept"] == "text/event-stream"
                assert request.headers["accept-encoding"] == "identity"
                assert request.body == {
                    "model": _MODEL,
                    "messages": [{"role": "user", "content": _PROMPT}],
                    "max_tokens": 32,
                    "temperature": 0,
                    "n": 1,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "chat_template_kwargs": {"enable_thinking": False},
                }
            for record in result.records:
                assert (
                    record.dispatch_lag_ns == record.dispatch_ns - record.scheduled_ns
                )
                assert (
                    record.dispatch_ns
                    <= record.response_headers_ns
                    <= record.first_body_byte_ns
                    <= record.first_content_ns
                    <= record.protocol_done_ns
                    <= record.terminal_ns
                )
                assert record.completion_tokens == 7
                assert record.prompt_tokens == 4
                assert record.usage_provenance == "SERVER_REPORTED_STREAM_USAGE"
            _assert_sanitized(result, pair)

    asyncio.run(exercise())


def test_chunked_http_split_utf8_frames_and_metadata_precede_content() -> None:
    async def exercise() -> None:
        received = asyncio.Event()
        callbacks: list[bytes] = []
        unicode_start = _CONTENT.index("🌊".encode())
        # Split inside a UTF-8 character and inside CRLF/frame terminators.
        fragments = [
            _METADATA,
            _CONTENT[: unicode_start + 1],
            _CONTENT[unicode_start + 1 : -3],
            _CONTENT[-3:-1],
            _CONTENT[-1:],
            _FINISH[:-1],
            _FINISH[-1:],
            _USAGE,
            _DONE,
        ]

        class ObservedTransport(AiohttpTransport):
            async def stream(
                self,
                origin: str,
                body: bytes,
                on_headers: Callable[[int], None],
                on_bytes: Callable[[bytes], None],
            ) -> None:
                def observe(data: bytes) -> None:
                    callbacks.append(data)
                    on_bytes(data)
                    received.set()

                await super().stream(origin, body, on_headers, observe)

        async def handler(
            _request: _Request,
            _reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await _headers(writer, extra=b"Transfer-Encoding: chunked\r\n")
            for fragment in fragments:
                received.clear()
                writer.write(f"{len(fragment):x}\r\n".encode())
                await writer.drain()
                writer.write(fragment + b"\r\n")
                await writer.drain()
                # Gates enforce distinct client observations without wall-time sleeps.
                await received.wait()
            writer.write(b"0\r\n\r\n")
            await writer.drain()

        async with _servers(handler) as pair:
            config = _config(pair)
            transport = ObservedTransport(config)
            result = await asyncio.wait_for(
                run_evaluation(config, transport), _GUARD_SECONDS
            )
            record = result.records[0]
            assert record.outcome == "SUCCESS"
            assert callbacks == fragments
            assert record.first_body_byte_ns < record.first_content_ns
            assert len(record.content_event_times_ns) == 1
            assert record.completion_tokens == 7  # One event aggregated seven tokens.
            assert record.protocol_done_ns < record.terminal_ns
            assert transport.closed
            _assert_sanitized(result, pair)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("body", "outcome", "usage"),
    [
        (_CONTENT + _FINISH + _DONE, "SUCCESS", "UNAVAILABLE"),
        (_METADATA, "INCOMPLETE_STREAM", "UNAVAILABLE"),
        (_CONTENT + _FINISH, "INCOMPLETE_STREAM", "UNAVAILABLE"),
        (_SUCCESS[:-1], "INCOMPLETE_STREAM", "SERVER_REPORTED_STREAM_USAGE"),
        (b"data: {bad json}\n\n", "STREAM_ERROR", "UNAVAILABLE"),
        (b"data: \xff\n\n", "STREAM_ERROR", "UNAVAILABLE"),
        (_event({"error": {"message": _SERVER_ERROR}}), "STREAM_ERROR", "UNAVAILABLE"),
        (
            b"event: error\ndata: " + _SERVER_ERROR.encode() + b"\n\n",
            "STREAM_ERROR",
            "UNAVAILABLE",
        ),
        (_SUCCESS + _CONTENT, "STREAM_ERROR", "SERVER_REPORTED_STREAM_USAGE"),
    ],
    ids=[
        "optional-usage",
        "metadata-only",
        "missing-done",
        "partial-final-delimiter",
        "malformed-json",
        "invalid-utf8",
        "json-error",
        "sse-error",
        "data-after-done",
    ],
)
def test_real_sse_eof_and_errors_have_one_sanitized_terminal(
    body: bytes,
    outcome: str,
    usage: str,
) -> None:
    async def exercise() -> None:
        async def handler(
            _request: _Request,
            _reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await _response(writer, body)

        async with _servers(handler) as pair:
            config = _config(pair)
            transport = AiohttpTransport(config)
            result = await asyncio.wait_for(
                run_evaluation(config, transport), _GUARD_SECONDS
            )
            assert len(pair.requests) == 1
            assert result.records[0].outcome == outcome
            assert result.records[0].usage_provenance == usage
            assert result.records[0].attempts == 1
            assert transport.closed
            _assert_sanitized(result, pair)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("case", "outcome", "status"),
    [
        ("rate-limit", "HTTP_ERROR", 429),
        ("stalled-error-body", "HTTP_ERROR", 429),
        ("redirect", "HTTP_ERROR", 302),
        ("wrong-type", "STREAM_ERROR", 200),
        ("compressed", "STREAM_ERROR", 200),
        ("short-content-length", "INCOMPLETE_STREAM", 200),
        ("truncated-chunk", "INCOMPLETE_STREAM", 200),
        ("malformed-status", "TRANSPORT_ERROR", None),
        ("malformed-chunk-after-headers", "TIMEOUT", 200),
        ("disconnect", "TRANSPORT_ERROR", None),
    ],
)
def test_http_errors_framing_and_redirects_are_bounded(
    case: str,
    outcome: str,
    status: int | None,
) -> None:
    from aiohttp.http_parser import HttpResponseParser

    if (
        case == "malformed-chunk-after-headers"
        and HttpResponseParser.__module__ != "aiohttp._http_parser"
    ):
        pytest.skip("regression covers the pinned compiled HTTP parser")

    async def exercise() -> None:
        headers_observed = asyncio.Event()
        malformed_server_closed = asyncio.Event()

        class HeadersObservedTransport(AiohttpTransport):
            async def stream(
                self,
                origin: str,
                body: bytes,
                on_headers: Callable[[int], None],
                on_bytes: Callable[[bytes], None],
            ) -> None:
                def observe(status: int) -> None:
                    on_headers(status)
                    headers_observed.set()

                await super().stream(origin, body, observe, on_bytes)

        async def handler(
            request: _Request,
            _reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            assert request.endpoint == 0, "redirect was unexpectedly followed"
            if case == "rate-limit":
                await _response(writer, _SERVER_ERROR.encode(), status=429)
            elif case == "stalled-error-body":
                await _headers(writer, status=429, extra=b"Content-Length: 999999\r\n")
                assert await _reader.read() == b""
            elif case == "redirect":
                await _response(
                    writer,
                    status=302,
                    extra=f"Location: {pair.origins[1]}/private\r\n".encode(),
                )
            elif case == "wrong-type":
                await _response(writer, content_type="application/json")
            elif case == "compressed":
                await _response(writer, extra=b"Content-Encoding: gzip\r\n")
            elif case == "short-content-length":
                await _headers(
                    writer, extra=f"Content-Length: {len(_SUCCESS) + 100}\r\n".encode()
                )
                writer.write(_SUCCESS)
                await writer.drain()
            elif case == "truncated-chunk":
                await _headers(writer, extra=b"Transfer-Encoding: chunked\r\n")
                writer.write(f"{len(_SUCCESS) + 100:x}\r\n".encode() + _SUCCESS)
                await writer.drain()
            elif case == "malformed-status":
                writer.write(b"HTTP/1.1 NOT_A_STATUS\r\n\r\n")
                await writer.drain()
            elif case == "malformed-chunk-after-headers":
                await _headers(writer, extra=b"Transfer-Encoding: chunked\r\n")
                await headers_observed.wait()
                # aiohttp 3.13.5's C parser drops the rejected chunk's payload.
                # Once headers were delivered, its payload reader receives no
                # exception/EOF. The gate fixes this phase deterministically;
                # the declared request deadline still bounds client cleanup.
                writer.write(b"NOT_HEXADECIMAL\r\n" + _SUCCESS + b"0\r\n\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                malformed_server_closed.set()
            elif case == "disconnect":
                writer.transport.abort()
            else:
                raise AssertionError("unknown test scenario")

        async with _servers(handler) as pair:
            config = _config(pair)
            transport = HeadersObservedTransport(config)
            result = await asyncio.wait_for(
                run_evaluation(config, transport), _GUARD_SECONDS
            )
            assert len(pair.requests) == 1  # No retry and no redirect follow-up.
            if case == "malformed-chunk-after-headers":
                assert headers_observed.is_set()
                assert malformed_server_closed.is_set()
                assert result.records[0].terminal_ns >= config.bounds.request_timeout_ns
            assert result.records[0].outcome == outcome
            assert result.records[0].http_status == status
            assert result.records[0].attempts == 1
            assert transport.closed
            _assert_sanitized(result, pair)

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["headers", "body", "after-done"])
def test_stalled_http_deadline_closes_socket_and_keeps_partial_measurements(
    phase: str,
) -> None:
    async def exercise() -> None:
        disconnected = asyncio.Event()

        async def handler(
            _request: _Request,
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            if phase != "headers":
                await _headers(writer, extra=b"Transfer-Encoding: chunked\r\n")
                body = _SUCCESS if phase == "after-done" else _METADATA + _CONTENT
                writer.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
                await writer.drain()
            assert await reader.read() == b""
            disconnected.set()

        async with _servers(handler) as pair:
            config = _config(pair, request_timeout_ns=250_000_000)
            transport = AiohttpTransport(config)
            result = await asyncio.wait_for(
                run_evaluation(config, transport), _GUARD_SECONDS
            )
            await asyncio.wait_for(disconnected.wait(), _GUARD_SECONDS)
            record = result.records[0]
            assert record.outcome == "TIMEOUT"
            assert record.terminal_ns >= config.bounds.request_timeout_ns
            assert record.attempts == 1
            if phase == "headers":
                assert record.response_headers_ns is None
                assert record.first_body_byte_ns is None
            else:
                assert record.first_body_byte_ns is not None
                assert record.first_content_ns is not None
            assert (record.protocol_done_ns is not None) == (phase == "after-done")
            assert transport.closed
            _assert_sanitized(result, pair)

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("cancellation", ["stop-event", "task-cancel"])
def test_cancellation_closes_socket_and_terminalizes_queued_and_future_offers(
    phase: str,
    cancellation: str,
) -> None:
    async def exercise() -> None:
        ready = asyncio.Event()
        disconnected = asyncio.Event()
        stop = asyncio.Event()

        class ObservedTransport(AiohttpTransport):
            async def stream(
                self,
                origin: str,
                body: bytes,
                on_headers: Callable[[int], None],
                on_bytes: Callable[[bytes], None],
            ) -> None:
                def observe(data: bytes) -> None:
                    on_bytes(data)
                    ready.set()

                await super().stream(origin, body, on_headers, observe)

        async def handler(
            _request: _Request,
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            if phase == "body":
                await _headers(writer, extra=b"Transfer-Encoding: chunked\r\n")
                body = _METADATA + _CONTENT
                writer.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
                await writer.drain()
            else:
                ready.set()
            assert await reader.read() == b""
            disconnected.set()

        async with _servers(handler) as pair:
            config = _config(
                pair,
                concurrency=1,
                max_queue=1,
                offers=[
                    {
                        "scheduled_ns": scheduled,
                        "endpoint_id": "endpoint-a",
                        "prompt": _PROMPT,
                    }
                    for scheduled in (0, 0, 1_000_000_000)
                ],
            )
            transport = ObservedTransport(config)
            running = asyncio.create_task(run_evaluation(config, transport, stop=stop))
            await asyncio.wait_for(ready.wait(), _GUARD_SECONDS)
            if cancellation == "stop-event":
                stop.set()
            else:
                running.cancel()
            result = await asyncio.wait_for(running, _GUARD_SECONDS)
            await asyncio.wait_for(disconnected.wait(), _GUARD_SECONDS)
            assert result.cancelled
            assert [record.outcome for record in result.records] == ["CANCELLED"] * 3
            assert [record.attempts for record in result.records] == [1, 0, 0]
            assert result.records[1].arrival_observed_ns is not None
            assert result.records[2].arrival_observed_ns is None
            assert result.peak_queue == 1
            assert (result.records[0].first_content_ns is not None) == (phase == "body")
            assert transport.closed
            _assert_sanitized(result, pair)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("bounds", "body"),
    [
        ({"max_stream_bytes": 128, "max_event_bytes": 128}, b": x\n\n" * 100),
        ({"max_event_bytes": 128}, b"data: " + b"x" * 4096),
        ({"max_content_events": 1}, _CONTENT + _CONTENT + _FINISH + _DONE),
    ],
    ids=["stream-bytes", "frame-buffer", "content-timing-buffer"],
)
def test_real_stream_buffer_limits_close_connection(
    bounds: dict[str, int],
    body: bytes,
) -> None:
    async def exercise() -> None:
        disconnected = asyncio.Event()

        async def handler(
            _request: _Request,
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await _headers(writer, extra=b"Transfer-Encoding: chunked\r\n")
            writer.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
            await writer.drain()
            assert await reader.read() == b""
            disconnected.set()

        async with _servers(handler) as pair:
            config = _config(pair, **bounds)
            transport = AiohttpTransport(config)
            result = await asyncio.wait_for(
                run_evaluation(config, transport), _GUARD_SECONDS
            )
            await asyncio.wait_for(disconnected.wait(), _GUARD_SECONDS)
            assert result.records[0].outcome == "STREAM_LIMIT"
            assert len(result.records[0].content_event_times_ns) <= (
                config.bounds.max_content_events
            )
            assert transport.closed
            _assert_sanitized(result, pair)

    asyncio.run(exercise())


def test_transport_ignores_ambient_proxy_credentials_and_response_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # These are invented sentinels; no actual credential value is read.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    for name in ("OPENAI_API_KEY", "VLLM_API_KEY", "HF_TOKEN"):
        monkeypatch.setenv(name, "AMBIENT_TOKEN_SENTINEL")

    def forbidden_ambient_lookup(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ambient proxy or credential lookup attempted")

    monkeypatch.setattr(
        "aiohttp.client.get_env_proxy_for_url", forbidden_ambient_lookup
    )
    monkeypatch.setattr("aiohttp.client.netrc_from_env", forbidden_ambient_lookup)
    monkeypatch.setattr("aiohttp.helpers.netrc_from_env", forbidden_ambient_lookup)

    async def exercise() -> None:
        async def handler(
            request: _Request,
            _reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            assert "authorization" not in request.headers
            assert "proxy-authorization" not in request.headers
            assert "cookie" not in request.headers
            await _response(
                writer, extra=b"Set-Cookie: session=COOKIE_SENTINEL; Path=/\r\n"
            )

        async with _servers(handler) as pair:
            config = _config(
                pair,
                concurrency=1,
                offers=[
                    {"scheduled_ns": 0, "endpoint_id": "endpoint-a", "prompt": _PROMPT}
                    for _ in range(2)
                ],
            )
            transport = AiohttpTransport(config)
            result = await asyncio.wait_for(
                run_evaluation(config, transport), _GUARD_SECONDS
            )
            assert len(pair.requests) == 2
            assert [record.outcome for record in result.records] == ["SUCCESS"] * 2
            assert transport.closed
            assert "AMBIENT_TOKEN_SENTINEL" not in json.dumps(result.to_dict())
            assert "COOKIE_SENTINEL" not in json.dumps(result.to_dict())
            _assert_sanitized(result, pair)

    asyncio.run(exercise())
