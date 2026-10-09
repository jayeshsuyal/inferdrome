"""Planned timing reaches the real client and router using two local fake replicas."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from inferdrome import vllm_arrival_timing as timing
from inferdrome import vllm_router_study as study
from inferdrome import vllm_timed_result as timed_result
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_request_identity import REQUEST_ID_HEADER, verify_links
from inferdrome.vllm_router import Router, make_app
from inferdrome.vllm_timed_result import verify_timed_result


def _plan(kind: str = "baseline", *, interrupt: bool = False) -> dict[str, Any]:
    duration_ns = 5_000_000_000 if interrupt else 180_000_000
    if kind == "baseline":
        return study.make_plan(
            phase="fixture",
            seed=17,
            count=12,
            expected_prompt_tokens=200,
            duration_ns=duration_ns,
            max_tokens=4,
            first_content_slo_ns=5_000_000_000,
            completion_slo_ns=10_000_000_000,
        )
    return study.make_capacity_plan(
        phase="fixture",
        seed=17,
        count=12,
        duration_ns=duration_ns,
        workload={
            "document_count": 8,
            "target_prefix_tokens": 256,
            "document_repeats": [12] * 8,
            "document_prefix_tokens": [256] * 8,
            "hot_group_size": 2,
            "cycle_percent": 60,
            "hotspot_epochs": ["A", "B", "A"],
            "context_length": 512,
            "output_tokens": 4,
        },
        prompt_tokens_by_index=[272 + index % 3 for index in range(12)],
        first_content_slo_ns=5_000_000_000,
        completion_slo_ns=10_000_000_000,
    )


def _timing(plan: dict[str, Any]) -> dict[str, Any]:
    return timing.make_timing(
        plan,
        group_size=4,
        retained_spacing_bps=0,
        max_advance_ns=plan["duration_ns"],
    )


async def _serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


async def _trial(
    tmp_path: Path, kind: str = "baseline", *, interrupt: bool = False
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict], list[dict]]:
    plan = _plan(kind, interrupt=interrupt)
    descriptor = _timing(plan)
    offers = study.validate_plan(plan)
    prompt_lengths = {
        offer.prompt: (
            plan["prompt_tokens_by_index"][offer.index]
            if kind == "capacity"
            else plan["expected_prompt_tokens"]
        )
        for offer in offers
    }
    payloads: list[dict] = []

    def replica() -> web.Application:
        async def chat(request: web.Request) -> web.Response:
            assert REQUEST_ID_HEADER not in request.headers
            payload = await request.json()
            payloads.append(payload)
            prompt_tokens = prompt_lengths[payload["messages"][0]["content"]]
            usage = {
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": 4,
                    "total_tokens": prompt_tokens + 4,
                },
                "choices": [],
            }
            return web.Response(
                content_type="text/event-stream",
                body=(
                    'data: {"choices":[{"index":0,"delta":{"content":"x"},'
                    '"finish_reason":null}]}\n\n'
                    'data: {"choices":[{"index":0,"delta":{},'
                    '"finish_reason":"length"}]}\n\n'
                    f"data: {json.dumps(usage)}\n\n"
                    "data: [DONE]\n\n"
                ).encode(),
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", chat)
        return app

    a, a_url = await _serve(replica())
    b, b_url = await _serve(replica())
    ledger_path = tmp_path / "router.jsonl"
    router = Router((a_url, b_url), ledger_path, policy="round_robin")
    proxy, proxy_url = await _serve(make_app(router))
    try:
        task = asyncio.create_task(
            study.run_trial(
                plan,
                router_origin=proxy_url,
                model="fake",
                expected_policy="round_robin",
                correlate_requests=True,
                timing=descriptor,
            )
        )
        if interrupt:
            async with asyncio.timeout(2):
                while router.terminal == 0:
                    await asyncio.sleep(0.001)
            task.cancel()
        result = await task
        ledger = [json.loads(line) for line in ledger_path.read_text().splitlines()]
        return plan, descriptor, result, ledger, payloads
    finally:
        await proxy.cleanup()
        await a.cleanup()
        await b.cleanup()


@pytest.mark.parametrize("kind", ["baseline", "capacity"])
def test_timed_client_preserves_request_population_and_accounts_for_full_window(
    tmp_path: Path, kind: str
) -> None:
    plan, descriptor, result, ledger, payloads = asyncio.run(_trial(tmp_path, kind))
    measurement = result["measurement"]
    original = study.validate_plan(plan)
    transformed = timing.validate_timing(plan, descriptor)
    assert measurement["schema"] == timing.TIMED_RESULT_SCHEMA
    assert measurement["plan_sha256"] == descriptor["timing_sha256"]
    assert measurement["trace_sha256"] == descriptor["transformed_trace_sha256"]
    assert measurement["base_plan_schema"] == plan["schema"]
    assert measurement["base_plan_sha256"] == plan["plan_sha256"]
    assert measurement["base_trace_sha256"] == plan["trace_sha256"]
    assert (
        measurement["token_certificate_scope"] == "BASE_WORKLOAD_UNCHANGED_TIMING_ONLY"
    )
    assert measurement["arrival_timing_scope"] == "PLANNED_OFFERS_NOT_OBSERVED_ARRIVALS"
    assert measurement["evidence_class"] == "SYNTHETIC_ONLY"
    assert measurement["comparison_valid"] is True
    rows = measurement["rows"]
    assert [row["scheduled_ns"] for row in rows] == [
        offer.scheduled_ns for offer in transformed
    ]
    assert [row["epoch"] for row in rows] == [offer.epoch for offer in original]
    assert all(
        set(row) == {field.name for field in fields(study.RequestResult)}
        for row in rows
    )
    assert sorted(payload["messages"][0]["content"] for payload in payloads) == sorted(
        offer.prompt for offer in original
    )
    assert all(
        payload["max_tokens"] == plan["max_tokens"]
        and payload["ignore_eos"] is True
        and payload["chat_template_kwargs"] == {"enable_thinking": False}
        for payload in payloads
    )
    summary = measurement["summary"]
    assert summary["all_offered"]["offered"] == len(original)
    assert summary["all_offered"]["outcomes"] == {"completed": len(original)}
    assert summary["all_offered"]["goodput_denominator_ns"] == plan["duration_ns"]
    assert summary["all_offered"]["goodput_scope"] == "FULL_OFFERED_WINDOW"
    assert "client_timing" in summary
    assert (
        sum(item["goodput_denominator_ns"] for item in summary["by_epoch"].values())
        == plan["duration_ns"]
    )
    for epoch, group in summary["by_epoch"].items():
        assert group["offered"] == sum(offer.epoch == int(epoch) for offer in original)
    identity = verify_links(result, list(reversed(ledger)))
    assert identity["matched"] == len(original)
    assert identity["correlation_valid"] is True
    assert {row["replica"] for row in identity["links"]} == {0, 1}
    report = verify_timed_result(plan, descriptor, result)
    assert report["status"] == "VERIFIED"
    assert report["scope"] == "TIMING_AND_CLIENT_ACCOUNTING_ONLY"
    assert report["measurement_comparison_valid"] is True


def test_timed_interruption_retains_every_offer_and_its_unchanged_denominator(
    tmp_path: Path,
) -> None:
    plan, descriptor, result, ledger, _ = asyncio.run(_trial(tmp_path, interrupt=True))
    measurement = result["measurement"]
    assert measurement["status"] == "INTERRUPTED"
    assert measurement["comparison_valid"] is False
    assert measurement["summary"]["all_offered"]["offered"] == plan["offered_count"]
    assert (
        measurement["summary"]["all_offered"]["goodput_denominator_ns"]
        == plan["duration_ns"]
    )
    assert measurement["summary"]["client_timing"]["never_dispatched"] > 0
    assert [row["scheduled_ns"] for row in measurement["rows"]] == [
        row["scheduled_ns"] for row in descriptor["arrivals"]
    ]
    links = verify_links(result, ledger)
    assert links["matched"] + links["not_dispatched"] == plan["offered_count"]
    report = verify_timed_result(plan, descriptor, result)
    assert report["status"] == "VERIFIED"
    assert report["measurement_comparison_valid"] is False


@pytest.mark.parametrize(
    "failure", ["missing_correlation", "modified_arrival", "different_source"]
)
def test_invalid_timing_is_rejected_before_a_client_session_is_opened(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    plan = _plan()
    descriptor = _timing(plan)
    if failure == "modified_arrival":
        descriptor["arrivals"][0]["scheduled_ns"] += 1
    elif failure == "different_source":
        descriptor = _timing(_plan("capacity"))

    def unexpected_session(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid timing must fail before creating an HTTP session")

    monkeypatch.setattr(study.aiohttp, "ClientSession", unexpected_session)
    with pytest.raises(ValueError):
        asyncio.run(
            study.run_trial(
                plan,
                router_origin="http://127.0.0.1:8090",
                model="fake",
                expected_policy="round_robin",
                correlate_requests=failure != "missing_correlation",
                timing=descriptor,
            )
        )


def test_cli_requires_correlation_before_touching_sources_or_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "result.json"
    missing = str(tmp_path / "absent.json")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "study",
            "run",
            "--plan",
            missing,
            "--timing",
            missing,
            "--token-certificate",
            missing,
            "--router",
            "http://127.0.0.1:8090",
            "--model",
            "fake",
            "--policy",
            "round_robin",
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as raised:
        study.main()
    assert raised.value.code == 2
    assert not output.exists()


def _rehash(value: dict[str, Any], field: str = "result_sha256") -> None:
    value[field] = (
        "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {key: item for key, item in value.items() if key != field}
            )
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "scheduled",
        "epoch",
        "token_budget",
        "denominator",
        "base_plan",
        "identity",
        "float_schedule",
        "boolean_epoch",
        "missing_ready",
        "timestamp_order",
        "successful_summary",
        "router_count_boolean",
        "accounting_flag",
    ],
)
def test_result_verifier_rejects_rehashed_timing_or_accounting_changes(
    tmp_path: Path, mutation: str
) -> None:
    plan, descriptor, result, _, _ = asyncio.run(_trial(tmp_path))
    changed = copy.deepcopy(result)
    measurement = changed["measurement"]
    if mutation == "scheduled":
        measurement["rows"][0]["scheduled_ns"] += 1
    elif mutation == "epoch":
        measurement["rows"][0]["epoch"] = 2
    elif mutation == "token_budget":
        measurement["rows"][0]["completion_tokens"] += 1
    elif mutation == "denominator":
        measurement["summary"]["all_offered"]["goodput_denominator_ns"] -= 1
    elif mutation == "base_plan":
        measurement["base_plan_sha256"] = "sha256:" + "0" * 64
    elif mutation == "identity":
        changed["request_links"][0]["response_request_id"] = "0" * 32
    elif mutation == "float_schedule":
        measurement["rows"][0]["scheduled_ns"] = float(
            measurement["rows"][0]["scheduled_ns"]
        )
    elif mutation == "boolean_epoch":
        measurement["rows"][0]["epoch"] = False
    elif mutation == "missing_ready":
        measurement["rows"][0]["ready_ns"] = None
    elif mutation == "timestamp_order":
        measurement["rows"][0]["terminal_ns"] = (
            measurement["rows"][0]["dispatch_ns"] - 1
        )
    elif mutation == "successful_summary":
        measurement["summary"]["all_offered"]["successful_only"]["completed_count"] += 1
    elif mutation == "router_count_boolean":
        measurement["router_stats_after"]["in_flight"] = False
    elif mutation == "accounting_flag":
        measurement["router_accounting_valid"] = 1
    _rehash(measurement)
    _rehash(changed)
    with pytest.raises(ValueError):
        verify_timed_result(plan, descriptor, changed)


def test_verifier_cli_publishes_hash_bound_report_without_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, descriptor, result, _, _ = asyncio.run(_trial(tmp_path))
    sources = {"plan": plan, "timing": descriptor, "result": result}
    arguments = ["vllm_timed_result"]
    for name, value in sources.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value))
        arguments.extend([f"--{name}", str(path)])
    output = tmp_path / "verified.json"
    arguments.extend(["--output", str(output)])
    monkeypatch.setattr(sys, "argv", arguments)
    timed_result.main()
    assert json.loads(output.read_text()) == verify_timed_result(
        plan, descriptor, result
    )
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        timed_result.main()
    assert output.read_bytes() == before


@pytest.mark.parametrize(
    "bad_json",
    [
        '{"rows":[],"rows":[]}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
    ],
)
def test_verifier_cli_rejects_ambiguous_or_nonfinite_result_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_json: str
) -> None:
    plan = _plan()
    descriptor = _timing(plan)
    plan_path, timing_path, result_path, output = (
        tmp_path / name
        for name in ("plan.json", "timing.json", "result.json", "verified.json")
    )
    plan_path.write_text(json.dumps(plan))
    timing_path.write_text(json.dumps(descriptor))
    result_path.write_text(bad_json)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vllm_timed_result",
            "--plan",
            str(plan_path),
            "--timing",
            str(timing_path),
            "--result",
            str(result_path),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(ValueError):
        timed_result.main()
    assert not output.exists()


@pytest.mark.parametrize(
    "certificate", [None, {"certificate_sha256": "sha256:" + "0" * 64}]
)
def test_measured_capacity_result_requires_the_matching_base_token_certificate(
    tmp_path: Path, certificate: dict[str, str] | None
) -> None:
    plan, _, result, _, _ = asyncio.run(_trial(tmp_path, "capacity"))
    # Capacity plans allow the same fixed population/window for either phase.
    # This parser fixture makes no claim to actual tokenizer certification.
    plan["phase"] = "evaluation"
    _rehash(plan, "plan_sha256")
    descriptor = _timing(plan)
    measurement = result["measurement"]
    measurement.update(
        plan_sha256=descriptor["timing_sha256"],
        trace_sha256=descriptor["transformed_trace_sha256"],
        base_plan_sha256=plan["plan_sha256"],
        base_trace_sha256=plan["trace_sha256"],
        evidence_class="LOCAL_MEASUREMENT_ONLY",
    )
    _rehash(measurement)
    _rehash(result)
    with pytest.raises(ValueError, match="certificate"):
        verify_timed_result(plan, descriptor, result, token_certificate=certificate)


def test_study_cli_rejects_explicit_null_timing_before_running_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path, timing_path, certificate_path, output = (
        tmp_path / name
        for name in ("plan.json", "timing.json", "certificate.json", "result.json")
    )
    plan_path.write_text(json.dumps(_plan()))
    timing_path.write_text("null")
    certificate_path.write_text("{}")

    async def unexpected_trial(*_args: object, **_kwargs: object) -> dict:
        pytest.fail("explicit null timing must fail before calling run_trial")

    monkeypatch.setattr(study, "run_trial", unexpected_trial)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "study",
            "run",
            "--plan",
            str(plan_path),
            "--timing",
            str(timing_path),
            "--token-certificate",
            str(certificate_path),
            "--router",
            "http://127.0.0.1:8090",
            "--model",
            "fake",
            "--policy",
            "round_robin",
            "--output",
            str(output),
            "--correlate-requests",
        ],
    )
    with pytest.raises((ValueError, SystemExit)):
        study.main()
    assert not output.exists() or output.read_bytes() == b""


def _resummarize_and_rehash(plan: dict[str, Any], result: dict[str, Any]) -> None:
    measurement = result["measurement"]
    measurement["summary"] = study.summarize(
        plan,
        [study.RequestResult(**row) for row in measurement["rows"]],
        include_client_timing=True,
    )
    _rehash(measurement)
    _rehash(result)


@pytest.mark.parametrize("outcome", ["cancelled", "transport_error"])
def test_identity_verification_cannot_hide_undispatched_completed_accounting(
    tmp_path: Path, outcome: str
) -> None:
    plan, descriptor, result, ledger, _ = asyncio.run(_trial(tmp_path))
    measurement = result["measurement"]
    row = measurement["rows"][0]
    row["outcome"] = outcome
    for field in (
        "ready_ns",
        "dispatch_ns",
        "response_headers_ns",
        "first_body_byte_ns",
        "first_content_ns",
        "max_content_gap_ns",
        "http_status",
        "prompt_tokens",
        "completion_tokens",
    ):
        row[field] = None
    dropped_link = result["request_links"][0]
    dropped_link["response_request_id"] = None
    ledger = [
        item for item in ledger if item["request_id"] != dropped_link["request_id"]
    ]
    _resummarize_and_rehash(plan, result)

    identity = verify_links(result, ledger)
    assert identity["correlation_valid"] is True
    assert identity["not_dispatched"] == 1
    assert identity["links"][0]["state"] == "NOT_DISPATCHED"
    assert measurement["status"] == "COMPLETED"
    assert measurement["router_accounting_valid"] is True
    with pytest.raises(ValueError, match="accounting status"):
        verify_timed_result(plan, descriptor, result)


def test_completed_trial_cannot_include_a_cancelled_dispatched_request(
    tmp_path: Path,
) -> None:
    plan, descriptor, result, _, _ = asyncio.run(_trial(tmp_path))
    measurement = result["measurement"]
    measurement["rows"][0]["outcome"] = "cancelled"
    assert measurement["rows"][0]["dispatch_ns"] is not None
    _resummarize_and_rehash(plan, result)
    with pytest.raises(ValueError, match="accounting status"):
        verify_timed_result(plan, descriptor, result)


@pytest.mark.parametrize(
    ("outcome", "http_status"),
    [
        ("rejected", 200),
        ("rejected", 500),
        ("http_error", 200),
        ("http_error", 503),
        ("protocol_error", 503),
        ("prompt_length_mismatch", 503),
        ("output_length_mismatch", 500),
    ],
)
def test_reported_outcome_must_match_the_http_observation(
    tmp_path: Path, outcome: str, http_status: int
) -> None:
    plan, descriptor, result, _, _ = asyncio.run(_trial(tmp_path))
    measurement = result["measurement"]
    measurement["rows"][0].update(outcome=outcome, http_status=http_status)
    measurement["comparison_valid"] = outcome not in {
        "prompt_length_mismatch",
        "output_length_mismatch",
    }
    _resummarize_and_rehash(plan, result)
    with pytest.raises(ValueError, match="observations invalid"):
        verify_timed_result(plan, descriptor, result)


@pytest.mark.parametrize("kind", ["baseline", "capacity"])
def test_timed_fixture_validates_a_provided_certificate_before_opening_http(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    plan = _plan(kind)

    def unexpected_session(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a malformed provided certificate must fail before creating HTTP")

    monkeypatch.setattr(study.aiohttp, "ClientSession", unexpected_session)
    with pytest.raises(ValueError, match="certificate"):
        asyncio.run(
            study.run_trial(
                plan,
                router_origin="http://127.0.0.1:8090",
                model="fake",
                expected_policy="round_robin",
                correlate_requests=True,
                timing=_timing(plan),
                token_certificate={},
            )
        )
