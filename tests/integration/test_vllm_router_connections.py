"""Connection observations distinguish real fresh/reused failures without retries."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web

from inferdrome.vllm_request_identity import REQUEST_ID_HEADER
from inferdrome.vllm_router import Router, make_app

_PROMPT = "private-connection-test-prompt"
_GENERATION = "private-connection-test-generation"
_SSE = (
    'data: {"choices":[{"index":0,"delta":{"content":"'
    + _GENERATION
    + '"},"finish_reason":null}]}\n\n'
    + 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    + "data: [DONE]\n\n"
).encode()
_HTTP_OK = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
    + f"Content-Length: {len(_SSE)}\r\n\r\n".encode()
    + _SSE
)
_DIAGNOSTIC_FIELDS = {
    "schema",
    "request_id",
    "replica",
    "stage",
    "exception_type",
    "errno",
    "upstream_status",
    "downstream_prepared",
    "elapsed_ms",
    "connection",
}


@dataclass
class _Barrier:
    """Keep both failed POSTs in flight until the fixtures consume their bodies."""

    arrivals: int = 0
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def arrive(self) -> None:
        self.arrivals += 1
        if self.arrivals == 2:
            self.release.set()
        await self.release.wait()


@dataclass
class _Replica:
    successful_posts: int
    barrier: _Barrier | None = None
    connections: int = 0
    seen: list[tuple[int, bytes]] = field(default_factory=list)
    errors: list[Exception] = field(default_factory=list)
    fault_arrived: asyncio.Event = field(default_factory=asyncio.Event)
    url: str = ""


@asynccontextmanager
async def _replica(
    successful_posts: int, barrier: _Barrier | None = None
) -> AsyncIterator[_Replica]:
    """Serve complete keepalive streams, then consume a POST and close preheader."""
    replica = _Replica(successful_posts, barrier)
    writers: set[asyncio.StreamWriter] = set()
    tasks: set[asyncio.Task[None]] = set()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        writers.add(writer)
        replica.connections += 1
        connection = replica.connections
        try:
            async with asyncio.timeout(5):
                while True:
                    try:
                        headers = await reader.readuntil(b"\r\n\r\n")
                    except asyncio.IncompleteReadError as error:
                        assert error.partial == b""
                        return
                    assert headers.startswith(b"POST /v1/chat/completions HTTP/1.1\r\n")
                    assert REQUEST_ID_HEADER.lower().encode() not in headers.lower()
                    length = next(
                        int(line.split(b":", 1)[1])
                        for line in headers.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                    replica.seen.append((connection, await reader.readexactly(length)))
                    if len(replica.seen) <= replica.successful_posts:
                        writer.write(_HTTP_OK)
                        await writer.drain()
                        continue
                    replica.fault_arrived.set()
                    if replica.barrier is not None:
                        await replica.barrier.arrive()
                    return  # Closing here leaves this POST without response headers.
        except Exception as error:
            replica.errors.append(error)
        finally:
            writer.close()
            with suppress(ConnectionResetError):
                await writer.wait_closed()
            writers.discard(writer)
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    assert server.sockets is not None
    replica.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield replica
    finally:
        if barrier is not None:
            barrier.release.set()
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if tasks:
            await asyncio.gather(*tuple(tasks), return_exceptions=True)
        assert replica.errors == []


@asynccontextmanager
async def _proxy(router: Router) -> AsyncIterator[str]:
    runner = web.AppRunner(make_app(router))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    finally:
        await runner.cleanup()


def _body(extra: str) -> bytes:
    return json.dumps(
        {"stream": True, "messages": [{"role": "user", "content": _PROMPT + extra}]},
        separators=(",", ":"),
    ).encode()


async def _post(
    client: aiohttp.ClientSession,
    url: str,
    request_id: str,
    body: bytes,
    *,
    completed: bool,
) -> None:
    async with client.post(
        url + "/v1/chat/completions",
        data=body,
        headers={REQUEST_ID_HEADER: request_id, "Content-Type": "application/json"},
    ) as response:
        assert response.headers[REQUEST_ID_HEADER] == request_id
        if completed:
            assert response.status == 200
            assert await response.read() == _SSE
        else:
            assert response.status == 502
            assert await response.json() == {"error": "replica unavailable"}


async def _assert_idle(router: Router, offered: int, errors: int) -> None:
    async with asyncio.timeout(3):
        while router.terminal != offered:
            await asyncio.sleep(0.005)
    assert router.pending == router.active == 0
    assert router.busy == [0, 0]
    assert not router.sem.locked()
    assert router.session is not None
    assert router.session.connector is not None
    assert not router.session.connector._acquired
    assert router.offered == router.terminal == router.ledger_rows == offered
    assert router.accounting_failed is False
    expected = {"completed": offered - errors, "upstream_error": errors}
    assert router.outcomes == {name: count for name, count in expected.items() if count}


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _assert_connection(
    detail: dict[str, object],
    *,
    request_id: str,
    replica: int,
    body: bytes,
    reused: bool,
) -> None:
    assert set(detail) == _DIAGNOSTIC_FIELDS
    assert detail["schema"] == "inferdrome.vllm-router-transport.v2"
    assert detail["request_id"] == request_id
    assert detail["replica"] == replica
    assert detail["stage"] == "awaiting_upstream_headers"
    assert detail["exception_type"] == "ServerDisconnectedError"
    assert detail["errno"] is None
    assert detail["upstream_status"] is None
    assert detail["downstream_prepared"] is False
    assert isinstance(detail["elapsed_ms"], (int, float))
    assert math.isfinite(detail["elapsed_ms"])
    assert detail["elapsed_ms"] >= 0
    assert detail["connection"] == {
        "new_connection_attempts": int(not reused),
        "reused_connections": int(reused),
        "queued_for_connection": False,
        "connection_ready": True,
        "request_headers_observed": True,
        "request_body_bytes_observed": len(body),
        "phase": "body_observed",
    }
    connection = detail["connection"]
    assert isinstance(connection, dict)
    for name in (
        "new_connection_attempts",
        "reused_connections",
        "request_body_bytes_observed",
    ):
        assert type(connection[name]) is int
    for name in (
        "queued_for_connection",
        "connection_ready",
        "request_headers_observed",
    ):
        assert type(connection[name]) is bool


def _assert_private_outputs(router: Router) -> None:
    text = router.ledger.read_text()
    if router.diagnostics is not None:
        text += router.diagnostics.read_text()
    assert all(value not in text for value in (_PROMPT, _GENERATION, *router.origins))
    for row in _rows(router.ledger):
        assert (
            not (_DIAGNOSTIC_FIELDS - {"request_id", "replica", "upstream_status"})
            & row.keys()
        )


@pytest.mark.parametrize("reused", [False, True], ids=["fresh", "reused"])
@pytest.mark.parametrize("diagnostics", [False, True], ids=["trace-off", "trace-on"])
def test_preheader_close_observes_fresh_or_reused_without_retry(
    tmp_path: Path, reused: bool, diagnostics: bool
) -> None:
    async def exercise() -> None:
        async with _replica(int(reused)) as bad, _replica(2) as healthy:
            router = Router(
                (bad.url, healthy.url),
                tmp_path / "ledger.jsonl",
                max_active=2,
                max_queue=0,
                request_timeout_s=3,
                diagnostics=tmp_path / "transport.jsonl" if diagnostics else None,
            )
            warm_body, fault_body, good_body = map(_body, ("warm", "fault", "good"))
            async with _proxy(router) as url, aiohttp.ClientSession() as client:
                assert router.session is not None
                assert bool(router.session.trace_configs) is diagnostics
                if reused:
                    # Complete real proxy streams create both retained pool sockets.
                    await _post(client, url, "a" * 32, warm_body, completed=True)
                    await _post(client, url, "b" * 32, warm_body, completed=True)
                await _post(client, url, "c" * 32, fault_body, completed=False)
                await _post(client, url, "d" * 32, good_body, completed=True)
                await _assert_idle(router, 4 if reused else 2, 1)
                # Socket identity proves actual reuse. Counts also exclude a retry
                # on a fresh socket or a fallback POST to the other replica.
                assert bad.connections == healthy.connections == 1
                assert bad.seen == (
                    [(1, warm_body), (1, fault_body)] if reused else [(1, fault_body)]
                )
                assert healthy.seen == (
                    [(1, warm_body), (1, good_body)] if reused else [(1, good_body)]
                )
                rows = _rows(router.ledger)
                assert [row["request_id"] for row in rows] == (
                    [letter * 32 for letter in "abcd"]
                    if reused
                    else [letter * 32 for letter in "cd"]
                )
                assert router.diagnostics_failed is False
                assert router.diagnostic_rows == int(diagnostics)
                if diagnostics:
                    assert router.diagnostics is not None
                    [detail] = _rows(router.diagnostics)
                    _assert_connection(
                        detail,
                        request_id="c" * 32,
                        replica=0,
                        body=fault_body,
                        reused=reused,
                    )
                else:
                    assert not (tmp_path / "transport.jsonl").exists()
                _assert_private_outputs(router)

    asyncio.run(exercise())


def test_concurrent_fresh_and_reused_failures_keep_independent_request_contexts(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        barrier = _Barrier()
        async with _replica(1, barrier) as reused, _replica(0, barrier) as fresh:
            router = Router(
                (reused.url, fresh.url),
                tmp_path / "ledger.jsonl",
                max_active=2,
                max_queue=0,
                request_timeout_s=3,
                diagnostics=tmp_path / "transport.jsonl",
            )
            warm_body = _body("warm")
            fresh_body = _body("fresh" + "x" * 511)
            reused_body = _body("reused" + "y" * 47)
            async with _proxy(router) as url, aiohttp.ClientSession() as client:
                await _post(client, url, "a" * 32, warm_body, completed=True)
                fresh_task = asyncio.create_task(
                    _post(client, url, "b" * 32, fresh_body, completed=False)
                )
                async with asyncio.timeout(3):
                    await fresh.fault_arrived.wait()
                assert not fresh_task.done()
                assert router.active == 1
                reused_task = asyncio.create_task(
                    _post(client, url, "c" * 32, reused_body, completed=False)
                )
                await asyncio.gather(fresh_task, reused_task)
                await _assert_idle(router, 3, 2)
                assert barrier.arrivals == 2
                assert reused.connections == fresh.connections == 1
                assert reused.seen == [(1, warm_body), (1, reused_body)]
                assert fresh.seen == [(1, fresh_body)]
                assert router.diagnostics_failed is False
                assert router.diagnostic_rows == 2
                assert router.diagnostics is not None
                details = {row["request_id"]: row for row in _rows(router.diagnostics)}
                assert set(details) == {"b" * 32, "c" * 32}
                _assert_connection(
                    details["b" * 32],
                    request_id="b" * 32,
                    replica=1,
                    body=fresh_body,
                    reused=False,
                )
                _assert_connection(
                    details["c" * 32],
                    request_id="c" * 32,
                    replica=0,
                    body=reused_body,
                    reused=True,
                )
                rows = {row["request_id"]: row for row in _rows(router.ledger)}
                assert set(rows) == {letter * 32 for letter in "abc"}
                assert rows["a" * 32]["outcome"] == "completed"
                for request_id in ("b" * 32, "c" * 32):
                    assert rows[request_id]["replica"] == details[request_id]["replica"]
                    assert rows[request_id]["outcome"] == "upstream_error"
                    assert rows[request_id]["http_status"] == 502
                    assert rows[request_id]["upstream_status"] is None
                _assert_private_outputs(router)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("completed", "diagnostics_fault"),
    [(False, "context"), (True, "context"), (False, "snapshot")],
    ids=["context-upstream-close", "context-healthy", "snapshot-upstream-close"],
)
def test_instrumentation_failure_flags_diagnostics_without_changing_outcome(
    tmp_path: Path, completed: bool, diagnostics_fault: str
) -> None:
    async def exercise() -> None:
        async with _replica(int(completed)) as first, _replica(0) as unused:
            router = Router(
                (first.url, unused.url),
                tmp_path / "ledger.jsonl",
                max_active=1,
                max_queue=0,
                request_timeout_s=3,
                diagnostics=tmp_path / "transport.jsonl",
            )
            body = _body("malformed-context")
            async with _proxy(router) as url, aiohttp.ClientSession() as client:
                assert router.session is not None
                [trace] = router.session.trace_configs
                # Corrupt instrumentation only; serving still uses real TCP.
                failure = (
                    patch.object(
                        trace,
                        "trace_config_ctx",
                        return_value=SimpleNamespace(
                            trace_request_ctx={"connection": None}
                        ),
                    )
                    if diagnostics_fault == "context"
                    else patch(
                        "inferdrome.vllm_router._ConnectionTrace.snapshot",
                        side_effect=RuntimeError("private diagnostic serialization"),
                    )
                )
                with (
                    failure,
                    patch.object(
                        router, "_record_transport", wraps=router._record_transport
                    ) as recorded,
                ):
                    await _post(client, url, "a" * 32, body, completed=completed)
                await _assert_idle(router, 1, int(not completed))
                assert first.connections == 1
                assert first.seen == [(1, body)]
                assert unused.connections == 0
                assert unused.seen == []
                assert router.diagnostics_failed is True
                assert router.diagnostic_rows == 0
                assert router.diagnostics is not None
                assert router.diagnostics.read_text() == ""
                if completed:
                    recorded.assert_not_called()
                else:
                    recorded.assert_called_once()
                    assert isinstance(
                        recorded.call_args.args[0], aiohttp.ServerDisconnectedError
                    )
                [row] = _rows(router.ledger)
                assert row["http_status"] == (200 if completed else 502)
                assert row["outcome"] == (
                    "completed" if completed else "upstream_error"
                )
                _assert_private_outputs(router)

    asyncio.run(exercise())


@dataclass
class _IdleReplica:
    """Track real socket identities and a peer's delayed idle-expiry callback."""

    connections: int = 0
    seen: list[tuple[int, bytes]] = field(default_factory=list)
    errors: list[Exception] = field(default_factory=list)
    expired: list[int] = field(default_factory=list)
    idle_ready: asyncio.Event = field(default_factory=asyncio.Event)
    release_expiry: asyncio.Event = field(default_factory=asyncio.Event)
    half_closed: asyncio.Event = field(default_factory=asyncio.Event)
    peer_closed: asyncio.Event = field(default_factory=asyncio.Event)
    url: str = ""


