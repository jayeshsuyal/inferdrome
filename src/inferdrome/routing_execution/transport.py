"""Injected, no-redirect HTTP transport for admitted OpenAI-compatible targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class TransportError(OSError):
    """A bounded transport operation failed without exposing provider detail."""


class TransportTimedOut(TransportError):
    """One bounded transport operation timed out."""


class TransportCancelled(TransportError):
    """An injected transport observed cancellation before completion."""


@dataclass(frozen=True)
class TransportResponse:
    """The sole bounded raw response representation retained in memory."""

    status: int
    body: bytes


class EndpointTransport(Protocol):
    """A transport factory seam used by real sockets and deterministic tests."""

    def get(self, origin: str, path: str, *, timeout_ms: int) -> TransportResponse: ...

    def post_json(
        self, origin: str, path: str, body: bytes, *, timeout_ms: int
    ) -> TransportResponse: ...

    def close(self) -> None: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class UrllibEndpointTransport:
    """Small stdlib transport with no auth surface, redirects, or retry loop."""

    _MAX_RESPONSE_BYTES = 1_048_576

    def __init__(self) -> None:
        self._opener = build_opener(ProxyHandler({}), _NoRedirectHandler())

    @staticmethod
    def _url(origin: str, path: str) -> str:
        if path not in {"/health", "/metrics", "/v1/models", "/v1/chat/completions"}:
            raise TransportError("transport path is not allowed")
        return f"{origin}{path}"

    def _request(self, request: Request, *, timeout_ms: int) -> TransportResponse:
        try:
            with self._opener.open(request, timeout=timeout_ms / 1000) as response:
                body = response.read(self._MAX_RESPONSE_BYTES + 1)
                if len(body) > self._MAX_RESPONSE_BYTES:
                    raise TransportError("transport response exceeds its bound")
                return TransportResponse(status=int(response.status), body=body)
        except HTTPError as error:
            try:
                body = error.read(self._MAX_RESPONSE_BYTES + 1)
            except OSError:
                body = b""
            if len(body) > self._MAX_RESPONSE_BYTES:
                body = b""
            return TransportResponse(status=int(error.code), body=body)
        except TimeoutError:
            raise TransportTimedOut("transport timed out") from None
        except (URLError, OSError, ValueError):
            raise TransportError("transport request failed") from None

    def get(self, origin: str, path: str, *, timeout_ms: int) -> TransportResponse:
        return self._request(
            Request(self._url(origin, path), method="GET"), timeout_ms=timeout_ms
        )

    def post_json(
        self, origin: str, path: str, body: bytes, *, timeout_ms: int
    ) -> TransportResponse:
        request = Request(
            self._url(origin, path),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        return self._request(request, timeout_ms=timeout_ms)

    def close(self) -> None:
        """The stdlib opener owns no user-visible persistent credential state."""


TransportFactory = Protocol
