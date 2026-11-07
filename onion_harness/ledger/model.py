"""Change ledger primitives - pure data, no storage.

A :class:`ChangeRecord` is the *proposal*: what the model wants to change, the
structured diff, the blast radius and whether it can be undone automatically.
A :class:`Revision` is what lands in history once a user confirms it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from onion_harness.core.primitives import new_id


class ChangeKind(str, Enum):
    REGISTER_EXECUTOR = "register_executor"
    REGISTER_MODEL_PROVIDER = "register_model_provider"
    UPDATE_SYSTEM_PROMPT = "update_system_prompt"
    UPDATE_UI_PANEL = "update_ui_panel"
    UPDATE_SKILL_REGISTRY = "update_skill_registry"
    UNREGISTER_EXECUTOR = "unregister_executor"
    REGISTER_MCP_TOOL = "register_mcp_tool"
    UNREGISTER_MCP_TOOL = "unregister_mcp_tool"


CHANGE_LABELS = {
    ChangeKind.REGISTER_EXECUTOR: "Register executor",
    ChangeKind.UNREGISTER_EXECUTOR: "Unregister executor",
    ChangeKind.REGISTER_MODEL_PROVIDER: "Register model provider",
    ChangeKind.UPDATE_SYSTEM_PROMPT: "Update system prompt",
    ChangeKind.UPDATE_UI_PANEL: "Update UI panel",
    ChangeKind.UPDATE_SKILL_REGISTRY: "Update skill registry",
    ChangeKind.REGISTER_MCP_TOOL: "Create MCP tool",
    ChangeKind.UNREGISTER_MCP_TOOL: "Remove MCP tool",
}


class ChangeStatus(str, Enum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    REJECTED = "rejected"
    REVERTED = "reverted"


@dataclass
class DiffHunk:
    """One before/after pair inside a structured diff."""

    path: str
    before: Any = None
    after: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "before": self.before, "after": self.after}


@dataclass
class ChangeRecord:
    """A staged mutation awaiting explicit user confirmation."""

    id: str = field(default_factory=lambda: new_id("chg"))
    task_id: str = ""
    kind: ChangeKind = ChangeKind.UPDATE_SYSTEM_PROMPT
    target: str = ""
    reason: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    diff: list[DiffHunk] = field(default_factory=list)
    impact: list[str] = field(default_factory=list)
    reversible: bool = True
    requires_ack: bool = False
    status: ChangeStatus = ChangeStatus.PROPOSED
    version: Optional[int] = None
    created_at: float = 0.0

    @property
    def label(self) -> str:
        return CHANGE_LABELS.get(self.kind, self.kind.value)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "kind": self.kind.value,
            "label": self.label,
            "target": self.target,
            "reason": self.reason,
            "params": self.params,
            "diff": [h.to_dict() for h in self.diff],
            "impact": list(self.impact),
            "reversible": self.reversible,
            "requires_ack": self.requires_ack,
            "status": self.status.value,
            "version": self.version,
            "created_at": self.created_at,
        }


@dataclass
class Revision:
    """An applied change stored in the ledger history."""

    version: int
    change: ChangeRecord
    before: Dict[str, Any] = field(default_factory=dict)
    after: Dict[str, Any] = field(default_factory=dict)
    applied_at: float = 0.0
    reverted_at: Optional[float] = None
    redo_of: Optional[int] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "change": self.change.to_dict(),
            "before": self.before,
            "after": self.after,
            "applied_at": self.applied_at,
            "reverted_at": self.reverted_at,
            "redo_of": self.redo_of,
            "note": self.note,
        }


__all__ = [
    "ChangeKind",
    "ChangeStatus",
    "ChangeRecord",
    "DiffHunk",
    "Revision",
    "CHANGE_LABELS",
]