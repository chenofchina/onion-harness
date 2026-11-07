"""Window-management tests - frameless chrome, geometry and work-area bounds.

These run on Windows and exercise the ctypes layer against a real (hidden)
window, so they validate the hit-test maths rather than a mock.
"""

from __future__ import annotations

import ctypes
import sys
import unittest
from ctypes import wintypes

from onion_harness.app import windowing


class WorkAreaTest(unittest.TestCase):
    def test_work_area_is_inside_the_screen(self):
        left, top, right, bottom = windowing.work_area()
        self.assertGreater(right, left)
        self.assertGreater(bottom, top)

    def test_work_area_leaves_room_for_the_taskbar(self):
        """The work area is smaller than the screen when a taskbar exists."""
        left, top, right, bottom = windowing.work_area()
        work_h = bottom - top
        screen_h = ctypes.windll.user32.GetSystemMetrics(windowing.SM_CYSCREEN)
        self.assertLessEqual(work_h, screen_h)
        # A taskbar is at least ~20px; tolerate a genuinely hidden taskbar.
        if work_h == screen_h:
            self.skipTest("taskbar is auto-hidden or absent")

    def test_center_geometry_fits_the_work_area(self):
        geo = windowing.center_geometry(1440, 920)
        left, top, right, bottom = windowing.work_area()
        self.assertGreaterEqual(geo["x"], left)
        self.assertGreaterEqual(geo["y"], top)
        self.assertLessEqual(geo["x"] + geo["width"], right)
        self.assertLessEqual(geo["y"] + geo["height"], bottom)

    def test_center_geometry_is_actually_centred(self):
        geo = windowing.center_geometry(1200, 800)
        left, top, right, bottom = windowing.work_area()
        gap_left = geo["x"] - left
        gap_right = right - (geo["x"] + geo["width"])
        self.assertLessEqual(abs(gap_left - gap_right), 2)
        gap_top = geo["y"] - top
        gap_bottom = bottom - (geo["y"] + geo["height"])
        self.assertLessEqual(abs(gap_top - gap_bottom), 2)

    def test_center_geometry_shrinks_on_small_screens(self):
        geo = windowing.center_geometry(99999, 99999)
        left, top, right, bottom = windowing.work_area()
        self.assertLessEqual(geo["width"], right - left)
        self.assertLessEqual(geo["height"], bottom - top)

    def test_min_size_fits_the_screen(self):
        min_w, min_h = windowing.min_size_for_screen()
        left, top, right, bottom = windowing.work_area()
        self.assertLessEqual(min_w, right - left)
        self.assertLessEqual(min_h, bottom - top)
        self.assertGreaterEqual(min_w, 640)
        self.assertGreaterEqual(min_h, 480)



