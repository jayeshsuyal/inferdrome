"""Small local CLI for the v0.3 stale-telemetry qualification overlay."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from inferdrome.errors import VerificationError
from inferdrome.routing_campaign import RoutingCampaignError
from inferdrome.routing_qualification.qualification import (
    CapturedQualification,
    StaleTelemetryQualificationError,
    capture_qualification,
    publish_qualification,
    run_qualification,
    verify_qualification,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m inferdrome.routing_qualification"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser(
        "run", help="seal the fixed local campaign and its qualification descriptor"
    )
    run.add_argument("--campaign-plan", type=Path, required=True)
    run.add_argument("--request-trace", type=Path, required=True)
    run.add_argument("--fault-schedule", type=Path, required=True)
    run.add_argument("--trial-plan", type=Path, required=True)
    run.add_argument("--campaign-output", type=Path, required=True)
    run.add_argument("--qualification-output-root", type=Path, required=True)

    capture = commands.add_parser(
        "capture", help="bind a verified R1 package to a no-replace descriptor"
    )
    capture.add_argument("--campaign-package", type=Path, required=True)
    capture.add_argument("--expected-source-digest", required=True)
    capture.add_argument("--qualification-output-root", type=Path, required=True)

    for name, help_text in (
        ("verify", "independently verify one sealed qualification"),
        ("inspect", "show a bounded verified qualification summary"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--campaign-package", type=Path, required=True)
        command.add_argument("--qualification-root", type=Path, required=True)
        command.add_argument("--expected-qualification-digest", required=True)
    return parser


def _json_output(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _view(captured: CapturedQualification) -> dict[str, Any]:
    descriptor = captured.descriptor
    return {
        "qualification_id": descriptor.qualification_id,
        "qualification_retained_digest": captured.retained_digest,
        "source_campaign_id": descriptor.source_campaign_id,
        "source_package_retained_digest": descriptor.source_package_retained_digest,
        "fault_timeline": descriptor.fault_timeline.model_dump(mode="json"),
        "population_accounting": descriptor.population_accounting,
        "repetitions_per_mode": descriptor.repetitions_per_mode,
        "trials": [trial.model_dump(mode="json") for trial in descriptor.trials],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run a local, non-networking stale-telemetry qualification command."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "run":
            sealed = run_qualification(
                campaign_plan=arguments.campaign_plan,
                request_trace=arguments.request_trace,
                fault_schedule=arguments.fault_schedule,
                trial_plan=arguments.trial_plan,
                campaign_output=arguments.campaign_output,
                qualification_output_root=arguments.qualification_output_root,
            )
            captured = verify_qualification(
                arguments.qualification_output_root,
                campaign_package=sealed.campaign.path,
                expected_descriptor_digest=sealed.qualification.retained_digest,
            )
            _json_output(
                {
                    "campaign_package": str(sealed.campaign.path),
                    "qualification_directory": str(sealed.qualification.path),
                    "source_retained_digest": sealed.campaign.retained_digest,
                    **_view(captured),
                }
            )
            return 0
        if arguments.command == "capture":
            captured = capture_qualification(
                arguments.campaign_package,
                expected_source_digest=arguments.expected_source_digest,
            )
            published = publish_qualification(
                captured,
                campaign_package=arguments.campaign_package,
                output_root=arguments.qualification_output_root,
            )
            verified = verify_qualification(
                arguments.qualification_output_root,
                campaign_package=arguments.campaign_package,
                expected_descriptor_digest=published.retained_digest,
            )
            _json_output(
                {
                    "qualification_directory": str(published.path),
                    **_view(verified),
                }
            )
            return 0
        if arguments.command in {"verify", "inspect"}:
            captured = verify_qualification(
                arguments.qualification_root,
                campaign_package=arguments.campaign_package,
                expected_descriptor_digest=arguments.expected_qualification_digest,
            )
            payload = _view(captured)
            if arguments.command == "verify":
                payload["valid"] = True
            _json_output(payload)
            return 0
    except (
        OSError,
        RoutingCampaignError,
        StaleTelemetryQualificationError,
        VerificationError,
    ) as error:
        parser.error(str(error))
    raise AssertionError("routing qualification command dispatch is incomplete")
