"""Engine adapters - translate between neutral Onion types and the engine.

Three adapters live here:

* :class:`PermissionAdapter` denies every meta tool a model tries to call
  directly, and allows everything else that the catalogue exposes.
* :class:`EngineMessageTranslator` converts engine messages into Onion events.
* :class:`SessionFactory` builds the engine configuration for one task.

The engine's own types never leave this module except as data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from onion_harness.core.events import EventType, OnionEvent, event
from onion_harness.core.primitives import Layer
from onion_harness.harness.bridge import (
    EnginePermissionPolicy,
    adapt_tool,
)
from onion_harness.harness.errors import sanitize

def _explain_turn_limit(text: str, max_turns: int) -> str:
    """Turn the engine's bare "max_turns reached" into something actionable.

    The engine reports it as a plain error, which reads like a crash. It is not -
    the run simply used its whole budget - and the fix is a setting the user owns,
    so the message says that instead of leaving them to guess.
    """
    lowered = (text or "").lower()
    if "max_turns" not in lowered and "max turns" not in lowered:
        return text
    limit = max_turns or 0
    where = f" (currently {limit})" if limit else ""
    return (
        f"The task used its entire turn budget{where} without finishing. This is a "
        "limit, not a failure: raise Max agent turns in Settings and send the message "
        "again, or break the request into smaller steps."
    )


def _error_text(message: Any, fallback: str) -> str:
    """Read an engine message's error text safely.

    The engine's Message class defines a ``Message.error`` classmethod that sits
    on the same name as its ``error`` dataclass field. When the field is None the
    attribute lookup returns the bound method, and using it in a boolean-or chain
    put "<bound method Message.error ...>" into user-facing error cards.
    """
    raw = getattr(message, "error", None)
    if isinstance(raw, str) and raw.strip():
        return raw
    text = getattr(message, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    return fallback


META_TOOL_NAMES = {
    "onion_register_executor",
    "onion_register_model_provider",
    "onion_update_system_prompt",
    "onion_update_ui_panel",
    "onion_update_skill_registry",
}


@dataclass
class MetaCall:
    """A meta tool invocation observed in the stream (for the monitor panel)."""

    tool_name: str
    arguments: Dict[str, Any]
    call_id: str


class PermissionAdapter:
    """Runtime permission policy for a single task.

    The allow-list is exactly the tool set the harness built, which means the
    engine's own builtins (including its shell tool) never load.  Meta tools
    are allowed because they can only *propose*; there is no apply tool for a
    model to reach.
    """

    def __init__(self, allowed_tools: List[str]) -> None:
        self._allowed = set(allowed_tools)
        self._blocked_meta_calls: List[MetaCall] = []

    def engine_policy(self):
        return EnginePermissionPolicy(
            allow=sorted(self._allowed),
            deny=[],
            ask=[],
        )

    @property
    def blocked_meta_calls(self) -> List[MetaCall]:
        return list(self._blocked_meta_calls)


class EngineMessageTranslator:
    """Converts engine messages into Onion events with stable sequencing."""

    def __init__(
        self,
        task_id: str,
        emit,
        context_window: int = 128_000,
        max_turns: int = 0,
    ) -> None:
        self._task_id = task_id
        self._emit = emit
        # Used only to make the turn-limit error actionable; 0 means unknown.
        self.max_turns = int(max_turns or 0)
        # The real window comes from the run configuration; the class default is
        # only a fallback. Hardcoding it here made the usage ring always show
        # 128k no matter what the user configured.
        self.context_window = int(context_window or 128_000)
        self._seq = 0
        self._turn = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.last_turn_prompt = 0
        self.last_turn_completion = 0
        self._pending_tool_calls: Dict[str, Dict[str, Any]] = {}
        # Set by the runtime in Strict Coding mode: mark each successful tool
        # result as verified evidence the model may cite.
        self.verify_tools = False

    def next_seq(self) -> int:
        """Local monotonic counter used only for fallback ids."""
        self._seq += 1
        return self._seq

    def translate(self, message: Any) -> List[OnionEvent]:
        mtype = getattr(message, "type", None)
        events: List[OnionEvent] = []

        if mtype == "system":
            # Session init: the harness emits its own banner, so only record
            # model/session metadata here.
            self.context_window = int(
                getattr(message, "metadata", None).get("context_window", self.context_window)
                if isinstance(getattr(message, "metadata", None), dict)
                else self.context_window
            )
            return events

        if mtype == "assistant":
            self._turn += 1
            text = (getattr(message, "text", "") or "").strip()
            reasoning = getattr(self, "last_reasoning", "") or ""

            # Snapshot this turn's usage *before* the tool calls are emitted so
            # each call can carry its own cost without double counting.
            turn_usage = None
            usage = getattr(message, "usage", None)
            if usage is not None:
                turn_usage = {
                    "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                }
                self.last_turn_prompt = turn_usage["prompt_tokens"]
                self.last_turn_completion = turn_usage["completion_tokens"]

            if reasoning:
                events.append(
                    event(
                        EventType.REASONING,
                        self._task_id,
                        0,
                        text=sanitize(reasoning),
                        step=self._turn,
                        collapsed=True,
                    )
                )

            if text:
                # One reply may mix prose and interactive blocks. The assistant
                # event keeps the full text for history, and any <ui> blocks are
                # also emitted as structured events so the GUI renders them from
                # validated data instead of parsing markdown itself.
                events.append(
                    event(
                        EventType.ASSISTANT,
                        self._task_id,
                        0,
                        text=sanitize(text),
                        step=self._turn,
                    )
                )
                for block in self._ui_blocks(text):
                    events.append(
                        event(
                            EventType.UI_BLOCK,
                            self._task_id,
                            0,
                            step=self._turn,
                            **block,
                        )
                    )

            for call in getattr(message, "tool_calls", []) or []:
                call_id = getattr(call, "id", "") or f"call-{self._seq}-{len(events)}"
                name = getattr(call, "name", "")
                args = getattr(call, "arguments", {}) or {}
                self._pending_tool_calls[call_id] = {
                    "name": name,
                    "arguments": args,
                    "step": self._turn,
                }
                events.append(
                    event(
                        EventType.TOOL_CALL,
                        self._task_id,
                        0,
                        source=Layer.PLUGINS,
                        call_id=call_id,
                        name=name,
                        arguments=args,
                        executor=_executor_of(name),
                        status="running",
                        step=self._turn,
                        preview=_tool_preview(name, args),
                        reasoning=sanitize(reasoning),
                        usage=turn_usage,
                    )
                )

            if usage is not None:
                self._accumulate(usage)
                events.append(self._usage_event())

            if getattr(message, "is_error", False):
                events.append(
                    event(
                        EventType.ERROR,
                        self._task_id,
                        0,
                        message=sanitize(_error_text(message, "model error")),
                        code="model",
                    )
                )
            return events

        if mtype == "user":
            for result in getattr(message, "tool_results", []) or []:
                events.append(self._tool_result_event(result))
            return events

        if mtype == "result":
            usage = getattr(message, "usage", None)
            if usage is not None:
                self._accumulate(usage, replace=True)
            if getattr(message, "is_error", False):
                text = sanitize(_error_text(message, "task failed"))
                events.append(
                    event(
                        EventType.ERROR,
                        self._task_id,
                        0,
                        message=_explain_turn_limit(text, self.max_turns),
                        code="task",
                    )
                )
            return events

        return events

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _ui_blocks(text: str) -> List[Dict[str, Any]]:
        """Extract validated ``<ui>`` blocks from a reply.

        Parsing failure is not fatal: the assistant text still renders as Markdown,
        so a malformed block costs the widget, never the message.
        """
        try:
            from onion_harness.core.hybrid import render

            rendered = render(text)
        except Exception:  # noqa: BLE001 - never lose a message over markup
            return []
        return [
            segment.to_dict()
            for segment in rendered.segments
            if segment.kind in ("ui", "ui-error")
        ]

    def _tool_result_event(self, result: Any) -> OnionEvent:
        call_id = getattr(result, "tool_use_id", "") or ""
        pending = self._pending_tool_calls.pop(call_id, {}) or {}
        name = getattr(result, "name", "") or pending.get("name", "tool")
        content = sanitize(getattr(result, "content", "") or "")
        is_error = bool(getattr(result, "is_error", False))
        return event(
            EventType.TOOL_RESULT,
            self._task_id,
            0,
            source=Layer.PLUGINS,
            call_id=call_id,
            name=name,
            executor=_executor_of(name),
            content=content,
            status="failed" if is_error else "success",
            is_error=is_error,
            verified=bool(self.verify_tools and not is_error),
            summary=_summarise_output(content),
            arguments=pending.get("arguments", {}),
            step=pending.get("step", self._turn),
        )

    def _accumulate(self, usage: Any, replace: bool = False) -> None:
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        if replace:
            self.total_prompt_tokens = max(self.total_prompt_tokens, prompt)
            self.total_completion_tokens = max(self.total_completion_tokens, completion)
        else:
            self.total_prompt_tokens += prompt
            self.total_completion_tokens += completion

    def _usage_event(self) -> OnionEvent:
        total = self.total_prompt_tokens + self.total_completion_tokens
        ratio = min(1.0, total / self.context_window) if self.context_window else 0.0
        return event(
            EventType.TOKEN_USAGE,
            self._task_id,
            0,
            prompt_tokens=self.total_prompt_tokens,
            completion_tokens=self.total_completion_tokens,
            total_tokens=total,
            context_window=self.context_window,
            ratio=round(ratio, 4),
            level=_usage_level(ratio),
        )

    def context_event(self, prompt_tokens: int, summary: str = "") -> OnionEvent:
        self.context_window = self.context_window or 128_000
        ratio = min(1.0, prompt_tokens / self.context_window)
        return event(
            EventType.CONTEXT_USAGE,
            self._task_id,
            0,
            prompt_tokens=prompt_tokens,
            context_window=self.context_window,
            ratio=round(ratio, 4),
            level=_usage_level(ratio),
            summary=summary,
        )


class SessionFactory:
    """Builds neutral tool wrappers and engine options for one task."""

    def __init__(self, tool_adapter) -> None:
        self._tool_adapter = tool_adapter

    def build_tools(self, onion_tools) -> List[Any]:
        return [
            adapt_tool(t.name, t.description, t.input_schema, t.handler)
            for t in onion_tools
        ]

    def build_options(
        self,
        *,
        provider,
        tools: List[Any],
        system_prompt: str,
        allowed_tools: List[str],
        model: str,
        max_turns: int,
        temperature: Optional[float],
        top_p: Optional[float] = None,
    ):
        """Build engine options.

        ``allowed_tools`` is the exact tool-name list the harness registered.
        Passing it through means the engine's own builtins never enter the
        registry, so the model is never offered a tool the policy would deny.
        """
        from onion_harness.harness.bridge import EngineOptions, require_engine

        require_engine()
        policy = PermissionAdapter(allowed_tools)
        # ``top_p`` has no first-class engine option, so it rides the generic
        # kwargs channel the engine forwards to every provider call. This is how
        # a persona switch takes effect on the very next request.
        extra_kwargs: Dict[str, Any] = {}
        if top_p is not None:
            extra_kwargs["top_p"] = top_p
        options = EngineOptions(
            llm=provider,
            allowed_tools=sorted(set(allowed_tools)) or ["__none__"],
            system_prompt=system_prompt,
            max_turns=max_turns,
            model=model,
            temperature=temperature,
            extra_llm_kwargs=extra_kwargs,
            permission_policy=policy.engine_policy(),
        )
        return options, policy, tools


def _executor_of(tool_name: str) -> Optional[str]:
    name = (tool_name or "").strip()
    if name.startswith("run_"):
        return name[4:]
    return None


def _tool_preview(name: str, args: Dict[str, Any]) -> str:
    if isinstance(args, dict):
        code = args.get("code")
        if isinstance(code, str) and code.strip():
            flat = " ".join(code.strip().splitlines())
            return flat[:180]
        desc = args.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc.strip()[:180]
    return name


def _summarise_output(content: str, lines: int = 3, width: int = 240) -> str:
    if not content:
        return ""
    first = [ln.rstrip() for ln in content.splitlines() if ln.strip()][:lines]
    text = "\n".join(first)
    return text if len(text) <= width else text[: width - 1] + "…"


def _usage_level(ratio: float) -> str:
    if ratio >= 0.85:
        return "red"
    if ratio >= 0.6:
        return "yellow"
    return "green"


__all__ = [
    "PermissionAdapter",
    "EngineMessageTranslator",
    "SessionFactory",
    "MetaCall",
    "META_TOOL_NAMES",
]