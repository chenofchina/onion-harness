"""Executor registry - the plugin catalogue the harness exposes to the model."""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from onion_harness.core.tool import OnionTool
from onion_harness.plugins.base import BaseExecutor
from onion_harness.plugins.cmd_executor import CmdExecutor
from onion_harness.plugins.powershell_executor import PowerShellExecutor
from onion_harness.plugins.python_executor import PythonExecutor
from onion_harness.sandbox.api import Sandbox


class ExecutorRegistry:
    """Maps executor names to plugin instances.

    Registration is the *only* way a new executor becomes available, which is
    what makes ``register_executor`` a safe meta-operation.
    """

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox
        self._executors: Dict[str, BaseExecutor] = {}

    def register(self, executor: BaseExecutor) -> None:
        self._executors[executor.name] = executor

    def unregister(self, name: str) -> None:
        self._executors.pop(name, None)

    def get(self, name: str) -> Optional[BaseExecutor]:
        return self._executors.get(name)

    def all(self) -> List[BaseExecutor]:
        return list(self._executors.values())

    def names(self) -> List[str]:
        return list(self._executors.keys())

    def describe(self) -> List[dict]:
        return [ex.describe() for ex in self._executors.values()]

    def tools(self, invoke_factory) -> List[OnionTool]:
        """Build tool wrappers; ``invoke_factory(executor)`` returns a handler."""
        return [ex.as_tool(invoke_factory(ex)) for ex in self._executors.values()]

    def with_registry(self, executor_classes: Iterable[type[BaseExecutor]]) -> "ExecutorRegistry":
        """Clone with an explicit set of executor classes (used by metadata ops)."""
        clone = ExecutorRegistry(self._sandbox)
        for cls in executor_classes:
            try:
                clone.register(cls(self._sandbox))
            except Exception:  # noqa: BLE001 - a bad plugin must not kill the app
                continue
        return clone


BUILTIN_EXECUTOR_CLASSES = [PythonExecutor, PowerShellExecutor, CmdExecutor]

EXECUTOR_CATALOGUE = {
    "python": {
        "label": "Python",
        "class_name": "PythonExecutor",
        "module": "onion_harness.plugins.python_executor",
        "icon": "python",
        "file_suffix": ".py",
    },
    "powershell": {
        "label": "PowerShell",
        "class_name": "PowerShellExecutor",
        "module": "onion_harness.plugins.powershell_executor",
        "icon": "powershell",
        "file_suffix": ".ps1",
    },
    "cmd": {
        "label": "Command Prompt",
        "class_name": "CmdExecutor",
        "module": "onion_harness.plugins.cmd_executor",
        "icon": "cmd",
        "file_suffix": ".cmd",
    },
}


def builtin_executors(sandbox: Sandbox) -> ExecutorRegistry:
    registry = ExecutorRegistry(sandbox)
    for cls in BUILTIN_EXECUTOR_CLASSES:
        registry.register(cls(sandbox))
    return registry


def executor_class(name: str) -> Optional[type[BaseExecutor]]:
    """Resolve a catalogue entry to an executor class (no dynamic imports)."""
    return {
        "python": PythonExecutor,
        "powershell": PowerShellExecutor,
        "cmd": CmdExecutor,
    }.get(name)


__all__ = [
    "ExecutorRegistry",
    "builtin_executors",
    "executor_class",
    "EXECUTOR_CATALOGUE",
    "BUILTIN_EXECUTOR_CLASSES",
]