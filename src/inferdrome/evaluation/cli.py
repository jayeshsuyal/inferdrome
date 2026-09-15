"""Local-only entrypoint; no provider, deployment, publication or credentials."""

import argparse
import asyncio
import json
import signal
import sys
from dataclasses import replace
from pathlib import Path

from inferdrome.evaluation.cache import (
    CacheCellManifest,
    CachePreparation,
    load_cache_preparation_bytes,
    run_cache_cell,
)
from inferdrome.evaluation.cache_config import (
    CompiledCachePlan,
    cache_plan_bytes,
    compile_cache_experiment,
    load_cache_config_bytes,
)
from inferdrome.evaluation.cache_report import report_cache_experiment
from inferdrome.evaluation.contracts import EvaluationConfig, load_config_bytes
from inferdrome.evaluation.fault_config import (
    RoutingFaultConfig,
    load_routing_config_bytes,
)
from inferdrome.evaluation.faults import RoutingFaultResult, run_routing_fault
from inferdrome.evaluation.files import OutputFile, read_input
from inferdrome.evaluation.loopback import loopback_pair
from inferdrome.evaluation.observations import AiohttpProbeTransport
from inferdrome.evaluation.runner import EvaluationResult, run_evaluation
from inferdrome.evaluation.study import (
    StudyManifest,
    plan_bytes,
    preflight_metadata,
    report_study,
    run_study,
)
from inferdrome.evaluation.study_config import (
    StudyConfig,
    compile_study,
    load_study_config_bytes,
)
from inferdrome.evaluation.transport import AiohttpTransport
from inferdrome.routing_execution.canonical import canonical_json_bytes


async def _run(config: EvaluationConfig) -> EvaluationResult:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            loop.add_signal_handler(sig, stop.set)
        return await run_evaluation(config, AiohttpTransport(config), stop=stop)
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


async def _run_routing(config: RoutingFaultConfig) -> RoutingFaultResult:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            loop.add_signal_handler(sig, stop.set)
        return await run_routing_fault(
            config,
            AiohttpTransport(config.foreground),
            AiohttpTransport(config.background),
            AiohttpProbeTransport(
                config.foreground,
                max_response_bytes=config.telemetry.max_response_bytes,
            ),
            AiohttpProbeTransport(
                config.foreground,
                max_response_bytes=config.telemetry.max_response_bytes,
            ),
            stop=stop,
        )
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


async def demo() -> EvaluationResult:
    async with loopback_pair() as origins:
        config = load_config_bytes(
            json.dumps(
                {
                    "schema_version": "inferdrome.evaluation-config.v1",
                    "source_commit": "0" * 40,
                    "model": "synthetic-model",
                    "max_tokens": 16,
                    "endpoints": [
                        {"endpoint_id": endpoint, "origin": origin}
                        for endpoint, origin in zip(
                            ("endpoint-a", "endpoint-b"), origins, strict=True
                        )
                    ],
                    "bounds": {
                        "concurrency": 3,
                        "max_queue": 3,
                        "duration_ns": 100_000_000,
                    },
                    "offers": [
                        {
                            "scheduled_ns": i * 5_000_000,
                            "endpoint_id": "endpoint-a" if i % 2 == 0 else "endpoint-b",
                            "prompt": "A public synthetic loopback prompt.",
                        }
                        for i in range(12)
                    ],
                }
            ).encode()
        )
        result = await _run(config)
    return replace(result, evidence_class="SYNTHETIC_ONLY")


async def _run_study(config: StudyConfig, output_dir: Path) -> StudyManifest:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            loop.add_signal_handler(sig, stop.set)
        return await run_study(config, output_dir, stop=stop)
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


def _study_command(args: argparse.Namespace) -> int:
    config = load_study_config_bytes(read_input(args.config))
    if args.command == "study-plan":
        plan = compile_study(config)
        preflight_metadata(plan)
        with OutputFile(args.output) as destination:
            destination.write(plan_bytes(plan))
        print(
            json.dumps(
                {
                    "planned_trials": len(plan.trials),
                    "planned_replay_requests": plan.planned_request_count,
                    "evidence_eligible": False,
                }
            )
        )
        return 0
    if args.command == "study-run":
        manifest = asyncio.run(_run_study(config, args.output_dir))
        print(
            json.dumps(
                {
                    "status": manifest.status,
                    "returned_trials": sum(
                        row.state == "RETURNED" for row in manifest.trials
                    ),
                    "planned_trials": len(manifest.trials),
                    "evidence_eligible": False,
                }
            )
        )
        return {"COMPLETED": 0, "CANCELLED": 130, "ABORTED": 2}[manifest.status]
    report_study(config, args.study_dir, args.output_dir)
    print(json.dumps({"status": "REPORT_WRITTEN", "evidence_eligible": False}))
    return 0


async def _run_cache(
    plan: CompiledCachePlan,
    cell_id: str,
    preparation: CachePreparation,
    output_dir: Path,
) -> CacheCellManifest:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            loop.add_signal_handler(sig, stop.set)
        return await run_cache_cell(plan, cell_id, preparation, output_dir, stop=stop)
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


