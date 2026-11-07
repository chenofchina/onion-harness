"""Executor abstraction shared by every language plugin."""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from onion_harness.core.primitives import Context, Result, Task, TaskKind
from onion_harness.core.tool import OnionTool
from onion_harness.sandbox.api import Sandbox


@dataclass
class ExecutionOutcome:
    """What an executor hands back to the harness.

    ``output`` is the unified envelope the GUI and the model both consume:

    ``stream``   which stream dominates ("stdout", "stderr" or "mixed")
    ``data``     structured payload (exit code, duration, cwd, ...)
    ``timestamp`` wall-clock seconds when the run finished
    ``status``   one of success / failed / timeout / cancelled
    """

    result: Result
    output: Dict[str, Any] = field(default_factory=dict)
    command_preview: str = ""
    executor: str = ""

    @property
    def ok(self) -> bool:
        return self.result.ok

    def model_text(self, limit: int = 4000) -> str:
        """Compact rendering handed to the model as the tool result."""
        parts: List[str] = []
        if self.command_preview:
            parts.append(f"$ {self.command_preview}")
        if self.result.stdout:
            parts.append(self.result.stdout.rstrip())
        if self.result.stderr:
            parts.append(f"[stderr]\n{self.result.stderr.rstrip()}")
        if self.result.timed_out:
            parts.append(f"[timeout] {self.result.error or ''}".rstrip())
        if self.result.cancelled:
            parts.append("[cancelled]")
        if not self.result.ok and self.result.error:
            parts.append(f"[error] {self.result.error}")
        if self.result.exit_code not in (None, 0):
            parts.append(f"[exit code {self.result.exit_code}]")
        text = "\n".join(p for p in parts if p).strip() or "(no output)"
        if len(text) > limit:
            text = text[: limit - 200] + "\n... (truncated)"
        return text


class BaseExecutor(ABC):
    """One language/runtime plugin.

    Subclasses implement :meth:`build_command` (pure) and inherit the shared
    run/orchestration logic.  The only thing that touches the sandbox is
    :meth:`execute`.
    """

    name: str = "executor"
    label: str = "Executor"
    icon: str = "terminal"
    language: str = "text"
    file_suffix: str = ".txt"

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    # -- contract -----------------------------------------------------------
    @abstractmethod
    def build_command(
        self, script_path: Path, timeout_seconds: float, cwd: Path
    ) -> List[str]:
        """Return the argv that runs ``script_path``."""

    def prepare_script(self, code: str) -> str:
        """Hook for shell wrappers that need surrounding boilerplate."""
        return code

    @property
    def tool_name(self) -> str:
        return f"run_{self.name}"

    @property
    def description(self) -> str:
        raise NotImplementedError

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": f"{self.language} source or commands to execute.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "Hard wall-clock limit for this call.",
                    "default": 120,
                },
                "description": {
                    "type": "string",
                    "description": "Short human-readable summary of the intent.",
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        }

    # -- execution ----------------------------------------------------------
    async def execute(
        self, args: Dict[str, Any], ctx: Context, task: Optional[Task] = None
    ) -> ExecutionOutcome:
        from onion_harness.core.modes import mode_spec, plan_mode_violation

        code = str(args.get("code") or "")
        if not code.strip():
            return self._failure("code is empty", "")

        # Mode gate: Plan refuses state-changing snippets before they spawn.
        mode = (ctx.extra or {}).get("mode") if isinstance(ctx.extra, dict) else None
        violation = plan_mode_violation(mode, code)
        if violation:
            outcome = self._failure(violation, self.preview(code))
            outcome.result.data["blocked_by"] = "mode"
            return outcome

        timeout = float(args.get("timeout_seconds") or ctx.timeout_seconds or 120.0)
        timeout = max(1.0, min(timeout, 3600.0))

        workspace_raw = ctx.workspace or (task.workspace if task else None) or os.getcwd()
        workspace = Path(workspace_raw)
        workspace.mkdir(parents=True, exist_ok=True)

        # Elevation policy: only the "elevated" level opts in, and never in Plan.
        elevation = (ctx.extra or {}).get("elevation") if isinstance(ctx.extra, dict) else None
        elevate = bool(ctx.elevate) and elevation == "elevated" and not mode_spec(mode).read_only

        script_path = workspace / f".onion-{self.name}-{int(time.time() * 1000)}{self.file_suffix}"
        script_path.write_text(self.prepare_script(code), encoding="utf-8")
        try:
            argv = self.build_command(script_path, timeout, workspace)
            task_obj = (task or Task(kind=TaskKind.COMMAND)).derive(
                command=argv,
                workspace=str(workspace),
                timeout_seconds=timeout,
                elevate=elevate,
            )
            result = await self._sandbox.execute(task_obj, ctx)
        finally:
            try:
                script_path.unlink(missing_ok=True)
            except OSError:
                pass

        return ExecutionOutcome(
            result=result,
            output=_envelope(result, self.name),
            command_preview=self.preview(code),
            executor=self.name,
        )

    def preview(self, code: str, limit: int = 160) -> str:
        first = " ".join(part for part in code.strip().splitlines() if part.strip())[:limit]
        return f"{self.language}: {first}"

    # -- harness glue -------------------------------------------------------
    def as_tool(self, invoke) -> OnionTool:
        """Wrap this executor as an :class:`OnionTool`.

        ``invoke`` is supplied by the harness and receives the raw arguments;
        the executor stays unaware of the tool protocol.
        """
        return OnionTool(
            name=self.tool_name,
            description=self.description,
            input_schema=self.input_schema,
            handler=invoke,
            metadata={
                "kind": "executor",
                "executor": self.name,
                "label": self.label,
                "icon": self.icon,
                "language": self.language,
            },
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "icon": self.icon,
            "language": self.language,
            "tool_name": self.tool_name,
            "description": self.description,
        }

    def _failure(self, error: str, preview: str) -> ExecutionOutcome:
        result = Result.failure(error)
        return ExecutionOutcome(
            result=result,
            output=_envelope(result, self.name),
            command_preview=preview,
            executor=self.name,
        )


def _envelope(result: Result, executor: str) -> Dict[str, Any]:
    if result.stdout and result.stderr:
        stream = "mixed"
    elif result.stderr:
        stream = "stderr"
    else:
        stream = "stdout"

    if result.timed_out:
        status = "timeout"
    elif result.cancelled:
        status = "cancelled"
    elif result.ok:
        status = "success"
    else:
        status = "failed"

    return {
        "stream": stream,
        "data": {
            "executor": executor,
            **result.to_dict(),
        },
        "timestamp": time.time(),
        "status": status,
    }


__all__ = ["BaseExecutor", "ExecutionOutcome", "_envelope"]