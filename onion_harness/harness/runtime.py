"""OnionRuntime - the public façade for running an agent task.

Responsibilities, all inside the harness layer:

* lifecycle of one task run (start, stream, cancel, finish),
* translation of engine messages into :class:`OnionEvent`,
* system-prompt injection through the system channel,
* context compression via the provider decorator,
* meta-tool permission control (models may propose, never apply),
* structured error surfacing with the upstream package scrubbed.

Callers only ever see ``OnionTask``, ``OnionEvent``, ``OnionTool`` and
``OnionRuntime``.  ``OnionRuntime` deliberately exposes **no** method that can
apply a change.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from onion_harness.core.events import EventType, OnionEvent, event
from onion_harness.core.primitives import Context, Layer, Task, TaskKind, new_id
from onion_harness.core.tool import OnionTool
from onion_harness.harness.adapters import EngineMessageTranslator, SessionFactory
from onion_harness.harness.bridge import EngineLoop, require_engine
from onion_harness.harness.context import ContextCompressor, estimate_tokens
from onion_harness.harness.errors import BoundaryError, friendly_provider_error
from onion_harness.harness.memory_tools import build_memory_tools
from onion_harness.harness.meta import MetaTools
from onion_harness.harness.modes import mode_spec
from onion_harness.harness.provider import (
    CompressingProvider,
    HistoryReplayProvider,
    build_provider,
)
from onion_harness.core.persona import (
    clamp_temperature,
    clamp_top_p,
    compose_persona_overlay,
    persona_for,
)
from onion_harness.harness.presets import THINKING_BUDGET, preset_for

logger = logging.getLogger("onion.harness")

SYSTEM_PROMPT_TOOL_HINT = (
    "\n\n## Available execution surface\n"
    "You can run code through the executor tools below. Each call must include a short "
    "'description' and the 'code' to run. Prefer the smallest command that answers the "
    "question, then inspect its output before continuing.\n"
    "{tools}\n"
    "\n## Changing Onion\n"
    "Use the onion_* meta tools to propose changes to Onion itself. Proposals are never "
    "applied automatically; a human confirms them. Never state that a change is live until "
    "the user reports it as applied.\n"
)

# Only appended when memory is actually enabled. Telling a model it has a memory it
# does not have would be worse than saying nothing: it would answer "saved" and
# forget immediately. This is also the direct answer to "can you write memory?" -
# without it the agent correctly reported that it had no such capability.
MEMORY_PROMPT_HINT = (
    "\n## Long-term memory\n"
    "You do have persistent memory across conversations, and you can write to it "
    "yourself:\n"
    "- memory_remember: save a note. Use it when the user states a preference, "
    "corrects you, or tells you a lasting fact about their setup.\n"
    "- memory_search: look something up when automatic recall did not surface it.\n"
    "- memory_forget: delete a note the user says is wrong.\n"
    "Notes are stored encrypted on this machine and are visible to the user, who can "
    "edit or delete them. Write them as plain sentences. Do not save task-local "
    "details, and never claim to have remembered something you did not actually save.\n"
)


@dataclass
class ModelConfig:
    """Resolved model settings for a run."""

    provider_id: str = "deepseek"
    label: str = "DeepSeek"
    base_url: str = "https://api.deepseek.com/v1"
    wire_format: str = "openai"
    model: str = "deepseek-chat"
    api_key: str = ""
    thinking: str = "off"
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_turns: int = 40
    context_window: int = 128_000
    keep_recent: int = 10
    mode: str = "standard"
    elevation: str = "standard"
    # Which persona produced the sampling values, for the GUI chip. Purely
    # informational: the persona has already been applied by this point.
    persona_id: str = "default"

    @property
    def thinking_budget(self) -> int:
        return THINKING_BUDGET.get(self.thinking, 0)

    def to_public(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "base_url": self.base_url,
            "wire_format": self.wire_format,
            "model": self.model,
            "thinking": self.thinking,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "persona_id": self.persona_id,
            "max_turns": self.max_turns,
            "context_window": self.context_window,
            "keep_recent": self.keep_recent,
            "mode": self.mode,
            "elevation": self.elevation,
            "has_api_key": bool(self.api_key),
        }


@dataclass
class TaskSession:
    """In-memory handle for one running task."""

    task: Task
    model: ModelConfig
    cancel_event: Any = None
    events: List[OnionEvent] = field(default_factory=list)
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    error: Optional[str] = None
    meta_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    approval_pending: Dict[str, Any] = field(default_factory=dict)
    # Prior turns replayed into the model context (conversation continuity).
    history: List[Dict[str, Any]] = field(default_factory=list)


class OnionRuntime:
    """Framework façade. One instance per application, many tasks over its life."""

    def __init__(
        self,
        *,
        ledger,
        executors,
        sandbox,
        tool_adapters: Optional[List[OnionTool]] = None,
        emit: Optional[Callable[[OnionEvent], None]] = None,
        state_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        memory=None,
        personas=None,
        reviewer=None,
    ) -> None:
        self._ledger = ledger
        self._executors = executors
        self._sandbox = sandbox
        self._extra_tools = list(tool_adapters or [])
        self._emit = emit or (lambda _e: None)
        self._state_provider = state_provider or (lambda: ledger.state)
        self._sessions: Dict[str, TaskSession] = {}
        self._running: Dict[str, asyncio.Task] = {}
        self._factory = SessionFactory(self._adapt_tool)
        # Optional long-term memory port: ``recall(text)`` and
        # ``learn(events, task_id)``. Injected by the API layer so the harness
        # never touches the database file itself.
        self._memory = memory
        # Persona library (built-ins + user definition files), and the automatic
        # safety reviewer that runs before destructive tool calls reach the
        # sandbox. Both are optional so a bare runtime still works.
        self._personas = personas
        self._review_gate = reviewer
        # Set by the API layer to stream raw process output to the sidecar.
        self._output_listener = None
        # Set by the API layer to forward out-of-band approval events.
        self._approval_listener = None

    # -- introspection ------------------------------------------------------
    @property
    def ledger(self):
        return self._ledger

    def executor_descriptors(self) -> List[Dict[str, Any]]:
        return self._executors.describe()

    def available_tools(self) -> List[OnionTool]:
        return self._build_tools(Task(kind=TaskKind.AGENT))

    def session(self, task_id: str) -> Optional[TaskSession]:
        return self._sessions.get(task_id)

    def list_sessions(self) -> List[Dict[str, Any]]:
        return [
            {
                "task_id": s.task.id,
                "prompt": s.task.prompt,
                "status": s.status,
                "started_at": s.started_at,
                "finished_at": s.finished_at,
                "model": s.model.model,
                "provider": s.model.provider_id,
            }
            for s in sorted(self._sessions.values(), key=lambda s: s.started_at, reverse=True)
        ]

    # -- task lifecycle -----------------------------------------------------
    async def run_task(
        self,
        task: Task,
        model: ModelConfig,
        *,
        cancel_event=None,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncIterator[OnionEvent]:
        """Run one agent task, yielding Onion events as they happen.

        ``history`` replays a prior exchange so a follow-up prompt continues the
        same conversation instead of starting cold. Each item is
        ``{"role": "user"|"assistant", "text": str}``.

        The generator is the *only* way to observe progress.  Cancelling the
        consumer (or setting ``cancel_event``) stops the run.
        """
        session = TaskSession(task=task, model=model, cancel_event=cancel_event)
        session.history = list(history or [])
        self._sessions[task.id] = session

        yield self._emit_into(
            session,
            event(
                EventType.TASK_CREATED,
                task.id,
                source=Layer.HARNESS,
                prompt=task.prompt,
                workspace=task.workspace,
                model=model.to_public(),
            ),
        )

        try:
            async for ev in self._run_inner(session):
                yield self._emit_into(session, ev)
        except asyncio.CancelledError:
            session.status = "cancelled"
            session.finished_at = time.time()
            yield self._emit_into(
                session,
                event(EventType.TASK_STATUS, task.id, status="cancelled", error="cancelled"),
            )
            raise
        except BoundaryError as exc:
            session.status = "failed"
            session.error = exc.public_message
            session.finished_at = time.time()
            yield self._emit_into(
                session,
                event(
                    EventType.ERROR,
                    task.id,
                    message=exc.public_message,
                    code=exc.code,
                    detail=exc.detail,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - boundary
            friendly = friendly_provider_error(exc)
            session.status = "failed"
            session.error = friendly.public_message
            session.finished_at = time.time()
            logger.debug("task failed", exc_info=True)
            yield self._emit_into(
                session,
                event(
                    EventType.ERROR,
                    task.id,
                    message=friendly.public_message,
                    code=friendly.code,
                ),
            )
        finally:
            if session.finished_at is None:
                session.finished_at = time.time()
            if session.status == "running":
                session.status = "completed"
            # Learn from the finished run before announcing it, so a memory
            # learned from this task is already available to the next one.
            learned = self._learn_from(session)
            if learned:
                yield self._emit_into(
                    session,
                    event(
                        EventType.MEMORY_LEARNED,
                        task.id,
                        count=len(learned),
                        kinds=sorted({memory.kind.value for memory in learned}),
                    ),
                )
            yield self._emit_into(
                session,
                event(
                    EventType.TASK_DONE,
                    task.id,
                    status=session.status,
                    error=session.error,
                    duration_ms=int((session.finished_at - session.started_at) * 1000),
                    usage=session.usage,
                ),
            )

    def _recall_for(self, session: TaskSession) -> List[Any]:
        """Memories relevant to this task's prompt. Never raises."""
        if self._memory is None:
            return []
        try:
            return list(self._memory.recall(session.task.prompt))
        except Exception:  # noqa: BLE001 - memory must never fail a run
            logger.debug("memory recall failed", exc_info=True)
            return []

    def _resolve_persona(self, persona_id: Optional[str]):
        """Look up the active persona, honouring definitions dropped on disk."""
        try:
            if self._personas is not None:
                return self._personas.get(persona_id)
        except Exception:  # noqa: BLE001
            logger.debug("persona lookup failed", exc_info=True)
        return persona_for(persona_id)

    def _learn_from(self, session: TaskSession) -> List[Any]:
        """Persist what this run taught us. Never raises."""
        if self._memory is None:
            return []
        events = [item.to_dict() for item in session.events]
        # The runtime starts at TASK_CREATED; the user's own words live on the
        # task, and they are the richest source of preferences and facts. Add
        # them as the input event the extractor expects.
        if not any(item.get("type") == "user-input" for item in events):
            events.insert(0, {"type": "user-input", "payload": {"text": session.task.prompt}})
        try:
            return list(self._memory.learn(events, task_id=session.task.id))
        except Exception:  # noqa: BLE001 - memory must never fail a run
            logger.debug("memory learning failed", exc_info=True)
            return []

    async def _run_inner(self, session: TaskSession) -> AsyncIterator[OnionEvent]:
        require_engine()
        task = session.task
        model = session.model

        if not model.api_key:
            raise BoundaryError(
                "No API key configured for this provider. Add one in Settings.",
                code="auth",
            )

        state = self._state_provider() or {}
        recalled = self._recall_for(session)
        memory_block = self._memory.prompt_for(recalled) if recalled else ""
        # The persona is resolved here, not at construction, so switching it
        # takes effect on the next task without restarting the runtime.
        persona = self._resolve_persona(model.persona_id)
        system_prompt = self._compose_system_prompt(
            state, mode=model.mode, memory_block=memory_block, persona=persona
        )
        if persona is not None and persona.overlay:
            yield self._emit_into(
                session,
                event(
                    EventType.PERSONA_ACTIVE,
                    task.id,
                    persona_id=persona.id,
                    label=persona.label,
                    temperature=model.temperature,
                    top_p=model.top_p,
                ),
            )
        if memory_block:
            yield self._emit_into(
                session,
                event(
                    EventType.MEMORY_RECALLED,
                    task.id,
                    count=len(recalled),
                    memories=[memory.summary(120) for memory in recalled],
                ),
            )
        tools = self._build_tools(task, model=model, session=session)
        processor = EngineMessageTranslator(
            task.id, self._emit, context_window=model.context_window
        )
        processor.verify_tools = mode_spec(model.mode).verifies_tool_output

        raw_provider = build_provider(
            model.provider_id,
            model.base_url,
            model.api_key,
            model.model,
            wire_format=model.wire_format,
            temperature=model.temperature,
            top_p=model.top_p,
            thinking_budget=model.thinking_budget,
        )
        compressor = ContextCompressor(
            keep_recent=model.keep_recent, max_tokens=max(8_000, model.context_window // 2)
        )
        # Order: history replay sits closest to the wire, then compression, so a
        # long conversation is trimmed before the prior turns are re-inserted.
        provider = HistoryReplayProvider(
            CompressingProvider(raw_provider, compressor), session.history
        )

        options, policy, engine_tools = self._factory.build_options(
            provider=provider,
            tools=self._factory.build_tools(tools),
            system_prompt=system_prompt,
            allowed_tools=[t.name for t in tools],
            model=model.model,
            max_turns=model.max_turns,
            temperature=model.temperature,
            top_p=model.top_p,
        )

        loop = EngineLoop(options)
        # Inject tools that are not engine builtins (our executors + meta tools).
        for engine_tool in engine_tools:
            loop.registry.register(engine_tool)

        # Conversation continuity is handled at the provider layer (see
        # HistoryReplayProvider): the engine appends its system message inside
        # run(), so pre-seeding loop.context would put history ahead of it.

        cancel_event = session.cancel_event
        accumulated_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        try:
            async for message in loop.run(task.prompt):
                if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                    session.status = "cancelled"
                    break

                processor.last_reasoning = getattr(provider, "last_reasoning", "") or ""
                for ev in processor.translate(message):
                    if ev.type is EventType.TOKEN_USAGE:
                        accumulated_usage = {
                            "prompt_tokens": ev.payload.get("prompt_tokens", 0),
                            "completion_tokens": ev.payload.get("completion_tokens", 0),
                            "total_tokens": ev.payload.get("total_tokens", 0),
                        }
                        session.usage = dict(accumulated_usage)
                    yield ev

                compression = provider.last_compression
                if compression.get("compressed"):
                    yield processor.context_event(
                        int(provider.last_context_tokens * 3.6),
                        compression.get("summary", ""),
                    )
                elif provider.last_context_tokens:
                    yield processor.context_event(int(provider.last_context_tokens * 3.6))

            session.status = "cancelled" if (cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)()) else "completed"
        finally:
            await loop.aclose()
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass

    async def cancel(self, task_id: str) -> bool:
        session = self._sessions.get(task_id)
        if session is None:
            return False
        if session.cancel_event is not None:
            try:
                session.cancel_event.set()
            except Exception:  # noqa: BLE001
                pass
        running = self._running.get(task_id)
        if running is not None and not running.done():
            running.cancel()
        return True

    # -- tools --------------------------------------------------------------
    def _build_tools(
        self,
        task: Task,
        model: Optional[ModelConfig] = None,
        session: Optional[TaskSession] = None,
    ) -> List[OnionTool]:
        from onion_harness.harness.modes import mode_spec

        tools: List[OnionTool] = []
        ctx = Context.for_task(task, Layer.PLUGINS)
        if model is not None:
            ctx.extra = {
                "mode": model.mode,
                "elevation": model.elevation,
                "on_output": self._make_output_sink(session),
            }
        else:
            ctx.extra = {"on_output": None}

        for executor in self._executors.all():
            tools.append(
                executor.as_tool(self._make_executor_handler(executor, ctx, model, session))
            )
        tools.extend(self._extra_tools)
        tools.extend(MetaTools(self._ledger, task.id, emit=None).tools())
        # Memory tools let the agent save and correct notes itself instead of
        # waiting for the end-of-task extractor. Absent when memory is disabled.
        tools.extend(
            build_memory_tools(self._memory, task.id, emit=self._make_memory_emitter(session))
        )
        del mode_spec
        return tools

    def _make_output_sink(self, session: Optional[TaskSession]):
        """Live stdout/stderr for the terminal-mirror sidecar.

        The sink is synchronous because the sandbox calls it from its reading
        loop; it appends to the session buffer and forwards to the emit hook the
        API layer installed.
        """
        if session is None:
            return None

        def sink(channel: str, text: str) -> None:
            if not text:
                return
            listener = self._output_listener
            if listener is not None:
                try:
                    listener(session.task.id, channel, text)
                except Exception:  # noqa: BLE001
                    logger.debug("output listener failed", exc_info=True)

        return sink

    def _make_memory_emitter(self, session: Optional[TaskSession]):
        """Report an agent-written memory on the event stream.

        Memory writes happen inside a tool call, where the generator is blocked,
        so they go through the listener like approvals and review decisions. A
        silent write would defeat the point of the transcript: the user has to be
        able to see what the agent decided to remember.
        """
        if session is None:
            return None

        def emit(task_id: str, record: Dict[str, Any]) -> None:
            event_obj = event(
                EventType.MEMORY_LEARNED,
                task_id,
                source=Layer.HARNESS,
                count=1,
                kinds=[str(record.get("kind") or "")],
                by="agent",
                memory={"id": record.get("id"), "text": record.get("text")},
            )
            session.events.append(event_obj)
            listener = self._approval_listener
            if listener is not None:
                try:
                    listener(task_id, event_obj.to_dict())
                except Exception:  # noqa: BLE001
                    logger.debug("memory emitter failed", exc_info=True)

        return emit

    approval_pending: Dict[str, Any] = field(default_factory=dict)

    def _make_executor_handler(self, executor, ctx: Context, model: Optional["ModelConfig"] = None,
                               session: Optional[TaskSession] = None):
        async def _invoke(args: Dict[str, Any]) -> str:
            args = args or {}
            code = str(args.get("code") or "")

            # Automatic review runs first: it is a cheap gate in front of the
            # approval card, not a replacement for it. A rejected snippet never
            # reaches the user as a card, and never reaches the sandbox.
            blocked = await self._review_call(executor, code, session)
            if blocked is not None:
                return blocked

            # Approval gate (Standard mode) and the Plan-mode write refusal both key off
            # the same write detection, so the two never disagree.
            from onion_harness.harness.modes import requires_approval

            if session is not None and requires_approval(
                model.mode if model else None, code
            ):
                approved = await self._request_approval(executor, args, session)
                if not approved:
                    return (
                        "The user declined this tool call. Do not retry it; ask how "
                        "they would like to proceed instead."
                    )

            outcome = await executor.execute(args, ctx)
            return outcome.model_text()

        return _invoke

    async def _review_call(self, executor, code: str, session: Optional[TaskSession]) -> Optional[str]:
        """Run the safety reviewer. Returns a refusal message, or None to proceed.

        Emits ``review-decided`` through the approval listener, because this runs
        inside the tool call where the event generator is already blocked.
        """
        gate = self._review_gate
        if gate is None or session is None:
            return None
        try:
            result = await gate.review(code, tool_name=executor.tool_name)
        except Exception as exc:  # noqa: BLE001 - the gate must not break a run
            logger.debug("review gate crashed", exc_info=True)
            # A crash in our own gate is treated the same as an unreachable
            # reviewer: fail closed, and say so honestly.
            from onion_harness.harness.review import ReviewResult

            result = ReviewResult(
                "reject", reason=f"review crashed: {exc}", reviewer_model=""
            )

        if result.skipped:
            return None

        payload = {
            "call_id": new_id("rev"),
            "name": executor.tool_name,
            "executor": executor.name,
            "label": executor.label,
            "preview": executor.preview(code),
            **result.to_dict(),
        }
        self._record_review(session, payload)

        if result.approved:
            return None
        return result.blocked_message()

    def _record_review(self, session: TaskSession, payload: Dict[str, Any]) -> None:
        event_obj = event(
            EventType.REVIEW_DECIDED, session.task.id, source=Layer.HARNESS, **payload
        )
        session.events.append(event_obj)
        listener = self._approval_listener
        if listener is not None:
            try:
                listener(session.task.id, event_obj.to_dict())
            except Exception:  # noqa: BLE001
                logger.debug("review listener failed", exc_info=True)

    async def _request_approval(self, executor, args: Dict[str, Any], session: TaskSession) -> bool:
        """Ask the user, out of band.

        The event generator is blocked inside the tool call at this point (the
        engine is awaiting the tool), so an approval event cannot be yielded
        through it. It goes through a side-channel listener instead, which the
        API layer forwards straight onto the live event stream.
        """
        import asyncio as _asyncio

        call_id = new_id("apr")
        loop = _asyncio.get_running_loop()
        future: Any = loop.create_future()
        session.approval_pending[call_id] = future

        payload = {
            "call_id": call_id,
            # Note: no "task_id" here. event() already takes task_id as its
            # second positional parameter, so repeating it in the payload would
            # raise "got multiple values for argument 'task_id'" at call time.
            "name": executor.tool_name,
            "executor": executor.name,
            "label": executor.label,
            "preview": args.get("description") or executor.preview(str(args.get("code") or "")),
            "code": str(args.get("code") or "")[:1200],
            "status": "awaiting",
        }
        self._record_approval(session, payload)

        try:
            return await _asyncio.wait_for(future, timeout=1800)
        except _asyncio.TimeoutError:
            return False
        finally:
            session.approval_pending.pop(call_id, None)

    def _record_approval(self, session: TaskSession, payload: Dict[str, Any]) -> None:
        event_obj = event(
            EventType.TOOL_APPROVAL,
            session.task.id,
            source=Layer.HARNESS,
            **payload,
        )
        session.events.append(event_obj)
        listener = self._approval_listener
        if listener is not None:
            try:
                listener(session.task.id, event_obj.to_dict())
            except Exception:  # noqa: BLE001
                logger.debug("approval listener failed", exc_info=True)

    def resolve_approval(self, task_id: str, approval_id: str, approved: bool, note: str = "") -> bool:
        session = self._sessions.get(task_id)
        if session is None:
            return False
        future = session.approval_pending.get(approval_id)
        if future is None or future.done():
            return False

        def complete():
            if not future.done():
                future.set_result(bool(approved))

        try:
            future.get_loop().call_soon_threadsafe(complete)
        except Exception:  # noqa: BLE001
            return False

        self._record_approval(
            session,
            {
                "call_id": approval_id,
                "status": "approved" if approved else "denied",
                "note": note,
            },
        )
        return True

    def set_output_listener(self, listener) -> None:
        """Install the raw-output sink used by the terminal-mirror sidecar."""
        self._output_listener = listener

    def set_approval_listener(self, listener) -> None:
        """Install the out-of-band approval sink (the tool call blocks the stream)."""
        self._approval_listener = listener

    def set_extra_tools(self, tools) -> None:
        """Replace the MCP-provided tool set (hot reload)."""
        self._extra_tools = list(tools or [])

    def _adapt_tool(self, name, description, schema, handler):
        from onion_harness.harness.bridge import adapt_tool

        return adapt_tool(name, description, schema, handler)

    # -- prompt -------------------------------------------------------------
    def _compose_system_prompt(
        self,
        state: Dict[str, Any],
        mode: Optional[str] = None,
        memory_block: str = "",
        persona: Any = None,
    ) -> str:
        """Assemble the system channel, innermost concern first.

        Fixed order, and the order is the guarantee:

        1. core behaviour and the tool surface,
        2. persona overlay (tone and working style only),
        3. recalled memory (background, explicitly not instructions),
        4. the mode contract, always last.

        The mode contract is appended last in this one place, so neither a
        persona nor a recalled note can appear to override Plan's read-only rule
        or Strict's anti-hallucination contract.
        """
        from onion_harness.harness.modes import compose_mode_prompt
        from onion_harness.ledger.state import compose_system_prompt

        base = compose_system_prompt(state)
        lines = []
        for executor in self._executors.all():
            lines.append(f"- {executor.tool_name}: {executor.label} ({executor.language})")
        skill_lines = []
        for skill in state.get("skills", []):
            if skill.get("enabled", True):
                desc = skill.get("description") or "no description"
                skill_lines.append(f"- {skill.get('name')}: {desc}")

        hint = SYSTEM_PROMPT_TOOL_HINT.format(
            tools="\n".join(lines) or "- (no executors registered)"
        )
        if self._memory is not None:
            hint += MEMORY_PROMPT_HINT
        if skill_lines:
            hint += "\nMounted skills:\n" + "\n".join(skill_lines) + "\n"
        mcp_names = [tool.name for tool in self._extra_tools]
        if mcp_names:
            hint += "\nMCP tools available: " + ", ".join(mcp_names) + "\n"
        composed = (base + hint).strip()
        composed = compose_persona_overlay(persona, composed)
        if memory_block:
            composed = f"{composed}\n\n{memory_block}".strip()
        return compose_mode_prompt(mode, composed)

    # -- event plumbing -----------------------------------------------------
    def _emit_into(self, session: TaskSession, ev: OnionEvent) -> OnionEvent:
        """Assign a monotonic sequence number, record, and forward the event."""
        if ev.seq <= 0:
            session.seq += 1
            ev = OnionEvent(
                type=ev.type,
                task_id=ev.task_id,
                seq=session.seq,
                payload=ev.payload,
                source=ev.source,
                id=ev.id,
                timestamp=ev.timestamp,
            )
        else:
            session.seq = max(session.seq, ev.seq)
        session.events.append(ev)
        try:
            self._emit(ev)
        except Exception:  # noqa: BLE001 - a bad subscriber must not break a run
            logger.debug("event subscriber failed", exc_info=True)
        return ev

    # -- usage estimation ---------------------------------------------------
    @staticmethod
    def estimate_prompt_tokens(text: str) -> int:
        return estimate_tokens(text)


