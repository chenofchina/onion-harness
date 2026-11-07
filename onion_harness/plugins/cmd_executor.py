"""CMD executor - Windows command interpreter batches."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from onion_harness.plugins.base import BaseExecutor


class CmdExecutor(BaseExecutor):
    name = "cmd"
    label = "Command Prompt"
    icon = "cmd"
    language = "batch"
    file_suffix = ".cmd"

    @property
    def description(self) -> str:
        return (
            "Execute a Windows Command Prompt (batch) script. Use for cmd.exe "
            "builtins and legacy Windows tooling. Output is UTF-8. Elevation is off "
            "unless the task opts in."
        )

    def prepare_script(self, code: str) -> str:
        header = "@echo off\r\nchcp 65001 >nul\r\n"
        return header + code.replace("\n", "\r\n")

    def build_command(
        self, script_path: Path, timeout_seconds: float, cwd: Path
    ) -> List[str]:
        return ["cmd.exe", "/d", "/c", str(script_path)]

    @staticmethod
    def available() -> bool:
        return os.name == "nt"


__all__ = ["CmdExecutor"]