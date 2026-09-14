"""Actual local socket rehearsals for routing faults; no GPU/capacity claims."""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from aiohttp import web

from inferdrome.evaluation.fault_config import (
    RoutingFaultConfig,
    load_routing_config_bytes,
)
from inferdrome.evaluation.faults import RoutingFaultResult, run_routing_fault
from inferdrome.evaluation.observations import (
    AiohttpProbeTransport,
    ProbeError,
    ProbeLimitError,
    ProbeResponse,
)
from inferdrome.evaluation.policies import POLICY_IDS, PolicyId
from inferdrome.evaluation.transport import AiohttpTransport

_MODEL = "private-routing-model-sentinel"
_FOREGROUND = "private-foreground-prompt-sentinel"
_BACKGROUND = "private-background-prompt-sentinel"
_OUTPUT = "private-output-sentinel-🌊"
_GUARD_SECONDS = 5


def _event(value: object) -> bytes:
    return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\n\n"


@dataclass
class _Replica:
    active: int = 0
    background_active: int = 0
    background_requests: int = 0
    foreground_requests: int = 0
    metrics_mode: str = "normal"
    redirect_to: str = ""
    connections: set[asyncio.Transport] = field(default_factory=set)
    handlers: set[asyncio.Task[object]] = field(default_factory=set)
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    two_background_started: asyncio.Event = field(default_factory=asyncio.Event)
    blocked_probe_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_streams: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        task = asyncio.current_task()
        assert task is not None
        assert request.transport is not None
        self.handlers.add(task)
        self.connections.add(request.transport)
        self.requests.append((request.path, dict(request.headers)))
        try:
            if request.path == "/health":
                return web.Response()
            if request.path == "/metrics":
                return await self.metrics(request)
            assert request.path == "/v1/chat/completions"
            return await self.complete(request)
        finally:
            self.handlers.remove(task)

    async def metrics(self, request: web.Request) -> web.StreamResponse:
        if self.metrics_mode == "oversize":
            return web.Response(body=b"x" * 4096)
        if self.metrics_mode == "chunked-oversize":
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"x" * 256)
            await self.release_streams.wait()
            return response
        if self.metrics_mode == "oversize-headers":
            return web.Response(headers={f"X-Test-{index}": "x" for index in range(65)})
        if self.metrics_mode == "gzip":
            return web.Response(
                body=b"private-error-sentinel", headers={"Content-Encoding": "gzip"}
            )
        if self.metrics_mode == "redirect":
            return web.Response(status=302, headers={"Location": self.redirect_to})
        if self.metrics_mode in ("stall-error", "stall-body"):
            response = web.StreamResponse(
                status=429 if self.metrics_mode == "stall-error" else 200,
                headers={"Content-Length": "4096"},
            )
            await response.prepare(request)
            self.blocked_probe_started.set()
            await self.release_streams.wait()
            return response
        body = (
            f'vllm:num_requests_running{{model_name="{_MODEL}",engine="0"}} '
            f"{self.active}\n"
            f'vllm:num_requests_waiting{{engine="0",model_name="{_MODEL}"}} 0\n'
        ).encode()
        return web.Response(
            body=body,
            content_type="text/plain",
            headers={"Set-Cookie": "private_cookie=sentinel; Path=/"},
        )

    async def complete(self, request: web.Request) -> web.StreamResponse:
        payload = await request.json()
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["n"] == 1
        assert payload["model"] == _MODEL
        prompt = payload["messages"][0]["content"]
        assert prompt in (_FOREGROUND, _BACKGROUND)
        background = prompt == _BACKGROUND
        self.active += 1
        if background:
            self.background_requests += 1
            self.background_active += 1
            if self.background_active == 2:
                self.two_background_started.set()
        else:
            self.foreground_requests += 1
        try:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
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
            if background:
                # Real requests remain active until the owner cancels their sockets.
                await self.release_streams.wait()
            await response.write(
                _event(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": _OUTPUT},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                )
            )
            await response.write(
                _event(
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 3,
                            "completion_tokens": 4,
                            "total_tokens": 7,
                        },
                    }
                )
            )
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response
        finally:
            self.active -= 1
            if background:
                self.background_active -= 1

    async def assert_disconnected(self) -> None:
        async def settled() -> None:
            while self.handlers or any(
                not transport.is_closing() for transport in self.connections
            ):
                await asyncio.sleep(0.005)

        await asyncio.wait_for(settled(), 1)
        assert self.active == self.background_active == 0


