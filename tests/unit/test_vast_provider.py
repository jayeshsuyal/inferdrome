"""Concrete provider tests using synthetic replies only; no live API access."""

from __future__ import annotations

import json
import ssl
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from inferdrome.deployment import vast_provider as provider
from inferdrome.deployment.vast_bootstrap import record_digest
from inferdrome.deployment.vast_control import (
    ControlIntent,
    ControlJournal,
    CreateResult,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_vast_bootstrap import launch_intent


def reply(value: object, status: int = 200) -> provider.HttpReply:
    return provider.HttpReply(status, json.dumps(value).encode())


def page(
    rows: list[dict[str, Any]], *, token: str | None = None, total: int | None = None
) -> provider.HttpReply:
    return reply(
        {
            "success": True,
            "instances": rows,
            "instances_found": len(rows),
            "total_instances": len(rows) if total is None else total,
            "next_token": token,
        }
    )


class FakeTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, bytes | None, float]] = []

    def request(
        self, method: str, path: str, body: bytes | None, *, seconds: float
    ) -> provider.HttpReply:
        self.calls.append((method, path, body, seconds))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result(seconds)
        assert isinstance(result, provider.HttpReply)
        return result


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.launch = launch_intent()
        self.intent = ControlIntent(
            launch_request_sha256=record_digest(self.launch),
            execution_deadline_utc=self.launch.execution_deadline_utc,
            cleanup_deadline_utc=self.launch.cleanup_deadline_utc,
        )
        self.path = tmp_path / "journal"
        self.path.mkdir(mode=0o700)
        self.journal = ControlJournal(self.path)
        self.journal.initialize(self.intent)
        self.journal.record("guard-ready.json", {"synthetic": True})
        self.journal.start_create(self.intent)
        self.transport = FakeTransport([])
        self.provider = provider.VastProvider(
            self.launch, journal=self.journal, transport=self.transport
        )

    def retain(self) -> None:
        self.journal.retain_created(CreateResult(new_contract=101))

    def volume_proof(self) -> None:
        self.journal.record(
            "provider-no-volumes-101.json",
            {"instance_id": 101, "pagination_exhausted": True, "volume_info": []},
        )


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    value = Harness(tmp_path)
    try:
        yield value
    finally:
        value.journal.close()


def test_reviewable_create_wire_and_one_attempt(harness: Harness) -> None:
    harness.transport.responses = [reply({"success": True, "new_contract": 101})]
    wire = provider.compiled_create_bytes(harness.launch)
    assert json.loads(wire) == {
        "image": harness.launch.container_image.reference,
        "disk": 80,
        "runtype": "args",
        "target_state": "running",
        "user": "2000:0",
        "env": {},
        "args": [
            "bootstrap",
            "--run-nonce",
            harness.launch.run_nonce,
            "--intent-sha256",
            record_digest(harness.launch),
            "--deadline",
            harness.launch.execution_deadline_utc,
        ],
    }
    assert provider.compiled_create_sha256(harness.launch) == sha256_digest(wire)
    assert harness.provider.create(harness.intent, seconds=1).new_contract == 101
    assert harness.transport.calls[0][:3] == ("PUT", "/api/v0/asks/202/", wire)
    with pytest.raises(FileExistsError):
        harness.provider.create(harness.intent, seconds=1)
    assert len(harness.transport.calls) == 1
    assert harness.journal.exact_instance_id() is None
    assert json.loads((harness.path / "provider-create-attempt.json").read_bytes()) == {
        "intent_sha256": harness.intent.intent_sha256,
        "wire_sha256": sha256_digest(wire),
    }