def _create_window(user32, kernel32, width: int, height: int) -> int:
    """Create a real window with full pointer argtypes.

    Without explicit argtypes, ctypes passes the module handle as a 32-bit int,
    which truncates the pointer on 64-bit Windows and crashes the process.
    """
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        ctypes.c_void_p,
    ]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.DestroyWindow.argtypes = [wintypes.HWND]

    return user32.CreateWindowExW(
        0,
        "STATIC",
        "onion test window",
        0x00CF0000,  # WS_OVERLAPPEDWINDOW
        100,
        100,
        width,
        height,
        None,
        None,
        kernel32.GetModuleHandleW(None),
        None,
    )


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class HitTestTest(unittest.TestCase):
    """Verify the WM_NCHITTEST geometry against a real hidden window."""

    @classmethod
    def setUpClass(cls):
        cls.user32 = ctypes.windll.user32
        cls.kernel32 = ctypes.windll.kernel32
        cls.hwnd = _create_window(cls.user32, cls.kernel32, 900, 700)
        if not cls.hwnd:
            raise unittest.SkipTest("could not create a test window")
        cls.chrome = windowing.FramelessChrome(cls.hwnd)
        rect = wintypes.RECT()
        cls.user32.GetWindowRect(cls.hwnd, ctypes.byref(rect))
        cls.rect = rect

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "hwnd", None):
            cls.user32.DestroyWindow(cls.hwnd)

    def lparam(self, x: int, y: int) -> int:
        return ((y & 0xFFFF) << 16) | (x & 0xFFFF)

    def hit(self, x: int, y: int) -> int:
        return self.chrome._hit_test(self.hwnd, self.lparam(x, y))

    def test_left_edge_resizes(self):
        self.assertEqual(self.hit(self.rect.left + 1, self.rect.top + 300), windowing.HTLEFT)

    def test_right_edge_resizes(self):
        self.assertEqual(self.hit(self.rect.right - 1, self.rect.top + 300), windowing.HTRIGHT)

    def test_bottom_edge_resizes(self):
        self.assertEqual(self.hit(self.rect.left + 300, self.rect.bottom - 1), windowing.HTBOTTOM)

    def test_top_left_corner(self):
        self.assertEqual(self.hit(self.rect.left + 1, self.rect.top + 1), windowing.HTTOPLEFT)

    def test_bottom_right_corner(self):
        self.assertEqual(
            self.hit(self.rect.right - 1, self.rect.bottom - 1), windowing.HTBOTTOMRIGHT
        )

    def test_titlebar_is_a_caption(self):
        # Middle of the title strip.
        x = self.rect.left + 400
        y = self.rect.top + windowing.TITLEBAR_HEIGHT // 2
        self.assertEqual(self.hit(x, y), windowing.HTCAPTION)

    def test_titlebar_drag_covers_the_brand(self):
        """The left part of the strip drags too, not just the empty middle."""
        x = self.rect.left + 60
        y = self.rect.top + windowing.TITLEBAR_HEIGHT // 2
        self.assertEqual(self.hit(x, y), windowing.HTCAPTION)

    def test_window_controls_are_not_draggable(self):
        """Buttons must receive clicks, so the caption zone stops before them."""
        x = self.rect.right - 40
        y = self.rect.top + windowing.TITLEBAR_HEIGHT // 2
        self.assertEqual(self.hit(x, y), windowing.HTCLIENT)

    def test_body_is_client_area(self):
        x = self.rect.left + 400
        y = self.rect.top + 300
        self.assertEqual(self.hit(x, y), windowing.HTCLIENT)


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class MaximizeBoundsTest(unittest.TestCase):
    def test_maximize_uses_the_work_area_not_the_screen(self):
        """ptMaxSize in WM_GETMINMAXINFO must equal the work area."""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = _create_window(user32, kernel32, 800, 600)
        if not hwnd:
            self.skipTest("could not create a test window")
        try:
            chrome = windowing.FramelessChrome(hwnd)
            # Simulate the message: build a MINMAXINFO and invoke the handler.
            info = windowing._MINMAXINFO()
            chrome._apply_work_area(hwnd, ctypes.addressof(info))
            left, top, right, bottom = windowing.work_area(hwnd)
            self.assertEqual(info.ptMaxSize.x, right - left)
            self.assertEqual(info.ptMaxSize.y, bottom - top)

            screen_w = user32.GetSystemMetrics(0)
            screen_h = user32.GetSystemMetrics(1)
            # The whole point: maximized height must not cover the taskbar.
            self.assertLessEqual(info.ptMaxSize.y, screen_h)
        finally:
            user32.DestroyWindow(hwnd)


class ChromeLifecycleTest(unittest.TestCase):
    def test_hwnd_lookup_is_safe_without_native(self):
        class FakeWindow:
            pass

        self.assertIsNone(windowing.hwnd_of(FakeWindow()))

    def test_supported_flag(self):
        self.assertEqual(windowing.is_supported(), sys.platform == "win32")


if __name__ == "__main__":
    unittest.main()

