"""Local-only entrypoint; no provider, deployment, publication or credentials."""

import argparse
import asyncio
import json
import signal
import sys
from dataclasses import replace
from pathlib import Path

from inferdrome.evaluation.contracts import EvaluationConfig, load_config_bytes
from inferdrome.evaluation.files import OutputFile, read_input
from inferdrome.evaluation.loopback import loopback_pair
from inferdrome.evaluation.runner import EvaluationResult, run_evaluation
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded inference evaluation v1")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--config", required=True, type=Path)
    run_parser.add_argument("--output", required=True, type=Path)
    demo_parser = commands.add_parser("demo")
    demo_parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = (
            load_config_bytes(read_input(args.config))
            if args.command == "run"
            else None
        )
        with OutputFile(args.output) as destination:
            result = asyncio.run(demo() if config is None else _run(config))
            destination.write(canonical_json_bytes(result.to_dict()) + b"\n")
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
