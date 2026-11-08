"""API-layer service: the app's composition root.

This is the *only* module that knows every layer.  It assembles the sandbox,
executor registry, ledger and runtime, and exposes a small, protocol-agnostic
surface that the HTTP transport calls.

The API layer stays thin: it validates input, converts payloads, forwards to
the harness, and mirrors ledger transitions into the event stream.  No business
logic about agents, tools or isolation lives here.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from onion_harness.api.bus import EventBus
from onion_harness.api.secrets import SecretError, cipher_from_store
from onion_harness.api.store import AppStore, default_data_dir
from onion_harness.core.events import EventType, OnionEvent, event
from onion_harness.core.persona import PersonaLibrary, all_personas
from onion_harness.core.primitives import Layer, Task, TaskKind
from onion_harness.harness.errors import sanitize
from onion_harness.harness.mcp_factory import McpStore, McpToolSpec
from onion_harness.harness.mcp_runtime import McpRuntime
from onion_harness.harness.presets import THINKING_LEVELS, all_presets
from onion_harness.harness.review import ReviewConfig, ReviewGate
from onion_harness.harness.provider import build_provider
from onion_harness.harness.runtime import ModelConfig, OnionRuntime, build_model_config
from onion_harness.ledger import ChangeLedger, ChangeRecord, LedgerError, LedgerStore
from onion_harness.memory import MemoryBridge, MemoryStore
from onion_harness.plugins import builtin_executors
from onion_harness.plugins.base import BaseExecutor
from onion_harness.plugins.cmd_executor import CmdExecutor
from onion_harness.plugins.powershell_executor import PowerShellExecutor
from onion_harness.plugins.python_executor import PythonExecutor
from onion_harness.plugins.web_search import (
    WebSearchConfig,
    WebSearchTool,
    build_web_search_tool,
)
from onion_harness.sandbox import Sandbox

logger = logging.getLogger("onion.api")


class _PendingRun:
    """Placeholder registered before a run is scheduled.

    Filled with the real future once it exists. ``cancel_task`` cancels through
    it, so a task that finished in the same tick cannot leave a zombie entry.
    """

    future: Optional[Any] = None

    def cancel(self) -> None:
        if self.future is not None:
            self.future.cancel()


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class OnionService:
    """Protocol-neutral application service."""

    def __init__(self, data_dir: Optional[str | Path] = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.store = AppStore(self.data_dir)
        self.bus = EventBus()
        # Keys live in the OS credential store; the file only lists provider ids.
        # This runs before anything reads settings so a legacy plaintext key is
        # moved out on the first start after upgrading.
        self.settings = self.store.migrate_plaintext_keys(self.store.load_settings())

        self.ledger = ChangeLedger(
            LedgerStore(self.data_dir / "ledger.sqlite3"),
            emit=self._on_ledger_event,
        )

        workspace = self.settings.get("workspace") or ""
        self.sandbox = Sandbox(workspace_root=workspace or None)
        self.executors = builtin_executors(self.sandbox)
        self._apply_executor_config()

        # Generated MCP tools: loaded from disk, hot-reloaded after a change.
        self.mcp_store = McpStore(self.data_dir)
        self.mcp = McpRuntime(self.mcp_store)
        self._mcp_tools = self.mcp.load()

        # Web search through the local search-neo service. The tool is absent when
        # search is switched off, because telling the model it can search when it
        # cannot is how you get invented citations.
        self.web_search_config = WebSearchConfig.from_public(self.settings.get("web_search"))
        self._web_search_tool = build_web_search_tool(self.web_search_config)

        # Long-term memory. The key is derived from a random master secret kept
        # in the OS credential store, so the database on disk is ciphertext even
        # if the file is copied off this machine.
        self.memory_store, self.memory = self._open_memory()

        # Personas: built-ins plus any definition files in <data_dir>/personas.
        self.personas = PersonaLibrary.load(self.data_dir / "personas")

        # Automatic safety review. Built from settings, rebuilt when they change.
        self.review_gate = ReviewGate(
            self._review_config(), provider_factory=self._build_reviewer
        )

        self.runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            tool_adapters=self._extra_tools(),
            # The generator is the single publisher: consuming the run is what
            # fans events out. Emitting from both paths would duplicate them.
            emit=None,
            state_provider=lambda: self.ledger.state,
            memory=self.memory,
            personas=self.personas,
            reviewer=self.review_gate,
        )
        self.runtime.set_output_listener(self._on_process_output)
        self.runtime.set_approval_listener(self._on_approval_event)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._active: Dict[str, asyncio.Task] = {}
        self._roundtables: Dict[str, Any] = {}
        self._lock = threading.RLock()
        # Workspace state captured before each task, so the diff view can show
        # what that task actually changed rather than the whole session.
        self._file_snapshots: Dict[str, Any] = {}

        # Transcripts written before encryption was added are sealed on first
        # start. Doing it here (rather than lazily on read) means the plaintext
        # stops existing on disk as early as possible.
        if self.store.events_encrypted:
            try:
                self.store.migrate_plaintext_events()
            except Exception:  # noqa: BLE001 - never block startup
                logger.debug("transcript migration failed", exc_info=True)

    # -- review -------------------------------------------------------------
    def _review_config(self) -> ReviewConfig:
        return ReviewConfig.from_public(self.settings.get("auto_review"))

    def _build_reviewer(self, config: ReviewConfig):
        """Build the provider the review gate talks to.

        Review uses its own cheap model, so the gate keeps working when the
        conversation model is one that cannot be reached cheaply. It reuses the
        saved key for that provider.
        """
        from onion_harness.harness.presets import preset_for

        preset = preset_for(config.provider_id)
        base_url = (self.settings.get("base_urls", {}) or {}).get(config.provider_id) or (
            preset.base_url if preset else ""
        )
        api_key = self.store.vault.get(config.provider_id) or ""
        wire_format = preset.format if preset else "openai"
        return build_provider(
            config.provider_id,
            base_url,
            api_key,
            config.model,
            wire_format=wire_format,
            temperature=0.0,
        )

    # -- tool set -----------------------------------------------------------
    def _extra_tools(self) -> List[Any]:
        """Everything beyond the executors: generated MCP tools plus web search.

        One place decides what the model is offered, so a hot reload and the
        initial construction cannot drift apart.
        """
        tools: List[Any] = list(self._mcp_tools or [])
        if self._web_search_tool is not None:
            tools.append(self._web_search_tool)
        return tools

    def _refresh_tools(self) -> None:
        self.runtime.set_extra_tools(self._extra_tools())

    def web_search_config_public(self) -> Dict[str, Any]:
        return self.web_search_config.to_public()

    # -- lifecycle ----------------------------------------------------------
    def _open_memory(self):
        """Open the encrypted memory database, degrading safely.

        If no credential store exists the database cannot be encrypted, and a
        silent plaintext fallback would be worse than no memory at all. In that
        case memory is disabled and the rest of the app works unchanged.
        """
        path = self.data_dir / "memory.sqlite3"
        try:
            cipher = cipher_from_store(self.store.vault.store)
        except SecretError as exc:
            logger.warning("memory disabled: no encryption key available (%s)", exc)
            return None, None
        try:
            store = MemoryStore(path, cipher=cipher)
        except Exception as exc:  # noqa: BLE001 - a corrupt file must not block start
            logger.error("memory disabled: could not open %s (%s)", path, exc)
            return None, None
        return store, MemoryBridge(store)

    def start(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="onion-runtime", daemon=True
        )
        self._loop_thread.start()

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def stop(self, timeout: float = 5.0) -> None:
        """Cancel in-flight runs and shut the loop down cleanly.

        Stopping the loop outright abandons coroutines that were scheduled but
        not yet started, which surfaces as "coroutine was never awaited". Waiting
        for the cancellations to land avoids that and lets tasks unwind.
        """
        if self._loop is None:
            return
        pending = [entry for entry in list(self._active.values()) if entry is not None]
        for entry in pending:
            try:
                entry.cancel()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass

        done = threading.Event()

        async def _await_all() -> None:
            # Gather on the loop itself: calling future.result() from inside the
            # loop thread would block the very callbacks the cancellation needs.
            for entry in pending:
                future = getattr(entry, "future", None)
                if future is None:
                    continue
                wrapped = asyncio.wrap_future(future)
                try:
                    await asyncio.wait_for(wrapped, timeout=0.5)
                except Exception:  # noqa: BLE001 - cancelled or failed is fine
                    pass

        def _shutdown() -> None:
            async def _runner() -> None:
                try:
                    await _await_all()
                finally:
                    done.set()
                    self._loop.stop()

            self._loop.create_task(_runner())

        try:
            self._loop.call_soon_threadsafe(_shutdown)
            done.wait(timeout)
        except Exception:  # noqa: BLE001 - loop may already be closed
            pass

        # Anything still queued in the loop's ready queue would be dropped when
        # it stops, surfacing as "coroutine was never awaited". Close those
        # explicitly so shutdown is clean under tests as well as in the app.
        try:
            self._loop.call_soon_threadsafe(self._drain_ready_callbacks)
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:  # noqa: BLE001
            pass

        with self._lock:
            self._active.clear()
        self.ledger.close()
        if self.memory_store is not None:
            try:
                self.memory_store.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _drain_ready_callbacks() -> None:
        """Close coroutines left in the event loop's ready queue."""
        import asyncio as _asyncio

        loop = _asyncio.get_event_loop()
        ready = getattr(loop, "_ready", None)
        if not ready:
            return
        for handle in list(ready):
            callback = getattr(handle, "_callback", None)
            if callback is None:
                continue
            try:
                coro = callback.__self__  # bound coroutine wrapper, if any
            except AttributeError:
                continue
            close = getattr(coro, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

    # -- executor configuration --------------------------------------------
    def _executor_classes(self) -> List[type[BaseExecutor]]:
        classes = []
        for name in self.ledger.state.get("executors", []):
            cls = {
                "python": PythonExecutor,
                "powershell": PowerShellExecutor,
                "cmd": CmdExecutor,
            }.get(name)
            if cls is not None:
                classes.append(cls)
        if not classes:
            classes = [PythonExecutor]
        return classes

    def _apply_executor_config(self) -> None:
        """Rebuild the executor registry from ledger state (post-meta-change)."""
        keep = set(self.ledger.state.get("executors", []))
        for name in list(self.executors.names()):
            if name not in keep:
                self.executors.unregister(name)
        for cls in self._executor_classes():
            if self.executors.get(cls.name) is None:
                self.executors.register(cls(self.sandbox))

    # -- workspace browsing ------------------------------------------------
    def _workspace_root(self) -> Optional[Path]:
        """The directory the browser may show, or None when unset.

        Browsing without a configured workspace would expose the process's own
        working directory, which may be the application install. Refusing is the
        safer default and the GUI explains why the panel is empty.
        """
        raw = self.sandbox.workspace_root
        if not raw:
            return None
        root = Path(raw)
        return root if root.is_dir() else None

    def workspace_tree(
        self, path: str = "", depth: int = 1, task_id: str = ""
    ) -> Dict[str, Any]:
        """Browse the workspace, marking files the given task changed."""
        from onion_harness.sandbox.workspace import WorkspaceError, diff_workspace, list_tree

        root = self._workspace_root()
        if root is None:
            return {"root": "", "entries": [], "error": "no workspace configured"}

        changed: set = set()
        snapshot = self._file_snapshots.get(task_id) if task_id else None
        if snapshot is not None:
            try:
                changed = {change.path for change in diff_workspace(root, snapshot, include_text=False)}
            except Exception:  # noqa: BLE001 - the marker is a nicety, not essential
                changed = set()

        try:
            entries = list_tree(root, path, depth=depth, changed=changed)
        except WorkspaceError as exc:
            raise ApiError(str(exc), status=400) from exc
        return {
            "root": str(root),
            "path": path,
            "entries": [entry.to_dict() for entry in entries],
        }

    def workspace_file(self, path: str) -> Dict[str, Any]:
        from onion_harness.sandbox.workspace import read_file

        root = self._workspace_root()
        if root is None:
            raise ApiError("no workspace configured", status=409)
        view = read_file(root, path)
        if view.error and not view.text:
            # A missing or unreadable file is a 404; a binary file is still a 200
            # with a reason, because the request was valid.
            status = 404 if "not found" in view.error else 400
            raise ApiError(view.error, status=status)
        return view.to_dict()

    def task_file_changes(self, task_id: str) -> Dict[str, Any]:
        """What this task changed on disk, with before/after bodies."""
        from onion_harness.sandbox.workspace import diff_workspace

        snapshot = self._file_snapshots.get(task_id)
        if snapshot is None:
            return {"task_id": task_id, "changes": [], "note": "no snapshot for this task"}
        root = self._workspace_root()
        if root is None:
            return {"task_id": task_id, "changes": [], "note": "no workspace configured"}
        changes = diff_workspace(root, snapshot)
        return {"task_id": task_id, "changes": [change.to_dict() for change in changes]}

    def _take_snapshot(self, task_id: str) -> None:
        from onion_harness.sandbox.workspace import FileSnapshot

        root = self._workspace_root()
        if root is None:
            return
        try:
            self._file_snapshots[task_id] = FileSnapshot.take(root)
        except Exception:  # noqa: BLE001 - a diff view must not block a task
            logger.debug("could not snapshot the workspace", exc_info=True)

    def _publish_file_changes(self, task_id: str) -> None:
        """Emit a card for each file this task changed.

        Emitted as ordinary events so the change appears inline in the transcript,
        next to the message that caused it, and survives a reload.
        """
        from onion_harness.sandbox.workspace import diff_workspace

        snapshot = self._file_snapshots.get(task_id)
        root = self._workspace_root()
        if snapshot is None or root is None:
            return
        try:
            changes = diff_workspace(root, snapshot, include_text=False)
        except Exception:  # noqa: BLE001
            logger.debug("could not diff the workspace", exc_info=True)
            return
        if not changes:
            return
        summary = event(
            EventType.FILE_CHANGED,
            task_id,
            source=Layer.API,
            count=len(changes),
            files=[change.summary() for change in changes],
        )
        self.bus.publish(summary.to_dict())
        self.store.append_event(task_id, summary.to_dict())

    # -- object graph for the GUI ------------------------------------------
    def bootstrap(self) -> Dict[str, Any]:
        providers = all_presets(self.ledger.state.get("model_providers", []))
        return {
            "settings": self.store.public_settings(self.settings),
            "providers": providers,
            "thinking_levels": THINKING_LEVELS,
            "executors": self.executors.describe(),
            "panels": self.ledger.state.get("ui_panels", []),
            "prompt_segments": [
                {
                    "id": s["id"],
                    "title": s.get("title", s["id"]),
                    "enabled": s.get("enabled", True),
                    "chars": len(s.get("content", "")),
                }
                for s in self.ledger.state.get("system_prompt_segments", [])
            ],
            "skills": self.ledger.state.get("skills", []),
            "modes": self.modes()["modes"],
            "elevation_levels": self.modes()["elevation_levels"],
            "thinking_slider": self.modes()["thinking_slider"],
            "mcp_tools": self.mcp_tools(),
            "memory": self.memory_stats(),
            "personas": self.personas.to_public(),
            "persona_errors": list(self.personas.errors),
            "active_persona": self.personas.get(
                self.settings.get("active_persona")
            ).to_dict(),
            "auto_review": self._review_config().to_public(),
            "web_search": self.web_search_config.to_public(),
            "tasks": self.store.load_tasks(),
            "archived": self.archived_tasks(),
            "history": [r.to_dict() for r in self.ledger.list_history()],
            "proposals": [p.to_dict() for p in self.ledger.list_proposals()],
            "version": self._latest_version(),
            "data_dir": str(self.data_dir),
        }

    def _latest_version(self) -> int:
        history = self.ledger.list_history()
        return history[-1].version if history else 0

    # -- memory -------------------------------------------------------------
    def memory_stats(self) -> Dict[str, Any]:
        if self.memory is None:
            return {"enabled": False, "total": 0, "by_kind": {}}
        stats = dict(self.memory.stats())
        stats["enabled"] = True
        return stats

    def memory_list(self, limit: int = 100) -> List[Dict[str, Any]]:
        """What the system remembers, for the user to read and prune."""
        return self.memory.recent(limit) if self.memory is not None else []

    def memory_forget(self, memory_id: str) -> Dict[str, Any]:
        if self.memory is None:
            raise ApiError("long-term memory is disabled", status=409)
        removed = self.memory.forget(str(memory_id))
        if not removed:
            raise ApiError("memory not found", status=404)
        return {"forgotten": memory_id}

    def memory_add(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a note by hand, so the store is not only machine-written."""
        if self.memory is None:
            raise ApiError("long-term memory is disabled", status=409)
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ApiError("text is required")
        try:
            record = self.memory.add(
                text,
                kind=str(payload.get("kind") or "fact"),
                weight=payload.get("weight"),
            )
        except Exception as exc:  # noqa: BLE001 - MemoryStoreError and friends
            raise ApiError(str(exc)) from exc
        return record

    def memory_update(self, memory_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Edit a note's text, kind or weight. Re-indexed so search follows."""
        if self.memory is None:
            raise ApiError("long-term memory is disabled", status=409)
        fields: Dict[str, Any] = {}
        if "text" in payload:
            fields["text"] = str(payload.get("text") or "")
        if "kind" in payload:
            fields["kind"] = str(payload.get("kind") or "")
        if "weight" in payload and payload.get("weight") is not None:
            fields["weight"] = payload.get("weight")
        if not fields:
            raise ApiError("nothing to update")
        try:
            record = self.memory.update(str(memory_id), **fields)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(str(exc)) from exc
        if record is None:
            raise ApiError("memory not found", status=404)
        return record

    def memory_purge(self) -> Dict[str, Any]:
        if self.memory is None:
            raise ApiError("long-term memory is disabled", status=409)
        return {"purged": self.memory.purge()}

    # -- hybrid UI actions --------------------------------------------------
    def ui_action(self, task_id: str, action_id: str) -> Dict[str, Any]:
        """Feed a click on a rendered ``<ui>`` button back to the agent.

        The click becomes an ordinary user turn. That is deliberate: routing it
        through the normal path means the resulting work still goes through
        approval, the safety reviewer and the mode gate. A dedicated action channel
        would have been a side door around all three.
        """
        action = str(action_id or "").strip()
        if not action:
            raise ApiError("action_id is required")
        if not self._valid_action_id(action):
            raise ApiError("invalid action_id")
        return self.continue_task(task_id, f"[ui-action] {action}")

    @staticmethod
    def _valid_action_id(action: str) -> bool:
        # Same shape the parser accepts, so an action can never be a payload.
        import re

        return bool(re.match(r"^[A-Za-z0-9_.:-]{1,120}$", action))

    # -- settings -----------------------------------------------------------
    def get_settings(self) -> Dict[str, Any]:
        return self.store.public_settings(self.settings)

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "theme_mode",
            "provider_id",
            "model",
            "thinking",
            "workspace",
            "max_turns",
            "keep_recent_messages",
            "context_window",
            "mode",
            "elevation",
            "active_persona",
            # Kept so a per-conversation override survives a reload.
            "temperature",
            "top_p",
        }
        review_changed = False
        for key, value in (patch or {}).items():
            if key == "api_key" and patch.get("provider_id"):
                self.settings = self.store.set_provider_key(
                    self.settings, str(patch["provider_id"]), str(value)
                )
            elif key == "base_url" and patch.get("provider_id"):
                urls = dict(self.settings.get("base_urls", {}))
                override = str(value or "").strip()
                if override:
                    urls[str(patch["provider_id"])] = override
                else:
                    urls.pop(str(patch["provider_id"]), None)
                self.settings["base_urls"] = urls
            elif key == "api_keys" and isinstance(value, dict):
                for provider_id, api_key in value.items():
                    self.settings = self.store.set_provider_key(
                        self.settings, str(provider_id), str(api_key)
                    )
            elif key == "auto_review" and isinstance(value, dict):
                merged = {**self._review_config().to_public(), **value}
                self.settings["auto_review"] = ReviewConfig.from_public(merged).to_public()
                review_changed = True
            elif key == "web_search" and isinstance(value, dict):
                merged = {**self.web_search_config.to_public(), **value}
                self.web_search_config = WebSearchConfig.from_public(merged)
                self.settings["web_search"] = self.web_search_config.to_public()
                # Hot-apply: rebuild the tool so enabling search or moving the
                # endpoint takes effect without a restart. Disabling it removes the
                # tool entirely, and the prompt hint goes with it.
                self._web_search_tool = build_web_search_tool(self.web_search_config)
                self._refresh_tools()
            elif key == "active_persona":
                persona = self.personas.get(str(value))
                if persona.id != str(value or "").strip().lower():
                    raise ApiError(f"unknown persona: {value}", status=400)
                self.settings["active_persona"] = persona.id
            elif key in ("temperature", "top_p"):
                parsed = _clamp_sampling(key, value)
                if parsed is not _INVALID:
                    self.settings[key] = parsed
            elif key in allowed:
                clamped = _clamp_setting(key, value)
                if clamped is not _INVALID:
                    self.settings[key] = clamped

        if "workspace" in patch:
            self.sandbox = Sandbox(workspace_root=patch["workspace"] or None)
            self.executors = builtin_executors(self.sandbox)
            self._apply_executor_config()
            self.runtime._sandbox = self.sandbox
            self.runtime._executors = self.executors

        if review_changed:
            # Hot-apply: the gate caches its provider, which may now point at a
            # different model, so the config is pushed in rather than rebuilt.
            self.review_gate.update_config(self._review_config())

        self.settings = self.store.save_settings(self.settings)
        return self.store.public_settings(self.settings)

    def clear_api_key(self, provider_id: str) -> Dict[str, Any]:
        self.settings = self.store.clear_provider_key(self.settings, str(provider_id))
        self.settings = self.store.save_settings(self.settings)
        return self.store.public_settings(self.settings)

    def _keys(self) -> Dict[str, str]:
        """Live key values from the credential store, for provider construction."""
        return self.store.provider_keys(self.settings)

    # -- tasks --------------------------------------------------------------
    def create_roundtable(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Validate a roundtable config, create its task, and start the meeting."""
        from onion_harness.core.roundtable import (
            RoundtableConfig,
            RoundtableError,
            SeatSpec,
            clamp_rounds,
            validate_config,
        )
        from onion_harness.harness.presets import preset_for
        from onion_harness.harness.roundtable import RoundtableOrchestrator

        topic = str(payload.get("topic") or "").strip()
        raw_seats = payload.get("seats") or []
        if not isinstance(raw_seats, list):
            raise ApiError("seats must be a list")

        keys = self._keys()
        seats = []
        for index, raw in enumerate(raw_seats):
            if not isinstance(raw, dict):
                continue
            # A seat may use a known preset or a fully custom endpoint. Custom
            # means the caller supplies base_url + model (+ its own key), which
            # is how "Others" works in the dialog.
            provider_id = str(raw.get("provider_id") or self.settings.get("provider_id") or "")
            preset = preset_for(provider_id)
            custom = bool(raw.get("custom")) or provider_id == "custom"
            model = str(
                raw.get("model")
                or ("" if custom else (preset.models[0] if preset and preset.models else ""))
            ).strip()
            base_url = str(
                raw.get("base_url") or ("" if custom else (preset.base_url if preset else ""))
            ).strip()
            wire_format = str(
                raw.get("wire_format") or (preset.format if preset else "openai")
            )
            label = str(
                raw.get("label")
                or ("Custom" if custom else (preset.label if preset else provider_id))
            )
            # A custom seat may bring an inline key; otherwise fall back to the
            # saved key for its provider id (empty for custom endpoints).
            api_key = str(raw.get("api_key") or keys.get(provider_id) or "").strip()
            seats.append(
                SeatSpec(
                    id=str(raw.get("id") or f"seat{index + 1}"),
                    name=str(raw.get("name") or f"Seat {index + 1}"),
                    role=str(raw.get("role") or "member"),
                    provider_id=provider_id,
                    label=label,
                    base_url=base_url,
                    wire_format=wire_format,
                    model=model,
                    api_key=api_key,
                    thinking=str(raw.get("thinking") or payload.get("thinking") or "medium"),
                    custom=custom,
                )
            )

        host_id = str(payload.get("host_id") or (seats[0].id if seats else ""))
        config = RoundtableConfig(
            topic=topic,
            seats=seats,
            host_id=host_id,
            max_rounds=clamp_rounds(payload.get("max_rounds")),
            dissent=str(payload.get("dissent") or "medium"),
            thinking=str(payload.get("thinking") or "medium"),
        )
        try:
            validate_config(config)
        except RoundtableError as exc:
            # Missing keys are the common case and deserve a 401 so the GUI can
            # route the user straight to Advanced settings.
            message = str(exc)
            status = 401 if "API key" in message else 400
            raise ApiError(message, status=status) from exc

        task = Task(kind=TaskKind.AGENT, prompt=topic)
        record = {
            "id": task.id,
            "title": f"Roundtable · {_title_from_prompt(topic)}",
            "prompt": topic,
            "workspace": "",
            "provider": seats[host_id_to_index(seats, host_id)].provider_id if seats else "",
            "model": seats[host_id_to_index(seats, host_id)].model if seats else "",
            "mode": "roundtable",
            "kind": "roundtable",
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "archived": False,
            "usage": {},
            "roundtable": config.to_public(),
        }
        self.store.upsert_task(record)
        orchestrator = RoundtableOrchestrator(task.id, config, keys)
        self._roundtables[task.id] = orchestrator
        self._start_roundtable(task.id, orchestrator)
        return record

    def roundtable_roster(self) -> List[Dict[str, Any]]:
        """Providers the roundtable dialog can offer, with key availability.

        The built-in "custom" entry is kept and marked so the dialog can render
        it as an "Others" option that reveals base-URL and model fields.
        """
        presets = all_presets(self.ledger.state.get("model_providers", []))
        configured = set(self.store.public_settings(self.settings)["configured_providers"])
        out = []
        for preset in presets:
            item = dict(preset)
            item["has_api_key"] = preset["id"] in configured
            item["custom"] = preset["id"] == "custom"
            out.append(item)
        return out

    # -- live roundtable interaction ---------------------------------------
    def roundtable_message(self, task_id: str, text: str, to_seat: str = "all") -> Dict[str, Any]:
        """Speak into a running debate, addressed to one seat or to everyone."""
        orchestrator = self._roundtables.get(task_id)
        if orchestrator is None:
            raise ApiError("this task is not a roundtable", status=404)
        try:
            record = orchestrator.receive_human_message(text, to_seat or "all")
        except ValueError as exc:
            raise ApiError(str(exc)) from exc

        # While the meeting runs, the loop drains the queue at round
        # boundaries; publish now too so the hall updates without waiting.
        for pending in orchestrator.drain_notices():
            self.bus.publish(pending.to_dict())
            self.store.append_event(task_id, pending.to_dict())

        # After the meeting closed there is no loop left to answer, so the reply
        # is produced here. Without this a message sent post-meeting was recorded
        # but never answered.
        if orchestrator.finished:
            self._run_follow_up(task_id, orchestrator, text, to_seat or "all")
        return {"task_id": task_id, "message": record}

    def _run_follow_up(self, task_id: str, orchestrator, text: str, to_seat: str) -> None:
        assert self._loop is not None, "service not started"

        async def _consume() -> None:
            try:
                async for onion_event in orchestrator.follow_up(text, to_seat):
                    self.bus.publish(onion_event.to_dict())
                    self.store.append_event(task_id, onion_event.to_dict())
            except Exception as exc:  # noqa: BLE001
                logger.exception("roundtable follow-up failed")
                payload = event(
                    EventType.ERROR, task_id, message=sanitize(str(exc)), code="roundtable"
                ).to_dict()
                self.bus.publish(payload)
                self.store.append_event(task_id, payload)
            finally:
                with self._lock:
                    self._active.pop(task_id, None)

        # Register before scheduling: a task that finishes in the same tick would
        # otherwise pop first, and the later assignment would leave a zombie
        # entry that never gets cleaned up.
        placeholder = _PendingRun()
        with self._lock:
            self._active[task_id] = placeholder
        coro = _consume()
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            placeholder.future = future
        except Exception:
            coro.close()
            with self._lock:
                self._active.pop(task_id, None)
            raise

    def roundtable_add_seat(self, task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Add a debater mid-meeting, up to the same six-seat ceiling."""
        from onion_harness.core.roundtable import MAX_SEATS, SeatSpec
        from onion_harness.harness.presets import preset_for

        orchestrator = self._roundtables.get(task_id)
        if orchestrator is None:
            raise ApiError("this task is not a roundtable", status=404)
        if len(orchestrator.config.seats) >= MAX_SEATS:
            raise ApiError(f"a roundtable can hold at most {MAX_SEATS} seats", status=400)

        keys = self._keys()
        provider_id = str(payload.get("provider_id") or self.settings.get("provider_id") or "")
        preset = preset_for(provider_id)
        custom = bool(payload.get("custom")) or provider_id == "custom"
        api_key = str(payload.get("api_key") or keys.get(provider_id) or "").strip()
        if not api_key:
            raise ApiError(f"No API key saved for {preset.label if preset else provider_id}.", status=401)

        index = len(orchestrator.config.seats)
        seat = SeatSpec(
            id=str(payload.get("id") or f"seat{index + 1}"),
            name=str(payload.get("name") or f"Seat {index + 1}"),
            provider_id=provider_id,
            label=str(payload.get("label") or (preset.label if preset else provider_id)),
            base_url=str(payload.get("base_url") or (preset.base_url if preset else "")),
            wire_format=str(payload.get("wire_format") or (preset.format if preset else "openai")),
            model=str(
                payload.get("model")
                or ("" if custom else (preset.models[0] if preset and preset.models else ""))
            ),
            api_key=api_key,
            thinking=str(payload.get("thinking") or "medium"),
        )
        if not orchestrator.add_seat(seat):
            raise ApiError("could not add that seat (duplicate id?)", status=400)

        for pending in orchestrator.drain_notices():
            self.bus.publish(pending.to_dict())
            self.store.append_event(task_id, pending.to_dict())

        # Reflect the new roster on the task record so a reload shows it.
        tasks = self.store.load_tasks()
        for task in tasks:
            if task.get("id") == task_id and isinstance(task.get("roundtable"), dict):
                task["roundtable"]["seats"] = [s.to_public() for s in orchestrator.config.seats]
                task["updated_at"] = time.time()
        self.store.save_tasks(tasks)
        return {"task_id": task_id, "seat": seat.to_public()}

    def roundtable_seat_thoughts(self, task_id: str) -> Dict[str, Any]:
        """Per-seat chain of thought, folded from the persisted speech events."""
        events = self.task_events(task_id)
        seats: Dict[str, Dict[str, Any]] = {}
        for item in events:
            if item.get("type") == "roundtable-start":
                for seat in item["payload"].get("seats", []):
                    seats[seat["id"]] = {
                        "seat": seat,
                        "thoughts": [],
                        "messages": [],
                    }
            elif item.get("type") == "roundtable-speech":
                payload = item["payload"]
                entry = seats.setdefault(
                    payload["seat_id"],
                    {"seat": {"id": payload["seat_id"], "name": payload["name"]}, "thoughts": [], "messages": []},
                )
                entry["thoughts"].append(payload)
            elif item.get("type") == "roundtable-message":
                payload = item["payload"]
                target = payload.get("to") or "all"
                if target == "all":
                    for entry in seats.values():
                        entry["messages"].append(payload)
                else:
                    entry = seats.get(target)
                    if entry is not None:
                        entry["messages"].append(payload)
        return {"seats": list(seats.values())}

    def create_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ApiError("prompt is required")

        workspace = str(payload.get("workspace") or self.settings.get("workspace") or "").strip()
        if workspace:
            path = Path(workspace).expanduser()
            if not path.exists() or not path.is_dir():
                raise ApiError(f"workspace does not exist: {workspace}")

        model_raw = payload.get("model") or {
            "provider_id": self.settings.get("provider_id"),
            "model": self.settings.get("model"),
            "thinking": self.settings.get("thinking"),
            "mode": self.settings.get("mode"),
            "elevation": self.settings.get("elevation"),
        }
        model_raw = dict(model_raw)
        model_raw.setdefault("mode", self.settings.get("mode", "standard"))
        model_raw.setdefault("elevation", self.settings.get("elevation", "standard"))
        # Context controls come from settings unless the caller overrides them.
        model_raw.setdefault("context_window", self.settings.get("context_window", 128_000))
        model_raw.setdefault("keep_recent", self.settings.get("keep_recent_messages", 10))
        # Persona and its sampling. An explicit value on the request (the GUI
        # sends one only when the user changed it) wins; otherwise the persona's.
        model_raw.setdefault("persona_id", self.settings.get("active_persona", "default"))
        if model_raw.get("temperature") is None:
            model_raw["temperature"] = self.settings.get("temperature")
        if model_raw.get("top_p") is None:
            model_raw["top_p"] = self.settings.get("top_p")
        # A base URL typed in Advanced settings must reach the provider. The
        # preset only supplies a default, so without this an override was
        # silently ignored and every request went to the wrong host.
        overrides = self.settings.get("base_urls", {}) or {}
        saved_url = overrides.get(str(model_raw.get("provider_id") or ""))
        if saved_url and not model_raw.get("base_url"):
            model_raw["base_url"] = saved_url
        model = build_model_config(model_raw, self._keys())
        if not model.model:
            raise ApiError("Select a model before sending a task.")
        if not model.api_key:
            raise ApiError(
                f"No API key saved for {model.label}. Add one in Settings.", status=401
            )

        task = Task(
            kind=TaskKind.AGENT,
            prompt=prompt,
            workspace=workspace or None,
            timeout_seconds=float(payload.get("timeout_seconds") or 900),
            elevate=bool(payload.get("elevate", False)),
        )

        record = {
            "id": task.id,
            "title": _title_from_prompt(prompt),
            "prompt": prompt,
            "workspace": workspace,
            "provider": model.provider_id,
            "model": model.model,
            "mode": model.mode,
            # Recorded so a follow-up keeps the persona and sampling it started
            # with, and so the GUI can show what actually ran.
            "persona_id": model.persona_id,
            "temperature": model.temperature,
            "top_p": model.top_p,
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "archived": False,
            "turn_count": 1,
            "context_window": model.context_window,
            "keep_recent": model.keep_recent,
            "usage": {},
        }
        self.store.upsert_task(record)
        # Persist the user's own prompt. Without this the event stream has no
        # record of what was asked, so rebuilding history for a follow-up would
        # lose every user turn.
        #
        # This is the single source of truth for the user bubble: the event is
        # returned to the caller, which renders it directly. The GUI must not
        # also synthesise one, or the same message appears twice.
        user_event = event(
            EventType.USER_INPUT, task.id, source=Layer.API, text=prompt
        )
        self.bus.publish(user_event.to_dict())
        self.store.append_event(task.id, user_event.to_dict())
        record["user_event"] = user_event.to_dict()
        self._take_snapshot(task.id)
        self._start_run(task, model)
        return record

    def _start_run(
        self, task: Task, model: ModelConfig, history: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        assert self._loop is not None, "service not started"
        cancel_event = asyncio.Event()

        async def _consume() -> None:
            status = "completed"
            try:
                async for onion_event in self.runtime.run_task(
                    task, model, cancel_event=cancel_event, history=history
                ):
                    self.bus.publish(onion_event.to_dict())
                    self.store.append_event(task.id, onion_event.to_dict())
                    if onion_event.type is EventType.TASK_STATUS:
                        status = onion_event.payload.get("status", status)
                    elif onion_event.type is EventType.TASK_DONE:
                        status = onion_event.payload.get("status", status)
                        # Published before task-done closes the UI's run state, so
                        # the change cards appear in the transcript immediately.
                        self._publish_file_changes(task.id)
                        self._finish_task(task.id, status, onion_event.payload.get("usage", {}))
            except asyncio.CancelledError:
                status = "cancelled"
                self._finish_task(task.id, status, {})
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("task crashed")
                status = "failed"
                payload = event(
                    EventType.ERROR,
                    task.id,
                    message=sanitize(str(exc)),
                    code="runtime",
                ).to_dict()
                self.bus.publish(payload)
                self.store.append_event(task.id, payload)
                self._finish_task(task.id, status, {})
            finally:
                with self._lock:
                    self._active.pop(task.id, None)

        async def _runner() -> None:
            await _consume()

        placeholder = _PendingRun()
        with self._lock:
            self._active[task.id] = placeholder
        coro = _runner()
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            placeholder.future = future
        except Exception:
            # If scheduling failed the coroutine was never started; close it so
            # it does not linger as an un-awaited object.
            coro.close()
            with self._lock:
                self._active.pop(task.id, None)
            raise

    def _start_roundtable(self, task_id: str, orchestrator) -> None:
        """Run a roundtable meeting on the service loop.

        Deliberately separate from ``_start_run``: the orchestrator owns its own
        loop and emits the roundtable event family, so the ReAct path is not
        involved at all.
        """
        assert self._loop is not None, "service not started"

        async def _consume() -> None:
            status = "completed"
            usage: Dict[str, Any] = {}
            try:
                async for onion_event in orchestrator.run():
                    is_roundtable = onion_event.type.value.startswith("roundtable-")
                    # Roundtable events go to the bus immediately (the 2D view is
                    # live) and are persisted, but the single-agent transcript
                    # does not receive them.
                    self.bus.publish(onion_event.to_dict())
                    self.store.append_event(task_id, onion_event.to_dict())
                    if onion_event.type is EventType.ROUNDTABLE_END:
                        usage = {
                            "prompt_tokens": onion_event.payload.get("total_tokens", 0),
                            "completion_tokens": 0,
                            "total_tokens": onion_event.payload.get("total_tokens", 0),
                        }
                        if onion_event.payload.get("requires_human"):
                            status = "needs-human"
                        else:
                            status = "completed"
                    del is_roundtable
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("roundtable crashed")
                status = "failed"
                payload = event(
                    EventType.ERROR,
                    task_id,
                    message=sanitize(str(exc)),
                    code="roundtable",
                ).to_dict()
                self.bus.publish(payload)
                self.store.append_event(task_id, payload)
            finally:
                self._finish_task(task_id, status, usage)
                self.store.append_event(
                    task_id,
                    event(
                        EventType.TASK_DONE,
                        task_id,
                        status=status,
                        usage=usage,
                        kind="roundtable",
                    ).to_dict(),
                )
                with self._lock:
                    self._active.pop(task_id, None)

        # Register before scheduling: a task that finishes in the same tick would
        # otherwise pop first, and the later assignment would leave a zombie
        # entry that never gets cleaned up.
        placeholder = _PendingRun()
        with self._lock:
            self._active[task_id] = placeholder
        coro = _consume()
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            placeholder.future = future
        except Exception:
            coro.close()
            with self._lock:
                self._active.pop(task_id, None)
            raise

    def _finish_task(self, task_id: str, status: str, usage: Dict[str, Any]) -> None:
        tasks = self.store.load_tasks()
        for task in tasks:
            if task.get("id") == task_id:
                task["status"] = status
                task["updated_at"] = time.time()
                if usage:
                    task["usage"] = usage
                break
        self.store.save_tasks(tasks)

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            future = self._active.get(task_id)
        if future is None:
            raise ApiError("task is not running", status=404)
        future.cancel()
        return {"task_id": task_id, "status": "cancelling"}

    def task_events(self, task_id: str) -> List[Dict[str, Any]]:
        live = self.bus.replay_for(task_id)
        stored = self.store.load_events(task_id)
        seen = {e.get("id") for e in stored}
        merged = stored + [e for e in live if e.get("id") not in seen]
        merged.sort(key=lambda e: (e.get("timestamp", 0), e.get("seq", 0)))
        return merged

    def delete_task(self, task_id: str) -> Dict[str, Any]:
        self.store.delete_task(task_id)
        self.bus.reset_task(task_id)
        return {"task_id": task_id, "deleted": True}

    # -- conversation continuity --------------------------------------------
    def task_history(self, task_id: str, limit: int = 40) -> List[Dict[str, str]]:
        """Rebuild the conversation so far from the persisted event stream.

        Only user prompts and final assistant replies are replayed. Tool traffic
        is deliberately excluded: it would inject stale tool-call ids that the
        new request never made, and providers reject those.
        """
        history: List[Dict[str, str]] = []
        for item in self.task_events(task_id):
            kind = item.get("type")
            payload = item.get("payload") or {}
            if kind == "user-input":
                text = str(payload.get("text") or "").strip()
                if text:
                    history.append({"role": "user", "text": text})
            elif kind == "assistant":
                text = str(payload.get("text") or "").strip()
                if text:
                    history.append({"role": "assistant", "text": text})
        return history[-limit:]

    def continue_task(
        self,
        task_id: str,
        prompt: str,
        model_payload: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send another prompt into an existing conversation.

        The task id is reused, its prior turns are replayed so the model keeps
        the thread, and new events append to the same transcript.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise ApiError("prompt is required")

        tasks = self.store.load_tasks()
        original = next((task for task in tasks if task.get("id") == task_id), None)
        if original is None:
            raise ApiError("task not found", status=404)
        if self._active.get(task_id) is not None:
            raise ApiError("this task is still running", status=409)

        # A follow-up continues the *same* conversation, so it inherits that
        # conversation's context settings unless the caller overrides them.
        # Falling back to bare defaults here silently reset a 1M window to 128k.
        model_payload = dict(model_payload or {})
        model_payload.setdefault(
            "provider_id", original.get("provider") or self.settings.get("provider_id")
        )
        model_payload.setdefault("model", original.get("model") or self.settings.get("model"))
        model_payload.setdefault("thinking", self.settings.get("thinking"))
        model_payload.setdefault("mode", self.settings.get("mode"))
        model_payload.setdefault("elevation", self.settings.get("elevation"))
        model_payload.setdefault(
            "context_window",
            original.get("context_window") or self.settings.get("context_window") or 128_000,
        )
        model_payload.setdefault(
            "keep_recent",
            original.get("keep_recent") or self.settings.get("keep_recent_messages") or 10,
        )
        # A follow-up keeps the persona it started with, so switching the global
        # persona mid-conversation does not silently change an existing thread's
        # tone or sampling.
        model_payload.setdefault(
            "persona_id", original.get("persona_id") or self.settings.get("active_persona", "default")
        )
        if model_payload.get("temperature") is None:
            model_payload["temperature"] = original.get("temperature")
        if model_payload.get("top_p") is None:
            model_payload["top_p"] = original.get("top_p")
        if original.get("max_turns"):
            model_payload.setdefault("max_turns", original["max_turns"])
        overrides = self.settings.get("base_urls", {}) or {}
        saved_url = overrides.get(str(model_payload.get("provider_id") or ""))
        if saved_url and not model_payload.get("base_url"):
            model_payload["base_url"] = saved_url

        # Explicit per-message overrides still win.
        for key in ("context_window", "keep_recent", "max_turns"):
            if context and context.get(key) is not None:
                model_payload[key] = context[key]

        model = build_model_config(model_payload, self._keys())
        if not model.api_key:
            raise ApiError(f"No API key saved for {model.label}. Add one in Settings.", status=401)

        history = self.task_history(task_id)
        task = Task(
            id=task_id,
            kind=TaskKind.AGENT,
            prompt=prompt,
            workspace=str(original.get("workspace") or "") or None,
            timeout_seconds=float(original.get("timeout_seconds") or 900),
        )

        original.update(
            {
                "status": "queued",
                "updated_at": time.time(),
                "turn_count": int(original.get("turn_count") or 1) + 1,
                "context_window": model.context_window,
                "keep_recent": model.keep_recent,
            }
        )
        self.store.save_tasks(tasks)

        # Record the user's turn immediately so the transcript stays ordered
        # even if the run fails before producing anything.
        user_event = event(
            EventType.USER_INPUT, task_id, source=Layer.API, text=prompt, continued=True
        )
        self.bus.publish(user_event.to_dict())
        self.store.append_event(task_id, user_event.to_dict())
        # Returned so the GUI renders this exact event instead of making its own.
        original["user_event"] = user_event.to_dict()

        if task_id not in self._file_snapshots:
            self._take_snapshot(task_id)
        self._start_run(task, model, history=history)
        return original

    def update_task_context(self, task_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Adjust context settings for a conversation (window size, keep-recent)."""
        tasks = self.store.load_tasks()
        found = None
        for task in tasks:
            if task.get("id") == task_id:
                found = task
                break
        if found is None:
            raise ApiError("task not found", status=404)

        if patch.get("context_window") is not None:
            value = int(patch["context_window"])
            found["context_window"] = max(4_000, min(value, 1_000_000))
        if patch.get("keep_recent") is not None:
            value = int(patch["keep_recent"])
            found["keep_recent"] = max(2, min(value, 40))
        found["updated_at"] = time.time()
        self.store.save_tasks(tasks)
        return found

    # -- task edit / archive ------------------------------------------------
    def edit_task(self, task_id: str, prompt: str, model_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Re-run a task with an edited prompt, appending a new turn."""
        prompt = (prompt or "").strip()
        if not prompt:
            raise ApiError("prompt is required")

        tasks = self.store.load_tasks()
        original = next((task for task in tasks if task.get("id") == task_id), None)
        if original is None:
            raise ApiError("task not found", status=404)

        model_payload = model_payload or {
            "provider_id": original.get("provider") or self.settings.get("provider_id"),
            "model": original.get("model") or self.settings.get("model"),
            "thinking": self.settings.get("thinking"),
            "mode": self.settings.get("mode"),
            "elevation": self.settings.get("elevation"),
        }
        model = build_model_config(model_payload, self._keys())
        if not model.api_key:
            raise ApiError(f"No API key saved for {model.label}. Add one in Settings.", status=401)

        # Keep the same task id so history stays one thread, but record the edit.
        task = Task(
            id=task_id,
            kind=TaskKind.AGENT,
            prompt=prompt,
            workspace=str(original.get("workspace") or "") or None,
            timeout_seconds=float(original.get("timeout_seconds") or 900),
        )
        original.update(
            {
                "prompt": prompt,
                "title": _title_from_prompt(prompt),
                "status": "queued",
                "updated_at": time.time(),
                "edited_at": time.time(),
                "edit_count": int(original.get("edit_count") or 0) + 1,
            }
        )
        self.store.save_tasks(tasks)

        system_event = event(
            EventType.USER_INPUT,
            task_id,
            source=Layer.API,
            text=prompt,
            edited=True,
        )
        self.bus.publish(system_event.to_dict())
        self.store.append_event(task_id, system_event.to_dict())
        original["user_event"] = system_event.to_dict()

        if task_id not in self._file_snapshots:
            self._take_snapshot(task_id)
        self._start_run(task, model)
        return original

    def archive_task(self, task_id: str, archived: bool = True) -> Dict[str, Any]:
        tasks = self.store.load_tasks()
        found = False
        for task in tasks:
            if task.get("id") == task_id:
                task["archived"] = archived
                task["updated_at"] = time.time()
                found = True
        if not found:
            raise ApiError("task not found", status=404)
        self.store.save_tasks(tasks)
        return {"task_id": task_id, "archived": archived}

    def archived_tasks(self) -> List[Dict[str, Any]]:
        return [task for task in self.store.load_tasks() if task.get("archived")]

    # -- ledger -------------------------------------------------------------
    def propose_change(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Explicit user-initiated proposal (not exposed to the model)."""
        from onion_harness.ledger import ChangeKind

        try:
            kind = ChangeKind(payload.get("kind"))
        except (ValueError, TypeError):
            raise ApiError("unknown change kind") from None
        record = ChangeRecord(
            task_id=str(payload.get("task_id") or ""),
            kind=kind,
            target=str(payload.get("target") or ""),
            reason=str(payload.get("reason") or "user-initiated"),
            params=payload.get("params") or {},
        )
        try:
            change_id = self.ledger.propose(record)
        except LedgerError as exc:
            raise ApiError(str(exc)) from exc
        return {"change_id": change_id}

    def apply_change(self, change_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            revision = self.ledger.apply(
                change_id,
                override_params=payload.get("params") or None,
                accept_irreversible=bool(payload.get("accept_irreversible", False)),
                note=str(payload.get("note") or "confirmed in UI"),
            )
        except LedgerError as exc:
            raise ApiError(str(exc)) from exc
        self._after_ledger_change()
        return revision.to_dict()

    def reject_change(self, change_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            record = self.ledger.discard(change_id, reason=str(payload.get("reason") or ""))
        except LedgerError as exc:
            raise ApiError(str(exc)) from exc
        return record.to_dict()

    def revert_version(self, version: int) -> Dict[str, Any]:
        try:
            reverted = self.ledger.revert(version)
        except LedgerError as exc:
            raise ApiError(str(exc)) from exc
        self._after_ledger_change()
        return {"reverted": [r.to_dict() for r in reverted], "state": self.ledger.state}

    def redo(self) -> Dict[str, Any]:
        try:
            revisions = self.ledger.redo_all()
        except LedgerError as exc:
            raise ApiError(str(exc)) from exc
        self._after_ledger_change()
        return {"redone": [r.to_dict() for r in revisions], "state": self.ledger.state}

    def history(self) -> Dict[str, Any]:
        return {
            "history": [r.to_dict() for r in self.ledger.list_history()],
            "proposals": [p.to_dict() for p in self.ledger.list_proposals()],
        }

    def _after_ledger_change(self) -> None:
        self._apply_executor_config()
        self.runtime._executors = self.executors
        self._refresh_mcp()

    def _refresh_mcp(self) -> None:
        """Materialise the ledger's MCP records to disk and hot-reload the tools."""
        try:
            for record in self.ledger.state.get("mcp_tools", []):
                name = record.get("name")
                if not name:
                    continue
                spec = McpToolSpec(
                    name=name,
                    title=record.get("title") or name,
                    description=record.get("description") or "",
                    parameters=record.get("parameters") or {},
                    code=record.get("code") or "",
                    enabled=bool(record.get("enabled", True)),
                )
                self.mcp_store.write_tool(spec)
                self.mcp_store.upsert(spec)
                self.mcp_store.set_enabled(name, spec.enabled)

            known = {record.get("name") for record in self.ledger.state.get("mcp_tools", [])}
            for entry in self.mcp_store.load_index():
                if entry.get("name") not in known:
                    self.mcp_store.remove(entry.get("name"))

            self._mcp_tools = self.mcp.load()
            self._refresh_tools()
        except Exception:  # noqa: BLE001 - a broken tool must not kill the app
            logger.exception("mcp refresh failed")

    # -- ledger -> event stream mirror --------------------------------------
    def _on_ledger_event(self, kind: str, payload: Dict[str, Any]) -> None:
        """Mirror ledger transitions into the bus so the GUI can render them."""
        task_id = ""
        change = payload.get("change") or {}
        if isinstance(change, dict):
            task_id = change.get("task_id") or ""
        revision = payload.get("revision") or {}
        if isinstance(revision, dict):
            change = revision.get("change") or change
            task_id = task_id or (change.get("task_id") if isinstance(change, dict) else "")

        type_map = {
            "change-proposal": EventType.CHANGE_PROPOSAL,
            "change-applied": EventType.CHANGE_APPLIED,
            "change-rejected": EventType.CHANGE_REJECTED,
            "change-reverted": EventType.CHANGE_REVERTED,
            "change-redone": EventType.CHANGE_REDONE,
        }
        event_type = type_map.get(kind)
        if event_type is None:
            return

        ev = event(
            event_type,
            task_id or "system",
            source=Layer.API,
            **payload,
        )
        self.bus.publish(ev.to_dict())
        if task_id:
            self.store.append_event(task_id, ev.to_dict())

    # -- fan-out to the runtime ---------------------------------------------
    def _publish(self, onion_event: OnionEvent) -> None:
        payload = onion_event.to_dict()
        self.bus.publish(payload)
        if onion_event.task_id:
            self.store.append_event(onion_event.task_id, payload)

    def _on_process_output(self, task_id: str, channel: str, text: str) -> None:
        """Raw stdout/stderr from the sandbox, for the terminal-mirror sidecar."""
        self.bus.publish(
            {
                "id": f"out_{task_id}_{time.time_ns()}",
                "type": EventType.TOOL_OUTPUT.value,
                "task_id": task_id,
                "source": "sandbox",
                "timestamp": time.time(),
                "seq": 0,
                "payload": {"channel": channel, "text": text},
            }
        )

    def _on_approval_event(self, task_id: str, payload: Dict[str, Any]) -> None:
        """Approval events cannot ride the generator (the tool call is blocked)."""
        self.bus.publish(payload)
        if task_id:
            self.store.append_event(task_id, payload)

    # -- helpers ------------------------------------------------------------
    def executor_catalogue(self) -> List[Dict[str, Any]]:
        return self.executors.describe()

    # -- approvals ----------------------------------------------------------
    def resolve_approval(self, task_id: str, approval_id: str, approved: bool, note: str = "") -> Dict[str, Any]:
        ok = self.runtime.resolve_approval(task_id, approval_id, approved, note)
        if not ok:
            raise ApiError("approval request is no longer pending", status=404)
        return {"task_id": task_id, "approval_id": approval_id, "approved": approved}

    # -- modes --------------------------------------------------------------
    def modes(self) -> Dict[str, Any]:
        from onion_harness.harness.modes import ELEVATION_LEVELS, all_modes
        from onion_harness.harness.presets import THINKING_SLIDER

        return {
            "modes": all_modes(),
            "elevation_levels": ELEVATION_LEVELS,
            "thinking_slider": THINKING_SLIDER,
        }

    # -- mcp ----------------------------------------------------------------
    def mcp_tools(self) -> List[Dict[str, Any]]:
        live = {tool.metadata.get("label") for tool in self._mcp_tools}
        entries = []
        for entry in self.mcp_store.load_index():
            item = dict(entry)
            item["loaded"] = bool(entry.get("enabled", True))
            entries.append(item)
        del live
        return entries

    def set_mcp_enabled(self, name: str, enabled: bool) -> Dict[str, Any]:
        if not self.mcp_store.set_enabled(name, enabled):
            raise ApiError(f"unknown MCP tool {name}", status=404)
        # Mirror into ledger state so a rollback restores the flag.
        for record in self.ledger.state.get("mcp_tools", []):
            if record.get("name") == name:
                record["enabled"] = enabled
        self._save_ledger_state()
        self._mcp_tools = self.mcp.load()
        self._refresh_tools()
        return {"name": name, "enabled": enabled}

    def delete_mcp_tool(self, name: str) -> Dict[str, Any]:
        self.mcp_store.remove(name)
        self.ledger.state["mcp_tools"] = [
            record for record in self.ledger.state.get("mcp_tools", [])
            if record.get("name") != name
        ]
        self._save_ledger_state()
        self._mcp_tools = self.mcp.load()
        self._refresh_tools()
        return {"name": name, "deleted": True}

    def _save_ledger_state(self) -> None:
        self.ledger._store.save_state(self.ledger.state)

    def set_task_status(self, task_id: str, status: str) -> None:
        self._finish_task(task_id, status, {})


_INVALID = object()

_SETTING_BOUNDS = {
    "context_window": (4_000, 1_000_000),
    "keep_recent_messages": (2, 40),
    "max_turns": (1, 200),
}


def _clamp_sampling(key: str, value: Any) -> Any:
    """Clamp a sampling override to the range the provider accepts.

    ``None`` clears the override so the persona's value applies again; any other
    non-number is rejected rather than silently dropped.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _INVALID
    if key == "temperature":
        return max(0.0, min(2.0, number))
    return max(0.0, min(1.0, number))


def _clamp_setting(key: str, value: Any) -> Any:
    """Clamp a numeric setting, rejecting non-numbers.

    Mirrors the bounds the runtime enforces, so a hand-edited settings file or a
    stray API call cannot push the agent outside a sane range.
    """
    bounds = _SETTING_BOUNDS.get(key)
    if bounds is None:
        return value
    try:
        number = int(value)
    except (TypeError, ValueError):
        return _INVALID
    low, high = bounds
    return max(low, min(number, high))


def host_id_to_index(seats: List[Any], host_id: str) -> int:
    for index, seat in enumerate(seats):
        if seat.id == host_id:
            return index
    return 0


def _title_from_prompt(prompt: str) -> str:
    flat = " ".join(prompt.split())
    return flat[:48] + ("…" if len(flat) > 48 else "")


__all__ = ["OnionService", "ApiError"]