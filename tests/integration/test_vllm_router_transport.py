"""Real loopback transport failures retain correlation without retrying POSTs."""

from __future__ import annotations

import asyncio
import errno
import json
import math
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web

from inferdrome.vllm_request_identity import REQUEST_ID_HEADER
from inferdrome.vllm_router import Router, make_app

_SCHEMA = "inferdrome.vllm-router-transport.v1"
_PROMPT = "private-prompt-must-not-enter-transport-diagnostics"
_GENERATION = "private-generation-must-not-enter-transport-diagnostics"
_FIRST_EVENT = (
    'data: {"choices":[{"index":0,"delta":{"content":"'
    + _GENERATION
    + '"},"finish_reason":null}]}\n\n'
).encode()
_SSE = (
    _FIRST_EVENT
    + b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    + b"data: [DONE]\n\n"
)
_PAYLOAD = {"stream": True, "messages": [{"role": "user", "content": _PROMPT}]}
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
}


async def _serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def _healthy_replica(seen: list[bytes]) -> web.Application:
    async def chat(request: web.Request) -> web.Response:
        # Correlation is local to client/router; it must not change upstream input.
        assert REQUEST_ID_HEADER not in request.headers
        seen.append(await request.read())
        return web.Response(body=_SSE, content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


@asynccontextmanager
async def _fault_replica(
    mode: str,
) -> AsyncIterator[tuple[str, list[bytes], asyncio.Event]]:
    """Consume a real POST, then close before headers or in a chunked body."""
    seen: list[bytes] = []
    release = asyncio.Event()
    writers: set[asyncio.StreamWriter] = set()
    tasks: set[asyncio.Task[None]] = set()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        writers.add(writer)
        try:
            async with asyncio.timeout(3):
                headers = await reader.readuntil(b"\r\n\r\n")
                assert headers.startswith(b"POST /v1/chat/completions HTTP/1.1\r\n")
                assert REQUEST_ID_HEADER.lower().encode() not in headers.lower()
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                seen.append(await reader.readexactly(length))
                if mode == "truncated_stream":
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                        b"Transfer-Encoding: chunked\r\n\r\n"
                        + f"{len(_FIRST_EVENT):x}\r\n".encode()
                        + _FIRST_EVENT
                        + b"\r\n"
                    )
                    await writer.drain()
                    # The client observes the first event before this fixture
                    # closes without the terminating HTTP chunk or SSE [DONE].
                    await release.wait()
                else:
                    assert mode == "closed_before_headers"
        finally:
            writer.close()
            await writer.wait_closed()
            writers.discard(writer)
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    assert server.sockets is not None
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield url, seen, release
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if tasks:
            await asyncio.gather(*tuple(tasks), return_exceptions=True)


