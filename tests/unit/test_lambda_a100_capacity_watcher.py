"""The A100 capacity CLI polls within bounds and never implies launch authority."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import scripts.watch_lambda_a100_capacity as watcher
from inferdrome.lambda_capacity import (
    LambdaCapacityObservation,
    LambdaInstanceTypeOffer,
    LambdaRegion,
)

NOW = datetime(2026, 8, 23, 3, 0, tzinfo=UTC)


def _observation(status: str) -> LambdaCapacityObservation:
    ready = status == "READY_FOR_OPERATOR_CONFIRMATION"
    has_offer = ready or status == "OUT_OF_CAPACITY"
    return LambdaCapacityObservation(
        active_instance_count=0 if ready else None,
        api_paths_observed=(
            ("/instance-types", "/instances")
            if ready
            else ("/instance-types",)
        ),
        catalog_projection_sha256="sha256:" + "0" * 64,
        instance_type=(
            LambdaInstanceTypeOffer(
                architecture="x86_64",
                description="1x A100 (40 GB PCIe)",
                gpu_description="A100 (40 GB PCIe)",
                gpus=1,
                memory_gib=200,
                name="gpu_1x_a100_pcie",
                price_cents_per_hour=199,
                regions_with_capacity=(
                    (
                        LambdaRegion(
                            description="California, USA",
                            name="us-west-1",
                        ),
                    )
                    if ready
                    else ()
                ),
                storage_gib=512,
                vcpus=30,
            )
            if has_offer
            else None
        ),
        observed_at=NOW,
        status=status,  # type: ignore[arg-type]
    )


def test_local_check_requires_no_api_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert watcher.main(["--check"]) == 0
    assert capsys.readouterr().out == (
        "lambda-a100-capacity: local invariants valid\n"
    )


def test_missing_api_key_is_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("LAMBDA_CLOUD_API_KEY", raising=False)

    assert watcher.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "lambda-a100-capacity: LAMBDA_CLOUD_API_KEY is missing or invalid\n"
    )


def test_one_shot_emits_once_and_returns_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unavailable = _observation("OUT_OF_CAPACITY")
    emitted: list[LambdaCapacityObservation] = []
    monkeypatch.setattr(
        watcher,
        "observe_a100_pcie_capacity",
        lambda _client: unavailable,
    )

    result = watcher.watch_capacity(
        object(),  # type: ignore[arg-type]
        watch=False,
        poll_seconds=60,
        max_wait_seconds=3_600,
        emit=emitted.append,
    )

    assert result is unavailable
    assert emitted == [unavailable]


def test_watch_polls_only_capacity_absence_and_stops_when_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = iter(
        [
            _observation("OUT_OF_CAPACITY"),
            _observation("READY_FOR_OPERATOR_CONFIRMATION"),
        ]
    )
    emitted: list[LambdaCapacityObservation] = []
    sleeps: list[float] = []
    clock = iter([0.0, 0.0, 60.0])
    monkeypatch.setattr(
        watcher,
        "observe_a100_pcie_capacity",
        lambda _client: next(observations),
    )

    result = watcher.watch_capacity(
        object(),  # type: ignore[arg-type]
        watch=True,
        poll_seconds=60,
        max_wait_seconds=3_600,
        emit=emitted.append,
        sleeper=sleeps.append,
        monotonic=lambda: next(clock),
    )

    assert result.status == "READY_FOR_OPERATOR_CONFIRMATION"
    assert [item.status for item in emitted] == [
        "OUT_OF_CAPACITY",
        "READY_FOR_OPERATOR_CONFIRMATION",
    ]
    assert sleeps == [60]


def test_watch_stops_immediately_on_rate_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mismatch = _observation("RATE_MISMATCH")
    sleeps: list[float] = []
    monkeypatch.setattr(
        watcher,
        "observe_a100_pcie_capacity",
        lambda _client: mismatch,
    )

    result = watcher.watch_capacity(
        object(),  # type: ignore[arg-type]
        watch=True,
        poll_seconds=60,
        max_wait_seconds=3_600,
        emit=lambda _observation: None,
        sleeper=sleeps.append,
        monotonic=lambda: 0,
    )

    assert result is mismatch
    assert sleeps == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--poll-seconds", "14"], "poll interval must be between 15 and 3600"),
        (
            ["--max-wait-seconds", "86401"],
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
