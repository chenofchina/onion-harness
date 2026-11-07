"""Layer 1 - core primitives.

This layer has **zero internal dependencies** and only uses the standard
library.  Every other layer is allowed to import from here; nothing here may
import from another Onion layer.

The four data structures are intentionally closed: :class:`Task`,
:class:`Context`, :class:`Result` and :class:`Layer`.  They are the vocabulary
the whole runtime speaks.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional


class Layer(str, Enum):
    """The onion layers, ordered from the centre outwards.

    The numeric value is used by the dependency-direction test to make sure an
    inner layer never reaches for an outer one.
    """

    CORE = "core"
    SANDBOX = "sandbox"
    HARNESS = "harness"
    PLUGINS = "plugins"
    API = "api"
    GUI = "gui"

    @property
    def depth(self) -> int:
        return list(Layer).index(self)


class TaskKind(str, Enum):
    """What kind of unit of work a :class:`Task` describes."""

    AGENT = "agent"
    COMMAND = "command"


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}_{raw[:20]}" if prefix else raw


@dataclass(frozen=True)
class Task:
    """A unit of work travelling between layers.

    ``kind="agent"`` carries a natural-language prompt for the harness.
    ``kind="command"`` carries an explicit ``command`` vector for the sandbox.
    Executors always produce command tasks; the sandbox refuses to run anything
    else.  ``elevate`` is a per-task, opt-in flag handled entirely inside the
    sandbox launcher; the harness layer never inspects it.
    """

    id: str = field(default_factory=lambda: new_id("task"))
    kind: TaskKind = TaskKind.AGENT
    prompt: str = ""
    command: Optional[List[str]] = None
    stdin: Optional[str] = None
    workspace: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 120.0
    elevate: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def derive(self, **changes: Any) -> "Task":
        """Return a copy with ``changes`` applied (keeps a stable task id)."""
        return replace(self, **changes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "prompt": self.prompt,
            "command": list(self.command) if self.command else None,
            "workspace": self.workspace,
            "timeout_seconds": self.timeout_seconds,
            "elevate": self.elevate,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass
class Context:
    """Per-execution ambient state handed to a layer.

    ``cancel`` is an optional ``threading.Event``-like object with ``is_set()``;
    the sandbox polls it while waiting on a process.
    """

    task_id: str
    layer: Layer = Layer.SANDBOX
    workspace: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 120.0
    elevate: bool = False
    cancel: Any = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_task(cls, task: Task, layer: Layer) -> "Context":
        return cls(
            task_id=task.id,
            layer=layer,
            workspace=task.workspace,
            env=dict(task.env),
            timeout_seconds=task.timeout_seconds,
            elevate=task.elevate,
        )

    def cancellation_requested(self) -> bool:
        return bool(self.cancel is not None and getattr(self.cancel, "is_set", lambda: False)())


@dataclass
class Result:
    """The outcome of executing a :class:`Task`."""

    ok: bool = False
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: int = 0
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    artifacts: List[str] = field(default_factory=list)

    @classmethod
    def success(cls, stdout: str = "", **kwargs: Any) -> "Result":
        return cls(ok=True, exit_code=kwargs.pop("exit_code", 0), stdout=stdout, **kwargs)

    @classmethod
    def failure(cls, error: str, **kwargs: Any) -> "Result":
        return cls(ok=False, error=error, **kwargs)

    @classmethod
    def from_exit(cls, exit_code: int, stdout: str = "", stderr: str = "", **kwargs: Any) -> "Result":
        return cls(ok=exit_code == 0, exit_code=exit_code, stdout=stdout, stderr=stderr, **kwargs)

    @property
    def summary(self) -> str:
        if self.timed_out:
            return "execution timed out"
        if self.cancelled:
            return "execution cancelled"
        if self.error:
            return self.error
        if self.exit_code not in (None, 0):
            return f"exit code {self.exit_code}"
        return "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "artifacts": list(self.artifacts),
        }


__all__ = ["Layer", "TaskKind", "Task", "Context", "Result", "new_id"]