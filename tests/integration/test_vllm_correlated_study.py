"""Client → router → fake replicas → offline links; no GPU performance claims."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import fields
from pathlib import Path

import pytest
from aiohttp import web

from inferdrome import vllm_router_study as study
from inferdrome.vllm_request_identity import (
    REQUEST_ID_HEADER,
    validate_correlated_result,
    verify_links,
)
from inferdrome.vllm_router import Router, make_app


async def _serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def _replica() -> web.Application:
    async def chat(request: web.Request) -> web.Response:
        assert REQUEST_ID_HEADER not in request.headers
        assert (await request.json())["stream"] is True
        return web.Response(
            content_type="text/event-stream",
            body=(
                b'data: {"choices":[{"index":0,"delta":{"content":"x"},'
                b'"finish_reason":null}]}\n\n'
                b'data: {"choices":[{"index":0,"delta":{},'
                b'"finish_reason":"length"}]}\n\n'
                b'data: {"usage":{"prompt_tokens":200,"completion_tokens":4,'
                b'"total_tokens":204},"choices":[]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


async def _trial(
    tmp_path: Path,
    policy: str = "round_robin",
    *,
    correlate: bool = True,
    interrupt: bool = False,
    echo: str = "valid",
) -> tuple[dict, list[dict]]:
    a, a_url = await _serve(_replica())
    b, b_url = await _serve(_replica())
    ledger = tmp_path / "router.jsonl"
    router = Router((a_url, b_url), ledger, policy=policy)
    app = make_app(router)

    async def alter_echo(request: web.Request, response: web.StreamResponse) -> None:
        if request.path != "/v1/chat/completions":
            return
        if echo == "missing":
            response.headers.pop(REQUEST_ID_HEADER, None)
        elif echo == "malformed":
            response.headers[REQUEST_ID_HEADER] = "private-untrusted-value"
        elif echo == "duplicate":
            response.headers.add(REQUEST_ID_HEADER, "f" * 32)

    app.on_response_prepare.append(alter_echo)
    proxy, proxy_url = await _serve(app)
    try:
        plan = study.make_plan(
            phase="fixture",
            seed=17,
            count=6,
            expected_prompt_tokens=200,
            duration_ns=5_000_000_000 if interrupt else 120_000_000,
            max_tokens=4,
            first_content_slo_ns=5_000_000_000,
            completion_slo_ns=10_000_000_000,
        )
        task = asyncio.create_task(
            study.run_trial(
                plan,
                router_origin=proxy_url,
                model="fake",
                expected_policy=policy,
                correlate_requests=correlate,
            )
        )
        if interrupt:
            async with asyncio.timeout(2):
                while router.terminal == 0:
                    await asyncio.sleep(0.001)
            task.cancel()
        result = await task
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        return result, rows
    finally:
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


@pytest.mark.parametrize("policy", study.POLICIES)
def test_all_policies_correlate_real_client_and_router(
    tmp_path: Path, policy: str
) -> None:
    result, ledger = asyncio.run(_trial(tmp_path, policy))
    report = verify_links(result, list(reversed(ledger)))
    measurement = result["measurement"]
    assert report["matched"] == report["offered"] == 6
    assert report["correlation_valid"] is True
    assert measurement["comparison_valid"] is True
    assert measurement["evidence_class"] == "SYNTHETIC_ONLY"
    assert measurement["summary"]["all_offered"]["outcomes"] == {"completed": 6}
    assert "This archived incident" not in json.dumps(result)
    assert all(
        row["request_id"] == row["response_request_id"]
        for row in result["request_links"]
    )
    if policy == "round_robin":
        assert {row["replica"] for row in report["links"]} == {0, 1}


def test_default_result_keeps_frozen_measurement_fields(tmp_path: Path) -> None:
    result, _ = asyncio.run(_trial(tmp_path, correlate=False))
    assert result["schema"] == study.RESULT_SCHEMA
    assert "measurement" not in result and "request_links" not in result
    expected = {field.name for field in fields(study.RequestResult)} - {
        "ready_ns",
        "document_id",
    }
    assert all(set(row) == expected for row in result["rows"])


def test_interruption_retains_identity_for_every_scheduled_offer(
    tmp_path: Path,
) -> None:
    result, ledger = asyncio.run(_trial(tmp_path, interrupt=True))
    report = verify_links(result, ledger)
    assert result["measurement"]["status"] == "INTERRUPTED"
    assert report["offered"] == 6
    assert report["not_dispatched"] > 0
    assert report["matched"] + report["not_dispatched"] == 6
    assert report["correlation_valid"] is True
    assert report["measurement_comparison_valid"] is False
    assert len({row["request_id"] for row in result["request_links"]}) == 6


@pytest.mark.parametrize("echo", ["missing", "malformed", "duplicate"])
def test_bad_echo_cannot_silently_claim_verified_identity(
    tmp_path: Path, echo: str
) -> None:
    result, ledger = asyncio.run(_trial(tmp_path, echo=echo))
    assert result["measurement"]["summary"]["all_offered"]["outcomes"] == {
        "completed": 6
    }
    assert all(row["response_request_id"] is None for row in result["request_links"])
    assert "private-untrusted-value" not in json.dumps(result)
    with pytest.raises(ValueError, match="identity missing or mismatched"):
        verify_links(result, ledger)


def test_cli_retains_invalid_identity_observation_and_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = asyncio.run(_trial(tmp_path, echo="missing"))

    async def trial(*_args: object, **kwargs: object) -> dict:
        assert kwargs["correlate_requests"] is True
        return result

    monkeypatch.setattr(study, "run_trial", trial)
    source = tmp_path / "input.json"
    source.write_text("{}")
    output = tmp_path / "result.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "study",
            "run",
            "--plan",
            str(source),
            "--token-certificate",
            str(source),
            "--router",
            "http://127.0.0.1:8090",
            "--model",
            "fake",
            "--policy",
            "round_robin",
            "--correlate-requests",
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        study.main()
    assert exit_info.value.code == 2
    saved = json.loads(output.read_text())
    assert saved == result
    with pytest.raises(ValueError, match="identity missing or mismatched"):
        validate_correlated_result(saved)
