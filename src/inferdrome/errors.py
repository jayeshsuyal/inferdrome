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


class DashboardError(InferdromeError):
    """The local read-only dashboard could not be started or queried safely."""


class DashboardRunNotFound(DashboardError):
    """A dashboard run ID was not present in the verified index."""


class DashboardPaginationError(DashboardError):
    """A dashboard collection cursor or page bound was invalid."""


class CancellationRequested(InferdromeError):
    """Cooperative execution stopped after a cancellation request."""
