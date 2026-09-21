"""Agent-native, read-only access to reproducible inference evidence.

This package is the thin data layer an MCP evidence adapter will expose. It only
reads sealed evidence already on disk; it never executes a run, mutates or
verifies evidence, or contacts a provider. Verification and the protocol
surface are added in later slices.
"""

from inferdrome.mcp.runs import (
    ConfigDifference,
    EvidenceVerification,
    MetricDelta,
    RunComparison,
    RunDetail,
    RunStatus,
    RunSummary,
    VerificationStatus,
    compare_runs,
    get_run,
    list_runs,
    verify_evidence,
)

__all__ = [
    "ConfigDifference",
    "EvidenceVerification",
    "MetricDelta",
    "RunComparison",
    "RunDetail",
    "RunStatus",
    "RunSummary",
    "VerificationStatus",
    "compare_runs",
    "get_run",
    "list_runs",
    "verify_evidence",
]
