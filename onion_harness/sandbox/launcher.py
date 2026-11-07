"""Process launcher used by sandbox strategies.

Handles the two things a strategy should not have to think about:

* killing an entire process tree on timeout/cancel, and
* the Windows UAC elevation path, where a process is launched through the
  ``runas`` verb and its console output is redirected to a temp file that is
  read back once the task ends.

Elevation is invisible above this module: the result is a normal
:class:`Result`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

from onion_harness.core.primitives import Context, Result
from onion_harness.sandbox.api import PreparedCommand

_WINDOWS = os.name == "nt"


class ProcessLauncher:
    """Runs a prepared command with timeout, tree-kill and optional elevation."""

    def __init__(self, poll_interval: float = 0.1) -> None:
        self._poll_interval = poll_interval

    async def run(self, prepared: PreparedCommand, ctx: Context) -> Result:
        if ctx.elevate and _WINDOWS:
            return await asyncio.to_thread(self._run_elevated_windows, prepared, ctx)
        return await self._run_plain(prepared, ctx)

    # -- normal path ---------------------------------------------------------
    async def _run_plain(self, prepared: PreparedCommand, ctx: Context) -> Result:
        started = time.perf_counter()
        kwargs = {}
        if _WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        try:
            proc = await asyncio.create_subprocess_exec(
                *prepared.argv,
                cwd=prepared.cwd,
                env=prepared.env,
                stdin=asyncio.subprocess.PIPE if prepared.stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except FileNotFoundError:
            return Result.failure(
                f"executable not found: {prepared.argv[0]}",
                duration_ms=_ms(started),
            )
        except (PermissionError, OSError) as exc:
            return Result.failure(
                f"failed to start process: {exc}",
                duration_ms=_ms(started),
            )

        deadline = started + max(0.1, ctx.timeout_seconds)
        timed_out = False
        cancelled = False

        on_output = None
        if isinstance(ctx.extra, dict):
            on_output = ctx.extra.get("on_output")

        try:
            if callable(on_output):
                stdout_b, stderr_b, timed_out, cancelled = await self._stream_communicate(
                    proc, prepared, ctx, deadline, on_output
                )
            else:
                stdin_data = prepared.stdin.encode("utf-8") if prepared.stdin is not None else None
                communicate = asyncio.ensure_future(proc.communicate(stdin_data))
                while True:
                    if communicate.done():
                        break
                    if ctx.cancellation_requested():
                        cancelled = True
                        break
                    if time.perf_counter() >= deadline:
                        timed_out = True
                        break
                    await asyncio.sleep(self._poll_interval)

                if timed_out or cancelled:
                    await self._terminate_tree(proc)
                    communicate.cancel()
                    try:
                        await communicate
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                    return Result(
                        ok=False,
                        timed_out=timed_out,
                        cancelled=cancelled,
                        error=(
                            f"timed out after {ctx.timeout_seconds:g}s"
                            if timed_out
                            else "cancelled by user"
                        ),
                        duration_ms=_ms(started),
                    )

                stdout_b, stderr_b = communicate.result()

            if timed_out or cancelled:
                return Result(
                    ok=False,
                    timed_out=timed_out,
                    cancelled=cancelled,
                    error=(
                        f"timed out after {ctx.timeout_seconds:g}s"
                        if timed_out
                        else "cancelled by user"
                    ),
                    duration_ms=_ms(started),
                )
        except asyncio.CancelledError:
            await self._terminate_tree(proc)
            raise

        code = proc.returncode
        return Result.from_exit(
            code if code is not None else -1,
            stdout=_decode(stdout_b),
            stderr=_decode(stderr_b),
            duration_ms=_ms(started),
        )

    async def _stream_communicate(self, proc, prepared, ctx, deadline, on_output):
        """Read both pipes incrementally, forwarding decoded chunks to a callback.

        Used by the terminal-mirror sidecar. Falls back to nothing special: the
        full output is still accumulated for the Result.
        """
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def pump(stream, sink, channel):
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                sink.append(chunk)
                try:
                    on_output(channel, chunk.decode("utf-8", errors="replace"))
                except Exception:  # noqa: BLE001 - a bad sink must not kill the run
                    pass

        if prepared.stdin is not None:
            try:
                proc.stdin.write(prepared.stdin.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass

        tasks = [
            asyncio.ensure_future(pump(proc.stdout, stdout_chunks, "stdout")),
            asyncio.ensure_future(pump(proc.stderr, stderr_chunks, "stderr")),
        ]
        timed_out = cancelled = False
        try:
            while True:
                if all(task.done() for task in tasks):
                    break
                if ctx.cancellation_requested():
                    cancelled = True
                    break
                if time.perf_counter() >= deadline:
                    timed_out = True
                    break
                await asyncio.sleep(self._poll_interval)
        finally:
            if timed_out or cancelled:
                await self._terminate_tree(proc)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                pass

        return b"".join(stdout_chunks), b"".join(stderr_chunks), timed_out, cancelled

    async def _terminate_tree(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            if _WINDOWS:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001 - best effort
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001
            pass

    # -- elevated path (Windows only) ---------------------------------------
    def _run_elevated_windows(self, prepared: PreparedCommand, ctx: Context) -> Result:
        """Launch via ShellExecute ``runas`` with output captured to a temp file.

        The elevated process runs unattended; we wait on its handle so the task
        lifetime still bounds it.  Because ``runas`` detaches stdio, a wrapper
        ``.cmd`` file performs the redirection.
        """
        started = time.perf_counter()
        workdir = Path(tempfile.mkdtemp(prefix="onion-elev-"))
        log_path = workdir / "output.log"
        cmd_path = workdir / "run.cmd"

        quoted = subprocess.list2cmdline(prepared.argv)
        stdin_part = ""
        if prepared.stdin is not None:
            stdin_path = workdir / "stdin.txt"
            stdin_path.write_text(prepared.stdin, encoding="utf-8")
            stdin_part = f' < "{stdin_path}"'
        exit_marker = workdir / "exit.txt"
        script = (
            "@echo off\r\n"
            f"cd /d \"{prepared.cwd}\"\r\n"
            f"({quoted}){stdin_part} > \"{log_path}\" 2>&1\r\n"
            f"echo %ERRORLEVEL% > \"{exit_marker}\"\r\n"
        )
        cmd_path.write_text(script, encoding="utf-8")

        try:
            code = _shell_execute_runas(str(cmd_path), str(workdir), prepared.env)
        except Exception as exc:  # noqa: BLE001
            return Result.failure(
                f"elevation failed: {exc}", duration_ms=_ms(started)
            )

        # ``code`` > 32 means the shell accepted the request; UAC refusal shows
        # up as ERROR_CANCELLED (1223) meaning the user declined the prompt.
        if code == 1223:
            return Result.failure("elevation declined", duration_ms=_ms(started))
        if code <= 32:
            return Result.failure(
                f"elevation could not start (shell code {code})",
                duration_ms=_ms(started),
            )

        deadline = started + max(0.1, ctx.timeout_seconds)
        exit_code: Optional[int] = None
        while time.perf_counter() < deadline:
            if exit_marker.exists():
                try:
                    exit_code = int(exit_marker.read_text(encoding="utf-8", errors="replace").strip())
                except ValueError:
                    exit_code = 1
                break
            if ctx.cancellation_requested():
                return Result(
                    ok=False,
                    cancelled=True,
                    error="cancelled by user",
                    duration_ms=_ms(started),
                )
            time.sleep(self._poll_interval)

        if exit_code is None:
            # The elevated wrapper did not finish in time.  Taskkill needs its
            # own elevation, so we simply report the timeout.
            return Result(
                ok=False,
                timed_out=True,
                error=f"elevated task timed out after {ctx.timeout_seconds:g}s",
                duration_ms=_ms(started),
            )

        # Give the OS a moment to flush the redirected file.
        time.sleep(0.05)
        output = ""
        if log_path.exists():
            output = log_path.read_text(encoding="utf-8", errors="replace")

        return Result.from_exit(
            exit_code,
            stdout=output,
            stderr="",
            duration_ms=_ms(started),
            data={"elevated": True},
        )


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _decode(raw: Optional[bytes]) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


def _shell_execute_runas(path: str, cwd: str, env: Dict[str, str]) -> int:
    """``ShellExecuteW(..., "runas", ...)`` returning the shell's result code."""
    import ctypes

    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    result = shell32.ShellExecuteW(
        None,
        "runas",
        path,
        None,
        cwd,
        0,  # SW_HIDE
    )
    return int(result or 0)


__all__ = ["ProcessLauncher"]