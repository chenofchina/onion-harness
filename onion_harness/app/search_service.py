"""Supervising the local search service.

The web search tool talks to a service on localhost. Until now that service had to
be started by hand before the agent could search, which is a step users forget and
then report as "search is broken". This module starts it with the app and stops it
with the app.

Three things make this safe to do at startup:

* **Absence is not an error.** The service may not be installed. The app must start
  regardless, and the tool must degrade to its documented fail-closed behaviour.
* **An already-running instance is reused.** Starting a second copy would either
  fail on the port or, worse, shadow the one the user started with different
  settings. If something answers on the endpoint, this supervisor does nothing.
* **It is silent.** A console window appearing at launch, or every few seconds when
  the service restarts, would be the same defect the executors just fixed. The
  spawn flags come from the sandbox's shared helper.

Nothing here retries forever: a bounded number of restarts, then the supervisor
gives up and says so. A service that crashes on every launch is a configuration
problem, and a restart loop would only hide it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from onion_harness.sandbox.safe_exec import spawn_kwargs

logger = logging.getLogger("onion.searchsvc")

# One binary name, plus the common shapes it ships under.
DEFAULT_COMMAND = ("search-neo",)
SERVICE_COMMAND_CANDIDATES = ("search-neo", "searchneo", "search_neo")

PROBE_TIMEOUT = 1.0
START_TIMEOUT = 12.0
MAX_RESTARTS = 2
RESTART_BACKOFF = 3.0


@dataclass
class ServiceStatus:
    """What the supervisor currently knows. Reported to the GUI."""

    state: str = "stopped"  # stopped | starting | running | external | unavailable | failed
    pid: Optional[int] = None
    managed: bool = False
    detail: str = ""
    restarts: int = 0

    @property
    def available(self) -> bool:
        # "builtin" counts as available: search works, just not through an external
        # service, and saying otherwise would make the GUI look broken.
        return self.state in ("running", "external", "builtin")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "pid": self.pid,
            "managed": self.managed,
            "available": self.available,
            "detail": self.detail,
            "restarts": self.restarts,
        }


def probe(endpoint: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Does something answer at this endpoint?

    Any HTTP response counts, including an error status: a 404 means a server is
    listening, which is what this needs to know. Only a connection failure means
    "nothing there".
    """
    import urllib.error
    import urllib.request

    # A system proxy must not be consulted for loopback, and urllib honours the
    # environment by default - which would make a local service look unreachable
    # whenever a proxy is configured but down.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(endpoint, timeout=timeout) as response:
            response.read(1)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001 - refused, timeout, DNS, TLS
        return False


