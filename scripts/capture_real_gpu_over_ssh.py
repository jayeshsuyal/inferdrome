#!/usr/bin/env python3
"""Run the pinned Inferdrome proof pack on one operator-provided SSH host."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__:
    from scripts import lambda_gpu_guard, real_gpu_capture
else:
    import lambda_gpu_guard
    import real_gpu_capture

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_DESTINATION_PATTERN = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_.-]*@)?(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:]+\])\Z"
)
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_DEFAULT_REMOTE_TIMEOUT_SECONDS = 9_900


class RemoteCaptureError(RuntimeError):
    """Expected, user-facing remote capture failure."""


def _run(
    arguments: Sequence[str],
    *,
    label: str,
    capture_output: bool = False,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RemoteCaptureError(f"{label} could not complete") from None
    if check and completed.returncode != 0:
        detail = ""
        if capture_output and completed.stderr:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            if len(detail) > 500:
                detail = detail[:500] + "…"
        suffix = f": {detail}" if detail else ""
        raise RemoteCaptureError(f"{label} failed{suffix}")
    return completed


def _git(*arguments: str) -> str:
    completed = _run(
        ["git", "-C", str(REPOSITORY_ROOT), *arguments],
        label="local Git inspection",
        capture_output=True,
        timeout=30,
    )
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise RemoteCaptureError("local Git output is not UTF-8") from None


def _validate_destination(value: str) -> str:
    if _DESTINATION_PATTERN.fullmatch(value) is None or value.startswith("-"):
        raise argparse.ArgumentTypeError(
            "SSH destination must be a simple user@host, hostname, IPv4, "
            "or bracketed IPv6"
        )
    return value


def _bounded_integer(
    value: str,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError(f"{label} must be an integer")
    selected = int(value)
    if not minimum <= selected <= maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return selected


def _port(value: str) -> int:
    return _bounded_integer(value, label="SSH port", minimum=1, maximum=65_535)


def _gpu_index(value: str) -> int:
    return _bounded_integer(value, label="GPU index", minimum=0, maximum=255)


def _startup_timeout(value: str) -> int:
    return _bounded_integer(
        value,
        label="startup timeout",
        minimum=1,
        maximum=3_600,
    )


def _remote_timeout(value: str) -> int:
    return _bounded_integer(
        value,
        label="remote timeout",
        minimum=1_800,
        maximum=10_800,
    )


def _require_checkout(expected_commit: str | None) -> str:
    if shutil.which("git") is None:
        raise RemoteCaptureError("git is required")
    commit = _git("rev-parse", "--verify", "HEAD")
    if _COMMIT_PATTERN.fullmatch(commit) is None:
        raise RemoteCaptureError("local HEAD is not a full lowercase Git commit")
    if expected_commit is not None:
        if _COMMIT_PATTERN.fullmatch(expected_commit) is None:
            raise RemoteCaptureError(
                "--expected-commit must be 40 lowercase hex digits"
            )
        if expected_commit != commit:
            raise RemoteCaptureError("local HEAD is not --expected-commit")
    if _git("status", "--porcelain", "--untracked-files=normal"):
        raise RemoteCaptureError("the local Inferdrome checkout must be clean")
    return commit


def _require_identity(path_text: str | None) -> Path | None:
    if path_text is None:
        return None
    path = Path(path_text).expanduser().absolute()
    try:
        metadata = os.lstat(path)
    except OSError:
        raise RemoteCaptureError("SSH identity file is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise RemoteCaptureError("SSH identity must be a regular, non-symlink file")
    return path


def _host_key_digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError(
            "host-key SHA-256 must be 64 lowercase hex digits"
        )
    return value


def _ssh_options(
    *,
    identity: Path | None,
    known_hosts: Path,
    port: int,
) -> list[str]:
    options = [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(port),
    ]
    if identity is not None:
        options.extend(["-i", str(identity)])
    return options


def _scp_options(
    *,
    identity: Path | None,
    known_hosts: Path,
    port: int,
) -> list[str]:
    options = _ssh_options(
        identity=identity,
        known_hosts=known_hosts,
        port=port,
    )
    port_index = options.index("-p")
    options[port_index] = "-P"
    return options


def _bash_command(script: str) -> str:
    return "bash -c " + shlex.quote(script)


def _remote_preflight_script(remote_root: str) -> str:
    quoted_root = shlex.quote(remote_root)
    return f"""set -euo pipefail
