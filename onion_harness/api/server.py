"""Threaded HTTP + SSE transport for the local GUI.

Deliberately dependency-free: a small WSGI-style handler on
``http.server``/``socketserver``.  It binds to ``127.0.0.1`` on a random port,
serves the packaged static bundle, and exposes a JSON API plus one SSE stream.

This module is pure protocol conversion: parse the request, call the service,
serialise the result.  No agent logic.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from onion_harness.api.bus import sse_format
from onion_harness.api.service import ApiError, OnionService
from onion_harness.harness.errors import sanitize

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
    ".map": "application/json; charset=utf-8",
}


class OnionHTTPServer:
    """Owns the socket server and its lifecycle."""

    def __init__(self, service: OnionService, static_dir: Optional[str | Path] = None) -> None:
        self.service = service
        self.static_dir = Path(static_dir) if static_dir else None
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("server not started")
        return self._httpd.server_address[1]

    def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        handler = _make_handler(self.service, self.static_dir)
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="onion-http", daemon=True
        )
        self._thread.start()
        return f"http://{host}:{self.port}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def _make_handler(service: OnionService, static_dir: Optional[Path]):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "OnionHarness"

        # -- logging ------------------------------------------------------
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            if "/api/events" in (self.path or ""):
                return
            print(f"[onion] {self.address_string()} {fmt % args}")

        # -- verbs --------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch("PATCH")

        def _dispatch(self, method: str) -> None:
            try:
                self._route(method)
            except ApiError as exc:
                self._json({"error": exc.message, "code": "api"}, status=exc.status)
            except BrokenPipeError:
                pass
            except Exception as exc:  # noqa: BLE001
                self._json({"error": sanitize(str(exc)), "code": "internal"}, status=500)

        # -- routing ------------------------------------------------------
        def _route(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path.startswith("/api/"):
                self._api(method, path, query)
                return
            self._static(path)

        def _api(self, method: str, path: str, query: Dict[str, list]) -> None:
            body = self._body() if method in {"POST", "DELETE", "PATCH"} else {}

            if method == "GET" and path == "/api/bootstrap":
                return self._json(service.bootstrap())
            if method == "GET" and path == "/api/modes":
                return self._json(service.modes())
            if method == "GET" and path == "/api/roundtable/roster":
                return self._json({"providers": service.roundtable_roster()})
            if method == "POST" and path == "/api/roundtable":
                return self._json(service.create_roundtable(body), status=201)
            match = re.match(r"^/api/roundtable/([^/]+)/messages$", path)
            if match:
                if method == "POST":
                    return self._json(
                        service.roundtable_message(
                            match.group(1),
                            str(body.get("text") or ""),
                            str(body.get("to") or "all"),
                        ),
                        status=201,
                    )
                if method == "GET":
                    return self._json(service.roundtable_seat_thoughts(match.group(1)))
            match = re.match(r"^/api/roundtable/([^/]+)/seats$", path)
            if match and method == "POST":
                return self._json(
                    service.roundtable_add_seat(match.group(1), body), status=201
                )
            match = re.match(r"^/api/roundtable/([^/]+)/thoughts$", path)
            if match and method == "GET":
                return self._json(service.roundtable_seat_thoughts(match.group(1)))
            if method == "GET" and path == "/api/mcp":
                return self._json({"tools": service.mcp_tools()})
            if method == "GET" and path == "/api/memory":
                return self._json(
                    {"stats": service.memory_stats(), "memories": service.memory_list()}
                )
            if method == "POST" and path == "/api/memory":
                return self._json(service.memory_add(body), status=201)
            if method == "DELETE" and path == "/api/memory":
                return self._json(service.memory_purge())
            match = re.match(r"^/api/memory/([^/]+)$", path)
            if match and method == "DELETE":
                return self._json(service.memory_forget(match.group(1)))
            if match and method in ("PATCH", "POST"):
                return self._json(service.memory_update(match.group(1), body))
            if method == "GET" and path == "/api/workspace":
                return self._json(
                    service.workspace_tree(
                        query.get("path", [""])[0],
                        int(query.get("depth", ["1"])[0] or 1),
                        query.get("task_id", [""])[0],
                    )
                )
            if method == "GET" and path == "/api/workspace/file":
                return self._json(service.workspace_file(query.get("path", [""])[0]))
            match = re.match(r"^/api/tasks/([^/]+)/files$", path)
            if match and method == "GET":
                return self._json(service.task_file_changes(match.group(1)))
            if method == "GET" and path == "/api/tasks/archived":
                return self._json({"tasks": service.archived_tasks()})
            if method == "GET" and path == "/api/settings":
                return self._json(service.get_settings())
            if method == "POST" and path == "/api/settings":
                return self._json(service.update_settings(body))
            if method == "POST" and path == "/api/settings/api-key":
                provider = str(body.get("provider_id") or "")
                if not provider:
                    raise ApiError("provider_id is required")
                return self._json(
                    service.update_settings(
                        {"provider_id": provider, "api_key": body.get("api_key", "")}
                    )
                )
            if method == "DELETE" and path == "/api/settings/api-key":
                provider = str(query.get("provider_id", [""])[0])
                return self._json(service.clear_api_key(provider))
            if method == "GET" and path == "/api/tasks":
                return self._json({"tasks": service.store.load_tasks()})
            if method == "POST" and path == "/api/tasks":
                return self._json(service.create_task(body), status=201)

            match = re.match(r"^/api/tasks/([^/]+)$", path)
            if match and method == "DELETE":
                return self._json(service.delete_task(match.group(1)))
            match = re.match(r"^/api/tasks/([^/]+)/events$", path)
            if match and method == "GET":
                return self._json({"events": service.task_events(match.group(1))})
            match = re.match(r"^/api/tasks/([^/]+)/cancel$", path)
            if match and method == "POST":
                return self._json(service.cancel_task(match.group(1)))
            match = re.match(r"^/api/tasks/([^/]+)/continue$", path)
            if match and method == "POST":
                return self._json(
                    service.continue_task(
                        match.group(1),
                        str(body.get("prompt") or ""),
                        body.get("model"),
                        body.get("context"),
                    )
                )
            match = re.match(r"^/api/tasks/([^/]+)/context$", path)
            if match and method == "POST":
                return self._json(service.update_task_context(match.group(1), body))
            match = re.match(r"^/api/tasks/([^/]+)/ui-action$", path)
            if match and method == "POST":
                return self._json(
                    service.ui_action(match.group(1), str(body.get("action_id") or ""))
                )
            match = re.match(r"^/api/tasks/([^/]+)/history$", path)
            if match and method == "GET":
                return self._json({"history": service.task_history(match.group(1))})
            match = re.match(r"^/api/tasks/([^/]+)/edit$", path)
            if match and method == "POST":
                return self._json(
                    service.edit_task(
                        match.group(1),
                        str(body.get("prompt") or ""),
                        body.get("model"),
                    )
                )
            match = re.match(r"^/api/tasks/([^/]+)/archive$", path)
            if match and method == "POST":
                return self._json(
                    service.archive_task(match.group(1), bool(body.get("archived", True)))
                )
            match = re.match(r"^/api/tasks/([^/]+)/approvals/([^/]+)$", path)
            if match and method == "POST":
                return self._json(
                    service.resolve_approval(
                        match.group(1),
                        match.group(2),
                        bool(body.get("approved")),
                        str(body.get("note") or ""),
                    )
                )
            match = re.match(r"^/api/mcp/([^/]+)/enabled$", path)
            if match and method == "POST":
                return self._json(
                    service.set_mcp_enabled(match.group(1), bool(body.get("enabled", True)))
                )
            match = re.match(r"^/api/mcp/([^/]+)$", path)
            if match and method == "DELETE":
                return self._json(service.delete_mcp_tool(match.group(1)))

            if method == "GET" and path == "/api/events":
                return self._stream(query)

            if method == "GET" and path == "/api/history":
                return self._json(service.history())
            if method == "POST" and path == "/api/changes":
                return self._json(service.propose_change(body), status=201)
            match = re.match(r"^/api/changes/([^/]+)/apply$", path)
            if match and method == "POST":
                return self._json(service.apply_change(match.group(1), body))
            match = re.match(r"^/api/changes/([^/]+)/reject$", path)
            if match and method == "POST":
                return self._json(service.reject_change(match.group(1), body))
            match = re.match(r"^/api/history/(\d+)/revert$", path)
            if match and method == "POST":
                return self._json(service.revert_version(int(match.group(1))))
            if method == "POST" and path == "/api/history/redo":
                return self._json(service.redo())

            raise ApiError(f"unknown endpoint: {path}", status=404)

        # -- SSE ----------------------------------------------------------
        def _stream(self, query: Dict[str, list]) -> None:
            task_id = (query.get("task_id") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            sub = service.bus.subscribe(task_id)
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                last_ping = time.time()
                while True:
                    payload = sub.get(timeout=10.0)
                    if payload is None:
                        break
                    if payload == {}:
                        if time.time() - last_ping > 12:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            last_ping = time.time()
                        continue
                    self.wfile.write(sse_format(payload))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                service.bus.unsubscribe(sub)

        # -- static -------------------------------------------------------
        def _static(self, path: str) -> None:
            if static_dir is None or not (static_dir / "index.html").exists():
                self._json(
                    {
                        "error": "GUI bundle not built",
                        "hint": "Run `npm run build` in frontend/ then restart.",
                    },
                    status=503,
                )
                return

            rel = path.lstrip("/") or "index.html"
            candidate = (static_dir / rel).resolve()
            try:
                candidate.relative_to(static_dir.resolve())
            except ValueError:
                self._json({"error": "invalid path"}, status=403)
                return

            if not candidate.is_file():
                candidate = static_dir / "index.html"
                if not candidate.is_file():
                    self._json({"error": "not found"}, status=404)
                    return

            ctype = _STATIC_TYPES.get(candidate.suffix.lower()) or mimetypes.guess_type(
                str(candidate)
            )[0] or "application/octet-stream"
            data = candidate.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        # -- helpers ------------------------------------------------------
        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError("invalid JSON body") from None
            return parsed if isinstance(parsed, dict) else {}

        def _json(self, payload: Any, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return Handler


__all__ = ["OnionHTTPServer"]