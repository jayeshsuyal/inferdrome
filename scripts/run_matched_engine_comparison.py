#!/usr/bin/env python3
"""Preview, offline readiness, or explicit local matched engine execution."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
from pathlib import Path

from inferdrome.evaluation.engine_comparison import encoded, run_comparison
from inferdrome.evaluation.engine_comparison_local import (
    LocalInputs,
    drivers,
    launch_argv,
)
from inferdrome.evaluation.engine_comparison_readiness import (
    SessionPreparation,
    offline_readiness,
)
from inferdrome.evaluation.files import read_input


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--preparation", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute-local", action="store_true")
    args = parser.parse_args()
    try:
        inputs = LocalInputs.model_validate_json(read_input(args.inputs))
        if args.preparation is None:
            if args.execute_local:
                raise ValueError
            # Pure finite argv projection; reports digests, never paths/prompts.
            commands = [
                launch_argv(r, inputs, i)
                for r in (inputs.vllm, inputs.sglang)
                for i in (0, 1)
            ]
            from inferdrome.routing_execution.canonical import sha256_digest

            print(
                json.dumps(
                    {
                        "status": "PREVIEW_ONLY_NOT_RENTAL_READY",
                        "plan_sha256": inputs.plan.digest,
                        "launch_sha256": sha256_digest(encoded(commands)),
                        "provider_calls": 0,
                    }
                )
            )
            return 0
        preparation = SessionPreparation.model_validate_json(
            read_input(args.preparation)
        )
        receipt = offline_readiness(inputs, preparation)
        if not args.execute_local:
            print(json.dumps(receipt, sort_keys=True))
            return 0
        if args.output is None or str(args.output) != preparation.result_destination:
            raise ValueError

        async def run() -> None:
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(signum, stop.set)
            try:
                await run_comparison(
                    inputs.plan, drivers(inputs), args.output, stop=stop
                )
            finally:
                for signum in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(signum)

        asyncio.run(run())
        return 0
    except (Exception, KeyboardInterrupt):
        print("Matched comparison withheld; inspect local inputs and cleanup report.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
