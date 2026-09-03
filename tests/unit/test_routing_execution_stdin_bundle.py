"""Focused contract checks for the bounded runtime stdin bundle."""

from __future__ import annotations

import base64

import pytest

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.routing_execution.stdin_bundle import (
    MAX_RUNTIME_INPUT_CONFIG_BYTES,
    MAX_RUNTIME_INPUT_IAP_MAP_BYTES,
    MAX_RUNTIME_INPUT_WORKLOAD_BYTES,
    RUNTIME_INPUT_BUNDLE_SCHEMA_VERSION,
    RuntimeInputBundle,
    RuntimeInputBundleError,
)


def _value(
    *, config: object, workload: object, iap_map: object = None
) -> dict[str, object]:
    return {
        "config_base64": config,
        "iap_transport_map_base64": iap_map,
        "schema_version": RUNTIME_INPUT_BUNDLE_SCHEMA_VERSION,
        "workload_base64": workload,
    }


def _encoded(value: dict[str, object]) -> bytes:
    return canonical_json_bytes(value)


def test_runtime_input_bundle_round_trip_is_byte_stable() -> None:
    bundle = RuntimeInputBundle(
        config_bytes=b'{"configuration":"bounded"}',
        workload_bytes=b'{"prompt":"fixed"}\n',
        iap_transport_map_bytes=b'{"map":"ephemeral"}',
    )

    encoded = bundle.encode()

    assert encoded == bundle.encode()
    assert RuntimeInputBundle.decode(encoded) == bundle
    assert RuntimeInputBundle.decode(
        RuntimeInputBundle(
            config_bytes=b"c", workload_bytes=b"w", iap_transport_map_bytes=None
        ).encode()
    ).iap_transport_map_bytes is None


@pytest.mark.parametrize(
    "content",
    (
        b"not-json",
        (
            b'{"workload_base64":"dw==", "config_base64":"Yw==", '
            b'"iap_transport_map_base64":null, "schema_version":'
            b'"inferdrome.routing-execution-runtime-input-bundle.v1"}'
        ),
        (
            b'{"config_base64":"Yw==","config_base64":"Yw==",'
            b'"iap_transport_map_base64":null,"schema_version":'
            b'"inferdrome.routing-execution-runtime-input-bundle.v1",'
            b'"workload_base64":"dw=="}'
        ),
    ),
)
def test_runtime_input_bundle_rejects_malformed_noncanonical_and_duplicate_content(
    content: bytes,
) -> None:
    with pytest.raises(RuntimeInputBundleError, match="RUNTIME_INPUT_BUNDLE_REJECTED"):
        RuntimeInputBundle.decode(content)


@pytest.mark.parametrize(
    "field,maximum",
    (
        ("config", MAX_RUNTIME_INPUT_CONFIG_BYTES),
        ("workload", MAX_RUNTIME_INPUT_WORKLOAD_BYTES),
        ("iap", MAX_RUNTIME_INPUT_IAP_MAP_BYTES),
    ),
)
def test_runtime_input_bundle_rejects_individual_oversize_fields(
    field: str, maximum: int
) -> None:
    too_large = b"x" * (maximum + 1)
    config = b"c" if field != "config" else too_large
    workload = b"w" if field != "workload" else too_large
    iap_map = b"m" if field != "iap" else too_large
    with pytest.raises(
        RuntimeInputBundleError, match="RUNTIME_INPUT_BUNDLE_REJECTED"
    ):
        RuntimeInputBundle(
            config_bytes=config,
            workload_bytes=workload,
            iap_transport_map_bytes=iap_map,
        ).encode()


def test_runtime_input_bundle_rejects_bad_base64_and_map_type() -> None:
    with pytest.raises(
        RuntimeInputBundleError, match="RUNTIME_INPUT_BUNDLE_REJECTED"
    ):
        RuntimeInputBundle.decode(
            _encoded(
                _value(
                    config="not*base64",
                    workload=base64.b64encode(b"w").decode("ascii"),
                )
            )
        )
    with pytest.raises(
        RuntimeInputBundleError, match="RUNTIME_INPUT_BUNDLE_REJECTED"
    ):
        RuntimeInputBundle.decode(
            _encoded(
                _value(
                    config=base64.b64encode(b"c").decode("ascii"),
                    workload=base64.b64encode(b"w").decode("ascii"),
                    iap_map=123,
                )
            )
        )


def test_runtime_input_bundle_rejects_total_oversize_before_json_parse() -> None:
    with pytest.raises(RuntimeInputBundleError, match="RUNTIME_INPUT_BUNDLE_REJECTED"):
        RuntimeInputBundle.decode(b"x" * 3_000_000)
