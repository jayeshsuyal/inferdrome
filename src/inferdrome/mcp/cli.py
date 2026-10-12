"""Command-line entrypoint over the read-only MCP evidence functions.

`python -m inferdrome.mcp <command>` exposes the same read-only, verify-first
functions an MCP server will wrap, emitting structured JSON to stdout. It never
executes a run, mutates or fabricates evidence, or contacts a provider. This is
the invocable surface before the protocol server; the server will call the same
functions.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from inferdrome.mcp.runs import (
    RunStatus,
    compare_runs,
    get_run,
    list_runs,
    verify_evidence,
)

_STATUSES: tuple[RunStatus, ...] = ("RETRIEVED", "FAILED", "UNVERIFIED")


def _emit(payload: object) -> int:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inferdrome.mcp",
        description="Read-only access to reproducible inference evidence.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def with_root(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("--evidence-root", required=True)
        return sub

    listing = with_root(subparsers.add_parser("list-runs"))
    listing.add_argument("--model-id", default=None)
    listing.add_argument("--status", choices=_STATUSES, default=None)

    detail = with_root(subparsers.add_parser("get-run"))
    detail.add_argument("--run-id", required=True)

    verify = with_root(subparsers.add_parser("verify-evidence"))
    verify.add_argument("--run-id", required=True)

    comparison = with_root(subparsers.add_parser("compare-runs"))
    comparison.add_argument("--baseline", required=True)
    comparison.add_argument("--candidate", required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.evidence_root)
    try:
        if args.command == "list-runs":
            runs = list_runs(
                root,
                model_id=args.model_id,
                status=cast("RunStatus | None", args.status),
            )
            return _emit([run.model_dump(mode="json") for run in runs])
        if args.command == "get-run":
            return _emit(get_run(root, args.run_id).model_dump(mode="json"))
        if args.command == "verify-evidence":
            return _emit(
                verify_evidence(root, args.run_id).model_dump(mode="json")
            )
        if args.command == "compare-runs":
            comparison = compare_runs(root, args.baseline, args.candidate)
            return _emit(comparison.model_dump(mode="json"))
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyError as error:
        print(f"error: unknown run {error}", file=sys.stderr)
        return 2
    return 2  # pragma: no cover - argparse requires a known subcommand


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