umask 077
[[ $(uname -s) == Linux ]]
for executable in bash curl git ninja python3.12 nvidia-smi sha256sum tar timeout; do
  command -v "$executable" >/dev/null || {{
    echo "missing required host executable: $executable" >&2
    exit 1
  }}
done
python3.12 - <<'PY'
from pathlib import Path
import sysconfig

include = sysconfig.get_path("include")
if not include or not (Path(include) / "Python.h").is_file():
    raise SystemExit("missing Python.h; install the Python 3.12 development headers")
PY
[[ ! -e {quoted_root} && ! -L {quoted_root} ]]
mkdir -m 700 -- {quoted_root}
nvidia-smi --query-gpu=index,name,uuid,driver_version --format=csv,noheader,nounits
"""


def _remote_capture_script(
    remote_root: str,
    commit: str,
    *,
    gpu_index: int,
    startup_timeout_seconds: int,
    remote_timeout_seconds: int,
) -> str:
    root = shlex.quote(remote_root)
    expected = shlex.quote(commit)
    return f"""set -euo pipefail
umask 077
git clone --quiet {root}/repo.bundle {root}/repo
cd {root}/repo
[[ $(git rev-parse --verify HEAD) == {expected} ]]
[[ -z $(git status --porcelain --untracked-files=normal) ]]
unset CUDA_VISIBLE_DEVICES NVIDIA_VISIBLE_DEVICES
timeout --foreground --signal=TERM --kill-after=60s {remote_timeout_seconds}s \\
  ./scripts/run_real_gpu_capture.sh \\
  --state-root {root}/state \\
  --capture-root {root}/capture \\
  --gpu-index {gpu_index} \\
  --startup-timeout-seconds {startup_timeout_seconds}