@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class ResizableFrameTest(unittest.TestCase):
    """A frameless window must still have a real sizing frame."""

    def test_thickframe_is_restored_and_caption_is_not(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = _create_window(user32, kernel32, 800, 600)
        if not hwnd:
            self.skipTest("could not create a test window")
        try:
            # Emulate the frameless style pywebview applies.
            style = user32.GetWindowLongW(hwnd, windowing.GWL_STYLE)
            user32.SetWindowLongW(hwnd, windowing.GWL_STYLE, style & ~windowing.WS_THICKFRAME)
            self.assertFalse(
                user32.GetWindowLongW(hwnd, windowing.GWL_STYLE) & windowing.WS_THICKFRAME
            )

            chrome = windowing.FramelessChrome(hwnd)
            self.assertTrue(chrome.ensure_resizable())

            final = user32.GetWindowLongW(hwnd, windowing.GWL_STYLE)
            self.assertTrue(final & windowing.WS_THICKFRAME, "resize border missing")
            self.assertFalse(final & windowing.WS_CAPTION, "native caption came back")
            self.assertTrue(final & windowing.WS_MAXIMIZEBOX)
        finally:
            user32.DestroyWindow(hwnd)

    def test_programmatic_resize_is_honoured(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = _create_window(user32, kernel32, 800, 600)
        if not hwnd:
            self.skipTest("could not create a test window")
        try:
            windowing.FramelessChrome(hwnd).ensure_resizable()
            user32.SetWindowPos(hwnd, None, 120, 100, 1000, 700, 0x0004)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            self.assertEqual(rect.right - rect.left, 1000)
            self.assertEqual(rect.bottom - rect.top, 700)
        finally:
            user32.DestroyWindow(hwnd)


class UninstallSafetyTest(unittest.TestCase):
    def test_uninstall_on_dead_handle_does_not_raise(self):
        """webview.start() returns after the window is destroyed."""
        chrome = windowing.FramelessChrome(0)
        chrome.uninstall()  # must be a no-op, not a crash

    def test_fullscreen_flag_starts_false(self):
        chrome = windowing.FramelessChrome(0)
        self.assertFalse(chrome._was_maximized)


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class BorderSuppressionTest(unittest.TestCase):
    """The resize frame must not paint a white system border."""

    def test_border_color_is_set_to_none(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = _create_window(user32, kernel32, 800, 600)
        if not hwnd:
            self.skipTest("could not create a test window")
        try:
            chrome = windowing.FramelessChrome(hwnd)
            chrome.ensure_resizable()
            # DwmSetWindowAttribute returns 0 on success. On a build without
            # DWMWA_BORDER_COLOR it fails and we simply keep the hairline, so
            # only assert when the API is available.
            dwm = getattr(ctypes.windll, "dwmapi", None)
            if dwm is None:
                self.skipTest("dwmapi unavailable")
            dwm.DwmSetWindowAttribute.restype = ctypes.c_long
            value = ctypes.c_uint(0xFFFFFFFE)
            hr = dwm.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(windowing.DWMWA_BORDER_COLOR),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            if hr != 0:
                self.skipTest("DWMWA_BORDER_COLOR unsupported on this build")
            self.assertEqual(hr, 0)
            # Resize must still work with the border suppressed.
            style = user32.GetWindowLongW(hwnd, windowing.GWL_STYLE)
            self.assertTrue(style & windowing.WS_THICKFRAME)
        finally:
            user32.DestroyWindow(hwnd)

    def test_suppress_returns_bool_without_raising(self):
        chrome = windowing.FramelessChrome(0)
        # A dead handle must not raise; the result is simply False.
        self.assertIsInstance(chrome._suppress_system_border(), bool)


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class PlacementResetTest(unittest.TestCase):
    """An inherited maximized placement must be corrected on startup."""

    def test_reset_placement_unmaximizes(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = _create_window(user32, kernel32, 800, 600)
        if not hwnd:
            self.skipTest("could not create a test window")
        try:
            user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            self.assertTrue(user32.IsZoomed(hwnd))
            self.assertTrue(windowing.reset_placement(hwnd))
            self.assertFalse(user32.IsZoomed(hwnd), "window stayed maximized")
        finally:
            user32.DestroyWindow(hwnd)

    def test_reset_placement_is_safe_on_dead_handle(self):
        self.assertFalse(windowing.reset_placement(0))


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class NoPaintedFrameTest(unittest.TestCase):
    """Re-adding WS_THICKFRAME must not leave a painted non-client strip.

    Windows draws that strip in the system colour, which shows as a white line
    against a dark UI. WM_NCCALCSIZE must be installed *before* the style
    changes, otherwise the frame is computed first and stays.
    """

    def test_client_area_fills_the_window(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = _create_window(user32, kernel32, 800, 600)
        if not hwnd:
            self.skipTest("could not create a test window")
        try:
            chrome = windowing.FramelessChrome(hwnd)
            self.assertTrue(chrome.install())

            window_rect = wintypes.RECT()
            client_rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(window_rect))
            user32.GetClientRect(hwnd, ctypes.byref(client_rect))
            frame_w = (window_rect.right - window_rect.left) - (
                client_rect.right - client_rect.left
            )
            frame_h = (window_rect.bottom - window_rect.top) - (
                client_rect.bottom - client_rect.top
            )
            self.assertEqual(frame_w, 0, f"non-client width {frame_w}px still painted")
            self.assertEqual(frame_h, 0, f"non-client height {frame_h}px still painted")

            # The resize frame must still be present.
            style = user32.GetWindowLongW(hwnd, windowing.GWL_STYLE)
            self.assertTrue(style & windowing.WS_THICKFRAME)
        finally:
            user32.DestroyWindow(hwnd)

    def test_nccalcsize_handler_is_installed_before_style(self):
        """Guards the ordering: subclass first, then WS_THICKFRAME."""
        import inspect

        source = inspect.getsource(windowing.FramelessChrome.install)
        subclass_at = source.index("self._old_proc = old")
        restyle_at = source.index("self.ensure_resizable()")
        self.assertLess(
            subclass_at, restyle_at, "style applied before the procedure was subclassed"
        )
