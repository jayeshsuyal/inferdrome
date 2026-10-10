"""External, exact-instance Vast cleanup backstop for an approved pilot.

This module can destroy one explicitly authorized instance. It never rents,
creates, starts or discovers an offer. The offline pilot packet is not approval.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from inferdrome.vllm_router_gpu import IMAGE

_APPROVAL_KEYS = {
    "schema",
    "execution_authorized",
    "provider",
    "instance_id",
    "billing_start_utc",
    "max_session_s",
    "cleanup_reserve_s",
    "hourly_rate_usd",
    "cap_usd",
    "reserve_usd",
    "storage_allowance_usd",
    "transfer_allowance_usd",
    "plan_sha256",
    "source_revision",
    "image_reference",
    "guardian_host_kind",
}
_MONEY_KEYS = (
    "hourly_rate_usd",
    "cap_usd",
    "reserve_usd",
    "storage_allowance_usd",
    "transfer_allowance_usd",
)
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_COMMAND_TIMEOUT_S = 30
_RETRY_INTERVAL_S = 5


class GuardError(ValueError):
    """Approval, external guardian, or provider readback is unconfirmed."""


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _utc(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ):
        raise GuardError("expected a UTC timestamp with whole seconds and Z")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GuardError("invalid UTC timestamp") from error


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GuardError("duplicate JSON key")
        result[key] = value
    return result


def _json(data: bytes) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_object_pairs)
    except (ValueError, UnicodeError) as error:
        raise GuardError("invalid or ambiguous JSON") from error


def load_approval(path: Path) -> dict[str, Any]:
    """Read a bounded local approval; this performs no provider operation."""
    if path.is_symlink() or not path.is_file():
        raise GuardError("approval must be a regular file")
    with path.open("rb") as stream:
        data = stream.read(65_537)
    if len(data) > 65_536:
        raise GuardError("approval exceeds the size limit")
    return parse_approval(_json(data))


def parse_approval(value: object) -> dict[str, Any]:
    """Validate exact paid authorization, never the undecided planning template."""
    if not isinstance(value, dict) or set(value) != _APPROVAL_KEYS:
        raise GuardError("approval fields differ from the exact pilot contract")
    if (
        value["schema"] != "inferdrome.breakpoint-pilot-approval.v1"
        or value["execution_authorized"] is not True
        or value["provider"] != "vast"
        or value["guardian_host_kind"] != "EXTERNAL_OPERATOR_HOST"
        or value["image_reference"] != IMAGE
    ):
        raise GuardError("pilot execution and external cleanup are not authorized")
    for key, pattern in (
        ("instance_id", r"[1-9][0-9]{0,17}"),
        ("source_revision", r"[0-9a-f]{40}"),
        ("plan_sha256", r"sha256:[0-9a-f]{64}"),
    ):
        if not isinstance(value[key], str) or not re.fullmatch(pattern, value[key]):
            raise GuardError(f"invalid {key}")
    _utc(value["billing_start_utc"])
    for key, low, high in (
        ("max_session_s", 601, 12_600),
        ("cleanup_reserve_s", 600, 1_800),
    ):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise GuardError(f"invalid {key}")
    if value["cleanup_reserve_s"] >= value["max_session_s"]:
        raise GuardError("cleanup reserve consumes the whole session")
    money: dict[str, Decimal] = {}
    for key in _MONEY_KEYS:
        item = value[key]
        if not isinstance(item, str) or not re.fullmatch(
            r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,6})?", item
        ):
            raise GuardError(f"invalid decimal USD value: {key}")
        money[key] = Decimal(item)
    if money["hourly_rate_usd"] <= 0 or money["cap_usd"] <= 0:
        raise GuardError("hourly rate and cap must be positive")
    planned = (
        money["hourly_rate_usd"] * value["max_session_s"] / Decimal(3600)
        + money["storage_allowance_usd"]
        + money["transfer_allowance_usd"]
        + money["reserve_usd"]
    )
    if planned > money["cap_usd"]:
        raise GuardError("quoted compute and other allowances exceed the USD cap")
    return dict(value)


def approval_sha256(approval: dict[str, Any]) -> str:
    return _digest(_canonical(parse_approval(approval)))


def deadlines(approval: dict[str, Any]) -> tuple[datetime, datetime]:
    """Return cleanup start and hard session deadline from actual billing origin."""
    approval = parse_approval(approval)
    end = _utc(approval["billing_start_utc"]) + timedelta(
        seconds=approval["max_session_s"]
    )
    return end - timedelta(seconds=approval["cleanup_reserve_s"]), end


def validate_ready_receipt(
    approval: dict[str, Any], receipt: object, *, now: datetime | None = None
) -> None:
    """Validate a recent heartbeat; a copied receipt cannot attest future uptime."""
    approval = parse_approval(approval)
    cleanup, hard = deadlines(approval)
    now = now or datetime.now(UTC)
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema",
        "status",
        "approval_sha256",
        "instance_id_sha256",
        "armed_at_utc",
        "observed_at_utc",
        "cleanup_start_utc",
        "hard_deadline_utc",
        "guardian_pid",
        "external_host_declaration",
    }:
        raise GuardError("invalid guardian receipt fields")
    if (
        receipt["schema"] != "inferdrome.breakpoint-pilot-guard-ready.v1"
        or receipt["status"] != "ARMED"
        or receipt["approval_sha256"] != approval_sha256(approval)
        or receipt["instance_id_sha256"] != _digest(approval["instance_id"].encode())
        or receipt["cleanup_start_utc"] != _stamp(cleanup)
        or receipt["hard_deadline_utc"] != _stamp(hard)
        or receipt["external_host_declaration"] != "EXTERNAL_OPERATOR_HOST"
        or type(receipt["guardian_pid"]) is not int
        or receipt["guardian_pid"] <= 0
    ):
        raise GuardError("guardian receipt is not bound to this approval")
    armed = _utc(receipt["armed_at_utc"])
    observed = _utc(receipt["observed_at_utc"])
    if not _utc(approval["billing_start_utc"]) <= armed <= observed:
        raise GuardError("guardian receipt chronology is invalid")
    if not 0 <= (now - observed).total_seconds() <= 60 or now >= cleanup:
        raise GuardError("guardian receipt is stale or cleanup has begun")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr_sha256: str


class CommandRunner(Protocol):
    def __call__(self, argv: list[str], timeout_s: float) -> CommandResult: ...


def run_command(argv: list[str], timeout_s: float) -> CommandResult:
    """Bound time and output without shell evaluation or credential output."""
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise GuardError("provider command has no remaining time")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    output = bytearray()
    errors = hashlib.sha256()
    count = 0
    deadline = time.monotonic() + timeout_s
    try:
        assert process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GuardError("provider command timed out")
                for key, _ in selector.select(min(remaining, 0.2)):
                    data = os.read(key.fd, 65_536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    count += len(data)
                    if count > _MAX_OUTPUT_BYTES:
                        raise GuardError("provider command output exceeded limit")
                    if key.data == "stdout":
                        output.extend(data)
                    else:
                        errors.update(data)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GuardError("provider command timed out")
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                raise GuardError("provider command timed out") from error
        return CommandResult(code, bytes(output), "sha256:" + errors.hexdigest())
    finally:
        # A CLI child must not outlive its bounded operation, even on interrupt.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as error:
            raise GuardError("provider command process cleanup unconfirmed") from error
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _instance_present(result: CommandResult, instance_id: str) -> bool:
    if result.returncode != 0:
        raise GuardError("provider inventory command failed")
    rows = _json(result.stdout)
    if not isinstance(rows, list) or len(rows) > 10_000:
        raise GuardError("provider inventory is not a complete flat JSON list")
    identifiers: set[int] = set()
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int:
            raise GuardError("provider inventory has an invalid instance ID")
        identifier = row["id"]
        if identifier <= 0 or identifier in identifiers:
            raise GuardError("provider inventory IDs are invalid or duplicate")
        identifiers.add(identifier)
    return int(instance_id) in identifiers


def _write(path: Path, value: object, *, replace: bool = False) -> None:
    """Write private, fsynced receipts; only the live heartbeat is replaceable."""
    target = path.with_name(path.name + ".next") if replace else path
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(target, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if replace and target.exists():
            target.unlink()


class Guardian:
    """An externally supervised waiter with exact-ID destruction and readback."""

    def __init__(
        self,
        approval: dict[str, Any],
        executable: Path,
        output_dir: Path,
        *,
        runner: CommandRunner = run_command,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.approval = parse_approval(approval)
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise GuardError("Vast CLI must be an absolute executable file")
        self.executable = executable
        self.output_dir = output_dir
        self.runner, self.now, self.monotonic, self.sleep = (
            runner,
            now,
            monotonic,
            sleep,
        )
        self.cleanup_start, self.hard_deadline = deadlines(self.approval)
        self.interrupted = False
        self.attempt = 0
        self.record_errors = False

    def _remaining(self, deadline: datetime, monotonic_deadline: float) -> float:
        return min(
            (deadline - self.now()).total_seconds(),
            monotonic_deadline - self.monotonic(),
        )

    def _call(self, operation: str, timeout_s: float) -> bool | None:
        self.attempt += 1
        argv = [str(self.executable)]
        if operation == "destroy":
            argv += ["destroy", "instance", self.approval["instance_id"], "-y", "--raw"]
        else:
            argv += ["show", "instances", "--raw"]
        record: dict[str, Any] = {
            "schema": "inferdrome.breakpoint-pilot-guard-command.v1",
            "operation": operation,
            "observed_at_utc": _stamp(self.now()),
            "approval_sha256": approval_sha256(self.approval),
            "status": "UNCONFIRMED",
        }
        present: bool | None = None
        try:
            result = self.runner(argv, min(timeout_s, _COMMAND_TIMEOUT_S))
            if len(result.stdout) > _MAX_OUTPUT_BYTES:
                raise GuardError("provider command output exceeded limit")
            record.update(
                {
                    "returncode": result.returncode,
                    "stdout_sha256": _digest(result.stdout),
                    "stderr_sha256": result.stderr_sha256,
                }
            )
            if operation == "inventory":
                present = _instance_present(result, self.approval["instance_id"])
                record["target_present"] = present
                record["status"] = "READBACK_VALID"
            else:
                # Some Vast CLI versions omit destroy JSON. Only independent
                # inventory absence can close this guardian, even after rc=0.
                record["status"] = "RETURNED" if result.returncode == 0 else "FAILED"
        except (OSError, GuardError):
            record["status"] = "CALL_FAILED"
        finally:
            try:
                _write(self.output_dir / f"command-{self.attempt:04d}.json", record)
            except OSError:
                self.record_errors = True
        return present

    def _cleanup(self, monotonic_hard: float, *, trigger: str) -> dict[str, Any]:
        absent = False
        original_monotonic_hard = monotonic_hard
        # Even an already-expired approval still authorizes one immediate
        # bounded cleanup attempt; record the overrun rather than abandon it.
        deadline = self.hard_deadline
        if self._remaining(deadline, monotonic_hard) <= 0:
            deadline = self.now() + timedelta(seconds=60)
            monotonic_hard = self.monotonic() + 60
        try:
            while self._remaining(deadline, monotonic_hard) > 0:
                remaining = self._remaining(deadline, monotonic_hard)
                self._call("destroy", min(remaining / 2, _COMMAND_TIMEOUT_S))
                remaining = self._remaining(deadline, monotonic_hard)
                if remaining <= 0:
                    break
                present = self._call("inventory", min(remaining, _COMMAND_TIMEOUT_S))
                if present is False:
                    absent = True
                    break
                remaining = self._remaining(deadline, monotonic_hard)
                if remaining > 0:
                    self.sleep(min(_RETRY_INTERVAL_S, remaining))
        except (KeyboardInterrupt, SystemExit, Exception):
            trigger = "CLEANUP_INTERRUPTED_OR_FAILED"
        result = {
            "schema": "inferdrome.breakpoint-pilot-guard-result.v1",
            "approval_sha256": approval_sha256(self.approval),
            "instance_id_sha256": _digest(self.approval["instance_id"].encode()),
            "status": "ABSENCE_CONFIRMED" if absent else "CLEANUP_UNCONFIRMED",
            "trigger": trigger,
            "ended_at_utc": _stamp(self.now()),
            "hard_deadline_utc": _stamp(self.hard_deadline),
            "deadline_met": absent
            and self.now() <= self.hard_deadline
            and self.monotonic() <= original_monotonic_hard,
            "command_receipts_complete": not self.record_errors,
            "readback_provenance": "LIVE_CLI_OBSERVATION_HASH_ONLY_NOT_REPLAYABLE",
            "provider_invoice_verified": False,
            "other_resources_covered": False,
        }
        _write(self.output_dir / "result.json", result)
        return result

    def watch(self) -> dict[str, Any]:
        """Arm after observing the exact target, then destroy on deadline/signal."""
        initial_now = self.now()
        if _utc(self.approval["billing_start_utc"]) > initial_now:
            raise GuardError("observed billing start cannot be in the future")
        self.output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        _write(self.output_dir / "approval.json", self.approval)
        monotonic_hard = self.monotonic() + max(
            0, (self.hard_deadline - initial_now).total_seconds()
        )
        monotonic_cleanup = monotonic_hard - self.approval["cleanup_reserve_s"]
        trigger = "ARM_FAILED"
        try:
            if self._remaining(self.cleanup_start, monotonic_cleanup) <= 0:
                trigger = "EXPIRED_AT_ARM"
            elif self._call("inventory", _COMMAND_TIMEOUT_S) is not True:
                trigger = "TARGET_NOT_CONFIRMED_AT_ARM"
            else:
                armed_at = _stamp(self.now())
                while not self.interrupted:
                    remaining = self._remaining(self.cleanup_start, monotonic_cleanup)
                    if remaining <= 0:
                        trigger = "DEADLINE"
                        break
                    receipt = {
                        "schema": "inferdrome.breakpoint-pilot-guard-ready.v1",
                        "status": "ARMED",
                        "approval_sha256": approval_sha256(self.approval),
                        "instance_id_sha256": _digest(
                            self.approval["instance_id"].encode()
                        ),
                        "armed_at_utc": armed_at,
                        "observed_at_utc": _stamp(self.now()),
                        "cleanup_start_utc": _stamp(self.cleanup_start),
                        "hard_deadline_utc": _stamp(self.hard_deadline),
                        "guardian_pid": os.getpid(),
                        "external_host_declaration": "EXTERNAL_OPERATOR_HOST",
                    }
                    _write(self.output_dir / "ready.json", receipt, replace=True)
                    self.sleep(min(15, remaining))
                if self.interrupted:
                    trigger = "SIGNAL"
        except (KeyboardInterrupt, SystemExit):
            trigger = "INTERRUPTED"
        except Exception:
            trigger = "GUARD_FAILED"
        finally:
            # A ready heartbeat stops being offered before cleanup begins.
            ready = self.output_dir / "ready.json"
            if ready.exists():
                try:
                    ready.unlink()
                except OSError:
                    self.record_errors = True
        return self._cleanup(monotonic_hard, trigger=trigger)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--vast-executable", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--confirm", required=True, choices=["DESTROY_EXACT_INSTANCE_AT_DEADLINE"]
    )
    args = parser.parse_args(argv)
    previous: dict[int, Any] = {}
    try:
        guardian = Guardian(
            load_approval(args.approval), args.vast_executable, args.output_dir
        )

        def interrupt(_signum: int, _frame: object) -> None:
            guardian.interrupted = True

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, interrupt)
        result = guardian.watch()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "ABSENCE_CONFIRMED" else 2
    except (GuardError, OSError):
        print(
            "pilot guardian failed; inspect private receipts and provider state",
            file=sys.stderr,
        )
        return 2
    finally:
        for restored_signum, handler in previous.items():
            signal.signal(restored_signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
