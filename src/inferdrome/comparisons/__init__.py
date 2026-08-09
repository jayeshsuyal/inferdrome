"""Operator-attested controlled-comparison plans, results, and verification."""

from inferdrome.comparisons.executor import (
    ComparisonExecutionProgress,
    ExecutedComparison,
    PlannedRunProgress,
    execute_comparison_plan,
    inspect_comparison_execution,
)
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
    "ComparisonExecutionProgress",
    "ComparisonResultDeclaration",
    "ExecutedComparison",
    "PlannedRunProgress",
    "VerifiedComparisonPlan",
    "VerifiedComparisonResult",
    "create_comparison_plan",
    "create_comparison_result",
    "execute_comparison_plan",
    "inspect_comparison_execution",
    "inspect_comparison_result_declaration",
    "verify_comparison_plan",
    "verify_comparison_result",
]
