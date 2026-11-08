"""Supervising the local search service.

The behaviour that matters most is the boring one: when the service is not
installed, launching the app must still succeed and web search must report itself
as unavailable. A missing optional dependency taking down startup would be a much
worse failure than a search that says it cannot run.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from onion_harness.app.search_service import (
    MAX_RESTARTS,
    SearchService,
    ServiceStatus,
    probe,
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b'{"answer":"ok","top":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server:
    """A real HTTP server on a free port, for probe and reuse tests."""

    def __init__(self, status=200):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.port}/search"

    def stop(self):
        self.server.shutdown()


def _pid_alive(pid: int) -> bool:
    """Windows-safe liveness check. ``os.kill(pid, 0)`` is not one."""
    import os as _os

    if _os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
        )
        return str(pid) in (result.stdout or "")
    try:
        _os.kill(pid, 0)
        return True
    except OSError:
        return False


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ProbeTest(unittest.TestCase):
    def test_live_server_is_detected(self):
        server = _Server()
        try:
            self.assertTrue(probe(server.endpoint, timeout=2))
        finally:
            server.stop()

    def test_closed_port_is_not(self):
        self.assertFalse(probe(f"http://127.0.0.1:{_free_port()}/search", timeout=1))

    def test_error_status_still_counts_as_alive(self):
        """A server answering 500 is running; only a refused connection is absent."""
        class Failing(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(500)
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Failing)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(probe(f"http://127.0.0.1:{port}/search", timeout=2))
        finally:
            server.shutdown()


class StatusTest(unittest.TestCase):
    def test_available_states(self):
        self.assertTrue(ServiceStatus(state="running").available)
        self.assertTrue(ServiceStatus(state="external").available)
        self.assertFalse(ServiceStatus(state="stopped").available)
        self.assertFalse(ServiceStatus(state="unavailable").available)
        self.assertFalse(ServiceStatus(state="failed").available)

    def test_to_dict_is_json_safe(self):
        import json

        json.dumps(ServiceStatus(state="running", pid=1).to_dict())


class AbsenceTest(unittest.TestCase):
    """The critical case: no service installed."""

    def test_missing_service_reports_unavailable_without_raising(self):
        supervisor = SearchService(
            f"http://127.0.0.1:{_free_port()}/search",
            command=["definitely-not-installed-xyz"],
        )
        status = supervisor.start()
        # The command exists as a setting but cannot be found: it must report a
        # failure rather than propagate the error.
        self.assertIn(status.state, ("failed", "unavailable"))
        supervisor.stop()

    def test_disabled_means_unavailable(self):
        supervisor = SearchService("http://127.0.0.1:1/search", enabled=False)
        status = supervisor.start()
        self.assertEqual(status.state, "unavailable")
        self.assertIn("disabled", status.detail)

    def test_start_is_fast_when_nothing_is_installed(self):
        supervisor = SearchService(f"http://127.0.0.1:{_free_port()}/search")
        started = time.perf_counter()
        supervisor.start()
        elapsed = time.perf_counter() - started
        supervisor.stop()
        # It must not block startup on a long timeout when the service is absent.
        self.assertLess(elapsed, 20)


class ReuseTest(unittest.TestCase):
    def test_an_existing_service_is_reused_not_duplicated(self):
        """Starting a second copy would fight over the port or shadow the user's."""
        server = _Server()
        try:
            supervisor = SearchService(server.endpoint, command=["should-never-run"])
            status = supervisor.start()
            self.assertEqual(status.state, "external")
            self.assertFalse(status.managed)
            self.assertIsNone(supervisor.status.pid)
            supervisor.stop()
            # Stopping our supervisor must not kill a process we did not start.
            self.assertTrue(probe(server.endpoint, timeout=2))
        finally:
            server.stop()


