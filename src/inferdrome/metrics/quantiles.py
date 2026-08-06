"""Small exact numeric primitives used by the v1 reducer."""

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from inferdrome.errors import ReductionError

_SIX_PLACES = Decimal("0.000001")


def nearest_rank(values: tuple[int, ...], percentile: int) -> int:
    """Return the exact 1-indexed nearest-rank percentile."""

    if not values:
        raise ReductionError("nearest-rank quantile requires a non-empty population")
    if not 1 <= percentile <= 100:
        raise ReductionError("percentile must be between one and one hundred")
    ordered = sorted(values)
    rank = (percentile * len(ordered) + 99) // 100
    return ordered[rank - 1]


def decimal_ratio(numerator: int, denominator: int) -> str:
    """Divide integers and emit decimal-half-even text with six places."""

    if numerator < 0:
        raise ReductionError("decimal numerator cannot be negative")
    if denominator <= 0:
        raise ReductionError("decimal denominator must be positive")
    precision = max(50, len(str(numerator)) + len(str(denominator)) + 20)
    with localcontext() as context:
        context.prec = precision
        value = (Decimal(numerator) / Decimal(denominator)).quantize(
            _SIX_PLACES,
            rounding=ROUND_HALF_EVEN,
        )
    return format(value, "f")
