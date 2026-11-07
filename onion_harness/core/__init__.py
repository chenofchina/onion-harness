"""Layer 1 package - core primitives and the only public type vocabulary.

Nothing here imports another Onion layer or any third-party package.
"""

from __future__ import annotations

from onion_harness.core.events import EventType, OnionEvent, event
from onion_harness.core.primitives import (
    Context,
    Layer,
    Result,
    Task,
    TaskKind,
    new_id,
)
from onion_harness.core.tool import OnionTool

# Public aliases: the names the rest of the system (and the GUI) uses.
OnionTask = Task
OnionContext = Context
OnionResult = Result

__all__ = [
    "Layer",
    "TaskKind",
    "Task",
    "Context",
    "Result",
    "OnionTask",
    "OnionContext",
    "OnionResult",
    "OnionEvent",
    "OnionTool",
    "EventType",
    "event",
    "new_id",
]