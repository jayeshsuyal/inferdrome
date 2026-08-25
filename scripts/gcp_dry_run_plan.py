#!/usr/bin/env python3
"""Offline GCP dry-run plan and verification command.

This command reads only the supplied deployment, inventory, and explicit
compute-scope files.  It never contacts GCP or resolves credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Final

from inferdrome.deployment import (
    DeploymentSpec,
    GcpDryRunPlan,
    GcpInventorySnapshot,
    GcpPlanError,
    GcpPlanningContext,
    canonical_gcp_plan_bytes,
    gcp_plan_id,
    gcp_plan_sha256,
    parse_deployment_spec_json,
    parse_gcp_inventory_json,
    plan_gcp_dry_run,
    publish_gcp_dry_run_plan,
    verify_gcp_dry_run_plan_bytes,
)

MAX_INPUT_BYTES: Final = 524_288


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink < 1:
            raise ValueError("input is not a regular file")
        if metadata.st_size > MAX_INPUT_BYTES:
            raise ValueError("input exceeds its bound")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_INPUT_BYTES:
            chunk = os.read(descriptor, min(65_536, MAX_INPUT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        content = b"".join(chunks)
        final_metadata = os.fstat(descriptor)
        if (
            final_metadata.st_dev != metadata.st_dev
            or final_metadata.st_ino != metadata.st_ino
            or final_metadata.st_size != len(content)
            or len(content) > MAX_INPUT_BYTES
        ):
            raise ValueError("input changed during read")
        return content
    except (OSError, ValueError):
        raise ValueError("GCP plan input is unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_context(path: Path) -> GcpPlanningContext:
    return GcpPlanningContext.model_validate_json(_read_regular(path))


def _load_inputs(
    args: argparse.Namespace,
) -> tuple[DeploymentSpec, GcpInventorySnapshot, GcpPlanningContext]:
    spec = parse_deployment_spec_json(_read_regular(args.deployment_spec))
    inventory = parse_gcp_inventory_json(_read_regular(args.inventory))
    context = _load_context(args.context)
    return spec, inventory, context


def _metadata(plan: GcpDryRunPlan) -> str:
    return json.dumps(
        {
            "plan_id": gcp_plan_id(plan),
            "plan_sha256": gcp_plan_sha256(plan),
            "valid": True,
            "execution_authorized": False,
            "provider_mutation_performed": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="offline GCP dry-run planning")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    verify = subparsers.add_parser("verify")
    for command in (plan, verify):
        command.add_argument("--deployment-spec", type=Path, required=True)
        command.add_argument("--inventory", type=Path, required=True)
        command.add_argument("--context", type=Path, required=True)
    plan.add_argument("--output-root", type=Path)
    verify.add_argument("--plan", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        spec, inventory, context = _load_inputs(args)
        if args.command == "plan":
            plan = plan_gcp_dry_run(spec, inventory, context)
            if args.output_root is None:
                sys.stdout.buffer.write(canonical_gcp_plan_bytes(plan))
                sys.stdout.buffer.write(b"\n")
            else:
                published = publish_gcp_dry_run_plan(
                    root=args.output_root,
                    plan=plan,
                    expected_spec=spec,
                    expected_inventory=inventory,
                    expected_context=context,
                )
                print(_metadata(published.plan))
        else:
            plan_bytes = _read_regular(args.plan)
            plan = verify_gcp_dry_run_plan_bytes(
                plan_bytes,
                expected_spec=spec,
                expected_inventory=inventory,
                expected_context=context,
            )
            print(_metadata(plan))
        return 0
    except (GcpPlanError, ValueError, OSError, TypeError):
        print("gcp dry-run plan failed: invalid input", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
