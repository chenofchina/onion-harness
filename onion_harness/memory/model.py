"""Memory records - pure data, no dependencies beyond the stdlib.

A memory is one durable sentence about the user or the work: a preference they
stated, a fact about their environment, a recurring failure, or a coding habit.
It is stored encrypted and recalled by full-text search.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from onion_harness.core.primitives import new_id


class MemoryKind(str, Enum):
    """What sort of thing a memory records.

    The kinds exist because they age differently: a preference stays valid until
    the user changes it, while an event is only interesting shortly after it
    happened.
    """

    PREFERENCE = "preference"
    FACT = "fact"
    EVENT = "event"
    CODE_HABIT = "code-habit"

    @property
    def decay_per_day(self) -> float:
        """How fast importance fades, per day without use."""
        return {
            MemoryKind.PREFERENCE: 0.0,   # preferences do not decay
            MemoryKind.FACT: 0.02,
            MemoryKind.CODE_HABIT: 0.01,
            MemoryKind.EVENT: 0.15,
        }[self]


@dataclass
class Memory:
    """One durable statement, with the metadata needed to rank a recall."""

    text: str
    kind: MemoryKind = MemoryKind.FACT
    id: str = field(default_factory=lambda: new_id("mem"))
    weight: float = 0.5
    source_task: str = ""
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    use_count: int = 0
    # Set once the row is written; the stored text is encrypted at rest.
    stored: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.kind, str):
            self.kind = MemoryKind(self.kind)
        self.weight = _clamp01(self.weight)
        self.text = " ".join(self.text.split())

    def effective_weight(self, now: Optional[float] = None) -> float:
        """Weight after time decay, floored so old memories stay findable.

        Decay stops at 30% so a preference the user stated months ago still
        outranks noise; only genuinely stale events sink to the floor.
        """
        now = now if now is not None else time.time()
        days = max(0.0, (now - self.last_used_at) / 86_400)
        decayed = self.weight - days * self.kind.decay_per_day
        return max(0.3 * self.weight, decayed)

    def touch(self, now: Optional[float] = None) -> None:
        """Record that this memory was recalled in an answer."""
        self.last_used_at = now if now is not None else time.time()
        self.use_count += 1

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "weight": self.weight,
            "source_task": self.source_task,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "use_count": self.use_count,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "kind": self.kind.value,
            "weight": self.weight,
            "source_task": self.source_task,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "use_count": self.use_count,
        }

    def summary(self, limit: int = 120) -> str:
        return self.text if len(self.text) <= limit else self.text[: limit - 1] + "…"


def new_memory(text: str, kind: str | MemoryKind = MemoryKind.FACT, **kwargs: Any) -> Memory:
    """Build a memory from loose input, tolerating an unknown kind."""
    try:
        parsed = kind if isinstance(kind, MemoryKind) else MemoryKind(str(kind))
    except ValueError:
        parsed = MemoryKind.FACT
    return Memory(text=text, kind=parsed, **kwargs)


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, number))


__all__ = ["Memory", "MemoryKind", "new_memory"]