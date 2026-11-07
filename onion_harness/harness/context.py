"""Context compression.

Policy: keep the **most recent K messages verbatim**, replace everything older
with a compact summary block.  The summary is delivered as a ``system`` role
message, which means it travels through the system channel and survives
provider-level truncation and further compression passes.

The summariser is deterministic and local (no extra model call): it extracts
user intents, tool names and error lines.  That keeps compression cheap,
predictable and testable.  A model-backed summariser can be plugged in via
``summarizer=`` without touching the policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

SUMMARY_HEADER = "[compressed earlier context]"
MAX_SUMMARY_CHARS = 3000


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~3.6 chars/token for mixed CJK + Latin text."""
    if not text:
        return 0
    return max(1, int(len(text) / 3.6) + 1)


@dataclass
class CompressionStats:
    compressed: bool = False
    before: int = 0
    after: int = 0
    dropped_messages: int = 0
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "compressed": self.compressed,
            "before": self.before,
            "after": self.after,
            "dropped_messages": self.dropped_messages,
            "summary": self.summary,
            "saved": max(0, self.before - self.after),
        }


Summarizer = Callable[[List[Any]], str]


class ContextCompressor:
    """Keeps the last ``keep_recent`` messages and summarises the rest."""

    def __init__(
        self,
        keep_recent: int = 8,
        max_tokens: int = 60_000,
        summarizer: Optional[Summarizer] = None,
    ) -> None:
        self.keep_recent = max(2, keep_recent)
        self.max_tokens = max_tokens
        self._summarizer = summarizer or _default_summarizer
        self.enabled = True

    def compress(self, messages: List[Any]) -> Tuple[List[Any], Dict[str, Any]]:
        before = sum(estimate_tokens(_text_of(m)) for m in messages)
        stats = CompressionStats(before=before, after=before)

        if not self.enabled or len(messages) <= self.keep_recent:
            return list(messages), stats.to_dict()

        system_msgs: List[Any] = []
        body: List[Any] = []
        for message in messages:
            if getattr(message, "type", None) == "system":
                system_msgs.append(message)
            else:
                body.append(message)

        if len(body) <= self.keep_recent:
            return list(messages), stats.to_dict()

        recent = body[-self.keep_recent :]
        older = body[: -self.keep_recent]

        # Never split an assistant/tool-result pair: if the oldest kept message
        # is a tool result, pull its assistant partner into the older group.
        if recent and getattr(recent[0], "type", None) == "user" and getattr(
            recent[0], "tool_results", None
        ):
            if older:
                recent = [older[-1]] + recent
                older = older[:-1]

        summary_text = self._summarizer(older)
        if summary_text:
            summary_text = summary_text[:MAX_SUMMARY_CHARS]
        stats.summary = summary_text
        stats.dropped_messages = len(older)

        compressed: List[Any] = list(system_msgs)
        if summary_text:
            compressed.append(make_system_message(summary_text))
        compressed.extend(recent)

        after = sum(estimate_tokens(_text_of(m)) for m in compressed)
        stats.after = after
        stats.compressed = True

        # If we are still over budget, drop older *recent* messages too, keeping
        # at least the last two and always keeping the summary.
        if after > self.max_tokens and len(recent) > 2:
            trim = after - self.max_tokens
            kept = list(recent)
            while trim > 0 and len(kept) > 2:
                dropped = kept.pop(0)
                trim -= estimate_tokens(_text_of(dropped))
            compressed = list(system_msgs)
            if summary_text:
                compressed.append(make_system_message(summary_text))
            compressed.extend(kept)
            stats.after = sum(estimate_tokens(_text_of(m)) for m in compressed)

        return compressed, stats.to_dict()


def make_system_message(text: str) -> Any:
    """Create a provider-neutral system message through the engine's helper."""
    from onion_harness.harness.bridge import require_engine

    require_engine()
    from onion_harness.harness.bridge import EngineMessage

    return EngineMessage.system(text=text)


def _default_summarizer(messages: List[Any]) -> str:
    """Deterministic, local summary of dropped messages."""
    user_intents: List[str] = []
    tools_used: List[str] = []
    errors: List[str] = []
    assistant_notes: List[str] = []

    for message in messages:
        mtype = getattr(message, "type", None)
        text = (getattr(message, "text", "") or "").strip()

        if mtype == "user":
            if text and not getattr(message, "tool_results", None):
                user_intents.append(_clip(text, 200))
            for result in getattr(message, "tool_results", []) or []:
                name = getattr(result, "name", "") or "tool"
                if name not in tools_used:
                    tools_used.append(name)
                content = getattr(result, "content", "") or ""
                if getattr(result, "is_error", False):
                    errors.append(f"{name}: {_clip(content, 140)}")
                else:
                    lowered = content.lower()
                    if "error" in lowered or "traceback" in lowered or "exit code" in lowered:
                        line = next(
                            (ln for ln in content.splitlines() if ln.strip()),
                            "",
                        )
                        errors.append(f"{name}: {_clip(line, 140)}")
        elif mtype == "assistant":
            if text:
                assistant_notes.append(_clip(text, 180))
            for call in getattr(message, "tool_calls", []) or []:
                name = getattr(call, "name", "") or "tool"
                if name not in tools_used:
                    tools_used.append(name)

    lines = [SUMMARY_HEADER]
    if user_intents:
        lines.append("Requested by the user earlier:")
        lines.extend(f"- {item}" for item in _dedupe(user_intents[-4:]))
    if tools_used:
        lines.append("Tools already invoked: " + ", ".join(tools_used[:12]))
    if errors:
        lines.append("Previous errors to avoid repeating:")
        lines.extend(f"- {item}" for item in _dedupe(errors[-4:]))
    if assistant_notes:
        lines.append("Earlier conclusions:")
        lines.extend(f"- {item}" for item in _dedupe(assistant_notes[-3:]))
    if len(lines) == 1:
        lines.append(f"{len(messages)} earlier messages were compressed.")

    return "\n".join(lines)


def _text_of(message: Any) -> str:
    parts = [getattr(message, "text", "") or ""]
    for call in getattr(message, "tool_calls", []) or []:
        parts.append(str(getattr(call, "name", "")))
        parts.append(json.dumps(getattr(call, "arguments", {}) or {}, ensure_ascii=False))
    for result in getattr(message, "tool_results", []) or []:
        parts.append(getattr(result, "content", "") or "")
    return "\n".join(parts)


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


__all__ = ["ContextCompressor", "CompressionStats", "estimate_tokens", "make_system_message"]