def _cache_command(args: argparse.Namespace) -> int:
    config = load_cache_config_bytes(read_input(args.config))
    plan = compile_cache_experiment(config, tokenizer_root=args.tokenizer_root)
    if args.command == "cache-plan":
        with OutputFile(args.output) as destination:
            destination.write(cache_plan_bytes(plan))
        print(
            json.dumps(
                {
                    "planned_cells": len(plan.cells),
                    "planned_replay_requests": plan.planned_request_count,
                    "verification_status": plan.verification_status,
                    "executable": plan.executable,
                    "evidence_eligible": False,
                }
            )
        )
        return 0
    if args.command == "cache-run-cell":
        if not plan.executable:
            print(
                "cache tokenization is unavailable; no replay started", file=sys.stderr
            )
            return 2
        preparation = load_cache_preparation_bytes(read_input(args.preparation))
        manifest = asyncio.run(
            _run_cache(plan, args.cell_id, preparation, args.output_dir)
        )
        print(
            json.dumps(
                {
                    "cell_id": manifest.cell_id,
                    "status": manifest.status,
                    "reason": manifest.reason,
                    "evidence_class": manifest.evidence_class,
                    "runtime_verification": "UNVERIFIED",
                    "evidence_eligible": False,
                }
            )
        )
        return (
            130
            if manifest.reason == "CANCELLED"
            else (0 if manifest.status == "COMPLETED" else 2)
        )
    inputs: dict[str, Path] = {}
    if len(args.cell_input) > len(plan.cells):
        raise ValueError("too many cache inputs")
    for value in args.cell_input:
        key, separator, path = value.partition("=")
        if not separator or not path or key in inputs:
            raise ValueError("invalid cache input mapping")
        inputs[key] = Path(path)
    report_cache_experiment(plan, inputs, args.output_dir)
    print(json.dumps({"status": "REPORT_WRITTEN", "evidence_eligible": False}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded inference evaluation v1")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--config", required=True, type=Path)
    run_parser.add_argument("--output", required=True, type=Path)
    routing_parser = commands.add_parser("routing-run")
    routing_parser.add_argument("--config", required=True, type=Path)
    routing_parser.add_argument("--output", required=True, type=Path)
    demo_parser = commands.add_parser("demo")
    demo_parser.add_argument("--output", required=True, type=Path)
    study_plan_parser = commands.add_parser("study-plan")
    study_plan_parser.add_argument("--config", required=True, type=Path)
    study_plan_parser.add_argument("--output", required=True, type=Path)
    study_run_parser = commands.add_parser("study-run")
    study_run_parser.add_argument("--config", required=True, type=Path)
    study_run_parser.add_argument("--output-dir", required=True, type=Path)
    study_report_parser = commands.add_parser("study-report")
    study_report_parser.add_argument("--config", required=True, type=Path)
    study_report_parser.add_argument("--study-dir", required=True, type=Path)
    study_report_parser.add_argument("--output-dir", required=True, type=Path)
    for name in ("cache-plan", "cache-run-cell", "cache-report"):
        cache_parser = commands.add_parser(name)
        cache_parser.add_argument("--config", required=True, type=Path)
        cache_parser.add_argument("--tokenizer-root", type=Path)
        if name == "cache-plan":
            cache_parser.add_argument("--output", required=True, type=Path)
        else:
            cache_parser.add_argument("--output-dir", required=True, type=Path)
        if name == "cache-run-cell":
            cache_parser.add_argument("--cell-id", required=True)
            cache_parser.add_argument("--preparation", required=True, type=Path)
        elif name == "cache-report":
            cache_parser.add_argument("--cell-input", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command in {"cache-plan", "cache-run-cell", "cache-report"}:
            return _cache_command(args)
        if args.command in {"study-plan", "study-run", "study-report"}:
            return _study_command(args)
        config = (
            load_config_bytes(read_input(args.config))
            if args.command == "run"
            else None
        )
        routing_config = (
            load_routing_config_bytes(read_input(args.config))
            if args.command == "routing-run"
            else None
        )
        with OutputFile(args.output) as destination:
            result: EvaluationResult | RoutingFaultResult
            if routing_config is not None:
                result = asyncio.run(_run_routing(routing_config))
            else:
                result = asyncio.run(demo() if config is None else _run(config))
            destination.write(canonical_json_bytes(result.to_dict()) + b"\n")
        if isinstance(result, RoutingFaultResult):
            print(
                json.dumps(
                    {
                        "status": result.status,
                        "foreground_offered_count": len(result.foreground.records),
                        "background_offered_count": len(result.background.records),
                        "evidence_class": result.evidence_class,
                        "evidence_eligible": False,
                    }
                )
            )
            return {"COMPLETED": 0, "WARMUP_FAILED": 2, "CANCELLED": 130}[result.status]
        print(
            json.dumps(
                {
                    "offered_count": len(result.records),
                    "outcomes": result.to_dict()["outcomes"],
                    "evidence_class": result.evidence_class,
                    "evidence_eligible": False,
                }
            )
        )
        return 130 if result.cancelled else 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("evaluation failed; no completed report was produced", file=sys.stderr)
        return 2
