"""Bounded parsing of one OpenAI-compatible chat completion SSE stream.

Content timestamps measure receipt of the complete SSE frame. One frame may carry
multiple generated tokens, so these are content-event timings, never exact token
timings. The parser retains no generated text or raw server error messages.
"""

import json
from functools import partial

from inferdrome.parsing import (
    BoundedParseError,
    StructuredDataLimits,
    bounded_json_float,
    bounded_json_int,
    validate_json_structure,
)


class StreamError(ValueError):
    """The server stream is invalid; messages contain no server-provided data."""


class StreamLimitError(StreamError):
    """The stream exceeded a configured resource ceiling."""


class IncompleteStream(StreamError):
    """The stream ended without a complete, successful protocol termination."""


_JSON_LIMITS = StructuredDataLimits(
    max_depth=32, max_tokens=8192, max_integer_digits=32
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StreamError("stream JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise StreamError("stream JSON contains a non-finite number")


def _token_count(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 9_007_199_254_740_991
    ):
        raise StreamError("stream usage contains an invalid token count")
    return value


class StreamParser:
    """Parse bounded SSE frames without retaining their contents.

    Byte limits include comments, metadata, and delimiters. Total frame count is
    limited to ``4 * max_content_events + 64``, providing bounded headroom for
    non-content frames. Usage is optional and may be reported once, with or after
    the generation finish event. Only choice zero and stop/length finishes are
    supported. Success requires nonempty content, a finish reason, and [DONE].

    ``observed_ns`` is a nonnegative monotonic timestamp, with ties permitted for
    frames received together. ``first_body_byte_ns`` includes metadata/comments;
    ``first_content_ns`` excludes empty and metadata-only content events.
    """

    def __init__(
        self,
        *,
        max_stream_bytes: int,
        max_event_bytes: int,
        max_content_events: int,
    ) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (max_stream_bytes, max_event_bytes, max_content_events)
        ):
            raise ValueError("stream limits must be positive integers")
        self.first_body_byte_ns: int | None = None
        self.first_content_ns: int | None = None
        self.content_event_times_ns: list[int] = []
        self.finish_reason: str | None = None
        self.completion_tokens: int | None = None
        self.prompt_tokens: int | None = None
        self.done = False
        self._max_stream_bytes = max_stream_bytes
        self._max_event_bytes = max_event_bytes
        self._max_content_events = max_content_events
        self._max_events = 4 * max_content_events + 64
        self._stream_bytes = 0
        self._events = 0
        self._pending = bytearray()
        self._line_start = 0
        self._last_observed_ns: int | None = None
        self._failed = False
        self._closed = False

    def feed(self, data: bytes, observed_ns: int) -> None:
        """Consume body bytes, timestamping frames when their delimiter arrives."""
        try:
            self._feed(data, observed_ns)
        except StreamError:
            self._fail()
            raise

    def _fail(self) -> None:
        self._failed = True
        self._pending.clear()
        self._line_start = 0

    def _feed(self, data: bytes, observed_ns: int) -> None:
        if self._failed or self._closed:
            raise StreamError("stream parser is already closed")
        if not isinstance(data, bytes):
            raise StreamError("stream body must be bytes")
        if (
            isinstance(observed_ns, bool)
            or not isinstance(observed_ns, int)
            or observed_ns < 0
            or (
                self._last_observed_ns is not None
                and observed_ns < self._last_observed_ns
            )
        ):
            raise StreamError("stream timestamp must be monotonic nanoseconds")
        self._last_observed_ns = observed_ns
        if data and self.first_body_byte_ns is None:
            self.first_body_byte_ns = observed_ns
        self._stream_bytes += len(data)
        if self._stream_bytes > self._max_stream_bytes:
            raise StreamLimitError("stream byte limit exceeded")
        offset = 0
        while offset < len(data):
            newline = data.find(b"\n", offset)
            end = len(data) if newline < 0 else newline + 1
            if len(self._pending) + end - offset > self._max_event_bytes:
                raise StreamLimitError("stream event byte limit exceeded")
            self._pending.extend(data[offset:end])
            offset = end
            if newline < 0:
                break
            line = bytes(self._pending[self._line_start :])
            if line in (b"\n", b"\r\n"):
                self._events += 1
                if self._events > self._max_events:
                    raise StreamLimitError("stream event count limit exceeded")
                frame = bytes(self._pending)
                self._pending.clear()
                self._line_start = 0
                self._event(frame, observed_ns)
            else:
                self._line_start = len(self._pending)

    def _event(self, frame: bytes, observed_ns: int) -> None:
        try:
            text = frame.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise StreamError("stream event is not valid UTF-8") from None
        data_lines: list[str] = []
        event_name = "message"
        for raw_line in text.split("\n"):
            line = raw_line.removesuffix("\r")
            if "\r" in line or "\x00" in line:
                raise StreamError("stream event contains an invalid SSE line")
            if not line or line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator:
                value = value.removeprefix(" ")
            if field == "data":
                data_lines.append(value)
            elif field == "event":
                event_name = value
            elif field == "retry":
                if not value.isascii() or not value.isdecimal():
                    raise StreamError("stream event contains invalid SSE retry")
            elif field != "id":
                raise StreamError("stream event contains an unsupported SSE field")
        if event_name == "error":
            raise StreamError("server reported an SSE error")
        if not data_lines:
            return
        if self.done:
            raise StreamError("stream contains payload after DONE")
        payload = "\n".join(data_lines)
        if payload == "[DONE]":
            if self.finish_reason is None or self.first_content_ns is None:
                raise IncompleteStream("stream terminated without completed content")
            self.done = True
            return
        try:
            validate_json_structure(payload, limits=_JSON_LIMITS)
            decoded: object = json.loads(
                payload,
                object_pairs_hook=_unique_object,
                parse_int=partial(bounded_json_int, limits=_JSON_LIMITS),
                parse_float=partial(bounded_json_float, limits=_JSON_LIMITS),
                parse_constant=_reject_constant,
            )
        except BoundedParseError:
            raise StreamLimitError("stream JSON exceeds structural limits") from None
        except (json.JSONDecodeError, RecursionError):
            raise StreamError("stream event contains invalid JSON") from None
        if not isinstance(decoded, dict):
            raise StreamError("stream JSON must be an object")
        if "error" in decoded:
            raise StreamError("server reported a stream error")
        choices = decoded.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise StreamError("stream must contain at most one choice")
        if choices:
            self._choice(choices[0], observed_ns)
        usage = decoded.get("usage")
        if usage is not None:
            self._usage(usage)

    def _choice(self, value: object, observed_ns: int) -> None:
        if not isinstance(value, dict):
            raise StreamError("stream choice must be an object")
        index = value.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index != 0:
            raise StreamError("stream choice index must be zero")
        delta = value.get("delta")
        if not isinstance(delta, dict):
            raise StreamError("stream choice delta must be an object")
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise StreamError("stream content must be text or null")
        if isinstance(content, str):
            try:
                content.encode("utf-8")
            except UnicodeEncodeError:
                raise StreamError("stream content is not valid Unicode") from None
        reason = value.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str) or reason not in {"stop", "length"}:
                raise StreamError("stream finish reason is unsupported")
            if self.finish_reason is not None:
                raise StreamError("stream repeats a generation finish")
        if content:
            if self.finish_reason is not None:
                raise StreamError("stream contains content after generation finish")
            try:
                content.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise StreamError("stream content is not valid Unicode") from None
            if len(self.content_event_times_ns) >= self._max_content_events:
                raise StreamLimitError("stream content event limit exceeded")
            if self.first_content_ns is None:
                self.first_content_ns = observed_ns
            self.content_event_times_ns.append(observed_ns)
        if reason is not None:
            self.finish_reason = reason

    def _usage(self, value: object) -> None:
        if not isinstance(value, dict):
            raise StreamError("stream usage must be an object or null")
        if self.completion_tokens is not None:
            raise StreamError("stream repeats token usage")
        if self.finish_reason is None:
            raise StreamError("stream usage precedes generation finish")
        prompt = _token_count(value.get("prompt_tokens"))
        completion = _token_count(value.get("completion_tokens"))
        total = _token_count(value.get("total_tokens"))
        if prompt + completion != total:
            raise StreamError("stream token usage is inconsistent")
        if completion == 0 and self.first_content_ns is not None:
            raise StreamError("stream token usage contradicts generated content")
        self.prompt_tokens = prompt
        self.completion_tokens = completion

    def finish(self) -> None:
        """Validate EOF after complete frames and the required terminal marker."""
        if self._failed:
            raise StreamError("stream parser is already closed")
        if self._pending or not self.done:
            self._fail()
            raise IncompleteStream("stream ended before complete protocol termination")
        self._closed = True
