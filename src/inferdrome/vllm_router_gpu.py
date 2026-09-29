"""One bounded, operator-supplied two-GPU vLLM router study.

This module never rents or destroys a provider resource. It owns only the two
local engine process groups it starts, and runs the PR2 study against a fresh
in-process router for every condition. All endpoints bind to loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import aiohttp
from aiohttp import web

from inferdrome.errors import AdapterError
from inferdrome.evaluation.stream import StreamParser
from inferdrome.execution.managed_vllm import snapshot_directory_identity
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
)
from inferdrome.vllm_router import Router, make_app
from inferdrome.vllm_router_study import (
    POLICIES,
    make_plan,
    run_trial,
    validate_plan,
    validate_token_certificate,
    verify_token_lengths,
)

IMAGE = (
    "vastai/vllm@sha256:"
    "39f2f782305dd7bf8478b748140a2ed719de2824bed51d1862f355dde9891f77"
)
ORDERS = (
    ("round_robin", "least_busy", "cache_plus_load", "cache_only"),
    ("least_busy", "cache_only", "round_robin", "cache_plus_load"),
    ("cache_only", "cache_plus_load", "least_busy", "round_robin"),
    ("cache_plus_load", "round_robin", "cache_only", "least_busy"),
)
EVALUATION_SEEDS = (29, 31, 37, 41)
COUNTS = (300, 600, 1200)
PHASE_SEEDS: tuple[
    tuple[Literal["calibration", "evaluation"], tuple[int, ...]], ...
] = (("calibration", (17,)), ("evaluation", EVALUATION_SEEDS))
GPU_NAMES = {"NVIDIA A100-PCIE-40GB", "NVIDIA A100-SXM4-40GB"}
ENGINE_PORTS = (8001, 8002)
ROUTER_PORT = 8090
_METRIC_NAMES = (
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:prefix_cache_hits",
    "vllm:prefix_cache_hits_total",
    "vllm:prefix_cache_queries",
    "vllm:prefix_cache_queries_total",
)


class StudyError(Exception):
    """A specific condition, reset, or local lifecycle failure."""


class TrialInterrupted(StudyError):
    """The client returned a retained interrupted trial."""


def _save(path: Path, value: object) -> str:
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise StudyError(f"artifact is not a regular file: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def prepare(out: Path, tokenizer_root: Path) -> None:
    out.mkdir(parents=True, exist_ok=False)
    for phase, seeds in PHASE_SEEDS:
        for seed in seeds:
            for count in COUNTS:
                stem = f"{phase}-{seed}-{count}"
                plan = make_plan(
                    phase=phase, seed=seed, count=count, expected_prompt_tokens=286
                )
                certificate = verify_token_lengths(plan, tokenizer_root)
                _save(out / f"{stem}-plan.json", plan)
                _save(out / f"{stem}-certificate.json", certificate)


def _plans(
    root: Path,
) -> dict[tuple[str, int, int], tuple[dict[str, Any], dict[str, Any]]]:
    result: dict[tuple[str, int, int], tuple[dict[str, Any], dict[str, Any]]] = {}
    for phase, seeds in PHASE_SEEDS:
        for seed in seeds:
            for count in COUNTS:
                stem = f"{phase}-{seed}-{count}"
                plan = json.loads((root / f"{stem}-plan.json").read_text())
                certificate = json.loads(
                    (root / f"{stem}-certificate.json").read_text()
                )
                validate_plan(plan)
                validate_token_certificate(plan, certificate)
                if plan != make_plan(
                    phase=phase, seed=seed, count=count, expected_prompt_tokens=286
                ):
                    raise StudyError(f"plan mismatch: {stem}")
                result[(phase, seed, count)] = plan, certificate
    return result


def _gpu_observation() -> list[dict[str, str]]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,uuid,memory.total,memory.used,driver_version",
        "--format=csv,noheader",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=True)
    rows = [row.strip().split(", ") for row in completed.stdout.splitlines()]
    if len(rows) != 2 or any(len(row) != 5 for row in rows):
        raise StudyError("expected exactly two observable GPUs")
    if rows[0][0] != rows[1][0] or rows[0][0] not in GPU_NAMES:
        raise StudyError("GPU model is not a matched two-A100 40GB pair")
    if rows[0][1] == rows[1][1]:
        raise StudyError("GPU UUIDs are not distinct")
    if any(int(row[3].split()[0]) > 500 for row in rows):
        raise StudyError("GPUs are not idle before startup")
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


def _snapshot(model_dir: Path) -> dict[str, Any]:
    try:
        identity = snapshot_directory_identity(
            model_dir, kind="model", revision=QWEN3_8B_REVISION
        )
    except AdapterError as error:
        raise StudyError(f"model snapshot verification failed: {error}") from error
    if identity.sha256 != qwen3_expected_snapshot_sha256():
        raise StudyError("model snapshot differs from pinned Qwen3-8B manifest")
    return {
        "sha256": identity.sha256,
        "file_count": identity.file_count,
        "total_bytes": identity.total_bytes,
    }


def _vllm_version(executable: Path) -> str:
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise StudyError("vLLM executable is unavailable")
    completed = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, check=True
    )
    version = completed.stdout.strip()
    if "0.26.0" not in version:
        raise StudyError(f"vLLM version is not 0.26.0: {version[:100]}")
    return version[:100]


@dataclass(frozen=True)
class Budget:
    billing_start: datetime
    hourly_rate: Decimal
    cap: Decimal
    reserve: Decimal

    def require(self, seconds: int) -> None:
        elapsed = Decimal(str((datetime.now(UTC) - self.billing_start).total_seconds()))
        projected = (elapsed + seconds) * self.hourly_rate / Decimal(3600)
        if projected + self.reserve > self.cap:
            raise StudyError(
                "declared spend envelope lacks time for next condition and cleanup"
            )


def _budget(args: argparse.Namespace) -> Budget:
    started = datetime.fromisoformat(args.billing_start_utc.replace("Z", "+00:00"))
    if started.tzinfo is None:
        raise StudyError("billing start needs UTC offset")
    started = started.astimezone(UTC)
    if (started - datetime.now(UTC)).total_seconds() > 30:
        raise StudyError("billing start cannot be in the future")
    rate, cap, reserve = map(
        Decimal, (args.hourly_rate_usd, args.cap_usd, args.reserve_usd)
    )
    if rate <= 0 or cap <= 0 or reserve < 0 or reserve >= cap:
        raise StudyError("declared cost bounds are invalid")
    return Budget(started, rate, cap, reserve)


def _engine_argv(
    executable: Path,
    model_dir: Path,
    port: int,
    *,
    context_length: int = 2048,
    prefix_caching: bool = True,
) -> list[str]:
    return [
        str(executable),
        "serve",
        str(model_dir),
        "--served-model-name",
        QWEN3_8B_MODEL_ID,
        "--revision",
        QWEN3_8B_REVISION,
        "--tokenizer",
        str(model_dir),
        "--tokenizer-revision",
        QWEN3_8B_REVISION,
        "--dtype",
        "bfloat16",
        "--tensor-parallel-size",
        "1",
        "--gpu-memory-utilization",
        "0.90",
        "--max-model-len",
        str(context_length),
        "--enable-prefix-caching" if prefix_caching else "--no-enable-prefix-caching",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-enable-log-requests",
    ]


def _ports_closed() -> bool:
    for port in (*ENGINE_PORTS, ROUTER_PORT):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return False
        except ConnectionRefusedError:
            continue
    return True


def _start_engines(
    executable: Path,
    model_dir: Path,
    out: Path,
    *,
    context_length: int = 2048,
    prefix_caching: bool = True,
) -> tuple[list[subprocess.Popen[bytes]], list[Any]]:
    processes: list[subprocess.Popen[bytes]] = []
    logs: list[Any] = []
    try:
        for index, port in enumerate(ENGINE_PORTS):
            log = (out / f"engine-{index}.log").open("xb")
            logs.append(log)
            env = os.environ.copy()
            env.update(
                {
                    "CUDA_VISIBLE_DEVICES": str(index),
                    "HF_HUB_OFFLINE": "1",
                    "VLLM_SERVER_DEV_MODE": "1",
                }
            )
            process = subprocess.Popen(
                _engine_argv(
                    executable,
                    model_dir,
                    port,
                    context_length=context_length,
                    prefix_caching=prefix_caching,
                ),
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
            processes.append(process)
    except BaseException:
        _stop_engines(processes, logs)
        raise
    return processes, logs


def _stop_engines(processes: list[subprocess.Popen[bytes]], logs: list[Any]) -> None:
    try:
        for process in processes:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            # vLLM may have spawned workers that outlive the CLI process.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    finally:
        for log in logs:
            log.close()


async def _get(session: aiohttp.ClientSession, url: str) -> tuple[int, str]:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
        return response.status, await response.text()


async def _ready(
    session: aiohttp.ClientSession,
    processes: list[subprocess.Popen[bytes]],
    budget: Budget,
) -> None:
    ready = [False, False]
    while not all(ready):
        budget.require(120)
        for index, port in enumerate(ENGINE_PORTS):
            if processes[index].poll() is not None:
                raise StudyError(
                    f"engine {index} exited during startup: "
                    f"{processes[index].returncode}"
                )
            if ready[index]:
                continue
            try:
                status, _ = await _get(session, f"http://127.0.0.1:{port}/health")
                ready[index] = status == 200
            except (aiohttp.ClientError, TimeoutError, OSError):
                pass
        print(f"engine readiness: {sum(ready)}/2", flush=True)
        if not all(ready):
            await asyncio.sleep(5)


def _metrics(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        match = re.match(r"^(vllm:[\w]+)(?:\{[^}]*\})?\s+([\d.eE+-]+)$", line)
        if match and match[1] in _METRIC_NAMES:
            values[match[1]] = values.get(match[1], 0.0) + float(match[2])
    return values


async def _quiescent_metrics(
    session: aiohttp.ClientSession, port: int
) -> dict[str, float]:
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        status, content = await _get(session, f"http://127.0.0.1:{port}/metrics")
        if status != 200:
            raise StudyError(f"engine {port} metrics HTTP {status}")
        metrics = _metrics(content)
        keys = ("vllm:num_requests_running", "vllm:num_requests_waiting")
        missing = [key for key in keys if key not in metrics]
        if missing:
            raise StudyError(
                f"engine {port} metrics missing quiescence gauges: {missing}"
            )
        if all(metrics[key] == 0 for key in keys):
            return metrics
        if asyncio.get_running_loop().time() >= deadline:
            raise StudyError(
                f"engine {port} remained active after metrics settling: "
                f"running={metrics[keys[0]]}, waiting={metrics[keys[1]]}"
            )
        await asyncio.sleep(0.2)


async def _warm_and_reset(
    session: aiohttp.ClientSession, out: Path, label: str
) -> dict[str, Any]:
    record: dict[str, Any] = {"label": label, "replicas": []}
    for port in ENGINE_PORTS:
        body = {
            "model": QWEN3_8B_MODEL_ID,
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
            max_stream_bytes=1_048_576, max_event_bytes=65_536, max_content_events=512
        )
        async with session.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            json=body,
            timeout=aiohttp.ClientTimeout(total=90),
            allow_redirects=False,
        ) as response:
            if response.status != 200 or response.content_type != "text/event-stream":
                raise StudyError(f"engine {port} warmup HTTP/content type failed")
            async for chunk in response.content.iter_chunked(16_384):
                parser.feed(chunk, time.monotonic_ns())
            parser.finish()
        if parser.completion_tokens != 8 or parser.prompt_tokens is None:
            raise StudyError(f"engine {port} warmup usage length failed")
        before = await _quiescent_metrics(session, port)
        async with session.post(
            f"http://127.0.0.1:{port}/reset_prefix_cache",
            timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=False,
        ) as response:
            if response.status != 200 or await response.json() != {"success": True}:
                raise StudyError(
                    f"engine {port} prefix reset did not return success=true"
                )
        after = await _quiescent_metrics(session, port)
        record["replicas"].append(
            {
                "port": port,
                "warmup_prompt_tokens": parser.prompt_tokens,
                "warmup_completion_tokens": parser.completion_tokens,
                "metrics_before_reset": before,
                "metrics_after_reset": after,
                "reset_success": True,
            }
        )
    _save(out / f"reset-{label}.json", record)
    return record


async def _condition(
    session: aiohttp.ClientSession,
    out: Path,
    label: str,
    policy: str,
    plan: dict[str, Any],
    certificate: dict[str, Any],
    *,
    router_max_active: int = 32,
    router_max_queue: int = 32,
) -> dict[str, Any]:
    ledger = out / f"{label}-router.jsonl"
    router = Router(
        (
            f"http://127.0.0.1:{ENGINE_PORTS[0]}",
            f"http://127.0.0.1:{ENGINE_PORTS[1]}",
        ),
        ledger,
        policy=policy,
        max_active=router_max_active,
        max_queue=router_max_queue,
    )
    app = make_app(router)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=ROUTER_PORT)
    try:
        await site.start()
        result = await run_trial(
            plan,
            router_origin=f"http://127.0.0.1:{ROUTER_PORT}",
            model=QWEN3_8B_MODEL_ID,
            expected_policy=policy,
            token_certificate=certificate,
        )
    except BaseException as error:
        _save(
            out / f"{label}-error.json",
            {
                "status": "CONDITION_FAILED",
                "reason": f"{type(error).__name__}: {str(error)[:300]}",
                "ledger_sha256": _file_digest(ledger) if ledger.exists() else None,
            },
        )
        raise
    finally:
        await runner.cleanup()
    result_digest = _save(out / f"{label}-client.json", result)
    return {
        "label": label,
        "policy": policy,
        "plan_sha256": plan["plan_sha256"],
        "result_sha256": result_digest,
        "ledger_sha256": _file_digest(ledger),
        "trial_status": result["status"],
        "comparison_valid": result["comparison_valid"],
        "summary": result["summary"],
    }


async def _after_metrics(
    session: aiohttp.ClientSession,
    out: Path,
    label: str,
    reset: dict[str, Any],
) -> dict[str, Any]:
    rows = []
    for replica in reset["replicas"]:
        port = replica["port"]
        after = await _quiescent_metrics(session, port)
        before = replica["metrics_after_reset"]
        deltas = {
            key: after[key] - before[key]
            for key in after.keys() & before.keys()
            if key.startswith("vllm:prefix_cache_")
        }
        rows.append({"port": port, "after": after, "prefix_counter_deltas": deltas})
    value = {"label": label, "replicas": rows}
    _save(out / f"metrics-{label}.json", value)
    return value


def _select_rate(results: list[dict[str, Any]]) -> int:
    qualified: list[int] = []
    for count, item in zip(COUNTS, results, strict=True):
        summary = item["summary"]["all_offered"]
        outcomes = summary["outcomes"]
        completed = outcomes.get("completed", 0)
        rejected = outcomes.get("rejected", 0)
        p95 = summary["successful_only"]["scheduled_to_terminal"]["p95_ns"]
        if (
            item["comparison_valid"]
            and completed >= 0.95 * count
            and rejected <= 0.01 * count
            and p95 is not None
            and p95 <= 5_000_000_000
        ):
            qualified.append(count)
    if not qualified:
        raise StudyError("no predeclared calibration rate qualified")
    return max(qualified)


def _evaluation_schedule(selected: int) -> list[dict[str, Any]]:
    return [
        {
            "block": block,
            "seed": seed,
            "position": position,
            "policy": policy,
            "label": f"evaluation-b{block}-p{position}-{policy}",
            "offered_count": selected,
        }
        for block, (seed, order) in enumerate(
            zip(EVALUATION_SEEDS, ORDERS, strict=True), 1
        )
        for position, policy in enumerate(order, 1)
    ]


async def _record_condition(
    session: aiohttp.ClientSession,
    out: Path,
    session_record: dict[str, Any],
    label: str,
    policy: str,
    plan: dict[str, Any],
    certificate: dict[str, Any],
    reset: dict[str, Any],
    *,
    router_max_active: int = 32,
    router_max_queue: int = 32,
) -> dict[str, Any]:
    item = await _condition(
        session,
        out,
        label,
        policy,
        plan,
        certificate,
        router_max_active=router_max_active,
        router_max_queue=router_max_queue,
    )
    item["engine_metrics_status"] = "NOT_COLLECTED"
    session_record["conditions"].append(item)
    number = len(session_record["conditions"])
    _save(out / f"progress-{number:02}-client.json", session_record)
    if item["trial_status"] != "COMPLETED":
        item["engine_metrics_status"] = "SKIPPED_TRIAL_STATUS"
        if item["trial_status"] == "INTERRUPTED":
            raise TrialInterrupted(f"{label} trial returned INTERRUPTED")
        raise StudyError(f"{label} trial returned {item['trial_status']}")
    if not item["comparison_valid"]:
        item["engine_metrics_status"] = "SKIPPED_INVALID_COMPARISON"
        raise StudyError(f"{label} failed comparison validity")
    try:
        item["engine_metrics"] = await _after_metrics(session, out, label, reset)
        item["engine_metrics_status"] = "CAPTURED"
    except BaseException as error:
        item["engine_metrics_status"] = "FAILED"
        item["engine_metrics_error"] = f"{type(error).__name__}: {str(error)[:300]}"
        _save(
            out / f"metrics-error-{label}.json",
            {"label": label, "reason": item["engine_metrics_error"]},
        )
        raise
    _save(out / f"progress-{number:02}.json", session_record)
    return item


def report(raw_root: Path, output_root: Path) -> None:
    """Verify the retained raw population and render a descriptive report."""
    session = json.loads((raw_root / "session.json").read_text())
    if session.get("schema") != "inferdrome.vllm-router-gpu-session.v1":
        raise StudyError("session schema is invalid")
    conditions = session.get("conditions")
    if not isinstance(conditions, list):
        raise StudyError("session has no condition list")
    labels = [item["label"] for item in conditions]
    if len(set(labels)) != len(labels):
        raise StudyError("duplicate condition identity in session")
    values: list[dict[str, Any]] = []
    inventory: dict[str, str] = {}
    for item in conditions:
        label = item["label"]
        if not isinstance(label, str) or not re.fullmatch(r"[a-z0-9_-]+", label):
            raise StudyError("condition label is unsafe")
        if item["policy"] not in POLICIES:
            raise StudyError(f"condition policy is invalid: {label}")
        client_path = raw_root / f"{label}-client.json"
        ledger_path = raw_root / f"{label}-router.jsonl"
        if (
            _file_digest(client_path) != item["result_sha256"]
            or _file_digest(ledger_path) != item["ledger_sha256"]
        ):
            raise StudyError(f"raw artifact hash mismatch: {label}")
        client = json.loads(client_path.read_text())
        if (
            client["plan_sha256"] != item["plan_sha256"]
            or client["comparison_valid"] != item["comparison_valid"]
            or client["status"] != item["trial_status"]
        ):
            raise StudyError(f"client/condition mismatch: {label}")
        summary = client["summary"]
        all_offered = summary["all_offered"]
        values.append(
            {
                "label": label,
                "policy": item["policy"],
                "trial_status": item["trial_status"],
                "valid": item["comparison_valid"],
                "engine_metrics_status": item.get("engine_metrics_status"),
                "engine_metrics_error": item.get("engine_metrics_error"),
                "offered": all_offered["offered"],
                "outcomes": all_offered["outcomes"],
                "goodput_rps": all_offered["slo_goodput_rps"],
                "epoch_goodput_rps": [
                    summary["by_epoch"][str(index)]["slo_goodput_rps"]
                    for index in range(3)
                ],
                "successful_only": all_offered["successful_only"],
            }
        )
        inventory[client_path.name] = item["result_sha256"]
        inventory[ledger_path.name] = item["ledger_sha256"]
    for path in sorted(raw_root.iterdir()):
        if path.is_file() and path.name not in inventory:
            inventory[path.name] = _file_digest(path)
    output_root.mkdir(parents=True, exist_ok=False)
    evaluation = [value for value in values if value["label"].startswith("evaluation-")]
    selected = session.get("selected_offers")
    schedule = session.get("evaluation_schedule")
    expected_schedule = _evaluation_schedule(selected) if selected in COUNTS else None
    schedule_valid = (
        expected_schedule is not None
        and schedule == expected_schedule
        and "evaluation-schedule.json" in inventory
        and inventory.get("evaluation-schedule.json")
        == session.get("evaluation_schedule_sha256")
    )
    if schedule_valid:
        saved_schedule = json.loads((raw_root / "evaluation-schedule.json").read_text())
        schedule_valid = saved_schedule == {
            "selected_offers": selected,
            "conditions": expected_schedule,
        }
    expected_conditions = [
        (f"calibration-17-{count}", "round_robin") for count in COUNTS
    ] + (
        [(item["label"], item["policy"]) for item in expected_schedule]
        if expected_schedule is not None
        else []
    )
    complete = (
        session["status"] == "COMPLETED"
        and schedule_valid
        and [(item["label"], item["policy"]) for item in values] == expected_conditions
        and len(evaluation) == 16
        and all(
            value["valid"]
            and value["trial_status"] == "COMPLETED"
            and value["engine_metrics_status"] == "CAPTURED"
            for value in values
        )
    )
    result = {
        "schema": "inferdrome.vllm-router-gpu-report.v1",
        "status": "COMPLETE_DESCRIPTIVE" if complete else "INCOMPLETE",
        "raw_session_sha256": inventory["session.json"],
        "raw_inventory": inventory,
        "session_status": session["status"],
        "session_error": session.get("error"),
        "selected_offers": selected,
        "evaluation_schedule_valid": schedule_valid,
        "conditions": values,
        "limitations": [
            "Synthetic two-document trace; no production representativeness claim.",
            "Four blocks describe this host and workload; no significance claim.",
            "Router affinity does not prove KV residency or causal cache gains.",
        ],
    }
    _save(output_root / "report.json", result)
    lines = [
        "# Two-replica vLLM router GPU study",
        "",
        f"Status: **{result['status']}**. "
        f"Raw session SHA-256: `{inventory['session.json']}`.",
        f"Session status: `{session['status']}`; error: `{session.get('error')}`.",
        "",
        "| Condition | Policy | Trial | Metrics | Valid | Offered | "
        "SLO goodput (req/s) | Outcomes |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for value in values:
        outcomes = ", ".join(
            f"{key}={count}" for key, count in sorted(value["outcomes"].items())
        )
        lines.append(
            f"| {value['label']} | {value['policy']} | {value['trial_status']} | "
            f"{value['engine_metrics_status']} | {value['valid']} | "
            f"{value['offered']} | {value['goodput_rps']:.3f} | {outcomes} |"
        )
    lines.extend(
        [
            "",
            "Latency and stall p95 in report.json cover completed requests only; "
            "SLO goodput and outcomes cover all offered requests.",
            "The plot shows individual conditions without a significance claim.",
            "The operator must separately verify container provenance, provider cost, "
            "exact-instance destruction and absence readback.",
            "",
        ]
    )
    with (output_root / "report.md").open("x") as stream:
        stream.write("\n".join(lines))
    # A simple fixed-scale condition plot keeps the report portable and honest.
    max_value = max((float(v["goodput_rps"]) for v in evaluation), default=1.0)
    scale = 400 / max(max_value, 1.0)
    height = 55 + 28 * len(evaluation)
    plot = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="720" height="{height}" '
        'viewBox="0 0 720 ' + str(height) + '">',
        '<rect width="720" height="100%" fill="white"/>',
        '<text x="12" y="24" font-size="17">'
        "SLO goodput by evaluation condition (req/s)</text>",
    ]
    for index, value in enumerate(evaluation):
        y = 45 + 28 * index
        width = float(value["goodput_rps"]) * scale
        color = "#285ea8" if value["valid"] else "#a83a28"
        plot.append(
            f'<text x="12" y="{y + 14}" font-size="11">b{index // 4 + 1} '
            f"{value['policy']}</text>"
        )
        plot.append(
            f'<rect x="230" y="{y}" width="{width:.1f}" height="18" fill="{color}"/>'
        )
        plot.append(
            f'<text x="{240 + width:.1f}" y="{y + 14}" font-size="11">'
            f"{value['goodput_rps']:.3f}</text>"
        )
    plot.append("</svg>")
    with (output_root / "goodput.svg").open("x") as stream:
        stream.write("\n".join(plot))


async def run(args: argparse.Namespace) -> None:
    budget = _budget(args)
    args.output_root.mkdir(parents=True, exist_ok=False)
    out = args.output_root
    try:
        plans = _plans(args.plans_dir)
        if args.image_reference != IMAGE:
            raise StudyError(
                "outer image declaration differs from pinned Vast vLLM image"
            )
        if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
            raise StudyError("source commit is not an exact SHA-1")
        if not re.fullmatch(r"[0-9]+", args.instance_id):
            raise StudyError("provider instance ID must be exact numeric ID")
        snapshot = _snapshot(args.model_dir)
        version = _vllm_version(args.vllm_executable)
        gpus = _gpu_observation()
        if not _ports_closed():
            raise StudyError("engine or router loopback port is occupied")
        budget.require(900)
    except Exception as error:
        _save(
            out / "preflight-error.json",
            {
                "status": "FAILED_BEFORE_ENGINE_START",
                "reason": f"{type(error).__name__}: {str(error)[:300]}",
            },
        )
        raise
    session_record: dict[str, Any] = {
        "schema": "inferdrome.vllm-router-gpu-session.v1",
        "status": "STARTING",
        "source_commit": args.source_commit,
        "provider_instance_id": args.instance_id,
        "image_reference_declared": IMAGE,
        "image_runtime_provenance": "OPERATOR_DECLARED_NOT_ATTESTED",
        "model": QWEN3_8B_MODEL_ID,
        "revision": QWEN3_8B_REVISION,
        "snapshot": snapshot,
        "vllm_version": version,
        "gpus": gpus,
        "billing_start_utc": budget.billing_start.isoformat(),
        "hourly_rate_usd_declared": str(budget.hourly_rate),
        "cap_usd_declared": str(budget.cap),
        "reserve_usd_declared": str(budget.reserve),
        "conditions": [],
        "error": None,
    }
    processes: list[subprocess.Popen[bytes]] = []
    logs: list[Any] = []
    try:
        processes, logs = _start_engines(args.vllm_executable, args.model_dir, out)
        async with aiohttp.ClientSession(trust_env=False) as session:
            await _ready(session, processes, budget)
            for port in ENGINE_PORTS:
                status, content = await _get(
                    session, f"http://127.0.0.1:{port}/v1/models"
                )
                if status != 200 or QWEN3_8B_MODEL_ID not in content:
                    raise StudyError(f"engine {port} did not serve pinned model name")
            calibration: list[dict[str, Any]] = []
            for count in COUNTS:
                budget.require(420)
                label = f"calibration-17-{count}"
                reset = await _warm_and_reset(session, out, label)
                plan, cert = plans[("calibration", 17, count)]
                item = await _record_condition(
                    session,
                    out,
                    session_record,
                    label,
                    "round_robin",
                    plan,
                    cert,
                    reset,
                )
                calibration.append(item)
            selected = _select_rate(calibration)
            session_record["selected_offers"] = selected
            schedule = _evaluation_schedule(selected)
            session_record["evaluation_schedule"] = schedule
            session_record["evaluation_schedule_sha256"] = _save(
                out / "evaluation-schedule.json",
                {"selected_offers": selected, "conditions": schedule},
            )
            _save(out / "progress-03-selection.json", session_record)
            for planned in schedule:
                budget.require(420)
                label = planned["label"]
                reset = await _warm_and_reset(session, out, label)
                plan, cert = plans[("evaluation", planned["seed"], selected)]
                await _record_condition(
                    session,
                    out,
                    session_record,
                    label,
                    planned["policy"],
                    plan,
                    cert,
                    reset,
                )
            session_record["status"] = "COMPLETED"
    except BaseException as error:
        session_record["status"] = (
            "INTERRUPTED"
            if isinstance(
                error, KeyboardInterrupt | asyncio.CancelledError | TrialInterrupted
            )
            else "FAILED"
        )
        session_record["error"] = f"{type(error).__name__}: {str(error)[:300]}"
        raise
    finally:
        cleanup_error: str | None = None
        try:
            _stop_engines(processes, logs)
            if processes and not _ports_closed():
                raise StudyError("owned engine or router port remains open")
        except Exception as error:
            cleanup_error = f"{type(error).__name__}: {str(error)[:300]}"
            session_record["status"] = "CLEANUP_UNCONFIRMED"
            session_record["cleanup_error"] = cleanup_error
        session_record["ended_utc"] = datetime.now(UTC).isoformat()
        session_record["engine_log_sha256"] = {
            str(index): _file_digest(out / f"engine-{index}.log")
            for index in range(len(processes))
        }
        _save(out / "session.json", session_record)
        if cleanup_error is not None:
            raise StudyError(f"owned process cleanup unconfirmed: {cleanup_error}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Controlled two-A100 vLLM router study"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--tokenizer-root", type=Path, required=True)
    prep.add_argument("--output-root", type=Path, required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--plans-dir", type=Path, required=True)
    execute.add_argument("--model-dir", type=Path, required=True)
    execute.add_argument("--vllm-executable", type=Path, required=True)
    execute.add_argument("--output-root", type=Path, required=True)
    execute.add_argument("--image-reference", required=True)
    execute.add_argument("--source-commit", required=True)
    execute.add_argument("--instance-id", required=True)
    execute.add_argument("--billing-start-utc", required=True)
    execute.add_argument("--hourly-rate-usd", required=True)
    execute.add_argument("--cap-usd", required=True)
    execute.add_argument("--reserve-usd", required=True)
    render = commands.add_parser("report")
    render.add_argument("--raw-root", type=Path, required=True)
    render.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args.output_root, args.tokenizer_root)
        elif args.command == "report":
            report(args.raw_root, args.output_root)
        else:
            asyncio.run(run(args))
    except (StudyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"vllm-router-gpu: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
