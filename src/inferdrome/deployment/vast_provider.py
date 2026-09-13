"""One-create Vast HTTPS adapter with exact-ID, fail-closed cleanup evidence.

The authority and wire profile are fixed. Credentials are supplied explicitly;
no environment, config, registry login, proxy or credential discovery is used.
See Vast's create/show/destroy docs and vast-cli vastai/api/instances.py for the
wire contract. No official-client retry machinery is reused here.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from inferdrome.deployment.vast_bootstrap import LaunchIntent, record_digest
from inferdrome.deployment.vast_control import (
    UNCONFIRMED,
    AbsenceResult,
    ControlFailure,
    ControlIntent,
    ControlJournal,
    CreateResult,
    DestroyResult,
    _bounded_call,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

AUTHORITY = "https://console.vast.ai"
DISK_GB = 80
MAX_RESPONSE_BYTES = 1_048_576
MAX_PAGES = 32
_LOG_URL = re.compile(
    r"https://s3\.amazonaws\.com/vast\.ai/instance_logs/[A-Za-z0-9_-]{1,128}\.log\Z"
)


class ProviderFailure(ControlFailure):
    """Sanitized failure only; never retain raw API data or transport errors."""


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: bytes


class HttpsTransport(Protocol):
    def request(
        self, method: str, path: str, body: bytes | None, *, seconds: float
    ) -> HttpReply: ...


def _id(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 9_007_199_254_740_991:
        raise ProviderFailure("VAST_PROVIDER_ID_INVALID")
    return value


def _deadline(seconds: float, monotonic: Callable[[], float]) -> float:
    if (
        type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or not 0 < seconds <= 60
    ):
        raise ProviderFailure("VAST_PROVIDER_DEADLINE")
    return monotonic() + seconds


def _remaining(end: float, monotonic: Callable[[], float]) -> float:
    seconds = end - monotonic()
    if not math.isfinite(seconds) or seconds <= 0:
        raise ProviderFailure("VAST_PROVIDER_DEADLINE")
    return seconds


def _route(method: str, path: str) -> None:
    parsed = urlsplit(path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or any(ord(value) < 33 or ord(value) > 126 for value in path)
        or len(path) > 8192
    ):
        raise ProviderFailure("VAST_PROVIDER_ROUTE_INVALID")
    allowed = (
        (method == "GET" and parsed.path == "/api/v1/instances/")
        or (
            method == "PUT"
            and not parsed.query
            and re.fullmatch(
                r"/api/v0/(?:asks|instances/request_logs)/[1-9][0-9]{0,15}/",
                parsed.path,
            )
            is not None
        )
        or (
            method == "DELETE"
            and not parsed.query
            and re.fullmatch(r"/api/v0/instances/[1-9][0-9]{0,15}/", parsed.path)
            is not None
        )
    )
    if not allowed:
        raise ProviderFailure("VAST_PROVIDER_ROUTE_INVALID")


class VastHttpsTransport:
    """Fresh verified TLS connection per request; no redirect/proxy machinery."""

    def __init__(self, api_key: str) -> None:
        if (
            not isinstance(api_key, str)
            or not 1 <= len(api_key) <= 4096
            or not api_key.isascii()
            or any(not 33 <= ord(c) <= 126 for c in api_key)
        ):
            raise ProviderFailure("VAST_PROVIDER_CREDENTIAL_INVALID")
        self._api_key = api_key

    def __repr__(self) -> str:
        return f"VastHttpsTransport(authority={AUTHORITY!r})"

    def request(
        self, method: str, path: str, body: bytes | None, *, seconds: float
    ) -> HttpReply:
        _route(method, path)
        _deadline(seconds, time.monotonic)
        if body is not None and (type(body) is not bytes or len(body) > 131_072):
            raise ProviderFailure("VAST_PROVIDER_REQUEST_INVALID")

        def exchange() -> HttpReply:
            connection = http.client.HTTPSConnection(
                "console.vast.ai",
                443,
                timeout=seconds,
                context=ssl.create_default_context(),
            )
            try:
                connection.request(
                    method,
                    path,
                    body=body,
                    headers={
                        "Authorization": "Bearer " + self._api_key,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Content-Type": "application/json",
                        "User-Agent": "Inferdrome-Vast-Control/1",
                    },
                )
                response = connection.getresponse()
                if response.status != 200 or response.getheader(
                    "Content-Encoding"
                ) not in (None, "identity"):
                    raise ProviderFailure("VAST_PROVIDER_HTTP_REJECTED")
                length = response.getheader("Content-Length")
                if length is not None and (
                    not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES
                ):
                    raise ProviderFailure("VAST_PROVIDER_RESPONSE_LIMIT")
                content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ProviderFailure("VAST_PROVIDER_RESPONSE_LIMIT")
                return HttpReply(response.status, content)
            finally:
                connection.close()

        try:
            return _bounded_call(exchange, seconds)
        except Exception:
            raise ProviderFailure("VAST_PROVIDER_REQUEST_FAILED") from None


def compiled_create_bytes(launch: LaunchIntent) -> bytes:
    """Reviewable deterministic wire bytes; no constructor payload overrides."""
    launch = LaunchIntent.model_validate(launch.model_dump(mode="python"), strict=True)
    return canonical_json_bytes(
        {
            "image": launch.container_image.reference,
            "disk": DISK_GB,
            "runtype": "args",
            "target_state": "running",
            "user": "2000:0",
            "env": {},
            "args": [
                "bootstrap",
                "--run-nonce",
                launch.run_nonce,
                "--intent-sha256",
                record_digest(launch),
                "--deadline",
                launch.execution_deadline_utc,
            ],
        }
    )


def compiled_create_sha256(launch: LaunchIntent) -> str:
    return sha256_digest(compiled_create_bytes(launch))


def _json_reply(reply: HttpReply) -> dict[str, Any]:
    if (
        not isinstance(reply, HttpReply)
        or type(reply.status) is not int
        or reply.status != 200
        or type(reply.body) is not bytes
        or len(reply.body) > MAX_RESPONSE_BYTES
    ):
        raise ProviderFailure("VAST_PROVIDER_RESPONSE_INVALID")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    def invalid_constant(value: str) -> None:
        raise ValueError

    try:
        value = json.loads(
            reply.body, object_pairs_hook=unique, parse_constant=invalid_constant
        )
        if not isinstance(value, dict) or value.get("success") is not True:
            raise ValueError
        return value
    except (ValueError, RecursionError):
        raise ProviderFailure("VAST_PROVIDER_RESPONSE_INVALID") from None


class VastProvider:
    def __init__(
        self,
        launch: LaunchIntent | None,
        *,
        journal: ControlJournal,
        api_key: str | None = None,
        transport: HttpsTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._launch = (
            LaunchIntent.model_validate(launch.model_dump(mode="python"), strict=True)
            if launch is not None
            else None
        )
        self._journal = journal
        if transport is None:
            if api_key is None:
                raise ProviderFailure("VAST_PROVIDER_CREDENTIAL_REQUIRED")
            transport = VastHttpsTransport(api_key)
        elif api_key is not None:
            raise ProviderFailure("VAST_PROVIDER_DUPLICATE_TRANSPORT")
        self._transport = transport
        self._monotonic = monotonic

    def __repr__(self) -> str:
        mode = self._launch is None
        return f"VastProvider(authority={AUTHORITY!r}, cleanup_only={mode})"

    def cleanup_for(self, journal: ControlJournal) -> VastProvider:
        return VastProvider(
            None, journal=journal, transport=self._transport, monotonic=self._monotonic
        )

    def _request(
        self, method: str, path: str, body: bytes | None, end: float
    ) -> dict[str, Any]:
        try:
            _route(method, path)
            seconds = _remaining(end, self._monotonic)
            reply = _bounded_call(
                lambda: self._transport.request(method, path, body, seconds=seconds),
                seconds,
            )
            _remaining(end, self._monotonic)
            return _json_reply(reply)
        except Exception:
            raise ProviderFailure("VAST_PROVIDER_OPERATION_FAILED") from None

    def _authorized_id(self, instance_id: int) -> int:
        instance_id = _id(instance_id)
        if self._journal.exact_instance_id() != instance_id:
            raise ProviderFailure("VAST_PROVIDER_EXACT_ID_REQUIRED")
        return instance_id

    def _record_matches(self, name: str, expected: dict[str, Any]) -> bool:
        with self._journal._locked():
            observed = self._journal._read(name)
        if observed is None:
            return False
        if canonical_json_bytes(observed) != canonical_json_bytes(expected):
            raise ProviderFailure("VAST_PROVIDER_RECORD_INVALID")
        return True

    def create(self, intent: ControlIntent, *, seconds: float) -> CreateResult:
        end = _deadline(seconds, self._monotonic)
        launch = self._launch
        if launch is None:
            raise ProviderFailure("VAST_PROVIDER_CLEANUP_ONLY")
        if (
            intent.launch_request_sha256 != record_digest(launch)
            or intent.execution_deadline_utc != launch.execution_deadline_utc
            or intent.cleanup_deadline_utc != launch.cleanup_deadline_utc
        ):
            raise ProviderFailure("VAST_PROVIDER_LAUNCH_MISMATCH")
        self._journal.require_intent(intent)
        if not self._journal.has("create-start.json") or not self._journal.has(
            "guard-ready.json"
        ):
            raise ProviderFailure("VAST_PROVIDER_CREATE_NOT_ARMED")
        if self._journal.exact_instance_id() is not None:
            raise ProviderFailure("VAST_PROVIDER_CREATE_ALREADY_RETAINED")
        body = compiled_create_bytes(launch)
        self._journal.record(
            "provider-create-attempt.json",
            {
                "intent_sha256": intent.intent_sha256,
                "wire_sha256": sha256_digest(body),
            },
        )
        result = self._request(
            "PUT", f"/api/v0/asks/{_id(launch.offer_id)}/", body, end
        )
        return CreateResult(new_contract=_id(result.get("new_contract")))

    def _instances(self, instance_id: int, end: float) -> list[dict[str, Any]]:
        params = {
            "select_filters": canonical_json_bytes(
                {"id": {"eq": instance_id}}
            ).decode(),
            "order_by": '[{"col":"id","dir":"asc"}]',
            "limit": "25",
        }
        rows: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        total: int | None = None
        for _ in range(MAX_PAGES):
            data = self._request(
                "GET", "/api/v1/instances/?" + urlencode(params), None, end
            )
            page = data.get("instances")
            if (
                not isinstance(page, list)
                or len(page) > 25
                or type(data.get("instances_found")) is not int
                or data["instances_found"] != len(page)
                or type(data.get("total_instances")) is not int
                or not 0 <= data["total_instances"] <= 1
                or "next_token" not in data
            ):
                raise ProviderFailure("VAST_PROVIDER_INVENTORY_INVALID")
            if total is not None and total != data["total_instances"]:
                raise ProviderFailure("VAST_PROVIDER_INVENTORY_CHANGED")
            total = data["total_instances"]
            for row in page:
                if (
                    not isinstance(row, dict)
                    or _id(row.get("id")) != instance_id
                    or rows
                ):
                    raise ProviderFailure("VAST_PROVIDER_INVENTORY_INVALID")
                rows.append(row)
            token = data["next_token"]
            if token is None:
                if total != len(rows):
                    raise ProviderFailure("VAST_PROVIDER_INVENTORY_INCOMPLETE")
                return rows
            if (
                not isinstance(token, str)
                or not 1 <= len(token) <= 2048
                or not token.isascii()
                or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or token in seen_tokens
            ):
                raise ProviderFailure("VAST_PROVIDER_CURSOR_INVALID")
            seen_tokens.add(token)
            params["after_token"] = token
        raise ProviderFailure("VAST_PROVIDER_PAGE_LIMIT")

    def _volume_inventory(self, instance_id: int, rows: list[dict[str, Any]]) -> bool:
        if not rows:
            return False
        inventory = rows[0].get("volume_info")
        if not isinstance(inventory, list):
            raise ProviderFailure("VAST_PROVIDER_VOLUME_INVENTORY_MISSING")
        if inventory:
            self._journal.record_once(
                "unexpected-volumes.json",
                {
                    "instance_id": instance_id,
                    "status": UNCONFIRMED,
                    "inventory_status": "NONEMPTY_VOLUME_INFO_IDS_UNRESOLVED",
                    "volume_info_sha256": sha256_digest(
                        canonical_json_bytes(inventory)
                    ),
                },
            )
            return False
        return True

    @staticmethod
    def _no_volumes(instance_id: int) -> dict[str, Any]:
        return {
            "instance_id": instance_id,
            "pagination_exhausted": True,
            "volume_info": [],
        }

    def destroy(self, instance_id: int, *, seconds: float) -> DestroyResult:
        end = _deadline(seconds, self._monotonic)
        instance_id = self._authorized_id(instance_id)
        acknowledged = {"instance_id": instance_id, "acknowledged": True}
        if self._record_matches(
            f"provider-destroy-ack-{instance_id}.json", acknowledged
        ):
            return DestroyResult(instance_id=instance_id, acknowledged=True)
        rows = self._instances(instance_id, end)
        empty_volumes = self._volume_inventory(instance_id, rows)
        if not rows:
            # Absence cannot manufacture a destroy acknowledgement or volume proof.
            return DestroyResult(instance_id=instance_id, acknowledged=False)
        if empty_volumes:
            self._journal.record_once(
                f"provider-no-volumes-{instance_id}.json", self._no_volumes(instance_id)
            )
            self._record_matches(
                f"provider-no-volumes-{instance_id}.json", self._no_volumes(instance_id)
            )
        self._request("DELETE", f"/api/v0/instances/{instance_id}/", None, end)
        self._journal.record_once(
            f"provider-destroy-ack-{instance_id}.json", acknowledged
        )
        self._record_matches(f"provider-destroy-ack-{instance_id}.json", acknowledged)
        return DestroyResult(instance_id=instance_id, acknowledged=True)

    def observe_absence(self, instance_id: int, *, seconds: float) -> AbsenceResult:
        end = _deadline(seconds, self._monotonic)
        instance_id = self._authorized_id(instance_id)
        rows = self._instances(instance_id, end)
        self._volume_inventory(instance_id, rows)
        if self._journal.has("unexpected-volumes.json") or not self._record_matches(
            f"provider-no-volumes-{instance_id}.json", self._no_volumes(instance_id)
        ):
            raise ProviderFailure("VAST_PROVIDER_RESIDUAL_VOLUMES_UNCONFIRMED")
        return AbsenceResult(
            query_instance_id=instance_id,
            succeeded=True,
            matching_instance_ids=tuple(row["id"] for row in rows),
            pagination_exhausted=True,
            persistent_volume_ids=(),
        )

    def request_logs(self, instance_id: int, *, seconds: float) -> str:
        end = _deadline(seconds, self._monotonic)
        if self._launch is None:
            raise ProviderFailure("VAST_PROVIDER_CLEANUP_ONLY")
        instance_id = self._authorized_id(instance_id)
        result = self._request(
            "PUT", f"/api/v0/instances/request_logs/{instance_id}/", b"{}", end
        )
        url = result.get("result_url")
        if not isinstance(url, str) or _LOG_URL.fullmatch(url) is None:
            raise ProviderFailure("VAST_PROVIDER_LOG_URL_INVALID")
        return url
