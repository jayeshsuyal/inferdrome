"""Lexical ceilings applied before recursive JSON or YAML construction."""

import json
import re
from dataclasses import dataclass
from math import isfinite
from typing import Final

import yaml
from yaml.tokens import (
    AliasToken,
    BlockEndToken,
    BlockMappingStartToken,
    BlockSequenceStartToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
    ScalarToken,
)


class BoundedParseError(ValueError):
    """Structured input crossed a lexical limit before object construction."""


@dataclass(frozen=True)
class StructuredDataLimits:
    """Structural ceilings generous enough for all frozen v0.1 documents."""

    max_depth: int = 64
    max_tokens: int = 1_000_000
    max_integer_digits: int = 256

    def __post_init__(self) -> None:
        values = (self.max_depth, self.max_tokens, self.max_integer_digits)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError("structured-data limits must be positive integers")


DEFAULT_STRUCTURED_DATA_LIMITS: Final = StructuredDataLimits()
_YAML_INTEGER = re.compile(
    r"""
    [+-]?(?:
        0b[0-1_]+
        |0[0-7_]+
        |(?:0|[1-9][0-9_]*)
        |0x[0-9a-fA-F_]+
        |[1-9][0-9_]*(?::[0-5]?[0-9])+
    )\Z
    """,
    re.VERBOSE,
)
_JSON_NUMBER_CHARACTERS = frozenset("0123456789.eE+-")


def _raise_if_too_many_integer_digits(
    token: str,
    *,
    limits: StructuredDataLimits,
) -> None:
    unsigned = token.removeprefix("-").removeprefix("+")
    integer_part = unsigned.split(".", 1)[0].split("e", 1)[0].split("E", 1)[0]
    if sum(character.isdigit() for character in integer_part) > (
        limits.max_integer_digits
    ):
        raise BoundedParseError("integer exceeds its digit limit")


def _raise_if_too_many_yaml_integer_digits(
    token: str,
    *,
    limits: StructuredDataLimits,
) -> None:
    unsigned = token.removeprefix("-").removeprefix("+")
    if unsigned.startswith(("0b", "0x")):
        unsigned = unsigned[2:]
    digit_count = sum(character.isalnum() for character in unsigned)
    if digit_count > limits.max_integer_digits:
        raise BoundedParseError("integer exceeds its digit limit")


def bounded_json_int(
    token: str,
    *,
    limits: StructuredDataLimits = DEFAULT_STRUCTURED_DATA_LIMITS,
) -> int:
    """Convert a pre-bounded JSON integer without reaching Python's digit guard."""

    _raise_if_too_many_integer_digits(token, limits=limits)
    try:
        return int(token)
    except ValueError:
        raise BoundedParseError("JSON integer is invalid") from None


def bounded_json_float(
    token: str,
    *,
    limits: StructuredDataLimits = DEFAULT_STRUCTURED_DATA_LIMITS,
) -> float:
    """Convert a bounded finite JSON float without silent overflow to infinity."""

    mantissa = token.split("e", 1)[0].split("E", 1)[0]
    if sum(character.isdigit() for character in mantissa) > (
        limits.max_integer_digits
    ):
        raise BoundedParseError("number exceeds its digit limit")
    try:
        value = float(token)
    except ValueError:
        raise BoundedParseError("JSON number is invalid") from None
    if not isfinite(value):
        raise BoundedParseError("JSON number is not finite")
    return value


def validate_json_structure(
    text: str,
    *,
    limits: StructuredDataLimits = DEFAULT_STRUCTURED_DATA_LIMITS,
) -> None:
    """Bound JSON nesting, significant tokens, and integer digits lexically."""

    depth = 0
    tokens = 0
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character.isspace():
            index += 1
            continue
        tokens += 1
        if tokens > limits.max_tokens:
            raise BoundedParseError("JSON token count exceeds its limit")
        if character == '"':
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == '"':
                    index += 1
                    break
                index += 1
            continue
        if character in "[{":
            depth += 1
            if depth > limits.max_depth:
                raise BoundedParseError("JSON structural depth exceeds its limit")
            index += 1
            continue
        if character in "]}":
            depth = max(0, depth - 1)
            index += 1
            continue
        if character == "-" or character.isdigit():
            end = index + 1
            while end < length and text[end] in _JSON_NUMBER_CHARACTERS:
                end += 1
            _raise_if_too_many_integer_digits(text[index:end], limits=limits)
            index = end
            continue
        if character.isalpha():
            index += 1
            while index < length and text[index].isalpha():
                index += 1
            continue
        index += 1


