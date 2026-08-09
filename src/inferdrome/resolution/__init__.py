"""Source resolution and canonicalization."""

from inferdrome.resolution.resolver import (
    ResolutionResult,
    resolve_experiment,
    validate_resolution_result,
)

__all__ = [
    "ResolutionResult",
    "resolve_experiment",
    "validate_resolution_result",
]