@pytest.mark.parametrize(
    "result",
    [
        TimeoutError("secret-token"),
        reply({"success": False, "msg": "secret-token"}),
        reply({"success": "true", "new_contract": 101}),
        reply({"success": True, "new_contract": True}),
        reply({"success": True, "new_contract": "101"}),
        reply({"success": True, "offer_id": 202}),
        reply({"success": True, "new_contract": 101}, 302),
        reply({"success": True, "new_contract": 101}, 429),
        provider.HttpReply(
            200, b'{"success":true,"new_contract":101,"new_contract":202}'
        ),
        provider.HttpReply(200, b'{"success":true,"new_contract":101,"extra":NaN}'),
    ],
)
def test_ambiguous_create_never_retries_or_infers_id(
    harness: Harness, result: Any
) -> None:
    harness.transport.responses = [result]
    with pytest.raises(provider.ProviderFailure) as error:
        harness.provider.create(harness.intent, seconds=1)
    assert "secret-token" not in str(error.value)
    assert harness.journal.exact_instance_id() is None
    with pytest.raises(FileExistsError):
        harness.provider.create(harness.intent, seconds=1)
    assert len(harness.transport.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("launch_request_sha256", "sha256:" + "0" * 64),
        ("execution_deadline_utc", "2030-01-01T00:00:00Z"),
        ("cleanup_deadline_utc", "2030-01-01T00:01:00Z"),
    ],
)
def test_create_binds_all_control_fields(
    harness: Harness, field: str, value: str
) -> None:
    changed = harness.intent.model_copy(update={field: value})
    with pytest.raises(provider.ProviderFailure, match="LAUNCH_MISMATCH"):
        harness.provider.create(changed, seconds=1)
    assert harness.transport.calls == []


def test_retained_id_blocks_create_even_without_provider_attempt(
    harness: Harness,
) -> None:
    harness.retain()
    with pytest.raises(provider.ProviderFailure, match="ALREADY_RETAINED"):
        harness.provider.create(harness.intent, seconds=1)
    assert harness.transport.calls == []


def test_destroy_ack_and_volume_proof_are_durable_before_absence(
    harness: Harness,
) -> None:
    harness.retain()

    def acknowledge(seconds: float) -> provider.HttpReply:
        assert seconds > 0
        assert harness.journal.has("provider-no-volumes-101.json")
        assert not harness.journal.has("provider-destroy-ack-101.json")
        return reply({"success": True, "msg": "raw-provider-secret"})

    harness.transport.responses = [
        page([{"id": 101, "volume_info": []}]),
        acknowledge,
        page([]),
    ]
    assert harness.provider.destroy(101, seconds=1).acknowledged
    assert harness.journal.has("provider-destroy-ack-101.json")
    absence = harness.provider.observe_absence(101, seconds=1)
    assert absence.pagination_exhausted and absence.succeeded
    assert absence.matching_instance_ids == absence.persistent_volume_ids == ()
    assert [call[0] for call in harness.transport.calls] == ["GET", "DELETE", "GET"]
    assert not any(
        b"raw-provider-secret" in f.read_bytes() for f in harness.path.iterdir()
    )


@pytest.mark.parametrize(
    "row",
    [
        {"id": 101},
        {"id": 101, "volume_info": None},
        {"id": 101, "volume_info": {}},
        {"id": 101, "volume_info": ""},
    ],
)
def test_missing_volume_inventory_still_deletes_without_confirming(
    harness: Harness, row: dict[str, Any]
) -> None:
    harness.retain()
    harness.transport.responses = [page([row]), reply({"success": True}), page([])]
    assert harness.provider.destroy(101, seconds=1).acknowledged
    with pytest.raises(provider.ProviderFailure, match="VOLUMES_UNCONFIRMED"):
        harness.provider.observe_absence(101, seconds=1)
    assert [call[0] for call in harness.transport.calls] == ["GET", "DELETE", "GET"]
    assert harness.transport.calls[1][1] == "/api/v0/instances/101/"
    assert not harness.journal.has("provider-no-volumes-101.json")
    assert harness.journal.has("provider-destroy-ack-101.json")


