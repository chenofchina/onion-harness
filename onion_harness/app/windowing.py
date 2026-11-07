"""Native window chrome for the frameless shell (Windows).

A ``FormBorderStyle.None`` window has no non-client area, so Windows provides no
resize borders, no caption drag, and maximizes to the *screen* rather than the
*work area* (covering the taskbar). pywebview does not install hit-testing for
frameless windows, so this module subclasses the window procedure to restore
standard behaviour while keeping the app's own Material 3 title bar:

* ``WM_NCHITTEST`` - a resize ring around the edges, and ``HTCAPTION`` over the
  title-bar strip (excluding the window-control buttons) so dragging, snapping
  and double-click-maximize are all native. No JS latency, no jitter.
* ``WM_NCCALCSIZE`` - size the non-client area to zero. Re-adding
  ``WS_THICKFRAME`` for resizeability otherwise produces a 16px frame that
  Windows paints in its system colour, which reads as a white strip against a
  dark UI. Zeroing it keeps the resize hit-testing while removing the paint.
* ``WM_GETMINMAXINFO`` - constrain maximize/restore to the monitor work area so
  the taskbar stays visible.
* ``WM_DPICHANGED`` is left to Windows; sizes are scaled from the live DPI.

The subclass callback must stay referenced for the lifetime of the window;
Python would otherwise collect it and the process would crash on the next
message.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Callable, Optional, Tuple

WM_NCHITTEST = 0x0084
WM_NCCALCSIZE = 0x0083
WM_GETMINMAXINFO = 0x0024
WM_DPICHANGED = 0x02E0

HTCLIENT = 1
HTCAPTION = 2
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

GWL_WNDPROC = -4
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MAXIMIZEBOX = 0x00010000
WS_MINIMIZEBOX = 0x00020000
WS_SYSMENU = 0x00080000
SWP_FRAMECHANGED = 0x0020
# DWM window attributes (Windows 11).
DWMWA_BORDER_COLOR = 34
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_COLOR_NONE = 0xFFFFFFFE
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SM_CXSCREEN = 0
SM_CYSCREEN = 1

MONITOR_DEFAULTTONEAREST = 2

# Default logical measurements, matched to the CSS in styles.css.
TITLEBAR_HEIGHT = 38
CONTROLS_WIDTH = 138
RESIZE_BORDER = 6
MIN_WIDTH = 940
MIN_HEIGHT = 620


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", _POINT),
        ("ptMaxSize", _POINT),
        ("ptMaxPosition", _POINT),
        ("ptMinTrackSize", _POINT),
        ("ptMaxTrackSize", _POINT),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _get_x_lparam(lparam: int) -> int:
    value = lparam & 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def _get_y_lparam(lparam: int) -> int:
    value = (lparam >> 16) & 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def enable_dpi_awareness() -> str:
    """Declare per-monitor-v2 DPI awareness for this process.

    Must run *before* any window is created. A DPI-unaware process receives
    virtualized coordinates: ``GetSystemMetrics``/``SystemParametersInfo`` report
    the scaled logical screen while the window manager places the real window in
    physical pixels, so computed positions land off-target and sizing looks
    stuck. Declaring awareness makes every API agree on physical pixels.

    Returns the mode that was applied, for logging.
    """
    if sys.platform != "win32":
        return "n/a"
    user32 = ctypes.windll.user32
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:  # noqa: BLE001 - pre-1703 Windows
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:  # noqa: BLE001
        pass
    try:
        user32.SetProcessDPIAware()
        return "system"
    except Exception:  # noqa: BLE001
        return "unaware"


def work_area(hwnd: Optional[int] = None) -> Tuple[int, int, int, int]:
    """Return (left, top, right, bottom) of the monitor work area.

    Values are physical pixels; call :func:`enable_dpi_awareness` first so this
    matches what the window manager uses when placing the window.
    """
    user32 = ctypes.windll.user32
    monitor = user32.MonitorFromWindow(
        hwnd or user32.GetDesktopWindow(), MONITOR_DEFAULTTONEAREST
    )
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        rc = info.rcWork
        if rc.right > rc.left and rc.bottom > rc.top:
            return rc.left, rc.top, rc.right, rc.bottom
    left, top, right, bottom = _system_work_area()
    return left, top, right, bottom


def _system_work_area() -> Tuple[int, int, int, int]:
    rect = _RECT()
    try:
        # SPI_GETWORKAREA
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            if rect.right > rect.left and rect.bottom > rect.top:
                return rect.left, rect.top, rect.right, rect.bottom
    except Exception:  # noqa: BLE001
        pass
    user32 = ctypes.windll.user32
    width = user32.GetSystemMetrics(SM_CXSCREEN)
    height = user32.GetSystemMetrics(SM_CYSCREEN)
    return 0, 0, width, height


def center_geometry(
    width: int = 1440, height: int = 920, margin: int = 48
) -> dict:
    """A centred window rect that always fits inside the work area."""
    left, top, right, bottom = work_area()
    avail_w = max(320, right - left - margin * 2)
    avail_h = max(240, bottom - top - margin * 2)
    win_w = min(width, avail_w)
    win_h = min(height, avail_h)
    return {
        "width": win_w,
        "height": win_h,
        "x": left + (right - left - win_w) // 2,
        "y": top + (bottom - top - win_h) // 2,
    }


def min_size_for_screen() -> Tuple[int, int]:
    """Adapt the minimum size so the window can fit on small laptops."""
    left, top, right, bottom = work_area()
    return (
        min(MIN_WIDTH, max(640, right - left - 80)),
        min(MIN_HEIGHT, max(480, bottom - top - 80)),
    )


class FramelessChrome:
    """Restores native drag/resize/maximize on a frameless window."""

    def __init__(
        self,
        hwnd: int,
        titlebar_height: int = TITLEBAR_HEIGHT,
        controls_width: int = CONTROLS_WIDTH,
        border: int = RESIZE_BORDER,
        on_maximize_change: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.hwnd = int(hwnd)
        self._titlebar = titlebar_height
        self._controls = controls_width
        self._border = border
        self._on_maximize_change = on_maximize_change
        self._old_proc = None
        self._callback = None
        self._was_maximized = False
        self._user32 = ctypes.windll.user32
        self._is_aware = self._detect_awareness()

        is_64 = ctypes.sizeof(ctypes.c_void_p) == 8
        self._set_long = (
            self._user32.SetWindowLongPtrW if is_64 else self._user32.SetWindowLongW
        )
        self._set_long.restype = ctypes.c_void_p
        self._set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        self._call_proc = self._user32.CallWindowProcW
        self._call_proc.restype = ctypes.c_ssize_t
        self._call_proc.argtypes = [
            ctypes.c_void_p,
            wintypes.HWND,
            wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        # Window handles are 64-bit; without these signatures ctypes truncates
        # them to 32-bit and the call faults.
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self._user32.MonitorFromWindow.restype = wintypes.HANDLE
        self._user32.GetMonitorInfoW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_MONITORINFO),
        ]
        self._user32.IsZoomed.argtypes = [wintypes.HWND]

    # -- lifecycle ----------------------------------------------------------
    def ensure_resizable(self) -> bool:
        """Give the frameless window a real sizing frame.

        ``FormBorderStyle.None`` strips ``WS_THICKFRAME``, and without it Windows
        draws no resize border and shows no resize cursor even though
        ``WM_NCHITTEST`` returns the edge codes. The frame is re-added here
        without a caption, so the app keeps its own title bar.
        """
        try:
            style = self._user32.GetWindowLongW(self.hwnd, GWL_STYLE)
            wanted = (
                style
                | WS_THICKFRAME
                | WS_MAXIMIZEBOX
                | WS_MINIMIZEBOX
                | WS_SYSMENU
            ) & ~WS_CAPTION
            self._user32.SetWindowLongW(self.hwnd, GWL_STYLE, wanted)
            # SWP_FRAMECHANGED makes Windows recalculate the non-client area so
            # the new frame takes effect immediately.
            self._user32.SetWindowPos(
                self.hwnd, None, 0, 0, 0, 0,
                SWP_FRAMECHANGED | SWP_NOZORDER | SWP_NOACTIVATE | 0x0001 | 0x0002,
            )
            # Re-adding WS_THICKFRAME makes the DWM paint a 1px system border,
            # which shows as a white line against the dark UI in light mode.
            # Windows 11 lets us suppress it outright.
            self._suppress_system_border()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _suppress_system_border(self) -> bool:
        """Ask DWM to draw no window border.

        Falls back silently on builds without DWMWA_BORDER_COLOR; the window is
        still resizable, it just keeps the system hairline.
        """
        try:
            dwm = ctypes.windll.dwmapi
            value = ctypes.c_uint(DWMWA_COLOR_NONE)
            hr = dwm.DwmSetWindowAttribute(
                wintypes.HWND(self.hwnd),
                ctypes.c_uint(DWMWA_BORDER_COLOR),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            return hr == 0
        except Exception:  # noqa: BLE001
            return False

    def set_dark_titlebar(self, dark: bool) -> bool:
        """Match the DWM titlebar/dark chrome to the app theme where possible."""
        try:
            dwm = ctypes.windll.dwmapi
            value = ctypes.c_int(1 if dark else 0)
            hr = dwm.DwmSetWindowAttribute(
                wintypes.HWND(self.hwnd),
                ctypes.c_uint(DWMWA_USE_IMMERSIVE_DARK_MODE),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            return hr == 0
        except Exception:  # noqa: BLE001
            return False

    def install(self) -> bool:
        if self._callback is not None:
            return True
        # Subclass FIRST, then add WS_THICKFRAME. If the style changes first,
        # Windows computes the new 16px non-client frame before our
        # WM_NCCALCSIZE handler exists, and that frame is then cached and painted
        # as a white strip.
        proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t
        )

        def handler(hwnd, msg, wparam, lparam):  # noqa: ANN001
            try:
                if msg == WM_NCHITTEST:
                    result = self._hit_test(hwnd, lparam)
                    if result is not None:
                        return result
                elif msg == WM_NCCALCSIZE:
                    # wparam TRUE means lparam is an NCCALCSIZE_PARAMS; return 0
                    # so the client area fills the whole window and no frame is
                    # painted. Not maximized: Windows inserts a border here to
                    # keep maximized content off screen edges.
                    if wparam and not self._is_maximized(hwnd):
                        return 0
                elif msg == WM_GETMINMAXINFO:
                    self._apply_work_area(hwnd, lparam)
            except Exception:  # noqa: BLE001 - never break the message loop
                pass
            return self._call_proc(self._old_proc, hwnd, msg, wparam, lparam)

        self._callback = proc_type(handler)
        old = self._set_long(
            self.hwnd, GWL_WNDPROC, ctypes.cast(self._callback, ctypes.c_void_p)
        )
        if not old:
            self._callback = None
            return False
        self._old_proc = old
        # Now that WM_NCCALCSIZE is ours, the frame can be added without
        # leaving a painted border.
        self.ensure_resizable()
        return True

    def uninstall(self) -> None:
        """Restore the original procedure, but only on a live window.

        ``webview.start()`` returns after the window is destroyed, so by then the
        HWND may be gone and writing to it would fault. Restoring is also
        unnecessary once destroyed, so skip it in that case.
        """
        if self._callback is None:
            return
        try:
            if self._user32.IsWindow(self.hwnd):
                self._set_long(
                    self.hwnd, GWL_WNDPROC, ctypes.cast(self._old_proc, ctypes.c_void_p)
                )
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass
        self._old_proc = None
        self._callback = None

    # -- geometry -----------------------------------------------------------
    def _scale(self, hwnd: int) -> float:
        """Pixels per logical unit for this window.

        Once the process is per-monitor DPI aware, window coordinates and CSS
        pixels are both physical, so no scaling is applied. A DPI-unaware
        process (awareness could not be set) still needs the window's DPI factor
        to line the hit regions up with the CSS layout.
        """
        if self._is_aware:
            return 1.0
        try:
            dpi = self._user32.GetDpiForWindow(hwnd)
            return (dpi or 96) / 96.0
        except Exception:  # noqa: BLE001 - older Windows
            return 1.0

    @staticmethod
    def _detect_awareness() -> bool:
        try:
            user32 = ctypes.windll.user32
            ctx = user32.GetThreadDpiAwarenessContext()
            return bool(user32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(-4)))
        except Exception:  # noqa: BLE001
            return False

    def _is_maximized(self, hwnd: int) -> bool:
        return bool(self._user32.IsZoomed(hwnd))

    def _hit_test(self, hwnd: int, lparam: int) -> Optional[int]:
        # Maximized windows have no draggable frame: everything is client area
        # except the title bar, which must still start a restore-drag.
        scale = self._scale(hwnd)
        x = _get_x_lparam(lparam)
        y = _get_y_lparam(lparam)
        rect = wintypes.RECT()
        self._user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left

        border = max(3, int(round(self._border * scale)))
        left = x - rect.left
        top = y - rect.top
        right = rect.right - x
        bottom = rect.bottom - y

        if not self._is_maximized(hwnd):
            on_left = left < border
            on_right = right < border
            on_top = top < border
            on_bottom = bottom < border
            if on_top and on_left:
                return HTTOPLEFT
            if on_top and on_right:
                return HTTOPRIGHT
            if on_bottom and on_left:
                return HTBOTTOMLEFT
            if on_bottom and on_right:
                return HTBOTTOMRIGHT
            if on_left:
                return HTLEFT
            if on_right:
                return HTRIGHT
            if on_top:
                return HTTOP
            if on_bottom:
                return HTBOTTOM

        # Title-bar strip, minus the window-control cluster on the right.
        title_h = int(round(self._titlebar * scale))
        controls_w = int(round(self._controls * scale))
        if top >= 0 and top < title_h and left < width - controls_w:
            return HTCAPTION
        return HTCLIENT

    def _apply_work_area(self, hwnd: int, lparam: int) -> None:
        info = ctypes.cast(lparam, ctypes.POINTER(_MINMAXINFO)).contents
        left, top, right, bottom = work_area(hwnd)
        monitor = self._user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mon_info = _MONITORINFO()
        mon_info.cbSize = ctypes.sizeof(_MONITORINFO)
        mon_left, mon_top = left, top
        if monitor and self._user32.GetMonitorInfoW(monitor, ctypes.byref(mon_info)):
            mon_left = mon_info.rcMonitor.left
            mon_top = mon_info.rcMonitor.top

        # Maximized bounds = the work area, expressed relative to the monitor.
        info.ptMaxPosition.x = left - mon_left
        info.ptMaxPosition.y = top - mon_top
        info.ptMaxSize.x = right - left
        info.ptMaxSize.y = bottom - top

        min_w, min_h = min_size_for_screen()
        scale = self._scale(hwnd)
        info.ptMinTrackSize.x = int(round(min_w * scale))
        info.ptMinTrackSize.y = int(round(min_h * scale))
        # ptMaxTrackSize is left to Windows so multi-monitor spans still work.

        maximized = self._is_maximized(hwnd)
        if maximized != self._was_maximized:
            self._was_maximized = maximized
            if self._on_maximize_change is not None:
                try:
                    self._on_maximize_change(maximized)
                except Exception:  # noqa: BLE001
                    pass


def place_window(hwnd: int, width: int = 1440, height: int = 920, margin: int = 48) -> bool:
    """Centre and size a live window inside its monitor's work area.

    Runs after creation because the DPI awareness in effect when the window is
    made determines the coordinate space the window manager expects. Measuring
    the work area for *this HWND* keeps the two in agreement regardless of what
    the host toolkit set.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    user32 = ctypes.windll.user32
    try:
        left, top, right, bottom = work_area(hwnd)
        avail_w = max(320, right - left - margin * 2)
        avail_h = max(240, bottom - top - margin * 2)
        win_w = min(width, avail_w)
        win_h = min(height, avail_h)
        x = left + (right - left - win_w) // 2
        y = top + (bottom - top - win_h) // 2
        # SWP_NOZORDER | SWP_NOACTIVATE
        user32.SetWindowPos(hwnd, None, x, y, win_w, win_h, 0x0004 | 0x0010)
        return True
    except Exception:  # noqa: BLE001
        return False


