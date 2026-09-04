#!/usr/bin/env python3
"""Guarded command surface for the exact two-A100 GCP_PRIVATE canary.

``proposal`` and ``preview`` are offline/local commands.  The former live
``execute`` edge is deliberately fail-closed in v0.2: this profile does not
yet have a reviewed, two-A100, independently durable watchdog worker.  It
therefore never constructs a lifecycle controller, a Google transport, an SDK
client, or a create request.  Separately authorized cleanup-only diagnostics
remain available for an exact pre-existing ownership record; they have no
create surface.  The command never discovers or prints credentials and offers
no generic provider operation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from inferdrome.deployment.gcp_private_campaign_google import (
    create_google_private_campaign_cleanup_transport,
)
from inferdrome.deployment.gcp_private_campaign_v2 import (
    GcpPrivateCampaignCleanupAuthorization,
    GcpPrivateCampaignError,
    GcpPrivateCampaignJournal,
    GcpPrivateCampaignLifecycleController,
    GcpPrivateCampaignProposal,
    GcpPrivateCampaignProposalPayload,
    GcpPrivateCampaignStartupPayload,
    GcpPrivateCampaignStartupPayloadPayload,
    build_gcp_private_campaign_create_request,
    canonical_gcp_private_campaign_proposal_bytes,
    canonical_gcp_private_campaign_startup_bytes,
    gcp_private_campaign_startup_payload_id,
    issue_gcp_private_campaign_proposal,
    verify_gcp_private_campaign_evidence_destination,
)
from inferdrome.deployment.gcp_securefs import SafeDirFD, SafeDirFSError

_MAX_INPUT_BYTES = 524_288
_LIVE_EXECUTION_WATCHDOG_UNAVAILABLE = "LIVE_EXECUTION_WATCHDOG_UNAVAILABLE"


def _read_regular(path: Path) -> bytes:
    selected = path.absolute()
    parent: SafeDirFD | None = None
    descriptor: int | None = None
    try:
        parent = SafeDirFD.open(selected.parent)
        descriptor = parent.open_child(selected.name, os.O_RDONLY)
        metadata = parent.validated_regular_child(selected.name, descriptor=descriptor)
        if not 1 <= metadata.st_size <= _MAX_INPUT_BYTES:
            raise ValueError
        content = os.read(descriptor, _MAX_INPUT_BYTES + 1)
        final = parent.validated_regular_child(selected.name, descriptor=descriptor)
        if len(content) != metadata.st_size or final.st_ino != metadata.st_ino:
            raise ValueError
        return content
    except (OSError, SafeDirFSError, ValueError):
        raise GcpPrivateCampaignError("COMMAND_INPUT_UNAVAILABLE") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            parent.close()


def _write_no_replace(path: Path, content: bytes) -> None:
    selected = path.absolute()
    parent: SafeDirFD | None = None
    descriptor: int | None = None
    try:
        parent = SafeDirFD.open(selected.parent)
        descriptor = parent.open_child(
            selected.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(descriptor)
        parent.validated_regular_child(selected.name, descriptor=descriptor)
        parent.fsync()
    except FileExistsError:
        raise GcpPrivateCampaignError("COMMAND_OUTPUT_EXISTS") from None
    except (OSError, SafeDirFSError):
        raise GcpPrivateCampaignError("COMMAND_OUTPUT_UNAVAILABLE") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            parent.close()


def _parse(model: type[Any], path: Path) -> Any:
    try:
        return model.model_validate_json(_read_regular(path))
    except (ValidationError, ValueError, TypeError):
        raise GcpPrivateCampaignError("COMMAND_INPUT_INVALID") from None


def _emit(value: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")


def _write_or_stdout(output: Path | None, content: bytes) -> None:
    if output is None:
        sys.stdout.buffer.write(content)
        sys.stdout.buffer.write(b"\n")
    else:
        _write_no_replace(output, content)


def _proposal(arguments: argparse.Namespace) -> int:
    payload = _parse(GcpPrivateCampaignProposalPayload, arguments.payload)
    proposal = issue_gcp_private_campaign_proposal(payload)
    _write_or_stdout(
        arguments.output, canonical_gcp_private_campaign_proposal_bytes(proposal)
    )
    return 0


def _startup(arguments: argparse.Namespace) -> int:
    payload = _parse(GcpPrivateCampaignStartupPayloadPayload, arguments.payload)
    startup = GcpPrivateCampaignStartupPayload(
        **payload.model_dump(mode="python"),
        startup_payload_id=gcp_private_campaign_startup_payload_id(payload),
    )
    _write_or_stdout(
        arguments.output, canonical_gcp_private_campaign_startup_bytes(startup)
    )
    return 0


def _preview(arguments: argparse.Namespace) -> int:
    proposal = _parse(GcpPrivateCampaignProposal, arguments.proposal)
    startup = _parse(GcpPrivateCampaignStartupPayload, arguments.startup)
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    _emit(
        {
            "create_request_digest": request.create_request_digest,
            "machine_type": proposal.topology.machine_type,
            "provider_call_performed": False,
            "proposal_id": proposal.proposal_id,
            "quote_is_controller_estimate_not_invoice": True,
            "startup_payload_digest": proposal.startup_payload_digest,
            "two_private_engines": True,
        }
    )
    return 0


def _controller(arguments: argparse.Namespace) -> GcpPrivateCampaignLifecycleController:
    journal = GcpPrivateCampaignJournal(arguments.journal_root.absolute())
    evidence_root = arguments.evidence_root.absolute()

    def launch_preflight(proposal: GcpPrivateCampaignProposal) -> SafeDirFD:
        return verify_gcp_private_campaign_evidence_destination(
            proposal, evidence_root=evidence_root
        )

    def transport_factory() -> object:
        # This defensive fallback is never selected by the CLI's disabled
        # ``execute`` command.  It also prevents a future accidental call to
        # this shared recovery controller from reintroducing a create-capable
        # Google factory before a separately reviewed private watchdog bridge
        # exists.  Recovery receives its dedicated no-create factory below.
        raise GcpPrivateCampaignError(_LIVE_EXECUTION_WATCHDOG_UNAVAILABLE)

    # ``recover-cleanup`` and ``discover-orphans`` select only this dedicated
    # no-create factory after cleanup-only authorization.  They never fall
    # back to ``transport_factory`` above.
    return GcpPrivateCampaignLifecycleController(
        journal=journal,
        launch_preflight=launch_preflight,
        transport_factory=transport_factory,
        cleanup_transport_factory=create_google_private_campaign_cleanup_transport,
        receipt_root=evidence_root,
    )


def _execute(arguments: argparse.Namespace) -> int:
    # Do not even parse a human approval or construct the lifecycle here: a
    # valid-looking artifact must not turn the unavailable two-A100 watchdog
    # design into a reachable provider boundary.  The explicit terminal state
    # below is emitted by ``main`` and truthfully states that no cleanup was
    # required because no provider action was reachable.
    del arguments
    raise GcpPrivateCampaignError(_LIVE_EXECUTION_WATCHDOG_UNAVAILABLE)


def _recover_cleanup(arguments: argparse.Namespace) -> int:
    proposal = _parse(GcpPrivateCampaignProposal, arguments.proposal)
    authorization = _parse(
        GcpPrivateCampaignCleanupAuthorization, arguments.authorization
    )
    startup = _parse(GcpPrivateCampaignStartupPayload, arguments.startup)
    outcome = _controller(arguments).recover_exact_cleanup(
        proposal=proposal, authorization=authorization, startup_payload=startup
    )
    _emit(
        {
            "cleanup_confirmed": outcome.confirmed,
            "journal_state": outcome.journal_state,
            "proposal_id": proposal.proposal_id,
            "retained_artifact_error": outcome.retained_artifact_error,
        }
    )
    return 0 if outcome.confirmed else 2


def _discover(arguments: argparse.Namespace) -> int:
    proposal = _parse(GcpPrivateCampaignProposal, arguments.proposal)
    authorization = _parse(
        GcpPrivateCampaignCleanupAuthorization, arguments.authorization
    )
    inventory = _controller(arguments).discover_exact_orphans(
        proposal=proposal, authorization=authorization
    )
    _emit(
        {
            "disk_count": len(inventory.disks),
            "instance_state": inventory.instance_state,
            "pagination_complete": inventory.pagination_complete,
            "proposal_id": proposal.proposal_id,
            "read_only": True,
        }
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="guarded GCP_PRIVATE two-A100 pre-campaign controller"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    proposal = commands.add_parser("proposal", help="issue a local proposal identity")
    proposal.add_argument("--payload", type=Path, required=True)
    proposal.add_argument("--output", type=Path)
    startup = commands.add_parser("startup", help="issue a local startup identity")
    startup.add_argument("--payload", type=Path, required=True)
    startup.add_argument("--output", type=Path)
    preview = commands.add_parser(
        "preview", help="validate and preview without provider access"
    )
    preview.add_argument("--proposal", type=Path, required=True)
    preview.add_argument("--startup", type=Path, required=True)

    def lifecycle_inputs(command: argparse.ArgumentParser) -> None:
        command.add_argument("--proposal", type=Path, required=True)
        command.add_argument("--startup", type=Path, required=True)
        command.add_argument("--journal-root", type=Path, required=True)
        command.add_argument("--evidence-root", type=Path, required=True)

    execute = commands.add_parser(
        "execute",
        help="fail closed: live execution awaits a reviewed two-A100 watchdog",
    )
    lifecycle_inputs(execute)
    execute.add_argument("--approval", type=Path, required=True)
    recovery = commands.add_parser(
        "recover-cleanup", help="run exact cleanup after a launch approval expires"
    )
    lifecycle_inputs(recovery)
    recovery.add_argument("--authorization", type=Path, required=True)
    discover = commands.add_parser(
        "discover-orphans", help="read-only label-scoped exact residual discovery"
    )
    lifecycle_inputs(discover)
    discover.add_argument("--authorization", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "proposal":
            return _proposal(arguments)
        if arguments.command == "startup":
            return _startup(arguments)
        if arguments.command == "preview":
            return _preview(arguments)
        if arguments.command == "execute":
            return _execute(arguments)
        if arguments.command == "recover-cleanup":
            return _recover_cleanup(arguments)
        if arguments.command == "discover-orphans":
            return _discover(arguments)
    except GcpPrivateCampaignError as error:
        response: dict[str, object] = {"error": error.code}
        if (
            arguments.command == "execute"
            and error.code == _LIVE_EXECUTION_WATCHDOG_UNAVAILABLE
        ):
            # Nothing has reached the controller, transport, or provider, so
            # reporting confirmed cleanup here would be false.  Make the
            # no-resource terminal state explicit instead.
            response.update(
                {
                    "cleanup_status": "NOT_REQUIRED",
                    "provider_call_performed": False,
                }
            )
        _emit(response)
        return 2
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