@pytest.mark.parametrize("acknowledged", [False, True])
def test_absent_instance_still_requires_actual_delete_ack_and_volume_proof(
    harness: Harness,
    acknowledged: bool,
) -> None:
    harness.retain()
    harness.transport.responses = [page([]), reply({"success": acknowledged}), page([])]
    if acknowledged:
        assert harness.provider.destroy(101, seconds=1).acknowledged
    else:
        with pytest.raises(provider.ProviderFailure):
            harness.provider.destroy(101, seconds=1)
    with pytest.raises(provider.ProviderFailure, match="VOLUMES_UNCONFIRMED"):
        harness.provider.observe_absence(101, seconds=1)
    assert harness.journal.has("provider-destroy-ack-101.json") == acknowledged
    assert not harness.journal.has("provider-no-volumes-101.json")
    assert harness.transport.calls[1][:3] == ("DELETE", "/api/v0/instances/101/", None)


@pytest.mark.parametrize(
    "responses",
    [
        [TimeoutError("synthetic-secret")],
        [reply({"success": False, "msg": "synthetic-secret"})],
        [reply({"success": True}, 503)],
        [provider.HttpReply(200, b"malformed synthetic-secret")],
        [page([{"id": 202, "volume_info": []}])],
        [page([], total=1)],
        [page([], token="more"), TimeoutError("synthetic-secret")],
        [page([], token="same"), page([], token="same")],
    ],
)
def test_failed_or_incomplete_readback_still_deletes_only_retained_id(
    harness: Harness, responses: list[Any]
) -> None:
    harness.retain()
    harness.transport.responses = [*responses, reply({"success": True}), page([])]
    assert harness.provider.destroy(101, seconds=1).acknowledged
    assert harness.provider.destroy(101, seconds=1).acknowledged
    with pytest.raises(provider.ProviderFailure, match="VOLUMES_UNCONFIRMED"):
        harness.provider.observe_absence(101, seconds=1)
    deletes = [call for call in harness.transport.calls if call[0] == "DELETE"]
    assert len(deletes) == 1 and deletes[0][:3] == (
        "DELETE",
        "/api/v0/instances/101/",
        None,
    )
    assert not harness.journal.has("provider-no-volumes-101.json")
    assert not any(
        b"synthetic-secret" in f.read_bytes() for f in harness.path.iterdir()
    )


def test_hanging_metadata_leaves_original_budget_for_delete(harness: Harness) -> None:
    def hang(seconds: float) -> provider.HttpReply:
        assert 0 < seconds <= 0.1
        time.sleep(2)
        raise AssertionError("readback timer did not interrupt")

    def acknowledge(seconds: float) -> provider.HttpReply:
        assert 0.1 < seconds < 0.25
        return reply({"success": True})

    harness.retain()
    harness.transport.responses = [hang, acknowledge]
    started = time.monotonic()
    assert harness.provider.destroy(101, seconds=0.3).acknowledged
    assert time.monotonic() - started < 1
    assert [call[0] for call in harness.transport.calls] == ["GET", "DELETE"]
    assert not harness.journal.has("provider-no-volumes-101.json")


def test_nonempty_volumes_are_sticky_and_never_invent_volume_ids(
    harness: Harness,
) -> None:
    harness.retain()
    harness.transport.responses = [
        page([{"id": 101, "volume_info": [{"unknown_shape": "sensitive"}]}]),
        reply({"success": True}),
        page([]),
    ]
    assert harness.provider.destroy(101, seconds=1).acknowledged
    record = (harness.path / "unexpected-volumes.json").read_bytes()
    assert b"UNCONFIRMED" in record and b"sensitive" not in record
    assert b"volume_info_sha256" in record
    with pytest.raises(provider.ProviderFailure, match="VOLUMES_UNCONFIRMED"):
        harness.provider.observe_absence(101, seconds=1)


