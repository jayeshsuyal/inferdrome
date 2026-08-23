#!/usr/bin/env python3
"""Watch exact Lambda GPU capacity without launching an instance."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence

from inferdrome.lambda_capacity import (
    LAMBDA_CAPACITY_GPU_TIERS,
    LambdaCapacityClient,
    LambdaCapacityError,
    LambdaCapacityObservation,
    lambda_capacity_target,
    observe_gpu_capacity,
)
from inferdrome.qwen3_gpu_tiers import qwen3_gpu_tier_policy

_DEFAULT_POLL_SECONDS = 60
_DEFAULT_MAX_WAIT_SECONDS = 3_600
_POLLABLE_STATUSES = frozenset({"OUT_OF_CAPACITY", "TARGET_NOT_OFFERED"})
_EXPECTED_LOCAL_INVARIANTS = {
    "a100-40gb-pcie": {
        "architecture": "x86_64",
        "expected_nvidia_smi_name": "NVIDIA A100-PCIE-40GB",
        "gpus": 1,
        "hourly_rate_usd": "1.99",
        "max_session_cost_usd": "1.25",
        "memory_gib": None,
        "provider_description": "1x A100 (40 GB PCIe)",
        "provider_gpu_description": "A100 (40 GB PCIe)",
        "storage_gib": None,
        "vcpus": None,
    },
    "h100-80gb-pcie": {
        "architecture": "x86_64",
        "expected_nvidia_smi_name": "NVIDIA H100 PCIe",
        "gpus": 1,
        "hourly_rate_usd": "3.29",
        "max_session_cost_usd": "2.25",
        "memory_gib": 225,
        "provider_description": "1x H100 (80 GB PCIe)",
        "provider_gpu_description": "H100 (80 GB PCIe)",
        "storage_gib": 1_024,
        "vcpus": 26,
    },
}


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
            "Read only Lambda inventory until one exact frozen GPU target is "
            "ready for separate operator confirmation"
        )
    )
    parser.add_argument(
        "--gpu-tier",
        choices=LAMBDA_CAPACITY_GPU_TIERS,
        required=True,
        help="exact implemented Qwen3 campaign GPU tier",
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
    gpu_tier_id: str,
    watch: bool,
    poll_seconds: int,
    max_wait_seconds: int,
    emit: Callable[[LambdaCapacityObservation], None],
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    observer: Callable[
        [LambdaCapacityClient, str], LambdaCapacityObservation
    ] = observe_gpu_capacity,
) -> LambdaCapacityObservation:
    """Emit bounded observations and stop before any launch action."""

    started = monotonic()
    deadline = started + max_wait_seconds
    while True:
        observation = observer(client, gpu_tier_id)
        if observation.gpu_tier_id != gpu_tier_id:
            raise LambdaCapacityError("capacity observer returned the wrong GPU tier")
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


def _check(gpu_tier_id: str) -> None:
    target = lambda_capacity_target(gpu_tier_id)
    policy = qwen3_gpu_tier_policy(gpu_tier_id)
    observed = {
        "architecture": target.architecture,
        "expected_nvidia_smi_name": policy.expected_nvidia_smi_name,
        "gpus": target.gpus,
        "hourly_rate_usd": str(policy.hourly_rate_usd),
        "max_session_cost_usd": str(policy.max_session_cost_usd),
        "memory_gib": target.memory_gib,
        "provider_description": target.provider_description,
        "provider_gpu_description": target.provider_gpu_description,
        "storage_gib": target.storage_gib,
        "vcpus": target.vcpus,
    }
    if observed != _EXPECTED_LOCAL_INVARIANTS[gpu_tier_id]:
        raise LambdaCapacityError("frozen Lambda capacity policy drifted")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _check(args.gpu_tier)
        if args.check:
            print(f"lambda-gpu-capacity: {args.gpu_tier} local invariants valid")
            return 0
        client = LambdaCapacityClient.from_environment(
            timeout_seconds=args.timeout_seconds
        )
        observation = watch_capacity(
            client,
            gpu_tier_id=args.gpu_tier,
            watch=args.watch,
            poll_seconds=args.poll_seconds,
            max_wait_seconds=args.max_wait_seconds,
            emit=_emit_json,
        )
    except LambdaCapacityError as error:
        print(f"lambda-gpu-capacity: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("lambda-gpu-capacity: interrupted", file=sys.stderr)
        return 130
    if (
        args.watch
        and not observation.launch_preflight_ready
        and observation.status in _POLLABLE_STATUSES
    ):
        print(
            "lambda-gpu-capacity: bounded watch ended without a ready target",
            file=sys.stderr,
        )
    return 0 if observation.launch_preflight_ready else 3


if __name__ == "__main__":
    raise SystemExit(main())
