"""Onion Harness - a layered local agent runtime."""

from __future__ import annotations

__version__ = "0.1.0"

from onion_harness.core import (  # noqa: E402
    Context,
    EventType,
    Layer,
    OnionContext,
    OnionEvent,
    OnionResult,
    OnionTask,
    OnionTool,
    Result,
    Task,
    TaskKind,
)

__all__ = [
    "__version__",
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
]


def __getattr__(name: str):  # pragma: no cover - convenience only
    if name == "OnionRuntime":
        from onion_harness.harness.runtime import OnionRuntime

        return OnionRuntime
    raise AttributeError(name)