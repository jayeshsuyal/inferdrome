"""Small live streaming router for two independent OpenAI-compatible replicas.

This is a serving path, separate from the frozen evidence replay policies. The
JSONL ledger contains timings and outcomes only; prompts and generations are
never recorded by the router.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from inferdrome.evaluation.stream import StreamError, StreamParser
from inferdrome.vllm_affinity import AffinityHistory, choose, document_digest


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("replica origins must be literal loopback HTTP host:port")
    return value.rstrip("/")


class Router:
    def __init__(
        self,
        origins: tuple[str, str],
        ledger: Path,
        *,
        policy: str = "round_robin",
        max_active: int = 32,
        max_queue: int = 32,
        queue_timeout_s: float = 5.0,
        request_timeout_s: float = 120.0,
        max_body_bytes: int = 262_144,
        max_stream_bytes: int = 16_777_216,
    ) -> None:
        if policy not in {
            "round_robin",
            "least_busy",
            "cache_only",
            "cache_plus_load",
        }:
            raise ValueError("unsupported router policy")
        if min(max_active, max_body_bytes, max_stream_bytes) <= 0 or max_queue < 0:
            raise ValueError("router bounds are invalid")
        if queue_timeout_s <= 0 or request_timeout_s <= 0:
            raise ValueError("router timeouts must be positive")
        self.origins = tuple(_origin(value) for value in origins)
        if self.origins[0] == self.origins[1]:
            raise ValueError("replicas must have distinct origins")
        self.ledger = ledger
        self.policy = policy
        self.max_active = max_active
        self.max_queue = max_queue
        self.queue_timeout_s = queue_timeout_s
        self.request_timeout_s = request_timeout_s
        self.max_body_bytes = max_body_bytes
        self.max_stream_bytes = max_stream_bytes
        self.sem = asyncio.Semaphore(max_active)
        self.pending = 0
        self.active = 0
        self.busy = [0, 0]
        self.history = AffinityHistory()
        self.turn = 0
        self.offered = 0
        self.terminal = 0
        self.outcomes: dict[str, int] = {}
        self.accounting_failed = False
        self.ledger_rows = 0
        self.session: aiohttp.ClientSession | None = None

    async def start(self, _app: web.Application) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.initial_ledger_bytes = (
            self.ledger.stat().st_size if self.ledger.exists() else 0
        )
        self._ledger_file = self.ledger.open("a", encoding="utf-8")
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=self.max_active, use_dns_cache=False),
            timeout=aiohttp.ClientTimeout(total=None),
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            headers={"Accept-Encoding": "identity"},
        )

    async def stop(self, _app: web.Application) -> None:
        if self.session is not None:
            await self.session.close()
        self._ledger_file.close()

    def _record(self, row: dict[str, object]) -> None:
        outcome = str(row["outcome"])
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        self.terminal += 1
        if not self.accounting_failed:
            try:
                self._ledger_file.write(json.dumps(row, separators=(",", ":")) + "\n")
                self._ledger_file.flush()
            except (OSError, ValueError):
                self.accounting_failed = True
            else:
                self.ledger_rows += 1

    def _choose(self, digest: str | None) -> tuple[int, str, tuple[int, int]]:
        now_ns = time.monotonic_ns()
        scores = self.history.scores(digest, now_ns)
        decision = choose(self.policy, (self.busy[0], self.busy[1]), self.turn, scores)
        self.turn += 1
        self.history.record(digest, decision.replica, now_ns)
        return decision.replica, decision.reason, scores

    async def stats(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "offered": self.offered,
                "terminal": self.terminal,
                "in_flight": self.offered - self.terminal,
                "pending": self.pending,
                "active": self.active,
                "busy": self.busy,
                "affinity_history_keys": self.history.size,
                "outcomes": self.outcomes,
                "policy": self.policy,
                "accounting_failed": self.accounting_failed,
                "ledger_rows": self.ledger_rows,
                "initial_ledger_bytes": self.initial_ledger_bytes,
            }
        )

    async def chat(self, request: web.Request) -> web.StreamResponse:
        arrived_ns = time.monotonic_ns()
        request_id = uuid.uuid4().hex
        self.offered += 1
        row: dict[str, object] = {
            "request_id": request_id,
            "arrived_ns": arrived_ns,
            "policy": self.policy,
            "replica": None,
            "queued_ms": None,
            "first_byte_ms": None,
            "terminal_ms": None,
            "stream_bytes": 0,
            "http_status": None,
            "upstream_status": None,
            "document_sha256": None,
            "estimated_affinity": None,
            "busy_at_decision": None,
            "route_reason": None,
            "outcome": "error",
        }
        task = asyncio.current_task()
        assert task is not None
        monitor = asyncio.create_task(self._watch_disconnect(request, task))
        deadline = asyncio.get_running_loop().time() + self.request_timeout_s
        admitted = False
        slot_owned = False
        index: int | None = None
        try:
            if self.accounting_failed:
                row["outcome"] = "rejected_accounting"
                row["http_status"] = 503
                return web.json_response(
                    {"error": "router ledger unavailable"}, status=503
                )
            if self.active + self.pending >= self.max_active + self.max_queue:
                row["outcome"] = "rejected_capacity"
                row["http_status"] = 503
                return web.json_response(
                    {"error": "router capacity exceeded"}, status=503
                )
            self.pending += 1
            admitted = True
            async with asyncio.timeout_at(deadline):
                try:
                    async with asyncio.timeout(self.queue_timeout_s):
                        await self.sem.acquire()
                except TimeoutError:
                    row["outcome"] = "rejected_queue_timeout"
                    row["http_status"] = 503
                    return web.json_response(
                        {"error": "router queue timeout"}, status=503
                    )
                self.pending -= 1
                admitted = False
                self.active += 1
                slot_owned = True
                row["queued_ms"] = (time.monotonic_ns() - arrived_ns) / 1e6
                if self.policy in {"round_robin", "least_busy"}:
                    row["busy_at_decision"] = list(self.busy)
                    index, reason, affinity = self._choose(None)
                    self.busy[index] += 1
                    row["replica"] = index
                    row["route_reason"] = reason
                    row["estimated_affinity"] = list(affinity)
                if (
                    request.content_length is not None
                    and request.content_length > self.max_body_bytes
                ):
                    row["outcome"] = "rejected_body"
                    row["http_status"] = 413
                    return web.json_response({"error": "request too large"}, status=413)
                # A single read(n) may return only the first TCP/chunked fragment.
                body = bytearray()
                while chunk := await request.content.read(
                    min(16_384, self.max_body_bytes + 1 - len(body))
                ):
                    body.extend(chunk)
                    if len(body) > self.max_body_bytes:
                        row["outcome"] = "rejected_body"
                        row["http_status"] = 413
                        return web.json_response(
                            {"error": "request too large"}, status=413
                        )
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                payload = None
            if not isinstance(payload, dict) or payload.get("stream") is not True:
                row["outcome"] = "rejected_input"
                row["http_status"] = 400
                return web.json_response(
                    {"error": "stream: true is required"}, status=400
                )
            digest = document_digest(payload)
            row["document_sha256"] = digest
            if index is None:
                row["busy_at_decision"] = list(self.busy)
                index, reason, affinity = self._choose(digest)
                self.busy[index] += 1
                row["replica"] = index
                row["route_reason"] = reason
                row["estimated_affinity"] = list(affinity)
            return await self._proxy(
                request, bytes(body), index, row, arrived_ns, deadline
            )
        except asyncio.CancelledError:
            row["outcome"] = (
                "disconnected" if self._disconnected(request) else "cancelled"
            )
            raise
        except TimeoutError:
            row["outcome"] = "timeout"
            row["http_status"] = 504
            return web.json_response({"error": "router request timeout"}, status=504)
        except (aiohttp.ClientError, OSError):
            row["outcome"] = "upstream_error"
            row["http_status"] = 502
            return web.json_response({"error": "replica unavailable"}, status=502)
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            if admitted:
                self.pending -= 1
            if index is not None:
                self.busy[index] -= 1
            if slot_owned:
                self.active -= 1
                self.sem.release()
            row["terminal_ms"] = (time.monotonic_ns() - arrived_ns) / 1e6
            self._record(row)

    @staticmethod
    def _disconnected(request: web.Request) -> bool:
        return request.transport is None or request.transport.is_closing()

    async def _watch_disconnect(
        self, request: web.Request, task: asyncio.Task[web.StreamResponse]
    ) -> None:
        # Covers admission, upload, idle upstream and downstream backpressure.
        while not self._disconnected(request):
            await asyncio.sleep(0.05)
        task.cancel()

    async def _proxy(
        self,
        request: web.Request,
        body: bytes,
        index: int,
        row: dict[str, object],
        arrived_ns: int,
        deadline: float,
    ) -> web.StreamResponse:
        assert self.session is not None
        # A row can fail while this request is waiting for admission or uploading.
        if self.accounting_failed:
            row["outcome"] = "rejected_accounting"
            row["http_status"] = 503
            return web.json_response({"error": "router ledger unavailable"}, status=503)
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        try:
            # The same absolute deadline covers admission, upload, connect, reads
            # and writes. A terminal timeout cannot be mistaken for an idle poll.
            async with asyncio.timeout_at(deadline):
                async with self.session.post(
                    self.origins[index] + "/v1/chat/completions",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    allow_redirects=False,
                ) as upstream:
                    row["upstream_status"] = upstream.status
                    if upstream.status != 200:
                        row["outcome"] = "upstream_http_error"
                        row["http_status"] = 502
                        return web.json_response(
                            {"error": "replica rejected request"}, status=502
                        )
                    if upstream.content_type != "text/event-stream":
                        row["outcome"] = "upstream_protocol_error"
                        row["http_status"] = 502
                        return web.json_response(
                            {"error": "replica did not stream"}, status=502
                        )
                    await response.prepare(request)
                    row["http_status"] = 200
                    total = 0
                    parser = StreamParser(
                        max_stream_bytes=self.max_stream_bytes,
                        max_event_bytes=262_144,
                        max_content_events=65_536,
                    )
                    while chunk := await upstream.content.read(16_384):
                        total += len(chunk)
                        if total > self.max_stream_bytes:
                            row["outcome"] = "stream_limit"
                            return response
                        parser.feed(chunk, time.monotonic_ns())
                        if row["first_byte_ms"] is None:
                            row["first_byte_ms"] = (
                                time.monotonic_ns() - arrived_ns
                            ) / 1e6
                        await response.write(chunk)  # aiohttp awaits downstream drain.
                        row["stream_bytes"] = total
                    parser.finish()
                    await response.write_eof()
                    row["outcome"] = "completed"
        except (ConnectionResetError, BrokenPipeError):
            if not response.prepared:
                raise
            row["outcome"] = "disconnected"
        except TimeoutError:
            if not response.prepared:
                raise
            row["outcome"] = "timeout"
        except StreamError:
            row["outcome"] = "upstream_protocol_error"
        except (aiohttp.ClientError, OSError):
            if not response.prepared:
                raise
            row["outcome"] = "upstream_error"
        finally:
            # An incomplete stream must end as a broken connection, never a clean
            # HTTP EOF or a second HTTP response after the 200 headers were sent.
            if response.prepared and row["outcome"] != "completed":
                response.force_close()
                if request.transport is not None:
                    request.transport.close()
        return response


def make_app(router: Router) -> web.Application:
    app = web.Application(client_max_size=router.max_body_bytes)
    app.router.add_post("/v1/chat/completions", router.chat)
    app.router.add_get("/router/stats", router.stats)
    app.on_startup.append(router.start)
    app.on_cleanup.append(router.stop)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Two-replica vLLM streaming router")
    parser.add_argument("--replica-a", required=True)
    parser.add_argument("--replica-b", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument(
        "--policy",
        choices=("round_robin", "least_busy", "cache_only", "cache_plus_load"),
        default="round_robin",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--max-active", type=int, default=32)
    parser.add_argument("--max-queue", type=int, default=32)
    args = parser.parse_args()
    router = Router(
        (args.replica_a, args.replica_b),
        args.ledger,
        policy=args.policy,
        max_active=args.max_active,
        max_queue=args.max_queue,
    )
    web.run_app(make_app(router), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
