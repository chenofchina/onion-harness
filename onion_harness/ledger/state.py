"""The materialised settings state that meta-operations edit.

Everything the AI is allowed to change about Onion lives in this dict-shaped
document.  It is small on purpose: no HTML, no JS, no arbitrary code paths.
Each applier is a pure ``state -> state`` transform, which is what makes
rollback a matter of restoring a snapshot.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

from onion_harness.ledger.model import ChangeKind, ChangeRecord

UI_PANEL_TYPES = {
    "event-stream",
    "tool-monitor",
    "token-usage",
    "meta-confirmations",
    "task-history",
    "model-selector",
    "context-bar",
}

UI_PANEL_POSITIONS = {"left", "center", "right", "top", "bottom", "hidden"}
UI_PANEL_SIZES = {"compact", "regular", "wide"}

DEFAULT_SYSTEM_PROMPT = """You are Onion, a careful local automation agent running inside the Onion Harness.

Operating rules:
- Work step by step, using the available executors to inspect and change files.
- Prefer read-only inspection before destructive actions.
- Keep commands small and verify results before moving on.
- Never assume a command succeeded; read the output.
- When you want to change Onion itself, use the meta tools. They only propose
  changes; a human must confirm them. Never claim a change is applied until the
  confirmation arrives.
- Report outcomes plainly, including failures and skipped steps.
"""

DEFAULT_STATE: Dict[str, Any] = {
    "system_prompt_segments": [
        {
            "id": "core",
            "title": "Core behaviour",
            "content": DEFAULT_SYSTEM_PROMPT,
            "enabled": True,
            "order": 0,
        }
    ],
    "executors": ["python", "powershell", "cmd"],
    "model_providers": [],
    "ui_panels": [
        {"id": "task-history", "type": "task-history", "position": "left", "visible": True, "size": "regular"},
        {"id": "event-stream", "type": "event-stream", "position": "center", "visible": True, "size": "wide"},
        {"id": "tool-monitor", "type": "tool-monitor", "position": "right", "visible": True, "size": "regular"},
        {"id": "token-usage", "type": "token-usage", "position": "right", "visible": True, "size": "compact"},
        {"id": "meta-confirmations", "type": "meta-confirmations", "position": "right", "visible": True, "size": "regular"},
        {"id": "context-bar", "type": "context-bar", "position": "top", "visible": True, "size": "compact"},
    ],
    "skills": [],
    "mcp_tools": [],
}


def default_state() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_STATE)


def normalise_state(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    state = default_state()
    if not raw:
        return state
    for key, value in raw.items():
        if key in state and value is not None:
            state[key] = copy.deepcopy(value)
    return state


class ChangeRejected(ValueError):
    """The change cannot be applied (invalid params or unknown target)."""


def validate(record: ChangeRecord, state: Dict[str, Any]) -> None:
    """Cheap structural validation before a proposal is staged."""
    kind = record.kind
    params = record.params

    if kind is ChangeKind.REGISTER_EXECUTOR:
        name = str(params.get("name", "")).strip().lower()
        if not name.isidentifier():
            raise ChangeRejected("executor name must be a simple identifier")
        if name not in {"python", "powershell", "cmd", "node", "ruby", "bash", "pwsh"}:
            raise ChangeRejected(
                "unknown executor kind; allowed: python, powershell, cmd, node, ruby, bash, pwsh"
            )
    elif kind is ChangeKind.UNREGISTER_EXECUTOR:
        name = str(params.get("name", ""))
        if name not in state.get("executors", []):
            raise ChangeRejected(f"executor {name!r} is not registered")
        if name in {"python", "cmd"}:
            raise ChangeRejected("core executors cannot be removed")
    elif kind is ChangeKind.REGISTER_MODEL_PROVIDER:
        if not str(params.get("label", "")).strip():
            raise ChangeRejected("provider label is required")
        if not str(params.get("base_url", "")).strip().startswith(("http://", "https://")):
            raise ChangeRejected("provider base_url must be an http(s) URL")
        fmt = params.get("format", "openai")
        if fmt not in {"openai", "anthropic"}:
            raise ChangeRejected("provider format must be 'openai' or 'anthropic'")
    elif kind is ChangeKind.UPDATE_SYSTEM_PROMPT:
        content = params.get("content")
        if content is not None and not isinstance(content, str):
            raise ChangeRejected("system prompt content must be a string")
        if content is not None and len(content) > 20000:
            raise ChangeRejected("system prompt segment is too long (max 20000 chars)")
    elif kind is ChangeKind.UPDATE_UI_PANEL:
        panel_id = str(params.get("panel_id", ""))
        panel = next((p for p in state.get("ui_panels", []) if p["id"] == panel_id), None)
        if panel is None:
            raise ChangeRejected(f"unknown UI panel {panel_id!r}")
        position = params.get("position")
        if position is not None and position not in UI_PANEL_POSITIONS:
            raise ChangeRejected(f"position must be one of {sorted(UI_PANEL_POSITIONS)}")
        size = params.get("size")
        if size is not None and size not in UI_PANEL_SIZES:
            raise ChangeRejected(f"size must be one of {sorted(UI_PANEL_SIZES)}")
    elif kind is ChangeKind.UPDATE_SKILL_REGISTRY:
        action = params.get("action", "mount")
        if action not in {"mount", "unmount"}:
            raise ChangeRejected("skill action must be 'mount' or 'unmount'")
        if action == "mount" and not str(params.get("name", "")).strip():
            raise ChangeRejected("skill name is required when mounting")
    elif kind in (ChangeKind.REGISTER_MCP_TOOL, ChangeKind.UNREGISTER_MCP_TOOL):
        from onion_harness.core.mcp_contract import McpError, validate_spec

        if kind is ChangeKind.UNREGISTER_MCP_TOOL:
            name = str(params.get("name", "")).strip()
            known = {entry.get("name") for entry in state.get("mcp_tools", [])}
            if name not in known:
                raise ChangeRejected(f"MCP tool {name!r} does not exist")
        else:
            try:
                validate_spec(
                    str(params.get("name", "")),
                    params.get("parameters") or {},
                    str(params.get("code", "")),
                )
            except McpError as exc:
                raise ChangeRejected(str(exc)) from exc
            if not str(params.get("description", "")).strip():
                raise ChangeRejected("description is required")


def apply_to_state(record: ChangeRecord, state: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new state with ``record`` applied. Never mutates the input."""
    new_state = copy.deepcopy(state)
    kind = record.kind
    params = record.params

    if kind is ChangeKind.REGISTER_EXECUTOR:
        name = str(params["name"]).strip().lower()
        executors = list(new_state.get("executors", []))
        if name not in executors:
            executors.append(name)
        new_state["executors"] = executors

    elif kind is ChangeKind.UNREGISTER_EXECUTOR:
        name = str(params["name"])
        new_state["executors"] = [e for e in new_state.get("executors", []) if e != name]

    elif kind is ChangeKind.REGISTER_MODEL_PROVIDER:
        provider = {
            "id": str(params.get("id") or params["label"]).strip().lower().replace(" ", "-"),
            "label": str(params["label"]).strip(),
            "base_url": str(params["base_url"]).strip(),
            "format": params.get("format", "openai"),
            "models": list(params.get("models") or []),
            "icon": params.get("icon", "openai"),
            "builtin": False,
        }
        providers = [p for p in new_state.get("model_providers", []) if p.get("id") != provider["id"]]
        providers.append(provider)
        new_state["model_providers"] = providers

    elif kind is ChangeKind.UPDATE_SYSTEM_PROMPT:
        segment_id = str(params.get("segment_id", "core")).strip() or "core"
        segments = list(new_state.get("system_prompt_segments", []))
        found = False
        for segment in segments:
            if segment["id"] == segment_id:
                found = True
                if params.get("content") is not None:
                    segment["content"] = params["content"]
                if params.get("title") is not None:
                    segment["title"] = params["title"]
                if params.get("enabled") is not None:
                    segment["enabled"] = bool(params["enabled"])
        if not found:
            if params.get("content") is None:
                raise ChangeRejected(f"unknown prompt segment {segment_id!r}")
            segments.append(
                {
                    "id": segment_id,
                    "title": params.get("title") or segment_id,
                    "content": params["content"],
                    "enabled": bool(params.get("enabled", True)),
                    "order": len(segments),
                }
            )
        segments.sort(key=lambda s: s.get("order", 0))
        new_state["system_prompt_segments"] = segments

    elif kind is ChangeKind.UPDATE_UI_PANEL:
        panel_id = str(params["panel_id"])
        for panel in new_state.get("ui_panels", []):
            if panel["id"] != panel_id:
                continue
            for field in ("position", "size", "visible"):
                if params.get(field) is not None:
                    panel[field] = params[field]

    elif kind is ChangeKind.UPDATE_SKILL_REGISTRY:
        name = str(params.get("name", "")).strip()
        skills = list(new_state.get("skills", []))
        if params.get("action", "mount") == "mount":
            entry = {
                "name": name,
                "description": params.get("description", ""),
                "path": params.get("path", ""),
                "enabled": True,
            }
            skills = [s for s in skills if s.get("name") != name] + [entry]
        else:
            skills = [s for s in skills if s.get("name") != name]
        new_state["skills"] = skills

    elif kind is ChangeKind.REGISTER_MCP_TOOL:
        name = str(params["name"]).strip()
        tools = [t for t in new_state.get("mcp_tools", []) if t.get("name") != name]
        tools.append(
            {
                "name": name,
                "title": params.get("title") or name,
                "description": params.get("description", ""),
                "parameters": params.get("parameters") or {},
                "code": params.get("code", ""),
                "enabled": bool(params.get("enabled", True)),
            }
        )
        new_state["mcp_tools"] = tools

    elif kind is ChangeKind.UNREGISTER_MCP_TOOL:
        name = str(params["name"]).strip()
        new_state["mcp_tools"] = [
            t for t in new_state.get("mcp_tools", []) if t.get("name") != name
        ]

    else:  # pragma: no cover - enum exhaustiveness
        raise ChangeRejected(f"unsupported change kind: {kind}")

    return new_state


def compose_system_prompt(state: Dict[str, Any]) -> str:
    segments = [s for s in state.get("system_prompt_segments", []) if s.get("enabled", True)]
    segments.sort(key=lambda s: s.get("order", 0))
    return "\n\n".join(s.get("content", "") for s in segments if s.get("content")).strip()


__all__ = [
    "DEFAULT_STATE",
    "DEFAULT_SYSTEM_PROMPT",
    "UI_PANEL_TYPES",
    "UI_PANEL_POSITIONS",
    "UI_PANEL_SIZES",
    "default_state",
    "normalise_state",
    "validate",
    "apply_to_state",
    "compose_system_prompt",
    "ChangeRejected",
]