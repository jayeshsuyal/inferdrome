"""GPU-free checks of the versioned cache-pressure study's decisions and evidence."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from inferdrome import vllm_router_capacity as capacity
from inferdrome import vllm_router_gpu as gpu
from inferdrome import vllm_router_study as study


class WordTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> SimpleNamespace:
        assert add_special_tokens is False
        return SimpleNamespace(ids=text.split())


def _workload(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(study, "_capacity_tokenizer", lambda _root: WordTokenizer())
    return study.capacity_workload(
        Path("unused"), document_count=8, target_prefix_tokens=256, context_length=512
    )


def _plan(monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    workload = _workload(monkeypatch)
    offers = study.capacity_trace(101, 12, 12_000_000_000, workload)
    lengths = capacity._tokenizer_lengths(offers, Path("unused"))
    plan = study.make_capacity_plan(
        phase="evaluation",
        seed=101,
        count=12,
        duration_ns=12_000_000_000,
        workload=workload,
        prompt_tokens_by_index=lengths,
    )
    cert = study.certify_capacity_plan(plan, Path("unused"))
    return plan, cert


def _item(*, first_ns: int = 100_000_000, lag_ns: int = 1_000_000) -> dict:
    return {
        "comparison_valid": True,
        "trial_status": "COMPLETED",
        "engine_metrics_status": "CAPTURED",
        "summary": {
            "all_offered": {
                "slo_good": 96,
                "outcomes": {"completed": 100},
                "successful_only": {
                    "scheduled_to_first_content": {"p95_ns": first_ns},
                    "scheduled_to_terminal": {"p95_ns": 4_000_000_000},
                },
            },
            "client_timing": {
                "scheduling_lag": {"p95_ns": lag_ns},
                "client_queue_delay": {"p95_ns": 1_000_000},
                "never_dispatched": 0,
            },
        },
        "capacity_diagnostics": {
            "router_ledger_rows": 100,
            "router_outcomes": {"completed": 100},
        },
    }


def test_ttft_failure_fails_even_when_completion_passes() -> None:
    assessment = capacity._condition_assessment(_item(first_ns=600_000_000), 100)
    assert assessment["completion_p95_ns"] == 4_000_000_000
    assert assessment["passed"] is False


def test_unbracketed_and_harness_limited_pilots() -> None:
    passed = {
        "capacity_assessment": {
            "passed": True,
            "generator_limited": False,
            "admission_limited": False,
        }
    }
    assert capacity._pilot_boundary([passed] * 3, (2, 4, 6))["status"] == "UNBRACKETED"
    failed = {
        "capacity_assessment": {
            "passed": False,
            "generator_limited": False,
            "admission_limited": False,
        }
    }
    assert capacity._pilot_boundary([passed, passed, failed], (2, 4, 6)) == {
        "status": "BRACKETED",
        "rates_rps": [2, 4, 6],
        "first_failed_rps": 6,
    }
    limited = {
        "capacity_assessment": {
            "passed": False,
            "generator_limited": True,
            "admission_limited": False,
        }
    }
    assert (
        capacity._pilot_boundary([passed, limited], (2, 4, 6))["status"]
        == "HARNESS_LIMIT"
    )


def test_generator_and_router_admission_limits_are_explicit() -> None:
    delayed = capacity._condition_assessment(_item(lag_ns=80_000_000), 100)
    assert delayed["generator_limited"] is True
    assert delayed["passed"] is False
    item = _item()
    item["capacity_diagnostics"]["router_outcomes"] = {"rejected_capacity": 1}
    admitted = capacity._condition_assessment(item, 100)
    assert admitted["admission_limited"] is True
    assert admitted["passed"] is False


def test_exact_prompt_and_context_certificate(monkeypatch: pytest.MonkeyPatch) -> None:
    plan, certificate = _plan(monkeypatch)
    assert plan["schema"] == study.CAPACITY_SCHEMA
    assert max(plan["prompt_tokens_by_index"]) + plan["max_tokens"] <= 512
    study.validate_capacity_certificate(plan, certificate)
    offers = study.validate_plan(plan)
    assert len({offer.document_id for offer in offers}) > 1
    tampered = dict(plan)
    tampered["prompt_tokens_by_index"] = list(plan["prompt_tokens_by_index"])
    tampered["prompt_tokens_by_index"][0] += 1
    with pytest.raises(ValueError, match="frozen deterministic recipe"):
        study.validate_capacity_plan(tampered)
    with pytest.raises(ValueError, match="bounds"):
        study.make_capacity_plan(
            phase="evaluation",
            seed=101,
            count=12,
            duration_ns=12_000_000_000,
            workload=plan["workload"],
            prompt_tokens_by_index=[512] * 12,
        )


def test_matched_trace_for_every_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    plan, _ = _plan(monkeypatch)
    schedule = capacity._schedule([1, 2, 3])
    block = [row for row in schedule if row["block"] == 1 and row["rate_rps"] == 1]
    assert {row["policy"] for row in block} == set(study.POLICIES)
    assert len({row["seed"] for row in block}) == 1
    assert len({plan["trace_sha256"] for _row in block}) == 1


def test_failed_offer_stays_in_goodput_denominator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _ = _plan(monkeypatch)
    plan["offered_count"] = 2
    plan["duration_ns"] = 1_000_000_000
    rows = [
        study.RequestResult(
            0,
            0,
            0,
            "cycle",
            "tenant-0",
            0,
            10,
            20,
            100_000_000,
            400_000_000,
            0,
            "completed",
            200,
            300,
            128,
            0,
            0,
        ),
        study.RequestResult(
            1,
            500_000_000,
            1,
            "hot",
            "tenant-1",
            500_000_000,
            510_000_000,
            None,
            None,
            600_000_000,
            None,
            "rejected",
            503,
            None,
            None,
            500_000_000,
            1,
        ),
    ]
    summary = study.summarize(plan, rows)
    assert summary["all_offered"]["offered"] == 2
    assert summary["all_offered"]["slo_good"] == 1
    assert summary["all_offered"]["slo_goodput_rps"] == 1.0


def test_capacity_candidate_uses_observed_log_size(tmp_path: Path) -> None:
    logs = [tmp_path / "engine-0.log", tmp_path / "engine-1.log"]
    for log in logs:
        log.write_text("GPU KV cache size: 138,144 tokens\n")
    observation = capacity._cache_capacity(
        logs, {"document_prefix_tokens": [4096] * 48}
    )
    assert observation["candidate_valid"] is True
    assert observation["residency_claim"] == "NONE_OBSERVED_CAPACITY_ONLY"


def test_pinned_cli_flags_for_apc_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def help_result(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        assert argv[-1] == "--help=all"
        return SimpleNamespace(
            stdout="--enable-prefix-caching --no-enable-prefix-caching "
            "--no-enable-log-requests --max-model-len",
            stderr="",
        )

    monkeypatch.setattr(capacity.subprocess, "run", help_result)
    assert all(capacity._verify_cli_flags(tmp_path / "vllm").values())
    monkeypatch.setattr(
        capacity.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="Config Groups: model, scheduler, cache", stderr=""
        ),
    )
    with pytest.raises(gpu.StudyError, match="missing required flags"):
        capacity._verify_cli_flags(tmp_path / "vllm")
    argv = gpu._engine_argv(
        tmp_path / "vllm",
        tmp_path / "model",
        8001,
        context_length=8192,
        prefix_caching=False,
    )
    assert "--no-enable-prefix-caching" in argv
    assert "--no-enable-log-requests" in argv
    assert argv[argv.index("--max-model-len") + 1] == "8192"


@pytest.mark.parametrize("command", ["run", "diagnostic", "report"])
def test_documented_module_entry_fails_safely_before_gpu(
    tmp_path: Path, command: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    common = [
        "--prepared-dir",
        str(tmp_path / "missing-prepared"),
        "--model-dir",
        str(tmp_path / "missing-model"),
        "--vllm-executable",
        str(tmp_path / "missing-vllm"),
        "--image-reference",
        gpu.IMAGE,
        "--instance-id",
        "123",
        "--billing-start-utc",
        datetime.now(UTC).isoformat(),
        "--hourly-rate-usd",
        "1",
        "--cap-usd",
        "10",
        "--reserve-usd",
        "1",
        "--output-root",
        str(tmp_path / command),
    ]
    arguments = (
        [
            "--raw-root",
            str(tmp_path / "missing-raw"),
            "--output-root",
            str(tmp_path / command),
        ]
        if command == "report"
        else common + (["--source-commit", "a" * 40] if command == "run" else [])
    )
    result = subprocess.run(
        [sys.executable, "-m", "inferdrome.vllm_router_capacity", command, *arguments],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "vllm-router-capacity:" in result.stderr
    assert "NameError" not in result.stderr
    if command == "run":
        assert (tmp_path / "run/preflight-error.json").is_file()


def test_incomplete_report_and_tampered_input_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workload(monkeypatch)
    prepared = tmp_path / "prepared"
    capacity.prepare(
        prepared,
        Path("unused"),
        rates=(1, 2, 3),
        duration_s=60,
        document_count=8,
        target_prefix_tokens=256,
        context_length=512,
    )
    raw = tmp_path / "raw"
    raw.mkdir()
    shutil.copytree(prepared, raw / "prepared")
    gpu._save(
        raw / "session.json",
        {
            "schema": capacity.SESSION_SCHEMA,
            "status": "FAILED",
            "conditions": [],
            "prepared_manifest_sha256": gpu._file_digest(prepared / "manifest.json"),
            "error": "fixture failure",
        },
    )
    capacity.report(raw, tmp_path / "report")
    result = json.loads((tmp_path / "report/report.json").read_text())
    assert result["status"] == "INCOMPLETE"
    (raw / "prepared/manifest.json").write_text("{}")
    with pytest.raises((gpu.StudyError, KeyError, ValueError)):
        capacity.report(raw, tmp_path / "tampered")


@pytest.mark.parametrize("bracketed", [True, False])
def test_pilot_freezes_schedule_or_reports_unbracketed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bracketed: bool
) -> None:
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    (prepared / "manifest.json").write_text("{}")
    manifest = {
        "rates_rps": [1, 2, 3],
        "duration_s": 60,
        "workload": {"context_length": 8192},
        "router_max_active": 128,
        "router_max_queue": 256,
        "plans": {},
    }
    plans = {
        capacity._plan_name(phase, seed, rate): ({"offered_count": rate * 60}, {})
        for phase, seeds in (
            ("pilot", (capacity.PILOT_SEED,)),
            ("evaluation", capacity.EVALUATION_SEEDS),
        )
        for seed in seeds
        for rate in (1, 2, 3)
    }
    monkeypatch.setattr(capacity, "_prepared", lambda _root: (manifest, plans))
    monkeypatch.setattr(gpu, "_snapshot", lambda _path: {"sha256": "fixture"})
    monkeypatch.setattr(gpu, "_vllm_version", lambda _path: "vllm 0.26.0")
    monkeypatch.setattr(capacity, "_verify_cli_flags", lambda _path: {"ok": True})
    monkeypatch.setattr(gpu, "_gpu_observation", lambda: [{"model": "A100"}] * 2)
    monkeypatch.setattr(gpu, "_ports_closed", lambda: True)
    monkeypatch.setattr(gpu, "_start_engines", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(gpu, "_stop_engines", lambda *_a: None)
    monkeypatch.setattr(
        capacity,
        "_cache_capacity",
        lambda *_a: {"candidate_valid": True},
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
        *,
        label: str,
        policy: str,
        plan: dict,
        **_kwargs: object,
    ) -> dict:
        nonlocal evaluation_seen
        if label.startswith("evaluation-"):
            evaluation_seen += 1
            assert (out / "pilot-freeze.json").is_file()
            assert (out / "evaluation-schedule.json").is_file()
        rate = int(label.rsplit("-", 1)[-1][:-3]) if label.startswith("pilot-") else 0
        passed = not (bracketed and rate == 3)
        item = {
            "label": label,
            "policy": policy,
            "comparison_valid": True,
            "trial_status": "COMPLETED",
            "capacity_assessment": {
                "passed": passed,
                "generator_limited": False,
                "admission_limited": False,
            },
            "offered_count": plan["offered_count"],
        }
        record["conditions"].append(item)
        return item

    monkeypatch.setattr(capacity, "_run_condition", condition)
    raw = tmp_path / "raw"
    args = SimpleNamespace(
        billing_start_utc=datetime.now(UTC).isoformat(),
        hourly_rate_usd="1",
        cap_usd="100",
        reserve_usd="1",
        output_root=raw,
        prepared_dir=prepared,
        image_reference=gpu.IMAGE,
        source_commit="a" * 40,
        instance_id="123",
        model_dir=tmp_path,
        vllm_executable=tmp_path / "vllm",
    )
    asyncio.run(capacity.run(args))
    session = json.loads((raw / "session.json").read_text())
    if bracketed:
        assert session["status"] == "COMPLETED"
        assert evaluation_seen == 48
        assert len(session["conditions"]) == 51
    else:
        assert session["status"] == "UNBRACKETED"
        assert evaluation_seen == 0
        assert len(session["conditions"]) == 3


def test_complete_offline_report_verifies_matched_raw_population(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workload(monkeypatch)
    prepared = tmp_path / "prepared"
    capacity.prepare(
        prepared,
        Path("unused"),
        rates=(1, 2, 3),
        duration_s=60,
        document_count=8,
        target_prefix_tokens=256,
        context_length=512,
    )
    _manifest, plans = capacity._prepared(prepared)
    raw = tmp_path / "raw"
    raw.mkdir()
    shutil.copytree(prepared, raw / "prepared")
    items: list[dict] = []

    def add(label: str, phase: str, seed: int, rate: int, policy: str) -> None:
        plan, cert = plans[capacity._plan_name(phase, seed, rate)]
        count = plan["offered_count"]
        failed_pilot = phase == "pilot" and rate == 3
        first_ns = 600_000_000 if failed_pilot else 100_000_000
        good = int(count * 0.9) if failed_pilot else count
        summary = {
            "all_offered": {
                "offered": count,
                "slo_good": good,
                "slo_goodput_rps": good / 60,
                "goodput_denominator_ns": 60_000_000_000,
                "outcomes": {"completed": count},
                "successful_only": {
                    "scheduled_to_first_content": {"p95_ns": first_ns},
                    "scheduled_to_terminal": {"p95_ns": 4_000_000_000},
                },
            },
            "client_timing": {
                "scheduling_lag": {"p95_ns": 1_000_000},
                "client_queue_delay": {"p95_ns": 1_000_000},
                "never_dispatched": 0,
            },
        }
        ledger = raw / f"{label}-router.jsonl"
        ledger.write_text(
            "".join(
                json.dumps(
                    {
                        "outcome": "completed",
                        "replica": index % 2,
                        "route_reason": policy,
                        "estimated_affinity": [0, 0],
                        "busy_at_decision": [0, 0],
                        "queued_ms": 1,
                    }
                )
                + "\n"
                for index in range(count)
            )
        )
        client = {
            "plan_sha256": plan["plan_sha256"],
            "token_certificate_sha256": cert["certificate_sha256"],
            "status": "COMPLETED",
            "comparison_valid": True,
            "summary": summary,
            "rows": [
                {"index": index, "outcome": "completed"} for index in range(count)
            ],
        }
        result_sha = gpu._save(raw / f"{label}-client.json", client)
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
                for port in (8001, 8002)
            ],
        }
        gpu._save(raw / f"metrics-{label}.json", metrics)
        gpu._save(
            raw / f"reset-{label}.json",
            {
                "label": label,
                "replicas": [{"reset_success": True}, {"reset_success": True}],
            },
        )
        item = {
            "label": label,
            "policy": policy,
            "plan_sha256": plan["plan_sha256"],
            "result_sha256": result_sha,
            "ledger_sha256": gpu._file_digest(ledger),
            "trial_status": "COMPLETED",
            "comparison_valid": True,
            "summary": summary,
            "engine_metrics_status": "CAPTURED",
            "engine_metrics": metrics,
        }
        item["capacity_diagnostics"] = capacity._ledger_diagnostics(ledger, plan)
        item["capacity_assessment"] = capacity._condition_assessment(item, count)
        items.append(item)

    for rate in (1, 2, 3):
        add(
            capacity._plan_name("pilot", capacity.PILOT_SEED, rate),
            "pilot",
            capacity.PILOT_SEED,
            rate,
            "least_busy",
        )
    boundary = capacity._pilot_boundary(items, (1, 2, 3))
    assert boundary["status"] == "BRACKETED"
    schedule = capacity._schedule(boundary["rates_rps"])
    schedule_hash = gpu._save(
        raw / "evaluation-schedule.json",
        {
            "rates_rps": boundary["rates_rps"],
            "conditions": schedule,
        },
    )
    for planned in schedule:
        add(
            planned["label"],
            "evaluation",
            planned["seed"],
            planned["rate_rps"],
            planned["policy"],
        )
    gpu._save(
        raw / "session.json",
        {
            "schema": capacity.SESSION_SCHEMA,
            "status": "COMPLETED",
            "conditions": items,
            "prepared_manifest_sha256": gpu._file_digest(prepared / "manifest.json"),
            "pilot_boundary": boundary,
            "evaluation_schedule": schedule,
            "evaluation_schedule_sha256": schedule_hash,
            "error": None,
        },
    )
    capacity.report(raw, tmp_path / "report")
    result = json.loads((tmp_path / "report/report.json").read_text())
    assert result["status"] == "COMPLETE_DESCRIPTIVE"
    assert len(result["block_differences"]) == 48
    assert len(result["policy_rate_summary"]) == 12
    assert all(
        row["difference_vs_least_busy"] == "TIE"
        for row in result["policy_rate_summary"]
    )
    ledger = raw / f"{schedule[0]['label']}-router.jsonl"
    ledger.write_text(ledger.read_text() + "{}\n")
    with pytest.raises(gpu.StudyError, match="raw artifact hash mismatch"):
        capacity.report(raw, tmp_path / "tampered-report")
