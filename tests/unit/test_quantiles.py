"""Exact nearest-rank and decimal-half-even primitives."""

import pytest

from inferdrome.errors import ReductionError
from inferdrome.metrics.quantiles import decimal_ratio, nearest_rank


@pytest.mark.parametrize(
    ("percentile", "expected"),
    [(1, 10), (25, 10), (50, 20), (95, 40), (99, 40), (100, 40)],
)
def test_nearest_rank_is_one_indexed(percentile: int, expected: int) -> None:
    assert nearest_rank((40, 10, 30, 20), percentile) == expected


@pytest.mark.parametrize("percentile", [0, 101])
def test_nearest_rank_rejects_invalid_percentiles(percentile: int) -> None:
    with pytest.raises(ReductionError, match="between"):
        nearest_rank((1,), percentile)


def test_nearest_rank_rejects_empty_population() -> None:
    with pytest.raises(ReductionError, match="non-empty"):
        nearest_rank((), 95)


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        (1, 2, "0.500000"),
        (1, 3, "0.333333"),
        (2, 3, "0.666667"),
        (1_234_5645, 10_000_000, "1.234564"),
        (1_234_5655, 10_000_000, "1.234566"),
        (0, 9, "0.000000"),
    ],
)
def test_decimal_ratio_uses_half_even_six_places(
    numerator: int, denominator: int, expected: str
) -> None:
    assert decimal_ratio(numerator, denominator) == expected


@pytest.mark.parametrize(("numerator", "denominator"), [(-1, 2), (1, 0), (1, -1)])
def test_decimal_ratio_rejects_invalid_inputs(
    numerator: int, denominator: int
) -> None:
    with pytest.raises(ReductionError):
        decimal_ratio(numerator, denominator)
