"""The generic GPU capacity CLI is bounded, tier-exact, and launch-free."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import scripts.watch_lambda_gpu_capacity as watcher
from inferdrome.lambda_capacity import (
    LambdaCapacityError,
    LambdaCapacityObservation,
    LambdaInstanceTypeOffer,
    LambdaRegion,
)

NOW = datetime(2026, 8, 23, 3, 0, tzinfo=UTC)
H100_TIER = "h100-80gb-pcie"


def _observation(
    status: str,
    *,
    gpu_tier_id: str = H100_TIER,
) -> LambdaCapacityObservation:
    ready = status == "READY_FOR_OPERATOR_CONFIRMATION"
    has_offer = ready or status == "OUT_OF_CAPACITY"
    h100 = gpu_tier_id == H100_TIER
    return LambdaCapacityObservation(
        active_instance_count=0 if ready else None,
        api_paths_observed=(
            ("/instance-types", "/instances")
            if ready
            else ("/instance-types",)
        ),
        catalog_projection_sha256="sha256:" + "0" * 64,
        gpu_tier_id=gpu_tier_id,
        instance_type=(
            LambdaInstanceTypeOffer(
                architecture="x86_64",
                description=(
                    "1x H100 (80 GB PCIe)" if h100 else "1x A100 (40 GB PCIe)"
                ),
                gpu_description=(
                    "H100 (80 GB PCIe)" if h100 else "A100 (40 GB PCIe)"
                ),
                gpus=1,
                memory_gib=200,
                name="gpu_1x_h100_pcie" if h100 else "gpu_1x_a100_pcie",
                price_cents_per_hour=329 if h100 else 199,
                regions_with_capacity=(
                    (
                        LambdaRegion(
                            description="Virginia, USA",
                            name="us-east-1",
                        ),
                    )
                    if ready
                    else ()
                ),
                storage_gib=1_024 if h100 else 512,
                vcpus=26 if h100 else 30,
            )
            if has_offer
            else None
        ),
        observed_at=NOW,
        status=status,  # type: ignore[arg-type]
    )


def test_h100_local_check_requires_no_api_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert watcher.main(["--gpu-tier", H100_TIER, "--check"]) == 0
    assert capsys.readouterr().out == (
        "lambda-gpu-capacity: h100-80gb-pcie local invariants valid\n"
    )


def test_missing_api_key_is_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("LAMBDA_CLOUD_API_KEY", raising=False)

    assert watcher.main(["--gpu-tier", H100_TIER]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "lambda-gpu-capacity: LAMBDA_CLOUD_API_KEY is missing or invalid\n"
    )


def test_one_shot_emits_exact_h100_observation() -> None:
    unavailable = _observation("OUT_OF_CAPACITY")
    emitted: list[LambdaCapacityObservation] = []

    result = watcher.watch_capacity(
        object(),  # type: ignore[arg-type]
        gpu_tier_id=H100_TIER,
        watch=False,
        poll_seconds=60,
        max_wait_seconds=3_600,
        emit=emitted.append,
        observer=lambda _client, _tier: unavailable,
    )

    assert result is unavailable
    assert emitted == [unavailable]


def test_watch_polls_only_capacity_absence_and_stops_when_ready() -> None:
    observations = iter(
        [
            _observation("OUT_OF_CAPACITY"),
            _observation("READY_FOR_OPERATOR_CONFIRMATION"),
        ]
    )
    emitted: list[LambdaCapacityObservation] = []
    sleeps: list[float] = []
    clock = iter([0.0, 0.0, 60.0])

    result = watcher.watch_capacity(
        object(),  # type: ignore[arg-type]
        gpu_tier_id=H100_TIER,
        watch=True,
        poll_seconds=60,
        max_wait_seconds=3_600,
        emit=emitted.append,
        sleeper=sleeps.append,
        monotonic=lambda: next(clock),
        observer=lambda _client, _tier: next(observations),
    )

    assert result.status == "READY_FOR_OPERATOR_CONFIRMATION"
    assert [item.status for item in emitted] == [
        "OUT_OF_CAPACITY",
        "READY_FOR_OPERATOR_CONFIRMATION",
    ]
    assert sleeps == [60]


def test_observer_cannot_substitute_a_different_gpu_tier() -> None:
    wrong_tier = _observation(
        "OUT_OF_CAPACITY",
        gpu_tier_id="a100-40gb-pcie",
    )

    with pytest.raises(LambdaCapacityError, match="wrong GPU tier"):
        watcher.watch_capacity(
            object(),  # type: ignore[arg-type]
            gpu_tier_id=H100_TIER,
            watch=False,
            poll_seconds=60,
            max_wait_seconds=3_600,
            emit=lambda _observation: None,
            observer=lambda _client, _tier: wrong_tier,
        )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["--gpu-tier", H100_TIER, "--poll-seconds", "14"],
            "poll interval must be between 15 and 3600",
        ),
        (
            ["--gpu-tier", H100_TIER, "--max-wait-seconds", "86401"],
            "maximum wait must be between 15 and 86400",
        ),
    ],
)
def test_cli_rejects_unbounded_polling(
    arguments: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="2"):
        watcher.build_parser().parse_args(arguments)

    assert message in capsys.readouterr().err
