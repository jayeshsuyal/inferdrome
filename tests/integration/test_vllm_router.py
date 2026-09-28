"""Socket-level tests for the live two-replica streaming router."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web

from inferdrome.vllm_router import Router, make_app

_SSE = (
    b'data: {"choices":[{"index":0,"delta":{"content":"hello"},'
    b'"finish_reason":null}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


async def _serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def _replica(
    name: str,
    seen: list[str],
    gate: asyncio.Event | None = None,
    cancelled: asyncio.Event | None = None,
) -> web.Application:
    async def chat(request: web.Request) -> web.StreamResponse:
        assert (await request.json())["stream"] is True
        seen.append(name)

        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(_SSE[:80])
        try:
            if gate is not None:
                await gate.wait()
            await response.write(_SSE[80:])
            await response.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            if cancelled is not None:
                cancelled.set()
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


async def _exercise_round_robin(ledger: Path) -> None:
    seen: list[str] = []
    a, a_url = await _serve(_replica("a", seen))
    b, b_url = await _serve(_replica("b", seen))
    router = Router((a_url, b_url), ledger, max_active=2)
    proxy, proxy_url = await _serve(make_app(router))
    try:
        async with aiohttp.ClientSession() as client:
            for _ in range(4):
                async with client.post(
                    proxy_url + "/v1/chat/completions", json={"stream": True}
                ) as response:
                    assert response.status == 200
                    assert await response.read() == _SSE
            async with client.get(proxy_url + "/router/stats") as response:
                stats = await response.json()
        assert seen == ["a", "b", "a", "b"]
        assert (stats["offered"], stats["terminal"], stats["in_flight"]) == (4, 4, 0)
        assert stats["outcomes"] == {"completed": 4}
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        assert len(rows) == 4
        assert [row["replica"] for row in rows] == [0, 1, 0, 1]
        assert all(row["first_byte_ms"] is not None for row in rows)
    finally:
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


async def _exercise_queue_and_load(ledger: Path) -> None:
    seen: list[str] = []
    gate = asyncio.Event()
    a, a_url = await _serve(_replica("a", seen, gate))
    b, b_url = await _serve(_replica("b", seen))
    router = Router(
        (a_url, b_url),
        ledger,
        policy="least_busy",
        max_active=2,
        max_queue=0,
        queue_timeout_s=0.1,
    )
    proxy, proxy_url = await _serve(make_app(router))
    try:
        async with aiohttp.ClientSession() as client:
            first = await client.post(
                proxy_url + "/v1/chat/completions", json={"stream": True}
            )
            assert first.status == 200
            # The first event arrives before the replica is allowed to finish.
            assert await asyncio.wait_for(first.content.readexactly(80), 1) == _SSE[:80]
            second = await client.post(
                proxy_url + "/v1/chat/completions", json={"stream": True}
            )
            assert second.status == 200
            assert await second.read() == _SSE
            # Replica A is still busy. Least-busy chooses B for the next call.
            third = await client.post(
                proxy_url + "/v1/chat/completions", json={"stream": True}
            )
            assert third.status == 200
            assert await third.read() == _SSE
            assert seen == ["a", "b", "b"]
            gate.set()
            assert await first.read() == _SSE[80:]
            await first.release()
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        assert [row["outcome"] for row in rows] == ["completed"] * 3
    finally:
        gate.set()
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


async def _exercise_capacity_and_disconnect(ledger: Path) -> None:
    seen: list[str] = []
    gate = asyncio.Event()
    cancelled = asyncio.Event()
    a, a_url = await _serve(_replica("a", seen, gate, cancelled))
    b, b_url = await _serve(_replica("b", seen, gate, cancelled))
    router = Router((a_url, b_url), ledger, max_active=1, max_queue=0)
    proxy, proxy_url = await _serve(make_app(router))
    try:
        async with aiohttp.ClientSession() as client:
            first = await client.post(
                proxy_url + "/v1/chat/completions", json={"stream": True}
            )
            assert first.status == 200
            second = await client.post(
                proxy_url + "/v1/chat/completions", json={"stream": True}
            )
            assert second.status == 503
            await second.release()
            first.close()
            for _ in range(40):
                if router.terminal == 2:
                    break
                await asyncio.sleep(0.05)
            assert router.terminal == 2
            assert router.outcomes == {"rejected_capacity": 1, "disconnected": 1}
            # The router has relinquished its upstream connection; aiohttp's
            # fake server can notice a peer close only on a later large write.
            assert router.session is not None
            assert not router.session.connector._acquired
            gate.set()
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        assert len(rows) == router.offered == router.terminal == 2
    finally:
        gate.set()
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


async def _exercise_bounded_queue(ledger: Path) -> None:
    seen: list[str] = []
    gate = asyncio.Event()
    a, a_url = await _serve(_replica("a", seen, gate))
    b, b_url = await _serve(_replica("b", seen))
    router = Router(
        (a_url, b_url), ledger, max_active=1, max_queue=1, queue_timeout_s=0.1
    )
    proxy, proxy_url = await _serve(make_app(router))
    try:
        async with aiohttp.ClientSession() as client:
            first = await client.post(
                proxy_url + "/v1/chat/completions", json={"stream": True}
            )
            assert first.status == 200
            async with client.post(
                proxy_url + "/v1/chat/completions", json={"stream": True}
            ) as second:
                assert second.status == 503
            gate.set()
            assert await first.read() == _SSE
        assert router.outcomes == {"rejected_queue_timeout": 1, "completed": 1}
        assert router.offered == router.terminal == 2
        assert len(ledger.read_text().splitlines()) == 2
    finally:
        gate.set()
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


def test_round_robin_streaming_and_ledger(tmp_path: Path) -> None:
    asyncio.run(_exercise_round_robin(tmp_path / "round.jsonl"))


def test_least_busy_uses_live_in_flight_load(tmp_path: Path) -> None:
    asyncio.run(_exercise_queue_and_load(tmp_path / "load.jsonl"))


def test_capacity_and_disconnect_close_upstream(tmp_path: Path) -> None:
    asyncio.run(_exercise_capacity_and_disconnect(tmp_path / "cancel.jsonl"))


def test_queue_timeout_is_accounted(tmp_path: Path) -> None:
    asyncio.run(_exercise_bounded_queue(tmp_path / "queue.jsonl"))


async def _wait_terminal(router: Router, count: int) -> None:
    async with asyncio.timeout(2):
        while router.terminal != count:
            await asyncio.sleep(0.01)


async def _exercise_fragmented_body(ledger: Path, oversized: bool) -> None:
    seen: list[str] = []
    a, a_url = await _serve(_replica("a", seen))
    b, b_url = await _serve(_replica("b", seen))
    router = Router((a_url, b_url), ledger, max_body_bytes=32)
    proxy, proxy_url = await _serve(make_app(router))

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"stream":'
        await asyncio.sleep(0.05)
        yield b"true}" + (b" " * 32 if oversized else b"")

    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                proxy_url + "/v1/chat/completions", data=chunks()
            ) as response:
                assert response.status == (413 if oversized else 200)
                if not oversized:
                    assert await response.read() == _SSE
            await _wait_terminal(router, 1)
        assert seen == ([] if oversized else ["a"])
        assert router.outcomes == {"rejected_body" if oversized else "completed": 1}
        assert router.busy == [0, 0]
    finally:
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


@pytest.mark.parametrize("oversized", [False, True])
def test_chunked_body_is_read_completely_with_size_bound(
    tmp_path: Path, oversized: bool
) -> None:
    asyncio.run(_exercise_fragmented_body(tmp_path / "body.jsonl", oversized))


async def _exercise_deadline(ledger: Path, stage: str) -> None:
    gate = asyncio.Event()
    seen: list[str] = []

    async def stall(request: web.Request) -> web.StreamResponse:
        await request.read()
        seen.append("a")
        if stage == "headers":
            await gate.wait()
            return web.Response()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(_SSE[:80])
        await gate.wait()
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", stall)
    a, a_url = await _serve(app)
    b, b_url = await _serve(_replica("b", seen))
    router = Router((a_url, b_url), ledger, max_active=1, request_timeout_s=0.2)
    proxy, proxy_url = await _serve(make_app(router))

    async def slow_upload() -> AsyncIterator[bytes]:
        yield b'{"stream":'
        await gate.wait()
        yield b"true}"

    try:
        async with aiohttp.ClientSession() as client:
            body = slow_upload() if stage == "upload" else b'{"stream":true}'
            async with client.post(
                proxy_url + "/v1/chat/completions", data=body
            ) as response:
                assert response.status == (200 if stage == "stream" else 504)
                if stage == "stream":
                    # Failure after headers preserves status and aborts the payload.
                    with pytest.raises(aiohttp.ClientPayloadError):
                        await response.read()
            await _wait_terminal(router, 1)
            assert router.outcomes == {"timeout": 1}
            assert router.pending == 0
            assert router.busy == [0, 0]
            assert router.sem.locked() is False
            row = json.loads(ledger.read_text())
            assert row["http_status"] == (200 if stage == "stream" else 504)
            assert row["terminal_ms"] < 1500
            assert seen == ([] if stage == "upload" else ["a"])
            # Capacity is reusable after cancellation; the next call selects B.
            async with client.post(
                proxy_url + "/v1/chat/completions", json={"stream": True}
            ) as response:
                assert await response.read() == _SSE
            await _wait_terminal(router, 2)
            assert router.outcomes == {"timeout": 1, "completed": 1}
    finally:
        gate.set()
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


@pytest.mark.parametrize("stage", ["upload", "headers", "stream"])
def test_deadline_releases_capacity_at_every_stage(tmp_path: Path, stage: str) -> None:
    # A regression used to busy-loop on a stored upstream TimeoutError. Keep an
    # external watchdog so that bug fails this test instead of hanging all CI.
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, runpy, sys; from pathlib import Path; "
            "ns = runpy.run_path(sys.argv[1]); "
            "asyncio.run(ns['_exercise_deadline'](Path(sys.argv[2]), sys.argv[3]))",
            __file__,
            str(tmp_path / "deadline.jsonl"),
            stage,
        ],
        check=True,
        timeout=10,
        capture_output=True,
        text=True,
    )


async def _exercise_queued_disconnect(ledger: Path) -> None:
    seen: list[str] = []
    gate = asyncio.Event()
    a, a_url = await _serve(_replica("a", seen, gate))
    b, b_url = await _serve(_replica("b", seen))
    router = Router((a_url, b_url), ledger, max_active=1, max_queue=1)
    proxy, proxy_url = await _serve(make_app(router))
    try:
        async with aiohttp.ClientSession() as client:
            first = await client.post(
                proxy_url + "/v1/chat/completions", json={"stream": True}
            )
            _, writer = await asyncio.open_connection(
                "127.0.0.1", int(proxy_url.rsplit(":", 1)[1])
            )
            writer.write(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Length: 15\r\nContent-Type: application/json\r\n\r\n"
                b'{"stream":true}'
            )
            await writer.drain()
            async with asyncio.timeout(2):
                while router.pending != 1:
                    await asyncio.sleep(0.01)
            writer.close()
            await writer.wait_closed()
            await _wait_terminal(router, 1)
            assert router.outcomes == {"disconnected": 1}
            assert router.pending == 0
            assert router.busy == [1, 0]
            assert seen == ["a"]
            gate.set()
            assert await first.read() == _SSE
            await _wait_terminal(router, 2)
            async with client.post(
                proxy_url + "/v1/chat/completions", json={"stream": True}
            ) as third:
                assert await third.read() == _SSE
            await _wait_terminal(router, 3)
        assert seen == ["a", "b"]
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        assert len(rows) == router.offered == router.terminal == 3
        assert rows[0]["replica"] is None
    finally:
        gate.set()
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


def test_queued_disconnect_does_not_dispatch_or_leak_admission(tmp_path: Path) -> None:
    asyncio.run(_exercise_queued_disconnect(tmp_path / "queued-cancel.jsonl"))


async def _exercise_ledger_failure(ledger: Path) -> None:
    seen: list[str] = []
    a, a_url = await _serve(_replica("a", seen))
    b, b_url = await _serve(_replica("b", seen))
    router = Router((a_url, b_url), ledger)
    proxy, proxy_url = await _serve(make_app(router))
    try:
        with patch.object(
            router._ledger_file, "write", side_effect=OSError("disk full")
        ):
            async with aiohttp.ClientSession() as client:
                async with client.post(
                    proxy_url + "/v1/chat/completions", json={"stream": True}
                ) as first:
                    assert await first.read() == _SSE
                await _wait_terminal(router, 1)
                async with client.post(
                    proxy_url + "/v1/chat/completions", json={"stream": True}
                ) as second:
                    assert second.status == 503
                await _wait_terminal(router, 2)
                async with client.get(proxy_url + "/router/stats") as response:
                    stats = await response.json()
        assert stats["accounting_failed"] is True
        assert stats["ledger_rows"] == 0
        assert stats["in_flight"] == 0
        assert stats["terminal"] == 2
        assert seen == ["a"]
    finally:
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


def test_ledger_failure_is_visible_and_stops_dispatch(tmp_path: Path) -> None:
    asyncio.run(_exercise_ledger_failure(tmp_path / "failed-ledger.jsonl"))
