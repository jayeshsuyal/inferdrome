"""Verify the v2 archive and publish aggregate affinity diagnostics only."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import tempfile
from collections import Counter, defaultdict
from itertools import pairwise
from pathlib import Path
from typing import Any

from inferdrome import vllm_router_capacity as capacity
from inferdrome import vllm_router_gpu as gpu
from inferdrome.vllm_router_study import capacity_document

SCHEMA = "inferdrome.vllm-affinity-diagnosis.v1"


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.95 + 0.5))]


def _document_ids(workload: dict[str, Any]) -> dict[str, int]:
    return {
        hashlib.sha256(
            capacity_document(index, repeat).encode("utf-8")
        ).hexdigest(): index
        for index, repeat in enumerate(workload["document_repeats"])
    }


def diagnose(
    raw_root: Path, published_report: Path, export_inventory: Path, output: Path
) -> dict[str, Any]:
    """Regenerate the validated v2 report before analyzing its raw route rows."""
    exported = json.loads(export_inventory.read_text())
    if exported.get("schema") != "inferdrome.operator-export.v1":
        raise gpu.StudyError("export inventory schema is invalid")
    inventory_files = exported["files"]
    for path in raw_root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        name = "private/capacity-run/" + str(path.relative_to(raw_root))
        entry = inventory_files.get(name)
        if (
            not isinstance(entry, dict)
            or entry.get("sha256") != gpu._file_digest(path).removeprefix("sha256:")
            or entry.get("bytes") != path.stat().st_size
        ):
            raise gpu.StudyError(f"retrieved export inventory differs: {path.name}")
    with tempfile.TemporaryDirectory(prefix="inferdrome-diagnose-") as temporary:
        regenerated = Path(temporary) / "report"
        capacity.report(raw_root, regenerated)
        verified = json.loads((regenerated / "report.json").read_text())
    original = json.loads(published_report.read_text())
    if verified != original or original["status"] != "COMPLETE_DESCRIPTIVE":
        raise gpu.StudyError(
            "archived capacity report differs from verified raw inputs"
        )
    manifest, plans = capacity._prepared(raw_root / "prepared")
    workload = manifest["workload"]
    documents = _document_ids(workload)
    inventory = original["raw_inventory"]
    blocks: list[dict[str, Any]] = []
    selected_hashes: dict[str, str] = {}
    for condition in original["conditions"]:
        if condition["phase"] != "evaluation":
            continue
        label = condition["label"]
        planned = next(x for x in original["conditions"] if x["label"] == label)
        plan_name = capacity._plan_name(
            "evaluation",
            (101, 103, 107, 109)[planned["block"] - 1],
            planned["rate_rps"],
        )
        plan, _certificate = plans[plan_name]
        for filename in (
            f"{label}-client.json",
            f"{label}-router.jsonl",
            f"metrics-{label}.json",
            f"prepared/{plan_name}-plan.json",
            f"prepared/{plan_name}-certificate.json",
        ):
            selected_hashes[filename] = inventory[filename]
        client = json.loads((raw_root / f"{label}-client.json").read_text())
        route_rows = [
            json.loads(line)
            for line in (raw_root / f"{label}-router.jsonl").read_text().splitlines()
        ]
        if len(route_rows) != plan["offered_count"]:
            raise gpu.StudyError(f"router row count differs from plan: {label}")
        route_rows.sort(key=lambda row: row["arrived_ns"])
        first_arrival = route_rows[0]["arrived_ns"]
        by_document: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for route in route_rows:
            digest = route.get("document_sha256")
            if digest not in documents:
                raise gpu.StudyError(f"unknown routed document in {label}")
            by_document[documents[digest]].append(route)
        escaped = [
            row for row in route_rows if row["route_reason"] == "overload_escape"
        ]
        escape_burden = Counter[int]()
        after_escape = Counter[str]()
        after_non_escape = Counter[str]()
        for doc_rows in by_document.values():
            for prior, following in pairwise(doc_rows):
                # Same document, same 40-second time third. This is an association,
                # not a matched client request or proof of KV residence.
                prior_epoch = min(
                    2,
                    int(
                        (prior["arrived_ns"] - first_arrival) * 3 // plan["duration_ns"]
                    ),
                )
                next_epoch = min(
                    2,
                    int(
                        (following["arrived_ns"] - first_arrival)
                        * 3
                        // plan["duration_ns"]
                    ),
                )
                if prior_epoch != next_epoch:
                    continue
                target = (
                    after_escape
                    if prior["route_reason"] == "overload_escape"
                    else after_non_escape
                )
                affinity = following.get("estimated_affinity")
                state = (
                    "exclusive"
                    if affinity in ([1, 0], [0, 1])
                    else "tie"
                    if affinity == [1, 1]
                    else "neither"
                    if affinity == [0, 0]
                    else "unavailable"
                )
                target[state] += 1
        for row in escaped:
            preferred_busy = max(row["busy_at_decision"])
            escape_burden[preferred_busy] += 1
        completed = [row for row in client["rows"] if row["outcome"] == "completed"]
        post_first = [
            (row["terminal_ns"] - row["first_content_ns"]) / 1e6
            for row in completed
            if row["first_content_ns"] is not None
        ]
        counters = condition["cache_counters"]["replicas"]
        hits = sum(row["hits"] for row in counters)
        queries = sum(row["queries"] for row in counters)
        blocks.append(
            {
                "label": label,
                "block": condition["block"],
                "rate_rps": condition["rate_rps"],
                "policy": condition["policy"],
                "offered": condition["offered"],
                "outcomes": condition["outcomes"],
                "slo_goodput_rps": condition["slo_goodput_rps"],
                "first_content_p95_ms": condition["first_content_p95_ms"],
                "completion_p95_ms": condition["completion_p95_ms"],
                "post_first_content_p95_ms": _p95(post_first),
                "router_queue_p95_ms": condition["router_queue_p95_ms"],
                "replica_requests": condition["router_diagnostics"]["replica_requests"],
                "route_reasons": condition["router_diagnostics"]["route_reasons"],
                "affinity_states": condition["router_diagnostics"]["affinity_states"],
                "selected_replica_busy_p95": condition["router_diagnostics"][
                    "selected_replica_busy_p95"
                ],
                "escape_preferred_busy_histogram": dict(sorted(escape_burden.items())),
                "same_document_same_epoch_next_affinity_after_escape": dict(
                    after_escape
                ),
                "same_document_same_epoch_next_affinity_after_other": dict(
                    after_non_escape
                ),
                "prefix_cache_hits": hits,
                "prefix_cache_queries": queries,
                "prefix_cache_hit_fraction": hits / queries,
            }
        )
    policy_rate: list[dict[str, Any]] = []
    for rate in sorted({row["rate_rps"] for row in blocks}):
        for policy in ("round_robin", "least_busy", "cache_only", "cache_plus_load"):
            matched = [
                row
                for row in blocks
                if row["rate_rps"] == rate and row["policy"] == policy
            ]
            hits = sum(row["prefix_cache_hits"] for row in matched)
            queries = sum(row["prefix_cache_queries"] for row in matched)
            policy_rate.append(
                {
                    "rate_rps": rate,
                    "policy": policy,
                    "blocks": len(matched),
                    "mean_goodput_rps": statistics.mean(
                        row["slo_goodput_rps"] for row in matched
                    ),
                    "prefix_cache_hits": hits,
                    "prefix_cache_queries": queries,
                    "prefix_cache_hit_fraction": hits / queries,
                    "escapes": sum(
                        row["route_reasons"].get("overload_escape", 0)
                        for row in matched
                    ),
                    "affinity_ties": sum(
                        row["affinity_states"].get("tie", 0) for row in matched
                    ),
                }
            )
    result = {
        "schema": SCHEMA,
        "source_report_sha256": gpu._file_digest(published_report),
        "source_export_inventory_sha256": gpu._file_digest(export_inventory),
        "source_session_sha256": inventory["session.json"],
        "input_artifact_sha256": dict(sorted(selected_hashes.items())),
        "workload": {
            "document_count": workload["document_count"],
            "cycle_percent": 60,
            "hotspot_epochs": ["A", "B", "A"],
            "output_tokens": workload["output_tokens"],
        },
        "policy_rate": policy_rate,
        "blocks": blocks,
        "unavailable": [
            "per-request prefix-cache hits",
            "engine prefill/decode phase timing",
            "engine queue duration",
            "preemption events",
        ],
        "interpretation": (
            "Document-route association and estimated affinity only; time thirds "
            "are relative to first router arrival. No individual KV-residency "
            "or causal escape effect is inferred."
        ),
    }
    gpu._save(output, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--published-report", type=Path, required=True)
    parser.add_argument("--export-inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    diagnose(args.raw_root, args.published_report, args.export_inventory, args.output)


if __name__ == "__main__":
    main()
