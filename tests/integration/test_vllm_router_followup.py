"""GPU-free checks for the single affinity saturation candidate and protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from inferdrome import vllm_router_capacity as capacity
from inferdrome import vllm_router_followup as followup
from inferdrome import vllm_router_gpu as gpu
from inferdrome import vllm_router_study as study
from inferdrome.vllm_affinity import SATURATION_RULE_VERSION, choose


class WordTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> SimpleNamespace:
        assert add_special_tokens is False
        return SimpleNamespace(ids=text.split())


def _workloads(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(study, "_capacity_tokenizer", lambda _root: WordTokenizer())
    base = study.capacity_workload(
        Path("unused"),
        document_count=8,
        target_prefix_tokens=256,
        context_length=512,
    )
    return {
        "control": {**base, "pattern_version": "control.v1"},
        "burst": {
            **base,
            "pattern_version": "burst-hot-shift.v1",
            "cycle_percent": 20,
            "hot_group_size": 2,
            "burst_size": 8,
            "burst_window_ns": 200_000_000,
        },
    }


def test_candidate_requires_both_saturation_and_relative_imbalance() -> None:
    held = choose("cache_saturation", (2, 0), 0, (1, 0), saturation_active=8)
    assert held.replica == 0 and held.reason == "estimated_affinity"
    assert held.relative_imbalance_reached and not held.saturation_reached
    below = choose("cache_saturation", (7, 5), 0, (1, 0), saturation_active=8)
    assert below.replica == 0
    at = choose("cache_saturation", (8, 6), 0, (1, 0), saturation_active=8)
    assert at.replica == 1 and at.reason == "saturated_escape"
    balanced = choose("cache_saturation", (8, 7), 0, (1, 0), saturation_active=8)
    assert balanced.replica == 0
    tie = choose("cache_saturation", (8, 6), 0, (1, 1), saturation_active=8)
    assert tie.replica == 1 and tie.preferred_replica == 0
    assert (
        choose("cache_saturation", (8, 6), 1, (1, 1), saturation_active=8).replica == 1
    )
    assert choose("cache_plus_load", (2, 0), 0, (1, 0)).replica == 1
    with pytest.raises(ValueError, match="bounded active threshold"):
        choose("cache_saturation", (2, 0), 0, (1, 0))


def test_control_reproduces_v2_and_burst_is_concentrated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workloads = _workloads(monkeypatch)
    seed, count, duration = 301, 600, 120_000_000_000
    old = study.capacity_trace(
        seed,
        count,
        duration,
        {
            key: value
            for key, value in workloads["control"].items()
            if key != "pattern_version"
        },
    )
    control = study.capacity_trace(seed, count, duration, workloads["control"])
    burst = study.capacity_trace(seed, count, duration, workloads["burst"])
    assert old == control
    assert [offer.scheduled_ns for offer in control] != [
        offer.scheduled_ns for offer in burst
    ]
    assert sum(offer.traffic_class == "hot" for offer in burst) > sum(
        offer.traffic_class == "hot" for offer in control
    )
    assert burst[7].scheduled_ns - burst[0].scheduled_ns < 200_000_000
    assert burst[8].scheduled_ns - burst[7].scheduled_ns > 1_000_000_000
    assert {offer.epoch for offer in burst} == {0, 1, 2}


def test_prepared_traces_orders_and_separate_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(followup, "_workloads", lambda _root: _workloads(monkeypatch))
    prepared = tmp_path / "prepared"
    followup.prepare(prepared, Path("unused"))
    manifest, plans = followup._prepared(prepared)
    assert manifest["calibration_seed"] not in manifest["evaluation_seeds"]
    assert manifest["pilot_seed"] not in manifest["evaluation_seeds"]
    assert len(plans) == 36
    assert all(plan["max_tokens"] == 128 for plan, _cert in plans.values())
    schedule = followup._schedule((4, 5))
    assert len(schedule) == 64
    for pattern in followup.PATTERNS:
        for rate in (4, 5):
            for block in range(1, 5):
                cells = [
                    x
                    for x in schedule
                    if (x["pattern"], x["rate_rps"], x["block"])
                    == (pattern, rate, block)
                ]
                assert set(x["policy"] for x in cells) == set(study.FOLLOWUP_POLICIES)
                assert (
                    len(
                        {
                            plans[
                                followup._name(
                                    "evaluation", pattern, cells[0]["seed"], rate
                                )
                            ][0]["trace_sha256"]
                        }
                    )
                    == 1
                )
    candidate_positions = [
        next(
            x["position"]
            for x in schedule
            if x["block"] == block
            and x["pattern"] == "control"
            and x["rate_rps"] == 4
            and x["policy"] == "cache_saturation"
        )
        for block in range(1, 5)
    ]
    assert set(candidate_positions) == {1, 2, 3, 4}
    chosen = followup._threshold_choice(
        [
            {
                "saturation_active": threshold,
                "pattern": pattern,
                "summary": {
                    "all_offered": {
                        "slo_goodput_rps": (5.0 if threshold == 10 else 4.0)
                    }
                },
            }
            for threshold in followup.THRESHOLDS
            for pattern in followup.PATTERNS
        ]
    )
    assert chosen == 10


def test_runner_freezes_threshold_and_rates_before_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    (prepared / "manifest.json").write_text("{}")
    manifest = {
        "patterns": {"control": {"context_length": 8192}},
        "plans": {},
    }
    monkeypatch.setattr(followup, "_prepared", lambda _root: (manifest, {}))
    monkeypatch.setattr(gpu, "_snapshot", lambda _path: {"sha256": "fixture"})
    monkeypatch.setattr(gpu, "_vllm_version", lambda _path: "0.26.0+cu129")
    monkeypatch.setattr(capacity, "_verify_cli_flags", lambda _path: {"ok": True})
    monkeypatch.setattr(gpu, "_gpu_observation", lambda: [{"model": "A100"}] * 2)
    monkeypatch.setattr(gpu, "_ports_closed", lambda: True)
    monkeypatch.setattr(gpu, "_start_engines", lambda *_args, **_kwargs: ([], []))
    monkeypatch.setattr(gpu, "_stop_engines", lambda *_args: None)
    monkeypatch.setattr(
        capacity, "_cache_capacity", lambda *_args: {"candidate_valid": True}
    )

    async def ready(*_args: object) -> None:
        return None

    async def get(*_args: object) -> tuple[int, str]:
        return 200, "Qwen/Qwen3-8B"

    monkeypatch.setattr(gpu, "_ready", ready)
    monkeypatch.setattr(gpu, "_get", get)
    evaluation_seen = 0

    async def condition(
        _session: object,
        out: Path,
        record: dict,
        _plans: dict,
        *,
        phase: str,
        pattern: str,
        rate: int,
        policy: str,
        label: str,
        saturation_active: int | None = None,
        **_kwargs: object,
    ) -> dict:
        nonlocal evaluation_seen
        if phase == "evaluation":
            evaluation_seen += 1
            assert (out / "threshold-freeze.json").is_file()
            assert (out / "rate-freeze.json").is_file()
            assert (out / "evaluation-schedule.json").is_file()
            assert saturation_active == (10 if policy == "cache_saturation" else None)
        goodput = (
            (5.0 if saturation_active == 10 else 4.0)
            if phase == "calibration"
            else float(rate)
        )
        passed = not (phase == "pilot" and pattern == "burst" and rate == 5)
        item = {
            "label": label,
            "phase": phase,
            "pattern": pattern,
            "rate_rps": rate,
            "policy": policy,
            "saturation_active": saturation_active,
            "trial_status": "COMPLETED",
            "comparison_valid": True,
            "engine_metrics_status": "CAPTURED",
            "capacity_assessment": {
                "passed": passed,
                "generator_limited": False,
                "admission_limited": False,
            },
            "summary": {"all_offered": {"slo_goodput_rps": goodput}},
        }
        record["conditions"].append(item)
        return item

    monkeypatch.setattr(followup, "_condition", condition)
    args = SimpleNamespace(
        prepared_dir=prepared,
        model_dir=tmp_path,
        vllm_executable=tmp_path / "vllm",
        image_reference=gpu.IMAGE,
        source_commit="a" * 40,
        instance_id="123",
        billing_start_utc="2026-09-29T00:00:00+00:00",
        hourly_rate_usd="1",
        cap_usd="100",
        reserve_usd="1",
        output_root=tmp_path / "raw",
    )
    # The budget clock must begin near the test's current UTC time.
    from datetime import UTC, datetime

    args.billing_start_utc = datetime.now(UTC).isoformat()
    asyncio.run(followup.run(args))
    session = json.loads((args.output_root / "session.json").read_text())
    assert session["status"] == "COMPLETED"
    assert session["frozen_saturation_active"] == 10
    assert session["frozen_rates_rps"] == [4, 5]
    assert evaluation_seen == 64
    assert len(session["conditions"]) == 74


def test_forged_decision_is_rejected(tmp_path: Path) -> None:
    ledger = tmp_path / "router.jsonl"
    row = {
        "policy": "cache_saturation",
        "replica": 0,
        "route_reason": "estimated_affinity",
        "preferred_replica": 0,
        "saturation_threshold_active": 8,
        "saturation_rule_version": SATURATION_RULE_VERSION,
        "escape_busy_delta": 2,
        "saturation_reached": False,
        "relative_imbalance_reached": True,
        "turn_at_decision": 0,
        "busy_at_decision": [2, 0],
        "estimated_affinity": [1, 0],
    }
    ledger.write_text(json.dumps(row) + "\n")
    assert (
        followup._verify_route_decisions(ledger, "cache_saturation", 8)["decisions"]
        == 1
    )
    row["replica"] = 1
    ledger.write_text(json.dumps(row) + "\n")
    with pytest.raises(gpu.StudyError, match="frozen rule"):
        followup._verify_route_decisions(ledger, "cache_saturation", 8)


def test_invalid_evidence_and_harness_limits_do_not_count_as_policy_losses() -> None:
    item = {
        "engine_metrics_status": "INVALID_COUNTERS",
        "trial_status": "COMPLETED",
        "comparison_valid": True,
    }
    assert followup._condition_failure(item) == "INVALID_EVIDENCE"
    item["engine_metrics_status"] = "CAPTURED"
    item["capacity_assessment"] = {
        "generator_limited": False,
        "admission_limited": True,
    }
    assert followup._condition_failure(item) == "HARNESS_LIMIT"


@pytest.mark.parametrize("command", ["prepare", "run", "report"])
def test_module_entry_fails_safely_before_gpu(tmp_path: Path, command: str) -> None:
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / command
    arguments = (
        ["--tokenizer-root", str(tmp_path / "missing"), "--output-root", str(output)]
        if command == "prepare"
        else ["--raw-root", str(tmp_path / "missing"), "--output-root", str(output)]
        if command == "report"
        else [
            "--prepared-dir",
            str(tmp_path / "missing"),
            "--model-dir",
            str(tmp_path / "missing-model"),
            "--vllm-executable",
            str(tmp_path / "missing-vllm"),
            "--image-reference",
            gpu.IMAGE,
            "--source-commit",
            "a" * 40,
            "--instance-id",
            "123",
            "--billing-start-utc",
            "2026-09-29T00:00:00+00:00",
            "--hourly-rate-usd",
            "1",
            "--cap-usd",
            "10",
            "--reserve-usd",
            "1",
            "--output-root",
            str(output),
        ]
    )
    result = subprocess.run(
        [sys.executable, "-m", "inferdrome.vllm_router_followup", command, *arguments],
        cwd=root,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                filter(None, (str(root / "src"), os.environ.get("PYTHONPATH")))
            ),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "vllm-router-followup:" in result.stderr


def test_incomplete_report_rejects_forged_row_and_invalid_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(followup, "_workloads", lambda _root: _workloads(monkeypatch))
    prepared = tmp_path / "prepared"
    followup.prepare(prepared, Path("unused"))
    _manifest, plans = followup._prepared(prepared)
    raw = tmp_path / "raw"
    raw.mkdir()
    import shutil

    shutil.copytree(prepared, raw / "prepared")
    label = "calibration-control-t8-5rps"
    plan, cert = plans[
        followup._name("calibration", "control", followup.CALIBRATION_SEED, 5)
    ]
    offers = study.validate_capacity_plan(plan)
    rows = [
        study.RequestResult(
            index=offer.index,
            scheduled_ns=offer.scheduled_ns,
            epoch=offer.epoch,
            traffic_class=offer.traffic_class,
            tenant=offer.tenant,
            document_id=offer.document_id,
            ready_ns=offer.scheduled_ns + 1_000_000,
            dispatch_ns=offer.scheduled_ns + 2_000_000,
            response_headers_ns=offer.scheduled_ns + 10_000_000,
            first_body_byte_ns=offer.scheduled_ns + 20_000_000,
            first_content_ns=offer.scheduled_ns + 100_000_000,
            terminal_ns=offer.scheduled_ns + 4_000_000_000,
            max_content_gap_ns=100_000_000,
            outcome="completed",
            http_status=200,
            prompt_tokens=plan["prompt_tokens_by_index"][offer.index],
            completion_tokens=plan["max_tokens"],
        )
        for offer in offers
    ]
    summary = study.summarize(plan, rows)
    count = len(rows)
    client = {
        "schema": "inferdrome.vllm-router-capacity-result.v2",
        "plan_sha256": plan["plan_sha256"],
        "trace_sha256": plan["trace_sha256"],
        "token_certificate_sha256": cert["certificate_sha256"],
        "policy": "cache_saturation",
        "status": "COMPLETED",
        "comparison_valid": True,
        "router_accounting_valid": True,
        "router_stats_after": {
            "policy": "cache_saturation",
            "offered": count,
            "terminal": count,
            "ledger_rows": count,
            "in_flight": 0,
            "pending": 0,
            "active": 0,
            "busy": [0, 0],
            "accounting_failed": False,
            "saturation_active": 8,
            "saturation_rule_version": SATURATION_RULE_VERSION,
        },
        "summary": summary,
        "rows": [asdict(row) for row in rows],
    }
    client["result_sha256"] = study._digest(client)
    client_path = raw / f"{label}-client.json"
    client_hash = gpu._save(client_path, client)
    ledger = raw / f"{label}-router.jsonl"
    ledger.write_text(
        "".join(
            json.dumps(
                {
                    "policy": "cache_saturation",
                    "replica": index % 2,
                    "route_reason": "affinity_tie",
                    "preferred_replica": index % 2,
                    "saturation_threshold_active": 8,
                    "saturation_rule_version": SATURATION_RULE_VERSION,
                    "escape_busy_delta": 2,
                    "saturation_reached": False,
                    "relative_imbalance_reached": False,
                    "turn_at_decision": index,
                    "busy_at_decision": [0, 0],
                    "estimated_affinity": [0, 0],
                    "outcome": "completed",
                    "queued_ms": 1,
                    "arrived_ns": index,
                    "document_sha256": hashlib.sha256(
                        study.capacity_document(
                            offers[index].document_id,
                            plan["workload"]["document_repeats"][
                                offers[index].document_id
                            ],
                        ).encode("utf-8")
                    ).hexdigest(),
                }
            )
            + "\n"
            for index in range(count)
        )
    )
    metrics = {
        "label": label,
        "replicas": [
            {
                "port": port,
                "prefix_counter_deltas": {
                    "vllm:prefix_cache_hits_total": 100,
                    "vllm:prefix_cache_queries_total": 200,
                },
            }
            for port in gpu.ENGINE_PORTS
        ],
    }
    (raw / f"metrics-{label}.json").write_text(json.dumps(metrics))
    (raw / f"reset-{label}.json").write_text(
        json.dumps(
            {
                "label": label,
                "replicas": [{"reset_success": True}] * 2,
            }
        )
    )
    item = {
        "label": label,
        "phase": "calibration",
        "pattern": "control",
        "rate_rps": 5,
        "seed": followup.CALIBRATION_SEED,
        "block": None,
        "policy": "cache_saturation",
        "saturation_active": 8,
        "plan_sha256": plan["plan_sha256"],
        "result_sha256": client_hash,
        "ledger_sha256": gpu._file_digest(ledger),
        "trial_status": "COMPLETED",
        "comparison_valid": True,
        "summary": summary,
        "engine_metrics_status": "CAPTURED",
        "engine_metrics": metrics,
    }
    item["capacity_diagnostics"] = capacity._ledger_diagnostics(ledger, plan)
    item["capacity_assessment"] = capacity._condition_assessment(item, count)
    session = {
        "schema": followup.SESSION_SCHEMA,
        "status": "INVALID_CALIBRATION",
        "conditions": [item],
        "prepared_manifest_sha256": gpu._file_digest(raw / "prepared/manifest.json"),
        "error": None,
    }
    (raw / "session.json").write_text(json.dumps(session))
    assert followup.report(raw, tmp_path / "incomplete")["status"] == "INCOMPLETE"
    client["rows"][0]["document_id"] = 999
    client["result_sha256"] = study._digest(
        {k: v for k, v in client.items() if k != "result_sha256"}
    )
    client_path.write_text(json.dumps(client))
    item["result_sha256"] = gpu._file_digest(client_path)
    (raw / "session.json").write_text(json.dumps(session))
    with pytest.raises(gpu.StudyError, match="row trace mismatch"):
        followup.report(raw, tmp_path / "forged-row")
    client["rows"][0]["document_id"] = offers[0].document_id
    client["result_sha256"] = study._digest(
        {k: v for k, v in client.items() if k != "result_sha256"}
    )
    client_path.write_text(json.dumps(client))
    item["result_sha256"] = gpu._file_digest(client_path)
    metrics["replicas"][0]["prefix_counter_deltas"] = {}
    (raw / f"metrics-{label}.json").write_text(json.dumps(metrics))
    (raw / "session.json").write_text(json.dumps(session))
    result = followup.report(raw, tmp_path / "invalid-counters")
    assert result["status"] == "INCOMPLETE"
    assert result["conditions"][0]["cache_counter_status"] == "INVALID_OR_MISSING"