def reset_placement(hwnd: int) -> bool:
    """Force a normal (non-maximized) placement for a live window.

    Windows remembers the previous placement of a window whose class/title match
    what it saw before, so a session that ended maximized can reopen maximized
    even when the creation call asked for a normal window. Writing the placement
    explicitly clears that inherited state.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32

        class _WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.UINT),
                ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT),
                ("ptMinPosition", _POINT),
                ("ptMaxPosition", _POINT),
                ("rcNormalPosition", wintypes.RECT),
            ]

        placement = _WINDOWPLACEMENT()
        placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
        user32.GetWindowPlacement(
            wintypes.HWND(hwnd), ctypes.byref(placement)
        )
        if placement.showCmd != 1:  # SW_SHOWNORMAL
            placement.showCmd = 1
            user32.SetWindowPlacement(
                wintypes.HWND(hwnd), ctypes.byref(placement)
            )
        return True
    except Exception:  # noqa: BLE001
        return False


def set_app_user_model_id(app_id: str = "OnionHarness.Desktop") -> bool:
    """Give the process its own taskbar identity.

    Without this the taskbar groups the window under the host executable's
    identity, so it shows that interpreter's icon (Python) no matter what icon
    the window itself sets.
    """
    if sys.platform != "win32":
        return False
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(app_id)
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def apply_window_icon(hwnd: int, ico_path) -> bool:
    """Set both the window icon and its small variant."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        path = str(ico_path)
        if not os.path.isfile(path):  # noqa: F821 - os imported below
            return False
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        # IMAGE_ICON = 1, LR_LOADFROMFILE = 0x10
        big = user32.LoadImageW(None, path, 1, 0, 0, 0x10)
        small = user32.LoadImageW(None, path, 1, 16, 16, 0x10)
        if not big:
            return False
        # WM_SETICON: 0 = ICON_SMALL, 1 = ICON_BIG
        user32.SendMessageW(hwnd, 0x0080, 1, big)
        if small:
            user32.SendMessageW(hwnd, 0x0080, 0, small)
        return True
    except Exception:  # noqa: BLE001
        return False


def hwnd_of(window) -> Optional[int]:  # noqa: ANN001
    """Best-effort HWND lookup for a pywebview window."""
    native = getattr(window, "native", None)
    handle = getattr(native, "Handle", None)
    if handle is None:
        return None
    try:
        return int(handle.ToInt32() if hasattr(handle, "ToInt32") else handle)
    except Exception:  # noqa: BLE001
        return None


def is_supported() -> bool:
    return sys.platform == "win32"


__all__ = [
    "FramelessChrome",
    "enable_dpi_awareness",
    "center_geometry",
    "place_window",
    "set_app_user_model_id",
    "apply_window_icon",
    "reset_placement",
    "min_size_for_screen",
    "work_area",
    "hwnd_of",
    "is_supported",
]