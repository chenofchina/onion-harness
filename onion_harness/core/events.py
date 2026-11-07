"""Structured event stream - the only way the GUI learns about the runtime.

Events are plain data.  The harness emits them, the API layer serialises them,
the GUI renders them.  No event ever carries a live object, so the stream is
safe to persist and replay.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict

from onion_harness.core.primitives import Layer, new_id


class EventType(str, Enum):
    # Task lifecycle
    TASK_CREATED = "task-created"
    TASK_STATUS = "task-status"
    TASK_DONE = "task-done"

    # Conversation cards
    USER_INPUT = "user-input"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    TOOL_CALL = "tool-call"
    TOOL_RESULT = "tool-result"
    TOOL_OUTPUT = "tool-output"
    TOOL_APPROVAL = "tool-approval"

    # Sidecar preview
    PREVIEW_OPEN = "preview-open"
    SERVER_DETECTED = "server-detected"

    # Roundtable
    ROUNDTABLE_START = "roundtable-start"
    ROUNDTABLE_ROUND = "roundtable-round"
    ROUNDTABLE_TURN = "roundtable-turn"
    ROUNDTABLE_SPEECH = "roundtable-speech"
    ROUNDTABLE_REBUTTAL = "roundtable-rebuttal"
    ROUNDTABLE_CONSENSUS = "roundtable-consensus"
    ROUNDTABLE_BREAKER = "roundtable-breaker"
    ROUNDTABLE_END = "roundtable-end"
    # Live interaction: a human speaks into the hall, or a seat joins mid-debate.
    ROUNDTABLE_MESSAGE = "roundtable-message"
    ROUNDTABLE_SEAT_ADDED = "roundtable-seat-added"
    ROUNDTABLE_NOTICE = "roundtable-notice"

    # Self-modification cards
    CHANGE_PROPOSAL = "change-proposal"
    CHANGE_REQUEST = "change-request"
    CHANGE_APPLIED = "change-applied"
    CHANGE_REJECTED = "change-rejected"
    CHANGE_REVERTED = "change-reverted"
    CHANGE_REDONE = "change-redone"

    # Telemetry
    TOKEN_USAGE = "token-usage"
    CONTEXT_USAGE = "context-usage"
    ERROR = "error"


@dataclass(frozen=True)
class OnionEvent:
    """A serialisable record in the task event stream."""

    type: EventType
    task_id: str
    seq: int = 0
    payload: Dict[str, Any] = field(default_factory=dict)
    source: Layer = Layer.HARNESS
    id: str = field(default_factory=lambda: new_id("evt"))
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "seq": self.seq,
            "type": self.type.value,
            "task_id": self.task_id,
            "source": self.source.value,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "OnionEvent":
        return cls(
            type=EventType(raw["type"]),
            task_id=raw.get("task_id", ""),
            seq=int(raw.get("seq", 0)),
            payload=raw.get("payload") or {},
            source=Layer(raw.get("source", "harness")),
            id=raw.get("id") or new_id("evt"),
            timestamp=float(raw.get("timestamp") or time.time()),
        )


def event(
    type: EventType,
    task_id: str,
    seq: int = 0,
    source: Layer = Layer.HARNESS,
    **payload: Any,
) -> OnionEvent:
    """Convenience constructor used across the harness."""
    return OnionEvent(type=type, task_id=task_id, seq=seq, payload=payload, source=source)


__all__ = ["EventType", "OnionEvent", "event"]