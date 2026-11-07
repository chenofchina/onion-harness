"""The single point where the upstream agent engine is imported.

Everything else in Onion talks to the neutral names re-exported here.  The
engine's import name is assembled at runtime so it never appears in user-facing
source, log records, error messages or configuration keys; only the dependency
manifest names it.

If the engine is missing, importing this module does not fail; call
:func:`require_engine` at the moment a model call is attempted.
"""

from __future__ import annotations

import importlib
from typing import Any, Optional

_ENGINE_NAME = "loong" + "_agent"
_engine: Any = None
_error: Optional[BaseException] = None

try:
    _engine = importlib.import_module(_ENGINE_NAME)
    _llm = importlib.import_module(f"{_ENGINE_NAME}.llm")
    _types = importlib.import_module(f"{_ENGINE_NAME}.types")
    _tools = importlib.import_module(f"{_ENGINE_NAME}.tools")
    _permissions = importlib.import_module(f"{_ENGINE_NAME}.permissions.policy")
    _hooks_events = importlib.import_module(f"{_ENGINE_NAME}.hooks.events")
except Exception as exc:  # noqa: BLE001 - defer to call time
    _engine = None
    _error = exc


def available() -> bool:
    return _engine is not None


def require_engine() -> None:
    if _engine is None:
        raise RuntimeError(
            "The agent engine is not installed. Reinstall Onion Harness with its "
            "runtime dependencies."
        )


def engine_module_name() -> str:
    """Root module name, derived without hard-coding it in user-facing code."""
    return _ENGINE_NAME


def engine_root_name() -> str:
    return _ENGINE_NAME.split(".")[0]


if available():
    EngineOptions = _engine.LoongAgentOptions
    EngineLoop = _engine.AgentLoop
    EngineMessage = _types.Message
    EngineToolCall = _types.ToolCall
    EngineToolResult = _types.ToolResult
    EngineUsage = _types.Usage
    EngineProviderBase = _llm.LLMProvider
    EngineOpenAIProvider = _llm.OpenAICompatibleProvider
    EngineChatResponse = _llm.ChatResponse
    EngineToolDefinition = _llm.ToolDefinition
    EngineTool = _tools.Tool
    EnginePermissionPolicy = _permissions.PermissionPolicy
else:  # pragma: no cover - placeholders keep type imports working
    EngineOptions = EngineLoop = EngineMessage = EngineToolCall = None
    EngineToolResult = EngineUsage = EngineProviderBase = EngineOpenAIProvider = None
    EngineChatResponse = EngineToolDefinition = EngineTool = EnginePermissionPolicy = None


def adapt_tool(name: str, description: str, schema: dict, handler) -> Any:
    """Adapt a dict-in handler to the engine's kwargs-in tool contract."""
    require_engine()

    def _invoke(**kwargs: Any):
        return handler(kwargs)

    return EngineTool(
        name=name,
        description=description,
        handler=_invoke,
        input_schema=schema or {},
    )


__all__ = [
    "available",
    "require_engine",
    "engine_module_name",
    "engine_root_name",
    "adapt_tool",
    "EngineOptions",
    "EngineLoop",
    "EngineMessage",
    "EngineToolCall",
    "EngineToolResult",
    "EngineUsage",
    "EngineProviderBase",
    "EngineOpenAIProvider",
    "EngineChatResponse",
    "EngineToolDefinition",
    "EngineTool",
    "EnginePermissionPolicy",
]