def test_sticky_volume_publication_failure_is_not_metadata_fallback(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.retain()
    harness.volume_proof()
    proof_path = harness.path / "provider-no-volumes-101.json"
    prior_proof = proof_path.read_bytes()
    harness.transport.responses = [
        page([{"id": 101, "volume_info": [{"synthetic_volume": 202}]}]),
        reply({"success": True}),
    ]
    failure = OSError("synthetic journal publication failure")
    attempted: list[str] = []

    def fail_publication(name: str, value: dict[str, Any]) -> None:
        attempted.append(name)
        assert name == "unexpected-volumes.json"
        assert value["instance_id"] == 101
        raise failure

    monkeypatch.setattr(harness.journal, "record_once", fail_publication)
    with pytest.raises(OSError) as error:
        harness.provider.destroy(101, seconds=1)
    assert error.value is failure
    assert attempted == ["unexpected-volumes.json"]
    assert [call[0] for call in harness.transport.calls] == ["GET"]
    assert not harness.journal.has("provider-destroy-ack-101.json")
    assert not harness.journal.has("cleanup-confirmed.json")
    assert proof_path.read_bytes() == prior_proof


def test_prior_volume_proof_survives_failed_readback_and_actual_delete(
    harness: Harness,
) -> None:
    harness.retain()
    harness.transport.responses = [
        page([{"id": 101, "volume_info": []}]),
        TimeoutError("synthetic first delete timeout"),
        TimeoutError("synthetic later metadata timeout"),
        reply({"success": True}),
        page([]),
    ]
    with pytest.raises(provider.ProviderFailure):
        harness.provider.destroy(101, seconds=1)
    proof_path = harness.path / "provider-no-volumes-101.json"
    prior_proof = proof_path.read_bytes()
    assert not harness.journal.has("provider-destroy-ack-101.json")

    destroyed = harness.provider.destroy(101, seconds=1)
    absence = harness.provider.observe_absence(101, seconds=1)
    assert destroyed.instance_id == absence.query_instance_id == 101
    assert destroyed.acknowledged and absence.succeeded
    assert absence.pagination_exhausted
    assert absence.matching_instance_ids == absence.persistent_volume_ids == ()
    assert harness.journal.has("provider-destroy-ack-101.json")
    assert not harness.journal.has("unexpected-volumes.json")
    assert proof_path.read_bytes() == prior_proof
    assert [call[0] for call in harness.transport.calls] == [
        "GET",
        "DELETE",
        "GET",
        "DELETE",
        "GET",
    ]
    assert all(
        call[1] == "/api/v0/instances/101/"
        for call in harness.transport.calls
        if call[0] == "DELETE"
    )


def test_cleanup_clone_cannot_create_or_request_logs_and_reuses_ack(
    harness: Harness,
) -> None:
    harness.retain()
    harness.transport.responses = [
        page([{"id": 101, "volume_info": []}]),
        reply({"success": True}),
    ]
    clone = harness.provider.cleanup_for(harness.journal)
    with pytest.raises(provider.ProviderFailure, match="CLEANUP_ONLY"):
        clone.create(harness.intent, seconds=1)
    with pytest.raises(provider.ProviderFailure, match="CLEANUP_ONLY"):
        clone.request_logs(101, seconds=1)
    assert clone.destroy(101, seconds=1).acknowledged
    assert clone.destroy(101, seconds=1).acknowledged
    assert len(harness.transport.calls) == 2


@pytest.mark.parametrize("method", ["destroy", "observe_absence", "request_logs"])
def test_exact_retained_id_required_for_every_later_operation(
    harness: Harness, method: str
) -> None:
    harness.retain()
    with pytest.raises(provider.ProviderFailure, match="EXACT_ID"):
        getattr(harness.provider, method)(202, seconds=1)
    assert harness.transport.calls == []


def test_complete_pagination_keeps_exact_filter_and_encodes_token(
    harness: Harness,
) -> None:
    harness.retain()
    harness.volume_proof()
    harness.transport.responses = [page([], token="token+/=", total=0), page([])]
    assert harness.provider.observe_absence(101, seconds=1).matching_instance_ids == ()
    for _, path, body, _ in harness.transport.calls:
        parsed = urlsplit(path)
        assert parsed.path == "/api/v1/instances/" and body is None
        query = parse_qs(parsed.query)
        assert json.loads(query["select_filters"][0]) == {"id": {"eq": 101}}
        assert query["limit"] == ["25"]
    assert parse_qs(urlsplit(harness.transport.calls[1][1]).query)["after_token"] == [
        "token+/="
    ]


@pytest.mark.parametrize(
    "responses",
    [
        [
            reply(
                {
                    "success": True,
                    "instances": [],
                    "instances_found": 0,
                    "total_instances": 0,
                }
            )
        ],
        [
            reply(
                {
                    "success": True,
                    "instances": [],
                    "instances_found": True,
                    "total_instances": 0,
                    "next_token": None,
                }
            )
        ],
        [page([{"id": 202, "volume_info": []}])],
        [page([{"id": 101}, {"id": 101}], total=1)],
        [page([], token="same"), page([], token="same")],
        [page([], token="more"), TimeoutError("secret")],
        [page([], token="more", total=1), page([], total=0)],
        [page([], total=1)],
    ],
)
def test_partial_or_malformed_absence_never_confirms(
    harness: Harness, responses: list[Any]
) -> None:
    harness.retain()
    harness.volume_proof()
    harness.transport.responses = responses
    with pytest.raises(provider.ProviderFailure):
        harness.provider.observe_absence(101, seconds=1)


def test_real_local_sleeping_fake_is_interrupted_without_retry(
    harness: Harness,
) -> None:
    def hang(seconds: float) -> provider.HttpReply:
        time.sleep(2)
        raise AssertionError("timer did not interrupt")

    harness.transport.responses = [hang]
    start = time.monotonic()
    with pytest.raises(provider.ProviderFailure):
        harness.provider.create(harness.intent, seconds=0.05)
    assert time.monotonic() - start < 1
    assert len(harness.transport.calls) == 1
    assert harness.journal.exact_instance_id() is None


@pytest.mark.parametrize(
    "result",
    [
        TimeoutError("synthetic-secret"),
        reply({"success": False, "msg": "synthetic-secret"}),
        reply({"success": True}, 404),
        reply({"success": "true"}),
    ],
)
def test_failed_destroy_never_publishes_acknowledgement(
    harness: Harness, result: Any
) -> None:
    harness.retain()
    harness.transport.responses = [
        page([{"id": 101, "volume_info": []}]),
        result,
    ]
    with pytest.raises(provider.ProviderFailure) as error:
        harness.provider.destroy(101, seconds=1)
    assert "synthetic-secret" not in str(error.value)
    assert not harness.journal.has("provider-destroy-ack-101.json")
    assert [call[0] for call in harness.transport.calls] == ["GET", "DELETE"]


def test_hanging_destroy_call_is_bounded_and_never_acknowledged(
    harness: Harness,
) -> None:
    def hang(seconds: float) -> provider.HttpReply:
        time.sleep(2)
        raise AssertionError("timer did not interrupt")

    harness.retain()
    harness.transport.responses = [page([{"id": 101, "volume_info": []}]), hang]
    started = time.monotonic()
    with pytest.raises(provider.ProviderFailure):
        harness.provider.destroy(101, seconds=0.05)
    assert time.monotonic() - started < 1
    assert not harness.journal.has("provider-destroy-ack-101.json")
    assert len(harness.transport.calls) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://s3.amazonaws.com/vast.ai/instance_logs/ok.log?secret=1",
        "https://s3.amazonaws.com/vast.ai/instance_logs/ok.log#fragment",
        "https://s3.amazonaws.com/vast.ai/instance_logs/../other.log",
        "https://s3.amazonaws.com/vast.ai/instance_logs/%61.log",
        "https://elsewhere.invalid/vast.ai/instance_logs/a.log",
        "https://s3.amazonaws.com:443/vast.ai/instance_logs/a.log",
    ],
)
def test_request_logs_rejects_unsafe_result_url(harness: Harness, url: str) -> None:
    harness.retain()
    harness.transport.responses = [reply({"success": True, "result_url": url})]
    with pytest.raises(provider.ProviderFailure, match="LOG_URL"):
        harness.provider.request_logs(101, seconds=1)


