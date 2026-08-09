"""Stable user-facing exception hierarchy."""


class InferdromeError(Exception):
    """Base class for expected Inferdrome failures."""


class SourceInputError(InferdromeError):
    """The source specification or workload is unsafe or invalid."""


class ResolutionError(InferdromeError):
    """Valid source input cannot be resolved into the frozen contract."""


class WorkspaceError(InferdromeError):
    """A run workspace operation failed closed."""


class AdapterError(InferdromeError):
    """A benchmark adapter could not produce internally consistent evidence."""


class NormalizationError(InferdromeError):
    """Pinned producer output does not match its supported native contract."""


class ReductionError(InferdromeError):
    """Canonical observations cannot be reduced without ambiguity."""


class BundleError(InferdromeError):
    """A bundle could not be staged or sealed safely."""


class VerificationError(InferdromeError):
    """An offline evidence-bundle integrity invariant failed."""


class TrialSetError(InferdromeError):
    """A repeated-trial grouping could not be created or verified safely."""


class ControlledComparisonError(InferdromeError):
    """A controlled comparison could not be created or verified safely."""


class ControlledComparisonExecutionError(ControlledComparisonError):
    """A frozen controlled-comparison schedule could not execute safely."""


class DashboardError(InferdromeError):
    """The local read-only dashboard could not be started or queried safely."""


class DashboardRunNotFound(DashboardError):
    """A dashboard run ID was not present in the verified index."""


class DashboardTrialSetNotFound(DashboardError):
    """A dashboard trial-set ID was not present in the verified index."""


class DashboardControlledComparisonNotFound(DashboardError):
    """A comparison-plan ID was not present in the verified dashboard index."""


class DashboardPaginationError(DashboardError):
    """A dashboard collection cursor or page bound was invalid."""


class CancellationRequested(InferdromeError):
    """Cooperative execution stopped after a cancellation request."""
