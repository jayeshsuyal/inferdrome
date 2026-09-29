"""Functional fake-replica checks; these are not GPU performance results."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import sys
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
    main,
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


async def _wait_terminal(router: Router, expected: int) -> None:
    async with asyncio.timeout(2):
        while router.terminal != expected:
            await asyncio.sleep(0.01)


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
            await _wait_terminal(router, 1)
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


async def _candidate_unavailable_and_cancelled(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        unavailable = f"http://127.0.0.1:{probe.getsockname()[1]}"
    seen: list[str] = []
    gate = asyncio.Event()
    a, a_url = await _serve(_replica("a", seen, gate))
    b, b_url = await _serve(_replica("b", seen, None))
    # First, a selected but unavailable replica must release all router work.
    absent_router = Router(
        (unavailable, b_url),
        tmp_path / "candidate-absent.jsonl",
        policy="cache_saturation",
        saturation_active=3,
        max_active=4,
    )
    absent_proxy, absent_url = await _serve(make_app(absent_router))
    try:
        async with (
            aiohttp.ClientSession() as client,
            client.post(absent_url + "/v1/chat/completions", json=_body()) as response,
        ):
            assert response.status == 502
        await _wait_terminal(absent_router, 1)
        assert absent_router.active == 0 and absent_router.busy == [0, 0]
        assert absent_router.pending == 0 and absent_router.ledger_rows == 1
    finally:
        await absent_proxy.cleanup()
    # A downstream disconnect after response headers must also free work.
    cancel_router = Router(
        (a_url, b_url),
        tmp_path / "candidate-cancel.jsonl",
        policy="cache_saturation",
        saturation_active=3,
        max_active=4,
    )
    cancel_proxy, cancel_url = await _serve(make_app(cancel_router))
    try:
        async with aiohttp.ClientSession() as client:
            response = await client.post(
                cancel_url + "/v1/chat/completions", json=_body()
            )
            assert response.status == 200
            response.close()
            await _wait_terminal(cancel_router, 1)
        assert cancel_router.active == 0 and cancel_router.busy == [0, 0]
        assert cancel_router.pending == 0 and cancel_router.ledger_rows == 1
        row = json.loads((tmp_path / "candidate-cancel.jsonl").read_text())
        assert row["outcome"] in {"disconnected", "cancelled"}
        assert row["saturation_threshold_active"] == 3
    finally:
        gate.set()
        await cancel_proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


def test_candidate_accounts_for_unavailable_replica_and_disconnect(
    tmp_path: Path,
) -> None:
    asyncio.run(_candidate_unavailable_and_cancelled(tmp_path))


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
            await _wait_terminal(router, 1)
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
            first_content_slo_ns=5_000_000_000,
            completion_slo_ns=10_000_000_000,
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


async def _interrupted_study(tmp_path: Path) -> None:
    seen: list[str] = []
    a, a_url = await _serve(_replica("a", seen, None))
    b, b_url = await _serve(_replica("b", seen, None))
    router = Router((a_url, b_url), tmp_path / "interrupted.jsonl")
    proxy, proxy_url = await _serve(make_app(router))
    try:
        plan = make_plan(
            phase="fixture",
            seed=17,
            count=9,
            expected_prompt_tokens=200,
            duration_ns=5_000_000_000,
            max_tokens=4,
            first_content_slo_ns=5_000_000_000,
            completion_slo_ns=10_000_000_000,
        )
        trial = asyncio.create_task(
            run_trial(
                plan,
                router_origin=proxy_url,
                model="fake",
                expected_policy="round_robin",
            )
        )
        await _wait_terminal(router, 1)
        trial.cancel()
        result = await trial
        assert result["status"] == "INTERRUPTED"
        assert result["comparison_valid"] is False
        assert result["summary"]["all_offered"]["offered"] == 9
        assert len(result["rows"]) == 9
        assert any(row["outcome"] == "cancelled" for row in result["rows"])
        assert not any(
            task.get_name().startswith("inferdrome-offer-") and not task.done()
            for task in asyncio.all_tasks()
        )
        offered = router.offered
        await asyncio.sleep(0.8)
        assert router.offered == offered
        assert router.active == 0 and router.busy == [0, 0]
    finally:
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


def test_parent_interruption_settles_all_offer_tasks(tmp_path: Path) -> None:
    asyncio.run(_interrupted_study(tmp_path))


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
    first = measured["successful_only"]["scheduled_to_first_content"]
    assert first == {"count": 1, "p95_ns": 600_000_000}


def _measured_row(index: int, epoch: int, outcome: str) -> RequestResult:
    scheduled = epoch * 100_000_000_000
    completed = outcome == "completed"
    return RequestResult(
        index=index,
        scheduled_ns=scheduled,
        epoch=epoch,
        traffic_class="hot",
        tenant="tenant-a",
        dispatch_ns=scheduled if completed else None,
        response_headers_ns=scheduled if completed else None,
        first_body_byte_ns=scheduled + 1_000_000 if completed else None,
        first_content_ns=scheduled + 1_000_000 if completed else None,
        terminal_ns=scheduled + (2_000_000 if completed else 60_000_000_000),
        max_content_gap_ns=0 if completed else None,
        outcome=outcome,
        http_status=200 if completed else None,
        prompt_tokens=277 if completed else None,
        completion_tokens=128 if completed else None,
    )


def test_mixed_outcomes_label_successful_only_latency() -> None:
    plan = make_plan(phase="evaluation", seed=29, count=100, expected_prompt_tokens=277)
    rows = [_measured_row(0, 0, "completed")]
    rows.extend(_measured_row(i, 0, "timeout") for i in range(1, 100))
    measured = summarize(plan, rows)["all_offered"]
    assert measured["offered"] == 100
    assert measured["outcomes"] == {"completed": 1, "timeout": 99}
    assert measured["slo_good"] == 1
    assert measured["successful_only"]["completed_count"] == 1
    assert measured["successful_only"]["scheduled_to_first_content"] == {
        "count": 1,
        "p95_ns": 1_000_000,
    }
    assert "scheduled_to_first_content_p95_ns" not in measured


def test_epoch_goodput_uses_each_scheduled_epoch_window() -> None:
    plan = make_plan(phase="evaluation", seed=29, count=3, expected_prompt_tokens=277)
    summary = summarize(plan, [_measured_row(i, i, "completed") for i in range(3)])
    assert summary["all_offered"]["goodput_denominator_ns"] == 300_000_000_000
    for epoch in range(3):
        group = summary["by_epoch"][str(epoch)]
        assert group["goodput_denominator_ns"] == 100_000_000_000
        assert group["goodput_scope"] == "EPOCH_OFFERED_WINDOW"
        assert group["slo_goodput_rps"] == 0.01
    assert summary["by_class"]["hot"]["goodput_denominator_ns"] == 300_000_000_000


def test_cli_output_errors_prevent_trial_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    certificate = tmp_path / "certificate.json"
    plan.write_text("{}")
    certificate.write_text("{}")

    async def unexpected_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("trial dispatched before output reservation")

    monkeypatch.setattr("inferdrome.vllm_router_study.run_trial", unexpected_run)
    arguments = [
        "vllm_router_study",
        "run",
        "--plan",
        str(plan),
        "--token-certificate",
        str(certificate),
        "--router",
        "http://127.0.0.1:8090",
        "--model",
        "fake",
        "--policy",
        "round_robin",
        "--output",
    ]
    existing = tmp_path / "existing.json"
    existing.write_text("old")
    monkeypatch.setattr(sys, "argv", [*arguments, str(existing)])
    with pytest.raises(FileExistsError):
        main()
    assert existing.read_text() == "old"
    monkeypatch.setattr(
        sys, "argv", [*arguments, str(tmp_path / "missing" / "out.json")]
    )
    with pytest.raises(FileNotFoundError):
        main()

    denied = tmp_path / "denied.json"
    original_open = Path.open

    def denied_open(path: Path, *args: object, **kwargs: object) -> object:
        if path == denied:
            raise PermissionError("output destination is unwritable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    monkeypatch.setattr(sys, "argv", [*arguments, str(denied)])
    with pytest.raises(PermissionError):
        main()
    monkeypatch.setattr(Path, "open", original_open)

    async def interrupted_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "INTERRUPTED", "comparison_valid": False, "rows": []}

    monkeypatch.setattr("inferdrome.vllm_router_study.run_trial", interrupted_run)
    output = tmp_path / "interrupted-result.json"
    monkeypatch.setattr(sys, "argv", [*arguments, str(output)])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
    assert json.loads(output.read_text())["status"] == "INTERRUPTED"


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
