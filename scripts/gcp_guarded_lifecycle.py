#!/usr/bin/env python3
"""Offline GCP arm/preview/fake-controller command.

There is intentionally no ``execute`` subcommand in PR8.  A future live command
must select an explicit transport after all gates and operator authorization;
this script can only issue/verify an arm, preview a private request, or run the
injected deterministic fake.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from inferdrome.deployment import (
    DeploymentSpec,
    FakeGcpComputeTransport,
    GcpCapacityInput,
    GcpCostQuote,
    GcpDryRunPlan,
    GcpExecutionEnvironment,
    GcpGuardedLifecycleController,
    GcpInventorySnapshot,
    GcpLeaseJournal,
    GcpPlanningContext,
    InMemoryExecutionArmStore,
    canonical_gcp_execution_arm_bytes,
    canonical_gcp_execution_outcome_bytes,
    gcp_execution_arm_id,
    gcp_execution_arm_sha256,
    issue_gcp_execution_arm,
    parse_deployment_spec_json,
    parse_gcp_execution_arm_json,
    parse_gcp_inventory_json,
    plan_gcp_dry_run,
    verify_gcp_execution_arm_bytes,
)

MAX_INPUT_BYTES: Final = 524_288


def _read_regular(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_INPUT_BYTES:
            raise ValueError
        content = os.read(descriptor, MAX_INPUT_BYTES + 1)
        final = os.fstat(descriptor)
        if (
            len(content) > MAX_INPUT_BYTES
            or final.st_ino != metadata.st_ino
            or final.st_size != len(content)
        ):
            raise ValueError
        return content
    except (OSError, ValueError):
        raise ValueError("GCP execution input is unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_inputs(
    args: argparse.Namespace,
) -> tuple[DeploymentSpec, GcpInventorySnapshot, GcpPlanningContext, GcpDryRunPlan]:
    spec = parse_deployment_spec_json(_read_regular(args.deployment_spec))
    inventory = parse_gcp_inventory_json(_read_regular(args.inventory))
    context = GcpPlanningContext.model_validate_json(_read_regular(args.context))
    plan = plan_gcp_dry_run(spec, inventory, context)
    if args.plan is not None:
        plan = GcpDryRunPlan.model_validate_json(_read_regular(args.plan))
    return spec, inventory, context, plan


def _publish_no_replace(path: Path, content: bytes) -> None:
    parent = path.parent
    stage = parent / f".{path.name}.{os.getpid()}.stage"
    descriptor: int | None = None
    try:
        metadata = parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError
        descriptor = os.open(
            stage,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(stage, path)
        stage.unlink()
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, ValueError):
        raise ValueError("GCP execution output is unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if stage.exists() or stage.is_symlink():
                stage.unlink()
        except OSError:
            pass


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _arm(args: argparse.Namespace) -> int:
    spec, inventory, context, plan = _load_inputs(args)
    arm = issue_gcp_execution_arm(
        plan=plan,
        expected_spec=spec,
        expected_inventory=inventory,
        expected_context=context,
        controller_id=args.controller_id,
        nonce=args.nonce,
        issued_at=_parse_time(args.issued_at),
        max_controller_duration_seconds=args.duration,
        confirmation=args.confirmation,
    )
    content = canonical_gcp_execution_arm_bytes(arm)
    if args.output is None:
        sys.stdout.buffer.write(content)
        sys.stdout.buffer.write(b"\n")
    else:
        _publish_no_replace(args.output, content)
        print(
            json.dumps(
                {"arm_id": arm.arm_id, "arm_sha256": gcp_execution_arm_sha256(arm)},
                sort_keys=True,
            )
        )
    return 0


def _verify(args: argparse.Namespace) -> int:
    spec, inventory, context, plan = _load_inputs(args)
    arm_bytes = _read_regular(args.arm)
    arm = verify_gcp_execution_arm_bytes(
        arm_bytes,
        expected_plan=plan,
        expected_spec=spec,
        expected_inventory=inventory,
        expected_context=context,
        now=_parse_time(args.now),
        expected_controller_id=args.controller_id,
    )
    print(
        json.dumps(
            {"valid": True, "arm_id": gcp_execution_arm_id(arm), "one_shot": True},
            sort_keys=True,
        )
    )
    return 0


def _fake_run(args: argparse.Namespace) -> int:
    spec, inventory, context, plan = _load_inputs(args)
    arm_bytes = _read_regular(args.arm)
    arm = parse_gcp_execution_arm_json(arm_bytes)
    environment = GcpExecutionEnvironment.model_validate_json(
        _read_regular(args.environment)
    )
    quote = GcpCostQuote.model_validate_json(_read_regular(args.quote))
    capacity = GcpCapacityInput.model_validate_json(_read_regular(args.capacity))
    now = _parse_time(args.now)
    from inferdrome.deployment.gcp_lifecycle import GcpClock

    controller = GcpGuardedLifecycleController(
        transport=FakeGcpComputeTransport(),
        arm_store=InMemoryExecutionArmStore(),
        journal=GcpLeaseJournal(args.journal),
        clock=GcpClock(now_fn=lambda: now, monotonic_fn=lambda: 0.0),
    )
    outcome = controller.execute(
        plan=plan,
        expected_spec=spec,
        expected_inventory=inventory,
        expected_context=context,
        arm_bytes=canonical_gcp_execution_arm_bytes(arm),
        environment=environment,
        quote=quote,
        capacity=capacity,
    )
    content = canonical_gcp_execution_outcome_bytes(outcome)
    if args.output is None:
        sys.stdout.buffer.write(content)
        sys.stdout.buffer.write(b"\n")
    else:
        _publish_no_replace(args.output, content)
    return 0 if outcome.status == "SUCCEEDED" else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="offline guarded GCP lifecycle boundary"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_inputs(
        command: argparse.ArgumentParser, *, with_plan: bool = False
    ) -> None:
        command.add_argument("--deployment-spec", type=Path, required=True)
        command.add_argument("--inventory", type=Path, required=True)
        command.add_argument("--context", type=Path, required=True)
        if with_plan:
            command.add_argument("--plan", type=Path)

    arm = subparsers.add_parser("arm")
    add_inputs(arm, with_plan=True)
    arm.add_argument("--controller-id", required=True)
    arm.add_argument("--nonce", required=True)
    arm.add_argument("--issued-at", required=True)
    arm.add_argument("--duration", type=int, required=True)
    arm.add_argument("--confirmation", required=True)
    arm.add_argument("--output", type=Path)
    verify = subparsers.add_parser("verify-arm")
    add_inputs(verify, with_plan=True)
    verify.add_argument("--arm", type=Path, required=True)
    verify.add_argument("--now", required=True)
    verify.add_argument("--controller-id")
    fake = subparsers.add_parser("fake-run")
    add_inputs(fake, with_plan=True)
    fake.add_argument("--arm", type=Path, required=True)
    fake.add_argument("--environment", type=Path, required=True)
    fake.add_argument("--quote", type=Path, required=True)
    fake.add_argument("--capacity", type=Path, required=True)
    fake.add_argument("--journal", type=Path, required=True)
    fake.add_argument("--now", required=True)
    fake.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "arm":
            return _arm(args)
        if args.command == "verify-arm":
            return _verify(args)
        if args.command == "fake-run":
            return _fake_run(args)
        return 2
    except Exception:
        print("gcp guarded lifecycle failed: invalid offline input", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
