"""Adversarial tests for bounded streaming content-event measurements."""

import json

import pytest

from inferdrome.evaluation.stream import (
    IncompleteStream,
    StreamError,
    StreamLimitError,
    StreamParser,
)


def parser(**overrides: int) -> StreamParser:
    return StreamParser(
        **{
            "max_stream_bytes": 100_000,
            "max_event_bytes": 10_000,
            "max_content_events": 100,
            **overrides,
        }
    )


def frame(value: object) -> bytes:
    return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\n\n"


def choice(
    content: object = None, *, finish: object = None, index: object = 0
) -> bytes:
    return frame(
        {
            "choices": [
                {
                    "index": index,
                    "delta": {"content": content},
                    "finish_reason": finish,
                }
            ],
            "usage": None,
        }
    )


def usage(prompt: object = 2, completion: object = 3, total: object = 5) -> bytes:
    return frame(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
            },
        }
    )


def finish(stream: StreamParser, timestamp: int = 99) -> None:
    stream.feed(choice(finish="stop") + b"data: [DONE]\n\n", timestamp)
    stream.finish()


def test_escaped_surrogate_content_is_rejected() -> None:
    stream = parser()
    with pytest.raises(StreamError, match="not valid Unicode"):
        stream.feed(
            b'data: {"choices":[{"index":0,"delta":{"content":"\\ud800"}}]}\n\n',
            1,
        )


def test_body_content_and_completion_are_distinct_and_usage_optional() -> None:
    stream = parser()
    stream.feed(b"", 0)
    assert stream.first_body_byte_ns is None
    stream.feed(b": connected\n\n", 1)
    stream.feed(choice(""), 2)
    stream.feed(choice(None), 3)
    stream.feed(choice("many tokens in one frame"), 7)
    stream.feed(choice("next") + choice("third"), 9)
    finish(stream)
    assert stream.first_body_byte_ns == 1
    assert stream.first_content_ns == 7
    assert stream.content_event_times_ns == [7, 9, 9]
    assert stream.finish_reason == "stop"
    assert stream.done
    assert stream.prompt_tokens is stream.completion_tokens is None


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_every_possible_split_including_unicode_and_delimiters(
    line_ending: bytes,
) -> None:
    payload = choice("h\u00e9llo \U0001f642", finish="length") + b"data: [DONE]\n\n"
    payload = payload.replace(b"\n", line_ending)
    content_end = payload.index(line_ending + line_ending) + 2 * len(line_ending)
    for split in range(len(payload) + 1):
        stream = parser()
        stream.feed(payload[:split], 10)
        stream.feed(payload[split:], 20)
        stream.finish()
        assert stream.first_content_ns == (10 if split >= content_end else 20)
        assert stream.content_event_times_ns == [stream.first_content_ns]
        assert stream.finish_reason == "length"


def test_one_byte_feeds_and_multiline_json() -> None:
    stream = parser()
    payload = (
        b"id: opaque\nretry: 123\nevent: message\n: comment\n"
        b'data: {"choices": [{"index": 0,\n'
        b'data: "delta": {"content": "hello"}, "finish_reason": "stop"}]}\n\n'
    )
    for timestamp, byte in enumerate(payload):
        stream.feed(bytes([byte]), timestamp)
    assert stream.first_content_ns == len(payload) - 1
    stream.feed(usage() + b"data: [DONE]\n\n: trailing comment\n\n", len(payload))
    stream.finish()
    assert stream.prompt_tokens == 2
    assert stream.completion_tokens == 3


@pytest.mark.parametrize(
    "payload",
    [
        b"data: not-json\n\n",
        b'data: {"choices": [], "choices": []}\n\n',
        b'data: {"choices": [], "metadata": {"key": 1, "key": 2}}\n\n',
        b'data: {"choices": [], "metadata": NaN}\n\n',
        b'data: {"choices": [], "metadata": Infinity}\n\n',
        b'data: {"choices": [], "metadata": 1e999}\n\n',
        b"data: []\n\n",
        b"data: \xff\n\n",
        b"data: {}\rbroken\n\n",
        b"unrecognized: content\n\n",
        b"retry: never\n\n",
        b"id: invalid\x00identifier\n\n",
        b"data:\n\n",
        frame({"choices": None}),
        frame({"choices": [None]}),
        frame({"choices": [{"index": 0, "delta": None}]}),
        frame({"choices": [{"delta": {}}]}),
        frame({"choices": [{"index": 0, "delta": {}}, {}]}),
        choice(index=True),
        choice(index=1),
        choice(index=0.0),
        choice(content=1),
        choice(content=[]),
        choice(finish="tool_calls"),
        choice(finish=True),
        b'data: {"choices": [{"index": 0, "delta": {"content": "\\ud800"}}]}\n\n',
    ],
)
def test_malformed_streams_are_rejected(payload: bytes) -> None:
    with pytest.raises(StreamError):
        parser().feed(payload, 1)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"data: ",
        choice("text"),
        choice("text", finish="stop"),
        choice("text", finish="stop") + b"data: [DONE]\n",
        choice("text", finish="stop") + b"data: [DONE]\n\n\r",
    ],
)
def test_truncated_or_unfinished_stream_is_incomplete(payload: bytes) -> None:
    stream = parser()
    stream.feed(payload, 1)
    with pytest.raises(IncompleteStream):
        stream.finish()
    assert stream._pending == b""


@pytest.mark.parametrize(
    "payload",
    [
        b"data: [DONE]\n\n",
        choice("text") + b"data: [DONE]\n\n",
        choice(finish="stop") + b"data: [DONE]\n\n",
    ],
)
def test_done_requires_both_generated_content_and_finish(payload: bytes) -> None:
    with pytest.raises(IncompleteStream):
        parser().feed(payload, 1)