def test_request_logs_only_returns_url_and_never_downloads_it(harness: Harness) -> None:
    harness.retain()
    url = "https://s3.amazonaws.com/vast.ai/instance_logs/synthetic-ABC_01.log"
    harness.transport.responses = [reply({"success": True, "result_url": url})]
    assert harness.provider.request_logs(101, seconds=1) == url
    assert len(harness.transport.calls) == 1
    assert harness.transport.calls[0][:3] == (
        "PUT",
        "/api/v0/instances/request_logs/101/",
        b"{}",
    )


def test_explicit_credentials_and_no_duplicate_transport(harness: Harness) -> None:
    with pytest.raises(provider.ProviderFailure, match="CREDENTIAL_REQUIRED"):
        provider.VastProvider(None, journal=harness.journal)
    with pytest.raises(provider.ProviderFailure, match="DUPLICATE_TRANSPORT"):
        provider.VastProvider(
            None,
            journal=harness.journal,
            api_key="synthetic",
            transport=harness.transport,
        )
    assert "synthetic-secret" not in repr(
        provider.VastHttpsTransport("synthetic-secret")
    )


def test_https_pins_authority_tls_and_does_not_use_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    content = canonical_json_bytes({"success": True})

    class Connection:
        def __init__(self, host: str, port: int, **kwargs: Any) -> None:
            observed.update(host=host, port=port, **kwargs)

        def request(self, method: str, path: str, **kwargs: Any) -> None:
            observed.update(method=method, path=path, **kwargs)

        def getresponse(self) -> Any:
            return self

        status = 200

        def getheader(self, name: str) -> str | None:
            return str(len(content)) if name == "Content-Length" else None

        def read(self, bound: int) -> bytes:
            assert bound == provider.MAX_RESPONSE_BYTES + 1
            return content

        def close(self) -> None:
            observed["closed"] = True

    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid")
    monkeypatch.setattr(provider.http.client, "HTTPSConnection", Connection)
    result = provider.VastHttpsTransport("synthetic-secret").request(
        "PUT", "/api/v0/asks/202/", b"{}", seconds=1
    )
    assert result.body == content
    assert observed["host"] == "console.vast.ai" and observed["port"] == 443
    assert observed["context"].verify_mode == ssl.CERT_REQUIRED
    assert observed["context"].check_hostname
    assert observed["headers"]["Authorization"] == "Bearer synthetic-secret"
    assert observed["closed"] is True


