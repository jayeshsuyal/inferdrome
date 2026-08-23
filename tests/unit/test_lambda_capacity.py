"""Lambda capacity watching is exact, read-only, bounded, and secret-safe."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from inferdrome.lambda_capacity import (
    LAMBDA_A100_PCIE_DESCRIPTION,
    LAMBDA_A100_PCIE_GPU_DESCRIPTION,
    LambdaCapacityApiError,
    LambdaCapacityClient,
    LambdaCapacityError,
    LambdaCapacityObservation,
    observe_a100_pcie_capacity,
)

API_KEY = "lambda-secret-api-key-value"
NOW = datetime(2026, 8, 23, 3, 0, tzinfo=UTC)


def _offer(
    *,
    name: str = "gpu_1x_a100_pcie",
    description: str = LAMBDA_A100_PCIE_DESCRIPTION,
    gpu_description: str = LAMBDA_A100_PCIE_GPU_DESCRIPTION,
    price_cents_per_hour: int = 199,
    gpus: int = 1,
    architecture: str = "x86_64",
    regions: list[dict[str, str]] | None = None,
) -> tuple[str, dict[str, object]]:
    return (
        name,
        {
            "instance_type": {
                "architecture": architecture,
                "description": description,
                "gpu_description": gpu_description,
                "name": name,
                "price_cents_per_hour": price_cents_per_hour,
                "specs": {
                    "gpus": gpus,
                    "memory_gib": 200,
                    "storage_gib": 512,
                    "vcpus": 30,
                },
            },
            "regions_with_capacity_available": regions or [],
        },
    )


def _catalog(*offers: tuple[str, dict[str, object]]) -> dict[str, object]:
    return {"data": dict(offers)}


def _active_instance(
    instance_id: str = "a" * 32,
    *,
    status: str = "active",
) -> dict[str, object]:
    return {
        "id": instance_id,
        "jupyter_token": "must-never-escape",
        "status": status,
    }


class FakeGetTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, path: str, api_key: str, timeout: float) -> object:
        self.calls.append((path, api_key, timeout))
        if not self.responses:
            raise AssertionError("unexpected Lambda API request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_exact_available_target_is_ready_only_after_zero_instance_check() -> None:
    transport = FakeGetTransport(
        [
            _catalog(
                _offer(
                    regions=[
                        {"description": "California, USA", "name": "us-west-1"}
                    ]
                )
            ),
            {"data": []},
        ]
    )
    sleeps: list[float] = []
    client = LambdaCapacityClient(
        API_KEY,
        transport=transport,
        sleeper=sleeps.append,
        monotonic=lambda: 0,
    )

    observation = observe_a100_pcie_capacity(client, now=lambda: NOW)
    record = observation.public_record()

    assert observation.status == "READY_FOR_OPERATOR_CONFIRMATION"
    assert observation.launch_preflight_ready is True
    assert record["active_instance_count"] == 0
    assert record["hardware_attestation"] is False
    assert record["launch_authorization"] == (
        "EXPLICIT_OPERATOR_CONFIRMATION_REQUIRED"
    )
    assert record["api_methods_observed"] == ["GET"]
    assert record["api_paths_observed"] == ["/instance-types", "/instances"]
    assert record["instance_type"] == {
        "architecture": "x86_64",
        "description": "1x A100 (40 GB PCIe)",
        "gpu_description": "A100 (40 GB PCIe)",
        "gpus": 1,
        "hourly_rate_usd": "1.99",
        "name": "gpu_1x_a100_pcie",
        "regions_with_capacity": [
            {"description": "California, USA", "name": "us-west-1"}
        ],
    }
    assert transport.calls == [
        ("/instance-types", API_KEY, 20),
        ("/instances", API_KEY, 20),
    ]
    assert sleeps == [pytest.approx(1.05)]


def test_out_of_capacity_never_queries_instances_or_selects_available_sxm() -> None:
    transport = FakeGetTransport(
        [
            _catalog(
                _offer(),
                _offer(
                    name="gpu_1x_a100_sxm4",
                    description="1x A100 (40 GB SXM4)",
                    gpu_description="A100 (40 GB SXM4)",
                    regions=[
                        {"description": "Texas, USA", "name": "us-south-1"}
                    ],
                ),
            )
        ]
    )
    client = LambdaCapacityClient(API_KEY, transport=transport)

    observation = observe_a100_pcie_capacity(client, now=lambda: NOW)

    assert observation.status == "OUT_OF_CAPACITY"
    assert observation.launch_preflight_ready is False
    assert observation.instance_type is not None
    assert observation.instance_type.name == "gpu_1x_a100_pcie"
    assert observation.active_instance_count is None
    assert [call[0] for call in transport.calls] == ["/instance-types"]


@pytest.mark.parametrize(
    ("offer", "expected_status"),
    [
        (
            _offer(price_cents_per_hour=200),
            "RATE_MISMATCH",
        ),
        (
            _offer(gpu_description="A100 (40 GB SXM4)"),
            "TARGET_METADATA_MISMATCH",
        ),
        (
            _offer(gpus=2),
            "TARGET_METADATA_MISMATCH",
        ),
        (
            _offer(architecture="arm64"),
            "TARGET_METADATA_MISMATCH",
        ),
    ],
)
def test_target_metadata_and_rate_drift_fail_closed(
    offer: tuple[str, dict[str, object]],
    expected_status: str,
) -> None:
    client = LambdaCapacityClient(
        API_KEY,
        transport=FakeGetTransport([_catalog(offer)]),
    )

    observation = observe_a100_pcie_capacity(client, now=lambda: NOW)

    assert observation.status == expected_status
    assert observation.launch_preflight_ready is False


def test_target_description_must_be_unique() -> None:
    client = LambdaCapacityClient(
        API_KEY,
        transport=FakeGetTransport(
            [
                _catalog(
                    _offer(name="gpu_1x_a100_pcie_a"),
                    _offer(name="gpu_1x_a100_pcie_b"),
                )
            ]
        ),
    )

    observation = observe_a100_pcie_capacity(client, now=lambda: NOW)

    assert observation.status == "TARGET_AMBIGUOUS"
    assert observation.instance_type is None


def test_available_target_with_active_instance_is_not_launch_ready() -> None:
    transport = FakeGetTransport(
        [
            _catalog(
                _offer(
                    regions=[
                        {"description": "California, USA", "name": "us-west-1"}
                    ]
                )
            ),
            {"data": [_active_instance()]},
        ]
    )
    client = LambdaCapacityClient(
        API_KEY,
        transport=transport,
        sleeper=lambda _seconds: None,
        monotonic=lambda: 0,
    )

    observation = observe_a100_pcie_capacity(client, now=lambda: NOW)
    rendered = json.dumps(observation.public_record())

    assert observation.status == "ACTIVE_INSTANCE_CONFLICT"
    assert observation.active_instance_count == 1
    assert observation.launch_preflight_ready is False
    assert "must-never-escape" not in rendered
    assert API_KEY not in rendered


def test_contradictory_ready_observation_cannot_be_constructed() -> None:
    with pytest.raises(LambdaCapacityError, match="internally inconsistent"):
        LambdaCapacityObservation(
            active_instance_count=None,
            api_paths_observed=("/instance-types",),
            catalog_projection_sha256="sha256:" + "0" * 64,
            instance_type=None,
            observed_at=NOW,
            status="READY_FOR_OPERATOR_CONFIRMATION",
        )


@pytest.mark.parametrize(
    "response",
    [
        {"data": []},
        {"data": {"INVALID-NAME": _offer()[1]}},
        _catalog(
            (
                "gpu_1x_a100_pcie",
                {
                    **_offer()[1],
                    "regions_with_capacity_available": [
                        {"description": "California", "name": "us-west-1"},
                        {"description": "Duplicate", "name": "us-west-1"},
                    ],
                },
            )
        ),
    ],
)
def test_malformed_catalogs_reject(response: object) -> None:
    client = LambdaCapacityClient(
        API_KEY,
        transport=FakeGetTransport([response]),
    )

    with pytest.raises(LambdaCapacityApiError):
        client.list_instance_types()


def test_active_instance_shape_and_duplicates_reject() -> None:
    client = LambdaCapacityClient(
        API_KEY,
        transport=FakeGetTransport(
            [{"data": [_active_instance(), _active_instance()]}]
        ),
    )

    with pytest.raises(LambdaCapacityApiError, match="duplicate"):
        client.count_active_instances()


def test_client_requires_secret_safe_environment_key() -> None:
    with pytest.raises(LambdaCapacityError, match="missing or invalid"):
        LambdaCapacityClient.from_environment({})

    with pytest.raises(LambdaCapacityError, match="missing or invalid"):
        LambdaCapacityClient("secret with whitespace")


def test_urllib_transport_uses_get_and_explicit_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inferdrome.lambda_capacity as capacity

    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"data":{}}'

    class FakeOpener:
        def open(self, request: Any, *, timeout: float) -> FakeResponse:
            captured["method"] = request.get_method()
            captured["url"] = request.full_url
            captured["headers"] = {
                key.lower(): value for key, value in request.header_items()
            }
            captured["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(
        capacity.urllib.request,
        "build_opener",
        lambda *_handlers: FakeOpener(),
    )

    response = capacity._urllib_get("/instance-types", API_KEY, 20)

    assert response == {"data": {}}
    assert captured == {
        "headers": {
            "accept": "application/json",
            "authorization": f"Bearer {API_KEY}",
            "user-agent": capacity._API_USER_AGENT,
        },
        "method": "GET",
        "timeout": 20,
        "url": "https://cloud.lambda.ai/api/v1/instance-types",
    }

    with pytest.raises(LambdaCapacityApiError, match="not permitted"):
        capacity._urllib_get("/instance-operations/launch", API_KEY, 20)
