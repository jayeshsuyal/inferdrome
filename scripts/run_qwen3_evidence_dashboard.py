#!/usr/bin/env python3
"""Verify the reviewed Qwen3 A10 archive and open its read-only dashboard."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import scripts.qwen3_gpu_capture as qwen3_capture
    import scripts.real_gpu_capture as capture
    import scripts.review_qwen3_gpu_evidence_publication as publication
except ModuleNotFoundError:  # Direct script execution adds scripts/, not the repo root.
    import qwen3_gpu_capture as qwen3_capture  # type: ignore[no-redef]
    import real_gpu_capture as capture  # type: ignore[no-redef]
    import review_qwen3_gpu_evidence_publication as publication  # type: ignore[no-redef]


class Qwen3DashboardError(RuntimeError):
    """The reviewed real-GPU dashboard could not be opened safely."""


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 1 <= parsed <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def dashboard_command(runs_root: Path, *, port: int, open_browser: bool) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "inferdrome",
        "dashboard",
        "--runs-root",
        str(runs_root),
        "--port",
        str(port),
    ]
    if open_browser:
        command.append("--open")
    return command


def _require_published_outputs(outputs: dict[Path, bytes]) -> None:
    mismatches = [
        path.relative_to(publication.REPOSITORY_ROOT).as_posix()
        for path, expected in outputs.items()
        if not path.exists() or path.read_bytes() != expected
    ]
    if mismatches:
        raise Qwen3DashboardError(
            "reviewed publication metadata is stale or missing: "
            + ", ".join(mismatches)
        )


def verify_dashboard_source(
    archive: Path,
    capture_record: Path,
) -> dict[Path, bytes]:
    """Require exact archive, post-termination receipts, and committed anchors."""

    try:
        outputs = publication.render_outputs(archive, capture_record)
    except (
        OSError,
        publication.PublicationReviewError,
        capture.CaptureError,
        qwen3_capture.Qwen3CaptureError,
    ) as error:
        raise Qwen3DashboardError(str(error)) from None
    _require_published_outputs(outputs)
    return outputs


def _check(archive: Path, capture_record: Path) -> int:
    verify_dashboard_source(archive, capture_record)
    print(
        json.dumps(
            {
                "archive_sha256": publication.ARCHIVE_SHA256,
                "bundle_digest": publication.BUNDLE_DIGEST,
                "dashboard_source": "VERIFIED_REVIEWED_QWEN3_A10_CAPTURE",
                "publication_status": "EXTERNAL_ONLY",
                "run_id": publication.RUN_ID,
                "semantic_verification": "VALID_AFTER_PROVIDER_TERMINATION",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _launch(
    archive: Path,
    capture_record: Path,
    *,
    port: int,
    open_browser: bool,
) -> int:
    verify_dashboard_source(archive, capture_record)
    with tempfile.TemporaryDirectory(
        prefix="inferdrome-qwen3-evidence-dashboard-"
    ) as temporary:
        capture_root: Path | None = None
        try:
            capture_root = capture.extract_capture_archive(
                archive.absolute(),
                Path(temporary),
            )
            verification = qwen3_capture.verify_capture(
                capture_root,
                expected_repository_commit=publication.CAPTURE_PRODUCER_COMMIT,
            )
            if verification["run"]["run_id"] != publication.RUN_ID:
                raise Qwen3DashboardError("verified dashboard run identity drifted")
            command = dashboard_command(
                capture_root / "runs",
                port=port,
                open_browser=open_browser,
            )
            print(
                "Opening reviewed genuine-GPU evidence. This is a bounded A10 "
                "observation, not an acceptance verdict or cross-GPU comparison.",
                flush=True,
            )
            return subprocess.run(command, check=False).returncode
        finally:
            if capture_root is not None:
                capture._make_directories_writable_for_cleanup(capture_root)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the reviewed Qwen3-8B A10 capture and open the local dashboard"
        )
    )
    parser.add_argument(
        "--archive",
        default=str(publication.DEFAULT_ARCHIVE),
        help="exact EXTERNAL_ONLY capture archive",
    )
    parser.add_argument(
        "--capture-record",
        default=str(publication.DEFAULT_CAPTURE_RECORD),
        help="retrieved record containing post-termination receipts",
    )
    parser.add_argument("--port", type=_port, default=8787)
    parser.add_argument("--open", action="store_true", help="open a browser tab")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the exact source and committed metadata without serving",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.check:
            return _check(Path(args.archive), Path(args.capture_record))
        return _launch(
            Path(args.archive),
            Path(args.capture_record),
            port=args.port,
            open_browser=args.open,
        )
    except Qwen3DashboardError as error:
        print(f"Qwen3 evidence dashboard: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