def _assign_to_kill_on_close_job(process: subprocess.Popen) -> bool:
    """Tie the child's lifetime to this process (Windows only).

    Without this, killing Onion in Task Manager, or a crash, leaves the search
    service running and holding its port - and the next launch then finds a stale
    instance it does not own. A Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    makes the OS do the cleanup even when our own halt path never runs.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # 0x2000 == JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.BasicLimitInformation.LimitFlags = 0x2000
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)  # 9 == ExtendedLimitInformation
        ):
            return False
        handle = int(process._handle)  # noqa: SLF001 - the only handle we have
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(handle)):
            return False
        # The job handle is deliberately leaked: closing it would kill the child
        # immediately, since the rule is "kill when the last handle closes".
        return True
    except Exception:  # noqa: BLE001 - lifetime cleanup is a nicety, not a gate
        logger.debug("could not attach the search service to a job object", exc_info=True)
        return False


class SearchService:
    """Starts, monitors and stops the local search service."""

    def __init__(
        self,
        endpoint: str,
        *,
        enabled: bool = True,
        command: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.endpoint = endpoint
        self.enabled = enabled
        self.command = list(command or [])
        self.cwd = cwd
        self.env = env
        self.status = ServiceStatus()
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> ServiceStatus:
        """Start supervising. Returns immediately; never raises."""
        if not self.enabled:
            self.status = ServiceStatus(state="unavailable", detail="web search is disabled")
            return self.status

        if probe(self.endpoint):
            # Someone else is serving. Own nothing, use it.
            self.status = ServiceStatus(state="external", detail="using an already-running service")
            return self.status

        resolved = self._resolve_command()
        if resolved is None:
            # Not a problem any more: the tool falls back to its built-in backend, so
            # search keeps working with no external service. Reported as "builtin" so
            # the GUI says what is actually serving requests.
            self.status = ServiceStatus(
                state="builtin",
                detail=(
                    "using Onion's built-in search; install search-neo or set "
                    "web_search_command for cross-engine summaries"
                ),
            )
            logger.info("no external search service; using the built-in backend")
            return self.status

        self.status = ServiceStatus(state="starting", detail="launching " + resolved[0])
        self._thread = threading.Thread(
            target=self._supervise, args=(resolved,), name="onion-search-svc", daemon=True
        )
        self._thread.start()

        # Wait briefly so the first task does not race the launch.
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if self.status.state in ("running", "failed", "unavailable"):
                return self.status
            time.sleep(0.15)
        return self.status

    def stop(self, timeout: float = 4.0) -> None:
        """Stop the supervised process (never one we did not start)."""
        self._stopping.set()
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # A service that ignores terminate is left to the tree kill.
                from onion_harness.sandbox.safe_exec import kill_tree

                kill_tree(process.pid)
        except Exception:  # noqa: BLE001 - teardown is best effort
            logger.debug("could not stop the search service", exc_info=True)

    # -- internals ---------------------------------------------------------
    def _resolve_command(self) -> Optional[List[str]]:
        """Find how to launch the service.

        An explicit command wins. Otherwise the console script is looked up on PATH
        and, for a source checkout, through the running interpreter - a frozen build
        has no ``search-neo`` on PATH even when the package is installed beside it.
        """
        if self.command:
            return list(self.command)

        from shutil import which

        for name in SERVICE_COMMAND_CANDIDATES:
            found = which(name)
            if found:
                return [found]

        # `python -m search_neo` is the shape most small services ship in.
        for module in ("search_neo", "searchneo"):
            try:
                import importlib.util

                if importlib.util.find_spec(module) is not None:
                    return [sys.executable or "python", "-m", module]
            except Exception:  # noqa: BLE001
                continue
        return None

    def _spawn(self, command: List[str]) -> Optional[subprocess.Popen]:
        try:
            process = subprocess.Popen(
                command,
                cwd=self.cwd,
                env=self.env or os.environ.copy(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **spawn_kwargs(),
            )
            # Attach before returning, so a crash of this process cannot leave the
            # service running. The supervisor's own stop() handles the normal path;
            # this handles the path where our code never runs again.
            _assign_to_kill_on_close_job(process)
            return process
        except FileNotFoundError:
            logger.info("search service executable not found: %s", command[0])
        except (PermissionError, OSError) as exc:
            logger.warning("could not start the search service: %s", exc)
        return None

    def _supervise(self, command: List[str]) -> None:
        """Keep the service alive, within a bounded number of restarts."""
        restarts = 0
        while not self._stopping.is_set():
            process = self._spawn(command)
            if process is None:
                self.status = ServiceStatus(
                    state="failed",
                    detail=f"could not launch {command[0]}",
                    restarts=restarts,
                )
                return
            self._process = process

            # Wait for the port to answer, then report running.
            deadline = time.time() + START_TIMEOUT
            became_ready = False
            while time.time() < deadline:
                if self._stopping.is_set() or process.poll() is not None:
                    break
                if probe(self.endpoint, timeout=0.6):
                    became_ready = True
                    break
                time.sleep(0.25)

            if self._stopping.is_set():
                return
            if not became_ready:
                exited = process.poll()
                detail = (
                    f"launched but never answered on {self.endpoint}"
                    if exited is None
                    else f"exited with code {exited}"
                )
                self.status = ServiceStatus(
                    state="failed", pid=process.pid, managed=True, detail=detail, restarts=restarts
                )
                try:
                    if exited is None:
                        process.terminate()
                except Exception:  # noqa: BLE001
                    pass
                return

            self.status = ServiceStatus(
                state="running", pid=process.pid, managed=True, restarts=restarts
            )
            logger.info("search service running on %s (pid %s)", self.endpoint, process.pid)

            # Block until it exits, then decide whether to restart.
            while not self._stopping.is_set():
                if process.poll() is not None:
                    break
                time.sleep(0.5)

            if self._stopping.is_set():
                return

            restarts += 1
            if restarts > MAX_RESTARTS:
                self.status = ServiceStatus(
                    state="failed",
                    managed=True,
                    detail=f"stopped {restarts} times; giving up",
                    restarts=restarts,
                )
                logger.warning("search service keeps exiting; not restarting again")
                return
            logger.info("search service exited; restarting (%d/%d)", restarts, MAX_RESTARTS)
            time.sleep(RESTART_BACKOFF)

    def refresh(self) -> ServiceStatus:
        """Re-probe without changing ownership. Used by the status endpoint."""
        if self.status.state in ("unavailable", "builtin") and not self.enabled:
            return self.status
        if self.status.state == "builtin":
            return self.status
        if self._process is not None and self._process.poll() is None:
            self.status.state = "running"
            self.status.pid = self._process.pid
            return self.status
        alive = probe(self.endpoint)
        if alive and self.status.state in ("external", "running"):
            return self.status
        if alive:
            self.status.state = "external"
            self.status.detail = "another process is serving this endpoint"
        elif self.status.state not in ("failed", "unavailable", "starting"):
            self.status.state = "stopped"
        return self.status


__all__ = [
    "SearchService",
    "ServiceStatus",
    "probe",
    "DEFAULT_COMMAND",
    "SERVICE_COMMAND_CANDIDATES",
    "MAX_RESTARTS",
]