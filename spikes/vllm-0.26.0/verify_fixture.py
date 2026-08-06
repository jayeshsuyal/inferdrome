#!/usr/bin/env python3
"""Validate structural expectations of the vLLM 0.26.0 spike fixture."""

import argparse
from collections import Counter
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List


EXPECTED_PROMPTS = 4
EXPECTED_COMPLETED = 3
EXPECTED_FAILED = 1
EXPECTED_FAILURE_INDEX = 2

DETAIL_ARRAYS = (
    "input_lens",
    "output_lens",
    "start_times",
    "ttfts",
    "itls",
    "generated_texts",
    "errors",
)

INTENTIONALLY_ABSENT = (
    "request_ids",
    "http_statuses",
    "finish_reasons",
    "latencies",
    "scheduled_offsets",
    "warmup_outputs",
)


class FixtureError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FixtureError(message)


def finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureError(f"cannot read native result: {exc}") from exc
    require(isinstance(value, dict), "native result must be one JSON object")
    return value


def load_json_lines(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                value = json.loads(line)
                require(
                    isinstance(value, dict),
                    f"mock trace line {line_number} must be an object",
                )
                records.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureError(f"cannot read mock trace: {exc}") from exc
    require(records, "mock trace must not be empty")
    return records


def validate_mock_trace(path: Path) -> Dict[str, Any]:
    records = load_json_lines(path)
    event_counts = Counter(record.get("event") for record in records)
    require(event_counts["server_started"] == 1, "mock server must start once")
    require(event_counts["server_stopped"] == 1, "mock server must stop once")

    received = [record for record in records if record.get("event") == "request_received"]
    require(len(received) == 7, "expected one preflight, two warmups, and four measured requests")
    received_ids = [record.get("request_id") for record in received]
    require(received_ids.count(None) == 3, "expected three non-measured requests")
    expected_measured_ids = [f"inferdrome-spike-{index}" for index in range(4)]
    require(
        Counter(value for value in received_ids if value is not None)
        == Counter(expected_measured_ids),
        "unexpected measured request IDs in mock trace",
    )

    response_errors = [record for record in records if record.get("event") == "response_error"]
    require(len(response_errors) == 1, "expected exactly one mock HTTP error")
    require(
        response_errors[0].get("request_id") == "inferdrome-spike-2"
        and response_errors[0].get("http_status") == 503,
        "expected measured request 2 to receive HTTP 503",
    )

    expected_stream_kinds = [
        "role_only",
        "content_alpha",
        "content_beta",
        "finish_reason",
        "usage",
    ]
    stream_events = [
        record for record in records if record.get("event") == "stream_event_sent"
    ]
    done_events = [record for record in records if record.get("event") == "stream_done_sent"]

    for request_id in (None, "inferdrome-spike-0", "inferdrome-spike-1", "inferdrome-spike-3"):
        expected_repetitions = 3 if request_id is None else 1
        kinds = [
            record.get("kind")
            for record in stream_events
            if record.get("request_id") == request_id
        ]
        require(
            Counter(kinds) == Counter(expected_stream_kinds * expected_repetitions),
            f"unexpected stream events for request ID {request_id!r}",
        )
        require(
            sum(record.get("request_id") == request_id for record in done_events)
            == expected_repetitions,
            f"unexpected stream completion count for request ID {request_id!r}",
        )

    require(
        not any(
            record.get("request_id") == "inferdrome-spike-2"
            for record in stream_events + done_events
        ),
        "failed measured request must not emit stream events",
    )

    return {
        "mock_trace": str(path),
        "mock_trace_events": len(records),
        "preflight_and_warmup_requests": 3,
        "measured_request_ids": expected_measured_ids,
        "mock_http_failure": {"request_id": "inferdrome-spike-2", "status": 503},
    }


def validate(path: Path) -> Dict[str, Any]:
    result = load_json(path)

    require(result.get("backend") == "openai-chat", "unexpected backend")
    require(result.get("model_id") == "inferdrome/mock-model", "unexpected model_id")
    require(result.get("num_prompts") == EXPECTED_PROMPTS, "unexpected num_prompts")
    require(result.get("completed") == EXPECTED_COMPLETED, "unexpected completed count")
    require(result.get("failed") == EXPECTED_FAILED, "unexpected failed count")
    require(
        result.get("completed", 0) + result.get("failed", 0) == EXPECTED_PROMPTS,
        "completed plus failed must equal measured prompt count",
    )
    require(
        result.get("inferdrome_spike_id") == "vllm-0.26.0-client-capability",
        "missing spike metadata",
    )
    require(
        result.get("inferdrome_producer_version") == "0.26.0",
        "missing producer metadata",
    )

    arrays: Dict[str, List[Any]] = {}
    for field in DETAIL_ARRAYS:
        value = result.get(field)
        require(isinstance(value, list), f"{field} must be present as a list")
        require(len(value) == EXPECTED_PROMPTS, f"{field} must have four entries")
        arrays[field] = value

    for field in INTENTIONALLY_ABSENT:
        require(field not in result, f"unexpected newly serialized field: {field}")

    require(
        all(finite_number(value) and value > 0 for value in arrays["start_times"]),
        "invalid start_times",
    )
    require(
        all(finite_number(value) and value >= 0 for value in arrays["ttfts"]),
        "invalid ttfts",
    )
    require(
        all(isinstance(value, int) and value >= 0 for value in arrays["input_lens"]),
        "invalid input_lens",
    )
    require(
        all(isinstance(value, int) and value >= 0 for value in arrays["output_lens"]),
        "invalid output_lens",
    )
    require(all(isinstance(value, list) for value in arrays["itls"]), "invalid itls")
    require(
        all(
            finite_number(item) and item >= 0
            for values in arrays["itls"]
            for item in values
        ),
        "ITL values must be finite and non-negative",
    )
    require(
        all(isinstance(value, str) for value in arrays["generated_texts"]),
        "invalid generated_texts",
    )
    require(all(isinstance(value, str) for value in arrays["errors"]), "invalid errors")

    failed_indexes = [
        index for index, error in enumerate(arrays["errors"]) if error
    ]
    require(
        failed_indexes == [EXPECTED_FAILURE_INDEX],
        f"unexpected failed indexes: {failed_indexes}",
    )
    require(
        arrays["output_lens"][EXPECTED_FAILURE_INDEX] == 0,
        "failed output must have zero output length",
    )
    require(
        arrays["generated_texts"][EXPECTED_FAILURE_INDEX] == "",
        "failed output must have empty generated text",
    )

    for index in range(EXPECTED_PROMPTS):
        if index == EXPECTED_FAILURE_INDEX:
            continue
        require(
            arrays["generated_texts"][index] == "alpha beta",
            f"unexpected generated text at index {index}",
        )
        require(
            arrays["output_lens"][index] == 2,
            f"unexpected output length at index {index}",
        )
        require(
            arrays["ttfts"][index] > 0,
            f"successful request {index} must have positive TTFT",
        )
        require(
            len(arrays["itls"][index]) >= 3,
            f"successful request {index} must retain subsequent choices events",
        )

    return {
        "fixture": str(path),
        "num_prompts": EXPECTED_PROMPTS,
        "completed": EXPECTED_COMPLETED,
        "failed": EXPECTED_FAILED,
        "failure_index": EXPECTED_FAILURE_INDEX,
        "native_keys": sorted(result),
        "status": "valid",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("native_result", type=Path)
    parser.add_argument("--mock-trace", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = validate(args.native_result)
        if args.mock_trace is not None:
            summary.update(validate_mock_trace(args.mock_trace))
    except FixtureError as exc:
        print(f"fixture validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
