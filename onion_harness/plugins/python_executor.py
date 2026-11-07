"""Python executor - runs code with the interpreter hosting the app."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from onion_harness.plugins.base import BaseExecutor


class PythonExecutor(BaseExecutor):
    name = "python"
    label = "Python"
    icon = "python"
    language = "python"
    file_suffix = ".py"

    @property
    def description(self) -> str:
        return (
            "Execute a Python 3 snippet in an isolated working directory. "
            "Use for scripting, data processing and automation. Provide complete, "
            "runnable code; print results you need to see. Runs without elevation "
            "unless the task explicitly requests it."
        )

    def prepare_script(self, code: str) -> str:
        # Force UTF-8 both ways so Windows console code pages never mangle output.
        header = (
            "# -*- coding: utf-8 -*-\n"
            "import sys as _onion_sys\n"
            "try:\n"
            "    _onion_sys.stdout.reconfigure(encoding='utf-8')\n"
            "    _onion_sys.stderr.reconfigure(encoding='utf-8')\n"
            "except Exception:\n"
            "    pass\n"
        )
        return header + code

    def build_command(
        self, script_path: Path, timeout_seconds: float, cwd: Path
    ) -> List[str]:
        return [sys.executable or "python", "-u", str(script_path)]


__all__ = ["PythonExecutor"]