@asynccontextmanager
async def _replicas() -> AsyncIterator[
    tuple[tuple[str, str], tuple[_Replica, _Replica]]
]:
    runners: list[web.AppRunner] = []
    listeners: list[socket.socket] = []
    servers: list[_Replica] = []
    origins: list[str] = []
    try:
        for _ in range(2):
            replica = _Replica()
            servers.append(replica)
            app = web.Application(client_max_size=262_144)
            app.router.add_get("/health", replica.handle)
            app.router.add_get("/metrics", replica.handle)
            app.router.add_post("/v1/chat/completions", replica.handle)
            runner = web.AppRunner(
                app, access_log=None, shutdown_timeout=1, handler_cancellation=True
            )
            runners.append(runner)
            await runner.setup()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.bind(("127.0.0.1", 0))
            origins.append(f"http://127.0.0.1:{listener.getsockname()[1]}")
            await web.SockSite(runner, listener).start()
        yield (origins[0], origins[1]), (servers[0], servers[1])
    finally:
        for replica in servers:
            replica.release_streams.set()
        for runner in runners:
            await asyncio.wait_for(runner.cleanup(), 2)
        for listener in listeners:
            listener.close()
        assert all(not replica.handlers for replica in servers)
        assert all(replica.active == 0 for replica in servers)


def _config(origins: tuple[str, str], policy: PolicyId) -> RoutingFaultConfig:
    def population(background: bool) -> dict[str, object]:
        schedules = (210, 220) if background else (150, 300, 350, 400, 500, 600, 700)
        return {
            "schema_version": "inferdrome.evaluation-config.v1",
            "source_commit": "1" * 40,
            "model": _MODEL,
            "max_tokens": 16,
            "endpoints": [
                {"endpoint_id": f"endpoint-{letter}", "origin": origin}
                for letter, origin in zip("ab", origins, strict=True)
            ],
            "bounds": {
                "max_requests": 16,
                "concurrency": 2,
                "max_queue": 2,
                "duration_ns": 800_000_000,
                "drain_ns": 200_000_000,
                "request_timeout_ns": 1_000_000_000,
                "max_content_events": 16,
            },
            "offers": [
                {
                    "scheduled_ns": milliseconds * 1_000_000,
                    "endpoint_id": "endpoint-a",
                    "prompt": _BACKGROUND if background else _FOREGROUND,
                }
                for milliseconds in schedules
            ],
        }

    return load_routing_config_bytes(
        json.dumps(
            {
                "schema_version": "inferdrome.evaluation-routing-config.v1",
                "foreground": population(False),
                "background": population(True),
                "policy_id": policy,
                "telemetry": {
                    "interval_ns": 25_000_000,
                    "poll_timeout_ns": 100_000_000,
                    "health_freshness_ns": 500_000_000,
                    "load_freshness_ns": 100_000_000,
                    "warmup_ns": 100_000_000,
                    "max_observations": 1000,
                    "max_response_bytes": 4096,
                },
                "fault": {
                    "target_endpoint_id": "endpoint-a",
                    "freeze_start_ns": 200_000_000,
                    "restore_ns": 450_000_000,
                    "background_stop_ns": 650_000_000,
                },
            }
        ).encode()
    )


def _clients(
    config: RoutingFaultConfig,
) -> tuple[
    AiohttpTransport,
    AiohttpTransport,
    AiohttpProbeTransport,
    AiohttpProbeTransport,
]:
    return (
        AiohttpTransport(config.foreground),
        AiohttpTransport(config.background),
        AiohttpProbeTransport(
            config.foreground, max_response_bytes=config.telemetry.max_response_bytes
        ),
        AiohttpProbeTransport(
            config.foreground, max_response_bytes=config.telemetry.max_response_bytes
        ),
    )


