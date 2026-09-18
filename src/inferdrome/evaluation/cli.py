"""Local-only entrypoint; no provider, deployment, publication or credentials."""

import argparse
import asyncio
import json
import signal
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic_ns

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
from inferdrome.evaluation.load_calibration import (
    calibration_plan_bytes,
    compile_calibration,
    confirmation_plan,
    load_calibration_observations_bytes,
    load_calibration_protocol_bytes,
    select_calibration_level,
)
from inferdrome.evaluation.load_calibration_operator import (
    export_host_local_rehearsal,
    load_host_local_authorization,
    prepare_host_local_rehearsal,
    run_authorized_host_local_rehearsal,
    verify_retrieved_host_local_export,
    write_preflight,
)
from inferdrome.evaluation.load_calibration_rehearsal import RehearsalResult
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


def _calibration_command(args: argparse.Namespace) -> int:
    """Compile or select local JSON declarations without opening a transport."""
    protocol = load_calibration_protocol_bytes(read_input(args.protocol))
    plan = compile_calibration(protocol)
    if args.command == "load-calibration-plan":
        with OutputFile(args.output) as destination:
            destination.write(calibration_plan_bytes(plan))
        print(
            json.dumps(
                {
                    "phase": "CALIBRATION",
                    "planned_trials": len(plan.trials),
                    "protocol_sha256": plan.protocol_sha256,
                    "evidence_eligible": False,
                }
            )
        )
        return 0
    observations = load_calibration_observations_bytes(read_input(args.observations))
    selection = select_calibration_level(plan, observations)
    confirmation = confirmation_plan(plan, selection)
    with OutputFile(args.selection_output) as destination:
        destination.write(
            canonical_json_bytes(selection.model_dump(mode="json")) + b"\n"
        )
    with OutputFile(args.confirmation_output) as destination:
        destination.write(
            canonical_json_bytes(confirmation.model_dump(mode="json")) + b"\n"
        )
    print(
        json.dumps(
            {
                "selection_status": selection.status,
                "selected_level_id": selection.selected_level_id,
                "confirmation_status": confirmation.status,
                "planned_confirmation_trials": len(confirmation.trials),
                "protocol_sha256": plan.protocol_sha256,
                "evidence_eligible": False,
            }
        )
    )
    return 0


async def _run_host_local_operator(args: argparse.Namespace) -> RehearsalResult:
    # This pair deliberately precedes all packet reads and compilation.  The
    # local command must never turn a slow preflight into renewed runtime.
    session_anchor_utc = datetime.now(UTC)
    session_started_ns = monotonic_ns()
    prepared = prepare_host_local_rehearsal(
        protocol_path=args.protocol,
        recipe_paths=tuple(args.recipe),
        runtime=args.runtime,
        sglang_profile_paths=tuple(args.sglang_profile),
    )
    authorization = load_host_local_authorization(args.authorization)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            loop.add_signal_handler(sig, stop.set)
        return await run_authorized_host_local_rehearsal(
            prepared,
            authorization=authorization,
            model_snapshot_path=args.model_snapshot,
            docker_config_directory=args.docker_config_directory,
            output_root=args.output_root,
            stop=stop,
            session_anchor_utc=session_anchor_utc,
            session_started_ns=session_started_ns,
        )
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


