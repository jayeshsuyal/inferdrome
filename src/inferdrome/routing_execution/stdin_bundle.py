"""Strict runtime-only envelope transfer for the private routing runner.

The envelope is deliberately separate from sealed execution evidence.  It is
only a bounded in-memory runner handoff for the already-bound execution
configuration, fixed workload bytes, and (optionally) an ephemeral IAP map.
No error emitted here includes any input value. Callers may transport it over a
strict stdin or private control boundary, but it is never sealed as evidence.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Final

from inferdrome.routing_execution.canonical import canonical_json_bytes

RUNTIME_INPUT_BUNDLE_SCHEMA_VERSION: Final = (
    "inferdrome.routing-execution-runtime-input-bundle.v1"
)
MAX_RUNTIME_INPUT_CONFIG_BYTES: Final = 1_048_576
MAX_RUNTIME_INPUT_WORKLOAD_BYTES: Final = 1_048_576
MAX_RUNTIME_INPUT_IAP_MAP_BYTES: Final = 16_384


def _base64_size(size: int) -> int:
    return 4 * ((size + 2) // 3)


# The fixed envelope keys and schema consume far fewer than 1 KiB.  Keeping a
# fixed allowance makes the input read bound explicit without coupling it to a
# particular JSON library's implementation details.
MAX_RUNTIME_INPUT_BUNDLE_BYTES: Final = (
    _base64_size(MAX_RUNTIME_INPUT_CONFIG_BYTES)
    + _base64_size(MAX_RUNTIME_INPUT_WORKLOAD_BYTES)
    + _base64_size(MAX_RUNTIME_INPUT_IAP_MAP_BYTES)
    + 1_024
)

_KEY_CONFIG: Final = "config_base64"
_KEY_IAP_MAP: Final = "iap_transport_map_base64"
_KEY_SCHEMA: Final = "schema_version"
_KEY_WORKLOAD: Final = "workload_base64"
_EXPECTED_KEYS: Final = frozenset(
    {_KEY_CONFIG, _KEY_IAP_MAP, _KEY_SCHEMA, _KEY_WORKLOAD}
)


class RuntimeInputBundleError(ValueError):
    """A runtime-only bundle is malformed or outside its bounded contract."""

    def __init__(self) -> None:
        super().__init__("RUNTIME_INPUT_BUNDLE_REJECTED")


class _BundleRejected(ValueError):
    """Internal parser sentinel that intentionally carries no input details."""


def _reject() -> RuntimeInputBundleError:
    return RuntimeInputBundleError()


def _require_bytes(
    value: object, *, maximum: int, optional: bool = False
) -> bytes | None:
    if optional and value is None:
        return None
    if type(value) is not bytes or not 1 <= len(value) <= maximum:
        raise _reject()
    return value


def _encode_base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_base64(
    value: object, *, maximum: int, optional: bool = False
) -> bytes | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise _BundleRejected()
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError, binascii.Error):
        raise _BundleRejected() from None
    if base64.b64encode(raw).decode("ascii") != value:
        raise _BundleRejected()
    if not 1 <= len(raw) <= maximum:
        raise _BundleRejected()
    return raw


def _strict_json(content: bytes) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise _BundleRejected() from None

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise _BundleRejected()
            value[key] = item
        return value

    def reject_constant(_: str) -> None:
        raise _BundleRejected()

    try:
        value = json.loads(
            text, object_pairs_hook=pairs, parse_constant=reject_constant
        )
    except _BundleRejected:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise _BundleRejected() from None
    if not isinstance(value, dict):
        raise _BundleRejected()
    try:
        if canonical_json_bytes(value) != content:
            raise _BundleRejected()
    except (TypeError, ValueError):
        raise _BundleRejected() from None
    return value


@dataclass(frozen=True)
class RuntimeInputBundle:
    """Canonical bounded bytes supplied only through a runner transport boundary."""

    config_bytes: bytes
    workload_bytes: bytes
    iap_transport_map_bytes: bytes | None

    def encode(self) -> bytes:
        """Return the only accepted canonical envelope without retaining a path."""

        config = _require_bytes(
            self.config_bytes, maximum=MAX_RUNTIME_INPUT_CONFIG_BYTES
        )
        workload = _require_bytes(
            self.workload_bytes, maximum=MAX_RUNTIME_INPUT_WORKLOAD_BYTES
        )
        iap_map = _require_bytes(
            self.iap_transport_map_bytes,
            maximum=MAX_RUNTIME_INPUT_IAP_MAP_BYTES,
            optional=True,
        )
        assert config is not None
        assert workload is not None
        value = {
            _KEY_CONFIG: _encode_base64(config),
            _KEY_IAP_MAP: None if iap_map is None else _encode_base64(iap_map),
            _KEY_SCHEMA: RUNTIME_INPUT_BUNDLE_SCHEMA_VERSION,
            _KEY_WORKLOAD: _encode_base64(workload),
        }
        try:
            encoded = canonical_json_bytes(value)
        except (TypeError, ValueError):
            raise _reject() from None
        if len(encoded) > MAX_RUNTIME_INPUT_BUNDLE_BYTES:
            raise _reject()
        return encoded

    @classmethod
    def decode(cls, content: bytes) -> RuntimeInputBundle:
        """Reject every noncanonical or unbounded envelope before any transport."""

        if (
            type(content) is not bytes
            or not 1 <= len(content) <= MAX_RUNTIME_INPUT_BUNDLE_BYTES
        ):
            raise _reject()
        try:
            value = _strict_json(content)
            if set(value) != _EXPECTED_KEYS:
                raise _BundleRejected()
            if value[_KEY_SCHEMA] != RUNTIME_INPUT_BUNDLE_SCHEMA_VERSION:
                raise _BundleRejected()
            config = _decode_base64(
                value[_KEY_CONFIG], maximum=MAX_RUNTIME_INPUT_CONFIG_BYTES
            )
            workload = _decode_base64(
                value[_KEY_WORKLOAD], maximum=MAX_RUNTIME_INPUT_WORKLOAD_BYTES
            )
            iap_map = _decode_base64(
                value[_KEY_IAP_MAP],
                maximum=MAX_RUNTIME_INPUT_IAP_MAP_BYTES,
                optional=True,
            )
            assert config is not None
            assert workload is not None
            return cls(
                config_bytes=config,
                workload_bytes=workload,
                iap_transport_map_bytes=iap_map,
            )
        except _BundleRejected:
            raise _reject() from None
