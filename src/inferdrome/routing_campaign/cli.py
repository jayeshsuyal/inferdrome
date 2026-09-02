"""Small local CLI for sealing, verifying, and inspecting R1 packages."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from inferdrome.errors import VerificationError
from inferdrome.routing_campaign.package import (
    RoutingCampaignError,
    run_campaign,
    verify_campaign,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m inferdrome.routing_campaign")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="seal the fixed synthetic CPU campaign")
    run.add_argument("--campaign-plan", type=Path, required=True)
    run.add_argument("--request-trace", type=Path, required=True)
    run.add_argument("--fault-schedule", type=Path, required=True)
    run.add_argument("--trial-plan", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="offline verify a sealed campaign")
    verify.add_argument("package", type=Path)
    verify.add_argument("--expected-digest")
    inspect = commands.add_parser("inspect", help="show a bounded verified summary")
    inspect.add_argument("package", type=Path)
    return parser


def _json_output(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    """Run a local, non-networking R1 command."""

    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "run":
            sealed = run_campaign(
                arguments.campaign_plan,
                arguments.request_trace,
                arguments.fault_schedule,
                arguments.trial_plan,
                arguments.output,
            )
            _json_output(
                {
                    "package_path": str(sealed.path),
                    "retained_digest": sealed.retained_digest,
                }
            )
            return 0
        if arguments.command == "verify":
            report = verify_campaign(
                arguments.package,
                expected_digest=arguments.expected_digest,
            )
            _json_output({"retained_digest": report.retained_digest, "valid": True})
            return 0
        if arguments.command == "inspect":
            report = verify_campaign(arguments.package)
            _json_output(
                {
                    "retained_digest": report.retained_digest,
                    "trial_ids": list(report.trial_ids),
                    "terminal_populations": list(report.terminal_populations),
                    "verified": True,
                }
            )
            return 0
    except (OSError, RoutingCampaignError, VerificationError) as error:
        _parser().error(str(error))
    raise AssertionError("routing campaign command dispatch is incomplete")