class SupervisionTest(unittest.TestCase):
    """Launch a real subprocess that serves HTTP, so ownership is exercised."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-svc-"))
        self.port = _free_port()
        self.script = self.dir / "fake_service.py"
        self.script.write_text(
            "\n".join(
                [
                    "import json",
                    "from http.server import BaseHTTPRequestHandler, HTTPServer",
                    "",
                    "class H(BaseHTTPRequestHandler):",
                    "    def log_message(self, *a): pass",
                    "    def do_GET(self):",
                    '        body = b\'{"answer":"served","top":[]}\'',
                    "        self.send_response(200)",
                    "        self.send_header('Content-Length', str(len(body)))",
                    "        self.end_headers()",
                    "        self.wfile.write(body)",
                    "",
                    f"HTTPServer(('127.0.0.1', {self.port}), H).serve_forever()",
                ]
            ),
            encoding="utf-8",
        )
        self.endpoint = f"http://127.0.0.1:{self.port}/search"

    def test_launch_reaches_running(self):
        supervisor = SearchService(
            self.endpoint, command=[sys.executable, "-u", str(self.script)]
        )
        try:
            status = supervisor.start()
            self.assertEqual(status.state, "running", status.detail)
            self.assertTrue(status.managed)
            self.assertIsNotNone(status.pid)
            self.assertTrue(probe(self.endpoint, timeout=2))
        finally:
            supervisor.stop()

    def test_stop_kills_the_managed_process(self):
        supervisor = SearchService(
            self.endpoint, command=[sys.executable, "-u", str(self.script)]
        )
        supervisor.start()
        self.assertTrue(probe(self.endpoint, timeout=2))
        supervisor.stop()
        # Give the OS a moment to release the port.
        deadline = time.time() + 10
        while time.time() < deadline:
            if not probe(self.endpoint, timeout=0.5):
                break
            time.sleep(0.2)
        self.assertFalse(probe(self.endpoint, timeout=0.5))

    def test_refresh_reports_running(self):
        supervisor = SearchService(
            self.endpoint, command=[sys.executable, "-u", str(self.script)]
        )
        try:
            supervisor.start()
            self.assertEqual(supervisor.refresh().state, "running")
        finally:
            supervisor.stop()

    def test_a_process_that_dies_is_reported_failed(self):
        broken = self.dir / "broken.py"
        broken.write_text("raise SystemExit(3)", encoding="utf-8")
        supervisor = SearchService(
            f"http://127.0.0.1:{_free_port()}/search",
            command=[sys.executable, "-u", str(broken)],
        )
        try:
            status = supervisor.start()
            self.assertIn(status.state, ("failed", "unavailable"))
        finally:
            supervisor.stop()

    def test_restart_count_is_bounded(self):
        """A service that exits immediately must not be restarted forever."""
        flapper = self.dir / "flapper.py"
        flapper.write_text("import time\ntime.sleep(0.3)", encoding="utf-8")
        supervisor = SearchService(
            f"http://127.0.0.1:{_free_port()}/search",
            command=[sys.executable, "-u", str(flapper)],
        )
        try:
            supervisor.start()
            # Whatever the intermediate state, the bound must exist.
            self.assertLessEqual(supervisor.status.restarts, MAX_RESTARTS + 1)
        finally:
            supervisor.stop()


class ChildLifetimeTest(unittest.TestCase):
    """A killed app must not leave the service holding its port."""

    def test_job_object_attaches_on_windows(self):
        import os

        from onion_harness.app.search_service import _assign_to_kill_on_close_job

        if os.name != "nt":
            self.skipTest("job objects are Windows-only")
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(_assign_to_kill_on_close_job(process))
        finally:
            process.kill()
            process.wait(timeout=5)

    def test_killing_the_parent_takes_the_child_with_it(self):
        """Simulates the real failure: the app is force-killed, not shut down.

        The supervisor's own stop() never runs in that case, so only the OS-level
        tie can prevent an orphaned service from holding the port forever.
        """
        import os

        if os.name != "nt":
            self.skipTest("job objects are Windows-only")

        dir_path = Path(tempfile.mkdtemp(prefix="onion-orphan-"))
        newline = "\n"
        script = dir_path / "svc.py"
        script.write_text("import time" + newline + "time.sleep(60)", encoding="utf-8")
        # A parent that starts the child, attaches it to a job, then hangs. Killing
        # the parent is the test.
        parent = dir_path / "parent.py"
        parent.write_text(
            newline.join(
                [
                    "import subprocess, sys, time",
                    f"sys.path.insert(0, {str(Path.cwd())!r})",
                    "from onion_harness.app.search_service import _assign_to_kill_on_close_job",
                    f"child = subprocess.Popen([sys.executable, {str(script)!r}],",
                    "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)",
                    "_assign_to_kill_on_close_job(child)",
                    "print(child.pid, flush=True)",
                    "time.sleep(60)",
                ]
            ),
            encoding="utf-8",
        )
        host = subprocess.Popen(
            [sys.executable, "-u", str(parent)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            line = host.stdout.readline().strip()
            self.assertTrue(line.isdigit(), f"parent did not report a pid: {line!r}")
            child_pid = int(line)
            host.kill()
            host.wait(timeout=10)

            # The child must die with the parent. Poll rather than assume instant.
            # os.kill(pid, 0) is not a liveness probe on Windows, so tasklist is used.
            deadline = time.time() + 12
            alive = True
            while time.time() < deadline:
                if _pid_alive(child_pid):
                    time.sleep(0.4)
                else:
                    alive = False
                    break
            self.assertFalse(alive, "the search service outlived the app")
        finally:
            try:
                host.kill()
            except Exception:
                pass


class AppIntegrationTest(unittest.TestCase):
    """The app shell must survive a missing service."""

    def setUp(self):
        import os

        os.environ.setdefault("ONION_SECRET_STORE", "memory")

    def test_missing_service_does_not_block_app_startup(self):
        from onion_harness.api.service import OnionService
        from onion_harness.app.main import _start_search_service

        service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-svcapp-")))
        service.start()
        service.update_settings(
            {"web_search": {"base_url": f"http://127.0.0.1:{_free_port()}"}}
        )
        try:
            supervisor = _start_search_service(service)
            # Either a supervisor object or None; never an exception.
            status = service.web_search_status()
            self.assertIn(status["state"], ("unavailable", "failed", "stopped", "external"))
            if supervisor is not None:
                supervisor.stop()
        finally:
            service.stop()

    def test_disabled_search_skips_supervision(self):
        from onion_harness.api.service import OnionService
        from onion_harness.app.main import _start_search_service

        service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-svcapp2-")))
        service.start()
        service.update_settings({"web_search": {"enabled": False}})
        try:
            self.assertIsNone(_start_search_service(service))
        finally:
            service.stop()


if __name__ == "__main__":
    unittest.main()