def _sanitized(result: RoutingFaultResult, origins: tuple[str, str]) -> None:
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)
    for secret in (_MODEL, _FOREGROUND, _BACKGROUND, _OUTPUT, *origins):
        assert secret not in serialized
    assert result.evidence_class == "LOCAL_MEASUREMENT_ONLY"
    assert result.evidence_eligible is False
    assert result.actual_overload == "NOT_ESTABLISHED_BY_FAULT_SCHEDULE"
    assert result.cleanup == "PUBLICATION_RESTORED_CLIENT_TASKS_AND_CONNECTIONS_CLOSED"
    for population in (result.foreground, result.background):
        assert len(population.records) == population.to_dict()["offered_count"]
        assert sum(population.to_dict()["outcomes"].values()) == len(population.records)
        assert len({row.request_index for row in population.records}) == len(
            population.records
        )


@pytest.mark.parametrize("policy", POLICY_IDS)
def test_real_two_replica_faults_retain_router_load_while_independent_load_changes(
    policy: PolicyId,
) -> None:
    async def exercise() -> None:
        async with _replicas() as (origins, replicas):
            config = _config(origins, policy)
            clients = _clients(config)
            result = await asyncio.wait_for(
                run_routing_fault(config, *clients), _GUARD_SECONDS
            )
            assert result.status == "COMPLETED"
            assert all(client.closed for client in clients)
            await asyncio.gather(
                *(replica.assert_disconnected() for replica in replicas)
            )
            assert len(result.foreground.records) == 7
            assert len(result.background.records) == 2
            assert replicas[0].background_requests == 2
            assert replicas[1].background_requests == 0
            assert all(
                row.endpoint_id == "endpoint-a" and row.attempts == 1
                for row in result.background.records
            )
            assert all(row.outcome == "CANCELLED" for row in result.background.records)
            events = {event.kind: event.observed_ns for event in result.events}
            freeze, restored = events["FREEZE_STARTED"], events["TELEMETRY_RESTORED"]
            assert events["WARMUP_PASSED"] < freeze < restored
            assert restored < events["BACKGROUND_STOP_REQUESTED"]
            before = [
                row
                for row in result.observations
                if row.channel == "ROUTER_LOAD"
                and row.endpoint_id == "endpoint-a"
                and row.published_to_router
                and row.published_ns < freeze
            ]
            retained = max(before, key=lambda row: row.sequence)
            suppressed = [
                row
                for row in result.observations
                if row.channel == "ROUTER_LOAD"
                and row.endpoint_id == "endpoint-a"
                and freeze <= row.published_ns < restored
            ]
            assert suppressed
            assert all(row.published_to_router is False for row in suppressed)
            assert all(row.running is row.waiting is None for row in suppressed)
            assert any(
                row.channel == "INDEPENDENT_LOAD"
                and row.endpoint_id == "endpoint-a"
                and row.status == "VALID"
                and row.running >= 2
                and freeze < row.started_ns < restored
                for row in result.observations
            )
            assert any(
                row.channel == "ROUTER_LOAD"
                and row.endpoint_id == "endpoint-b"
                and row.published_to_router
                and freeze < row.started_ns < restored
                for row in result.observations
            )
            assert any(
                row.channel == "HEALTH"
                and row.endpoint_id == "endpoint-a"
                and row.published_to_router
                and row.healthy
                and freeze < row.started_ns < restored
                for row in result.observations
            )
            during = [
                decision
                for decision in result.decisions
                if freeze < decision.decision_ns < restored
            ]
            assert during
            assert all(
                decision.snapshot.endpoints[0].load.sequence == retained.sequence
                for decision in during
            )
            assert all(
                decision.snapshot.endpoints[0].load.started_ns == retained.started_ns
                for decision in during
            )
            renewed = [
                row
                for row in result.observations
                if row.channel == "ROUTER_LOAD"
                and row.endpoint_id == "endpoint-a"
                and row.published_to_router
                and row.status == "VALID"
                and row.started_ns >= restored
            ]
            assert renewed
            assert all(row.sequence > retained.sequence for row in renewed)
            assert result.recovery["first_restored_publication_ns"] == min(
                row.published_ns for row in renewed
            )
            assert result.recovery["background_active_at_restore"] is True
            if policy == "evaluation_round_robin_v1":
                assert result.recovery["first_decision_using_restored_load_ns"] is None
            else:
                assert (
                    result.recovery["first_decision_using_restored_load_ns"] is not None
                )
                assert (
                    result.recovery["first_dispatch_using_restored_load_ns"] is not None
                )
            if policy == "evaluation_freshness_fallback_v1":
                assert any(decision.mode == "FALLBACK" for decision in during)
            if policy == "evaluation_fail_closed_v1":
                assert any(
                    row.outcome == "REJECTED_ROUTE" for row in result.foreground.records
                )
            else:
                assert all(
                    row.outcome == "SUCCESS" for row in result.foreground.records
                )
            _sanitized(result, origins)

    asyncio.run(exercise())