async def _wait_terminal(router: Router, count: int) -> None:
    async with asyncio.timeout(3):
        while router.terminal != count:
            await asyncio.sleep(0.005)


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def _exercise_fault(
    tmp_path: Path,
    mode: str,
    bad_url: str,
    bad_seen: list[bytes],
    release: asyncio.Event,
    *,
    diagnostics_enabled: bool = True,
    fail_diagnostics_write: bool = False,
) -> None:
    healthy_seen: list[bytes] = []
    healthy, healthy_url = await _serve(_healthy_replica(healthy_seen))
    ledger = tmp_path / "ledger.jsonl"
    diagnostics = tmp_path / "transport.jsonl"
    router = Router(
        (bad_url, healthy_url),
        ledger,
        max_active=1,
        max_queue=0,
        request_timeout_s=2,
        diagnostics=diagnostics if diagnostics_enabled else None,
    )
    proxy, proxy_url = await _serve(make_app(router))
    fault_id = "a" * 32
    good_id = "b" * 32
    write_failure = None
    if fail_diagnostics_write:
        assert router._diagnostics_file is not None
        write_failure = patch.object(
            router._diagnostics_file,
            "write",
            side_effect=OSError("private transport details must not be exposed"),
        )
        write_failure.start()
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                proxy_url + "/v1/chat/completions",
                json=_PAYLOAD,
                headers={REQUEST_ID_HEADER: fault_id},
            ) as failed:
                assert failed.headers[REQUEST_ID_HEADER] == fault_id
                if mode == "truncated_stream":
                    assert failed.status == 200
                    assert await failed.content.readexactly(len(_FIRST_EVENT)) == (
                        _FIRST_EVENT
                    )
                    release.set()
                    with pytest.raises(aiohttp.ClientPayloadError):
                        await failed.read()
                else:
                    assert failed.status == 502
                    assert await failed.json() == {"error": "replica unavailable"}
            await _wait_terminal(router, 1)
            assert router.pending == router.active == 0
            assert router.busy == [0, 0]
            assert not router.sem.locked()
            assert router.session is not None
            assert not router.session.connector._acquired
            # A real subsequent request proves the capacity was released and
            # failure did not silently reroute or retry to the healthy replica.
            assert healthy_seen == []
            async with client.post(
                proxy_url + "/v1/chat/completions",
                json=_PAYLOAD,
                headers={REQUEST_ID_HEADER: good_id},
            ) as succeeded:
                assert succeeded.status == 200
                assert succeeded.headers[REQUEST_ID_HEADER] == good_id
                assert await succeeded.read() == _SSE
            await _wait_terminal(router, 2)
            async with client.get(proxy_url + "/router/stats") as response:
                stats = await response.json()
        assert len(bad_seen) == (0 if mode == "refused_connection" else 1)
        assert len(healthy_seen) == 1
        rows = _rows(ledger)
        assert [
            (row["request_id"], row["replica"], row["outcome"]) for row in rows
        ] == [
            (fault_id, 0, "upstream_error"),
            (good_id, 1, "completed"),
        ]
        assert rows[0]["upstream_status"] == (
            200 if mode == "truncated_stream" else None
        )
        assert rows[0]["http_status"] == (200 if mode == "truncated_stream" else 502)
        assert rows[0]["stream_bytes"] == (
            len(_FIRST_EVENT) if mode == "truncated_stream" else 0
        )
        assert rows[1]["stream_bytes"] == len(_SSE)
        assert (stats["offered"], stats["terminal"], stats["in_flight"]) == (2, 2, 0)
        assert stats["pending"] == stats["active"] == 0
        assert stats["busy"] == [0, 0]
        assert stats["outcomes"] == {"upstream_error": 1, "completed": 1}
        assert stats["accounting_failed"] is False
        assert stats["ledger_rows"] == 2
        assert stats["diagnostics_enabled"] is diagnostics_enabled
        assert stats["diagnostics_failed"] is fail_diagnostics_write
        assert stats["diagnostic_rows"] == int(
            diagnostics_enabled and not fail_diagnostics_write
        )
        assert "private transport details" not in json.dumps(stats)
        if diagnostics_enabled and not fail_diagnostics_write:
            [detail] = _rows(diagnostics)
            assert set(detail) == _DIAGNOSTIC_FIELDS
            assert detail["schema"] == _SCHEMA
            assert detail["request_id"] == fault_id
            assert detail["replica"] == 0
            assert detail["stage"] == (
                "read_upstream_stream"
                if mode == "truncated_stream"
                else "awaiting_upstream_headers"
            )
            assert (
                detail["exception_type"]
                == {
                    "closed_before_headers": "ServerDisconnectedError",
                    "refused_connection": "ClientConnectorError",
                    "truncated_stream": "ClientPayloadError",
                }[mode]
            )
            assert detail["errno"] == (
                errno.ECONNREFUSED if mode == "refused_connection" else None
            )
            assert detail["upstream_status"] == rows[0]["upstream_status"]
            assert detail["downstream_prepared"] is (mode == "truncated_stream")
            assert isinstance(detail["elapsed_ms"], (float, int))
            assert math.isfinite(detail["elapsed_ms"])
            assert 0 <= detail["elapsed_ms"] <= rows[0]["terminal_ms"]
            text = diagnostics.read_text()
            assert all(value not in text for value in (_PROMPT, _GENERATION, bad_url))
            # This sidecar must not alter the historical measurement ledger.
            assert not (
                _DIAGNOSTIC_FIELDS - {"request_id", "replica", "upstream_status"}
            ) & (rows[0].keys())
        elif fail_diagnostics_write:
            assert diagnostics.read_text() == ""
        else:
            assert not diagnostics.exists()
    finally:
        if write_failure is not None:
            write_failure.stop()
        release.set()
        await proxy.cleanup()
        await healthy.cleanup()


@pytest.mark.parametrize("mode", ["closed_before_headers", "truncated_stream"])
def test_real_upstream_close_has_stage_and_one_post(tmp_path: Path, mode: str) -> None:
    async def exercise() -> None:
        async with _fault_replica(mode) as (url, seen, release):
            await _exercise_fault(tmp_path, mode, url, seen, release)

    asyncio.run(exercise())


def test_real_connection_refusal_has_errno_without_exception_text(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        # macOS can leave connections to bound-but-unlistened ports pending.
        # Release an ephemeral port so this is a real refusal, not a timeout.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{reserved.getsockname()[1]}"
        await _exercise_fault(tmp_path, "refused_connection", url, [], asyncio.Event())

    asyncio.run(exercise())


def test_diagnostics_opt_in_preserves_failure_and_ledger(tmp_path: Path) -> None:
    async def exercise() -> None:
        async with _fault_replica("closed_before_headers") as (url, seen, release):
            await _exercise_fault(
                tmp_path,
                "closed_before_headers",
                url,
                seen,
                release,
                diagnostics_enabled=False,
            )

    asyncio.run(exercise())


def test_diagnostics_write_failure_is_visible_without_changing_serving(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        async with _fault_replica("closed_before_headers") as (url, seen, release):
            await _exercise_fault(
                tmp_path,
                "closed_before_headers",
                url,
                seen,
                release,
                fail_diagnostics_write=True,
            )

    asyncio.run(exercise())