def _json_string_end(text: str, start: int) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index + 1
        index += 1
    return len(text)


def _validate_array_item_limit(
    text: str,
    start: int,
    *,
    key: str,
    max_items: int,
) -> None:
    depth = 1
    count = 0
    expecting_item = True
    index = start + 1
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if character == '"':
            if depth == 1 and expecting_item:
                count += 1
                expecting_item = False
            if count > max_items:
                raise BoundedParseError(f"JSON {key} array exceeds its item limit")
            index = _json_string_end(text, index)
            continue
        if character in "[{":
            if depth == 1 and expecting_item:
                count += 1
                expecting_item = False
                if count > max_items:
                    raise BoundedParseError(
                        f"JSON {key} array exceeds its item limit"
                    )
            depth += 1
            index += 1
            continue
        if character in "]}":
            depth -= 1
            if depth == 0:
                return
            index += 1
            continue
        if character == "," and depth == 1:
            expecting_item = True
            index += 1
            continue
        if depth == 1 and expecting_item:
            count += 1
            expecting_item = False
            if count > max_items:
                raise BoundedParseError(f"JSON {key} array exceeds its item limit")
        index += 1


def validate_top_level_json_array_limit(
    text: str,
    *,
    key: str,
    max_items: int,
    limits: StructuredDataLimits = DEFAULT_STRUCTURED_DATA_LIMITS,
) -> None:
    """Reject an oversized direct-child array before JSON object construction."""

    if not key or isinstance(max_items, bool) or not isinstance(max_items, int):
        raise ValueError("JSON array limit requires a key and integer ceiling")
    if max_items < 0:
        raise ValueError("JSON array item limit must be non-negative")
    validate_json_structure(text, limits=limits)
    depth = 0
    previous = ""
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if character == '"':
            end = _json_string_end(text, index)
            if depth == 1 and previous in {"{", ","}:
                try:
                    decoded = json.loads(text[index:end])
                except (RecursionError, ValueError, json.JSONDecodeError):
                    decoded = None
                cursor = end
                while cursor < len(text) and text[cursor].isspace():
                    cursor += 1
                if decoded == key and cursor < len(text) and text[cursor] == ":":
                    cursor += 1
                    while cursor < len(text) and text[cursor].isspace():
                        cursor += 1
                    if cursor < len(text) and text[cursor] == "[":
                        _validate_array_item_limit(
                            text,
                            cursor,
                            key=key,
                            max_items=max_items,
                        )
            previous = "value"
            index = end
            continue
        if character in "[{":
            depth += 1
        elif character in "]}":
            depth = max(0, depth - 1)
        previous = character
        index += 1


def validate_yaml_structure(
    text: str,
    *,
    limits: StructuredDataLimits = DEFAULT_STRUCTURED_DATA_LIMITS,
) -> None:
    """Bound YAML scanner tokens, collection depth, aliases, and integers."""

    depth = 0
    token_count = 0
    starts = (
        BlockMappingStartToken,
        BlockSequenceStartToken,
        FlowMappingStartToken,
        FlowSequenceStartToken,
    )
    ends = (BlockEndToken, FlowMappingEndToken, FlowSequenceEndToken)
    try:
        for token in yaml.scan(text):
            token_count += 1
            if token_count > limits.max_tokens:
                raise BoundedParseError("YAML token count exceeds its limit")
            if isinstance(token, AliasToken):
                raise BoundedParseError("YAML aliases are not supported")
            if isinstance(token, starts):
                depth += 1
                if depth > limits.max_depth:
                    raise BoundedParseError("YAML structural depth exceeds its limit")
            elif isinstance(token, ends):
                depth = max(0, depth - 1)
            elif (
                isinstance(token, ScalarToken)
                and token.style is None
                and _YAML_INTEGER.fullmatch(token.value)
            ):
                _raise_if_too_many_yaml_integer_digits(
                    token.value,
                    limits=limits,
                )
    except BoundedParseError:
        raise
    except (RecursionError, ValueError, yaml.YAMLError):
        raise BoundedParseError("YAML scanner rejected bounded input") from None
