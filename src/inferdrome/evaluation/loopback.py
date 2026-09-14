"""Small synthetic SSE demonstration on two ephemeral loopback endpoints."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiohttp import web


def _event(value: object) -> bytes:
    return ("data: " + json.dumps(value) + "\n\n").encode()


async def _complete(request: web.Request) -> web.StreamResponse:
    await request.read()
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    try:
        await response.write(
            _event(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": ""},
                            "finish_reason": None,
                        }
                    ]
                }
            )
        )
        await asyncio.sleep(0.02)
        for content in ("Synthetic ", "loopback reply."):
            frame = _event(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content},
                            "finish_reason": None,
                        }
                    ]
                }
            )
            await response.write(frame[:7])
            await asyncio.sleep(0.002)
            await response.write(frame[7:])
        await response.write(
            _event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        )
        # Counts are synthetic protocol fixtures, not tokenization evidence.
        await response.write(
            _event(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 4,
                        "total_tokens": 9,
                    },
                }
            )
        )
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
    except (ConnectionError, asyncio.CancelledError):
        pass
    return response


@asynccontextmanager
async def loopback_pair() -> AsyncIterator[tuple[str, str]]:
    runners: list[web.AppRunner] = []
    sockets: list[socket.socket] = []
    origins: list[str] = []
    try:
        for _ in range(2):
            app = web.Application(client_max_size=262_144)
            app.router.add_post("/v1/chat/completions", _complete)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
            runners.append(runner)
            await runner.setup()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(listener)
            listener.bind(("127.0.0.1", 0))
            origins.append(f"http://127.0.0.1:{listener.getsockname()[1]}")
            await web.SockSite(runner, listener).start()
        yield origins[0], origins[1]
    finally:
        for runner in runners:
            await runner.cleanup()
        for listener in sockets:
            listener.close()
