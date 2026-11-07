"""PowerShell executor - Windows only, no elevation by default."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from onion_harness.plugins.base import BaseExecutor


class PowerShellExecutor(BaseExecutor):
    name = "powershell"
    label = "PowerShell"
    icon = "powershell"
    language = "powershell"
    file_suffix = ".ps1"

    @property
    def description(self) -> str:
        return (
            "Execute a PowerShell script on Windows. Use for system administration, "
            "registry and service inspection, and Windows-specific automation. "
            "Prefer read-only commands; destructive operations should be proposed to "
            "the user first. Elevation is off unless the task opts in."
        )

    def prepare_script(self, code: str) -> str:
        header = (
            "$ErrorActionPreference = 'Continue'\n"
            "$ProgressPreference = 'SilentlyContinue'\n"
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        )
        return header + code

    def build_command(
        self, script_path: Path, timeout_seconds: float, cwd: Path
    ) -> List[str]:
        return [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ]

    def preview(self, code: str, limit: int = 160) -> str:
        return f"powershell: {' '.join(code.split())[:limit]}"

    @staticmethod
    def available() -> bool:
        return os.name == "nt"


__all__ = ["PowerShellExecutor"]