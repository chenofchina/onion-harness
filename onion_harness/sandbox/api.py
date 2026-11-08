"""Sandbox interface and the swappable execution strategy."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from onion_harness.core.primitives import Context, Layer, Result, Task, TaskKind

DEFAULT_OUTPUT_LIMIT = 120_000


class SandboxError(RuntimeError):
    """Raised for sandbox contract violations (not for command failures)."""


@dataclass
class PreparedCommand:
    """A strategy-approved command ready for the launcher."""

    argv: List[str]
    cwd: str
    env: Dict[str, str]
    stdin: Optional[str] = None
    metadata: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


class SandboxStrategy(ABC):
    """Pluggable policy deciding *how* a task is prepared and run.

    Implementations may reject a task, rewrite the command, or change the
    environment.  None of them may know about models, tools or UAC.
    """

    name = "strategy"

    async def prepare(self, task: Task, ctx: Context) -> PreparedCommand:
        raise NotImplementedError

    async def run(self, command: PreparedCommand, ctx: Context) -> Result:
        raise NotImplementedError


class Sandbox:
    """The single public entry point of the sandbox layer."""

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        strategy: Optional[SandboxStrategy] = None,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
    ) -> None:
        self._root = Path(workspace_root).resolve() if workspace_root else None
        if strategy is None:
            from onion_harness.sandbox.strategies import ProcessIsolationStrategy

            strategy = ProcessIsolationStrategy(workspace_root)
        self._strategy = strategy
        self._output_limit = output_limit

    @property
    def workspace_root(self) -> Optional[str]:
        return str(self._root) if self._root else None

    @property
    def strategy(self) -> SandboxStrategy:
        return self._strategy

    def with_strategy(self, strategy: SandboxStrategy) -> "Sandbox":
        """Return a sandbox sharing config but using a different strategy."""
        return Sandbox(
            workspace_root=self._root and str(self._root),
            strategy=strategy,
            output_limit=self._output_limit,
        )

    async def execute(self, task: Task, ctx: Optional[Context] = None) -> Result:
        """Execute ``task``; the only method outer layers should call."""
        ctx = ctx or Context.for_task(task, Layer.SANDBOX)
        # The task carries its own deadline; a caller-supplied context must not
        # silently widen it. This is what makes an explicit short timeout
        # actually short.
        if task.timeout_seconds and task.timeout_seconds > 0:
            ctx.timeout_seconds = min(ctx.timeout_seconds, task.timeout_seconds)
        started = time.perf_counter()

        if task.kind is not TaskKind.COMMAND:
            return Result.failure(
                "sandbox only executes command tasks",
                data={"task_kind": task.kind.value},
            )
        if not task.command:
            return Result.failure("task has no command")

        try:
            prepared = await self._strategy.prepare(task, ctx)
        except SandboxError as exc:
            return Result.failure(str(exc), duration_ms=_elapsed_ms(started))
        except Exception as exc:  # noqa: BLE001 - boundary
            return Result.failure(
                f"failed to prepare sandbox command: {type(exc).__name__}",
                duration_ms=_elapsed_ms(started),
            )

        if ctx.cancellation_requested():
            return Result.failure(
                "cancelled before start",
                cancelled=True,
                duration_ms=_elapsed_ms(started),
            )

        try:
            result = await self._strategy.run(prepared, ctx)
        except Exception as exc:  # noqa: BLE001 - boundary hardening
            result = Result.failure(
                f"sandbox execution error: {type(exc).__name__}",
                duration_ms=_elapsed_ms(started),
            )

        result.duration_ms = result.duration_ms or _elapsed_ms(started)
        _bound_output(result, self._output_limit)
        return result


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _bound_output(result: Result, limit: int) -> None:
    """Cap both streams and flag that truncation happened."""
    for attr in ("stdout", "stderr"):
        value = getattr(result, attr) or ""
        if len(value) > limit:
            head = value[: limit // 2]
            tail = value[-limit // 2 :]
            setattr(
                result,
                attr,
                f"{head}\n\n... [{len(value) - limit} characters truncated] ...\n\n{tail}",
            )
            result.truncated = True


def resolve_workspace(raw: Optional[str], root: Optional[Path] = None) -> Path:
    """Resolve and create a workspace directory, refusing to escape the root."""
    if raw:
        target = Path(os.path.expanduser(raw))
        if not target.is_absolute():
            base = root or Path.cwd()
            target = (base / target).resolve()
        else:
            target = target.resolve()
    else:
        target = Path(root or tempfile.gettempdir()).resolve()
        target.mkdir(parents=True, exist_ok=True)
        return target

    if root is not None:
        resolved_root = root.resolve()
        try:
            target.relative_to(resolved_root)
        except ValueError as exc:
            raise SandboxError(
                f"workspace {target} is outside the allowed root {resolved_root}"
            ) from exc
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise SandboxError(f"workspace {target} is not a directory")
    return target


def clean_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build a predictable environment without leaking the agent's own vars."""
    env = dict(os.environ)
    for noisy in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "NODE_OPTIONS"):
        env.pop(noisy, None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    if base:
        env.update({str(k): str(v) for k, v in base.items()})
    return env


def find_python() -> str:
    """The interpreter for model-supplied code. Delegates to core.

    Kept as a re-export because the sandbox is where callers historically looked;
    the implementation moved to :mod:`onion_harness.core.process` so the harness
    layer can use it without importing outward.
    """
    from onion_harness.core.process import find_python as _find_python

    return _find_python()


def normalise_argv(command: Sequence[str]) -> List[str]:
    argv = [str(part) for part in command if part is not None]
    if not argv:
        raise SandboxError("empty command")
    return argv


__all__ = [
    "Sandbox",
    "SandboxStrategy",
    "SandboxError",
    "PreparedCommand",
    "resolve_workspace",
    "clean_env",
    "find_python",
    "normalise_argv",
    "DEFAULT_OUTPUT_LIMIT",
]