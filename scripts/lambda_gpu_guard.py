#!/usr/bin/env python3
"""Bound Lambda GPU spend with an independent API termination watchdog."""

from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

API_BASE_URL = "https://cloud.lambda.ai/api/v1"
API_KEY_ENVIRONMENT_VARIABLE = "LAMBDA_CLOUD_API_KEY"
_API_USER_AGENT = "Inferdrome-Lambda-Guard/1.0"
_INSTANCE_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_DECIMAL_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?\Z")
_TERMINAL_STATUSES = frozenset({"terminated", "preempted"})
_INSTANCE_STATUSES = frozenset(
    {"booting", "active", "unhealthy", "terminating", *_TERMINAL_STATUSES}
)
_MAX_RESPONSE_BYTES = 1_048_576
_DEFAULT_POLL_SECONDS = 2
_DEFAULT_TERMINATION_TIMEOUT_SECONDS = 300
_DEFAULT_RETRY_WINDOW_SECONDS = 86_400
_DEFAULT_READINESS_TIMEOUT_SECONDS = 10
_MAX_FUTURE_CLOCK_SKEW_SECONDS = 30
_TERMINATION_SAFETY_MARGIN_SECONDS = 60


class LambdaGuardError(RuntimeError):
    """Expected, user-facing Lambda lifecycle guard failure."""


class LambdaCloudApiError(LambdaGuardError):
    """A bounded Lambda Cloud API request failed."""


@dataclass(frozen=True)
class LambdaInstance:
    """Secret-free projection of one Lambda instance response."""

    instance_id: str
    ip: str | None
    hostname: str | None
    status: str
    hourly_rate_usd: Decimal | None
    instance_type_name: str | None

    def public_record(self) -> dict[str, str | None]:
        return {
            "hostname": self.hostname,
            "hourly_rate_usd": (
                _decimal_text(self.hourly_rate_usd)
                if self.hourly_rate_usd is not None
                else None
            ),
            "instance_id": self.instance_id,
            "instance_type_name": self.instance_type_name,
            "ip": self.ip,
            "status": self.status,
        }


@dataclass(frozen=True)
class CostWindow:
    billing_started_at: datetime
    deadline: datetime
    cost_limit_deadline: datetime
    allowed_seconds: int
    termination_safety_margin_seconds: int
    hourly_rate_usd: Decimal
    max_cost_usd: Decimal

    def public_record(self) -> dict[str, str | int]:
        return {
            "allowed_seconds": self.allowed_seconds,
            "billing_started_at": _timestamp(self.billing_started_at),
            "cost_limit_deadline": _timestamp(self.cost_limit_deadline),
            "deadline": _timestamp(self.deadline),
            "hourly_rate_usd": _decimal_text(self.hourly_rate_usd),
            "max_cost_usd": _decimal_text(self.max_cost_usd),
            "termination_safety_margin_seconds": (
                self.termination_safety_margin_seconds
            ),
        }


@dataclass(frozen=True)
class TerminationResult:
    instance_id: str
    final_status: str
    request_sent: bool
    confirmed_at: datetime

    def public_record(self) -> dict[str, str | bool]:
        return {
            "confirmed_at": _timestamp(self.confirmed_at),
            "final_status": self.final_status,
            "instance_id": self.instance_id,
            "request_sent": self.request_sent,
        }


class LambdaGuardFinalizationError(LambdaGuardError):
    """Provider termination succeeded, but local guard cleanup did not."""

    def __init__(self, message: str, *, result: TerminationResult) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class WatchdogHandle:
    instance: LambdaInstance
    cost_window: CostWindow
    process: subprocess.Popen[bytes]
    receipt_path: Path
    ready_path: Path
    state_directory: Path
    client: LambdaCloudClient


JsonTransport = Callable[[str, str, object | None, str, float], object]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _urllib_transport(
    method: str,
    path: str,
    payload: object | None,
    api_key: str,
    timeout: float,
) -> object:
    if not path.startswith("/") or "://" in path:
        raise LambdaCloudApiError("Lambda Cloud API path is invalid")
    data = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": _API_USER_AGENT,
    }
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        API_BASE_URL + path,
        data=data,
        headers=headers,
        method=method,
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            content = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise LambdaCloudApiError(
            f"Lambda Cloud API request failed with HTTP {error.code}"
        ) from None
    except (OSError, urllib.error.URLError):
        raise LambdaCloudApiError(
            "Lambda Cloud API request could not complete"
        ) from None
    if len(content) > _MAX_RESPONSE_BYTES:
        raise LambdaCloudApiError("Lambda Cloud API response exceeded its size limit")
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LambdaCloudApiError(
            "Lambda Cloud API response is not valid JSON"
        ) from None


