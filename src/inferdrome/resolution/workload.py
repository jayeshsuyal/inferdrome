"""Strict custom-JSONL workload parsing."""

import json
from dataclasses import dataclass
from typing import Any

from inferdrome.errors import SourceInputError
from inferdrome.parsing import (
    BoundedParseError,
    bounded_json_float,
    bounded_json_int,
    validate_json_structure,
)

MAX_WORKLOAD_LINE_BYTES = 1_048_576
MAX_PROMPT_CHARACTERS = 1_048_000
MAX_WORKLOAD_RECORDS = 1_000_000


@dataclass(frozen=True)
class WorkloadLimits:
    """Ceilings for one digest-bound custom JSONL workload."""

    max_records: int = MAX_WORKLOAD_RECORDS
    max_line_bytes: int = MAX_WORKLOAD_LINE_BYTES
    max_prompt_characters: int = MAX_PROMPT_CHARACTERS

    def __post_init__(self) -> None:
        values = (
            self.max_records,
            self.max_line_bytes,
            self.max_prompt_characters,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError("workload limits must be positive integers")


DEFAULT_WORKLOAD_LIMITS = WorkloadLimits()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceInputError("workload JSON object keys must be unique")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise SourceInputError("workload JSON cannot contain non-finite numbers")


def parse_custom_workload(
    content: bytes,
    *,
    required_prompt_count: int | None = None,
    limits: WorkloadLimits = DEFAULT_WORKLOAD_LIMITS,
) -> tuple[str, ...]:
    """Count the bounded JSONL population, then parse only the consumed prefix."""

    if required_prompt_count is not None and (
        isinstance(required_prompt_count, bool)
        or not isinstance(required_prompt_count, int)
        or required_prompt_count <= 0
        or required_prompt_count > limits.max_records
    ):
        raise SourceInputError("required workload prompt count exceeds its limit")

    # First pass: bound every record and stop at N+1 without splitlines() or JSON
    # construction. The unused suffix stays byte/digest-bound but is not parsed or
    # retained by the resolver or adapter.
    record_count = 0
    start = 0
    while start < len(content):
        newline = content.find(b"\n", start)
        end = len(content) if newline < 0 else newline
        line_end = end - 1 if end > start and content[end - 1] == 13 else end
        record_count += 1
        if record_count > limits.max_records:
            raise SourceInputError("workload record count exceeds its limit")
        line_size = line_end - start
        if line_size > limits.max_line_bytes:
            raise SourceInputError(
                f"workload line {record_count} exceeds its limit"
            )
        if line_size <= 0 or not content[start:line_end].strip():
            raise SourceInputError(f"workload line {record_count} is blank")
        if newline < 0:
            break
        start = newline + 1

    if record_count == 0:
        raise SourceInputError("workload must contain at least one prompt")

    prompts: list[str] = []
    retained_count = (
        record_count if required_prompt_count is None else min(
            record_count,
            required_prompt_count,
        )
    )
    line_start = 0
    for line_number in range(1, retained_count + 1):
        newline = content.find(b"\n", line_start)
        line_end = len(content) if newline < 0 else newline
        if line_end > line_start and content[line_end - 1] == 13:
            line_end -= 1
        raw_line = content[line_start:line_end]
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            raise SourceInputError(
                f"workload line {line_number} is not valid UTF-8"
            ) from None
        try:
            validate_json_structure(line)
            value = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonfinite,
                parse_float=bounded_json_float,
                parse_int=bounded_json_int,
            )
        except SourceInputError:
            raise
        except (
            BoundedParseError,
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            raise SourceInputError(
                f"workload line {line_number} is not valid JSON"
            ) from None
        if not isinstance(value, dict) or set(value) != {"prompt"}:
            raise SourceInputError(
                f"workload line {line_number} must contain only prompt"
            )
        prompt = value["prompt"]
        if not isinstance(prompt, str):
            raise SourceInputError(f"workload line {line_number} prompt must be text")
        if not 1 <= len(prompt) <= limits.max_prompt_characters:
            raise SourceInputError(
                f"workload line {line_number} prompt length is outside limits"
            )
        prompts.append(prompt)
        line_start = len(content) if newline < 0 else newline + 1

    return tuple(prompts)
