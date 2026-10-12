"""Small live streaming router for two independent OpenAI-compatible replicas.

This is a serving path, separate from the frozen evidence replay policies. The
JSONL ledger contains timings and outcomes only; prompts and generations are
never recorded by the router.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, TextIO
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from inferdrome.evaluation.stream import StreamError, StreamParser
from inferdrome.vllm_affinity import (
    ESCAPE_BUSY_DELTA,
    SATURATION_RULE_VERSION,
    AffinityDecision,
    AffinityHistory,
    choose,
    document_digest,
)
from inferdrome.vllm_request_identity import REQUEST_ID_HEADER, valid_request_id

_REQUEST_ID_KEY = "inferdrome_request_id"
TRANSPORT_SCHEMA = "inferdrome.vllm-router-transport.v2"
UPSTREAM_KEEPALIVE_S = 1.0

# Use fixed labels, never an exception's message, repr, args or user-defined name.
_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    aiohttp.ClientConnectorError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientPayloadError,
    aiohttp.ClientConnectionResetError,
    aiohttp.ServerTimeoutError,
    aiohttp.ClientOSError,
    aiohttp.ClientConnectionError,
    aiohttp.ClientResponseError,
    aiohttp.ClientError,
    ConnectionResetError,
    BrokenPipeError,
    ConnectionRefusedError,
    TimeoutError,
    OSError,
)


def _transport_error(error: BaseException) -> tuple[str, int | None]:
    exception_type = next(
        (kind.__name__ for kind in _TRANSPORT_EXCEPTIONS if isinstance(error, kind)),
        "OtherTransportError",
    )
    # aiohttp may wrap an OS error; inspect only one known wrapper/cause.
    underlying = (
        error.os_error if isinstance(error, aiohttp.ClientConnectorError) else error
    )
    if not isinstance(underlying, OSError) and isinstance(error.__cause__, OSError):
        underlying = error.__cause__
    number = underlying.errno if isinstance(underlying, OSError) else None
    errno = number if type(number) is int and -(2**31) <= number < 2**31 else None
    return exception_type, errno


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


@dataclass(slots=True)
class _ConnectionTrace:
    new_connection_attempts: int = 0
    reused_connections: int = 0
    queued_for_connection: bool = False
    connection_ready: bool = False
    request_headers_observed: bool = False
    request_body_bytes_observed: int = 0
    phase: Literal[
        "unobserved", "queued", "creating", "ready", "headers_observed", "body_observed"
    ] = "unobserved"

    def snapshot(self) -> dict[str, object]:
        # Fixed allowlist: never retain trace params, URLs, headers or payloads.
        return {
            "new_connection_attempts": self.new_connection_attempts,
            "reused_connections": self.reused_connections,
            "queued_for_connection": self.queued_for_connection,
            "connection_ready": self.connection_ready,
            "request_headers_observed": self.request_headers_observed,
            "request_body_bytes_observed": self.request_body_bytes_observed,
            "phase": self.phase,
        }


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
        saturation_active: int | None = None,
        diagnostics: Path | None = None,
        upstream_keepalive_s: float = UPSTREAM_KEEPALIVE_S,
        reuse_upstream_connections: bool = True,
    ) -> None:
        if policy not in {
            "round_robin",
            "least_busy",
            "cache_only",
            "cache_plus_load",
            "cache_saturation",
        }:
            raise ValueError("unsupported router policy")
        if min(max_active, max_body_bytes, max_stream_bytes) <= 0 or max_queue < 0:
            raise ValueError("router bounds are invalid")
        if queue_timeout_s <= 0 or request_timeout_s <= 0:
            raise ValueError("router timeouts must be positive")
        if policy == "cache_saturation" and (
            saturation_active is None or not 1 <= saturation_active <= max_active
        ):
            raise ValueError("candidate threshold must be within router active bound")
        if policy != "cache_saturation" and saturation_active is not None:
            raise ValueError("candidate threshold applies only to cache_saturation")
        if (
            type(upstream_keepalive_s) not in (int, float)
            or type(reuse_upstream_connections) is not bool
        ):
            raise ValueError("upstream connection settings are invalid")
        try:
            upstream_keepalive_s = float(upstream_keepalive_s)
        except OverflowError as error:
            raise ValueError("upstream connection settings are invalid") from error
        if not math.isfinite(upstream_keepalive_s) or upstream_keepalive_s <= 0:
            raise ValueError("upstream connection settings are invalid")
        self.upstream_keepalive_s = upstream_keepalive_s
        self.reuse_upstream_connections = reuse_upstream_connections
        self.origins = tuple(_origin(value) for value in origins)
        if self.origins[0] == self.origins[1]:
            raise ValueError("replicas must have distinct origins")
        self.ledger = ledger
        if diagnostics is not None and diagnostics.resolve() == ledger.resolve():
            raise ValueError("transport diagnostics must be separate from the ledger")
        self.diagnostics = diagnostics
        self.diagnostics_failed = False
        self.diagnostic_rows = 0
        self._diagnostics_file: TextIO | None = None
        self.policy = policy
        self.max_active = max_active
        self.max_queue = max_queue
        self.queue_timeout_s = queue_timeout_s
        self.request_timeout_s = request_timeout_s
        self.max_body_bytes = max_body_bytes
        self.max_stream_bytes = max_stream_bytes
        self.saturation_active = saturation_active
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
        try:
            if self.diagnostics is not None:
                self.diagnostics.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation rejects stale files, hard links and symlinks.
                fd = os.open(
                    self.diagnostics, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                self._diagnostics_file = os.fdopen(fd, "w", encoding="utf-8")
            # Retire idle sockets before the owned server's longer idle limit.
            # This never closes an active response or retries an inference POST.
            connector = (
                aiohttp.TCPConnector(
                    limit=self.max_active,
                    use_dns_cache=False,
                    keepalive_timeout=self.upstream_keepalive_s,
                )
                if self.reuse_upstream_connections
                else aiohttp.TCPConnector(
                    limit=self.max_active, use_dns_cache=False, force_close=True
                )
            )
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=None),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
                headers={"Accept-Encoding": "identity"},
                trace_configs=[self._connection_trace_config()]
                if self.diagnostics is not None
                else [],
            )
        except BaseException:
            self._close_diagnostics()
            self._ledger_file.close()
            raise

    async def stop(self, _app: web.Application) -> None:
        try:
            if self.session is not None:
                await self.session.close()
        finally:
            try:
                self._ledger_file.close()
            finally:
                self._close_diagnostics()

    def _close_diagnostics(self) -> None:
        if self._diagnostics_file is not None:
            try:
                self._diagnostics_file.close()
            except (OSError, ValueError):
                self.diagnostics_failed = True

    def _trace_connection(
        self, context: SimpleNamespace, event: str, body_bytes: int = 0
    ) -> None:
        try:
            request_context = context.trace_request_ctx
            if not isinstance(request_context, dict):
                raise ValueError("missing per-request connection trace context")
            state = request_context.get("connection")
            if not isinstance(state, _ConnectionTrace):
                raise ValueError("missing per-request connection trace")
            if event == "queued":
                state.queued_for_connection = True
                state.phase = "queued"
            elif event == "creating":
                state.new_connection_attempts = min(
                    state.new_connection_attempts + 1, 2**31 - 1
                )
                state.phase = "creating"
            elif event == "reused":
                state.reused_connections = min(state.reused_connections + 1, 2**31 - 1)
                state.connection_ready = True
                state.phase = "ready"
            elif event == "ready":
                state.connection_ready = True
                state.phase = "ready"
            elif event == "headers_observed":
                state.request_headers_observed = True
                state.phase = "headers_observed"
            elif event == "body_observed":
                state.request_body_bytes_observed = min(
                    state.request_body_bytes_observed + body_bytes, 2**63 - 1
                )
                state.phase = "body_observed"
            else:
                raise ValueError("unknown connection trace event")
        except Exception:
            # Instrumentation errors fail required diagnostics without fabricating
            # an upstream failure or interrupting a request that may be accepted.
            self.diagnostics_failed = True

    def _connection_trace_config(self) -> aiohttp.TraceConfig:
        trace = aiohttp.TraceConfig()

        async def queued(
            _session: aiohttp.ClientSession, context: SimpleNamespace, _params: object
        ) -> None:
            self._trace_connection(context, "queued")

        async def creating(
            _session: aiohttp.ClientSession, context: SimpleNamespace, _params: object
        ) -> None:
            self._trace_connection(context, "creating")

        async def ready(
            _session: aiohttp.ClientSession, context: SimpleNamespace, _params: object
        ) -> None:
            self._trace_connection(context, "ready")

        async def reused(
            _session: aiohttp.ClientSession, context: SimpleNamespace, _params: object
        ) -> None:
            self._trace_connection(context, "reused")

        async def headers(
            _session: aiohttp.ClientSession, context: SimpleNamespace, _params: object
        ) -> None:
            self._trace_connection(context, "headers_observed")

        async def body(
            _session: aiohttp.ClientSession,
            context: SimpleNamespace,
            params: aiohttp.TraceRequestChunkSentParams,
        ) -> None:
            try:
                size = len(params.chunk)
            except Exception:
                self.diagnostics_failed = True
                return
            self._trace_connection(context, "body_observed", size)

        trace.on_connection_queued_start.append(queued)
        trace.on_connection_create_start.append(creating)
        trace.on_connection_create_end.append(ready)
        trace.on_connection_reuseconn.append(reused)
        trace.on_request_headers_sent.append(headers)
        trace.on_request_chunk_sent.append(body)
        return trace

    def _record_transport(
        self,
        error: BaseException,
        stage: str,
        row: dict[str, object],
        response: web.StreamResponse,
        arrived_ns: int,
        connection: _ConnectionTrace | None,
    ) -> None:
        if self._diagnostics_file is None or self.diagnostics_failed:
            return
        if connection is None:
            self.diagnostics_failed = True
            return
        try:
            exception_type, errno = _transport_error(error)
            diagnostic = {
                "schema": TRANSPORT_SCHEMA,
                "request_id": row["request_id"],
                "replica": row["replica"],
                "stage": stage,
                "exception_type": exception_type,
                "errno": errno,
                "upstream_status": row["upstream_status"],
                "downstream_prepared": response.prepared,
                "elapsed_ms": (time.monotonic_ns() - arrived_ns) / 1e6,
                "connection": connection.snapshot(),
            }
            self._diagnostics_file.write(
                json.dumps(diagnostic, separators=(",", ":")) + "\n"
            )
            self._diagnostics_file.flush()
        except Exception:
            self.diagnostics_failed = True
        else:
            self.diagnostic_rows += 1

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

    def _choose(self, digest: str | None) -> AffinityDecision:
        now_ns = time.monotonic_ns()
        scores = self.history.scores(digest, now_ns)
        decision = choose(
            self.policy,
            (self.busy[0], self.busy[1]),
            self.turn,
            scores,
            saturation_active=self.saturation_active,
        )
        self.turn += 1
        self.history.record(digest, decision.replica, now_ns)
        return decision

    @staticmethod
    def _record_decision(row: dict[str, object], decision: AffinityDecision) -> None:
        row["route_reason"] = decision.reason
        row["estimated_affinity"] = list(decision.estimated_affinity)
        row["preferred_replica"] = decision.preferred_replica
        row["saturation_reached"] = decision.saturation_reached
        row["relative_imbalance_reached"] = decision.relative_imbalance_reached

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
                "upstream_connection_reuse": self.reuse_upstream_connections,
                "upstream_keepalive_s": (
                    self.upstream_keepalive_s
                    if self.reuse_upstream_connections
                    else None
                ),
                "saturation_active": self.saturation_active,
                "saturation_rule_version": (
                    SATURATION_RULE_VERSION
                    if self.saturation_active is not None
                    else None
                ),
                "accounting_failed": self.accounting_failed,
                "ledger_rows": self.ledger_rows,
                "initial_ledger_bytes": self.initial_ledger_bytes,
                "diagnostics_enabled": self.diagnostics is not None,
                "diagnostics_failed": self.diagnostics_failed,
                "diagnostic_rows": self.diagnostic_rows,
            }
        )

    async def chat(self, request: web.Request) -> web.StreamResponse:
        arrived_ns = time.monotonic_ns()
        supplied_ids = request.headers.getall(REQUEST_ID_HEADER, [])
        accepted_id = len(supplied_ids) == 1 and valid_request_id(supplied_ids[0])
        request_id = supplied_ids[0] if accepted_id else uuid.uuid4().hex
        # Only a validated or generated value may reach the ledger or response.
        request[_REQUEST_ID_KEY] = request_id
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
            "preferred_replica": None,
            "saturation_threshold_active": self.saturation_active,
            "saturation_rule_version": (
                SATURATION_RULE_VERSION if self.saturation_active is not None else None
            ),
            "escape_busy_delta": ESCAPE_BUSY_DELTA,
            "saturation_reached": None,
            "relative_imbalance_reached": None,
            "turn_at_decision": None,
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
            if supplied_ids and not accepted_id:
                row["outcome"] = "rejected_request_id"
                row["http_status"] = 400
                return web.json_response(
                    {"error": "request identity must be one lowercase 32-hex value"},
                    status=400,
                )
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
                    decision = self._choose(None)
                    row["turn_at_decision"] = self.turn - 1
                    index = decision.replica
                    self.busy[index] += 1
                    row["replica"] = index
                    self._record_decision(row, decision)
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
                decision = self._choose(digest)
                row["turn_at_decision"] = self.turn - 1
                index = decision.replica
                self.busy[index] += 1
                row["replica"] = index
                self._record_decision(row, decision)
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
        stage = "awaiting_upstream_headers"
        connection = _ConnectionTrace() if self.diagnostics is not None else None
        try:
            # The same absolute deadline covers admission, upload, connect, reads
            # and writes. A terminal timeout cannot be mistaken for an idle poll.
            async with asyncio.timeout_at(deadline):
                async with self.session.post(
                    self.origins[index] + "/v1/chat/completions",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    allow_redirects=False,
                    trace_request_ctx={"connection": connection}
                    if connection is not None
                    else None,
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
                    stage = "prepare_downstream"
                    await response.prepare(request)
                    row["http_status"] = 200
                    total = 0
                    parser = StreamParser(
                        max_stream_bytes=self.max_stream_bytes,
                        max_event_bytes=262_144,
                        max_content_events=65_536,
                    )
                    while True:
                        stage = "read_upstream_stream"
                        chunk = await upstream.content.read(16_384)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.max_stream_bytes:
                            row["outcome"] = "stream_limit"
                            return response
                        parser.feed(chunk, time.monotonic_ns())
                        if row["first_byte_ms"] is None:
                            row["first_byte_ms"] = (
                                time.monotonic_ns() - arrived_ns
                            ) / 1e6
                        stage = "write_downstream"
                        await response.write(chunk)  # aiohttp awaits downstream drain.
                        row["stream_bytes"] = total
                    parser.finish()
                    stage = "write_downstream_eof"
                    await response.write_eof()
                    row["outcome"] = "completed"
                    stage = "release_upstream"
        except (ConnectionResetError, BrokenPipeError) as error:
            self._record_transport(error, stage, row, response, arrived_ns, connection)
            if not response.prepared:
                raise
            row["outcome"] = "disconnected"
        except TimeoutError as error:
            self._record_transport(error, stage, row, response, arrived_ns, connection)
            if not response.prepared:
                raise
            row["outcome"] = "timeout"
        except StreamError:
            row["outcome"] = "upstream_protocol_error"
        except (aiohttp.ClientError, OSError) as error:
            self._record_transport(error, stage, row, response, arrived_ns, connection)
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
    async def echo_request_id(
        request: web.Request, response: web.StreamResponse
    ) -> None:
        # A prepare hook also covers streamed responses and aiohttp error replies.
        request_id = request.get(_REQUEST_ID_KEY)
        if request_id is not None:
            response.headers[REQUEST_ID_HEADER] = request_id

    app = web.Application(client_max_size=router.max_body_bytes)
    app.router.add_post("/v1/chat/completions", router.chat)
    app.router.add_get("/router/stats", router.stats)
    app.on_startup.append(router.start)
    app.on_cleanup.append(router.stop)
    app.on_response_prepare.append(echo_request_id)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Two-replica vLLM streaming router")
    parser.add_argument("--replica-a", required=True)
    parser.add_argument("--replica-b", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument(
        "--diagnostics", type=Path, help="fresh private transport diagnostic JSONL"
    )
    parser.add_argument(
        "--policy",
        choices=(
            "round_robin",
            "least_busy",
            "cache_only",
            "cache_plus_load",
            "cache_saturation",
        ),
        default="round_robin",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--max-active", type=int, default=32)
    parser.add_argument("--max-queue", type=int, default=32)
    parser.add_argument("--saturation-active", type=int)
    parser.add_argument(
        "--upstream-keepalive-s",
        type=float,
        default=UPSTREAM_KEEPALIVE_S,
        help="Maximum upstream pooled idle time; keep below the server idle timeout",
    )
    parser.add_argument(
        "--no-upstream-reuse",
        action="store_true",
        help="Use a fresh upstream connection per POST (diagnostic control)",
    )
    args = parser.parse_args()
    router = Router(
        (args.replica_a, args.replica_b),
        args.ledger,
        policy=args.policy,
        max_active=args.max_active,
        max_queue=args.max_queue,
        saturation_active=args.saturation_active,
        diagnostics=args.diagnostics,
        upstream_keepalive_s=args.upstream_keepalive_s,
        reuse_upstream_connections=not args.no_upstream_reuse,
    )
    web.run_app(make_app(router), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
