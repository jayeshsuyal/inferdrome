"""GPU-free end-to-end exercise of the bounded PR3 coordinator."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from inferdrome import vllm_router_gpu as gpu
from inferdrome.vllm_router_study import make_plan


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _sse(prompt_tokens: int, completion_tokens: int) -> bytes:
    return (
        b'data: {"choices":[{"index":0,"delta":{"content":"x"},'
        b'"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n'
        + b'data: {"usage":{"prompt_tokens":'
        + str(prompt_tokens).encode()
        + b',"completion_tokens":'
        + str(completion_tokens).encode()
        + b',"total_tokens":'
        + str(prompt_tokens + completion_tokens).encode()
        + b'},"choices":[]}\n\n'
        + b"data: [DONE]\n\n"
    )


def _fake_engine(
    resets: list[int], port: int, *, reset_success: bool
) -> web.Application:
    async def health(_request: web.Request) -> web.Response:
        return web.Response()

    async def chat(request: web.Request) -> web.Response:
        payload = await request.json()
        completion = payload["max_tokens"]
        prompt = 286 if completion == 128 else 15
        return web.Response(
            body=_sse(prompt, completion), content_type="text/event-stream"
        )

    async def models(_request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "Qwen/Qwen3-8B"}]})

    async def reset(_request: web.Request) -> web.Response:
        resets.append(port)
        return web.json_response({"success": reset_success})

    async def metrics(_request: web.Request) -> web.Response:
        return web.Response(
            text="vllm:num_requests_running 0\nvllm:num_requests_waiting 0\n"
            "vllm:prefix_cache_hits_total 4\nvllm:prefix_cache_queries_total 8\n"
        )

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", models)
    app.router.add_get("/metrics", metrics)
    app.router.add_post("/reset_prefix_cache", reset)
    app.router.add_post("/v1/chat/completions", chat)
    return app


async def _fake_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, reset_success: bool
) -> tuple[Path, list[int]]:
    ports = (_port(), _port())
    monkeypatch.setattr(gpu, "ENGINE_PORTS", ports)
    monkeypatch.setattr(gpu, "ROUTER_PORT", _port())
    monkeypatch.setattr(gpu, "COUNTS", (1, 2, 3))
    monkeypatch.setattr(gpu, "_snapshot", lambda _path: {"sha256": "fixture"})
    monkeypatch.setattr(gpu, "_vllm_version", lambda _path: "vllm 0.26.0")
    monkeypatch.setattr(gpu, "_gpu_observation", lambda: [{"model": "fixture"}] * 2)
    monkeypatch.setattr(gpu, "_start_engines", lambda *_: ([], []))
    monkeypatch.setattr(gpu, "_stop_engines", lambda *_: None)
    monkeypatch.setattr(gpu, "_ports_closed", lambda: True)

    async def ready(*_args: object) -> None:
        return None

    monkeypatch.setattr(gpu, "_ready", ready)

    plans = {}
    for phase, seeds in gpu.PHASE_SEEDS:
        for seed in seeds:
            for count in gpu.COUNTS:
                plans[(phase, seed, count)] = (
                    make_plan(
                        phase="fixture",
                        seed=seed,
                        count=count,
                        expected_prompt_tokens=286,
                        duration_ns=120_000_000,
                    ),
                    {"certificate_sha256": "fixture"},
                )
    monkeypatch.setattr(gpu, "_plans", lambda _path: plans)
    resets: list[int] = []
    runners: list[web.AppRunner] = []
    for port in ports:
        runner = web.AppRunner(_fake_engine(resets, port, reset_success=reset_success))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        runners.append(runner)
    raw = tmp_path / "raw"
    args = SimpleNamespace(
        billing_start_utc=datetime.now(UTC).isoformat(),
        hourly_rate_usd="1.5",
        cap_usd="5",
        reserve_usd="1",
        output_root=raw,
        plans_dir=tmp_path,
        image_reference=gpu.IMAGE,
        model_dir=tmp_path,
        vllm_executable=tmp_path / "vllm",
        source_commit="a" * 40,
        instance_id="123",
    )
    try:
        if reset_success:
            await gpu.run(args)
        else:
            with pytest.raises(gpu.StudyError, match="reset did not return success"):
                await gpu.run(args)
    finally:
        for runner in runners:
            await runner.cleanup()
    return raw, resets


def test_full_fake_lifecycle_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, resets = asyncio.run(_fake_run(tmp_path, monkeypatch, reset_success=True))
    session = json.loads((raw / "session.json").read_text())
    assert session["status"] == "COMPLETED"
    assert session["selected_offers"] == 3
    assert len(session["conditions"]) == 19
    assert len(resets) == 38
    assert all(item["comparison_valid"] for item in session["conditions"])
    gpu.report(raw, tmp_path / "report")
    report = json.loads((tmp_path / "report/report.json").read_text())
    assert report["status"] == "COMPLETE_DESCRIPTIVE"
    assert len(report["conditions"]) == 19
    assert (tmp_path / "report/goodput.svg").is_file()


def test_reset_failure_preserves_terminal_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, resets = asyncio.run(_fake_run(tmp_path, monkeypatch, reset_success=False))
    session = json.loads((raw / "session.json").read_text())
    assert session["status"] == "FAILED"
    assert "reset did not return success" in session["error"]
    assert len(resets) == 1
    gpu.report(raw, tmp_path / "report")
    report = json.loads((tmp_path / "report/report.json").read_text())
    assert report["status"] == "INCOMPLETE"


def test_report_rejects_tampered_raw_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, _ = asyncio.run(_fake_run(tmp_path, monkeypatch, reset_success=True))
    ledger = next(raw.glob("evaluation-*-router.jsonl"))
    ledger.write_text(ledger.read_text() + "{}\n")
    with pytest.raises(gpu.StudyError, match="raw artifact hash mismatch"):
        gpu.report(raw, tmp_path / "report")


def test_owned_process_group_is_reaped(tmp_path: Path) -> None:
    log = (tmp_path / "engine.log").open("xb")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    gpu._stop_engines([process], [log])
    assert process.poll() is not None
    assert log.closed


def test_future_billing_start_is_rejected() -> None:
    args = SimpleNamespace(
        billing_start_utc=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        hourly_rate_usd="1.5",
        cap_usd="5",
        reserve_usd="1",
    )
    with pytest.raises(gpu.StudyError, match="cannot be in the future"):
        gpu._budget(args)
