"""Shared strict-model behavior."""

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Immutable, strict model that rejects unknown fields."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )
