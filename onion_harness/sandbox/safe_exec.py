"""Silent, bounded process execution.

The single guarantee this module exists for: on Windows, a child console process
must never open a window or steal focus. A GUI app that spawns ``powershell.exe``
or ``cmd.exe`` without ``CREATE_NO_WINDOW`` gets a black console flashing on every
tool call, which makes the whole application unusable.

Two independent mechanisms are applied, because either alone has a gap:

* ``CREATE_NO_WINDOW`` (0x08000000) tells the OS the child gets no console window.
* ``STARTUPINFO`` with ``SW_HIDE`` covers the case where the parent is a windowed
  process and the child would otherwise inherit a hidden-but-focusable window.
  Without both, ``pythonw``-hosted parents can still flash a frame.

Timeouts are hard: on expiry the *whole tree* is killed, because a script that
spawns a child (``cmd /c start``, a background service) would otherwise outlive the
kill and keep the pipe open, which is how a "5 minute limit" turns into a hang.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from onion_harness.core.primitives import Result

_WINDOWS = os.name == "nt"

# Documented constants. Declared literally so the code works on a non-Windows
# interpreter reading it, and so the value is visible in review.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008

DEFAULT_TIMEOUT = 300.0  # five minutes, per the execution contract
MAX_TIMEOUT = 3600.0


def spawn_kwargs(*, new_group: bool = True) -> Dict[str, Any]:
    """The keyword arguments every Windows child process must be started with.

    Returned separately from the executor so the async launcher and the blocking
    executor cannot drift: both call this.
    """
    if not _WINDOWS:
        # POSIX has no windows to hide; a new session lets us kill the group.
        return {"start_new_session": True} if new_group else {}

    creationflags = CREATE_NO_WINDOW
    if new_group:
        # CREATE_NO_WINDOW cannot be combined with DETACHED_PROCESS, but it does
        # combine with CREATE_NEW_PROCESS_GROUP, which is what lets taskkill /T
        # address the tree.
        creationflags |= CREATE_NEW_PROCESS_GROUP

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE

    return {"creationflags": creationflags, "startupinfo": startupinfo}


def powershell_argv(script_path: str) -> List[str]:
    """PowerShell invocation that cannot prompt, profile, or show a window.

    ``-NonInteractive`` stops a cmdlet from waiting on input forever (which would
    look like a hung task), ``-NoProfile`` keeps the run reproducible and fast,
    and ``-WindowStyle Hidden`` is belt and braces alongside CREATE_NO_WINDOW.
    """
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


def _clamp_timeout(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if seconds <= 0:
        return DEFAULT_TIMEOUT
    return max(0.1, min(seconds, MAX_TIMEOUT))


@dataclass
class CommandOutcome:
    """Decoded outcome of one silent execution."""

    ok: bool
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0
    error: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def to_result(self) -> Result:
        return Result(
            ok=self.ok,
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            error=self.error,
            duration_ms=self.duration_ms,
            timed_out=self.timed_out,
            data=dict(self.data),
        )


def _decode(raw: Optional[bytes]) -> str:
    if not raw:
        return ""
    # Windows tools still emit the ANSI/OEM code page; utf-8 with replacement is
    # what keeps a stray byte from aborting the read.
    return raw.decode("utf-8", errors="replace")


def kill_tree(pid: int) -> None:
    """Terminate a process and all its children. Never raises."""
    if _WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                # taskkill is itself a console program: without this it flashes.
                **spawn_kwargs(new_group=False),
            )
            return
        except Exception:  # noqa: BLE001 - fall through to a direct kill
            pass
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass


class SafeCommandExecutor:
    """Run a command with no window, captured output, and a hard timeout.

    Blocking on purpose: it is the simple, auditable path used for short utility
    calls (``taskkill``, one-shot probes) and as the reference implementation for
    the flags the async launcher must also set. Long-running agent work goes
    through the async launcher so the event loop is never blocked.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        encoding: str = "utf-8",
    ) -> None:
        self.timeout = _clamp_timeout(timeout)
        self.cwd = cwd
        self.env = env
        self.encoding = encoding

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandOutcome:
        """Execute ``argv`` and return its captured output. Never raises."""
        started = time.perf_counter()
        args = [str(part) for part in argv if part is not None]
        if not args:
            return CommandOutcome(
                ok=False, error="empty command", duration_ms=_ms(started)
            )

        effective = _clamp_timeout(timeout if timeout is not None else self.timeout)
        try:
            proc = subprocess.Popen(
                args,
                cwd=cwd or self.cwd,
                env=env or self.env,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                **spawn_kwargs(),
            )
        except FileNotFoundError:
            return CommandOutcome(
                ok=False,
                error=f"executable not found: {args[0]}",
                duration_ms=_ms(started),
            )
        except (PermissionError, OSError) as exc:
            return CommandOutcome(
                ok=False,
                error=f"failed to start process: {exc}",
                duration_ms=_ms(started),
            )

        payload = stdin.encode(self.encoding) if stdin is not None else None
        try:
            stdout_b, stderr_b = proc.communicate(payload, timeout=effective)
        except subprocess.TimeoutExpired:
            # The tree must die, not just the parent: a child holding the pipe
            # open would make communicate() hang past the timeout it just hit.
            kill_tree(proc.pid)
            try:
                stdout_b, stderr_b = proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001
                stdout_b, stderr_b = b"", b""
            return CommandOutcome(
                ok=False,
                exit_code=proc.returncode,
                stdout=_decode(stdout_b),
                stderr=_decode(stderr_b),
                timed_out=True,
                error=f"timed out after {effective:g}s",
                duration_ms=_ms(started),
            )

        code = proc.returncode
        return CommandOutcome(
            ok=code == 0,
            exit_code=code,
            stdout=_decode(stdout_b),
            stderr=_decode(stderr_b),
            duration_ms=_ms(started),
        )

    # -- convenience -------------------------------------------------------
    def powershell(self, script_path: str, **kwargs: Any) -> CommandOutcome:
        return self.run(powershell_argv(str(script_path)), **kwargs)

    def cmd(self, script_path: str, **kwargs: Any) -> CommandOutcome:
        return self.run(cmd_argv(str(script_path)), **kwargs)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def python_argv(script_path: str) -> List[str]:
    """Python invocation with unbuffered output, so streaming shows progress."""
    return [sys.executable or "python", "-u", str(script_path)]


__all__ = [
    "SafeCommandExecutor",
    "CommandOutcome",
    "spawn_kwargs",
    "powershell_argv",
    "cmd_argv",
    "python_argv",
    "kill_tree",
    "CREATE_NO_WINDOW",
    "CREATE_NEW_PROCESS_GROUP",
    "DEFAULT_TIMEOUT",
    "MAX_TIMEOUT",
]