@pytest.mark.parametrize(
    "path",
    [
        "https://elsewhere.invalid/api/v0/asks/202/",
        "//elsewhere.invalid/api/v0/asks/202/",
        "/api/v0/asks/202/?api_key=secret",
        "/api/v0/asks/202/#fragment",
        "/api/v0/asks/202/\r\nInjected:1",
    ],
)
def test_https_rejects_authority_or_route_overrides_before_connect(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("connection must not be constructed")

    monkeypatch.setattr(provider.http.client, "HTTPSConnection", unexpected)
    with pytest.raises(provider.ProviderFailure, match="ROUTE_INVALID"):
        provider.VastHttpsTransport("synthetic").request("PUT", path, b"{}", seconds=1)


@pytest.mark.parametrize(
    "status,length,encoding,oversized",
    [
        (302, None, None, False),
        (429, None, None, False),
        (200, "1048577", None, False),
        (200, "malformed", None, False),
        (200, None, "gzip", False),
        (200, None, None, True),
    ],
)
def test_tls_rejects_redirects_retry_codes_and_unbounded_responses(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    length: str | None,
    encoding: str | None,
    oversized: bool,
) -> None:
    calls: list[str] = []

    class Connection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls.append("connect")
            self.status = status

        def request(self, *args: Any, **kwargs: Any) -> None:
            calls.append("request")

        def getresponse(self) -> Any:
            return self

        def getheader(self, name: str) -> str | None:
            return length if name == "Content-Length" else encoding

        def read(self, bound: int) -> bytes:
            assert oversized
            assert bound == provider.MAX_RESPONSE_BYTES + 1
            return b"x" * bound

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(provider.http.client, "HTTPSConnection", Connection)
    with pytest.raises(provider.ProviderFailure, match="REQUEST_FAILED"):
        provider.VastHttpsTransport("synthetic").request(
            "PUT", "/api/v0/asks/202/", b"{}", seconds=1
        )
    assert calls == ["connect", "request", "close"]
