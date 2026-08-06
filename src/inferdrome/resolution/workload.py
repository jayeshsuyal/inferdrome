"""Strict custom-JSONL workload parsing."""

import json
from typing import Any

from inferdrome.errors import SourceInputError

MAX_WORKLOAD_LINE_BYTES = 1_048_576
MAX_PROMPT_CHARACTERS = 1_048_000


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceInputError("workload JSON object keys must be unique")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise SourceInputError("workload JSON cannot contain non-finite numbers")


def parse_custom_workload(content: bytes) -> tuple[str, ...]:
    """Parse vLLM custom JSONL as exact one-key prompt objects."""

    prompts: list[str] = []
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        if not raw_line.strip():
            raise SourceInputError(f"workload line {line_number} is blank")
        if len(raw_line) > MAX_WORKLOAD_LINE_BYTES:
            raise SourceInputError(f"workload line {line_number} exceeds its limit")
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            raise SourceInputError(
                f"workload line {line_number} is not valid UTF-8"
            ) from None
        try:
            value = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonfinite,
            )
        except SourceInputError:
            raise
        except (json.JSONDecodeError, TypeError):
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
        if not 1 <= len(prompt) <= MAX_PROMPT_CHARACTERS:
            raise SourceInputError(
                f"workload line {line_number} prompt length is outside limits"
            )
        prompts.append(prompt)

    if not prompts:
        raise SourceInputError("workload must contain at least one prompt")
    return tuple(prompts)
