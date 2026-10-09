"""Real timed client/router artifacts feed the offline paired oracle, without GPUs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import web

from inferdrome.vllm_arrival_timing import make_timing
from inferdrome.vllm_paired_comparison import compare
from inferdrome.vllm_paired_protocol import make_protocol
from inferdrome.vllm_router import Router, make_app
from inferdrome.vllm_router_study import make_plan, run_trial


async def _serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def _replica() -> web.Application:
    async def chat(_request: web.Request) -> web.Response:
        return web.Response(
            content_type="text/event-stream",
            text=(
                'data: {"choices":[{"index":0,"delta":{"content":"x"},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[{"index":0,"delta":{},'
                '"finish_reason":"length"}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":200,'
                '"completion_tokens":4,"total_tokens":204}}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


async def _campaign(tmp_path: Path) -> dict[str, Any]:
    plans = [
        make_plan(
            phase="fixture",
            seed=seed,
            count=12,
            duration_ns=120_000_000,
            expected_prompt_tokens=200,
            max_tokens=4,
            first_content_slo_ns=5_000_000_000,
            completion_slo_ns=10_000_000_000,
        )
        for seed in range(4)
    ]
    parameters = {
        "group_size": 4,
        "retained_spacing_bps": 0,
        "max_advance_ns": 120_000_000,
    }
    protocol = make_protocol(
        plans,
        policy_a="cache_only",
        policy_b="least_busy",
        candidate_parameters=parameters,
        order_seed=101,
        minimum_effect_microrps=0,
        max_scheduling_lag_p95_ns=5_000_000_000,
        max_client_queue_p95_ns=5_000_000_000,
        model="fake",
        source_revision="1" * 40,
        environment_sha256="sha256:" + "2" * 64,
        reset_procedure_sha256="sha256:" + "3" * 64,
    )
    inputs = []
    for trial in protocol["trials"]:
        plan = plans[trial["block"] - 1]
        timing = make_timing(
            plan,
            **(
                parameters
                if trial["condition"] == "candidate"
                else {
                    "group_size": 1,
                    "retained_spacing_bps": 10000,
                    "max_advance_ns": 0,
                }
            ),
        )
        # Each synthetic cell gets a new router and two independent fake servers.
        a, a_url = await _serve(_replica())
        b, b_url = await _serve(_replica())
        ledger_path = tmp_path / f"{trial['trial_id']}.jsonl"
        router = Router((a_url, b_url), ledger_path, policy=trial["policy"])
        proxy, proxy_url = await _serve(make_app(router))
        try:
            result = await run_trial(
                plan,
                router_origin=proxy_url,
                model="fake",
                expected_policy=trial["policy"],
                correlate_requests=True,
                timing=timing,
            )
            inputs.append(
                {
                    "trial_id": trial["trial_id"],
                    "timing": timing,
                    "result": result,
                    "ledger_rows": [
                        json.loads(line)
                        for line in ledger_path.read_text().splitlines()
                    ],
                    "token_certificate": None,
                    "execution": {
                        key: protocol[key]
                        for key in (
                            "source_revision",
                            "environment_sha256",
                            "reset_procedure_sha256",
                        )
                    }
                    | {"reset_completed": True},
                }
            )
            # The client can finish its final request before the full offered
            # window expires; do not begin another condition within that window.
            await asyncio.sleep(plan["duration_ns"] / 1e9)
        finally:
            await proxy.cleanup()
            await a.cleanup()
            await b.cleanup()
    return compare(protocol, plans, inputs)


def test_two_policies_two_schedules_four_blocks_recheck_real_loopback_artifacts(
    tmp_path: Path,
) -> None:
    report = asyncio.run(_campaign(tmp_path))
    assert report["status"] == "INCONCLUSIVE", report["ineligibility_reasons"]
    assert report["planned_trials"] == report["supplied_trials"] == 16
    assert report["ineligibility_reasons"] == []
    assert report["evidence_class"] == "SYNTHETIC_ONLY"
    assert report["evidence_eligible"] is False
    assert all(
        trial["slo_good"] == trial["offered"] == 12 for trial in report["trials"]
    )
    assert all(
        block["baseline"]["a_minus_b_slo_good"] == 0 for block in report["blocks"]
    )
    assert all(
        block["candidate"]["a_minus_b_slo_good"] == 0 for block in report["blocks"]
    )
    assert report["statistics"]["reversal_signal"] is False