@asynccontextmanager
async def _idle_replica(
    *, idle_s: float, gated_expiry: bool = False
) -> AsyncIterator[_IdleReplica]:
    replica = _IdleReplica()
    writers: set[asyncio.StreamWriter] = set()
    tasks: set[asyncio.Task[None]] = set()

    async def expire(writer: asyncio.StreamWriter, connection: int) -> None:
        await asyncio.sleep(idle_s)
        if connection == 1:
            replica.idle_ready.set()
        if gated_expiry:
            await replica.release_expiry.wait()
        if not writer.is_closing():
            replica.expired.append(connection)
            # FIN leaves the receive direction open: count the actual POST body
            # while returning no response headers. This makes replay unsafe.
            writer.write_eof()
            await writer.drain()
        if connection == 1:
            replica.half_closed.set()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        writers.add(writer)
        replica.connections += 1
        connection = replica.connections
        expiry: asyncio.Task[None] | None = None
        posts = 0
        try:
            async with asyncio.timeout(5):
                while True:
                    try:
                        headers = await reader.readuntil(b"\r\n\r\n")
                    except asyncio.IncompleteReadError as error:
                        assert error.partial == b""
                        if connection == 1:
                            replica.peer_closed.set()
                        return
                    assert headers.startswith(b"POST /v1/chat/completions HTTP/1.1\r\n")
                    assert REQUEST_ID_HEADER.lower().encode() not in headers.lower()
                    length = next(
                        int(line.split(b":", 1)[1])
                        for line in headers.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                    replica.seen.append((connection, await reader.readexactly(length)))
                    posts += 1
                    if connection in replica.expired:
                        return
                    writer.write(_HTTP_OK)
                    await writer.drain()
                    if expiry is not None:
                        expiry.cancel()
                        with suppress(asyncio.CancelledError):
                            await expiry
                    expiry = asyncio.create_task(expire(writer, connection))
                    if gated_expiry and connection == 1 and posts == 1:
                        # Model an expired idle-close callback delayed until the
                        # connector selects this pooled socket, then consume the
                        # next real body only after the peer has sent FIN.
                        await replica.half_closed.wait()
        except Exception as error:
            replica.errors.append(error)
        finally:
            if expiry is not None:
                expiry.cancel()
                with suppress(asyncio.CancelledError):
                    await expiry
            writer.close()
            with suppress(ConnectionResetError):
                await writer.wait_closed()
            writers.discard(writer)
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    assert server.sockets is not None
    replica.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    successful = False
    try:
        yield replica
        successful = True
    finally:
        server.close()
        # Python 3.12 waits for accepted sockets too: stop our owned handlers
        # before waiting, including a gated peer that has not read client EOF.
        for task in tuple(tasks):
            task.cancel()
        for writer in tuple(writers):
            writer.close()
        if tasks:
            await asyncio.gather(*tuple(tasks), return_exceptions=True)
        await server.wait_closed()
        if successful:
            assert replica.errors == []


@asynccontextmanager
async def _proxy_with_idle_race(
    router: Router, replica: _IdleReplica
) -> AsyncIterator[str]:
    """Order a real peer FIN after pool selection with a public test-only hook."""
    trace = aiohttp.TraceConfig()

    async def release_expiry(
        _session: aiohttp.ClientSession, _context: SimpleNamespace, _params: object
    ) -> None:
        assert replica.idle_ready.is_set()
        replica.release_expiry.set()
        # Do not await peer closure here: the real request is allowed to write
        # its headers/body before the FIN is consumed, as in the saved fault.

    trace.on_connection_reuseconn.append(release_expiry)
    session_type = aiohttp.ClientSession

    def traced_session(*args: object, **kwargs: object) -> aiohttp.ClientSession:
        kwargs["trace_configs"] = [*kwargs.get("trace_configs", []), trace]
        return session_type(*args, **kwargs)

    runner = web.AppRunner(make_app(router))
    # Only the router's session gets the hook; downstream requests use an
    # ordinary session. No exception, ledger row, or response is injected.
    with patch("inferdrome.vllm_router.aiohttp.ClientSession", traced_session):
        await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("reuse", [True, False], ids=["pooled", "fresh"])
@pytest.mark.parametrize("diagnostics", [False, True], ids=["trace-off", "trace-on"])
def test_idle_close_after_pool_selection_has_one_post_and_fresh_mode_avoids_race(
    tmp_path: Path, reuse: bool, diagnostics: bool
) -> None:
    async def exercise() -> None:
        async with (
            _idle_replica(idle_s=0.01, gated_expiry=True) as idle,
            _replica(2) as healthy,
        ):
            router = Router(
                (idle.url, healthy.url),
                tmp_path / "ledger.jsonl",
                max_active=2,
                max_queue=0,
                request_timeout_s=3,
                upstream_keepalive_s=1.0,
                reuse_upstream_connections=reuse,
                diagnostics=tmp_path / "transport.jsonl" if diagnostics else None,
            )
            warm, other, fault = map(_body, ("warm", "other", "race" + "x" * 23_700))
            async with (
                _proxy_with_idle_race(router, idle) as url,
                aiohttp.ClientSession() as client,
            ):
                await _post(client, url, "a" * 32, warm, completed=True)
                await _post(client, url, "b" * 32, other, completed=True)
                async with asyncio.timeout(3):
                    await idle.idle_ready.wait()
                await _post(client, url, "c" * 32, fault, completed=not reuse)
                async with asyncio.timeout(3):
                    while len(idle.seen) != 2:
                        await asyncio.sleep(0.005)
                await _assert_idle(router, 3, int(reuse))
                assert idle.connections == (1 if reuse else 2)
                assert idle.seen == [(1, warm), (1 if reuse else 2, fault)]
                assert healthy.connections == 1
                assert healthy.seen == [(1, other)]
                assert idle.expired == ([1] if reuse else [])
                assert idle.release_expiry.is_set() is reuse
                assert router.diagnostics_failed is False
                assert router.diagnostic_rows == int(reuse and diagnostics)
                if diagnostics:
                    assert router.diagnostics is not None
                    details = _rows(router.diagnostics)
                    if reuse:
                        [detail] = details
                        _assert_connection(
                            detail,
                            request_id="c" * 32,
                            replica=0,
                            body=fault,
                            reused=True,
                        )
                    else:
                        assert details == []
                else:
                    assert not (tmp_path / "transport.jsonl").exists()
                async with client.get(url + "/router/stats") as response:
                    stats = await response.json()
                assert stats["upstream_connection_reuse"] is reuse
                assert stats["upstream_keepalive_s"] == (1.0 if reuse else None)
                _assert_private_outputs(router)

    asyncio.run(exercise())


@pytest.mark.parametrize("diagnostics", [False, True], ids=["trace-off", "trace-on"])
def test_configured_client_idle_retirement_precedes_peer_idle_close(
    tmp_path: Path, diagnostics: bool
) -> None:
    async def exercise() -> None:
        async with _idle_replica(idle_s=5) as idle, _replica(2) as healthy:
            router = Router(
                (idle.url, healthy.url),
                tmp_path / "ledger.jsonl",
                max_active=2,
                max_queue=0,
                upstream_keepalive_s=0.05,
                diagnostics=tmp_path / "transport.jsonl" if diagnostics else None,
            )
            warm, other, next_body = map(_body, ("warm", "other", "after-idle"))
            async with _proxy(router) as url, aiohttp.ClientSession() as client:
                await _post(client, url, "a" * 32, warm, completed=True)
                await _post(client, url, "b" * 32, other, completed=True)
                # Wait for actual client EOF at the peer, not an assumed sleep.
                async with asyncio.timeout(3):
                    await idle.peer_closed.wait()
                assert idle.expired == []
                assert not idle.idle_ready.is_set()
                await _post(client, url, "c" * 32, next_body, completed=True)
                await _assert_idle(router, 3, 0)
                assert idle.connections == 2
                assert idle.seen == [(1, warm), (2, next_body)]
                assert healthy.seen == [(1, other)]
                assert idle.expired == []
                assert router.diagnostics_failed is False
                assert router.diagnostic_rows == 0
                if diagnostics:
                    assert router.diagnostics is not None
                    assert _rows(router.diagnostics) == []
                _assert_private_outputs(router)

    asyncio.run(exercise())


@pytest.mark.parametrize("diagnostics", [False, True], ids=["trace-off", "trace-on"])
def test_pooled_connection_reuses_same_socket_inside_configured_idle_bound(
    tmp_path: Path, diagnostics: bool
) -> None:
    async def exercise() -> None:
        async with _idle_replica(idle_s=5) as idle, _replica(2) as healthy:
            router = Router(
                (idle.url, healthy.url),
                tmp_path / "ledger.jsonl",
                max_active=2,
                max_queue=0,
                upstream_keepalive_s=1.0,
                diagnostics=tmp_path / "transport.jsonl" if diagnostics else None,
            )
            warm, other, next_body = map(_body, ("warm", "other", "still-idle"))
            async with _proxy(router) as url, aiohttp.ClientSession() as client:
                await _post(client, url, "a" * 32, warm, completed=True)
                await _post(client, url, "b" * 32, other, completed=True)
                await _post(client, url, "c" * 32, next_body, completed=True)
                await _assert_idle(router, 3, 0)
                assert idle.connections == 1
                assert idle.seen == [(1, warm), (1, next_body)]
                assert healthy.seen == [(1, other)]
                assert not idle.peer_closed.is_set()
                assert idle.expired == []
                assert router.diagnostics_failed is False
                assert router.diagnostic_rows == 0
                if diagnostics:
                    assert router.diagnostics is not None
                    assert _rows(router.diagnostics) == []
                _assert_private_outputs(router)

    asyncio.run(exercise())


@pytest.mark.parametrize("diagnostics", [False, True], ids=["trace-off", "trace-on"])
def test_fresh_connection_mode_preserves_preheader_failure_without_retry(
    tmp_path: Path, diagnostics: bool
) -> None:
    async def exercise() -> None:
        async with _replica(0) as bad, _replica(0) as unused:
            router = Router(
                (bad.url, unused.url),
                tmp_path / "ledger.jsonl",
                max_active=1,
                max_queue=0,
                reuse_upstream_connections=False,
                diagnostics=tmp_path / "transport.jsonl" if diagnostics else None,
            )
            body = _body("fresh-mode-failure")
            async with _proxy(router) as url, aiohttp.ClientSession() as client:
                await _post(client, url, "a" * 32, body, completed=False)
                await _assert_idle(router, 1, 1)
                assert bad.connections == 1
                assert bad.seen == [(1, body)]
                assert unused.connections == 0
                assert unused.seen == []
                assert router.diagnostics_failed is False
                assert router.diagnostic_rows == int(diagnostics)
                if diagnostics:
                    assert router.diagnostics is not None
                    [detail] = _rows(router.diagnostics)
                    _assert_connection(
                        detail,
                        request_id="a" * 32,
                        replica=0,
                        body=body,
                        reused=False,
                    )
                _assert_private_outputs(router)

    asyncio.run(exercise())