@pytest.mark.parametrize("interrupt", ["stop-event", "task-cancel"])
def test_cancellation_during_frozen_background_closes_all_four_clients(
    interrupt: str,
) -> None:
    async def exercise() -> None:
        async with _replicas() as (origins, replicas):
            config = _config(origins, "evaluation_freshness_fallback_v1")
            clients = _clients(config)
            stop = asyncio.Event()
            running = asyncio.create_task(
                run_routing_fault(config, *clients, stop=stop)
            )
            await asyncio.wait_for(
                replicas[0].two_background_started.wait(), _GUARD_SECONDS
            )
            if interrupt == "stop-event":
                stop.set()
            else:
                running.cancel()
            result = await asyncio.wait_for(running, _GUARD_SECONDS)
            assert result.status == "CANCELLED"
            kinds = [event.kind for event in result.events]
            assert "FREEZE_STARTED" in kinds
            assert "RESTORED_DURING_CLEANUP" in kinds
            assert "TELEMETRY_RESTORED" not in kinds
            assert len(result.foreground.records) == 7
            assert len(result.background.records) == 2
            assert any(
                row.outcome == "CANCELLED" and row.attempts == 0
                for row in result.foreground.records
            )
            assert all(row.outcome == "CANCELLED" for row in result.background.records)
            assert all(client.closed for client in clients)
            await asyncio.gather(
                *(replica.assert_disconnected() for replica in replicas)
            )
            _sanitized(result, origins)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "mode",
    [
        "oversize",
        "chunked-oversize",
        "oversize-headers",
        "gzip",
        "redirect",
        "stall-error",
    ],
)
def test_real_probe_body_limits_encodings_redirects_and_error_body_cleanup(
    mode: str,
) -> None:
    async def exercise() -> None:
        async with _replicas() as (origins, replicas):
            replicas[0].metrics_mode = mode
            replicas[0].redirect_to = origins[1] + "/metrics"
            config = _config(origins, "evaluation_round_robin_v1")
            transport = AiohttpProbeTransport(config.foreground, max_response_bytes=64)
            try:
                if mode in ("oversize", "chunked-oversize", "oversize-headers", "gzip"):
                    with pytest.raises(
                        ProbeLimitError
                        if mode in ("oversize", "chunked-oversize")
                        else ProbeError
                    ):
                        await asyncio.wait_for(transport.get(origins[0], "/metrics"), 1)
                else:
                    assert await asyncio.wait_for(
                        transport.get(origins[0], "/metrics"), 1
                    ) == (ProbeResponse(302 if mode == "redirect" else 429, b""))
                assert len(replicas[0].requests) == 1
                assert replicas[1].requests == []
            finally:
                await transport.close()
            await replicas[0].assert_disconnected()
            assert transport.closed

    asyncio.run(exercise())


