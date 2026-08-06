"""Identifier, version, path, and digest types."""

import hashlib
import secrets
from typing import Annotated

from pydantic import AfterValidator, StringConstraints

ExperimentId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$",
    ),
]
RunId = Annotated[
    str,
    StringConstraints(pattern=r"^run-[0-9a-f]{32}$"),
]
RequestId = Annotated[
    str,
    StringConstraints(pattern=r"^req-[0-9]{8}$"),
]
ProducerRequestId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
SemanticVersion = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
        )
    ),
]
Sha256Digest = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]
RelativeArtifactPath = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=512,
        pattern=(
            r"^[A-Za-z0-9][A-Za-z0-9._-]*"
            r"(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"
        ),
    ),
]
OpaqueName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]*$",
    ),
]
DecimalString = Annotated[
    str,
    StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"),
]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


def _validate_safe_prefix(value: str) -> str:
    if value.endswith(("-", ":", "_", ".")):
        return value
    raise ValueError("producer request ID prefix must end in a separator")


ProducerRequestIdPrefix = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
    AfterValidator(_validate_safe_prefix),
]


def new_run_id() -> str:
    """Create a run-scoped random identifier without external dependencies."""

    return f"run-{secrets.token_hex(16)}"


def request_id_from_index(sequence_index: int) -> str:
    """Derive the canonical request ID for a measured sequence index."""

    if not 0 <= sequence_index <= 99_999_999:
        raise ValueError("sequence index is outside the request ID domain")
    return f"req-{sequence_index:08d}"


def sha256_digest(data: bytes) -> str:
    """Return the tagged SHA-256 representation used by public contracts."""

    return f"sha256:{hashlib.sha256(data).hexdigest()}"
