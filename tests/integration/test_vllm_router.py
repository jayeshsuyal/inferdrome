"""Socket-level tests for the live two-replica streaming router."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
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
            # Upstream socket closure is observed by the fake server on its next write.
            gate.set()
            await asyncio.wait_for(cancelled.wait(), 2)
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
