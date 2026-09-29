"""Bounded, estimated document affinity for the synthetic routing pilot.

Recent routing history is only a hint. It cannot establish that vLLM still has
any KV block resident. No prompt or document text is retained here.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass

DOCUMENT_SEPARATOR = "\n\nQuestion:\n"
HISTORY_KEYS = 4096
HISTORY_TTL_NS = 60_000_000_000
ESCAPE_BUSY_DELTA = 2
SATURATION_RULE_VERSION = "active-request-gate.v1"


def _recent(value: int | None, now_ns: int) -> int:
    return int(value is not None and 0 <= now_ns - value <= HISTORY_TTL_NS)


def document_digest(payload: object) -> str | None:
    """Hash the fixed pilot's document prefix; other chat shapes have no hint."""
    if not isinstance(payload, dict):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        return None
    message = messages[0]
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    document, separator, suffix = content.partition(DOCUMENT_SEPARATOR)
    try:
        document_bytes = document.encode("utf-8")
    except UnicodeError:
        return None
    if not separator or not suffix or len(document_bytes) < 64:
        return None
    return hashlib.sha256(document_bytes).hexdigest()


@dataclass(frozen=True)
class AffinityDecision:
    replica: int
    reason: str
    estimated_affinity: tuple[int, int]
    busy_at_decision: tuple[int, int]
    preferred_replica: int | None = None
    saturation_active: int | None = None
    saturation_reached: bool | None = None
    relative_imbalance_reached: bool | None = None


class AffinityHistory:
    """One timestamp per replica and document, with bounded LRU eviction."""

    def __init__(self) -> None:
        self._entries: OrderedDict[str, tuple[int | None, int | None]] = OrderedDict()

    def scores(self, digest: str | None, now_ns: int) -> tuple[int, int]:
        if digest is None or digest not in self._entries:
            return (0, 0)
        timestamps = self._entries[digest]
        self._entries.move_to_end(digest)
        return (
            _recent(timestamps[0], now_ns),
            _recent(timestamps[1], now_ns),
        )

    def record(self, digest: str | None, replica: int, now_ns: int) -> None:
        if digest is None:
            return
        previous = self._entries.pop(digest, (None, None))
        updated = list(previous)
        updated[replica] = now_ns
        self._entries[digest] = (updated[0], updated[1])
        if len(self._entries) > HISTORY_KEYS:
            self._entries.popitem(last=False)

    @property
    def size(self) -> int:
        return len(self._entries)


def choose(
    policy: str,
    busy: tuple[int, int],
    turn: int,
    affinity: tuple[int, int],
    *,
    saturation_active: int | None = None,
) -> AffinityDecision:
    """Keep legacy choices; gate the opt-in escape by absolute active work."""
    if policy == "cache_saturation" and (
        saturation_active is None or not 1 <= saturation_active <= 512
    ):
        raise ValueError("cache_saturation needs a bounded active threshold")
    ring = turn % 2
    if policy == "round_robin":
        return AffinityDecision(ring, "round_robin", affinity, busy)
    if policy == "least_busy":
        selected = min((0, 1), key=lambda i: (busy[i], (i - ring) % 2))
        return AffinityDecision(selected, "least_busy", affinity, busy)
    preferred = (
        ring if affinity[0] == affinity[1] else (0 if affinity[0] > affinity[1] else 1)
    )
    imbalance = busy[preferred] >= busy[1 - preferred] + ESCAPE_BUSY_DELTA
    saturated = (
        busy[preferred] >= saturation_active if saturation_active is not None else None
    )
    if imbalance and (
        policy == "cache_plus_load" or (policy == "cache_saturation" and saturated)
    ):
        return AffinityDecision(
            1 - preferred,
            "overload_escape" if policy == "cache_plus_load" else "saturated_escape",
            affinity,
            busy,
            preferred,
            saturation_active,
            saturated,
            imbalance,
        )
    reason = "estimated_affinity" if affinity[0] != affinity[1] else "affinity_tie"
    return AffinityDecision(
        preferred,
        reason,
        affinity,
        busy,
        preferred,
        saturation_active,
        saturated,
        imbalance,
    )