def _host_local_operator_command(args: argparse.Namespace) -> int:
    if args.command == "load-calibration-host-preflight":
        prepared = prepare_host_local_rehearsal(
            protocol_path=args.protocol,
            recipe_paths=tuple(args.recipe),
            runtime=args.runtime,
            sglang_profile_paths=tuple(args.sglang_profile),
        )
        write_preflight(args.output, prepared.preflight)
        print(
            json.dumps(
                {
                    "status": "PREFLIGHT_WRITTEN",
                    "runtime": prepared.runtime,
                    "protocol_sha256": prepared.preflight.protocol_sha256,
                    "provider_action_performed": False,
                    "evidence_eligible": False,
                }
            )
        )
        return 0
    if args.command == "load-calibration-host-export":
        export = export_host_local_rehearsal(args.output_root, args.archive)
        print(
            json.dumps(
                {
                    "status": "EXPORT_VERIFIED",
                    "archive_sha256": export.archive_sha256,
                    "source_state": export.source_state,
                    "artifact_count": export.artifact_count,
                    "provider_termination_verified": False,
                    "evidence_eligible": False,
                }
            )
        )
        return 0
    if args.command == "load-calibration-host-verify-export":
        verification = verify_retrieved_host_local_export(
            args.archive, expected_archive_sha256=args.expected_archive_sha256
        )
        print(
            json.dumps(
                {
                    "status": "EXPORT_VERIFIED",
                    "archive_sha256": verification.archive_sha256,
                    "source_state": verification.source_state,
                    "artifact_count": verification.artifact_count,
                    "provider_termination_verified": False,
                    "evidence_eligible": False,
                }
            )
        )
        return 0
    if args.execute_approval != "I_UNDERSTAND_LOCAL_DOCKER_WILL_BE_INVOKED":
        raise ValueError("host-local execution requires its exact local confirmation")
    rehearsal = asyncio.run(_run_host_local_operator(args))
    print(
        json.dumps(
            {
                "status": "REHEARSAL_COMPLETED",
                "calibration_candidate_count": len(rehearsal.calibration_manifests),
                "confirmation_manifest_written": rehearsal.confirmation_manifest
                is not None,
                "provider_termination_verified": False,
                "evidence_eligible": False,
            }
        )
    )
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
    calibration_plan_parser = commands.add_parser("load-calibration-plan")
    calibration_plan_parser.add_argument("--protocol", required=True, type=Path)
    calibration_plan_parser.add_argument("--output", required=True, type=Path)
    calibration_select_parser = commands.add_parser("load-calibration-select")
    calibration_select_parser.add_argument("--protocol", required=True, type=Path)
    calibration_select_parser.add_argument("--observations", required=True, type=Path)
    calibration_select_parser.add_argument(
        "--selection-output", required=True, type=Path
    )
    calibration_select_parser.add_argument(
        "--confirmation-output", required=True, type=Path
    )
    for name in (
        "load-calibration-host-preflight",
        "load-calibration-host-run",
    ):
        host_parser = commands.add_parser(name)
        host_parser.add_argument("--protocol", required=True, type=Path)
        host_parser.add_argument("--recipe", required=True, action="append", type=Path)
        host_parser.add_argument("--runtime", required=True, choices=("vllm", "sglang"))
        host_parser.add_argument("--sglang-profile", action="append", default=[])
        if name == "load-calibration-host-preflight":
            host_parser.add_argument("--output", required=True, type=Path)
        else:
            host_parser.add_argument("--authorization", required=True, type=Path)
            host_parser.add_argument("--model-snapshot", required=True, type=Path)
            host_parser.add_argument(
                "--docker-config-directory", required=True, type=Path
            )
            host_parser.add_argument("--output-root", required=True, type=Path)
            host_parser.add_argument("--execute-approval", required=True)
    host_export_parser = commands.add_parser("load-calibration-host-export")
    host_export_parser.add_argument("--output-root", required=True, type=Path)
    host_export_parser.add_argument("--archive", required=True, type=Path)
    host_verify_parser = commands.add_parser("load-calibration-host-verify-export")
    host_verify_parser.add_argument("--archive", required=True, type=Path)
    host_verify_parser.add_argument("--expected-archive-sha256", required=True)
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
        if args.command in {"load-calibration-plan", "load-calibration-select"}:
            return _calibration_command(args)
        if args.command.startswith("load-calibration-host-"):
            return _host_local_operator_command(args)
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


if __name__ == "__main__":
    raise SystemExit(main())