class LambdaCloudClient:
    """Minimal, secret-safe Lambda Cloud instance lifecycle client."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: JsonTransport = _urllib_transport,
        timeout_seconds: float = 20,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not _valid_api_key(api_key):
            raise LambdaGuardError(
                f"{API_KEY_ENVIRONMENT_VARIABLE} is missing or invalid"
            )
        self._api_key = api_key
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._sleep = sleeper
        self._monotonic = monotonic
        self._now = now or (lambda: datetime.now(UTC))

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> LambdaCloudClient:
        selected = os.environ if environment is None else environment
        return cls(selected.get(API_KEY_ENVIRONMENT_VARIABLE, ""), **kwargs)

    def list_instances(self) -> tuple[LambdaInstance, ...]:
        response = self._transport(
            "GET",
            "/instances",
            None,
            self._api_key,
            self._timeout_seconds,
        )
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise LambdaCloudApiError(
                "Lambda Cloud instance list has an unexpected shape"
            )
        instances = tuple(_instance_from_api(value) for value in response["data"])
        identifiers = [instance.instance_id for instance in instances]
        if len(identifiers) != len(set(identifiers)):
            raise LambdaCloudApiError("Lambda Cloud instance list contains duplicates")
        return instances

    def resolve_instance(self, reference: str) -> LambdaInstance:
        instances = self.list_instances()
        matches = [
            instance
            for instance in instances
            if reference in {instance.instance_id, instance.ip, instance.hostname}
        ]
        if not matches:
            raise LambdaGuardError(
                "no running Lambda instance matches the guard target"
            )
        if len(matches) != 1:
            raise LambdaGuardError("Lambda guard target is ambiguous")
        return matches[0]

    def terminate_and_wait(
        self,
        instance_id: str,
        *,
        poll_seconds: int = _DEFAULT_POLL_SECONDS,
        timeout_seconds: int = _DEFAULT_TERMINATION_TIMEOUT_SECONDS,
    ) -> TerminationResult:
        instance_id = parse_instance_id(instance_id)
        current = self._instance_by_id(instance_id)
        if current is None or current.status in _TERMINAL_STATUSES:
            return TerminationResult(
                instance_id=instance_id,
                final_status=current.status if current is not None else "absent",
                request_sent=False,
                confirmed_at=self._now(),
            )

        request_error: LambdaCloudApiError | None = None
        try:
            self._request_termination(instance_id)
        except LambdaCloudApiError as error:
            request_error = error

        deadline = self._monotonic() + timeout_seconds
        while True:
            self._sleep(max(1, poll_seconds))
            current = self._instance_by_id(instance_id)
            if current is None or current.status in _TERMINAL_STATUSES:
                return TerminationResult(
                    instance_id=instance_id,
                    final_status=current.status if current is not None else "absent",
                    request_sent=request_error is None,
                    confirmed_at=self._now(),
                )
            if request_error is not None:
                raise request_error
            if self._monotonic() >= deadline:
                raise LambdaGuardError(
                    "Lambda instance termination was not confirmed before timeout"
                )

    def _instance_by_id(self, instance_id: str) -> LambdaInstance | None:
        return next(
            (
                instance
                for instance in self.list_instances()
                if instance.instance_id == instance_id
            ),
            None,
        )

    def _request_termination(self, instance_id: str) -> None:
        response = self._transport(
            "POST",
            "/instance-operations/terminate",
            {"instance_ids": [instance_id]},
            self._api_key,
            self._timeout_seconds,
        )
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            raise LambdaCloudApiError(
                "Lambda Cloud termination response has an unexpected shape"
            )
        terminated = response["data"].get("terminated_instances")
        if not isinstance(terminated, list):
            raise LambdaCloudApiError(
                "Lambda Cloud termination response has an unexpected shape"
            )
        returned_ids = {
            value.get("id")
            for value in terminated
            if isinstance(value, dict) and isinstance(value.get("id"), str)
        }
        if instance_id not in returned_ids:
            raise LambdaCloudApiError(
                "Lambda Cloud did not acknowledge the requested instance termination"
            )


def _valid_api_key(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 4_096
        and not any(character.isspace() for character in value)
    )


def _watchdog_environment() -> dict[str, str]:
    api_key = os.environ.get(API_KEY_ENVIRONMENT_VARIABLE, "")
    if not _valid_api_key(api_key):
        raise LambdaGuardError(f"{API_KEY_ENVIRONMENT_VARIABLE} is missing or invalid")
    allowed = (
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment[API_KEY_ENVIRONMENT_VARIABLE] = api_key
    return environment


def parse_instance_id(value: str) -> str:
    if _INSTANCE_ID_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "Lambda instance ID must be 32 lowercase hexadecimal characters"
        )
    return value


def _positive_decimal(value: str, *, label: str, maximum: Decimal) -> Decimal:
    if _DECIMAL_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            f"{label} must be a positive decimal with at most six decimal places"
        )
    try:
        selected = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{label} is invalid") from None
    if selected <= 0 or selected > maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be greater than zero and no more than {maximum}"
        )
    return selected


def parse_hourly_rate(value: str) -> Decimal:
    return _positive_decimal(
        value,
        label="Lambda hourly rate",
        maximum=Decimal("10000"),
    )


def parse_max_cost(value: str) -> Decimal:
    return _positive_decimal(
        value,
        label="Lambda maximum cost",
        maximum=Decimal("100000"),
    )


def parse_utc_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        selected = datetime.fromisoformat(normalized)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "billing start must be an ISO 8601 timestamp with a UTC offset"
        ) from None
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            "billing start must be an ISO 8601 timestamp with a UTC offset"
        )
    return selected.astimezone(UTC)


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


def _poll_seconds(value: str) -> int:
    return _bounded_integer(
        value,
        label="poll interval",
        minimum=1,
        maximum=60,
    )


def _termination_timeout(value: str) -> int:
    return _bounded_integer(
        value,
        label="termination timeout",
        minimum=30,
        maximum=3_600,
    )


def _termination_safety_margin(value: str) -> int:
    return _bounded_integer(
        value,
        label="termination safety margin",
        minimum=1,
        maximum=3_600,
    )


def _retry_window(value: str) -> int:
    return _bounded_integer(
        value,
        label="termination retry window",
        minimum=60,
        maximum=86_400,
    )


def _same_endpoint(expected: str, observed: str | None) -> bool:
    if observed is None:
        return False
    try:
        return ipaddress.ip_address(expected) == ipaddress.ip_address(observed)
    except ValueError:
        return expected.rstrip(".").casefold() == observed.rstrip(".").casefold()


def compute_cost_window(
    *,
    billing_started_at: datetime,
    hourly_rate_usd: Decimal,
    max_cost_usd: Decimal,
    termination_safety_margin_seconds: int = _TERMINATION_SAFETY_MARGIN_SECONDS,
    now: datetime | None = None,
) -> CostWindow:
    if billing_started_at.tzinfo is None or billing_started_at.utcoffset() is None:
        raise LambdaGuardError("billing start must be timezone-aware")
    selected_now = now or datetime.now(UTC)
    if selected_now.tzinfo is None or selected_now.utcoffset() is None:
        raise LambdaGuardError("current time must be timezone-aware")
    if hourly_rate_usd <= 0 or max_cost_usd <= 0:
        raise LambdaGuardError("Lambda cost inputs must be greater than zero")
    if (
        isinstance(termination_safety_margin_seconds, bool)
        or not isinstance(termination_safety_margin_seconds, int)
        or not 1 <= termination_safety_margin_seconds <= 3_600
    ):
        raise LambdaGuardError("termination safety margin is outside limits")
    selected_now = selected_now.astimezone(UTC)
    started_at = billing_started_at.astimezone(UTC)
    if started_at > selected_now + timedelta(seconds=_MAX_FUTURE_CLOCK_SKEW_SECONDS):
        raise LambdaGuardError("billing start cannot be in the future")
    if started_at > selected_now:
        started_at = selected_now
    allowed_seconds = int(
        (max_cost_usd / hourly_rate_usd * Decimal(3_600)).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    if allowed_seconds < 1:
        raise LambdaGuardError("Lambda cost cap permits less than one second")
    safety_margin_seconds = min(
        termination_safety_margin_seconds,
        allowed_seconds,
    )
    cost_limit_deadline = started_at + timedelta(seconds=allowed_seconds)
    return CostWindow(
        billing_started_at=started_at,
        deadline=cost_limit_deadline - timedelta(seconds=safety_margin_seconds),
        cost_limit_deadline=cost_limit_deadline,
        allowed_seconds=allowed_seconds,
        termination_safety_margin_seconds=safety_margin_seconds,
        hourly_rate_usd=hourly_rate_usd,
        max_cost_usd=max_cost_usd,
    )


def arm_watchdog(
    instance_reference: str,
    *,
    hourly_rate_usd: Decimal,
    max_cost_usd: Decimal,
    billing_started_at: datetime,
    state_root: Path,
    expected_endpoint: str | None = None,
    client: LambdaCloudClient | None = None,
    poll_seconds: int = _DEFAULT_POLL_SECONDS,
    termination_timeout_seconds: int = _DEFAULT_TERMINATION_TIMEOUT_SECONDS,
    termination_safety_margin_seconds: int = _TERMINATION_SAFETY_MARGIN_SECONDS,
    retry_window_seconds: int = _DEFAULT_RETRY_WINDOW_SECONDS,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    now: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    readiness_timeout_seconds: int = _DEFAULT_READINESS_TIMEOUT_SECONDS,
) -> WatchdogHandle:
    """Validate one paid instance and launch a detached termination watchdog."""

    selected_client = client or LambdaCloudClient.from_environment()
    selected_now = now or (lambda: datetime.now(UTC))
    current_time = selected_now()
    instance = selected_client.resolve_instance(instance_reference)
    if expected_endpoint is not None and not (
        _same_endpoint(expected_endpoint, instance.ip)
        or _same_endpoint(expected_endpoint, instance.hostname)
    ):
        raise LambdaGuardError(
            "Lambda instance endpoint does not match the SSH destination"
        )
    if instance.hourly_rate_usd is None:
        raise LambdaGuardError("Lambda API did not report an hourly rate")
    if instance.hourly_rate_usd != hourly_rate_usd:
        raise LambdaGuardError(
            "Lambda API hourly rate does not match --lambda-hourly-rate-usd"
        )
    cost_window = compute_cost_window(
        billing_started_at=billing_started_at,
        hourly_rate_usd=hourly_rate_usd,
        max_cost_usd=max_cost_usd,
        termination_safety_margin_seconds=termination_safety_margin_seconds,
        now=current_time,
    )
    current_time = current_time.astimezone(UTC)
    if cost_window.deadline <= current_time + timedelta(
        seconds=readiness_timeout_seconds
    ):
        result = selected_client.terminate_and_wait(
            instance.instance_id,
            poll_seconds=poll_seconds,
            timeout_seconds=termination_timeout_seconds,
        )
        raise LambdaGuardError(
            "Lambda termination deadline is exhausted or too close to arm safely; "
            "termination confirmed "
            f"with status {result.final_status}"
        )

    state_root = state_root.expanduser().absolute()
    _require_state_root(state_root)
    state_directory = Path(
        tempfile.mkdtemp(
            prefix=f"{instance.instance_id[:12]}-",
            dir=state_root,
        )
    )
    state_directory.chmod(0o700)
    receipt_path = state_directory / "termination-receipt.json"
    ready_path = state_directory / "watchdog-ready.json"
    log_path = state_directory / "watchdog.log"
    armed_path = state_directory / "guard-armed.json"
    readiness_token = secrets.token_hex(16)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "watch",
        "--instance-id",
        instance.instance_id,
        "--hourly-rate-usd",
        _decimal_text(hourly_rate_usd),
        "--max-cost-usd",
        _decimal_text(max_cost_usd),
        "--billing-started-at",
        _timestamp(cost_window.billing_started_at),
        "--termination-safety-margin-seconds",
        str(cost_window.termination_safety_margin_seconds),
        "--poll-seconds",
        str(poll_seconds),
        "--termination-timeout-seconds",
        str(termination_timeout_seconds),
        "--retry-window-seconds",
        str(retry_window_seconds),
        "--receipt-path",
        str(receipt_path),
        "--ready-path",
        str(ready_path),
        "--readiness-token",
        readiness_token,
    ]
    caffeinate = shutil.which("caffeinate") if platform.system() == "Darwin" else None
    if caffeinate is not None:
        command = [caffeinate, "-i", *command]
    process: subprocess.Popen[bytes] | None = None
    try:
        descriptor = os.open(
            log_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as log:
            process = popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=_watchdog_environment(),
                start_new_session=True,
                close_fds=True,
            )
        _wait_for_watchdog_ready(
            process,
            ready_path=ready_path,
            readiness_token=readiness_token,
            instance_id=instance.instance_id,
            timeout_seconds=readiness_timeout_seconds,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        _write_json_exclusive(
            armed_path,
            {
                "cost_window": cost_window.public_record(),
                "instance": instance.public_record(),
                "record_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
                "schema_version": "inferdrome.lambda-guard-armed.v2",
                "watchdog_ready": True,
            },
        )
    except (LambdaGuardError, OSError) as error:
        stop_error: LambdaGuardError | None = None
        if process is not None:
            try:
                _stop_watchdog(process)
            except LambdaGuardError as cleanup_error:
                stop_error = cleanup_error
        if stop_error is not None:
            raise LambdaGuardError(
                "Lambda watchdog startup failed and its process could not be stopped; "
                f"state remains at {state_directory}: {stop_error}"
            ) from error
        _remove_state_directory(state_directory)
        if isinstance(error, LambdaGuardError):
            raise
        raise LambdaGuardError("Lambda termination watchdog could not start") from None
    if process is None:
        _remove_state_directory(state_directory)
        raise LambdaGuardError("Lambda termination watchdog could not start")
    return WatchdogHandle(
        instance=instance,
        cost_window=cost_window,
        process=process,
        receipt_path=receipt_path,
        ready_path=ready_path,
        state_directory=state_directory,
        client=selected_client,
    )


def terminate_guarded_instance(
    handle: WatchdogHandle,
    *,
    trigger: str = "controller-finally",
) -> TerminationResult:
    """Terminate now, confirm provider state, then disarm the fallback watchdog."""

    result = handle.client.terminate_and_wait(handle.instance.instance_id)
    finalization_errors: list[str] = []
    try:
        _write_termination_receipt(
            handle.receipt_path,
            result=result,
            trigger=trigger,
            cost_window=handle.cost_window,
        )
    except LambdaGuardError as error:
        finalization_errors.append(str(error))
    try:
        _stop_watchdog(handle.process)
    except LambdaGuardError as error:
        finalization_errors.append(str(error))
    if finalization_errors:
        raise LambdaGuardFinalizationError(
            "Lambda termination was confirmed, but local guard finalization failed: "
            + "; ".join(finalization_errors),
            result=result,
        )
    return result


def watch_until_deadline(
    instance_id: str,
    *,
    cost_window: CostWindow,
    receipt_path: Path,
    client: LambdaCloudClient,
    poll_seconds: int,
    termination_timeout_seconds: int,
    retry_window_seconds: int,
    ready_path: Path | None = None,
    readiness_token: str | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> TerminationResult:
    """Wait independently for the spend deadline, then retry API termination."""

    if (ready_path is None) != (readiness_token is None):
        raise LambdaGuardError("Lambda watchdog readiness inputs are incomplete")
    if ready_path is not None and readiness_token is not None:
        selected_ready_path = ready_path.expanduser().absolute()
        selected_receipt_path = receipt_path.expanduser().absolute()
        if (
            selected_ready_path.parent != selected_receipt_path.parent
            or selected_ready_path.name != "watchdog-ready.json"
            or selected_receipt_path.name != "termination-receipt.json"
            or re.fullmatch(r"[0-9a-f]{32}", readiness_token) is None
        ):
            raise LambdaGuardError("Lambda watchdog readiness inputs are invalid")
        _write_watchdog_ready(
            selected_ready_path,
            instance_id=instance_id,
            readiness_token=readiness_token,
        )
    selected_now = now or (lambda: datetime.now(UTC))
    initial_remaining = max(
        0.0,
        (cost_window.deadline - selected_now().astimezone(UTC)).total_seconds(),
    )
    monotonic_deadline = monotonic() + initial_remaining
    while True:
        wall_remaining = (
            cost_window.deadline - selected_now().astimezone(UTC)
        ).total_seconds()
        monotonic_remaining = monotonic_deadline - monotonic()
        remaining = min(wall_remaining, monotonic_remaining)
        if remaining <= 0:
            break
        sleeper(min(30, remaining))

    retry_deadline = monotonic() + retry_window_seconds
    last_error: LambdaGuardError | None = None
    while True:
        try:
            result = client.terminate_and_wait(
                instance_id,
                poll_seconds=poll_seconds,
                timeout_seconds=termination_timeout_seconds,
            )
        except LambdaGuardError as error:
            last_error = error
            if monotonic() >= retry_deadline:
                raise LambdaGuardError(
                    "Lambda deadline passed but termination could not be confirmed"
                ) from last_error
            sleeper(max(2, poll_seconds))
            continue
        _write_termination_receipt(
            receipt_path,
            result=result,
            trigger="cost-deadline",
            cost_window=cost_window,
        )
        return result


def _stop_watchdog(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError as error:
        if error.errno == errno.ESRCH or process.poll() is not None:
            return
        raise LambdaGuardError(
            "Lambda instance is terminated, but the local watchdog could not be "
            "signaled"
        ) from None
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        if process.poll() is not None:
            return
        raise LambdaGuardError(
            "Lambda instance is terminated, but the local watchdog could not be reaped"
        ) from None
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError as error:
        if error.errno == errno.ESRCH or process.poll() is not None:
            return
        raise LambdaGuardError(
            "Lambda instance is terminated, but the local watchdog could not be killed"
        ) from None
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        if process.poll() is not None:
            return
        raise LambdaGuardError(
            "Lambda instance is terminated, but the local watchdog did not stop"
        ) from None


def _instance_from_api(value: object) -> LambdaInstance:
    if not isinstance(value, dict):
        raise LambdaCloudApiError("Lambda Cloud instance has an unexpected shape")
    instance_id = value.get("id")
    status = value.get("status")
    if (
        not isinstance(instance_id, str)
        or _INSTANCE_ID_PATTERN.fullmatch(instance_id) is None
        or not isinstance(status, str)
        or status not in _INSTANCE_STATUSES
    ):
        raise LambdaCloudApiError(
            "Lambda Cloud instance has invalid identity or status"
        )
    ip = value.get("ip")
    hostname = value.get("hostname")
    if ip is not None and not isinstance(ip, str):
        raise LambdaCloudApiError("Lambda Cloud instance IP is invalid")
    if hostname is not None and not isinstance(hostname, str):
        raise LambdaCloudApiError("Lambda Cloud instance hostname is invalid")
    hourly_rate: Decimal | None = None
    instance_type_name: str | None = None
    instance_type = value.get("instance_type")
    if isinstance(instance_type, dict):
        raw_name = instance_type.get("name")
        if raw_name is not None:
            if (
                not isinstance(raw_name, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9_]{0,127}", raw_name) is None
            ):
                raise LambdaCloudApiError("Lambda Cloud instance type name is invalid")
            instance_type_name = raw_name
        price_cents = instance_type.get("price_cents_per_hour")
        if isinstance(price_cents, int) and not isinstance(price_cents, bool):
            if price_cents <= 0:
                raise LambdaCloudApiError("Lambda Cloud instance rate is invalid")
            hourly_rate = Decimal(price_cents) / Decimal(100)
    return LambdaInstance(
        instance_id=instance_id,
        ip=ip,
        hostname=hostname,
        status=status,
        hourly_rate_usd=hourly_rate,
        instance_type_name=instance_type_name,
    )


def _require_state_root(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = os.lstat(path)
    except OSError:
        raise LambdaGuardError("Lambda guard state root is unavailable") from None
    if not path.is_dir() or path.is_symlink():
        raise LambdaGuardError("Lambda guard state root must be a real directory")
    path.chmod(metadata.st_mode & 0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_exclusive(
    path: Path,
    value: dict[str, object],
    *,
    allow_existing: bool = False,
) -> bool:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary_path = path.with_name(f".{path.name}.{secrets.token_hex(16)}.temporary")
    published = False
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.chmod(0o444)
        try:
            os.link(temporary_path, path, follow_symlinks=False)
        except OSError as error:
            if error.errno == errno.EEXIST and allow_existing:
                return False
            raise
        published = True
        _fsync_directory(path.parent)
    except OSError:
        raise LambdaGuardError("Lambda guard record could not be published") from None
    finally:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)
        if published:
            with suppress(OSError):
                _fsync_directory(path.parent)
    return True


def _read_json_record(
    path: Path,
    *,
    label: str,
    missing_ok: bool = False,
) -> dict[str, object] | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise LambdaGuardError(f"{label} is unavailable") from None
    except OSError:
        raise LambdaGuardError(f"{label} is unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_size < 2
        or metadata.st_size > 65_536
    ):
        raise LambdaGuardError(f"{label} is invalid")
    try:
        content = path.read_bytes()
        value = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise LambdaGuardError(f"{label} is invalid") from None
    if not isinstance(value, dict):
        raise LambdaGuardError(f"{label} is invalid")
    return value


def _remove_state_directory(path: Path) -> None:
    with suppress(OSError):
        path.chmod(0o700)
    with suppress(OSError):
        shutil.rmtree(path)


def _write_watchdog_ready(
    path: Path,
    *,
    instance_id: str,
    readiness_token: str,
) -> None:
    _write_json_exclusive(
        path,
        {
            "instance_id": instance_id,
            "pid": os.getpid(),
            "readiness_token": readiness_token,
            "record_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
            "schema_version": "inferdrome.lambda-watchdog-ready.v1",
        },
    )


def _wait_for_watchdog_ready(
    process: subprocess.Popen[bytes],
    *,
    ready_path: Path,
    readiness_token: str,
    instance_id: str,
    timeout_seconds: int,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    deadline = monotonic() + timeout_seconds
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            raise LambdaGuardError(
                "Lambda termination watchdog exited before readiness "
                f"with status {exit_code}"
            )
        ready = _read_json_record(
            ready_path,
            label="Lambda watchdog readiness record",
            missing_ok=True,
        )
        if ready is not None:
            if (
                set(ready)
                != {
                    "instance_id",
                    "pid",
                    "readiness_token",
                    "record_kind",
                    "schema_version",
                }
                or ready.get("instance_id") != instance_id
                or ready.get("readiness_token") != readiness_token
                or ready.get("record_kind")
                != "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
                or ready.get("schema_version") != "inferdrome.lambda-watchdog-ready.v1"
                or isinstance(ready.get("pid"), bool)
                or not isinstance(ready.get("pid"), int)
                or int(ready["pid"]) <= 0
                or process.poll() is not None
            ):
                raise LambdaGuardError("Lambda watchdog readiness record is invalid")
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise LambdaGuardError("Lambda termination watchdog did not become ready")
        sleeper(min(0.05, remaining))


def _write_termination_receipt(
    path: Path,
    *,
    result: TerminationResult,
    trigger: str,
    cost_window: CostWindow,
) -> None:
    published = _write_json_exclusive(
        path,
        {
            "cost_window": cost_window.public_record(),
            "record_kind": "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION",
            "schema_version": "inferdrome.lambda-termination-receipt.v2",
            "termination": result.public_record(),
            "trigger": trigger,
        },
        allow_existing=True,
    )
    if published:
        return
    existing = _read_json_record(
        path,
        label="Lambda termination receipt",
    )
    termination = existing.get("termination") if existing is not None else None
    if (
        existing is None
        or existing.get("record_kind") != "OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION"
        or existing.get("schema_version") != "inferdrome.lambda-termination-receipt.v2"
        or not isinstance(termination, dict)
        or termination.get("instance_id") != result.instance_id
        or termination.get("final_status") not in {*_TERMINAL_STATUSES, "absent"}
    ):
        raise LambdaGuardError("Lambda termination receipt is invalid")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _add_target(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--instance-id", type=parse_instance_id)
    group.add_argument("--public-ip")


def _add_cost_window(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hourly-rate-usd", type=parse_hourly_rate, required=True)
    parser.add_argument("--max-cost-usd", type=parse_max_cost, required=True)
    parser.add_argument(
        "--billing-started-at",
        type=parse_utc_timestamp,
        required=True,
        help="actual provider billing start in ISO 8601",
    )
    parser.add_argument(
        "--termination-safety-margin-seconds",
        type=_termination_safety_margin,
        default=_TERMINATION_SAFETY_MARGIN_SECONDS,
    )


def _add_termination_tuning(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--poll-seconds",
        type=_poll_seconds,
        default=_DEFAULT_POLL_SECONDS,
    )
    parser.add_argument(
        "--termination-timeout-seconds",
        type=_termination_timeout,
        default=_DEFAULT_TERMINATION_TIMEOUT_SECONDS,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Terminate a Lambda GPU at a bounded spend deadline"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="list secret-free running instance records")

    terminate = commands.add_parser(
        "terminate",
        help="terminate and confirm one instance",
    )
    _add_target(terminate)
    _add_termination_tuning(terminate)

    arm = commands.add_parser("arm", help="launch a detached deadline watchdog")
    _add_target(arm)
    _add_cost_window(arm)
    _add_termination_tuning(arm)
    arm.add_argument(
        "--retry-window-seconds",
        type=_retry_window,
        default=_DEFAULT_RETRY_WINDOW_SECONDS,
    )
    arm.add_argument(
        "--state-root",
        default=str(Path.home() / ".inferdrome" / "lambda-guards"),
    )

    watch = commands.add_parser("watch", help=argparse.SUPPRESS)
    watch.add_argument("--instance-id", type=parse_instance_id, required=True)
    _add_cost_window(watch)
    _add_termination_tuning(watch)
    watch.add_argument(
        "--retry-window-seconds",
        type=_retry_window,
        default=_DEFAULT_RETRY_WINDOW_SECONDS,
    )
    watch.add_argument("--receipt-path", required=True)
    watch.add_argument("--ready-path", required=True)
    watch.add_argument("--readiness-token", required=True)
    return parser


def _reference(args: argparse.Namespace) -> str:
    return args.instance_id or args.public_ip


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = LambdaCloudClient.from_environment()
        if args.command == "list":
            result: object = [
                instance.public_record() for instance in client.list_instances()
            ]
        elif args.command == "terminate":
            instance = client.resolve_instance(_reference(args))
            result = client.terminate_and_wait(
                instance.instance_id,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.termination_timeout_seconds,
            ).public_record()
        elif args.command == "arm":
            handle = arm_watchdog(
                _reference(args),
                hourly_rate_usd=args.hourly_rate_usd,
                max_cost_usd=args.max_cost_usd,
                billing_started_at=args.billing_started_at,
                termination_safety_margin_seconds=(
                    args.termination_safety_margin_seconds
                ),
                state_root=Path(args.state_root),
                client=client,
                poll_seconds=args.poll_seconds,
                termination_timeout_seconds=args.termination_timeout_seconds,
                retry_window_seconds=args.retry_window_seconds,
            )
            result = {
                "command": shlex.join(handle.process.args),
                "cost_window": handle.cost_window.public_record(),
                "instance": handle.instance.public_record(),
                "pid": handle.process.pid,
                "ready_path": str(handle.ready_path),
                "state_directory": str(handle.state_directory),
            }
        else:
            cost_window = compute_cost_window(
                billing_started_at=args.billing_started_at,
                hourly_rate_usd=args.hourly_rate_usd,
                max_cost_usd=args.max_cost_usd,
                termination_safety_margin_seconds=(
                    args.termination_safety_margin_seconds
                ),
            )
            result = watch_until_deadline(
                args.instance_id,
                cost_window=cost_window,
                receipt_path=Path(args.receipt_path).expanduser().absolute(),
                client=client,
                poll_seconds=args.poll_seconds,
                termination_timeout_seconds=args.termination_timeout_seconds,
                retry_window_seconds=args.retry_window_seconds,
                ready_path=Path(args.ready_path),
                readiness_token=args.readiness_token,
            ).public_record()
    except LambdaGuardError as error:
        print(f"lambda-gpu-guard: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
