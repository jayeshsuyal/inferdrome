"""Bounded two-endpoint routing execution evidence bridge (PR B).

The package namespace is deliberately lazy: importing an offline verifier must
not import the executor or any transport implementation as a side effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from inferdrome.routing_execution.executor import (
        ExecutionError,
        SealedRoutingExecution,
        run_execution,
    )
    from inferdrome.routing_execution.verifier import verify_execution_package

__all__ = [
    "ExecutionError",
    "SealedRoutingExecution",
    "run_execution",
    "verify_execution_package",
]


def __getattr__(name: str) -> Any:
    """Load the public execution or verification API only when requested."""

    if name in {"ExecutionError", "SealedRoutingExecution", "run_execution"}:
        from inferdrome.routing_execution import executor

        return getattr(executor, name)
    if name == "verify_execution_package":
        from inferdrome.routing_execution.verifier import verify_execution_package

        return verify_execution_package
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