"""


def _write_json(path: Path, value: dict[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o444)
    except OSError:
        raise RemoteCaptureError(f"could not publish {path.name}") from None


def _checksum_file(path: Path) -> str:
    try:
        content = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        raise RemoteCaptureError("remote archive checksum is unreadable") from None
    lines = content.splitlines()
    if len(lines) != 1:
        raise RemoteCaptureError("remote archive checksum has an invalid shape")
    fields = lines[0].split()
    if len(fields) != 2 or re.fullmatch(r"[0-9a-f]{64}", fields[0]) is None:
        raise RemoteCaptureError("remote archive checksum is invalid")
    if Path(fields[1].lstrip("*")).name != "capture.tar.gz":
        raise RemoteCaptureError("remote checksum names an unexpected archive")
    return "sha256:" + fields[0]


def _host_identity_digest(known_hosts: Path) -> str:
    try:
        content = known_hosts.read_bytes()
    except OSError:
        raise RemoteCaptureError("SSH host identity record is unavailable") from None
    if not content or len(content) > 1_048_576:
        raise RemoteCaptureError("SSH host identity record is invalid")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _static_check() -> None:
    required = [
        REPOSITORY_ROOT / "scripts" / "prepare_real_gpu_host.sh",
        REPOSITORY_ROOT / "scripts" / "run_real_gpu_capture.sh",
        REPOSITORY_ROOT / "scripts" / "run_real_gpu_demo.py",
        REPOSITORY_ROOT / "scripts" / "real_gpu_capture.py",
        REPOSITORY_ROOT / "scripts" / "lambda_gpu_guard.py",
    ]
    for path in required:
        if not path.is_file() or path.is_symlink():
            raise RemoteCaptureError(
                f"required capture asset is unavailable: {path.name}"
            )


def _destination_host(destination: str) -> str:
    host = destination.rsplit("@", 1)[-1]
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _lambda_guard_requested(args: argparse.Namespace) -> bool:
    selected = (
        args.lambda_instance_id,
        args.lambda_hourly_rate_usd,
        args.max_cost_usd,
        args.lambda_billing_started_at,
    )
    if not any(value is not None for value in selected):
        return False
    if args.lambda_hourly_rate_usd is None or args.max_cost_usd is None:
        raise RemoteCaptureError(
            "Lambda protection requires --lambda-hourly-rate-usd and --max-cost-usd"
        )
    return True


def _arm_lambda_watchdog(
    args: argparse.Namespace,
) -> lambda_gpu_guard.WatchdogHandle | None:
    if not _lambda_guard_requested(args):
        return None
    host = _destination_host(args.destination)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        expected_endpoint = None
    else:
        expected_endpoint = host
    reference = args.lambda_instance_id or host
    started_at = args.lambda_billing_started_at or datetime.now(UTC)
    try:
        handle = lambda_gpu_guard.arm_watchdog(
            reference,
            hourly_rate_usd=args.lambda_hourly_rate_usd,
            max_cost_usd=args.max_cost_usd,
            billing_started_at=started_at,
            state_root=Path(args.lambda_guard_state_root),
            expected_endpoint=expected_endpoint,
        )
    except lambda_gpu_guard.LambdaGuardError as error:
        raise RemoteCaptureError(
            f"Lambda cost guard could not be armed: {error}"
        ) from None
    print(
        "Lambda termination watchdog armed for "
        f"{lambda_gpu_guard._timestamp(handle.cost_window.deadline)}."
    )
    print(f"Lambda guard state: {handle.state_directory}")
    return handle


def _dry_run(args: argparse.Namespace, commit: str, identity: Path | None) -> None:
    remote_root = f"/tmp/inferdrome-gpu-{commit[:12]}-<random>"
    guarded = _lambda_guard_requested(args)
    plan = {
        "billing_boundary": (
            "LAMBDA API TERMINATION WATCHDOG PLUS CONTROLLER FINALLY"
            if guarded
            else "OPERATOR MUST TERMINATE THE CLOUD INSTANCE"
        ),
        "destination": args.destination,
        "gpu_index": args.gpu_index,
        "identity_file_configured": identity is not None,
        "lambda_cost_guard": (
            {
                "billing_started_at": (
                    lambda_gpu_guard._timestamp(args.lambda_billing_started_at)
                    if args.lambda_billing_started_at is not None
                    else "CONTROLLER_START"
                ),
                "hourly_rate_usd": str(args.lambda_hourly_rate_usd),
                "instance_reference": args.lambda_instance_id
                or _destination_host(args.destination),
                "max_cost_usd": str(args.max_cost_usd),
            }
            if guarded
            else None
        ),
        "repository_commit": commit,
        "remote_root": remote_root,
        "remote_timeout_seconds": args.remote_timeout_seconds,
        "startup_timeout_seconds": args.startup_timeout_seconds,
        "steps": [
            "verify a clean exact local commit",
            "create and upload a Git bundle of that commit",
            "preflight Linux, Python 3.12, NVIDIA, and required host tools",
            "prepare the pinned vLLM/model environment",
            "run the single proof and four-run controlled comparison",
            "download a SHA-256-anchored capture archive",
            "independently verify every retrieved proof artifact locally",
            (
                "terminate and confirm the Lambda instance through its API"
                if guarded
                else "prompt the operator to terminate the billable instance"
            ),
        ],
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))


def _capture_over_ssh(
    args: argparse.Namespace,
    commit: str,
    identity: Path | None,
) -> Path:
    for executable in ("ssh", "scp", "git"):
        if shutil.which(executable) is None:
            raise RemoteCaptureError(f"{executable} is required")
    output_root = Path(args.output_root).expanduser().absolute()
    output_root.mkdir(parents=True, exist_ok=True)
    token = os.urandom(4).hex()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{timestamp}-{commit[:12]}-{token}"
    final_path = output_root / name
    staging_path = output_root / f".{name}.staging"
    if final_path.exists() or staging_path.exists():
        raise RemoteCaptureError("local capture destination already exists")
    staging_path.mkdir(mode=0o700)
    known_hosts = staging_path / "ssh-known-hosts"
    archive = staging_path / "capture.tar.gz"
    checksum = staging_path / "capture.tar.gz.sha256"
    remote_root = f"/tmp/inferdrome-gpu-{commit[:12]}-{token}"
    pinned_host_key = (
        bytes.fromhex(args.host_key_sha256)
        if args.host_key_sha256 is not None
        else None
    )
    ssh_options = _ssh_options(
        identity=identity,
        known_hosts=known_hosts,
        port=args.port,
    )
    scp_options = _scp_options(
        identity=identity,
        known_hosts=known_hosts,
        port=args.port,
    )

    print(f"Inferdrome commit: {commit}")
    print(f"Remote workspace: {remote_root}")
    print("Preflighting the operator-provided GPU host…", flush=True)
    _run(
        [
            "ssh",
            *ssh_options,
            args.destination,
            _bash_command(_remote_preflight_script(remote_root)),
        ],
        label="remote GPU preflight",
        timeout=90,
    )
    observed_host_identity_sha256 = _host_identity_digest(known_hosts)
    if (
        pinned_host_key is not None
        and hashlib.sha256(known_hosts.read_bytes()).digest() != pinned_host_key
    ):
        raise RemoteCaptureError("SSH host identity does not match its expected digest")
    try:
        with tempfile.TemporaryDirectory(prefix="inferdrome-git-bundle-") as temporary:
            bundle = Path(temporary) / "repo.bundle"
            _run(
                [
                    "git",
                    "-C",
                    str(REPOSITORY_ROOT),
                    "bundle",
                    "create",
                    str(bundle),
                    "HEAD",
                ],
                label="exact Git bundle creation",
                timeout=120,
            )
            _run(
                ["git", "bundle", "verify", str(bundle)],
                label="exact Git bundle verification",
                capture_output=True,
                timeout=120,
            )
            _run(
                [
                    "scp",
                    *scp_options,
                    str(bundle),
                    f"{args.destination}:{remote_root}/repo.bundle",
                ],
                label="Git bundle upload",
                timeout=600,
            )

        print(
            "Running the bounded proof pack; model preparation is usually "
            "the slowest step…",
            flush=True,
        )
        remote_result = _run(
            [
                "ssh",
                *ssh_options,
                args.destination,
                _bash_command(
                    _remote_capture_script(
                        remote_root,
                        commit,
                        gpu_index=args.gpu_index,
                        startup_timeout_seconds=args.startup_timeout_seconds,
                        remote_timeout_seconds=args.remote_timeout_seconds,
                    )
                ),
            ],
            label="remote proof pack",
            timeout=args.remote_timeout_seconds + 600,
            check=False,
        )
        retrieval_error: RemoteCaptureError | None = None
        try:
            for remote_name, local_path in (
                ("capture.tar.gz", archive),
                ("capture.tar.gz.sha256", checksum),
            ):
                _run(
                    [
                        "scp",
                        *scp_options,
                        f"{args.destination}:{remote_root}/{remote_name}",
                        str(local_path),
                    ],
                    label=f"{remote_name} retrieval",
                    timeout=1_800,
                )
        except RemoteCaptureError as error:
            retrieval_error = error
        if retrieval_error is not None:
            raise retrieval_error

        expected_archive_sha256 = _checksum_file(checksum)
        try:
            actual_archive_sha256 = real_gpu_capture.archive_sha256(archive)
            if actual_archive_sha256 != expected_archive_sha256:
                raise RemoteCaptureError(
                    "retrieved archive failed SHA-256 verification"
                )
            capture_root = real_gpu_capture.extract_capture_archive(
                archive,
                staging_path,
            )
        except real_gpu_capture.CaptureError as error:
            raise RemoteCaptureError(str(error)) from None
        if remote_result.returncode != 0:
            failure_path = output_root / f"{name}-FAILED"
            os.replace(staging_path, failure_path)
            raise RemoteCaptureError(
                "remote proof pack failed; retained diagnostics at "
                f"{failure_path} (remote workspace {remote_root})"
            )
        try:
            verification = real_gpu_capture.verify_capture(
                capture_root,
                expected_repository_commit=commit,
            )
        except real_gpu_capture.CaptureError as error:
            failure_path = output_root / f"{name}-UNVERIFIED"
            os.replace(staging_path, failure_path)
            raise RemoteCaptureError(
                f"retrieved capture failed local verification: {error}; "
                f"retained at {failure_path}"
            ) from None
        retrieval = {
            "archive_sha256": actual_archive_sha256,
            "billing_action_required": "TERMINATE_THE_GPU_INSTANCE",
            "capture_manifest_sha256": real_gpu_capture.archive_sha256(
                capture_root / "capture-manifest.json"
            ),
            "repository_commit": commit,
            "schema_version": "inferdrome.real-gpu-retrieval.v1",
            "ssh_host_identity_sha256": observed_host_identity_sha256,
            "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "verification": verification,
        }
        _write_json(staging_path / "retrieval-receipt.json", retrieval)
        os.replace(staging_path, final_path)
    except Exception:
        if staging_path.exists():
            print(
                f"Partial local diagnostics remain at {staging_path}",
                file=sys.stderr,
            )
        raise

    if _lambda_guard_requested(args):
        print("\nCAPTURE VERIFIED. LAMBDA TERMINATION IS BEING CONFIRMED.")
    else:
        print("\nCAPTURE VERIFIED. TERMINATE THE BILLABLE GPU INSTANCE NOW.")
    print(f"Local capture record: {final_path}")
    print(f"Remote workspace retained until instance termination: {remote_root}")
    comparison_roots = sorted(
        (final_path / "capture" / "comparison").glob(
            "real-gpu-comparison-*"
        )
    )
    if len(comparison_roots) == 1:
        comparison_root = comparison_roots[0]
        dashboard_executable = shutil.which("inferdrome") or "inferdrome"
        dashboard_arguments = [
            dashboard_executable,
            "dashboard",
            "--runs-root",
            str(comparison_root / "runs"),
            "--trial-sets-root",
            str(comparison_root / "trial-sets"),
            "--comparison-plans-root",
            str(comparison_root / "comparison-plans"),
            "--comparison-results-root",
            str(comparison_root / "comparison-results"),
            "--open",
        ]
        print("Dashboard inspection command:")
        print("  " + shlex.join(dashboard_arguments))
    return final_path


def _capture(args: argparse.Namespace, commit: str, identity: Path | None) -> Path:
    watchdog = _arm_lambda_watchdog(args)
    try:
        return _capture_over_ssh(args, commit, identity)
    finally:
        if watchdog is not None:
            capture_failed = sys.exc_info()[0] is not None
            try:
                result = lambda_gpu_guard.terminate_guarded_instance(watchdog)
            except lambda_gpu_guard.LambdaGuardError as error:
                print(
                    "CRITICAL: immediate Lambda termination was not confirmed; "
                    f"the deadline watchdog remains armed: {error}",
                    file=sys.stderr,
                )
                if not capture_failed:
                    raise RemoteCaptureError(
                        "capture completed, but Lambda termination was not confirmed"
                    ) from None
            else:
                print(
                    "Lambda termination confirmed: "
                    f"{watchdog.instance.instance_id} ({result.final_status})."
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture the Inferdrome real-GPU proof pack over SSH"
    )
    parser.add_argument("destination", nargs="?", type=_validate_destination)
    parser.add_argument("--check", action="store_true", help="check local assets only")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the bounded workflow without contacting a host",
    )
    parser.add_argument("--expected-commit")
    parser.add_argument("--identity-file")
    parser.add_argument(
        "--host-key-sha256",
        type=_host_key_digest,
        help="optional SHA-256 hex digest of the capture-specific known_hosts bytes",
    )
    parser.add_argument("--port", type=_port, default=22)
    parser.add_argument("--gpu-index", type=_gpu_index, default=0)
    parser.add_argument(
        "--startup-timeout-seconds",
        type=_startup_timeout,
        default=900,
    )
    parser.add_argument(
        "--remote-timeout-seconds",
        type=_remote_timeout,
        default=_DEFAULT_REMOTE_TIMEOUT_SECONDS,
        help="bounded host workload time; this does not terminate the cloud instance",
    )
    parser.add_argument(
        "--output-root",
        default=str(REPOSITORY_ROOT / "gpu-proof-retrieved"),
    )
    parser.add_argument(
        "--lambda-instance-id",
        type=lambda_gpu_guard.parse_instance_id,
        help=(
            "optional Lambda instance ID; otherwise resolve the SSH host "
            "through the API"
        ),
    )
    parser.add_argument(
        "--lambda-hourly-rate-usd",
        type=lambda_gpu_guard.parse_hourly_rate,
        help="displayed Lambda hourly rate; checked against the API",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=lambda_gpu_guard.parse_max_cost,
        help="maximum spend from --lambda-billing-started-at or controller start",
    )
    parser.add_argument(
        "--lambda-billing-started-at",
        type=lambda_gpu_guard.parse_utc_timestamp,
        help="actual billing start in ISO 8601; defaults to controller start",
    )
    parser.add_argument(
        "--lambda-guard-state-root",
        default=str(Path.home() / ".inferdrome" / "lambda-guards"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        _static_check()
        if args.check:
            if (
                args.destination is not None
                or args.dry_run
                or _lambda_guard_requested(args)
            ):
                raise RemoteCaptureError(
                    "--check does not accept a destination, --dry-run, or Lambda guard"
                )
            print("real-GPU remote capture assets: OK")
            return 0
        if args.destination is None:
            raise RemoteCaptureError("an SSH destination is required")
        identity = _require_identity(args.identity_file)
        commit = _require_checkout(args.expected_commit)
        if args.dry_run:
            _dry_run(args, commit, identity)
        else:
            _capture(args, commit, identity)
    except RemoteCaptureError as error:
        print(f"remote-real-gpu-capture: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