@pytest.mark.parametrize(
    "extra",
    [choice("late"), choice(finish="stop")],
)
def test_content_or_repeated_finish_after_finish_is_rejected(extra: bytes) -> None:
    stream = parser()
    stream.feed(choice("text", finish="stop"), 1)
    with pytest.raises(StreamError):
        stream.feed(extra, 2)


@pytest.mark.parametrize("extra", [choice(), b"data: [DONE]\n\n"])
def test_payload_after_done_is_rejected(extra: bytes) -> None:
    stream = parser()
    stream.feed(choice("text", finish="stop") + b"data: [DONE]\n\n", 1)
    with pytest.raises(StreamError, match="payload after DONE"):
        stream.feed(extra, 2)


@pytest.mark.parametrize(
    "payload",
    [
        b"event: error\ndata: private server error SECRET\n\n",
        frame({"error": {"message": "private server error SECRET"}}),
    ],
)
def test_server_error_is_sanitized_and_parser_cannot_resume(payload: bytes) -> None:
    stream = parser()
    with pytest.raises(StreamError) as error:
        stream.feed(payload, 1)
    assert "SECRET" not in str(error.value)
    assert "private" not in str(error.value)
    assert stream._pending == b""
    with pytest.raises(StreamError, match="already closed"):
        stream.feed(choice("text"), 2)
    with pytest.raises(StreamError, match="already closed"):
        stream.finish()


@pytest.mark.parametrize("invalid", [-1, True, 1.5, None, "3", 2**53, 10**25])
@pytest.mark.parametrize("field", ["prompt", "completion", "total"])
def test_usage_counts_are_strict_nonnegative_integers(
    invalid: object, field: str
) -> None:
    stream = parser()
    stream.feed(choice("text", finish="stop"), 1)
    values: dict[str, object] = {"prompt": 2, "completion": 3, "total": 5}
    values[field] = invalid
    with pytest.raises(StreamError, match="invalid token count"):
        stream.feed(usage(**values), 2)


@pytest.mark.parametrize(
    "invalid_usage",
    [usage(total=6), usage(completion=0, total=2), frame({"choices": [], "usage": {}})],
)
def test_usage_contradictions_and_missing_counts_are_rejected(
    invalid_usage: bytes,
) -> None:
    stream = parser()
    stream.feed(choice("text", finish="stop"), 1)
    with pytest.raises(StreamError):
        stream.feed(invalid_usage, 2)


def test_usage_is_once_after_finish_and_null_does_not_count() -> None:
    stream = parser()
    stream.feed(choice("text", finish="stop"), 1)
    stream.feed(frame({"choices": [], "usage": None}) + usage(), 2)
    assert stream.prompt_tokens == 2
    assert stream.completion_tokens == 3
    with pytest.raises(StreamError, match="repeats token usage"):
        stream.feed(usage(), 3)
    with pytest.raises(StreamError, match="precedes generation finish"):
        parser().feed(usage(), 0)


@pytest.mark.parametrize("limit", ["max_stream_bytes", "max_event_bytes"])
def test_byte_limits_apply_before_buffering_and_include_metadata(limit: str) -> None:
    stream = parser(**{limit: 8})
    stream.feed(b": 12345", 1)
    with pytest.raises(StreamLimitError):
        stream.feed(b"6789\n\n", 2)
    assert stream._pending == b""


def test_content_event_limit_and_bounded_metadata_event_count() -> None:
    stream = parser(max_content_events=1)
    stream.feed(choice("one"), 1)
    with pytest.raises(StreamLimitError, match="content event limit"):
        stream.feed(choice("two"), 2)
    assert stream.content_event_times_ns == [1]
    stream = parser(max_content_events=1)
    stream.feed(b": ping\n\n" * 68, 1)
    with pytest.raises(StreamLimitError, match="event count limit"):
        stream.feed(b"\n", 2)


@pytest.mark.parametrize(
    "metadata",
    [b"[" * 33 + b"0" + b"]" * 33, b"1" * 33, b"[" + b"0," * 8192 + b"0]"],
)
def test_json_is_structurally_bounded(metadata: bytes) -> None:
    stream = parser(max_event_bytes=50_000)
    with pytest.raises(StreamLimitError):
        stream.feed(b'data: {"choices": [], "metadata": ' + metadata + b"}\n\n", 1)


@pytest.mark.parametrize("invalid", [-1, True, 1.2])
def test_timestamps_must_be_nonnegative_integers(invalid: int) -> None:
    with pytest.raises(StreamError, match="monotonic nanoseconds"):
        parser().feed(b"", invalid)


def test_timestamps_cannot_go_backwards() -> None:
    stream = parser()
    stream.feed(b": comment\n\n", 10)
    with pytest.raises(StreamError, match="monotonic nanoseconds"):
        stream.feed(choice("hello"), 9)


@pytest.mark.parametrize("invalid", [0, -1, True, 1.2])
@pytest.mark.parametrize(
    "limit", ["max_stream_bytes", "max_event_bytes", "max_content_events"]
)
def test_limits_must_be_positive_integers(limit: str, invalid: int) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        parser(**{limit: invalid})


def test_parser_retains_no_generated_text() -> None:
    stream = parser()
    stream.feed(choice("SENSITIVE GENERATED TEXT"), 1)
    finish(stream)
    assert "SENSITIVE" not in repr(vars(stream))
    assert stream._pending == b""
    stream.finish()
    with pytest.raises(StreamError, match="already closed"):
        stream.feed(b"", 100)
