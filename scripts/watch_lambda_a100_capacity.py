#!/usr/bin/env python3
"""Watch exact Lambda A100 PCIe capacity without launching an instance."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence

from inferdrome.lambda_capacity import (
    LambdaCapacityClient,
    LambdaCapacityError,
    LambdaCapacityObservation,
    observe_a100_pcie_capacity,
)
from inferdrome.qwen3_gpu_tiers import (
    QWEN3_A100_GPU_TIER_ID,
    qwen3_gpu_tier_policy,
)

_DEFAULT_POLL_SECONDS = 60
_DEFAULT_MAX_WAIT_SECONDS = 3_600
_POLLABLE_STATUSES = frozenset({"OUT_OF_CAPACITY", "TARGET_NOT_OFFERED"})


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
        minimum=15,
        maximum=3_600,
    )


def _max_wait_seconds(value: str) -> int:
    return _bounded_integer(
        value,
        label="maximum wait",
        minimum=15,
        maximum=86_400,
    )


def _timeout_seconds(value: str) -> int:
    return _bounded_integer(
        value,
        label="API timeout",
        minimum=1,
        maximum=60,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read only Lambda inventory until the frozen 1x A100 40 GB PCIe "
            "target is ready for separate operator confirmation"
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check frozen local invariants without API access",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="poll until ready, a fail-closed mismatch, or the bounded timeout",
    )
    parser.add_argument(
        "--poll-seconds",
        type=_poll_seconds,
        default=_DEFAULT_POLL_SECONDS,
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=_max_wait_seconds,
        default=_DEFAULT_MAX_WAIT_SECONDS,
    )
    parser.add_argument("--timeout-seconds", type=_timeout_seconds, default=20)
    return parser


def watch_capacity(
    client: LambdaCapacityClient,
    *,
    watch: bool,
    poll_seconds: int,
    max_wait_seconds: int,
    emit: Callable[[LambdaCapacityObservation], None],
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> LambdaCapacityObservation:
    """Emit bounded observations and stop before any launch action."""

    started = monotonic()
    deadline = started + max_wait_seconds
    while True:
        observation = observe_a100_pcie_capacity(client)
        emit(observation)
        if (
            not watch
            or observation.launch_preflight_ready
            or observation.status not in _POLLABLE_STATUSES
        ):
            return observation
        remaining = deadline - monotonic()
        if remaining <= 0:
            return observation
        sleeper(min(poll_seconds, remaining))


def _emit_json(observation: LambdaCapacityObservation) -> None:
    print(
        json.dumps(
            observation.public_record(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        flush=True,
    )


def _check() -> None:
    policy = qwen3_gpu_tier_policy(QWEN3_A100_GPU_TIER_ID)
    if (
        policy.expected_nvidia_smi_name != "NVIDIA A100-PCIE-40GB"
        or str(policy.hourly_rate_usd) != "1.99"
        or str(policy.max_session_cost_usd) != "1.25"
    ):
        raise LambdaCapacityError("frozen A100 capacity policy drifted")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _check()
        if args.check:
            print("lambda-a100-capacity: local invariants valid")
            return 0
        client = LambdaCapacityClient.from_environment(
            timeout_seconds=args.timeout_seconds
        )
        observation = watch_capacity(
            client,
            watch=args.watch,
            poll_seconds=args.poll_seconds,
            max_wait_seconds=args.max_wait_seconds,
            emit=_emit_json,
        )
    except LambdaCapacityError as error:
        print(f"lambda-a100-capacity: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("lambda-a100-capacity: interrupted", file=sys.stderr)
        return 130
    if (
        args.watch
        and not observation.launch_preflight_ready
        and observation.status in _POLLABLE_STATUSES
    ):
        print(
            "lambda-a100-capacity: bounded watch ended without a ready target",
            file=sys.stderr,
        )
    return 0 if observation.launch_preflight_ready else 3


if __name__ == "__main__":
    raise SystemExit(main())
