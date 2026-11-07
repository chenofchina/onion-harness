"""The tool contract shared by plugins, harness and the model.

:class:`OnionTool` is deliberately framework-agnostic.  The harness adapts it to
whatever tool protocol the underlying engine speaks; plugins never see that
protocol.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Union

ToolHandler = Callable[[Dict[str, Any]], Union[Any, Awaitable[Any]]]

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,63}$")


@dataclass(frozen=True)
class OnionTool:
    """A callable capability exposed to the model.

    ``handler`` receives the model-supplied arguments as a dict and returns
    either a string or a JSON-serialisable object.  ``metadata`` is opaque to
    the engine; the harness uses it to tag executor tools (``kind: executor``)
    so the GUI can render the right icon.
    """

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: ToolHandler
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"Invalid tool name: {self.name!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "metadata": dict(self.metadata),
        }


__all__ = ["OnionTool", "ToolHandler"]