"""Bounded YAML loading with duplicate-key and alias rejection."""

from typing import Any

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode

from inferdrome.errors import SourceInputError


class _StrictSafeLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):  # type: ignore[no-untyped-call]
            raise SourceInputError("YAML aliases are not supported")
        return super().compose_node(parent, index)

    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError:
                raise SourceInputError("YAML mapping keys must be scalar") from None
            if duplicate:
                raise SourceInputError("YAML mapping keys must be unique")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def load_strict_yaml(content: bytes) -> dict[str, Any]:
    """Decode exactly one safe YAML mapping without exposing source values."""

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise SourceInputError("source experiment must be valid UTF-8") from None

    try:
        value = yaml.load(text, Loader=_StrictSafeLoader)
    except SourceInputError:
        raise
    except yaml.YAMLError:
        raise SourceInputError("source experiment is not valid strict YAML") from None
    if not isinstance(value, dict):
        raise SourceInputError("source experiment root must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise SourceInputError("source experiment keys must be strings")
    return value
