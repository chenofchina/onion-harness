"""Sandbox strategies - swappable isolation policies.

The sandbox interface is ``execute(task, ctx) -> result``.  Everything below is
an implementation detail chosen at construction time, so stronger isolation
(containers, AppContainer, filesystem virtualisation) can be dropped in without
changing a single caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from onion_harness.core.primitives import Context, Result, Task
from onion_harness.sandbox.api import (
    PreparedCommand,
    SandboxError,
    SandboxStrategy,
    clean_env,
    normalise_argv,
    resolve_workspace,
)
from onion_harness.sandbox.launcher import ProcessLauncher


class ProcessIsolationStrategy(SandboxStrategy):
    """MVP strategy: process-group execution, forced cwd, clean env, timeout."""

    name = "process-isolation"

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        launcher: Optional[ProcessLauncher] = None,
    ) -> None:
        self._root = Path(workspace_root).resolve() if workspace_root else None
        self._launcher = launcher or ProcessLauncher()

    async def prepare(self, task: Task, ctx: Context) -> PreparedCommand:
        argv = normalise_argv(task.command or [])
        workspace = resolve_workspace(task.workspace or ctx.workspace, self._root)
        env = clean_env({**ctx.env, **task.env})
        return PreparedCommand(
            argv=argv,
            cwd=str(workspace),
            env=env,
            stdin=task.stdin,
            metadata={"workspace": str(workspace)},
        )

    async def run(self, command: PreparedCommand, ctx: Context) -> Result:
        result = await self._launcher.run(command, ctx)
        result.data.setdefault("cwd", command.cwd)
        result.data.setdefault("workspace", command.metadata.get("workspace", command.cwd))
        return result


class ReadOnlyWorkspaceStrategy(ProcessIsolationStrategy):
    """Reserves the strategy slot for read-only mounts.

    MVP: resolves the workspace exactly like the default strategy and marks the
    result so callers can tell the policy was applied.
    """

    name = "read-only-workspace"

    async def prepare(self, task: Task, ctx: Context) -> PreparedCommand:
        prepared = await super().prepare(task, ctx)
        prepared.env["ONION_WORKSPACE_READONLY"] = "1"
        prepared.metadata["readonly"] = True
        return prepared


class NetworkDenyStrategy(ProcessIsolationStrategy):
    """Reserves the strategy slot for network isolation.

    MVP: sets a marker variable and records the policy; the actual deny hook is
    expected to come from OS-level filtering later.
    """

    name = "network-deny"

    async def prepare(self, task: Task, ctx: Context) -> PreparedCommand:
        prepared = await super().prepare(task, ctx)
        prepared.env["ONION_NETWORK_DENIED"] = "1"
        prepared.metadata["network"] = "denied"
        workspace = Path(prepared.cwd)
        if not workspace.exists():
            raise SandboxError(f"workspace vanished: {workspace}")
        return prepared


__all__ = [
    "ProcessIsolationStrategy",
    "ReadOnlyWorkspaceStrategy",
    "NetworkDenyStrategy",
]