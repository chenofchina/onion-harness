"""Layer 6 - GUI shell (pywebview host).

The GUI is React inside a WebView2 control.  This module only: builds the local
server, locates the packaged bundle, and opens the window.  It contains no
rendering logic and consumes the same structured event stream as any other
client.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

from onion_harness.api.server import OnionHTTPServer
from onion_harness.api.service import OnionService
from onion_harness.harness.errors import sanitize_logger_name

# Declare DPI awareness at import time, before the GUI toolkit is imported.
# The window host queries DPI awareness when it initialises; setting it later
# leaves every window metric virtualised.
from onion_harness.app.windowing import enable_dpi_awareness, set_app_user_model_id

_DPI_MODE = enable_dpi_awareness()
# Must happen before any window exists so the taskbar uses our identity (and
# therefore our icon) instead of the host interpreter's.
_APP_ID_SET = set_app_user_model_id()

def _bundle_root() -> Path:
    """Directory holding packaged assets, source or frozen.

    PyInstaller sets ``sys._MEIPASS`` to the unpacked bundle root; in a source
    checkout the package directory is the root instead.
    """
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        return Path(frozen) / "onion_harness" / "app"
    return Path(__file__).parent


APP_ROOT = _bundle_root()
STATIC_DIR = APP_ROOT / "static"
ASSETS_DIR = APP_ROOT / "assets"
ICON_PATH = ASSETS_DIR / "onion.ico"


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    # Rename upstream log records as they pass through so nothing upstream-shaped
    # reaches the console.
    old_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.name = sanitize_logger_name(record.name)
        return record

    logging.setLogRecordFactory(factory)


def find_static_dir() -> Path:
    """Locate the built bundle, preferring an env override for development."""
    override = os.environ.get("ONION_STATIC_DIR")
    if override and Path(override).is_dir():
        return Path(override)
    if STATIC_DIR.is_dir() and (STATIC_DIR / "index.html").exists():
        return STATIC_DIR
    # Development fallback: repo layout frontend/dist.
    repo = Path(__file__).resolve().parents[2]
    candidate = repo / "frontend" / "dist"
    if candidate.is_dir():
        return candidate
    return STATIC_DIR


def run(host: str = "127.0.0.1", port: int = 0, data_dir: str | None = None, no_window: bool = False) -> None:
    dpi_mode = _DPI_MODE
    service = OnionService(data_dir=data_dir)
    service.start()

    static_dir = find_static_dir()
    server = OnionHTTPServer(service, static_dir=static_dir)
    url = server.start(host=host, port=port)

    print(f"[onion] serving {url}")
    print(f"[onion] data directory: {service.data_dir}")
    if not no_window:
        print(f"[onion] dpi awareness: {dpi_mode}")
    if not (static_dir / "index.html").exists():
        print(
            "[onion] WARNING: GUI bundle missing. Build it with:\n"
            "        cd frontend && npm install && npm run build",
            file=sys.stderr,
        )

    if no_window:
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
            service.stop()
        return

    try:
        import webview

        class WindowApi:
            """Minimal bridge for the custom frameless title bar."""

            def __init__(self) -> None:
                self._window = None

            def bind(self, window) -> None:
                self._window = window

            def minimize(self) -> bool:
                if self._window is not None:
                    self._window.minimize()
                    return True
                return False

            def toggle_maximize(self) -> bool:
                if self._window is None:
                    return False
                try:
                    if self._window.state == "maximized":
                        self._window.restore()
                    else:
                        self._window.maximize()
                except Exception:  # noqa: BLE001
                    return False
                return True

            def close(self) -> bool:
                if self._window is not None:
                    self._window.destroy()
                    return True
                return False

            def toggle_fullscreen(self) -> bool:
                """Toggle a work-area-filling window (taskbar stays visible).

                The toolkit's own fullscreen is display-exclusive and covers the
                taskbar, which is not what "full screen" should mean here. This
                maximizes against the work area instead, the same bounds the
                window-procedure subclass enforces.
                """
                if self._window is None:
                    return False
                try:
                    import ctypes

                    hwnd = int(self._window.native.Handle.ToInt32())
                    user32 = ctypes.windll.user32
                    user32.IsZoomed.argtypes = [ctypes.c_void_p]
                    zoomed = bool(user32.IsZoomed(hwnd))
                    # SC_MAXIMIZE / SC_RESTORE
                    user32.SendMessageW(
                        hwnd, 0x0112, 0xF030 if not zoomed else 0xF120, 0
                    )
                    return not zoomed
                except Exception:  # noqa: BLE001
                    return False

            def is_fullscreen(self) -> bool:
                """True while the window fills its work area (i.e. maximized)."""
                try:
                    import ctypes

                    hwnd = int(self._window.native.Handle.ToInt32())
                    user32 = ctypes.windll.user32
                    user32.IsZoomed.argtypes = [ctypes.c_void_p]
                    return bool(user32.IsZoomed(hwnd))
                except Exception:  # noqa: BLE001
                    return False

            def is_maximized(self) -> bool:
                try:
                    return bool(self._window and self._window.state == "maximized")
                except Exception:  # noqa: BLE001
                    return False

            def pick_directory(self) -> list:
                """Native folder picker. Returns [] when the user cancels."""
                if self._window is None:
                    return []
                try:
                    result = self._window.create_file_dialog(
                        webview.FOLDER_DIALOG, directory=str(Path.home())
                    )
                    return list(result or [])
                except Exception:  # noqa: BLE001
                    return []

            def pick_files(self) -> list:
                """Native multi-file picker, for attaching files as context."""
                if self._window is None:
                    return []
                try:
                    result = self._window.create_file_dialog(
                        webview.OPEN_DIALOG, allow_multiple=True, directory=str(Path.home())
                    )
                    return list(result or [])
                except Exception:  # noqa: BLE001
                    return []

        api = WindowApi()

        # Derive the initial geometry from the monitor work area. Without an
        # explicit position Windows drops a frameless window wherever the last
        # one was, often half off-screen at the bottom-right.
        from onion_harness.app.windowing import (
            FramelessChrome,
            center_geometry,
            hwnd_of,
            is_supported,
            min_size_for_screen,
            place_window,
            reset_placement,
        )

        geometry = center_geometry(1440, 920)
        window = webview.create_window(
            "Onion Harness",
            url,
            width=geometry["width"],
            height=geometry["height"],
            x=geometry["x"],
            y=geometry["y"],
            min_size=min_size_for_screen(),
            background_color="#0f1417",
            text_select=True,
            # Android-style window: no native chrome. Drag and resize are
            # restored natively by FramelessChrome once the HWND exists.
            frameless=True,
            easy_drag=False,
            shadow=True,
            js_api=api,
        )
        api.bind(window)

        chrome_holder: dict = {}

        def install_chrome() -> None:
            """Subclass the window procedure and restore native sizing."""
            if not is_supported():
                return
            hwnd = hwnd_of(window)
            if not hwnd:
                return

            chrome = chrome_holder.get("chrome")
            if chrome is None:
                chrome = FramelessChrome(hwnd)
                # install() also re-adds WS_THICKFRAME; without it the window
                # looks frameless but cannot be resized or snapped.
                if chrome.install():
                    # Keep the reference alive for the window's lifetime; if this
                    # callback were collected, the next message would crash.
                    chrome_holder["chrome"] = chrome
                else:
                    chrome = None
            elif not chrome_holder.get("restyled"):
                # Re-assert the sizing frame: the host can reset window styles
                # while creating the WebView, which silently drops resize.
                chrome.ensure_resizable()
            chrome_holder["restyled"] = True
            if not chrome_holder.get("icon"):
                from onion_harness.app.windowing import apply_window_icon

                if ICON_PATH.is_file() and apply_window_icon(hwnd, ICON_PATH):
                    chrome_holder["icon"] = True

            # Re-place after the handle exists. Creation-time coordinates are
            # interpreted in whatever space the toolkit's DPI awareness implies,
            # which is not necessarily the one we measured.
            if not chrome_holder.get("placed"):
                # Windows restores the previous placement for a matching window,
                # so a session that ended maximized could reopen that way.
                reset_placement(hwnd)
                if place_window(hwnd, 1440, 920):
                    chrome_holder["placed"] = True

        def on_shown() -> None:
            install_chrome()

        def bind_host_bridge() -> None:
            # We must not return a value: the host's event dispatcher collects
            # handler results into a set, and a dict is unhashable.
            window.evaluate_js(
                "window.onionHost = window.pywebview && window.pywebview.api;"
                "document.documentElement.classList.add('onion-hosted');"
            )

        window.events.shown += on_shown
        window.events.loaded += bind_host_bridge

        def on_shown() -> None:
            install_chrome()

        def bind_host_bridge() -> None:
            # We must not return a value: the host's event dispatcher collects
            # handler results into a set, and a dict is unhashable.
            window.evaluate_js(
                "window.onionHost = window.pywebview && window.pywebview.api;"
                "document.documentElement.classList.add('onion-hosted');"
            )

        window.events.shown += on_shown
        window.events.loaded += bind_host_bridge

        # Fallback: some backends raise `shown` before the handle is queryable.
        window.events.loaded += install_chrome
        webview.start(debug=bool(os.environ.get("ONION_DEBUG")), private_mode=False)
        chrome = chrome_holder.get("chrome")
        if chrome is not None:
            try:
                chrome.uninstall()
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
        del window
    finally:
        server.stop()
        service.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onion-harness", description="Onion Harness")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--data-dir", default=None, help="override the data directory")
    parser.add_argument("--no-window", action="store_true", help="serve only, do not open a window")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(args.verbose)
    run(args.host, args.port, args.data_dir, args.no_window)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())