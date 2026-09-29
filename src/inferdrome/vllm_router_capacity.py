"""Versioned two-replica cache-pressure and serving-capacity study.

Preparation and reporting run without a GPU. Paid execution requires an
operator-supplied host and explicit billing envelope; this module owns local
vLLM process groups only. The v1 two-document experiment remains unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

from inferdrome import vllm_router_gpu as gpu
from inferdrome.evaluation.stream import StreamParser
from inferdrome.qwen3_campaign import QWEN3_8B_MODEL_ID
from inferdrome.vllm_affinity import HISTORY_TTL_NS
from inferdrome.vllm_router_study import (
    POLICIES,
    RequestResult,
    _digest,
    capacity_trace,
    capacity_workload,
    certify_capacity_plan,
    make_capacity_plan,
    summarize,
    validate_capacity_certificate,
    validate_capacity_plan,
)

MANIFEST_SCHEMA = "inferdrome.vllm-capacity-manifest.v2"
SESSION_SCHEMA = "inferdrome.vllm-capacity-session.v2"
REPORT_SCHEMA = "inferdrome.vllm-capacity-report.v2"
PILOT_SEED = 71
EVALUATION_SEEDS = (101, 103, 107, 109)
ORDERS = gpu.ORDERS
DEFAULT_RATES = (2, 4, 6, 8, 12, 16)
MAX_CONDITIONS = 64
HARNESS_LAG_LIMIT_NS = 50_000_000
ROUTER_QUEUE_LIMIT_MS = 50


def _rate_list(value: str) -> tuple[int, ...]:
    try:
        rates = tuple(int(part) for part in value.split(","))
    except ValueError as error:
        raise gpu.StudyError("rates must be comma-separated integers") from error
    if (
        not 3 <= len(rates) <= 8
        or any(not 1 <= rate <= 100 for rate in rates)
        or sorted(set(rates)) != list(rates)
    ):
        raise gpu.StudyError("rates must be 3-8 strictly increasing integers")
    return rates


def _plan_name(phase: str, seed: int, rate: int) -> str:
    return f"{phase}-{seed}-{rate}rps"


def _tokenizer_lengths(offers: tuple[Any, ...], tokenizer_root: Path) -> list[int]:
    from inferdrome.qwen3_campaign import qwen3_rendered_prompt
    from inferdrome.vllm_router_study import _capacity_tokenizer

    tokenizer = _capacity_tokenizer(tokenizer_root)
    return [
        len(
            tokenizer.encode(
                qwen3_rendered_prompt(offer.prompt), add_special_tokens=False
            ).ids
        )
        for offer in offers
    ]


def prepare(
    out: Path,
    tokenizer_root: Path,
    *,
    rates: tuple[int, ...] = DEFAULT_RATES,
    duration_s: int = 120,
    document_count: int = 48,
    target_prefix_tokens: int = 4096,
    context_length: int = 8192,
    client_concurrency: int = 256,
    router_max_active: int = 128,
    router_max_queue: int = 256,
) -> None:
    if (
        not 60 <= duration_s <= 300
        or not 1 <= client_concurrency <= 1024
        or not 1 <= router_max_active <= 512
        or not 0 <= router_max_queue <= 1024
        or max(rates) * duration_s > 20_000
        or 3 * 4 * len(EVALUATION_SEEDS) > MAX_CONDITIONS
    ):
        raise gpu.StudyError("capacity preparation exceeds bounded resource settings")
    workload = capacity_workload(
        tokenizer_root,
        document_count=document_count,
        target_prefix_tokens=target_prefix_tokens,
        context_length=context_length,
    )
    out.mkdir(parents=True, exist_ok=False)
    entries: dict[str, dict[str, Any]] = {}
    for phase, seeds in (("pilot", (PILOT_SEED,)), ("evaluation", EVALUATION_SEEDS)):
        for seed in seeds:
            for rate in rates:
                count = rate * duration_s
                name = _plan_name(phase, seed, rate)
                offers = capacity_trace(
                    seed, count, duration_s * 1_000_000_000, workload
                )
                lengths = _tokenizer_lengths(offers, tokenizer_root)
                plan = make_capacity_plan(
                    phase=phase,
                    seed=seed,
                    count=count,
                    duration_ns=duration_s * 1_000_000_000,
                    workload=workload,
                    prompt_tokens_by_index=lengths,
                    max_client_concurrency=client_concurrency,
                )
                cert = certify_capacity_plan(plan, tokenizer_root)
                plan_file = f"{name}-plan.json"
                cert_file = f"{name}-certificate.json"
                entries[name] = {
                    "plan_file": plan_file,
                    "certificate_file": cert_file,
                    "plan_file_sha256": gpu._save(out / plan_file, plan),
                    "certificate_file_sha256": gpu._save(out / cert_file, cert),
                    "plan_sha256": plan["plan_sha256"],
                }
    gpu._save(
        out / "manifest.json",
        {
            "schema": MANIFEST_SCHEMA,
            "rates_rps": rates,
            "duration_s": duration_s,
            "workload": workload,
            "client_concurrency": client_concurrency,
            "router_max_active": router_max_active,
            "router_max_queue": router_max_queue,
            "harness_limits": {
                "scheduling_lag_p95_ns": HARNESS_LAG_LIMIT_NS,
                "client_queue_p95_ns": HARNESS_LAG_LIMIT_NS,
                "router_queue_p95_ms": ROUTER_QUEUE_LIMIT_MS,
            },
            "pilot_seed": PILOT_SEED,
            "evaluation_seeds": EVALUATION_SEEDS,
            "policy_orders": ORDERS,
            "plans": entries,
        },
    )


def _prepared(
    root: Path,
) -> tuple[dict[str, Any], dict[str, tuple[dict[str, Any], dict[str, Any]]]]:
    manifest = json.loads((root / "manifest.json").read_text())
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or tuple(manifest.get("rates_rps", ()))
        != _rate_list(",".join(map(str, manifest.get("rates_rps", ()))))
        or manifest.get("pilot_seed") != PILOT_SEED
        or tuple(manifest.get("evaluation_seeds", ())) != EVALUATION_SEEDS
        or tuple(tuple(row) for row in manifest.get("policy_orders", ())) != ORDERS
        or manifest.get("harness_limits")
        != {
            "scheduling_lag_p95_ns": HARNESS_LAG_LIMIT_NS,
            "client_queue_p95_ns": HARNESS_LAG_LIMIT_NS,
            "router_queue_p95_ms": ROUTER_QUEUE_LIMIT_MS,
        }
    ):
        raise gpu.StudyError("capacity manifest is invalid")
    expected_names = {
        _plan_name(phase, seed, rate)
        for phase, seeds in (("pilot", (PILOT_SEED,)), ("evaluation", EVALUATION_SEEDS))
        for seed in seeds
        for rate in manifest["rates_rps"]
    }
    if set(manifest["plans"]) != expected_names:
        raise gpu.StudyError("capacity manifest has missing or extra plans")
    result: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for name, entry in manifest["plans"].items():
        if not re.fullmatch(r"(?:pilot|evaluation)-[0-9]+-[0-9]+rps", name):
            raise gpu.StudyError("unsafe capacity plan name")
        plan_path = root / entry["plan_file"]
        cert_path = root / entry["certificate_file"]
        if (
            plan_path.name != f"{name}-plan.json"
            or cert_path.name != f"{name}-certificate.json"
            or gpu._file_digest(plan_path) != entry["plan_file_sha256"]
            or gpu._file_digest(cert_path) != entry["certificate_file_sha256"]
        ):
            raise gpu.StudyError(f"capacity plan artifact mismatch: {name}")
        plan = json.loads(plan_path.read_text())
        cert = json.loads(cert_path.read_text())
        validate_capacity_plan(plan)
        validate_capacity_certificate(plan, cert)
        phase, seed_text, rate_text = name.split("-")
        rate = int(rate_text.removesuffix("rps"))
        if (
            plan["plan_sha256"] != entry["plan_sha256"]
            or plan["phase"] != phase
            or plan["seed"] != int(seed_text)
            or plan["offered_count"] != rate * manifest["duration_s"]
            or plan["duration_ns"] != manifest["duration_s"] * 1_000_000_000
            or plan["workload"] != manifest["workload"]
            or plan["max_client_concurrency"] != manifest["client_concurrency"]
        ):
            raise gpu.StudyError(f"capacity plan binding mismatch: {name}")
        result[name] = plan, cert
    return manifest, result


def _percentile(values: list[float], fraction: float = 0.95) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5))]


def _millis(value: int | None) -> float | None:
    return value / 1_000_000 if value is not None else None


def _ledger_diagnostics(path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    offers = capacity_trace(
        plan["seed"], plan["offered_count"], plan["duration_ns"], plan["workload"]
    )
    last_seen: dict[int, int] = {}
    gaps_ms: list[float] = []
    for offer in offers:
        assert offer.document_id is not None
        if offer.document_id in last_seen:
            gaps_ms.append(
                (offer.scheduled_ns - last_seen[offer.document_id]) / 1_000_000
            )
        last_seen[offer.document_id] = offer.scheduled_ns
    affinity = Counter[str]()
    replica = Counter[str]()
    reasons = Counter[str]()
    outcomes = Counter[str]()
    queue_ms: list[float] = []
    selected_busy: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        outcome = str(row["outcome"])
        outcomes[outcome] += 1
        if row.get("replica") is not None:
            replica[str(row["replica"])] += 1
            busy = row.get("busy_at_decision")
            if isinstance(busy, list) and len(busy) == 2:
                selected_busy[str(row["replica"])].append(float(busy[row["replica"]]))
        if row.get("route_reason") is not None:
            reasons[str(row["route_reason"])] += 1
        scores = row.get("estimated_affinity")
        if scores is None:
            affinity["unavailable"] += 1
        elif scores == [0, 0]:
            affinity["neither"] += 1
        elif scores == [1, 1]:
            affinity["tie"] += 1
        else:
            affinity["exclusive"] += 1
        if row.get("queued_ms") is not None:
            queue_ms.append(float(row["queued_ms"]))
    return {
        "router_ledger_rows": len(rows),
        "router_outcomes": dict(outcomes),
        "replica_requests": dict(replica),
        "route_reasons": dict(reasons),
        "affinity_states": dict(affinity),
        "affinity_state_fractions": {
            key: affinity.get(key, 0) / len(rows) if rows else None
            for key in ("neither", "exclusive", "tie", "unavailable")
        },
        "selected_replica_busy_p95": {
            key: _percentile(values) for key, values in selected_busy.items()
        },
        "router_queue_p95_ms": _percentile(queue_ms),
        "router_queue_observed": len(queue_ms),
        "document_revisit_gap_p95_ms": _percentile(gaps_ms),
        "document_revisit_over_ttl": sum(
            gap * 1_000_000 > HISTORY_TTL_NS for gap in gaps_ms
        ),
        "document_revisit_observed": len(gaps_ms),
        "document_revisit_over_ttl_fraction": (
            sum(gap * 1_000_000 > HISTORY_TTL_NS for gap in gaps_ms) / len(gaps_ms)
            if gaps_ms
            else None
        ),
    }


def _condition_assessment(item: dict[str, Any], count: int) -> dict[str, Any]:
    summary = item["summary"]
    all_offered = summary["all_offered"]
    outcomes = all_offered["outcomes"]
    timing = summary["client_timing"]
    lag = timing["scheduling_lag"]["p95_ns"]
    client_queue = timing["client_queue_delay"]["p95_ns"]
    ledger = item["capacity_diagnostics"]
    generator_limited = (
        lag is None
        or lag > HARNESS_LAG_LIMIT_NS
        or client_queue is None
        or client_queue > HARNESS_LAG_LIMIT_NS
        or timing["never_dispatched"] > 0
        or ledger["router_ledger_rows"] != count
    )
    admission_limited = (
        any(
            ledger["router_outcomes"].get(outcome, 0) > 0
            for outcome in ("rejected_capacity", "rejected_queue_timeout")
        )
        or ledger["router_queue_observed"] != count
        or ledger["router_queue_p95_ms"] is None
        or ledger["router_queue_p95_ms"] > ROUTER_QUEUE_LIMIT_MS
    )
    first_p95 = all_offered["successful_only"]["scheduled_to_first_content"]["p95_ns"]
    completion_p95 = all_offered["successful_only"]["scheduled_to_terminal"]["p95_ns"]
    rejected = sum(
        value for outcome, value in outcomes.items() if outcome.startswith("reject")
    )
    passed = (
        item["comparison_valid"]
        and item["trial_status"] == "COMPLETED"
        and item["engine_metrics_status"] == "CAPTURED"
        and not generator_limited
        and not admission_limited
        and all_offered["slo_good"] / count >= 0.95
        and outcomes.get("completed", 0) / count >= 0.95
        and rejected / count <= 0.01
        and first_p95 is not None
        and first_p95 <= 500_000_000
        and completion_p95 is not None
        and completion_p95 <= 5_000_000_000
    )
    return {
        "passed": passed,
        "generator_limited": generator_limited,
        "admission_limited": admission_limited,
        "slo_attainment": all_offered["slo_good"] / count,
        "completed_fraction": outcomes.get("completed", 0) / count,
        "rejected_fraction": rejected / count,
        "first_content_p95_ns": first_p95,
        "completion_p95_ns": completion_p95,
        "rule": (
            "95% SLO-good and completed; <=1% rejected; completed-only "
            "p95 first <=500ms and terminal <=5s; scheduling/client queue "
            "p95 <=50ms, router queue p95 <=50ms, no admission rejects"
        ),
    }


def _pilot_boundary(
    items: list[dict[str, Any]], rates: tuple[int, ...]
) -> dict[str, Any]:
    observed = [item["capacity_assessment"] for item in items]
    if any(x["generator_limited"] or x["admission_limited"] for x in observed):
        return {"status": "HARNESS_LIMIT", "rates_rps": []}
    failed_at = next(
        (index for index, x in enumerate(observed) if not x["passed"]), None
    )
    if failed_at is None:
        return {"status": "UNBRACKETED", "rates_rps": []}
    if failed_at < 2:
        return {"status": "INSUFFICIENT_BELOW", "rates_rps": []}
    return {
        "status": "BRACKETED",
        "rates_rps": list(rates[failed_at - 2 : failed_at + 1]),
        "first_failed_rps": rates[failed_at],
    }


def _counter_deltas(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("engine_metrics_status") != "CAPTURED":
        return {"status": "MISSING", "replicas": []}
    replicas: list[dict[str, Any]] = []
    for replica in item.get("engine_metrics", {}).get("replicas", []):
        deltas = replica.get("prefix_counter_deltas", {})
        if not isinstance(deltas, dict):
            deltas = {}
        hits = deltas.get("vllm:prefix_cache_hits_total")
        queries = deltas.get("vllm:prefix_cache_queries_total")
        valid = (
            isinstance(hits, int | float)
            and isinstance(queries, int | float)
            and math.isfinite(hits)
            and math.isfinite(queries)
            and 0 <= hits <= queries
            and queries > 0
        )
        replicas.append(
            {
                "port": replica.get("port"),
                "hits": hits,
                "queries": queries,
                "hit_fraction": hits / queries if valid and queries else None,
                "valid": valid,
            }
        )
    return {
        "status": (
            "VALID"
            if len(replicas) == 2
            and {r["port"] for r in replicas} == set(gpu.ENGINE_PORTS)
            and all(r["valid"] for r in replicas)
            else "INVALID_OR_MISSING"
        ),
        "replicas": replicas,
    }


def _validate_client_population(
    client: dict[str, Any],
    plan: dict[str, Any],
    cert: dict[str, Any],
    item: dict[str, Any],
) -> None:
    """Recompute the client evidence from every offered request."""
    count = plan["offered_count"]
    if (
        client.get("schema") != "inferdrome.vllm-router-capacity-result.v2"
        or client.get("plan_sha256") != plan["plan_sha256"]
        or client.get("trace_sha256") != plan["trace_sha256"]
        or client.get("token_certificate_sha256") != cert["certificate_sha256"]
        or client.get("policy") != item["policy"]
        or client.get("status") != item["trial_status"]
        or client.get("comparison_valid") != item["comparison_valid"]
        or client.get("result_sha256")
        != _digest({k: v for k, v in client.items() if k != "result_sha256"})
    ):
        raise gpu.StudyError("capacity client identity or digest mismatch")
    raw_rows = client.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) != count:
        raise gpu.StudyError("capacity client population size mismatch")
    allowed_fields = {field.name for field in fields(RequestResult)}
    offers = validate_capacity_plan(plan)
    rows: list[RequestResult] = []
    for index, (raw, offer) in enumerate(zip(raw_rows, offers, strict=True)):
        if not isinstance(raw, dict) or set(raw) != allowed_fields:
            raise gpu.StudyError("capacity client row fields mismatch")
        try:
            row = RequestResult(**raw)
        except TypeError as error:
            raise gpu.StudyError("capacity client row shape mismatch") from error
        if (
            row.index != index
            or row.scheduled_ns != offer.scheduled_ns
            or row.epoch != offer.epoch
            or row.traffic_class != offer.traffic_class
            or row.tenant != offer.tenant
            or row.document_id != offer.document_id
            or not isinstance(row.terminal_ns, int)
            or row.terminal_ns < 0
        ):
            raise gpu.StudyError("capacity client row trace mismatch")
        stamps = [
            row.ready_ns,
            row.dispatch_ns,
            row.response_headers_ns,
            row.first_body_byte_ns,
            row.first_content_ns,
            row.terminal_ns,
        ]
        if any(
            value is not None and (not isinstance(value, int) or value < 0)
            for value in stamps
        ):
            raise gpu.StudyError("capacity client row timing invalid")
        observed = [value for value in stamps if value is not None]
        if observed != sorted(observed):
            raise gpu.StudyError("capacity client row timing order invalid")
        if row.outcome == "completed" and (
            row.http_status != 200
            or row.first_content_ns is None
            or row.terminal_ns < row.scheduled_ns
            or row.prompt_tokens != plan["prompt_tokens_by_index"][index]
            or row.completion_tokens != plan["max_tokens"]
        ):
            raise gpu.StudyError("capacity completed row token or HTTP mismatch")
        rows.append(row)
    computed = summarize(plan, rows)
    if client.get("summary") != computed or item.get("summary") != computed:
        raise gpu.StudyError("capacity client summary mismatch")
    if client["comparison_valid"]:
        after = client.get("router_stats_after")
        if (
            not isinstance(after, dict)
            or not client.get("router_accounting_valid")
            or (
                after.get("policy") != item["policy"]
                or after.get("offered") != count
                or after.get("terminal") != count
                or after.get("ledger_rows") != count
                or after.get("in_flight") != 0
                or after.get("pending") != 0
                or after.get("active") != 0
                or after.get("busy") != [0, 0]
                or after.get("accounting_failed") is not False
            )
        ):
            raise gpu.StudyError("capacity router accounting mismatch")


def report(raw_root: Path, output_root: Path) -> None:
    session_path = raw_root / "session.json"
    record = json.loads(session_path.read_text())
    if record.get("schema") != SESSION_SCHEMA:
        raise gpu.StudyError("capacity session schema is invalid")
    inputs = raw_root / "prepared"
    manifest, plans = _prepared(inputs)
    if gpu._file_digest(inputs / "manifest.json") != record["prepared_manifest_sha256"]:
        raise gpu.StudyError("prepared manifest digest differs from session")
    conditions = record["conditions"]
    labels = [item["label"] for item in conditions]
    if len(labels) != len(set(labels)):
        raise gpu.StudyError("duplicate capacity condition identity")
    scheduled = record.get("evaluation_schedule")
    boundary = record.get("pilot_boundary")
    frozen_rates = boundary.get("rates_rps", []) if isinstance(boundary, dict) else []
    canonical_schedule = _schedule(frozen_rates) if len(frozen_rates) == 3 else None
    schedule_path = raw_root / "evaluation-schedule.json"
    schedule_valid = bool(
        canonical_schedule is not None
        and scheduled == canonical_schedule
        and schedule_path.is_file()
        and gpu._file_digest(schedule_path) == record.get("evaluation_schedule_sha256")
        and json.loads(schedule_path.read_text())
        == {"rates_rps": frozen_rates, "conditions": canonical_schedule}
    )
    by_label = {item["label"]: item for item in canonical_schedule or []}
    values: list[dict[str, Any]] = []
    for item in conditions:
        label = item["label"]
        if not isinstance(label, str) or not re.fullmatch(r"[a-z0-9_-]+", label):
            raise gpu.StudyError("unsafe capacity condition label")
        if label.startswith("pilot-"):
            name = label
            rate = int(label.rsplit("-", 1)[1][:-3])
            phase = "pilot"
            block = None
            if item["policy"] != "least_busy":
                raise gpu.StudyError("pilot policy identity mismatch")
        elif label in by_label:
            planned = by_label[label]
            name = _plan_name("evaluation", planned["seed"], planned["rate_rps"])
            rate = planned["rate_rps"]
            phase = "evaluation"
            block = planned["block"]
            if item["policy"] != planned["policy"]:
                raise gpu.StudyError("evaluation policy identity mismatch")
        else:
            raise gpu.StudyError(f"unknown capacity condition identity: {label}")
        plan, cert = plans[name]
        client_path = raw_root / f"{label}-client.json"
        ledger_path = raw_root / f"{label}-router.jsonl"
        if (
            gpu._file_digest(client_path) != item["result_sha256"]
            or gpu._file_digest(ledger_path) != item["ledger_sha256"]
        ):
            raise gpu.StudyError(f"capacity raw artifact hash mismatch: {label}")
        client = json.loads(client_path.read_text())
        _validate_client_population(client, plan, cert, item)
        diagnostics = _ledger_diagnostics(ledger_path, plan)
        if item.get("capacity_diagnostics") not in (None, diagnostics):
            raise gpu.StudyError(f"capacity ledger diagnostics mismatch: {label}")
        if item.get("capacity_assessment") is not None:
            expected_assessment = _condition_assessment(
                {
                    **item,
                    "summary": client["summary"],
                    "capacity_diagnostics": diagnostics,
                },
                plan["offered_count"],
            )
            if item["capacity_assessment"] != expected_assessment:
                raise gpu.StudyError(f"capacity assessment mismatch: {label}")
        if item.get("engine_metrics_status") == "CAPTURED":
            metric_path = raw_root / f"metrics-{label}.json"
            if json.loads(metric_path.read_text()) != item["engine_metrics"]:
                raise gpu.StudyError(f"engine metrics mismatch: {label}")
        reset_path = raw_root / f"reset-{label}.json"
        reset_valid = False
        if reset_path.is_file():
            reset = json.loads(reset_path.read_text())
            reset_valid = (
                reset.get("label") == label
                and len(reset.get("replicas", [])) == 2
                and all(
                    replica.get("reset_success") is True
                    for replica in reset["replicas"]
                )
            )
        summary = client["summary"]
        all_offered = summary["all_offered"]
        timing = summary["client_timing"]
        values.append(
            {
                "label": label,
                "phase": phase,
                "block": block,
                "policy": item["policy"],
                "rate_rps": rate,
                "offered": plan["offered_count"],
                "trial_status": item["trial_status"],
                "valid": item["comparison_valid"],
                "reset_valid": reset_valid,
                "engine_metrics_status": item.get("engine_metrics_status"),
                "analysis_error": item.get("analysis_error"),
                "assessment": item.get("capacity_assessment"),
                "slo_goodput_rps": all_offered["slo_goodput_rps"],
                "slo_attainment": all_offered["slo_good"] / plan["offered_count"],
                "outcomes": all_offered["outcomes"],
                "first_content_p95_ms": _millis(
                    all_offered["successful_only"]["scheduled_to_first_content"][
                        "p95_ns"
                    ]
                ),
                "completion_p95_ms": _millis(
                    all_offered["successful_only"]["scheduled_to_terminal"]["p95_ns"]
                ),
                "scheduling_lag_p95_ms": _millis(timing["scheduling_lag"]["p95_ns"]),
                "client_queue_p95_ms": _millis(timing["client_queue_delay"]["p95_ns"]),
                "router_queue_p95_ms": diagnostics["router_queue_p95_ms"],
                "router_diagnostics": diagnostics,
                "cache_counters": _counter_deltas(item),
                "goodput_denominator_ns": all_offered["goodput_denominator_ns"],
            }
        )
    inventory = {
        str(path.relative_to(raw_root)): gpu._file_digest(path)
        for path in sorted(raw_root.rglob("*"))
        if path.is_file()
    }
    pilot_count = sum(value["phase"] == "pilot" for value in values)
    expected_pilot = [
        _plan_name("pilot", PILOT_SEED, rate)
        for rate in manifest["rates_rps"][:pilot_count]
    ]
    evaluation = [value for value in values if value["phase"] == "evaluation"]
    pilot_items = conditions[:pilot_count]
    boundary_valid = (
        isinstance(boundary, dict)
        and all(item.get("capacity_assessment") is not None for item in pilot_items)
        and boundary == _pilot_boundary(pilot_items, tuple(manifest["rates_rps"]))
    )
    complete = (
        record["status"] == "COMPLETED"
        and boundary_valid
        and boundary["status"] == "BRACKETED"
        and schedule_valid
        and labels
        == expected_pilot + [item["label"] for item in canonical_schedule or []]
        and len(evaluation) == 48
        and all(
            value["valid"]
            and value["reset_valid"]
            and value["trial_status"] == "COMPLETED"
            and value["engine_metrics_status"] == "CAPTURED"
            and value["cache_counters"]["status"] == "VALID"
            and value["assessment"] is not None
            and not value["assessment"]["generator_limited"]
            and not value["assessment"]["admission_limited"]
            for value in values
        )
    )
    status = (
        "COMPLETE_DESCRIPTIVE"
        if complete
        else "UNBRACKETED"
        if record["status"] == "UNBRACKETED"
        and boundary_valid
        and labels == expected_pilot
        and len(values) == len(manifest["rates_rps"])
        and all(
            value["assessment"]
            and value["assessment"]["passed"]
            and value["reset_valid"]
            and value["cache_counters"]["status"] == "VALID"
            for value in values
        )
        else "INCOMPLETE"
    )
    block_differences: list[dict[str, Any]] = []
    lookup = {
        (value["block"], value["rate_rps"], value["policy"]): value
        for value in evaluation
    }
    for value in evaluation:
        baseline = lookup.get((value["block"], value["rate_rps"], "least_busy"))
        if baseline is not None:
            block_differences.append(
                {
                    "block": value["block"],
                    "rate_rps": value["rate_rps"],
                    "policy": value["policy"],
                    "goodput_minus_least_busy_rps": value["slo_goodput_rps"]
                    - baseline["slo_goodput_rps"],
                    "first_content_p95_minus_least_busy_ms": (
                        value["first_content_p95_ms"] - baseline["first_content_p95_ms"]
                        if value["first_content_p95_ms"] is not None
                        and baseline["first_content_p95_ms"] is not None
                        else None
                    ),
                    "completion_p95_minus_least_busy_ms": (
                        value["completion_p95_ms"] - baseline["completion_p95_ms"]
                        if value["completion_p95_ms"] is not None
                        and baseline["completion_p95_ms"] is not None
                        else None
                    ),
                }
            )
    policy_rate_summary: list[dict[str, Any]] = []
    for rate in sorted({value["rate_rps"] for value in evaluation}):
        for policy in POLICIES:
            rows = [
                value
                for value in evaluation
                if value["rate_rps"] == rate and value["policy"] == policy
            ]
            differences = [
                row["goodput_minus_least_busy_rps"]
                for row in block_differences
                if row["rate_rps"] == rate and row["policy"] == policy
            ]
            if rows:
                policy_rate_summary.append(
                    {
                        "rate_rps": rate,
                        "policy": policy,
                        "blocks": len(rows),
                        "goodput_mean_rps": sum(row["slo_goodput_rps"] for row in rows)
                        / len(rows),
                        "goodput_range_rps": [
                            min(row["slo_goodput_rps"] for row in rows),
                            max(row["slo_goodput_rps"] for row in rows),
                        ],
                        "difference_vs_least_busy": (
                            "TIE"
                            if differences and all(value == 0 for value in differences)
                            else "HIGHER_IN_ALL_BLOCKS"
                            if differences and all(value > 0 for value in differences)
                            else "LOWER_IN_ALL_BLOCKS"
                            if differences and all(value < 0 for value in differences)
                            else "MIXED_OR_INCOMPLETE"
                        ),
                    }
                )
    output_root.mkdir(parents=True, exist_ok=False)
    result = {
        "schema": REPORT_SCHEMA,
        "status": status,
        "session_status": record["status"],
        "session_error": record.get("error"),
        "raw_session_sha256": inventory["session.json"],
        "raw_inventory": inventory,
        "cache_capacity": record.get("cache_capacity"),
        "pilot_boundary": boundary,
        "pilot_boundary_valid": boundary_valid,
        "evaluation_schedule_valid": schedule_valid,
        "conditions": values,
        "block_differences": block_differences,
        "policy_rate_summary": policy_rate_summary,
        "limitations": [
            "One host and four matched blocks support descriptive differences only.",
            "Correlated request rows are not independent experimental repetitions.",
            "Affinity history is binary with 60s TTL; it does not prove KV residency.",
            "A cache counter difference alone is not causal attribution.",
        ],
    }
    gpu._save(output_root / "report.json", result)
    lines = [
        "# vLLM router capacity and cache-pressure study",
        "",
        f"Status: **{status}**; session: `{record['status']}`. "
        f"Error: `{record.get('error')}`.",
        "",
        "SLO goodput counts all offered requests over each scheduled window. "
        "First-content and completion p95 describe completed requests only.",
        "",
        "| Block | Rate | Policy | Goodput | Attainment | First p95 ms | "
        "Completion p95 ms | Schedule lag p95 ms | Client queue p95 ms | "
        "Router queue p95 ms | Metrics | Harness | Outcomes |",
        "| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | "
        "---: | ---: | --- | --- | --- |",
    ]
    for value in evaluation:
        outcomes = ", ".join(
            f"{key}={count}" for key, count in sorted(value["outcomes"].items())
        )
        assessment = value["assessment"]
        harness = (
            f"{assessment['generator_limited']}/{assessment['admission_limited']}"
            if assessment
            else "MISSING"
        )
        lines.append(
            f"| {value['block']} | {value['rate_rps']} | {value['policy']} | "
            f"{value['slo_goodput_rps']:.3f} | {value['slo_attainment']:.3f} | "
            f"{value['first_content_p95_ms']} | "
            f"{value['completion_p95_ms']} | "
            f"{value['scheduling_lag_p95_ms']} | "
            f"{value['client_queue_p95_ms']} | "
            f"{value['router_queue_p95_ms']} | "
            f"{value['cache_counters']['status']} | "
            f"{harness} | {outcomes} |"
        )
    lines.extend(
        [
            "",
            "| Rate | Policy | Blocks | Mean goodput | Block range | "
            "Difference from least_busy |",
            "| ---: | --- | ---: | ---: | --- | --- |",
        ]
    )
    for row in policy_rate_summary:
        lines.append(
            f"| {row['rate_rps']} | {row['policy']} | {row['blocks']} | "
            f"{row['goodput_mean_rps']:.3f} | {row['goodput_range_rps']} | "
            f"{row['difference_vs_least_busy']} |"
        )
    lines.extend(
        [
            "",
            "Per-block differences from least_busy, replica distribution, routing "
            "reasons, affinity states, revisit gaps, and per-replica cache counter "
            "deltas are in report.json. Missing or invalid metrics keep the report "
            "incomplete. No significance or production-wide gain is claimed.",
        ]
    )
    (output_root / "report.md").write_text("\n".join(lines) + "\n")
    rates = sorted({value["rate_rps"] for value in evaluation})
    colors = {
        "round_robin": "#285ea8",
        "least_busy": "#26734d",
        "cache_only": "#ae6b1a",
        "cache_plus_load": "#8a398e",
    }
    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="400">',
        '<rect width="720" height="400" fill="white"/>',
        '<text x="20" y="28" font-size="17">SLO goodput vs offered rate</text>',
        '<text x="20" y="385" font-size="12">'
        "Offered requests/s; four block means</text>",
    ]
    if rates:
        max_axis = max(rates) * 1.1
        for index, policy in enumerate(colors):
            points = []
            for rate in rates:
                observations = [
                    value["slo_goodput_rps"]
                    for value in evaluation
                    if value["rate_rps"] == rate and value["policy"] == policy
                ]
                if observations:
                    x = 65 + 560 * rate / max_axis
                    y = 340 - 280 * (sum(observations) / len(observations)) / max_axis
                    points.append(f"{x:.1f},{y:.1f}")
            svg.append(
                f'<polyline points="{" ".join(points)}" fill="none" '
                f'stroke="{colors[policy]}" stroke-width="2"/>'
            )
            svg.append(
                f'<text x="515" y="{48 + 17 * index}" font-size="11" '
                f'fill="{colors[policy]}">{policy}</text>'
            )
    svg.append("</svg>")
    (output_root / "goodput-vs-rate.svg").write_text("\n".join(svg) + "\n")


async def _diagnostic_stream(
    session: aiohttp.ClientSession, prompt: str, expected_prompt_tokens: int
) -> dict[str, Any]:
    body = {
        "model": QWEN3_8B_MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 128,
        "temperature": 0,
        "n": 1,
        "stream": True,
        "stream_options": {"include_usage": True},
        "ignore_eos": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    start = time.monotonic_ns()
    parser = StreamParser(
        max_stream_bytes=16_777_216, max_event_bytes=65_536, max_content_events=4096
    )
    async with session.post(
        f"http://127.0.0.1:{gpu.ENGINE_PORTS[0]}/v1/chat/completions",
        json=body,
        timeout=aiohttp.ClientTimeout(total=90),
        allow_redirects=False,
    ) as response:
        if response.status != 200 or response.content_type != "text/event-stream":
            raise gpu.StudyError("APC diagnostic stream HTTP/content type failed")
        async for chunk in response.content.iter_chunked(16_384):
            parser.feed(chunk, time.monotonic_ns())
        parser.finish()
    if (
        parser.prompt_tokens != expected_prompt_tokens
        or parser.completion_tokens != 128
        or parser.first_content_ns is None
    ):
        raise gpu.StudyError("APC diagnostic token usage/timing mismatch")
    return {
        "prompt_tokens": parser.prompt_tokens,
        "completion_tokens": parser.completion_tokens,
        "first_content_ms": (parser.first_content_ns - start) / 1_000_000,
        "terminal_ms": (time.monotonic_ns() - start) / 1_000_000,
    }


async def _diagnostic_mode(
    session: aiohttp.ClientSession,
    out: Path,
    args: argparse.Namespace,
    budget: gpu.Budget,
    *,
    prefix_caching: bool,
    prompt: str,
    expected_prompt_tokens: int,
    context_length: int,
) -> dict[str, Any]:
    label = "apc-on" if prefix_caching else "apc-off"
    log_path = out / f"diagnostic-{label}.log"
    log = log_path.open("xb")
    env = os.environ.copy()
    env.update(
        {
            "CUDA_VISIBLE_DEVICES": "0",
            "HF_HUB_OFFLINE": "1",
            "VLLM_SERVER_DEV_MODE": "1",
        }
    )
    process = subprocess.Popen(
        gpu._engine_argv(
            args.vllm_executable,
            args.model_dir,
            gpu.ENGINE_PORTS[0],
            context_length=context_length,
            prefix_caching=prefix_caching,
        ),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    try:
        while True:
            budget.require(300)
            if process.poll() is not None:
                raise gpu.StudyError(f"{label} engine exited during startup")
            try:
                status, _ = await gpu._get(
                    session, f"http://127.0.0.1:{gpu.ENGINE_PORTS[0]}/health"
                )
                if status == 200:
                    break
            except (aiohttp.ClientError, TimeoutError, OSError):
                pass
            await asyncio.sleep(5)
        before = await gpu._quiescent_metrics(session, gpu.ENGINE_PORTS[0])
        reset_success: bool | None = None
        if prefix_caching:
            async with session.post(
                f"http://127.0.0.1:{gpu.ENGINE_PORTS[0]}/reset_prefix_cache",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                reset_success = response.status == 200 and await response.json() == {
                    "success": True
                }
            if not reset_success:
                raise gpu.StudyError("APC-on diagnostic reset did not succeed")
            before = await gpu._quiescent_metrics(session, gpu.ENGINE_PORTS[0])
        requests = [
            await _diagnostic_stream(session, prompt, expected_prompt_tokens)
            for _ in range(2)
        ]
        after = await gpu._quiescent_metrics(session, gpu.ENGINE_PORTS[0])
        counters = {
            key: after[key] - before[key]
            for key in after.keys() & before.keys()
            if key.startswith("vllm:prefix_cache_")
        }
        return {
            "mode": label,
            "reset_success": reset_success,
            "requests": requests,
            "prefix_counter_deltas": counters,
            "counter_status": (
                "VALID"
                if "vllm:prefix_cache_hits_total" in counters
                and "vllm:prefix_cache_queries_total" in counters
                else "MISSING"
            ),
        }
    finally:
        gpu._stop_engines([process], [log])
        if not gpu._ports_closed():
            raise gpu.StudyError(f"{label} engine port remained open after cleanup")


async def diagnostic(args: argparse.Namespace) -> None:
    budget = gpu._budget(args)
    out: Path = args.output_root
    out.mkdir(parents=True, exist_ok=False)
    record: dict[str, Any] = {
        "schema": "inferdrome.vllm-capacity-apc-diagnostic.v2",
        "status": "STARTING",
        "modes": [],
        "error": None,
    }
    try:
        manifest, plans = _prepared(args.prepared_dir)
        if args.image_reference != gpu.IMAGE:
            raise gpu.StudyError(
                "diagnostic image declaration differs from pinned image"
            )
        if not re.fullmatch(r"[0-9]+", args.instance_id):
            raise gpu.StudyError("provider instance ID must be numeric")
        record["provider_instance_id"] = args.instance_id
        record["model_snapshot"] = gpu._snapshot(args.model_dir)
        record["vllm_version"] = gpu._vllm_version(args.vllm_executable)
        record["cli_flags"] = _verify_cli_flags(args.vllm_executable)
        record["gpus"] = gpu._gpu_observation()
        if not gpu._ports_closed():
            raise gpu.StudyError("diagnostic loopback ports are occupied")
        first_rate = manifest["rates_rps"][0]
        plan, _ = plans[_plan_name("pilot", PILOT_SEED, first_rate)]
        offer = capacity_trace(
            PILOT_SEED, plan["offered_count"], plan["duration_ns"], plan["workload"]
        )[0]
        expected = plan["prompt_tokens_by_index"][0]
        for prefix_caching in (True, False):
            budget.require(900)
            async with aiohttp.ClientSession(trust_env=False) as session:
                mode = await _diagnostic_mode(
                    session,
                    out,
                    args,
                    budget,
                    prefix_caching=prefix_caching,
                    prompt=offer.prompt,
                    expected_prompt_tokens=expected,
                    context_length=manifest["workload"]["context_length"],
                )
            record["modes"].append(mode)
            gpu._save(out / f"diagnostic-{mode['mode']}.json", mode)
        record["status"] = (
            "COMPLETED_DESCRIPTIVE"
            if record["modes"][0]["counter_status"] == "VALID"
            else "INCOMPLETE_METRICS"
        )
    except BaseException as error:
        record["status"] = "FAILED"
        record["error"] = f"{type(error).__name__}: {str(error)[:300]}"
        raise
    finally:
        record["ended_utc"] = datetime.now(UTC).isoformat()
        record["engine_log_sha256"] = {
            path.name: gpu._file_digest(path)
            for path in sorted(out.glob("diagnostic-*.log"))
        }
        gpu._save(out / "diagnostic.json", record)


def main() -> None:
    parser = argparse.ArgumentParser(description="Two-replica vLLM capacity study")
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--tokenizer-root", type=Path, required=True)
    prep.add_argument("--output-root", type=Path, required=True)
    prep.add_argument("--rates-rps", default=",".join(map(str, DEFAULT_RATES)))
    prep.add_argument("--duration-s", type=int, default=120)
    prep.add_argument("--document-count", type=int, default=48)
    prep.add_argument("--target-prefix-tokens", type=int, default=4096)
    prep.add_argument("--context-length", type=int, default=8192)
    prep.add_argument("--client-concurrency", type=int, default=256)
    prep.add_argument("--router-max-active", type=int, default=128)
    prep.add_argument("--router-max-queue", type=int, default=256)
    for name in ("run", "diagnostic"):
        command = commands.add_parser(name)
        command.add_argument("--prepared-dir", type=Path, required=True)
        command.add_argument("--model-dir", type=Path, required=True)
        command.add_argument("--vllm-executable", type=Path, required=True)
        command.add_argument("--image-reference", required=True)
        command.add_argument("--instance-id", required=True)
        command.add_argument("--billing-start-utc", required=True)
        command.add_argument("--hourly-rate-usd", required=True)
        command.add_argument("--cap-usd", required=True)
        command.add_argument("--reserve-usd", required=True)
        command.add_argument("--output-root", type=Path, required=True)
        if name == "run":
            command.add_argument("--source-commit", required=True)
    render = commands.add_parser("report")
    render.add_argument("--raw-root", type=Path, required=True)
    render.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(
                args.output_root,
                args.tokenizer_root,
                rates=_rate_list(args.rates_rps),
                duration_s=args.duration_s,
                document_count=args.document_count,
                target_prefix_tokens=args.target_prefix_tokens,
                context_length=args.context_length,
                client_concurrency=args.client_concurrency,
                router_max_active=args.router_max_active,
                router_max_queue=args.router_max_queue,
            )
        elif args.command == "run":
            asyncio.run(run(args))
        elif args.command == "diagnostic":
            asyncio.run(diagnostic(args))
        else:
            report(args.raw_root, args.output_root)
    except (
        gpu.StudyError,
        OSError,
        ValueError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"vllm-router-capacity: {error}", file=sys.stderr)
        raise SystemExit(2) from error


def _verify_cli_flags(executable: Path) -> dict[str, bool]:
    completed = subprocess.run(
        [str(executable), "serve", "--help=all"],
        capture_output=True,
        text=True,
        check=True,
    )
    help_text = completed.stdout + completed.stderr
    flags = (
        "--enable-prefix-caching",
        "--no-enable-prefix-caching",
        "--no-enable-log-requests",
        "--max-model-len",
    )
    observed = {flag: flag in help_text for flag in flags}
    if not all(observed.values()):
        raise gpu.StudyError(f"vLLM 0.26.0 CLI is missing required flags: {observed}")
    return observed


def _cache_capacity(log_paths: list[Path], workload: dict[str, Any]) -> dict[str, Any]:
    capacities: list[int] = []
    for path in log_paths:
        text = path.read_text(errors="replace")
        matches = re.findall(r"GPU KV cache size:\s*([0-9,]+) tokens", text)
        if not matches:
            raise gpu.StudyError(f"KV cache capacity absent from {path.name}")
        capacities.append(int(matches[-1].replace(",", "")))
    working_set = sum(workload["document_prefix_tokens"])
    candidate_valid = working_set > min(capacities) and working_set < int(
        sum(capacities) * 0.85
    )
    return {
        "per_replica_tokens": capacities,
        "document_prefix_working_set_tokens": working_set,
        "candidate_valid": candidate_valid,
        "rule": (
            "document prefixes exceed one replica and use under 85% of "
            "aggregate reported cache tokens; active requests need remaining room"
        ),
        "residency_claim": "NONE_OBSERVED_CAPACITY_ONLY",
    }


def _schedule(rates: list[int]) -> list[dict[str, Any]]:
    scheduled: list[dict[str, Any]] = []
    for block, (seed, order) in enumerate(
        zip(EVALUATION_SEEDS, ORDERS, strict=True), 1
    ):
        shift = (block - 1) % len(rates)
        rotated_rates = rates[shift:] + rates[:shift]
        for rate in rotated_rates:
            for position, policy in enumerate(order, 1):
                scheduled.append(
                    {
                        "block": block,
                        "seed": seed,
                        "rate_rps": rate,
                        "position": position,
                        "policy": policy,
                        "label": f"evaluation-b{block}-r{rate}-p{position}-{policy}",
                    }
                )
    return scheduled


async def _run_condition(
    session: aiohttp.ClientSession,
    out: Path,
    record: dict[str, Any],
    *,
    label: str,
    policy: str,
    plan: dict[str, Any],
    certificate: dict[str, Any],
    router_max_active: int,
    router_max_queue: int,
    saturation_active: int | None = None,
    condition_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reset = await gpu._warm_and_reset(session, out, label)
    item = await gpu._condition(
        session,
        out,
        label,
        policy,
        plan,
        certificate,
        router_max_active=router_max_active,
        router_max_queue=router_max_queue,
        saturation_active=saturation_active,
    )
    item["engine_metrics_status"] = "NOT_COLLECTED"
    if condition_metadata is not None:
        item.update(condition_metadata)
    record["conditions"].append(item)
    number = len(record["conditions"])
    gpu._save(out / f"progress-{number:02}-client.json", record)
    if item["trial_status"] == "INTERRUPTED":
        item["engine_metrics_status"] = "SKIPPED_INTERRUPTED"
        raise gpu.TrialInterrupted(f"{label} trial returned INTERRUPTED")
    try:
        item["engine_metrics"] = await gpu._after_metrics(session, out, label, reset)
        item["engine_metrics_status"] = "CAPTURED"
        if _counter_deltas(item)["status"] != "VALID":
            item["engine_metrics_status"] = "INVALID_COUNTERS"
            item["analysis_error"] = (
                f"{label} prefix-cache counters are missing or invalid"
            )
            gpu._save(
                out / f"analysis-error-{label}.json",
                {"label": label, "reason": item["analysis_error"]},
            )
            gpu._save(out / f"progress-{number:02}.json", record)
            return item
        item["capacity_diagnostics"] = _ledger_diagnostics(
            out / f"{label}-router.jsonl", plan
        )
        item["capacity_assessment"] = _condition_assessment(item, plan["offered_count"])
    except BaseException as error:
        item["analysis_error"] = f"{type(error).__name__}: {str(error)[:300]}"
        gpu._save(
            out / f"analysis-error-{label}.json",
            {"label": label, "reason": item["analysis_error"]},
        )
        raise
    gpu._save(out / f"progress-{number:02}.json", record)
    return item


async def run(args: argparse.Namespace) -> None:
    budget = gpu._budget(args)
    out: Path = args.output_root
    out.mkdir(parents=True, exist_ok=False)
    try:
        manifest, plans = _prepared(args.prepared_dir)
        if args.image_reference != gpu.IMAGE:
            raise gpu.StudyError("outer image declaration differs from pinned image")
        if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
            raise gpu.StudyError("source commit must be an exact SHA-1")
        if not re.fullmatch(r"[0-9]+", args.instance_id):
            raise gpu.StudyError("provider instance ID must be numeric")
        snapshot = gpu._snapshot(args.model_dir)
        version = gpu._vllm_version(args.vllm_executable)
        flags = _verify_cli_flags(args.vllm_executable)
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
            context_length=manifest["workload"]["context_length"],
        )
        async with aiohttp.ClientSession(trust_env=False) as session:
            await gpu._ready(session, processes, budget)
            for port in gpu.ENGINE_PORTS:
                status, content = await gpu._get(
                    session, f"http://127.0.0.1:{port}/v1/models"
                )
                if status != 200 or QWEN3_8B_MODEL_ID not in content:
                    raise gpu.StudyError(f"engine {port} did not serve pinned model")
            record["cache_capacity"] = _cache_capacity(
                [out / f"engine-{index}.log" for index in range(2)],
                manifest["workload"],
            )
            gpu._save(out / "capacity-check.json", record["cache_capacity"])
            if not record["cache_capacity"]["candidate_valid"]:
                record["status"] = "CANDIDATE_INVALID"
                return
            rates = tuple(manifest["rates_rps"])
            pilot: list[dict[str, Any]] = []
            for rate in rates:
                budget.require(manifest["duration_s"] + 300)
                name = _plan_name("pilot", PILOT_SEED, rate)
                plan, cert = plans[name]
                item = await _run_condition(
                    session,
                    out,
                    record,
                    label=name,
                    policy="least_busy",
                    plan=plan,
                    certificate=cert,
                    router_max_active=manifest["router_max_active"],
                    router_max_queue=manifest["router_max_queue"],
                )
                pilot.append(item)
                if item["engine_metrics_status"] != "CAPTURED":
                    record["status"] = "INVALID_EVIDENCE"
                    return
                assessment = item["capacity_assessment"]
                if assessment["generator_limited"] or assessment["admission_limited"]:
                    record["status"] = "HARNESS_LIMIT"
                    return
                if not item["comparison_valid"] or item["trial_status"] != "COMPLETED":
                    record["status"] = "INVALID_PILOT"
                    return
                if not assessment["passed"]:
                    break
            boundary = _pilot_boundary(pilot, rates)
            record["pilot_boundary"] = boundary
            gpu._save(out / "pilot-boundary.json", boundary)
            if boundary["status"] != "BRACKETED":
                record["status"] = boundary["status"]
                return
            schedule = _schedule(boundary["rates_rps"])
            record["evaluation_schedule"] = schedule
            record["evaluation_schedule_sha256"] = gpu._save(
                out / "evaluation-schedule.json",
                {"rates_rps": boundary["rates_rps"], "conditions": schedule},
            )
            gpu._save(out / "pilot-freeze.json", record)
            for planned in schedule:
                budget.require(manifest["duration_s"] + 300)
                plan, cert = plans[
                    _plan_name("evaluation", planned["seed"], planned["rate_rps"])
                ]
                item = await _run_condition(
                    session,
                    out,
                    record,
                    label=planned["label"],
                    policy=planned["policy"],
                    plan=plan,
                    certificate=cert,
                    router_max_active=manifest["router_max_active"],
                    router_max_queue=manifest["router_max_queue"],
                )
                if item["engine_metrics_status"] != "CAPTURED":
                    record["status"] = "INVALID_EVIDENCE"
                    return
                if (
                    not item["comparison_valid"]
                    or item["trial_status"] != "COMPLETED"
                    or item["capacity_assessment"]["generator_limited"]
                    or item["capacity_assessment"]["admission_limited"]
                ):
                    record["status"] = "INVALID_EVALUATION"
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


if __name__ == "__main__":
    main()
