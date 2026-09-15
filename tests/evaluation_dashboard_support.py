"""Offline synthetic reducer fixtures for evaluation-report dashboard tests.

These helpers execute deterministic test transports before a dashboard is started.
They never stand in for genuine tokenizer, GPU, runtime, or cache-hit evidence.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from inferdrome.evaluation.cache_config import compile_cache_experiment
from inferdrome.evaluation.cache_report import report_cache_experiment
from inferdrome.evaluation.study_config import compile_study
from inferdrome.evaluation.study_report import summarize_study, summarize_trial
from inferdrome.evaluation.study_validation import validate_trial_result
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_evaluation_cache_config import (
    cache_payload,
    synthetic_verifier,
)
from tests.unit.test_evaluation_cache_config import (
    load as load_cache,
)
from tests.unit.test_evaluation_cache_report import _write_cells
from tests.unit.test_evaluation_study_config import load as load_study
from tests.unit.test_evaluation_study_config import study_payload
from tests.unit.test_evaluation_study_report import _manifest, measure


def write_study_report(
    root: Path,
    *,
    variant: str = "complete",
    scenario: str = "STALE_LOAD",
) -> tuple[Path, dict[str, Any]]:
    """Generate a study report with real reducers and explicit synthetic results."""
    if variant not in {
        "complete",
        "partial",
        "cancelled",
        "no-measurements",
        "warmup-failed",
        "censored",
    }:
        raise ValueError("unsupported synthetic study fixture variant")
    root.mkdir(mode=0o700, parents=True)
    payload = study_payload(scenario)
    if variant == "censored":
        for index, offer in enumerate(payload["blocks"][0]["foreground"]["offers"]):
            offer["scheduled_ns"] = (20 + 15 * index) * 1_000_000
    plan = compile_study(load_study(payload))
    summaries: list[dict[str, Any]] = []
    selected = plan.trials if variant in ("complete", "censored") else plan.trials[:1]
    if variant == "no-measurements":
        selected = ()
    mode = {
        "cancelled": "CANCELLED_BEFORE_START",
        "warmup-failed": "WARMUP_FAILED",
    }.get(variant, "COMPLETED")
    for trial in selected:
        result = replace(
            validate_trial_result(measure(trial, mode), trial.config),
            evidence_class="SYNTHETIC_ONLY",
        )
        summaries.append(summarize_trial(trial, result))
    status = "COMPLETED" if variant in ("complete", "censored") else "ABORTED"
    if variant == "cancelled":
        status = "CANCELLED"
    manifest = _manifest(plan, summaries, status)
    if variant == "cancelled":
        manifest["reason"] = "CANCELLED"
    if variant == "warmup-failed":
        manifest["reason"] = "WARMUP_FAILED"
    if variant == "partial":
        manifest["trials"][1]["state"] = "ABORTED"
    report = summarize_study(plan, summaries, manifest)
    path = root / "report.json"
    path.write_bytes(canonical_json_bytes(report) + b"\n")
    path.chmod(0o600)
    return path, report


def write_cache_report(
    root: Path,
    *,
    block_count: int = 1,
    variant: str = "complete",
) -> tuple[Path, dict[str, Any]]:
    """Run bounded fake cell transports and invoke the authoritative cache reducer."""
    mutations = {
        "complete": None,
        "all-failure": "reject-all",
        "cancelled": "cancelled",
        "aborted": "aborted",
        "missing": None,
        "invalid": None,
        "declarations": "unknown-cache",
        "no-measurements": None,
        "drain": "drain",
    }
    if variant not in mutations:
        raise ValueError("unsupported synthetic cache fixture variant")
    root.mkdir(mode=0o700, parents=True)
    plan = compile_cache_experiment(
        load_cache(cache_payload(block_count)), verifier=synthetic_verifier
    )
    cells_root = root / "cells"
    cells_root.mkdir(mode=0o700)
    inputs = (
        {}
        if variant == "no-measurements"
        else _write_cells(cells_root, plan, mutation=mutations[variant])
    )
    if variant == "missing":
        inputs.pop(plan.cells[1].cell_id)
    if variant == "invalid":
        invalid = root / "invalid-cell"
        invalid.mkdir(mode=0o700)
        inputs[plan.cells[1].cell_id] = invalid
    output = root / "reduced"
    report = report_cache_experiment(plan, inputs, output)
    return output / "report.json", report


def write_catalog(
    root: Path, *, study_variant: str = "complete", cache_block_count: int = 4
) -> dict[str, Any]:
    """Build at most eight private report pins for real-backend UI journeys."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    reports: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, str]] = []
    variants = (
        ("study-complete", "STUDY", study_variant, 1),
        ("study-partial", "STUDY", "partial", 1),
        ("study-cancelled", "STUDY", "cancelled", 1),
        ("study-empty", "STUDY", "no-measurements", 1),
        ("cache-complete", "PREFIX_CACHE", "complete", cache_block_count),
        ("cache-zero", "PREFIX_CACHE", "all-failure", 1),
        ("cache-cancelled", "PREFIX_CACHE", "cancelled", 1),
        ("cache-invalid", "PREFIX_CACHE", "invalid", 1),
    )
    for label, kind, variant, block_count in variants:
        source, report = (
            write_study_report(root / label, variant=variant)
            if kind == "STUDY"
            else write_cache_report(
                root / label, variant=variant, block_count=block_count
            )
        )
        entry = {
            "kind": kind,
            "report_path": str(source),
            "expected_sha256": sha256_digest(source.read_bytes()),
        }
        entries.append(entry)
        reports[label] = {
            **entry,
            "status": report["status"],
            "evidence_class": report["evidence_class"],
        }
    catalog = root / "evaluation-reports-catalog.json"
    catalog.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.dashboard-evaluation-reports-catalog.v1",
                "entries": entries,
            }
        )
        + b"\n"
    )
    catalog.chmod(0o600)
    return {
        "catalog": str(catalog),
        "reports": reports,
        "fixture_provenance": "SYNTHETIC_ONLY",
        "source_replay_by_viewer": "NOT_PERFORMED",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--study-variant", choices=("complete", "censored"), default="complete"
    )
    parser.add_argument("--cache-block-count", type=int, choices=(1, 4, 8), default=4)
    arguments = parser.parse_args()
    print(
        json.dumps(
            write_catalog(
                arguments.root.resolve(),
                study_variant=arguments.study_variant,
                cache_block_count=arguments.cache_block_count,
            )
        )
    )


if __name__ == "__main__":
    main()