def _resolve_sampling(explicit: Any, persona_value: float, clamp) -> float:
    """An explicit per-request value wins; otherwise the persona's value.

    ``clamp`` is the range for this particular knob: temperature spans 0-2 while
    top_p spans 0-1, so sharing one clamp let top_p through at illegal values.
    """
    if explicit is None or explicit == "":
        return clamp(persona_value)
    try:
        return clamp(float(explicit))
    except (TypeError, ValueError):
        return clamp(persona_value)


def build_model_config(raw: Dict[str, Any], api_keys: Optional[Dict[str, str]] = None) -> ModelConfig:
    """Normalise a GUI payload into a :class:`ModelConfig`."""
    provider_id = str(raw.get("provider_id") or "deepseek")
    preset = preset_for(provider_id)
    wire_format = raw.get("wire_format") or (preset.format if preset else "openai")
    base_url = str(raw.get("base_url") or (preset.base_url if preset else "")).strip()
    label = str(raw.get("label") or (preset.label if preset else provider_id))
    model = str(raw.get("model") or (preset.models[0] if preset and preset.models else "")).strip()
    keys = api_keys or {}
    api_key = str(raw.get("api_key") or keys.get(provider_id) or "").strip()

    persona = persona_for(raw.get("persona_id"))
    # Sampling resolution, in strict priority order:
    #   1. a value the caller passed explicitly for this request,
    #   2. the active persona's value,
    # so switching persona never silently discards a temperature the user set on
    # the conversation. ``None`` means "not specified" throughout.
    temperature = _resolve_sampling(raw.get("temperature"), persona.temperature, clamp_temperature)
    top_p = _resolve_sampling(raw.get("top_p"), persona.top_p, clamp_top_p)
    max_turns = int(raw.get("max_turns") or 40)
    context_window = int(raw.get("context_window") or 128_000)
    keep_recent = int(raw.get("keep_recent") or 10)

    return ModelConfig(
        provider_id=provider_id,
        label=label,
        base_url=base_url,
        wire_format=wire_format,
        model=model,
        api_key=api_key,
        thinking=str(raw.get("thinking") or "off"),
        temperature=temperature,
        top_p=top_p,
        persona_id=persona.id,
        max_turns=max(1, min(max_turns, 200)),
        # The GUI allows up to 1M tokens; clamp to a sane floor and ceiling so a
        # typo cannot make the budget meaningless.
        context_window=max(4_000, min(context_window, 1_000_000)),
        keep_recent=max(2, min(keep_recent, 40)),
        mode=str(raw.get("mode") or "standard").lower(),
        elevation=str(raw.get("elevation") or "standard").lower(),
    )


__all__ = ["OnionRuntime", "ModelConfig", "TaskSession", "build_model_config"]