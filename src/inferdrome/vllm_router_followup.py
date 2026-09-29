"""Bounded two-pattern comparison of a calibrated affinity saturation gate.

Preparation and reporting are offline. Run owns only local vLLM processes on
an operator-provided host; it never rents, stops, or destroys a provider VM.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

from inferdrome import vllm_router_capacity as capacity
from inferdrome import vllm_router_gpu as gpu
from inferdrome.errors import AdapterError
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    qwen3_expected_snapshot_sha256,
)
from inferdrome.vllm_affinity import (
    ESCAPE_BUSY_DELTA,
    SATURATION_RULE_VERSION,
    choose,
)
from inferdrome.vllm_router_study import (
    FOLLOWUP_POLICIES,
    capacity_document,
    capacity_trace,
    capacity_workload,
    certify_capacity_plan,
    make_capacity_plan,
    validate_capacity_certificate,
    validate_capacity_plan,
)

MANIFEST_SCHEMA = "inferdrome.vllm-affinity-followup-manifest.v1"
SESSION_SCHEMA = "inferdrome.vllm-affinity-followup-session.v1"
REPORT_SCHEMA = "inferdrome.vllm-affinity-followup-report.v1"
PATTERNS = ("control", "burst")
RATES = (4, 5, 6)
THRESHOLDS = (8, 10, 12)
CALIBRATION_SEED = 211
PILOT_SEED = 223
EVALUATION_SEEDS = (301, 307, 311, 313)
ORDERS = (
    FOLLOWUP_POLICIES,
    FOLLOWUP_POLICIES[1:] + FOLLOWUP_POLICIES[:1],
    FOLLOWUP_POLICIES[2:] + FOLLOWUP_POLICIES[:2],
    FOLLOWUP_POLICIES[3:] + FOLLOWUP_POLICIES[:3],
)
DURATION_S = 120
THRESHOLD_SELECTION_RULE = (
    "maximize minimum pattern SLO goodput at 5 rps; then sum; then larger threshold"
)
PILOT_SELECTION_RULE = (
    "4,5 if either pattern fails at 5; otherwise 5,6; both patterns must pass at 4"
)


def _name(phase: str, pattern: str, seed: int, rate: int) -> str:
    return f"{phase}-{pattern}-{seed}-{rate}rps"


def _workloads(tokenizer_root: Path) -> dict[str, dict[str, Any]]:
    base = capacity_workload(tokenizer_root)
    control = {**base, "pattern_version": "control.v1"}
    burst = {
        **base,
        "pattern_version": "burst-hot-shift.v1",
        "cycle_percent": 20,
        "hot_group_size": 2,
        "burst_size": 8,
        "burst_window_ns": 200_000_000,
    }
    return {"control": control, "burst": burst}


def prepare(output_root: Path, tokenizer_root: Path) -> None:
    workloads = _workloads(tokenizer_root)
    if not 1 <= min(THRESHOLDS) <= max(THRESHOLDS) <= 128:
        raise gpu.StudyError("threshold candidates exceed active router bound")
    output_root.mkdir(parents=True, exist_ok=False)
    entries: dict[str, dict[str, Any]] = {}
    phases = (
        ("calibration", (CALIBRATION_SEED,)),
        ("pilot", (PILOT_SEED,)),
        ("evaluation", EVALUATION_SEEDS),
    )
    for phase, seeds in phases:
        for pattern, workload in workloads.items():
            for seed in seeds:
                for rate in RATES:
                    name = _name(phase, pattern, seed, rate)
                    count = rate * DURATION_S
                    offers = capacity_trace(
                        seed, count, DURATION_S * 1_000_000_000, workload
                    )
                    lengths = capacity._tokenizer_lengths(offers, tokenizer_root)
                    # The capacity plan recipe accepts the versioned pattern
                    # while old plans without pattern_version remain identical.
                    plan = make_capacity_plan(
                        phase="pilot" if phase == "calibration" else phase,
                        seed=seed,
                        count=count,
                        duration_ns=DURATION_S * 1_000_000_000,
                        workload=workload,
                        prompt_tokens_by_index=lengths,
                    )
                    certificate = certify_capacity_plan(plan, tokenizer_root)
                    plan_file = f"{name}-plan.json"
                    cert_file = f"{name}-certificate.json"
                    entries[name] = {
                        "plan_file": plan_file,
                        "certificate_file": cert_file,
                        "plan_file_sha256": gpu._save(output_root / plan_file, plan),
                        "certificate_file_sha256": gpu._save(
                            output_root / cert_file, certificate
                        ),
                        "plan_sha256": plan["plan_sha256"],
                    }
    gpu._save(
        output_root / "manifest.json",
        {
            "schema": MANIFEST_SCHEMA,
            "patterns": workloads,
            "rates_rps": RATES,
            "duration_s": DURATION_S,
            "threshold_candidates_active": THRESHOLDS,
            "calibration_seed": CALIBRATION_SEED,
            "pilot_seed": PILOT_SEED,
            "evaluation_seeds": EVALUATION_SEEDS,
            "policy_orders": ORDERS,
            "router_max_active": 128,
            "router_max_queue": 256,
            "client_concurrency": 256,
            "harness_limits": {
                "scheduling_lag_p95_ns": capacity.HARNESS_LAG_LIMIT_NS,
                "client_queue_p95_ns": capacity.HARNESS_LAG_LIMIT_NS,
                "router_queue_p95_ms": capacity.ROUTER_QUEUE_LIMIT_MS,
            },
            "plans": entries,
        },
    )


def _prepared(
    root: Path,
) -> tuple[dict[str, Any], dict[str, tuple[dict[str, Any], dict[str, Any]]]]:
    manifest = json.loads((root / "manifest.json").read_text())
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or tuple(manifest.get("rates_rps", ())) != RATES
        or manifest.get("duration_s") != DURATION_S
        or tuple(manifest.get("threshold_candidates_active", ())) != THRESHOLDS
        or manifest.get("calibration_seed") != CALIBRATION_SEED
        or manifest.get("pilot_seed") != PILOT_SEED
        or tuple(manifest.get("evaluation_seeds", ())) != EVALUATION_SEEDS
        or tuple(tuple(order) for order in manifest.get("policy_orders", ())) != ORDERS
        or manifest.get("router_max_active") != 128
        or manifest.get("router_max_queue") != 256
        or manifest.get("client_concurrency") != 256
        or manifest.get("harness_limits")
        != {
            "scheduling_lag_p95_ns": capacity.HARNESS_LAG_LIMIT_NS,
            "client_queue_p95_ns": capacity.HARNESS_LAG_LIMIT_NS,
            "router_queue_p95_ms": capacity.ROUTER_QUEUE_LIMIT_MS,
        }
    ):
        raise gpu.StudyError("followup manifest is invalid")
    workloads = manifest["patterns"]
    if set(workloads) != set(PATTERNS):
        raise gpu.StudyError("followup pattern set is invalid")
    for pattern, workload in workloads.items():
        if workload["pattern_version"] != (
            "control.v1" if pattern == "control" else "burst-hot-shift.v1"
        ):
            raise gpu.StudyError("followup pattern version is invalid")
    expected = {
        _name(phase, pattern, seed, rate)
        for phase, seeds in (
            ("calibration", (CALIBRATION_SEED,)),
            ("pilot", (PILOT_SEED,)),
            ("evaluation", EVALUATION_SEEDS),
        )
        for pattern in PATTERNS
        for seed in seeds
        for rate in RATES
    }
    if set(manifest["plans"]) != expected:
        raise gpu.StudyError("followup plan set is incomplete")
    plans: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for name, entry in manifest["plans"].items():
        plan_file = root / entry["plan_file"]
        cert_file = root / entry["certificate_file"]
        if (
            entry["plan_file"] != f"{name}-plan.json"
            or entry["certificate_file"] != f"{name}-certificate.json"
            or gpu._file_digest(plan_file) != entry["plan_file_sha256"]
            or gpu._file_digest(cert_file) != entry["certificate_file_sha256"]
        ):
            raise gpu.StudyError(f"followup plan file binding failed: {name}")
        plan = json.loads(plan_file.read_text())
        cert = json.loads(cert_file.read_text())
        validate_capacity_plan(plan)
        validate_capacity_certificate(plan, cert)
        phase, pattern, seed_text, rate_text = name.split("-")
        if (
            plan["phase"] != ("pilot" if phase == "calibration" else phase)
            or plan["workload"] != workloads[pattern]
            or plan["seed"] != int(seed_text)
            or plan["offered_count"] != int(rate_text.removesuffix("rps")) * DURATION_S
            or plan["duration_ns"] != DURATION_S * 1_000_000_000
            or plan["max_client_concurrency"] != 256
            or plan["plan_sha256"] != entry["plan_sha256"]
        ):
            raise gpu.StudyError(f"followup plan content binding failed: {name}")
        plans[name] = plan, cert
    return manifest, plans


def _schedule(rates: tuple[int, int]) -> list[dict[str, Any]]:
    if (
        len(rates) != 2
        or any(rate not in RATES for rate in rates)
        or rates[0] >= rates[1]
    ):
        raise gpu.StudyError("two frozen increasing rates required")
    schedule: list[dict[str, Any]] = []
    for block, (seed, order) in enumerate(
        zip(EVALUATION_SEEDS, ORDERS, strict=True), 1
    ):
        patterns = PATTERNS if block % 2 else PATTERNS[::-1]
        block_rates = rates if block % 2 else rates[::-1]
        for pattern in patterns:
            for rate in block_rates:
                for position, policy in enumerate(order, 1):
                    schedule.append(
                        {
                            "block": block,
                            "seed": seed,
                            "pattern": pattern,
                            "rate_rps": rate,
                            "position": position,
                            "policy": policy,
                            "label": (
                                f"evaluation-b{block}-{pattern}-r{rate}"
                                f"-p{position}-{policy}"
                            ),
                        }
                    )
    return schedule


def _threshold_choice(items: list[dict[str, Any]]) -> int:
    if len(items) != len(PATTERNS) * len(THRESHOLDS):
        raise gpu.StudyError("threshold calibration population is incomplete")
    by_threshold: dict[int, dict[str, dict[str, Any]]] = {}
    for item in items:
        by_threshold.setdefault(item["saturation_active"], {})[item["pattern"]] = item
    if any(
        set(by_threshold.get(threshold, {})) != set(PATTERNS)
        for threshold in THRESHOLDS
    ):
        raise gpu.StudyError("threshold calibration cells are missing")
    return max(
        THRESHOLDS,
        key=lambda threshold: (
            min(
                by_threshold[threshold][pattern]["summary"]["all_offered"][
                    "slo_goodput_rps"
                ]
                for pattern in PATTERNS
            ),
            sum(
                by_threshold[threshold][pattern]["summary"]["all_offered"][
                    "slo_goodput_rps"
                ]
                for pattern in PATTERNS
            ),
            threshold,
        ),
    )


def _rate_choice(items: list[dict[str, Any]]) -> tuple[int, int]:
    observations = {(item["pattern"], item["rate_rps"]): item for item in items}
    if any(
        (pattern, rate) not in observations for pattern in PATTERNS for rate in (4, 5)
    ):
        raise gpu.StudyError("pilot lacks both patterns at 4 and 5 rps")
    if not all(
        observations[pattern, 4]["capacity_assessment"]["passed"]
        for pattern in PATTERNS
    ):
        raise gpu.StudyError("4 rps is not a valid passing common lower rate")
    if any(
        not observations[pattern, 5]["capacity_assessment"]["passed"]
        for pattern in PATTERNS
    ):
        return (4, 5)
    if any((pattern, 6) not in observations for pattern in PATTERNS):
        raise gpu.StudyError("pilot lacks 6 rps after both 5 rps conditions passed")
    return (5, 6)


async def _condition(
    session: aiohttp.ClientSession,
    out: Path,
    record: dict[str, Any],
    plans: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    *,
    phase: str,
    pattern: str,
    seed: int,
    rate: int,
    policy: str,
    label: str,
    saturation_active: int | None = None,
    block: int | None = None,
) -> dict[str, Any]:
    plan, cert = plans[_name(phase, pattern, seed, rate)]
    item = await capacity._run_condition(
        session,
        out,
        record,
        label=label,
        policy=policy,
        plan=plan,
        certificate=cert,
        router_max_active=128,
        router_max_queue=256,
        saturation_active=saturation_active,
        condition_metadata={
            "phase": phase,
            "pattern": pattern,
            "rate_rps": rate,
            "seed": seed,
            "block": block,
            "saturation_active": saturation_active,
        },
    )
    return item


def _valid_condition(item: dict[str, Any]) -> bool:
    return _condition_failure(item) is None


def _condition_failure(item: dict[str, Any]) -> str | None:
    assessment = item.get("capacity_assessment")
    if (
        item.get("engine_metrics_status") != "CAPTURED"
        or item.get("trial_status") != "COMPLETED"
        or item.get("comparison_valid") is not True
        or not isinstance(assessment, dict)
    ):
        return "INVALID_EVIDENCE"
    if assessment["generator_limited"] or assessment["admission_limited"]:
        return "HARNESS_LIMIT"
    return None


async def run(args: argparse.Namespace) -> None:
    budget = gpu._budget(args)
    out: Path = args.output_root
    out.mkdir(parents=True, exist_ok=False)
    try:
        manifest, plans = _prepared(args.prepared_dir)
        if args.image_reference != gpu.IMAGE:
            raise gpu.StudyError("outer image differs from pinned image")
        if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
            raise gpu.StudyError("source commit must be exact SHA-1")
        if not re.fullmatch(r"[0-9]+", args.instance_id):
            raise gpu.StudyError("provider instance ID must be numeric")
        snapshot = gpu._snapshot(args.model_dir)
        version = gpu._vllm_version(args.vllm_executable)
        flags = capacity._verify_cli_flags(args.vllm_executable)
        gpus = gpu._gpu_observation()
        if not gpu._ports_closed():
            raise gpu.StudyError("engine or router loopback port is occupied")
        budget.require(900)
        inputs = out / "prepared"
        inputs.mkdir()
        shutil.copyfile(args.prepared_dir / "manifest.json", inputs / "manifest.json")
        for entry in manifest["plans"].values():
            for key in ("plan_file", "certificate_file"):
                shutil.copyfile(args.prepared_dir / entry[key], inputs / entry[key])
    except Exception as error:
        gpu._save(
            out / "preflight-error.json",
            {"status": "FAILED_BEFORE_ENGINE_START", "reason": str(error)[:300]},
        )
        raise
    record: dict[str, Any] = {
        "schema": SESSION_SCHEMA,
        "status": "STARTING",
        "source_commit": args.source_commit,
        "provider_instance_id": args.instance_id,
        "image_reference_declared": gpu.IMAGE,
        "image_runtime_provenance": "OPERATOR_DECLARED_NOT_ATTESTED",
        "model_snapshot": snapshot,
        "vllm_version": version,
        "cli_flags": flags,
        "gpus": gpus,
        "prepared_manifest_sha256": gpu._file_digest(inputs / "manifest.json"),
        "hourly_rate_usd_declared": str(budget.hourly_rate),
        "cap_usd_declared": str(budget.cap),
        "reserve_usd_declared": str(budget.reserve),
        "billing_start_utc": budget.billing_start.isoformat(),
        "conditions": [],
        "error": None,
    }
    processes: list[subprocess.Popen[bytes]] = []
    logs: list[Any] = []
    try:
        processes, logs = gpu._start_engines(
            args.vllm_executable,
            args.model_dir,
            out,
            context_length=manifest["patterns"]["control"]["context_length"],
        )
        async with aiohttp.ClientSession(trust_env=False) as session:
            await gpu._ready(session, processes, budget)
            for port in gpu.ENGINE_PORTS:
                status, content = await gpu._get(
                    session, f"http://127.0.0.1:{port}/v1/models"
                )
                if status != 200 or QWEN3_8B_MODEL_ID not in content:
                    raise gpu.StudyError(f"engine {port} did not serve pinned model")
            record["cache_capacity"] = capacity._cache_capacity(
                [out / f"engine-{index}.log" for index in range(2)],
                manifest["patterns"]["control"],
            )
            gpu._save(out / "capacity-check.json", record["cache_capacity"])
            if not record["cache_capacity"]["candidate_valid"]:
                record["status"] = "CANDIDATE_INVALID"
                return

            calibration: list[dict[str, Any]] = []
            for threshold in THRESHOLDS:
                for pattern in PATTERNS:
                    budget.require(DURATION_S + 300)
                    label = f"calibration-{pattern}-t{threshold}-5rps"
                    item = await _condition(
                        session,
                        out,
                        record,
                        plans,
                        phase="calibration",
                        pattern=pattern,
                        seed=CALIBRATION_SEED,
                        rate=5,
                        policy="cache_saturation",
                        label=label,
                        saturation_active=threshold,
                    )
                    calibration.append(item)
                    if failure := _condition_failure(item):
                        record["status"] = failure
                        record["failed_phase"] = "calibration"
                        return
            selected = _threshold_choice(calibration)
            record["frozen_saturation_active"] = selected
            gpu._save(
                out / "threshold-freeze.json",
                {
                    "threshold_active": selected,
                    "rule_version": SATURATION_RULE_VERSION,
                    "selection": THRESHOLD_SELECTION_RULE,
                    "calibration_labels": [item["label"] for item in calibration],
                },
            )

            pilot: list[dict[str, Any]] = []
            for rate in RATES:
                for pattern in PATTERNS:
                    budget.require(DURATION_S + 300)
                    label = f"pilot-{pattern}-{rate}rps"
                    item = await _condition(
                        session,
                        out,
                        record,
                        plans,
                        phase="pilot",
                        pattern=pattern,
                        seed=PILOT_SEED,
                        rate=rate,
                        policy="cache_only",
                        label=label,
                    )
                    pilot.append(item)
                    if failure := _condition_failure(item):
                        record["status"] = failure
                        record["failed_phase"] = "pilot"
                        return
                if rate == 4 and any(
                    not item["capacity_assessment"]["passed"] for item in pilot
                ):
                    record["status"] = "INSUFFICIENT_PILOT"
                    return
                if rate == 5 and any(
                    not item["capacity_assessment"]["passed"]
                    for item in pilot
                    if item["rate_rps"] == 5
                ):
                    break
            try:
                rates = _rate_choice(pilot)
            except gpu.StudyError:
                record["status"] = "INSUFFICIENT_PILOT"
                return
            record["frozen_rates_rps"] = list(rates)
            gpu._save(
                out / "rate-freeze.json",
                {
                    "rates_rps": rates,
                    "rule": PILOT_SELECTION_RULE,
                    "pilot_labels": [item["label"] for item in pilot],
                },
            )
            schedule = _schedule(rates)
            record["evaluation_schedule"] = schedule
            record["evaluation_schedule_sha256"] = gpu._save(
                out / "evaluation-schedule.json",
                {"rates_rps": rates, "conditions": schedule},
            )
            gpu._save(out / "evaluation-freeze.json", record)
            for planned in schedule:
                budget.require(DURATION_S + 300)
                item = await _condition(
                    session,
                    out,
                    record,
                    plans,
                    phase="evaluation",
                    pattern=planned["pattern"],
                    seed=planned["seed"],
                    rate=planned["rate_rps"],
                    policy=planned["policy"],
                    label=planned["label"],
                    saturation_active=(
                        selected if planned["policy"] == "cache_saturation" else None
                    ),
                    block=planned["block"],
                )
                if failure := _condition_failure(item):
                    record["status"] = failure
                    record["failed_phase"] = "evaluation"
                    return
            record["status"] = "COMPLETED"
    except BaseException as error:
        record["status"] = (
            "INTERRUPTED"
            if isinstance(
                error, KeyboardInterrupt | asyncio.CancelledError | gpu.TrialInterrupted
            )
            else "FAILED"
        )
        record["error"] = f"{type(error).__name__}: {str(error)[:300]}"
        raise
    finally:
        cleanup_error: str | None = None
        try:
            gpu._stop_engines(processes, logs)
            if processes and not gpu._ports_closed():
                raise gpu.StudyError("owned engine or router port remains open")
        except Exception as error:
            cleanup_error = f"{type(error).__name__}: {str(error)[:300]}"
            record["status"] = "CLEANUP_UNCONFIRMED"
            record["cleanup_error"] = cleanup_error
        record["ended_utc"] = datetime.now(UTC).isoformat()
        record["engine_log_sha256"] = {
            str(index): gpu._file_digest(out / f"engine-{index}.log")
            for index in range(len(processes))
        }
        gpu._save(out / "session.json", record)
        if cleanup_error is not None:
            raise gpu.StudyError(f"owned process cleanup unconfirmed: {cleanup_error}")


def _verify_route_decisions(
    ledger: Path,
    policy: str,
    threshold: int | None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line]
    turns: set[int] = set()
    reasons = Counter[str]()
    for row in rows:
        if row.get("policy") != policy:
            raise gpu.StudyError("router ledger policy differs from condition")
        if row.get("saturation_threshold_active") != threshold or row.get(
            "saturation_rule_version"
        ) != (SATURATION_RULE_VERSION if threshold is not None else None):
            raise gpu.StudyError("router ledger threshold or rule version differs")
        turn = row.get("turn_at_decision")
        if turn is None:
            if row.get("replica") is not None or row.get("route_reason") is not None:
                raise gpu.StudyError("partial routing decision in ledger")
            continue
        busy = row.get("busy_at_decision")
        affinity = row.get("estimated_affinity")
        if (
            not isinstance(turn, int)
            or turn in turns
            or not isinstance(busy, list)
            or len(busy) != 2
            or not all(isinstance(value, int) and value >= 0 for value in busy)
            or not isinstance(affinity, list)
            or len(affinity) != 2
            or not all(value in (0, 1) for value in affinity)
        ):
            raise gpu.StudyError("router decision inputs are invalid")
        turns.add(turn)
        expected = choose(
            policy,
            (busy[0], busy[1]),
            turn,
            (affinity[0], affinity[1]),
            saturation_active=threshold,
        )
        if (
            row.get("replica") != expected.replica
            or row.get("route_reason") != expected.reason
            or row.get("preferred_replica") != expected.preferred_replica
            or row.get("saturation_reached") != expected.saturation_reached
            or row.get("relative_imbalance_reached")
            != expected.relative_imbalance_reached
            or row.get("escape_busy_delta") != ESCAPE_BUSY_DELTA
        ):
            raise gpu.StudyError("router decision does not follow frozen rule")
        reasons[expected.reason] += 1
    if turns != set(range(len(turns))):
        raise gpu.StudyError("router decision turns are not contiguous")
    if plan is not None and len(turns) == plan["offered_count"]:
        repeats = plan["workload"]["document_repeats"]
        digests = {
            index: hashlib.sha256(
                capacity_document(index, repeat).encode("utf-8")
            ).hexdigest()
            for index, repeat in enumerate(repeats)
        }
        offers = validate_capacity_plan(plan)
        if any(offer.document_id is None for offer in offers):
            raise gpu.StudyError("capacity plan has a missing document identity")
        expected_documents = Counter(
            digests[offer.document_id]
            for offer in offers
            if offer.document_id is not None
        )
        observed_documents = Counter(row.get("document_sha256") for row in rows)
        if observed_documents != expected_documents:
            raise gpu.StudyError("routed document population differs from plan")
    return {"decisions": len(turns), "reasons": dict(reasons)}


def report(raw_root: Path, output_root: Path) -> dict[str, Any]:
    record = json.loads((raw_root / "session.json").read_text())
    if record.get("schema") != SESSION_SCHEMA:
        raise gpu.StudyError("followup session schema is invalid")
    prepared = raw_root / "prepared"
    _manifest, plans = _prepared(prepared)
    if (
        gpu._file_digest(prepared / "manifest.json")
        != record["prepared_manifest_sha256"]
    ):
        raise gpu.StudyError("prepared manifest differs from session")
    frozen_threshold = record.get("frozen_saturation_active")
    frozen_rates = record.get("frozen_rates_rps")
    schedule = (
        _schedule(tuple(frozen_rates))
        if isinstance(frozen_rates, list) and len(frozen_rates) == 2
        else []
    )
    schedule_file = raw_root / "evaluation-schedule.json"
    schedule_valid = bool(
        schedule
        and record.get("evaluation_schedule") == schedule
        and schedule_file.is_file()
        and gpu._file_digest(schedule_file) == record.get("evaluation_schedule_sha256")
        and json.loads(schedule_file.read_text())
        == {"rates_rps": frozen_rates, "conditions": schedule}
    )
    by_label = {planned["label"]: planned for planned in schedule}
    values: list[dict[str, Any]] = []
    labels: list[str] = []
    for item in record["conditions"]:
        label = item["label"]
        if not isinstance(label, str) or not re.fullmatch(r"[a-z0-9_-]+", label):
            raise gpu.StudyError("unsafe followup condition label")
        if label in labels:
            raise gpu.StudyError("duplicate followup condition label")
        labels.append(label)
        phase = item["phase"]
        pattern = item["pattern"]
        rate = item["rate_rps"]
        seed = item["seed"]
        policy = item["policy"]
        threshold = item["saturation_active"]
        if (
            phase not in {"calibration", "pilot", "evaluation"}
            or pattern not in PATTERNS
            or rate not in RATES
            or policy not in FOLLOWUP_POLICIES
            or threshold != (None if policy != "cache_saturation" else threshold)
        ):
            raise gpu.StudyError("followup condition identity is invalid")
        if phase == "calibration":
            if (
                seed != CALIBRATION_SEED
                or rate != 5
                or policy != "cache_saturation"
                or threshold not in THRESHOLDS
                or label != f"calibration-{pattern}-t{threshold}-5rps"
            ):
                raise gpu.StudyError("calibration condition identity differs")
        elif phase == "pilot":
            if (
                seed != PILOT_SEED
                or policy != "cache_only"
                or label != f"pilot-{pattern}-{rate}rps"
            ):
                raise gpu.StudyError("pilot condition identity differs")
        else:
            planned = by_label.get(label)
            if (
                planned is None
                or any(
                    item.get(key) != planned[key]
                    for key in ("block", "seed", "pattern", "rate_rps", "policy")
                )
                or threshold
                != (frozen_threshold if policy == "cache_saturation" else None)
            ):
                raise gpu.StudyError(
                    "evaluation condition differs from frozen schedule"
                )
        plan, cert = plans[_name(phase, pattern, seed, rate)]
        client_path = raw_root / f"{label}-client.json"
        ledger_path = raw_root / f"{label}-router.jsonl"
        if (
            gpu._file_digest(client_path) != item["result_sha256"]
            or gpu._file_digest(ledger_path) != item["ledger_sha256"]
            or item["plan_sha256"] != plan["plan_sha256"]
        ):
            raise gpu.StudyError("followup raw artifact hash differs")
        client = json.loads(client_path.read_text())
        capacity._validate_client_population(client, plan, cert, item)
        after = client.get("router_stats_after", {})
        if client["comparison_valid"] and (
            after.get("saturation_active") != threshold
            or after.get("saturation_rule_version")
            != (SATURATION_RULE_VERSION if threshold is not None else None)
        ):
            raise gpu.StudyError("router stats threshold differs")
        route = _verify_route_decisions(ledger_path, policy, threshold, plan)
        diagnostics = capacity._ledger_diagnostics(ledger_path, plan)
        if item.get("capacity_diagnostics") not in (None, diagnostics):
            raise gpu.StudyError("followup ledger diagnostics differ")
        if item.get("capacity_assessment") is not None:
            expected_assessment = capacity._condition_assessment(
                {
                    **item,
                    "summary": client["summary"],
                    "capacity_diagnostics": diagnostics,
                },
                plan["offered_count"],
            )
            if item["capacity_assessment"] != expected_assessment:
                raise gpu.StudyError("followup assessment differs")
        if item.get("engine_metrics_status") == "CAPTURED":
            metrics = json.loads((raw_root / f"metrics-{label}.json").read_text())
            if metrics != item["engine_metrics"]:
                raise gpu.StudyError("followup engine metrics differ")
        reset_path = raw_root / f"reset-{label}.json"
        reset_valid = False
        if reset_path.is_file():
            reset = json.loads(reset_path.read_text())
            reset_valid = bool(
                reset.get("label") == label
                and len(reset.get("replicas", [])) == 2
                and all(row.get("reset_success") is True for row in reset["replicas"])
            )
        counters = capacity._counter_deltas(item)
        hits = (
            sum(row["hits"] for row in counters["replicas"])
            if counters["status"] == "VALID"
            else None
        )
        queries = (
            sum(row["queries"] for row in counters["replicas"])
            if counters["status"] == "VALID"
            else None
        )
        summary = client["summary"]["all_offered"]
        values.append(
            {
                "label": label,
                "phase": phase,
                "pattern": pattern,
                "block": item.get("block"),
                "rate_rps": rate,
                "policy": policy,
                "saturation_active": threshold,
                "offered": plan["offered_count"],
                "trial_status": item["trial_status"],
                "comparison_valid": item["comparison_valid"],
                "reset_valid": reset_valid,
                "assessment": item.get("capacity_assessment"),
                "engine_metrics_status": item.get("engine_metrics_status"),
                "cache_counter_status": counters["status"],
                "cache_counters": counters,
                "prefix_cache_hits": hits,
                "prefix_cache_queries": queries,
                "prefix_cache_hit_fraction": hits / queries
                if hits is not None and queries
                else None,
                "slo_goodput_rps": summary["slo_goodput_rps"],
                "slo_attainment": summary["slo_good"] / plan["offered_count"],
                "outcomes": summary["outcomes"],
                "first_content_p95_ms": capacity._millis(
                    summary["successful_only"]["scheduled_to_first_content"]["p95_ns"]
                ),
                "completion_p95_ms": capacity._millis(
                    summary["successful_only"]["scheduled_to_terminal"]["p95_ns"]
                ),
                "client_timing": client["summary"]["client_timing"],
                "router_queue_p95_ms": diagnostics["router_queue_p95_ms"],
                "replica_requests": diagnostics["replica_requests"],
                "affinity_states": diagnostics["affinity_states"],
                "route_reasons": route["reasons"],
                "router_outcomes": diagnostics["router_outcomes"],
                "routed_decisions": route["decisions"],
                "selected_replica_busy_p95": diagnostics["selected_replica_busy_p95"],
            }
        )
    calibration = [
        item for item in record["conditions"] if item["phase"] == "calibration"
    ]
    pilot = [item for item in record["conditions"] if item["phase"] == "pilot"]
    evaluation = [value for value in values if value["phase"] == "evaluation"]
    gpus = record.get("gpus")
    provenance_valid = bool(
        record.get("image_reference_declared") == gpu.IMAGE
        and record.get("model_snapshot", {}).get("sha256")
        == qwen3_expected_snapshot_sha256()
        and "0.26.0" in str(record.get("vllm_version", ""))
        and re.fullmatch(r"[0-9a-f]{40}", str(record.get("source_commit", "")))
        and isinstance(gpus, list)
        and len(gpus) == 2
        and gpus[0].get("model") == gpus[1].get("model")
        and gpus[0].get("model") in gpu.GPU_NAMES
        and record.get("cache_capacity", {}).get("candidate_valid") is True
    )
    calibration_labels = [
        f"calibration-{pattern}-t{threshold}-5rps"
        for threshold in THRESHOLDS
        for pattern in PATTERNS
    ]
    pilot_labels = [
        f"pilot-{pattern}-{rate}rps" for rate in RATES for pattern in PATTERNS
    ]
    threshold_valid = (
        bool(
            len(calibration) == len(calibration_labels)
            and labels[: len(calibration_labels)] == calibration_labels
            and all(_valid_condition(item) for item in calibration)
            and frozen_threshold == _threshold_choice(calibration)
            and (raw_root / "threshold-freeze.json").is_file()
            and json.loads((raw_root / "threshold-freeze.json").read_text())
            == {
                "threshold_active": frozen_threshold,
                "rule_version": SATURATION_RULE_VERSION,
                "selection": THRESHOLD_SELECTION_RULE,
                "calibration_labels": calibration_labels,
            }
        )
        if frozen_threshold is not None
        else False
    )
    rate_valid = False
    if pilot and all(_valid_condition(item) for item in pilot):
        try:
            computed_rates = _rate_choice(pilot)
        except gpu.StudyError:
            computed_rates = None
        rate_valid = (
            bool(
                computed_rates is not None
                and list(computed_rates) == frozen_rates
                and labels[
                    len(calibration_labels) : len(calibration_labels) + len(pilot)
                ]
                == pilot_labels[: len(pilot)]
                and (raw_root / "rate-freeze.json").is_file()
                and json.loads((raw_root / "rate-freeze.json").read_text())
                == {
                    "rates_rps": frozen_rates,
                    "rule": PILOT_SELECTION_RULE,
                    "pilot_labels": pilot_labels[: len(pilot)],
                }
            )
            if frozen_rates is not None
            else False
        )
    complete = bool(
        record["status"] == "COMPLETED"
        and provenance_valid
        and threshold_valid
        and rate_valid
        and schedule_valid
        and len(evaluation) == 64
        and labels
        == calibration_labels
        + pilot_labels[: len(pilot)]
        + [planned["label"] for planned in schedule]
        and all(
            value["comparison_valid"]
            and value["trial_status"] == "COMPLETED"
            and value["reset_valid"]
            and value["engine_metrics_status"] == "CAPTURED"
            and value["cache_counter_status"] == "VALID"
            and value["routed_decisions"] == value["offered"]
            and value["assessment"] is not None
            and not value["assessment"]["generator_limited"]
            and not value["assessment"]["admission_limited"]
            for value in values
        )
    )
    differences: list[dict[str, Any]] = []
    lookup = {
        (value["block"], value["pattern"], value["rate_rps"], value["policy"]): value
        for value in evaluation
    }
    for value in evaluation:
        baseline = lookup.get(
            (value["block"], value["pattern"], value["rate_rps"], "cache_only")
        )
        if baseline is not None:
            differences.append(
                {
                    "block": value["block"],
                    "pattern": value["pattern"],
                    "rate_rps": value["rate_rps"],
                    "policy": value["policy"],
                    "goodput_minus_cache_only_rps": value["slo_goodput_rps"]
                    - baseline["slo_goodput_rps"],
                    "first_content_p95_minus_cache_only_ms": (
                        value["first_content_p95_ms"] - baseline["first_content_p95_ms"]
                        if value["first_content_p95_ms"] is not None
                        and baseline["first_content_p95_ms"] is not None
                        else None
                    ),
                    "completion_p95_minus_cache_only_ms": (
                        value["completion_p95_ms"] - baseline["completion_p95_ms"]
                        if value["completion_p95_ms"] is not None
                        and baseline["completion_p95_ms"] is not None
                        else None
                    ),
                }
            )
    summaries: list[dict[str, Any]] = []
    for pattern in PATTERNS:
        for rate in sorted(set(frozen_rates or [])):
            for policy in FOLLOWUP_POLICIES:
                matched = [
                    value
                    for value in evaluation
                    if (value["pattern"], value["rate_rps"], value["policy"])
                    == (pattern, rate, policy)
                ]
                if matched:
                    hits = sum(
                        value["prefix_cache_hits"]
                        for value in matched
                        if value["prefix_cache_hits"] is not None
                    )
                    queries = sum(
                        value["prefix_cache_queries"]
                        for value in matched
                        if value["prefix_cache_queries"] is not None
                    )
                    summaries.append(
                        {
                            "pattern": pattern,
                            "rate_rps": rate,
                            "policy": policy,
                            "blocks": len(matched),
                            "mean_slo_goodput_rps": sum(
                                value["slo_goodput_rps"] for value in matched
                            )
                            / len(matched),
                            "passed_blocks": sum(
                                bool(
                                    value["assessment"]
                                    and value["assessment"]["passed"]
                                )
                                for value in matched
                            ),
                            "offered": sum(value["offered"] for value in matched),
                            "completed": sum(
                                value["outcomes"].get("completed", 0)
                                for value in matched
                            ),
                            "prefix_cache_hits": hits,
                            "prefix_cache_queries": queries,
                            "prefix_cache_hit_fraction": hits / queries
                            if queries
                            else None,
                        }
                    )
    inventory = {
        str(path.relative_to(raw_root)): gpu._file_digest(path)
        for path in sorted(raw_root.rglob("*"))
        if path.is_file()
    }
    result = {
        "schema": REPORT_SCHEMA,
        "status": "COMPLETE_DESCRIPTIVE" if complete else "INCOMPLETE",
        "session_status": record["status"],
        "session_error": record.get("error"),
        "raw_session_sha256": inventory["session.json"],
        "raw_inventory": inventory,
        "provenance_valid": provenance_valid,
        "frozen_saturation_active": frozen_threshold,
        "threshold_calibration_valid": threshold_valid,
        "frozen_rates_rps": frozen_rates,
        "pilot_selection_valid": rate_valid,
        "evaluation_schedule_valid": schedule_valid,
        "conditions": values,
        "block_differences": differences,
        "policy_pattern_rate_summary": summaries,
        "limitations": [
            "One host and four paired blocks support descriptive differences only.",
            "Estimated affinity is routing history, not verified KV residency.",
            "Active request count is not token-weighted engine load.",
            "Cache counters are aggregate; no per-request cache hit is inferred.",
            "Invalid evidence and harness limits are not policy losses.",
        ],
    }
    output_root.mkdir(parents=True, exist_ok=False)
    gpu._save(output_root / "report.json", result)
    lines = [
        "# vLLM affinity saturation follow-up",
        "",
        f"Status: **{result['status']}**; session: `{record['status']}`.",
        f"Frozen threshold: {frozen_threshold} active requests on preferred "
        f"replica; rates: {frozen_rates} rps.",
        "",
        "All-offered SLO goodput; cache fractions sum valid hit/query deltas.",
        "",
        "| Pattern | Rate | Policy | Blocks | Mean SLO goodput | Passed blocks | "
        "Completed/offered | Cache hit fraction |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for value in summaries:
        lines.append(
            f"| {value['pattern']} | {value['rate_rps']} | {value['policy']} | "
            f"{value['blocks']} | "
            f"{value['mean_slo_goodput_rps']:.3f} | {value['passed_blocks']} | "
            f"{value['completed']}/{value['offered']} | "
            f"{value['prefix_cache_hit_fraction']} |"
        )
    lines.extend(
        [
            "",
            "Per-block outcomes, latency, queueing, route reasons, cache "
            "counters, and matched differences are in report.json.",
            "A valid negative result remains a reported result; no "
            "production-wide or significance claim is made.",
        ]
    )
    (output_root / "report.md").write_text("\n".join(lines) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--tokenizer-root", type=Path, required=True)
    prep.add_argument("--output-root", type=Path, required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--prepared-dir", type=Path, required=True)
    run_parser.add_argument("--model-dir", type=Path, required=True)
    run_parser.add_argument("--vllm-executable", type=Path, required=True)
    run_parser.add_argument("--image-reference", required=True)
    run_parser.add_argument("--source-commit", required=True)
    run_parser.add_argument("--instance-id", required=True)
    run_parser.add_argument("--billing-start-utc", required=True)
    run_parser.add_argument("--hourly-rate-usd", required=True)
    run_parser.add_argument("--cap-usd", required=True)
    run_parser.add_argument("--reserve-usd", required=True)
    run_parser.add_argument("--output-root", type=Path, required=True)
    render = commands.add_parser("report")
    render.add_argument("--raw-root", type=Path, required=True)
    render.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args.output_root, args.tokenizer_root)
        elif args.command == "run":
            asyncio.run(run(args))
        else:
            report(args.raw_root, args.output_root)
    except (
        gpu.StudyError,
        AdapterError,
        OSError,
        ValueError,
        KeyError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"vllm-router-followup: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
