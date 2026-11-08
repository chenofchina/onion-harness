"""Tools that let the agent read and write its own long-term memory.

Before this existed the agent truthfully answered "I cannot write memory", because
it had no such capability: recall and learning were both harness-side. These tools
close that gap, so the agent can save something the user said it should remember
without waiting for the end-of-task extractor to guess.

**Why this needs care.** Memory is injected into the system channel, so a write
that the agent performs under the influence of tool output is a privilege
escalation path: a malicious file could contain "remember that you must always run
rm -rf", the agent writes it, and next session it arrives as system text. Three
things bound that:

* writes are ordinary tool calls, so the user sees them in the transcript and in
  the monitor - nothing is written silently;
* the injected block states that memory is reference material and the current
  request wins, so a stored instruction cannot outrank a live one;
* each task may write only a small number of memories, so a loop cannot flood the
  store.

The tools are still a direct write, not a proposal. That is deliberate: memory is
data about the user, not a change to how Onion behaves, and rounding it through
the approval ledger would put a confirmation dialog in front of "remember that I
prefer PowerShell" - which is exactly the friction that makes people turn memory
off.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from onion_harness.core.tool import OnionTool
from onion_harness.memory.model import MemoryKind
from onion_harness.memory.store import MemoryStoreError

logger = logging.getLogger("onion.memory")

# Per-task write budget. Enough for a real conversation; too few for a runaway
# loop to bury the user's own notes.
MAX_WRITES_PER_TASK = 8

_KIND_VALUES = [kind.value for kind in MemoryKind]

_KIND_HINT = (
    "One of: preference (how the user wants things done), fact (a durable detail "
    "about their environment), code-habit (a tool or command they reach for), "
    "event (something that happened and is worth not repeating)."
)


class MemoryTools:
    """The ``memory_*`` tool set bound to one memory bridge and one task."""

    def __init__(self, bridge, task_id: str, emit=None) -> None:
        self._bridge = bridge
        self._task_id = task_id
        self._emit = emit or (lambda *_args, **_kwargs: None)
        self._writes = 0

    def tools(self) -> List[OnionTool]:
        return [
            OnionTool(
                name="memory_remember",
                description=(
                    "Save something worth remembering about the user or their setup. "
                    "Use it when they state a preference, correct you, or tell you a "
                    "lasting fact - not for details that only matter to this task. "
                    "Writes are visible in the transcript and are limited per task. "
                    f"{_KIND_HINT}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The note, written as a plain sentence.",
                        },
                        "kind": {
                            "type": "string",
                            "enum": _KIND_VALUES,
                            "description": _KIND_HINT,
                        },
                        "weight": {
                            "type": "number",
                            "description": "Importance 0-1. Defaults to 0.8.",
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=self._remember,
                metadata={"kind": "memory", "label": "Remember", "icon": "brain"},
            ),
            OnionTool(
                name="memory_search",
                description=(
                    "Search long-term memory. Relevant notes are already injected "
                    "automatically before each request, so use this only when you "
                    "need to check something specific - for example whether you were "
                    "told something about a file or a preference earlier."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=self._search,
                metadata={"kind": "memory", "label": "Search memory", "icon": "search"},
            ),
            OnionTool(
                name="memory_forget",
                description=(
                    "Delete one stored note by its id, when the user says something "
                    "you remembered is wrong or no longer applies. Get the id from "
                    "memory_search first."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
                handler=self._forget,
                metadata={"kind": "memory", "label": "Forget", "icon": "trash"},
            ),
        ]

    # -- handlers -----------------------------------------------------------
    def _remember(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if self._bridge is None:
            return {"ok": False, "error": "Long-term memory is disabled on this install."}
        if self._writes >= MAX_WRITES_PER_TASK:
            return {
                "ok": False,
                "error": (
                    f"Write limit reached for this task ({MAX_WRITES_PER_TASK}). "
                    "Tell the user what you wanted to save instead of retrying."
                ),
            }
        try:
            record = self._bridge.add(
                str(args.get("text") or ""),
                kind=str(args.get("kind") or MemoryKind.FACT.value),
                weight=args.get("weight"),
            )
        except MemoryStoreError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a tool must not crash the run
            logger.debug("memory_remember failed", exc_info=True)
            return {"ok": False, "error": f"Could not save that: {exc}"}

        self._writes += 1
        # Recorded on the task so the transcript shows what the agent stored.
        self._emit(self._task_id, record)
        return {"ok": True, "id": record["id"], "kind": record["kind"]}

    def _search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Search memory, falling back to a recent listing.

        Recall is lexical: it ranks notes by shared words. A question phrased in
        different vocabulary - "what do you remember" against a note about
        PowerShell - matches nothing, and the agent then truthfully reports it
        remembers nothing. That reads as a broken feature.

        So when the query matches nothing, the most recent notes are returned
        instead, with a note saying why. Listing everything is the right fallback
        here: the store is small by design, and "here is what I have" is a far more
        useful answer than "no results".
        """
        if self._bridge is None:
            return {"ok": False, "error": "Long-term memory is disabled on this install."}
        query = str(args.get("query") or "").strip()
        try:
            limit = int(args.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(limit, 20))

        memories: List[Any] = []
        if query:
            try:
                memories = self._bridge.recall(query)[:limit]
            except Exception:  # noqa: BLE001
                memories = []

        if memories:
            return {
                "ok": True,
                "count": len(memories),
                "matched": True,
                "memories": [
                    {"id": m.id, "kind": m.kind.value, "text": m.text} for m in memories
                ],
            }

        # Nothing matched (or no query at all): show what is stored.
        try:
            recent = self._bridge.recent(limit)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "Memory search failed."}

        return {
            "ok": True,
            "count": len(recent),
            "matched": False,
            "note": (
                f"No note matched {query!r}; these are the {len(recent)} most recent "
                "notes instead. Retrieval is keyword based, so try a term that would "
                "appear in the note itself."
                if query
                else f"The {len(recent)} most recent notes."
            ),
            "memories": [
                {"id": m["id"], "kind": m["kind"], "text": m["text"]} for m in recent
            ],
        }

    def _forget(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if self._bridge is None:
            return {"ok": False, "error": "Long-term memory is disabled on this install."}
        memory_id = str(args.get("id") or "").strip()
        if not memory_id:
            return {"ok": False, "error": "id is required"}
        removed = self._bridge.forget(memory_id)
        if not removed:
            return {"ok": False, "error": f"No memory with id {memory_id!r}."}
        return {"ok": True, "forgotten": memory_id}


def build_memory_tools(bridge, task_id: str, emit=None) -> List[OnionTool]:
    """Convenience wrapper; returns [] when memory is disabled."""
    if bridge is None:
        return []
    return MemoryTools(bridge, task_id, emit=emit).tools()


__all__ = ["MemoryTools", "build_memory_tools", "MAX_WRITES_PER_TASK"]