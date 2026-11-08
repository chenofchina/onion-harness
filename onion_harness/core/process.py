"""Starting child processes: how to find an interpreter and how to stay silent.

Both concerns recur in every layer that launches something - executors, MCP servers,
the search supervisor - so they live in core rather than being reimplemented (and
drifting) in each one. Core is stdlib-only, which is all this needs.

Two hard-won rules live here.

**``sys.executable`` is not a Python interpreter in a frozen build.** It is the
harness binary. ``[sys.executable, "script.py"]`` then asks Onion to open its own
script path as a command-line argument - which is exactly the bug this module was
written to fix, and it only appears in the packaged app.

**A console must never appear.** A GUI app that spawns ``powershell.exe`` or
``cmd.exe`` without ``CREATE_NO_WINDOW`` gets a black frame flashing on every tool
call, stealing focus from the window the user is typing in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

_WINDOWS = os.name == "nt"

# Documented constants, spelled out so the values are visible in review.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def spawn_kwargs(*, new_group: bool = True) -> Dict[str, Any]:
    """Keyword arguments every child process should be started with.

    On Windows: ``CREATE_NO_WINDOW`` plus a hidden ``STARTUPINFO``. Both are applied
    because either alone leaves a gap - a windowed parent can still produce a
    focusable frame without the startup info.
    """
    if not _WINDOWS:
        return {"start_new_session": True} if new_group else {}

    flags = CREATE_NO_WINDOW
    if new_group:
        flags |= CREATE_NEW_PROCESS_GROUP

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": flags, "startupinfo": startupinfo}


def find_python() -> str:
    """The interpreter to run model-supplied Python code.

    Order: an explicit override, then ``sys.executable`` when it really is a Python
    (a venv, which carries the user's packages), then a search of ``PATH``.
    """
    override = os.environ.get("ONION_PYTHON")
    if override:
        return override

    if not _is_frozen() and sys.executable:
        name = os.path.basename(sys.executable).lower()
        if name.startswith("python") or name.startswith("py"):
            return sys.executable

    for candidate in ("python", "python3", "py"):
        found = shutil.which(candidate)
        if found:
            return found
    return "python"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def powershell_argv(script_path: str) -> List[str]:
    """PowerShell that cannot prompt, load a profile, or show a window."""
    return [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        script_path,
    ]


def cmd_argv(script_path: str) -> List[str]:
    return ["cmd.exe", "/d", "/c", script_path]


def python_argv(script_path: str) -> List[str]:
    """Unbuffered Python, so streamed output appears as it happens."""
    return [find_python(), "-u", script_path]


def kill_tree(pid: int) -> None:
    """Terminate a process and its children. Never raises."""
    if _WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                # taskkill is a console program: without this the cleanup itself
                # flashes the very window it is meant to prevent.
                **spawn_kwargs(new_group=False),
            )
            return
        except Exception:  # noqa: BLE001 - fall through to a direct kill
            pass
    try:
        import signal

        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        os.kill(pid, 9)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "spawn_kwargs",
    "find_python",
    "powershell_argv",
    "cmd_argv",
    "python_argv",
    "kill_tree",
    "CREATE_NO_WINDOW",
    "CREATE_NEW_PROCESS_GROUP",
]