def test_real_probe_ignores_ambient_authority_and_discards_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    for name in ("OPENAI_API_KEY", "VLLM_API_KEY", "HF_TOKEN"):
        monkeypatch.setenv(name, "invented-private-token-sentinel")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ambient credential or proxy lookup attempted")

    monkeypatch.setattr("aiohttp.client.get_env_proxy_for_url", forbidden)
    monkeypatch.setattr("aiohttp.client.netrc_from_env", forbidden)
    monkeypatch.setattr("aiohttp.helpers.netrc_from_env", forbidden)

    async def exercise() -> None:
        async with _replicas() as (origins, replicas):
            config = _config(origins, "evaluation_round_robin_v1")
            transport = AiohttpProbeTransport(
                config.foreground, max_response_bytes=4096
            )
            try:
                for path in ("/metrics", "/health"):
                    assert (await transport.get(origins[0], path)).status == 200
                for _path, headers in replicas[0].requests:
                    assert headers["Accept-Encoding"] == "identity"
                    assert not {key.lower() for key in headers} & {
                        "authorization",
                        "proxy-authorization",
                        "cookie",
                    }
            finally:
                await transport.close()
            await replicas[0].assert_disconnected()

    asyncio.run(exercise())


def test_real_probe_stalled_body_cancellation_closes_socket() -> None:
    async def exercise() -> None:
        async with _replicas() as (origins, replicas):
            replicas[0].metrics_mode = "stall-body"
            config = _config(origins, "evaluation_round_robin_v1")
            transport = AiohttpProbeTransport(
                config.foreground, max_response_bytes=4096
            )
            running = asyncio.create_task(transport.get(origins[0], "/metrics"))
            try:
                await asyncio.wait_for(replicas[0].blocked_probe_started.wait(), 1)
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            finally:
                await transport.close()
            await replicas[0].assert_disconnected()
            assert transport.closed

    asyncio.run(exercise())


@pytest.mark.parametrize("expected_status", ["COMPLETED", "WARMUP_FAILED", "CANCELLED"])
def test_routing_cli_writes_sanitized_population_report_for_each_terminal_status(
    tmp_path: Path,
    expected_status: str,
) -> None:
    async def exercise() -> None:
        async with _replicas() as (origins, replicas):
            if expected_status == "WARMUP_FAILED":
                replicas[0].metrics_mode = "gzip"
            config = _config(origins, "evaluation_freshness_fallback_v1")
            config_path, output_path = (
                tmp_path / "routing.json",
                tmp_path / "result.json",
            )
            config_path.write_text(config.model_dump_json())
            config_path.chmod(0o600)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "inferdrome.evaluation",
                "routing-run",
                "--config",
                str(config_path),
                "--output",
                str(output_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                if expected_status == "CANCELLED":
                    await asyncio.wait_for(
                        replicas[0].two_background_started.wait(),
                        _GUARD_SECONDS,
                    )
                    process.send_signal(signal.SIGTERM)
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), _GUARD_SECONDS
                )
            finally:
                if process.returncode is None:
                    process.kill()
                    await asyncio.wait_for(process.wait(), 1)
            assert (
                process.returncode
                == {
                    "COMPLETED": 0,
                    "WARMUP_FAILED": 2,
                    "CANCELLED": 130,
                }[expected_status]
            ), stderr.decode()
            assert stderr == b""
            report = json.loads(output_path.read_bytes())
            assert report["schema_version"] == "inferdrome.evaluation-routing-result.v1"
            assert report["status"] == expected_status
            assert report["evidence_eligible"] is False
            assert report["foreground"]["offered_count"] == 7
            assert report["background"]["offered_count"] == 2
            assert output_path.stat().st_mode & 0o777 == 0o400
            assert json.loads(stdout)["evidence_eligible"] is False
            if expected_status == "WARMUP_FAILED":
                assert all(
                    row["attempts"] == 0
                    for population in ("foreground", "background")
                    for row in report[population]["records"]
                )
                assert all(
                    replica.background_requests == replica.foreground_requests == 0
                    for replica in replicas
                )
            if expected_status == "CANCELLED":
                assert "RESTORED_DURING_CLEANUP" in {
                    event["kind"] for event in report["events"]
                }
            combined = stdout.decode() + json.dumps(report, ensure_ascii=False)
            for secret in (_MODEL, _FOREGROUND, _BACKGROUND, _OUTPUT, *origins):
                assert secret not in combined
            await asyncio.gather(
                *(replica.assert_disconnected() for replica in replicas)
            )

    asyncio.run(exercise())
