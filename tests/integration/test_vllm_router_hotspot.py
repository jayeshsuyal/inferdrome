"""Functional fake-replica checks; these are not GPU performance results."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_affinity import (
    HISTORY_KEYS,
    HISTORY_TTL_NS,
    AffinityHistory,
    document_digest,
)
from inferdrome.vllm_router import Router, make_app
from inferdrome.vllm_router_study import (
    RequestResult,
    make_plan,
    run_trial,
    summarize,
    trace,
    validate_plan,
    validate_token_certificate,
)


async def _serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def _body() -> dict[str, object]:
    return {
        "stream": True,
        "messages": [
            {
                "role": "user",
                "content": "A repeated synthetic document. " * 12
                + "\n\nQuestion:\nSummarize this case.",
            }
        ],
    }


def _sse(tokens: int = 4, prompt_tokens: int = 200) -> bytes:
    return (
        b'data: {"choices":[{"index":0,"delta":{"content":"x"},'
        b'"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n'
        + b'data: {"usage":{"prompt_tokens":'
        + str(prompt_tokens).encode()
        + b',"completion_tokens":'
        + str(tokens).encode()
        + b',"total_tokens":'
        + str(prompt_tokens + tokens).encode()
        + b'},"choices":[]}\n\n'
        + b"data: [DONE]\n\n"
    )


def _replica(name: str, seen: list[str], gate: asyncio.Event | None) -> web.Application:
    async def chat(request: web.Request) -> web.StreamResponse:
        await request.json()
        seen.append(name)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        if gate is not None:
            await gate.wait()
        await response.write(_sse())
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


async def _escape_case(tmp_path: Path, policy: str) -> list[dict[str, object]]:
    seen: list[str] = []
    gate = asyncio.Event()
    gate.set()
    a, a_url = await _serve(_replica("a", seen, gate))
    b, b_url = await _serve(_replica("b", seen, None))
    ledger = tmp_path / f"{policy}.jsonl"
    router = Router((a_url, b_url), ledger, policy=policy, max_active=4)
    proxy, proxy_url = await _serve(make_app(router))
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                proxy_url + "/v1/chat/completions", json=_body()
            ) as warm:
                assert await warm.read() == _sse()
            gate.clear()
            first = await client.post(proxy_url + "/v1/chat/completions", json=_body())
            second = await client.post(proxy_url + "/v1/chat/completions", json=_body())
            third = await client.post(proxy_url + "/v1/chat/completions", json=_body())
            assert router.busy[0] == (3 if policy == "cache_only" else 2)
            assert router.busy[1] == (0 if policy == "cache_only" else 1)
            gate.set()
            for response in (first, second, third):
                assert await response.read() == _sse()
                response.release()
        assert seen == (["a"] * 4 if policy == "cache_only" else ["a", "a", "a", "b"])
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        assert len(rows) == router.offered == router.terminal == 4
        assert all(row["document_sha256"] is not None for row in rows)
        return rows
    finally:
        gate.set()
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


def test_hotspot_overload_escape_is_functional_only(tmp_path: Path) -> None:
    cache_only = asyncio.run(_escape_case(tmp_path, "cache_only"))
    cache_load = asyncio.run(_escape_case(tmp_path, "cache_plus_load"))
    assert all(row["replica"] == 0 for row in cache_only)
    assert any(row["route_reason"] == "overload_escape" for row in cache_load)


async def _cache_input_rejection(tmp_path: Path) -> None:
    seen: list[str] = []
    a, a_url = await _serve(_replica("a", seen, None))
    b, b_url = await _serve(_replica("b", seen, None))
    router = Router(
        (a_url, b_url),
        tmp_path / "reject.jsonl",
        policy="cache_only",
        max_active=1,
        max_queue=0,
    )
    proxy, proxy_url = await _serve(make_app(router))
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                proxy_url + "/v1/chat/completions", json={"stream": False}
            ) as invalid:
                assert invalid.status == 400
            async with client.post(
                proxy_url + "/v1/chat/completions", json=_body()
            ) as valid:
                assert await valid.read() == _sse()
        assert router.active == 0 and router.busy == [0, 0]
        assert not router.sem.locked()
        assert router.offered == router.terminal == router.ledger_rows == 2
    finally:
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


def test_cache_input_rejection_releases_admission_slot(tmp_path: Path) -> None:
    asyncio.run(_cache_input_rejection(tmp_path))


async def _study_case(
    tmp_path: Path, prompt_tokens: int, completion_tokens: int = 4
) -> dict[str, object]:
    seen: list[str] = []
    a, a_url = await _serve(_replica("a", seen, None))
    b, b_url = await _serve(_replica("b", seen, None))
    # The generic replica above reports 200 prompt tokens. A second handler
    # supplies an intentional mismatch when requested by the test.
    if prompt_tokens != 200 or completion_tokens != 4:
        await a.cleanup()
        await b.cleanup()

        async def wrong(_request: web.Request) -> web.Response:
            return web.Response(
                body=_sse(tokens=completion_tokens, prompt_tokens=prompt_tokens),
                headers={"Content-Type": "text/event-stream"},
            )

        wrong_app = web.Application()
        wrong_app.router.add_post("/v1/chat/completions", wrong)
        a, a_url = await _serve(wrong_app)
        b, b_url = await _serve(wrong_app)
    router = Router((a_url, b_url), tmp_path / "study-ledger.jsonl")
    proxy, proxy_url = await _serve(make_app(router))
    try:
        plan = make_plan(
            phase="fixture",
            seed=17,
            count=9,
            expected_prompt_tokens=200,
            duration_ns=180_000_000,
            max_tokens=4,
        )
        result = await run_trial(
            plan, router_origin=proxy_url, model="fake", expected_policy="round_robin"
        )
        assert router.offered == router.terminal == router.ledger_rows == 9
        assert result["router_accounting_valid"] is True
        assert result["trace_sha256"] == plan["trace_sha256"]
        assert "This archived incident" not in json.dumps(result)
        return result
    finally:
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


def test_scheduled_arrival_client_accounts_for_all_offers(tmp_path: Path) -> None:
    result = asyncio.run(_study_case(tmp_path, 200))
    summary = result["summary"]["all_offered"]
    assert summary["offered"] == summary["slo_good"] == 9
    assert summary["completion_tokens_total"] == 36
    assert all(row["first_content_ns"] >= row["scheduled_ns"] for row in result["rows"])


def test_output_length_and_prompt_length_are_checked(tmp_path: Path) -> None:
    result = asyncio.run(_study_case(tmp_path / "prompt", 199))
    assert result["summary"]["all_offered"]["outcomes"] == {"prompt_length_mismatch": 9}
    assert result["summary"]["all_offered"]["slo_good"] == 0
    result = asyncio.run(_study_case(tmp_path / "output", 200, 3))
    assert result["summary"]["all_offered"]["outcomes"] == {"output_length_mismatch": 9}


def test_calibration_trace_is_separate_and_plan_is_frozen() -> None:
    calibration = make_plan(
        phase="calibration", seed=17, count=30, expected_prompt_tokens=285
    )
    evaluation = make_plan(
        phase="evaluation", seed=17, count=30, expected_prompt_tokens=285
    )
    assert calibration["trace_sha256"] != evaluation["trace_sha256"]
    assert len(validate_plan(calibration)) == 30
    offers = trace(17, 300, 300_000_000_000)
    hot_documents = {
        epoch: {
            offer.prompt.split(":", 1)[0]
            for offer in offers
            if offer.epoch == epoch and offer.traffic_class == "hot"
        }
        for epoch in range(3)
    }
    assert hot_documents == {
        0: {"Record a00000"},
        1: {"Record b00000"},
        2: {"Record a00000"},
    }
    altered = {**evaluation, "max_tokens": 64}
    try:
        validate_plan(altered)
    except ValueError:
        pass
    else:
        raise AssertionError("altered plan was accepted")


def test_certificate_binding_and_scheduled_origin() -> None:
    plan = make_plan(phase="evaluation", seed=29, count=1, expected_prompt_tokens=277)
    certificate = {
        "schema": "inferdrome.vllm-router-token-certificate.v1",
        "plan_sha256": plan["plan_sha256"],
        "trace_sha256": plan["trace_sha256"],
        "offered_count": 1,
        "prompt_tokens": 277,
        "max_tokens": 128,
        "context_length": 2048,
        "tokenizers_version": "0.22.1",
        "tokenizer_json_sha256": (
            "sha256:aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"
        ),
        "tokenizer_config_sha256": (
            "sha256:d5d09f07b48c3086c508b30d1c9114bd1189145b74e982a265350c923acd8101"
        ),
    }
    certificate["certificate_sha256"] = (
        "sha256:" + hashlib.sha256(canonical_json_bytes(certificate)).hexdigest()
    )
    validate_token_certificate(plan, certificate)
    with pytest.raises(ValueError):
        validate_token_certificate({**plan, "trace_sha256": "sha256:0"}, certificate)

    row = RequestResult(
        index=0,
        scheduled_ns=0,
        epoch=0,
        traffic_class="hot",
        tenant="tenant-a",
        dispatch_ns=200_000_000,
        response_headers_ns=300_000_000,
        first_body_byte_ns=350_000_000,
        first_content_ns=600_000_000,
        terminal_ns=700_000_000,
        max_content_gap_ns=0,
        outcome="completed",
        http_status=200,
        prompt_tokens=277,
        completion_tokens=128,
    )
    measured = summarize(plan, [row])["all_offered"]
    assert measured["slo_good"] == 0
    assert measured["scheduled_to_first_content_p95_ns"] == 600_000_000


def test_history_is_hashed_bounded_and_expires() -> None:
    digest = document_digest(_body())
    assert digest is not None and len(digest) == 64
    assert "synthetic" not in digest
    history = AffinityHistory()
    history.record(digest, 0, 100)
    assert history.scores(digest, 101) == (1, 0)
    assert history.scores(digest, 101 + HISTORY_TTL_NS) == (0, 0)
    for number in range(HISTORY_KEYS + 3):
        history.record(f"{number:064x}", 1, 200)
    assert history.size == HISTORY_KEYS
