"""aiohttp 3.13 streaming transport; literal private targets, no ambient authority."""

from collections.abc import Callable

import aiohttp

from inferdrome.evaluation.contracts import EvaluationConfig
from inferdrome.evaluation.runner import TransportError
from inferdrome.evaluation.stream import IncompleteStream, StreamError


class AiohttpTransport:
    """One bounded connection pool per replay, owned/closed by run_evaluation.

    No proxies, netrc, cookies, redirects, retries, compression, DNS names,
    authorization headers, raw logging, or automatic error-body reads.
    Request deadlines and cancellation belong to the replay coordinator.
    """

    def __init__(self, config: EvaluationConfig) -> None:
        self._origins = frozenset(endpoint.origin for endpoint in config.endpoints)
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=config.bounds.concurrency),
            timeout=aiohttp.ClientTimeout(total=None),
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            read_bufsize=16_384,
            max_line_size=4096,
            max_field_size=4096,
            max_headers=64,
            headers={"Accept": "text/event-stream", "Accept-Encoding": "identity"},
        )

    async def stream(
        self,
        origin: str,
        body: bytes,
        on_headers: Callable[[int], None],
        on_bytes: Callable[[bytes], None],
    ) -> None:
        if origin not in self._origins or len(body) > 262_144:
            raise TransportError("evaluation transport input is outside its bounds")
        try:
            async with self._session.post(
                origin + "/v1/chat/completions",
                data=body,
                allow_redirects=False,
                headers={"Content-Type": "application/json"},
            ) as response:
                on_headers(response.status)
                if response.status != 200:
                    response.close()
                    return
                if (
                    response.content_type != "text/event-stream"
                    or response.headers.get("Content-Encoding", "identity")
                    != "identity"
                ):
                    raise StreamError("evaluation response encoding is unsupported")
                async for data in response.content.iter_chunked(16_384):
                    on_bytes(data)
        except aiohttp.ClientPayloadError:
            raise IncompleteStream("evaluation HTTP stream was truncated") from None
        except (aiohttp.ClientError, OSError, ValueError) as error:
            if isinstance(error, StreamError):
                raise
            raise TransportError("evaluation HTTP request failed") from None

    async def close(self) -> None:
        await self._session.close()

    @property
    def closed(self) -> bool:
        return self._session.closed
