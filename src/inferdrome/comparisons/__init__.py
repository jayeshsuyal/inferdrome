"""Operator-attested controlled-comparison plans, results, and verification."""

from inferdrome.comparisons.service import (
    ComparisonResultDeclaration,
    VerifiedComparisonPlan,
    VerifiedComparisonResult,
    create_comparison_plan,
    create_comparison_result,
    inspect_comparison_result_declaration,
    verify_comparison_plan,
    verify_comparison_result,
)

__all__ = [
    "ComparisonResultDeclaration",
    "VerifiedComparisonPlan",
    "VerifiedComparisonResult",
    "create_comparison_plan",
    "create_comparison_result",
    "inspect_comparison_result_declaration",
    "verify_comparison_plan",
    "verify_comparison_result",
]
