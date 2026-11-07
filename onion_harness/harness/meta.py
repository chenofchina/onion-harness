"""Meta tools - the AI's only route to modifying Onion.

Every meta tool maps to exactly one :class:`ChangeKind` and *proposes* a change
through the ledger.  None of them can apply anything: the harness exposes no
apply capability to the model at all, so a model cannot confirm its own work.
"""

from __future__ import annotations

from typing import Any, Dict, List

from onion_harness.core.tool import OnionTool
from onion_harness.ledger import ChangeKind, ChangeLedger, ChangeRecord, LedgerError
from onion_harness.ledger.state import UI_PANEL_POSITIONS, UI_PANEL_SIZES


class MetaTools:
    """Builds the ``onion_*`` tool set bound to one ledger and one task."""

    def __init__(self, ledger: ChangeLedger, task_id: str, emit=None) -> None:
        self._ledger = ledger
        self._task_id = task_id
        self._emit = emit or (lambda *_args, **_kwargs: None)

    def tools(self) -> List[OnionTool]:
        return [
            OnionTool(
                name="onion_register_executor",
                description=(
                    "Propose adding an executor to the sandbox. Allowed kinds are fixed: "
                    "python, powershell, cmd, node, ruby, bash, pwsh. This only creates a "
                    "proposal; a human must confirm it in the UI before it takes effect."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Executor identifier."},
                        "reason": {"type": "string", "description": "Why this executor is needed."},
                    },
                    "required": ["name", "reason"],
                    "additionalProperties": False,
                },
                handler=self._register_executor,
                metadata={"kind": "meta", "label": "Register executor", "icon": "plug"},
            ),
            OnionTool(
                name="onion_register_model_provider",
                description=(
                    "Propose a new model provider preset. Provide a label, an http(s) base URL, "
                    "a wire format (openai or anthropic) and optionally model ids. The user "
                    "still supplies their own API key. Proposals require human confirmation."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "base_url": {"type": "string"},
                        "format": {"type": "string", "enum": ["openai", "anthropic"]},
                        "models": {"type": "array", "items": {"type": "string"}},
                        "icon": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["label", "base_url", "reason"],
                    "additionalProperties": False,
                },
                handler=self._register_provider,
                metadata={"kind": "meta", "label": "Register provider", "icon": "cloud"},
            ),
            OnionTool(
                name="onion_update_system_prompt",
                description=(
                    "Propose an edit to a system prompt segment. Use segment_id 'core' for the "
                    "main instructions or a new id to append an extra segment. The prompt is "
                    "injected through the system channel and survives context compression."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "segment_id": {"type": "string", "default": "core"},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "enabled": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                    "required": ["content", "reason"],
                    "additionalProperties": False,
                },
                handler=self._update_system_prompt,
                metadata={"kind": "meta", "label": "Update prompt", "icon": "file-text"},
            ),
            OnionTool(
                name="onion_update_ui_panel",
                description=(
                    "Propose a layout change for one existing panel. Only predefined panel ids, "
                    "positions (left, center, right, top, bottom, hidden) and sizes (compact, "
                    "regular, wide) are accepted. Free-form HTML/JS is impossible by design."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "panel_id": {"type": "string"},
                        "position": {
                            "type": "string",
                            "enum": sorted(UI_PANEL_POSITIONS),
                        },
                        "size": {"type": "string", "enum": sorted(UI_PANEL_SIZES)},
                        "visible": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                    "required": ["panel_id", "reason"],
                    "additionalProperties": False,
                },
                handler=self._update_ui_panel,
                metadata={"kind": "meta", "label": "Update panel", "icon": "layout"},
            ),
            OnionTool(
                name="onion_update_skill_registry",
                description=(
                    "Propose mounting or unmounting a skill. Mounting may touch state outside "
                    "Onion, so this proposal is marked as not automatically reversible and "
                    "requires the user to explicitly accept that before it can be applied."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["mount", "unmount"]},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "path": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["action", "name", "reason"],
                    "additionalProperties": False,
                },
                handler=self._update_skills,
                metadata={"kind": "meta", "label": "Update skills", "icon": "puzzle"},
            ),
            OnionTool(
                name="onion_create_mcp_tool",
                description=(
                    "Propose creating a new MCP tool. Provide a snake_case name, description, "
                    "JSON-schema parameters, and a Python body defining "
                    "`def handle(arguments):`. The body may import only standard utility "
                    "modules (json, re, math, datetime, pathlib, ...); network and shell "
                    "modules are refused. On confirmation the script is written to the local "
                    "mcp/ directory and hot-loaded into the tool registry. Nothing is written "
                    "until the user confirms."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "lowercase_snake_case tool name"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "parameters": {
                            "type": "object",
                            "description": "JSON Schema object describing the arguments",
                        },
                        "code": {
                            "type": "string",
                            "description": "Python body defining def handle(arguments): ...",
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["name", "description", "parameters", "code", "reason"],
                    "additionalProperties": False,
                },
                handler=self._create_mcp_tool,
                metadata={"kind": "meta", "label": "Create MCP tool", "icon": "mcp"},
            ),
            OnionTool(
                name="onion_remove_mcp_tool",
                description=(
                    "Propose removing a generated MCP tool. After the user confirms, the tool "
                    "is unloaded and its files are deleted. The audit record stays in the "
                    "change history."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["name", "reason"],
                    "additionalProperties": False,
                },
                handler=self._remove_mcp_tool,
                metadata={"kind": "meta", "label": "Remove MCP tool", "icon": "mcp"},
            ),
        ]

    # -- handlers -----------------------------------------------------------
    def _register_executor(self, args: Dict[str, Any]) -> str:
        return self._propose(
            ChangeKind.REGISTER_EXECUTOR,
            target=str(args.get("name", "")),
            reason=str(args.get("reason", "")),
            params={"name": str(args.get("name", ""))},
            impact=["tool catalogue", "available executors in the composer"],
        )

    def _register_provider(self, args: Dict[str, Any]) -> str:
        return self._propose(
            ChangeKind.REGISTER_MODEL_PROVIDER,
            target=str(args.get("label", "")),
            reason=str(args.get("reason", "")),
            params={
                "label": args.get("label", ""),
                "base_url": args.get("base_url", ""),
                "format": args.get("format", "openai"),
                "models": args.get("models") or [],
                "icon": args.get("icon", "openai"),
            },
            impact=["model selector options"],
        )

    def _update_system_prompt(self, args: Dict[str, Any]) -> str:
        return self._propose(
            ChangeKind.UPDATE_SYSTEM_PROMPT,
            target=str(args.get("segment_id", "core")),
            reason=str(args.get("reason", "")),
            params={
                "segment_id": args.get("segment_id", "core"),
                "title": args.get("title"),
                "content": args.get("content"),
                "enabled": args.get("enabled"),
            },
            impact=["all future model calls"],
        )

    def _update_ui_panel(self, args: Dict[str, Any]) -> str:
        return self._propose(
            ChangeKind.UPDATE_UI_PANEL,
            target=str(args.get("panel_id", "")),
            reason=str(args.get("reason", "")),
            params={
                "panel_id": args.get("panel_id"),
                "position": args.get("position"),
                "size": args.get("size"),
                "visible": args.get("visible"),
            },
            impact=["GUI layout only"],
        )

    def _update_skills(self, args: Dict[str, Any]) -> str:
        return self._propose(
            ChangeKind.UPDATE_SKILL_REGISTRY,
            target=str(args.get("name", "")),
            reason=str(args.get("reason", "")),
            params={
                "action": args.get("action", "mount"),
                "name": args.get("name"),
                "description": args.get("description"),
                "path": args.get("path"),
            },
            impact=["available skills", "external state may change"],
        )

    def _create_mcp_tool(self, args: Dict[str, Any]) -> str:
        return self._propose(
            ChangeKind.REGISTER_MCP_TOOL,
            target=str(args.get("name", "")),
            reason=str(args.get("reason", "")),
            params={
                "name": args.get("name"),
                "title": args.get("title") or args.get("name"),
                "description": args.get("description"),
                "parameters": args.get("parameters") or {},
                "code": args.get("code"),
                "enabled": True,
            },
            impact=["tool catalogue", "local mcp/ directory", "hot reload on confirm"],
        )

    def _remove_mcp_tool(self, args: Dict[str, Any]) -> str:
        return self._propose(
            ChangeKind.UNREGISTER_MCP_TOOL,
            target=str(args.get("name", "")),
            reason=str(args.get("reason", "")),
            params={"name": args.get("name")},
            impact=["tool catalogue", "local mcp/ directory"],
        )

    def _propose(
        self,
        kind: ChangeKind,
        target: str,
        reason: str,
        params: Dict[str, Any],
        impact: List[str],
    ) -> str:
        record = ChangeRecord(
            task_id=self._task_id,
            kind=kind,
            target=target,
            reason=reason,
            params=params,
            impact=impact,
        )
        try:
            change_id = self._ledger.propose(record)
        except LedgerError as exc:
            return f"Proposal rejected by validation: {exc}"
        return (
            f"Proposed change {change_id} ({record.label}) on '{target}'. "
            "It is NOT applied yet; the user must confirm it in the interface."
        )


__all__ = ["MetaTools"]