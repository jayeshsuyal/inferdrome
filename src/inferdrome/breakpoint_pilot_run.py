"""Bounded local collection and CPU loopback rehearsal for one paired pilot.

This module never rents a machine or uploads evidence. The real runner requires
an already supplied host and separately approved billing envelope. Its process
cleanup does not terminate provider billing.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from inferdrome import vllm_router_gpu as gpu
from inferdrome.evaluation.stream import StreamParser
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import make_timing
from inferdrome.vllm_paired_comparison import INPUT_SCHEMA, compare
from inferdrome.vllm_paired_protocol import make_protocol, validate_protocol
from inferdrome.vllm_request_identity import _read_json
from inferdrome.vllm_router import Router, make_app
from inferdrome.vllm_router_study import make_plan, run_trial

SCHEMA = "inferdrome.breakpoint-pilot-session.v1"
EXPORT_RESERVE_S = 600


def _hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _save(path: Path, value: object) -> str:
    """Create only; callers retain partial directories after failures."""
    data = canonical_json_bytes(value) + b"\n"
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sync(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


@dataclass(frozen=True)
class Deadline:
    """Monotonic remaining collection time, excluding an explicit cleanup reserve."""

    stop_at: float

    def require(self, seconds: float = 0) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("invalid time reservation")
        if time.monotonic() + seconds >= self.stop_at:
            raise gpu.StudyError("pilot collection deadline leaves insufficient time")

    def remaining(self) -> float:
        self.require()
        return self.stop_at - time.monotonic()


def billing_deadline(
    *,
    billing_start_utc: str,
    hourly_rate_usd: str,
    cap_usd: str,
    reserve_usd: str,
    max_session_s: int,
    cleanup_s: int,
) -> Deadline:
    """Include setup already billed; a local deadline cannot stop cloud charges."""
    started = datetime.fromisoformat(billing_start_utc.replace("Z", "+00:00"))
    if started.tzinfo is None:
        raise ValueError("billing start must include a timezone")
    elapsed = (datetime.now(UTC) - started.astimezone(UTC)).total_seconds()
    if elapsed < 0:
        raise ValueError("billing start must not be in the future")
    rate, cap, reserve = map(Decimal, (hourly_rate_usd, cap_usd, reserve_usd))
    if (
        not all(value.is_finite() for value in (rate, cap, reserve))
        or rate <= 0
        or cap <= 0
        or reserve < 0
        or reserve >= cap
        or type(max_session_s) is not int
        or not 1 <= max_session_s <= 12_600
        or type(cleanup_s) is not int
        or not 1 <= cleanup_s < max_session_s
    ):
        raise ValueError("billing envelope is invalid")
    usable = min(float((cap - reserve) * 3600 / rate), max_session_s - cleanup_s)
    deadline = Deadline(time.monotonic() + usable - elapsed)
    deadline.require()
    return deadline


async def _response(
    session: aiohttp.ClientSession, method: str, url: str
) -> dict[str, Any]:
    async with session.request(
        method, url, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False
    ) as response:
        raw = bytearray()
        async for chunk in response.content.iter_chunked(16_384):
            raw.extend(chunk)
            if len(raw) > 1_048_576:
                raise gpu.StudyError("engine control response exceeds size bound")
        return {"status": response.status, "body": raw.decode("utf-8")}


async def _quiescent(
    session: aiohttp.ClientSession,
    port: int,
    retained: dict[str, Any] | None = None,
    field: str = "metrics",
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(5):
            while True:
                reply = await _response(
                    session, "GET", f"http://127.0.0.1:{port}/metrics"
                )
                observations.append(reply)
                receipt = {**reply, "observations": observations}
                if retained is not None:
                    retained[field] = receipt
                metrics = gpu._metrics(reply["body"])
                if any(not math.isfinite(value) for value in metrics.values()):
                    raise gpu.StudyError("engine metrics contain a nonfinite value")
                keys = ("vllm:num_requests_running", "vllm:num_requests_waiting")
                reply["metrics"] = metrics
                receipt["metrics"] = metrics
                if reply["status"] != 200 or any(key not in metrics for key in keys):
                    raise gpu.StudyError("engine quiescence metrics are unavailable")
                if all(metrics[key] == 0 for key in keys):
                    return receipt
                await asyncio.sleep(0.2)
    except TimeoutError as error:
        raise gpu.StudyError(
            "engine is not observably quiescent after settling"
        ) from error


async def warm_reset(
    session: aiohttp.ClientSession,
    ports: tuple[int, int],
    output: Path,
    label: str,
    model: str,
) -> dict[str, Any]:
    """Keep exact responses, including a refused reset; never infer success."""
    record: dict[str, Any] = {
        "trial_id": label,
        "status": "INCOMPLETE",
        "replicas": [],
        "error": None,
    }
    try:
        # Both engines finish warmup before either cache is reset.
        for port in ports:
            replica: dict[str, Any] = {"port": port}
            record["replicas"].append(replica)
            body = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": "Disjoint startup health check: count to two.",
                    }
                ],
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_tokens": 8,
                "ignore_eos": True,
                "temperature": 0,
                "n": 1,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            parser = StreamParser(
                max_stream_bytes=1_048_576,
                max_event_bytes=65_536,
                max_content_events=512,
            )
            async with session.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                json=body,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as response:
                replica["warmup_http_status"] = response.status
                if (
                    response.status != 200
                    or response.content_type != "text/event-stream"
                ):
                    raw = await response.content.read(65_537)
                    replica["warmup_error_response"] = {
                        "body": raw[:65_536].decode("utf-8", errors="replace"),
                        "truncated": len(raw) > 65_536,
                    }
                    raise gpu.StudyError("warmup HTTP/content type failed")
                async for chunk in response.content.iter_chunked(16_384):
                    parser.feed(chunk, time.monotonic_ns())
                parser.finish()
            replica["warmup_prompt_tokens"] = parser.prompt_tokens
            replica["warmup_completion_tokens"] = parser.completion_tokens
            if parser.completion_tokens != 8 or parser.prompt_tokens is None:
                raise gpu.StudyError("warmup token accounting failed")
        for replica in record["replicas"]:
            await _quiescent(session, replica["port"], replica, "before_reset")
        for replica in record["replicas"]:
            reply = await _response(
                session,
                "POST",
                f"http://127.0.0.1:{replica['port']}/reset_prefix_cache",
            )
            replica["reset_response"] = reply
            parsed = _read_json(reply["body"].encode())
            if (
                reply["status"] != 200
                or not isinstance(parsed, dict)
                or set(parsed) != {"success"}
                or parsed["success"] is not True
            ):
                raise gpu.StudyError("prefix reset did not return exact success=true")
        for replica in record["replicas"]:
            await _quiescent(session, replica["port"], replica, "after_reset")
        record["status"] = "COMPLETED"
        return record
    except BaseException as error:
        record["error"] = f"{type(error).__name__}: {str(error)[:300]}"
        raise
    finally:
        _save(output / f"{label}-reset.json", record)


async def _trial(
    bundle: dict[str, Any],
    trial: dict[str, Any],
    output: Path,
    ports: tuple[int, int],
    router_port: int,
    deadline: Deadline,
) -> dict[str, Any]:
    block = trial["block"] - 1
    plan = bundle["plans"][block]
    certificate = bundle["certificates"][block]
    timing = bundle["timings"][block][trial["condition"]]
    label = trial["trial_id"]
    ledger = output / f"{label}-ledger.jsonl"
    # Router opens with append semantics: independently reserve a new empty file.
    with ledger.open("xb"):
        pass
    router = Router(
        (f"http://127.0.0.1:{ports[0]}", f"http://127.0.0.1:{ports[1]}"),
        ledger,
        policy=trial["policy"],
        max_active=128,
        max_queue=256,
    )
    runner = web.AppRunner(make_app(router), shutdown_timeout=5)
    result: dict[str, Any] | None = None
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", router_port).start()
        before = time.monotonic()
        async with asyncio.timeout(deadline.remaining()):
            result = await run_trial(
                plan,
                router_origin=f"http://127.0.0.1:{router_port}",
                model=bundle["protocol"]["model"],
                expected_policy=trial["policy"],
                token_certificate=certificate,
                timing=timing,
                correlate_requests=True,
            )
            # Save first: interruption while waiting must not discard measurement.
            _save(output / f"{label}-result.json", result)
            measurement = result["measurement"]
            # The client begins after a stats request. Honor its reported start
            # as well as a monotonic minimum, without trusting wall-clock jumps.
            remaining = max(
                before + plan["duration_ns"] / 1e9 - time.monotonic(),
                (
                    int(measurement["started_unix_ns"])
                    + plan["duration_ns"]
                    - time.time_ns()
                )
                / 1e9,
                0,
            )
            if remaining:
                await asyncio.sleep(remaining)
            deadline.require()
            if measurement["status"] != "COMPLETED":
                raise gpu.TrialInterrupted("client trial did not complete")
    finally:
        await runner.cleanup()
        if ledger.exists():
            _sync(ledger)
        # run_trial deliberately retains a cancelled population before returning.
        if result is not None and not (output / f"{label}-result.json").exists():
            _save(output / f"{label}-result.json", result)
    return {
        "trial_id": label,
        "timing": timing,
        "result": result,
        "ledger_rows": [_read_json(line) for line in ledger.read_bytes().splitlines()],
        "token_certificate": certificate,
        "execution": {
            **{
                key: bundle["protocol"][key]
                for key in (
                    "source_revision",
                    "environment_sha256",
                    "reset_procedure_sha256",
                )
            },
            "reset_completed": True,
        },
    }


def _retain_inputs(bundle: dict[str, Any], output: Path) -> dict[str, Any]:
    """The raw collection is independently replayable without absolute paths."""
    _save(output / "protocol.json", bundle["protocol"])
    plan_paths = []
    for index, plan in enumerate(bundle["plans"], 1):
        path = f"b{index:02d}-plan.json"
        _save(output / path, plan)
        plan_paths.append(path)
        certificate = bundle["certificates"][index - 1]
        if certificate is not None:
            _save(output / f"b{index:02d}-certificate.json", certificate)
        for condition, timing in bundle["timings"][index - 1].items():
            _save(output / f"b{index:02d}-{condition}-timing.json", timing)
    return {
        "schema": INPUT_SCHEMA,
        "protocol_sha256": bundle["protocol"]["protocol_sha256"],
        "plans": plan_paths,
        "trials": [],
    }


async def collect(
    bundle: dict[str, Any],
    output: Path,
    *,
    ports: tuple[int, int],
    router_port: int,
    deadline: Deadline,
    reset_allowance_s: float = 60,
) -> dict[str, Any]:
    """Collect exactly the frozen order; caller owns directory and engines."""
    validate_protocol(bundle["protocol"], bundle["plans"])
    if len(bundle["protocol"]["trials"]) != 32 or len(bundle["plans"]) != 8:
        raise ValueError("pilot requires exactly eight blocks and 32 trials")
    manifest = _retain_inputs(bundle, output)
    inputs: list[dict[str, Any]] = []
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            for trial in bundle["protocol"]["trials"]:
                plan = bundle["plans"][trial["block"] - 1]
                deadline.require(
                    reset_allowance_s + (plan["duration_ns"] + plan["drain_ns"]) / 1e9
                )
                label = trial["trial_id"]
                async with asyncio.timeout(
                    min(reset_allowance_s, deadline.remaining())
                ):
                    await warm_reset(
                        session, ports, output, label, bundle["protocol"]["model"]
                    )
                item = await _trial(bundle, trial, output, ports, router_port, deadline)
                inputs.append(item)
                block = trial["block"]
                manifest["trials"].append(
                    {
                        "trial_id": label,
                        "timing": f"b{block:02d}-{trial['condition']}-timing.json",
                        "result": f"{label}-result.json",
                        "ledger": f"{label}-ledger.jsonl",
                        "token_certificate": (
                            f"b{block:02d}-certificate.json"
                            if item["token_certificate"] is not None
                            else None
                        ),
                        "execution": item["execution"],
                    }
                )
                progress = compare(bundle["protocol"], bundle["plans"], inputs)
                _save(
                    output / f"progress-{trial['sequence']:02d}.json",
                    {
                        "completed_trial": label,
                        "supplied_trials": len(inputs),
                        "reset_receipt_sha256": gpu._file_digest(
                            output / f"{label}-reset.json"
                        ),
                        "result_file_sha256": gpu._file_digest(
                            output / f"{label}-result.json"
                        ),
                        "ledger_file_sha256": gpu._file_digest(
                            output / f"{label}-ledger.jsonl"
                        ),
                        "comparison": progress,
                    },
                )
                operational = [
                    reason
                    for reason in progress["ineligibility_reasons"]
                    if reason.get("trial_id") == label
                ]
                if operational:
                    raise gpu.StudyError(
                        f"trial failed comparison gates: {operational}"
                    )
                async with asyncio.timeout(min(5, deadline.remaining())):
                    for port in ports:
                        await _quiescent(session, port)
        report = compare(bundle["protocol"], bundle["plans"], inputs)
        _save(output / "comparison.json", report)
        return report
    finally:
        _save(output / "inputs.json", manifest)


def _fixture_bundle() -> dict[str, Any]:
    plans = [
        make_plan(
            phase="fixture",
            seed=1009 + index,
            count=8,
            expected_prompt_tokens=200,
            duration_ns=100_000_000,
            max_tokens=4,
        )
        for index in range(8)
    ]
    parameters = {
        "group_size": 4,
        "retained_spacing_bps": 2500,
        "max_advance_ns": 25_000_000,
    }
    protocol = make_protocol(
        plans,
        policy_a="cache_only",
        policy_b="least_busy",
        candidate_parameters=parameters,
        order_seed=104729,
        minimum_effect_microrps=100000,
        max_scheduling_lag_p95_ns=100_000_000,
        max_client_queue_p95_ns=100_000_000,
        model="synthetic-loopback-model",
        source_revision="0" * 40,
        environment_sha256=_hash({"scope": "CPU_LOOPBACK_SYNTHETIC_ONLY"}),
        reset_procedure_sha256=_hash({"scope": "FAKE_ENGINES_REAL_HTTP_RESETS"}),
    )
    timings = [
        {
            "baseline": make_timing(
                plan, group_size=1, retained_spacing_bps=10000, max_advance_ns=0
            ),
            "candidate": make_timing(plan, **parameters),
        }
        for plan in plans
    ]
    return {
        "protocol": protocol,
        "plans": plans,
        "certificates": [None] * 8,
        "timings": timings,
    }


def _fake_engine(
    *, reset_success: object = True, metrics_text: str | None = None
) -> web.Application:
    async def chat(request: web.Request) -> web.Response:
        body = await request.json()
        completion = body["max_tokens"]
        prompt = 15 if completion == 8 else 200
        content = (
            'data: {"choices":[{"index":0,"delta":{"content":"x"},'
            '"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{},'
            '"finish_reason":"length"}]}\n\n'
            + "data: "
            + json.dumps(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": prompt + completion,
                    },
                }
            )
            + "\n\ndata: [DONE]\n\n"
        )
        return web.Response(text=content, content_type="text/event-stream")

    async def metrics(_request: web.Request) -> web.Response:
        return web.Response(
            text=metrics_text
            if metrics_text is not None
            else ("vllm:num_requests_running 0\nvllm:num_requests_waiting 0\n")
        )

    async def reset(_request: web.Request) -> web.Response:
        return web.json_response({"success": reset_success})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_post("/reset_prefix_cache", reset)
    app.router.add_get("/metrics", metrics)
    return app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def rehearse(
    output: Path, *, reset_success: object = True, metrics_text: str | None = None
) -> dict[str, Any]:
    """Real loopback HTTP, router, client and comparison; no model or GPU."""
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    runners: list[web.AppRunner] = []
    session: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "FAILED",
        "evidence_class": "SYNTHETIC_ONLY",
        "evidence_eligible": False,
        "provider_teardown": "NOT_APPLICABLE_NO_PROVIDER",
        "limitations": ["Fake SSE engines; no GPU or model performance evidence."],
    }
    try:
        ports: list[int] = []
        for _ in range(2):
            runner = web.AppRunner(
                _fake_engine(reset_success=reset_success, metrics_text=metrics_text)
            )
            runners.append(runner)
            await runner.setup()
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            port = int(sock.getsockname()[1])
            await web.SockSite(runner, sock).start()
            ports.append(port)
        report = await collect(
            _fixture_bundle(),
            output,
            ports=(ports[0], ports[1]),
            router_port=_free_port(),
            deadline=Deadline(time.monotonic() + 300),
            reset_allowance_s=5,
        )
        session.update(status="COMPLETED", supplied_trials=report["supplied_trials"])
        return report
    except BaseException as error:
        session["error"] = f"{type(error).__name__}: {str(error)[:300]}"
        if isinstance(error, asyncio.CancelledError | gpu.TrialInterrupted):
            session["status"] = "INTERRUPTED"
        raise
    finally:
        for runner in runners:
            await runner.cleanup()
        session["local_cleanup"] = "COMPLETED"
        _save(output / "session-status.json", session)


def _command(arguments: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.run(
        arguments, cwd=cwd, capture_output=True, text=True, check=True, timeout=30
    ).stdout.strip()


def verify_source(source_dir: Path, revision: str) -> None:
    source_dir = source_dir.resolve()
    if (
        Path(__file__).resolve()
        != source_dir / "src/inferdrome/breakpoint_pilot_run.py"
    ):
        raise gpu.StudyError("collector must run from the declared source checkout")
    if _command(["git", "rev-parse", "HEAD"], cwd=source_dir) != revision:
        raise gpu.StudyError("source checkout does not match prepared revision")
    if _command(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=source_dir
    ):
        raise gpu.StudyError("source checkout must be clean before execution")


def _observed_gpus() -> list[dict[str, str]]:
    text = _command(
        [
            "nvidia-smi",
            "--query-gpu=name,uuid,memory.total,memory.used,driver_version",
            "--format=csv,noheader",
        ]
    )
    rows = [row.strip().split(", ") for row in text.splitlines()]
    if (
        len(rows) != 2
        or any(len(row) != 5 for row in rows)
        or rows[0][0] != rows[1][0]
        or rows[0][0] not in gpu.GPU_NAMES
        or rows[0][1] == rows[1][1]
        or any(int(row[3].split()[0]) > 500 for row in rows)
    ):
        raise gpu.StudyError("expected two distinct idle matched A100 40GB GPUs")
    return [
        {
            "index": str(index),
            "model": row[0],
            "uuid_sha256": "sha256:" + hashlib.sha256(row[1].encode()).hexdigest(),
            "memory_total": row[2],
            "memory_used_before": row[3],
            "driver": row[4],
        }
        for index, row in enumerate(rows)
    ]


async def _ready(
    processes: list[subprocess.Popen[bytes]], deadline: Deadline, model: str
) -> None:
    async with asyncio.timeout(min(1800, deadline.remaining())):
        async with aiohttp.ClientSession(trust_env=False) as session:
            for port, process in zip(gpu.ENGINE_PORTS, processes, strict=True):
                while True:
                    deadline.require()
                    if process.poll() is not None:
                        raise gpu.StudyError("engine exited during startup")
                    try:
                        health = await _response(
                            session, "GET", f"http://127.0.0.1:{port}/health"
                        )
                        if health["status"] == 200:
                            break
                    except (aiohttp.ClientError, OSError, TimeoutError):
                        pass
                    await asyncio.sleep(2)
                models = await _response(
                    session, "GET", f"http://127.0.0.1:{port}/v1/models"
                )
                if models["status"] != 200 or model not in {
                    item["id"] for item in _read_json(models["body"].encode())["data"]
                }:
                    raise gpu.StudyError("engine model identity failed")


def _stop_owned(processes: list[subprocess.Popen[bytes]], logs: list[Any]) -> None:
    """Attempt every owned group even if one stop fails; never target other PIDs."""
    errors = []
    for index, process in enumerate(processes):
        try:
            gpu._stop_engines([process], [])
        except Exception as error:
            errors.append(f"replica {index}: {type(error).__name__}: {error}")
    for log in logs:
        try:
            log.close()
        except Exception as error:
            errors.append(f"log close: {type(error).__name__}: {error}")
    if errors:
        raise gpu.StudyError("owned cleanup failed: " + "; ".join(errors))


def _start_engines(
    executable: Path,
    model_dir: Path,
    output: Path,
    context_length: int,
    *,
    processes: list[subprocess.Popen[bytes]] | None = None,
    logs: list[Any] | None = None,
) -> tuple[list[subprocess.Popen[bytes]], list[Any]]:
    """An explicit engine executable must not inherit collector import overlays."""
    processes = [] if processes is None else processes
    logs = [] if logs is None else logs
    try:
        for index, port in enumerate(gpu.ENGINE_PORTS):
            log = (output / f"engine-{index}.log").open("xb")
            logs.append(log)
            env = dict(os.environ)
            for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
                env.pop(name, None)
            env.update(
                {
                    "CUDA_VISIBLE_DEVICES": str(index),
                    "HF_HUB_OFFLINE": "1",
                    "VLLM_SERVER_DEV_MODE": "1",
                    "PATH": str(executable.parent) + os.pathsep + env.get("PATH", ""),
                }
            )
            processes.append(
                subprocess.Popen(
                    gpu._engine_argv(
                        executable, model_dir, port, context_length=context_length
                    ),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                )
            )
    except BaseException:
        _stop_owned(processes, logs)
        raise
    return processes, logs


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Future manual host execution; no provider lifecycle operations here."""
    from inferdrome.breakpoint_pilot import load_prepared, verify
    from inferdrome.breakpoint_pilot_guard import (
        approval_sha256,
        deadlines,
        load_approval,
        validate_ready_receipt,
    )
    from inferdrome.vllm_router_capacity import _cache_capacity

    verify(args.prepared_dir, args.tokenizer_dir)
    bundle = load_prepared(args.prepared_dir)
    plan = bundle["plan"]
    verify_source(args.source_dir, plan["source_revision"])
    approval = load_approval(args.approval)
    if (
        approval["plan_sha256"] != plan["plan_sha256"]
        or approval["source_revision"] != plan["source_revision"]
        or approval["image_reference"] != plan["config"]["pins"]["image_reference"]
        or approval["max_session_s"] > plan["config"]["runtime"]["max_session_s"]
        or approval["cleanup_reserve_s"]
        < plan["config"]["runtime"]["teardown_reserve_s"]
    ):
        raise gpu.StudyError("approval does not bind this prepared pilot")
    deadline = billing_deadline(
        billing_start_utc=approval["billing_start_utc"],
        hourly_rate_usd=approval["hourly_rate_usd"],
        cap_usd=approval["cap_usd"],
        reserve_usd=approval["reserve_usd"],
        max_session_s=approval["max_session_s"],
        cleanup_s=approval["cleanup_reserve_s"] + EXPORT_RESERVE_S,
    )
    runtime = plan["config"]["runtime"]
    reservation = (
        plan["config"]["workload"]["duration_s"]
        + runtime["drain_allowance_s_per_trial"]
        + runtime["warm_reset_setup_allowance_s_per_trial"]
    ) * 32
    deadline.require(reservation)
    output = args.output_dir
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    guard_cleanup, hard_deadline = deadlines(approval)
    session: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "FAILED",
        "evidence_class": "LOCAL_MEASUREMENT_ONLY",
        "evidence_eligible": False,
        "scope": "EXPLORATORY_PILOT_ONLY",
        "source_revision": plan["source_revision"],
        "plan_sha256": plan["plan_sha256"],
        "approval_sha256": approval_sha256(approval),
        "collector_deadline_utc": (
            guard_cleanup - timedelta(seconds=EXPORT_RESERVE_S)
        ).isoformat(),
        "guard_cleanup_start_utc": guard_cleanup.isoformat(),
        "hard_deadline_utc": hard_deadline.isoformat(),
        "local_cleanup_export_reserve_s": EXPORT_RESERVE_S,
        "image_runtime_provenance": "OPERATOR_DECLARED_NOT_ATTESTED",
        "provider_teardown": "UNVERIFIED_EXTERNAL_GUARDIAN_REQUIRED",
        "guardian_liveness": "RECENT_RECEIPT_NOT_FUTURE_UPTIME_ATTESTATION",
        "deadline_limitations": (
            "Network collection is bounded; synchronous snapshot hashing and disk "
            "writes are not interruptible by asyncio. External guardian uptime "
            "and provider teardown must be supervised independently."
        ),
        "error": None,
    }
    processes: list[subprocess.Popen[bytes]] = []
    logs: list[Any] = []
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    old_handler = signal.getsignal(signal.SIGTERM)
    if current is not None:
        loop.add_signal_handler(signal.SIGTERM, current.cancel)
    try:
        _save(output / "approval.json", approval)
        session["snapshot"] = gpu._snapshot(args.model_dir)
        version = _command([str(args.vllm_executable), "--version"])
        if not re.fullmatch(r"(?:vllm\s+)?0\.26\.0(?:\+[\w.-]+)?", version):
            raise gpu.StudyError("vLLM version differs from pinned 0.26.0")
        flags = _command([str(args.vllm_executable), "serve", "--help=all"])
        if any(
            flag not in flags
            for flag in (
                "--enable-prefix-caching",
                "--no-enable-log-requests",
                "--max-model-len",
            )
        ):
            raise gpu.StudyError("vLLM executable is missing required pinned flags")
        session["vllm_version"] = version
        session["gpus"] = _observed_gpus()
        if not gpu._ports_closed():
            raise gpu.StudyError("engine or router loopback port is already occupied")
        if args.guard_receipt.is_symlink() or not args.guard_receipt.is_file():
            raise gpu.StudyError("guardian ready receipt must be a regular file")
        with args.guard_receipt.open("rb") as stream:
            receipt_bytes = stream.read(65_537)
        if len(receipt_bytes) > 65_536:
            raise gpu.StudyError("guardian receipt exceeds size bound")
        receipt = _read_json(receipt_bytes)
        validate_ready_receipt(approval, receipt)
        _save(output / "guardian-ready.json", receipt)
        deadline.require(reservation)
        _save(output / "preflight.json", session)
        processes, logs = _start_engines(
            args.vllm_executable,
            args.model_dir,
            output,
            plan["config"]["workload"]["context_length"],
            processes=processes,
            logs=logs,
        )
        await _ready(processes, deadline, bundle["protocol"]["model"])
        capacity = _cache_capacity(
            [output / f"engine-{index}.log" for index in range(2)],
            bundle["plans"][0]["workload"],
        )
        _save(output / "cache-capacity.json", capacity)
        if not capacity["candidate_valid"]:
            raise gpu.StudyError(
                "observed GPU cache capacity does not fit pilot design"
            )
        deadline.require(reservation)
        report = await collect(
            bundle,
            output,
            ports=gpu.ENGINE_PORTS,
            router_port=gpu.ROUTER_PORT,
            deadline=deadline,
            reset_allowance_s=runtime["warm_reset_setup_allowance_s_per_trial"],
        )
        session.update(status="COMPLETED", supplied_trials=report["supplied_trials"])
        return report
    except BaseException as error:
        session["error"] = f"{type(error).__name__}: {str(error)[:300]}"
        if isinstance(
            error, asyncio.CancelledError | KeyboardInterrupt | gpu.TrialInterrupted
        ):
            session["status"] = "INTERRUPTED"
        raise
    finally:
        cleanup_error: str | None = None
        try:
            _stop_owned(processes, logs)
            if processes and not gpu._ports_closed():
                raise gpu.StudyError("owned engine or router port remains open")
            session["local_cleanup"] = "COMPLETED"
        except Exception as error:
            cleanup_error = f"{type(error).__name__}: {str(error)[:300]}"
            session.update(status="CLEANUP_UNCONFIRMED", local_cleanup=cleanup_error)
        session["ended_utc"] = datetime.now(UTC).isoformat()
        session["engine_logs"] = {}
        session["engine_log_errors"] = {}
        for path in output.glob("engine-*.log"):
            if cleanup_error is not None:
                session["engine_log_errors"][path.name] = (
                    "NOT_HASHED_CLEANUP_UNCONFIRMED"
                )
                continue
            try:
                session["engine_logs"][path.name] = gpu._file_digest(path)
            except OSError as error:
                session["engine_log_errors"][path.name] = str(error)[:300]
                session["status"] = "FAILED"
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, old_handler)
        _save(output / "session-status.json", session)
        if cleanup_error:
            raise gpu.StudyError(f"local process cleanup failed: {cleanup_error}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    rehearsal = commands.add_parser("rehearse", help="CPU-only live loopback rehearsal")
    rehearsal.add_argument("--output-dir", type=Path, required=True)
    execution = commands.add_parser("run", help="manually authorized local GPU session")
    for name in (
        "prepared-dir",
        "tokenizer-dir",
        "model-dir",
        "output-dir",
        "source-dir",
        "vllm-executable",
        "approval",
        "guard-receipt",
    ):
        execution.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "rehearse":
            report = asyncio.run(rehearse(args.output_dir))
            print(
                f"SYNTHETIC_ONLY: {report['supplied_trials']} loopback trials verified"
            )
        else:
            asyncio.run(run(args))
    except (
        OSError,
        ValueError,
        gpu.StudyError,
        TimeoutError,
        subprocess.SubprocessError,
    ) as error:
        print(f"breakpoint pilot: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
