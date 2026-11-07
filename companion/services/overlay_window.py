"""Win32 presenter: scrolling-log pill plus a status border with a calm
traveling glow.
Two click-through topmost windows, zero injection, zero hooks:
* pill - terminal-style lines anchored to the target window's top-right;
* border - a thin (~6px) frame drawn just inside the target window's edges,
  so it never spills onto a neighboring monitor. While scaling is pending
  the frame is blue, while active it is burgundy. A continuous gradient
  ribbon, a softer opposing highlight, and gentle breathing run on a
  presenter-side clock; state colors ease into one another without flashing.

Both windows are plain opaque popups shaped with window regions (no
layered windows: UpdateLayeredWindow proved unreliable on multi-GPU /
virtual-display systems, failing silently with error 87).

Never steals focus (NOACTIVATE everywhere, SW_SHOWNOACTIVATE, no foreground
calls). Any failure permanently disables the presenter so overlays can never
break scaling.
"""
from __future__ import annotations

import ctypes
import logging
import math
import threading
import time as _time
from ctypes import wintypes
from typing import Dict, List, Optional

logger = logging.getLogger("LSCompanion.OverlayWindow")


def _pump_idle_wait(seconds: float) -> None:
    """Idle wait for the presenter pump; indirection keeps the global time
    module unpatched by tests that mock time.sleep elsewhere."""
    _time.sleep(seconds)

WIDTH = 440
LINE_HEIGHT = 28
PAD_TOP = 12
PAD_LEFT = 44
MAX_DRAWN_LINES = 5
HEIGHT = PAD_TOP * 2 + LINE_HEIGHT * MAX_DRAWN_LINES
BORDER_PX = 6
BORDER_ANIM_MS = 16
BORDER_LAP_SECONDS = 5.2
BORDER_BREATHE_SECONDS = 4.8
BORDER_COLOR_SECONDS = 0.32
BORDER_GRADIENT_STEPS = 96
BORDER_TIMER_ID = 1

BG_COLOR = (24, 24, 24)
TEXT_COLOR = (235, 235, 235)
PENDING_COLOR = (26, 120, 255)
ACTIVE_COLOR = (145, 24, 62)

WS_POPUP = 0x80000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
SWP_NOZORDER = 0x0004
SWP_NOSIZE = 0x0000
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
WM_TIMER = 0x0113
WM_PAINT = 0x000F
WM_DESTROY = 0x0002
WM_USER_APPLY = 0x8001
WM_QUIT = 0x0012
WM_QUIT_MESSAGE = 0x0012
TRANSPARENT = 1
DT_SINGLELINE = 0x00000020
DT_END_ELLIPSIS = 0x00000020 | 0x00008000
MONITOR_DEFAULTTONEAREST = 2

_BORDER_COLORS = {
    "pending": PENDING_COLOR,
    "active": ACTIVE_COLOR,
}


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", _Rect),
                ("rcWork", _Rect), ("dwFlags", ctypes.c_uint32)]


class _PaintStruct(ctypes.Structure):
    # Win32 BOOL is 32 bits, unlike ctypes.c_bool. Using one-byte fields
    # allocates only 64 bytes on x64; BeginPaint writes the native 72 bytes
    # and corrupts the following allocation during the first overlay paint.
    _fields_ = [("hdc", ctypes.c_void_p),
                ("erase", wintypes.BOOL),
                ("paint", _Rect),
                ("restore", wintypes.BOOL),
                ("update", wintypes.BOOL),
                ("reserved", ctypes.c_ubyte * 32)]


class _GradientVertex(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long),
                ("red", ctypes.c_ushort), ("green", ctypes.c_ushort),
                ("blue", ctypes.c_ushort), ("alpha", ctypes.c_ushort)]


class _GradientRect(ctypes.Structure):
    _fields_ = [("first", ctypes.c_ulong), ("second", ctypes.c_ulong)]


def _border_tint(start: tuple, target: tuple, elapsed: float) -> tuple:
    progress = min(1.0, max(0.0, elapsed / BORDER_COLOR_SECONDS))
    eased = progress * progress * (3.0 - 2.0 * progress)
    return tuple(round(a + (b - a) * eased) for a, b in zip(start, target))


def _border_light(color: tuple, position: float, elapsed: float) -> tuple:
    """Continuous light around the perimeter, including across the wrap."""
    head = elapsed / BORDER_LAP_SECONDS
    delta = (position - head + 0.5) % 1.0 - 0.5
    width = 0.11 if delta < 0 else 0.035
    trail = math.exp(-0.5 * (delta / width) ** 2)
    opposite = (position - head) % 1.0 - 0.5
    echo = 0.2 * math.exp(-0.5 * (opposite / 0.13) ** 2)
    light = min(1.0, 0.08 + 0.82 * trail + echo)
    breathe = 0.79 + 0.025 * math.sin(2 * math.pi * elapsed / BORDER_BREATHE_SECONDS)
    return tuple(round((channel + (255 - channel) * light) * breathe)
                 for channel in color)


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p,
                              ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)


class _WndClassEx(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
                ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm", ctypes.c_void_p)]


_api = None
_api_lock = threading.RLock()


def _user32():
    global _api
    with _api_lock:
        if _api is not None:
            return _api
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        msimg32 = ctypes.WinDLL("msimg32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
        u = user32
        u.RegisterClassExW.argtypes = [ctypes.POINTER(_WndClassEx)]
        u.RegisterClassExW.restype = ctypes.c_ushort
        u.CreateWindowExW.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p,
                                      ctypes.c_wchar_p, ctypes.c_ulong,
                                      ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int,
                                      ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_void_p]
        u.CreateWindowExW.restype = ctypes.c_void_p
        u.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                     ctypes.c_void_p, ctypes.c_void_p]
        u.DefWindowProcW.restype = ctypes.c_longlong
        u.DestroyWindow.argtypes = [ctypes.c_void_p]
        u.DestroyWindow.restype = ctypes.c_bool
        u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_uint]
        u.SetWindowPos.restype = ctypes.c_bool
        u.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_bool]
        u.SetWindowRgn.restype = ctypes.c_int
        u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.ShowWindow.restype = ctypes.c_bool
        u.InvalidateRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_bool]
        u.InvalidateRect.restype = ctypes.c_bool
        u.BeginPaint.argtypes = [ctypes.c_void_p,
                                 ctypes.POINTER(_PaintStruct)]
        u.BeginPaint.restype = ctypes.c_void_p
        u.EndPaint.argtypes = [ctypes.c_void_p,
                               ctypes.POINTER(_PaintStruct)]
        u.EndPaint.restype = ctypes.c_bool
        u.GetWindowRect.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(_Rect)]
        u.GetWindowRect.restype = ctypes.c_bool
        u.IsWindow.argtypes = [ctypes.c_void_p]
        u.IsWindow.restype = ctypes.c_bool
        u.IsIconic.argtypes = [ctypes.c_void_p]
        u.IsIconic.restype = ctypes.c_bool
        u.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                     ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetCursorPos.argtypes = [ctypes.POINTER(_Point)]
        u.GetCursorPos.restype = ctypes.c_bool
        u.MonitorFromPoint.argtypes = [_Point, ctypes.c_ulong]
        u.MonitorFromPoint.restype = ctypes.c_void_p
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p,
                                      ctypes.POINTER(_MonitorInfo)]
        u.GetMonitorInfoW.restype = ctypes.c_bool
        u.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_uint, ctypes.c_void_p]
        u.SetTimer.restype = ctypes.c_void_p
        u.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.KillTimer.restype = ctypes.c_bool
        u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                  ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.c_uint]
        u.GetMessageW.restype = ctypes.c_int
        u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.TranslateMessage.restype = ctypes.c_bool
        u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                   ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_uint, ctypes.c_uint]
        u.PeekMessageW.restype = ctypes.c_bool
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.DispatchMessageW.restype = ctypes.c_longlong
        u.PostThreadMessageW.argtypes = [ctypes.c_ulong, ctypes.c_uint,
                                         ctypes.c_void_p, ctypes.c_void_p]
        u.PostThreadMessageW.restype = ctypes.c_bool
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.PostQuitMessage.restype = None
        u.MsgWaitForMultipleObjectsEx.argtypes = [ctypes.c_ulong, ctypes.c_void_p,
                                                 ctypes.c_ulong, ctypes.c_ulong,
                                                 ctypes.c_ulong]
        u.MsgWaitForMultipleObjectsEx.restype = ctypes.c_ulong
        u.DrawTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
        u.DrawTextW.restype = ctypes.c_int
        u.GetDC.argtypes = [ctypes.c_void_p]
        u.GetDC.restype = ctypes.c_void_p
        u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.ReleaseDC.restype = ctypes.c_int
        u.FillRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_void_p]
        u.FillRect.restype = ctypes.c_int
        g = gdi32
        g.DeleteDC.argtypes = [ctypes.c_void_p]
        g.DeleteDC.restype = ctypes.c_bool
        g.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        g.CreateCompatibleDC.restype = ctypes.c_void_p
        g.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        g.CreateCompatibleBitmap.restype = ctypes.c_void_p
        g.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                            ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        g.BitBlt.restype = wintypes.BOOL
        g.CreateSolidBrush.argtypes = [ctypes.c_ulong]
        g.CreateSolidBrush.restype = ctypes.c_void_p
        g.FillRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p]
        g.FillRgn.restype = ctypes.c_bool
        g.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        g.CreateRoundRectRgn.restype = ctypes.c_void_p
        g.CreateRectRgn.argtypes = [ctypes.c_int] * 4
        g.CreateRectRgn.restype = ctypes.c_void_p
        g.CombineRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_int]
        g.CombineRgn.restype = ctypes.c_int
        g.CreateFontW.argtypes = ([ctypes.c_int] * 5 + [ctypes.c_ulong] * 6
                                  + [ctypes.c_ubyte] * 2 + [ctypes.c_wchar_p])
        g.CreateFontW.restype = ctypes.c_void_p
        g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        g.SelectObject.restype = ctypes.c_void_p
        g.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        g.SetBkMode.restype = ctypes.c_int
        g.SetTextColor.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        g.SetTextColor.restype = ctypes.c_ulong
        g.DeleteObject.argtypes = [ctypes.c_void_p]
        g.DeleteObject.restype = ctypes.c_bool
        msimg32.GradientFill.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GradientVertex),
                                        ctypes.c_ulong, ctypes.POINTER(_GradientRect),
                                        ctypes.c_ulong, ctypes.c_ulong]
        msimg32.GradientFill.restype = wintypes.BOOL
        _api = {"user32": u, "gdi32": g, "kernel32": kernel32,
                "msimg32": msimg32}
        return _api


def _handles():
    api = _user32()
    return api["user32"], api["gdi32"], api["kernel32"]


def describe_window(hwnd: int):
    """(width, height, title) for a window, or None when unavailable."""
    try:
        user32, _, _ = _handles()
        handle = ctypes.c_void_p(int(hwnd))
        handle = ctypes.c_void_p(int(hwnd))
        if not user32.IsWindow(handle) or user32.IsIconic(handle):
            return None
        rect = _Rect()
        if not user32.GetWindowRect(handle, ctypes.byref(rect)):
            return None
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        length = user32.GetWindowTextLengthW(handle)
        title = ""
        if length and length < 512:
            buffer = ctypes.create_unicode_buffer(length + 1)
            if user32.GetWindowTextW(handle, buffer, length + 1):
                title = buffer.value
        return (width, height, title)
    except Exception:
        return None


class Win32OverlayPresenter:
    """Callable presenter driving the pill and border windows."""

    PILL_CLASS = "LSCompanionOverlay"
    BORDER_CLASS = "LSCompanionOverlayBorder"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._usable = True
        self._reported_failure = False
        self._hwnd = None
        self._border_hwnd = None
        self._thread = None
        self._thread_id = None
        self._pending = None
        self._has_pending = False
        self._pill_visible = False
        self._border_visible = False
        self._pill_rgn = None
        self._border_rgn = None
        self._pill_lines: List[Dict] = []
        self._border_color: Optional[tuple] = None
        self._border_timer = False
        self._border_anim_t0 = 0.0
        self._border_color_from = PENDING_COLOR
        self._border_color_t0 = 0.0
        self._border_geometry = None
        self._border_buffer = None
        try:
            self._user32, self._gdi32, self._kernel32 = _handles()
        except Exception:
            self._note_failure("handles")
            self._user32 = None
            self._gdi32 = None
            self._kernel32 = None

    def _wakeup(self) -> None:
        """Nudge the pump thread to apply pending content, if it exists."""
        try:
            thread_id = self._thread_id
            if thread_id:
                self._user32.PostThreadMessageW(thread_id,
                                                WM_USER_APPLY, 0, 0)
        except Exception:
            pass

    def __call__(self, payload: Optional[Dict]) -> None:
        try:
            if not self._usable:
                return
            with self._lock:
                self._pending = None if payload is None else dict(payload)
                self._has_pending = True
            self._ensure_thread()
            self._wakeup()
        except Exception:
            self._note_failure("post")

    def _note_failure(self, where: str) -> None:
        """Log the first Win32 failure; silent death hid bugs for weeks."""
        try:
            if not self._reported_failure:
                self._reported_failure = True
                logger.exception("Overlay presenter disabled (%s)", where)
        except Exception:
            pass
        self._usable = False

    def close(self) -> None:
        # NOTE: called from arbitrary threads (indicator stop, app
        # shutdown). Never touch windows here: the pump thread owns them
        # and may be inside a paint. Signal exit and join; the pump
        # destroys its own windows on the way out.
        try:
            self._stop_border_timer()
            thread = None
            with self._lock:
                thread, self._thread = self._thread, None
                self._thread_id = None
                self._pill_lines = []
                self._border_color = None
                self._border_geometry = None
                self._pending = None
                self._has_pending = False
            if thread is not None:
                try:
                    thread.join(timeout=5.0)
                except Exception:
                    pass
        except Exception:
            pass

    def _teardown_windows(self) -> None:
        """Destroy pump-owned windows; runs only on the pump thread."""
        try:
            self._release_border_buffer()
            with self._lock:
                pill, border = self._hwnd, self._border_hwnd
                self._border_geometry = None
                self._hwnd = None
                self._border_hwnd = None
                self._pill_lines = []
                self._border_color = None
                self._pending = None
                self._has_pending = False
            for handle in (pill, border):
                if handle:
                    try:
                        if self._user32.IsWindow(ctypes.c_void_p(handle)):
                            self._user32.DestroyWindow(ctypes.c_void_p(handle))
                    except Exception:
                        pass
        except Exception:
            pass

    # -- pump thread ------------------------------------------------------
    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None or not self._usable:
                return
            thread = threading.Thread(target=self._pump, daemon=True,
                                      name="LSOverlayPump")
            self._thread = thread
            thread.start()

    def _pump(self) -> None:
        try:
            self._thread_id = self._kernel32.GetCurrentThreadId()
            if not self._create_windows():
                self._note_failure("create_windows")
                return
            # Drain anything queued before the message loop existed: the
            # first PostThreadMessageW can predate the queue and is lost.
            self._apply_pending()
            message = wintypes.MSG()
            while True:
                with self._lock:
                    gone = self._thread is None
                if gone:
                    break
                has_message = self._user32.PeekMessageW(
                    ctypes.byref(message), None, 0, 0, 1)  # PM_REMOVE
                if not has_message:
                    with self._lock:
                        if self._thread is None:
                            break
                        pending = self._has_pending
                    if pending:
                        self._apply_pending()
                        continue
                    # Wake on messages/timers instead of quantizing every
                    # animation frame onto a separate 10 ms polling sleep.
                    if self._user32.MsgWaitForMultipleObjectsEx(
                            0, None, BORDER_ANIM_MS, 0x04FF, 0x0004) == 0xFFFFFFFF:
                        _pump_idle_wait(0.01)
                    continue
                if message.message == WM_QUIT:
                    break
                if message.message == WM_USER_APPLY:
                    self._apply_pending()
                else:
                    self._user32.TranslateMessage(ctypes.byref(message))
                    self._user32.DispatchMessageW(ctypes.byref(message))
        except Exception:
            self._note_failure("pump")
        finally:
            try:
                self._teardown_windows()
            except Exception:
                pass
            self._thread_id = None
            self._border_timer = False

    # -- windows ------------------------------------------------------------
    def _make_window(self, class_name: str) -> Optional[int]:
        def _handler(hwnd, message, wparam, lparam):
            try:
                if message == WM_DESTROY:
                    return 0
                if self._thread is None:
                    return self._user32.DefWindowProcW(
                        ctypes.c_void_p(hwnd or 0), message,
                        ctypes.c_void_p(wparam or 0),
                        ctypes.c_void_p(lparam or 0))
                if message == WM_TIMER:
                    try:
                        if (wparam == BORDER_TIMER_ID
                                and self._border_timer
                                and self._border_hwnd
                                and hwnd == self._border_hwnd):
                            self._user32.InvalidateRect(
                                ctypes.c_void_p(hwnd), None, False)
                    except Exception:
                        pass
                    return 0
                if message == WM_PAINT:
                    self._paint_window(hwnd)
                    return 0
                return self._user32.DefWindowProcW(
                    ctypes.c_void_p(hwnd or 0), message,
                    ctypes.c_void_p(wparam or 0), ctypes.c_void_p(lparam or 0))
            except Exception:
                return 0

        try:
            proc = _WNDPROC(_handler)
            if not hasattr(self, "_procs"):
                self._procs = []
            self._procs.append(proc)
            module = self._kernel32.GetModuleHandleW(None)
            wnd_class = _WndClassEx()
            wnd_class.cbSize = ctypes.sizeof(_WndClassEx)
            wnd_class.lpfnWndProc = proc
            wnd_class.hInstance = module
            wnd_class.lpszClassName = class_name
            registered = self._user32.RegisterClassExW(ctypes.byref(wnd_class))
            if not registered and ctypes.get_last_error() != 1410:
                return None
            hwnd = self._user32.CreateWindowExW(
                WS_EX_TRANSPARENT | WS_EX_TOPMOST |
                WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
                class_name, "LS Companion", WS_POPUP,
                0, 0, WIDTH, HEIGHT, None, None, module, None)
            if not hwnd:
                return None
            return hwnd
        except Exception:
            return None

    def _paint_window(self, hwnd: int) -> None:
        """Paint the stored content; runs on the pump thread via WM_PAINT."""
        try:
            paint = _PaintStruct()
            hdc = self._user32.BeginPaint(ctypes.c_void_p(hwnd), ctypes.byref(paint))
            try:
                if not hdc:
                    return
                with self._lock:
                    pill_lines = list(self._pill_lines)
                    border_color = self._border_color
                if hwnd == self._hwnd:
                    self._paint_pill(hdc, pill_lines)
                elif hwnd == self._border_hwnd and border_color is not None:
                    self._paint_border_frame(hdc, hwnd, border_color)
            finally:
                try:
                    self._user32.EndPaint(ctypes.c_void_p(hwnd),
                                          ctypes.byref(paint))
                except Exception:
                    pass
        except Exception:
            self._note_failure("paint_window")

    def _release_border_buffer(self) -> None:
        buffer, self._border_buffer = self._border_buffer, None
        if buffer:
            dc, bitmap, original, _width, _height = buffer
            self._gdi32.SelectObject(dc, original)
            self._gdi32.DeleteObject(bitmap)
            self._gdi32.DeleteDC(dc)

    def _border_back_buffer(self, hdc, width: int, height: int):
        buffer = self._border_buffer
        if buffer and buffer[3:] == (width, height):
            return buffer[0]
        self._release_border_buffer()
        dc = self._gdi32.CreateCompatibleDC(hdc)
        bitmap = self._gdi32.CreateCompatibleBitmap(hdc, width, height) if dc else None
        original = self._gdi32.SelectObject(dc, bitmap) if bitmap else None
        if not original or original == ctypes.c_void_p(-1).value:
            if bitmap:
                self._gdi32.DeleteObject(bitmap)
            if dc:
                self._gdi32.DeleteDC(dc)
            raise OSError("Could not create the border back buffer")
        self._border_buffer = (dc, bitmap, original, width, height)
        return dc

    def _border_gradients(self, width: int, height: int, color: tuple, elapsed: float):
        # One native GradientFill call per orientation; no per-dot brushes.
        groups = [([], []), ([], [])]
        perimeter = 2.0 * (width + height)
        offset = 0
        for side, length in enumerate((width, height, width, height)):
            vertices, rectangles = groups[side % 2]
            steps = max(1, round(BORDER_GRADIENT_STEPS * length / perimeter))
            points = [round(length * i / steps) for i in range(steps + 1)]
            colors = [_border_light(color, (offset + point) / perimeter, elapsed)
                      for point in points]
            for i in range(steps):
                start, end = points[i:i + 2]
                for low, high, shade in ((0, 2, 0.38), (2, 4, 1.0), (4, 6, 0.24)):
                    first, last = colors[i], colors[i + 1]
                    if side == 0:
                        box = (start, low, end, high)
                    elif side == 1:
                        box = (width - high, start, width - low, end)
                    elif side == 2:
                        box = (width - end, height - high, width - start, height - low)
                        first, last = last, first
                    else:
                        box = (low, height - end, high, height - start)
                        first, last = last, first
                    index = len(vertices)
                    for x, y, tint in ((box[0], box[1], first), (box[2], box[3], last)):
                        channels = [round(channel * shade) << 8 for channel in tint]
                        vertices.append(_GradientVertex(x, y, *channels, 0))
                    rectangles.append(_GradientRect(index, index + 1))
            offset += length
        return groups

    def _paint_border_frame(self, hdc, hwnd: int, color: tuple) -> None:
        """Draw the continuous shaded ribbon offscreen, then copy its thin edges."""
        try:
            with self._lock:
                t0 = self._border_anim_t0
                color_from = self._border_color_from
                color_t0 = self._border_color_t0
            now = _time.monotonic()
            elapsed = max(now - t0, 0.0)
            tint = _border_tint(color_from, color, now - color_t0)
            rect = _Rect()
            if not self._user32.GetWindowRect(ctypes.c_void_p(hwnd),
                                              ctypes.byref(rect)):
                return
            total_w = rect.right - rect.left
            total_h = rect.bottom - rect.top
            if total_w <= 0 or total_h <= 0:
                return
            memory = self._border_back_buffer(hdc, total_w, total_h)
            gradient = _user32()["msimg32"].GradientFill
            for orientation, (vertices, rectangles) in enumerate(
                    self._border_gradients(total_w, total_h, tint, elapsed)):
                vertex_buffer = (_GradientVertex * len(vertices))(*vertices)
                mesh_buffer = (_GradientRect * len(rectangles))(*rectangles)
                if not gradient(memory, vertex_buffer, len(vertices), mesh_buffer,
                                len(rectangles), orientation):
                    raise OSError("Could not paint the border gradient")
            # Keep memory traffic proportional to the six-pixel frame even on
            # 4K displays; the application's center is never copied or painted.
            for x, y, width, height in (
                    (0, 0, total_w, BORDER_PX),
                    (0, total_h - BORDER_PX, total_w, BORDER_PX),
                    (0, BORDER_PX, BORDER_PX, total_h - 2 * BORDER_PX),
                    (total_w - BORDER_PX, BORDER_PX, BORDER_PX, total_h - 2 * BORDER_PX)):
                if not self._gdi32.BitBlt(hdc, x, y, width, height, memory, x, y, 0x00CC0020):
                    raise OSError("Could not present the border frame")
        except Exception:
            self._note_failure("paint_border_frame")

    def _set_region(self, hwnd: int, slot: str, region) -> bool:
        """Transfer a successful region to Windows; free it only on failure."""
        try:
            if not region:
                return False
            applied = self._user32.SetWindowRgn(
                ctypes.c_void_p(hwnd), region, True)
            if applied:
                setattr(self, slot, region)
                # Windows owns a successfully applied region and releases the
                # previous one. Deleting that handle here can double-free it.
                region = None
            if region:
                try:
                    self._gdi32.DeleteObject(region)
                except Exception:
                    pass
            return bool(applied)
        except Exception:
            return False

    def _create_windows(self) -> bool:
        try:
            self._hwnd = self._make_window(self.PILL_CLASS)
            self._border_hwnd = self._make_window(self.BORDER_CLASS)
            return bool(self._hwnd and self._border_hwnd)
        except Exception:
            return False

    # -- apply --------------------------------------------------------------
    def _apply_pending(self) -> None:
        try:
            with self._lock:
                if not self._has_pending:
                    return
                has_pending = dict(self._pending) if self._pending else None
                self._has_pending = False
            if has_pending is None:
                self._hide_all()
                return
            lines = has_pending.get("lines") or []
            border = has_pending.get("border")
            pulse = bool(has_pending.get("pulse"))
            anchor = has_pending.get("border_anchor_hwnd",
                                     has_pending.get("anchor_hwnd"))
            try:
                anchor = int(anchor) if anchor else None
            except (TypeError, ValueError):
                anchor = None
            if lines:
                self._draw_pill(lines, anchor)
            else:
                self._hide_pill()
            if border in _BORDER_COLORS and anchor:
                self._draw_border(border, anchor, pulse)
            else:
                self._hide_border()
        except Exception:
            self._note_failure("apply_pending")

    def _hide_all(self) -> None:
        self._hide_pill()
        self._hide_border()

    def _hide_pill(self) -> None:
        try:
            if self._hwnd and self._pill_visible:
                self._user32.ShowWindow(ctypes.c_void_p(self._hwnd), SW_HIDE)
            self._pill_visible = False
        except Exception:
            pass

    def _hide_border(self) -> None:
        try:
            self._stop_border_timer()
            if self._border_hwnd and self._border_visible:
                self._user32.ShowWindow(ctypes.c_void_p(self._border_hwnd),
                                        SW_HIDE)
            self._border_visible = False
        except Exception:
            pass

    # -- drawing --------------------------------------------------------------
    @staticmethod
    def _rgb(color) -> int:
        red, green, blue = (max(0, min(255, int(channel))) for channel in color)
        return red | (green << 8) | (blue << 16)

    def _draw_pill(self, lines: List[Dict], anchor: Optional[int]) -> None:
        try:
            shown = lines[-MAX_DRAWN_LINES:]
            while len(shown) < MAX_DRAWN_LINES:
                shown = [{"text": "", "accent": (0, 0, 0)}] + shown
            with self._lock:
                self._pill_lines = [dict(item) for item in shown]
            self._place_pill(anchor)
            rounded = self._gdi32.CreateRoundRectRgn(
                0, 0, WIDTH + 1, HEIGHT + 1, 22, 22)
            self._set_region(self._hwnd, "_pill_rgn", rounded)
            self._user32.InvalidateRect(ctypes.c_void_p(self._hwnd), None,
                                        True)
            self._user32.ShowWindow(ctypes.c_void_p(self._hwnd), SW_SHOWNOACTIVATE)
            self._pill_visible = True
        except Exception:
            self._note_failure("draw_pill")

    def _paint_pill(self, memory, lines: List[Dict]) -> None:
        bg_brush = self._gdi32.CreateSolidBrush(self._rgb(BG_COLOR))
        font = self._gdi32.CreateFontW(
            -14, 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 0, 0, "Segoe UI")
        try:
            full_rect = _Rect(0, 0, WIDTH, HEIGHT)
            self._user32.FillRect(memory, ctypes.byref(full_rect), bg_brush)
            self._gdi32.SelectObject(memory, font)
            self._gdi32.SetBkMode(memory, TRANSPARENT)
            for index, line in enumerate(lines):
                text = str(line.get("text") or "")
                if not text:
                    continue
                y = PAD_TOP + index * LINE_HEIGHT
                bullet = self._gdi32.CreateSolidBrush(
                    self._rgb(line.get("accent") or (128, 128, 128)))
                try:
                    dot = _Rect(20, y + 7, 30, y + 17)
                    self._user32.FillRect(memory, ctypes.byref(dot), bullet)
                finally:
                    self._gdi32.DeleteObject(bullet)
                self._gdi32.SetTextColor(memory, self._rgb(TEXT_COLOR))
                rect = _Rect(38, y, WIDTH - 14, y + LINE_HEIGHT)
                self._user32.DrawTextW(
                    memory, text, -1, ctypes.byref(rect),
                    DT_SINGLELINE | DT_END_ELLIPSIS)
        finally:
            for handle in (font, bg_brush):
                try:
                    self._gdi32.DeleteObject(handle)
                except Exception:
                    pass

    def _place_pill(self, anchor: Optional[int]) -> None:
        try:
            placed = False
            if anchor:
                rect = _Rect()
                if self._user32.GetWindowRect(ctypes.c_void_p(anchor),
                                              ctypes.byref(rect)):
                    if rect.right - rect.left >= 120 and not self._user32.IsIconic(ctypes.c_void_p(anchor)):
                        x = rect.right - WIDTH - 16
                        y = rect.top + 48
                        if x < rect.left:
                            x = rect.left + 16
                        placed = True
                    else:
                        return
            if not placed:
                point = _Point()
                self._user32.GetCursorPos(ctypes.byref(point))
                monitor = self._user32.MonitorFromPoint(
                    point, MONITOR_DEFAULTTONEAREST)
                info = _MonitorInfo()
                info.cbSize = ctypes.sizeof(_MonitorInfo)
                if self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    work = info.rcWork
                    x = work.right - WIDTH - 16
                    y = work.top + 48
                else:
                    x, y = 64, 64
            self._user32.SetWindowPos(
                ctypes.c_void_p(self._hwnd), None, int(x), int(y),
                0, 0, SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOSIZE)
        except Exception:
            pass

    def _draw_border(self, mode: str, anchor: int, pulse: bool) -> None:
        try:
            rect = _Rect()
            if not self._user32.GetWindowRect(ctypes.c_void_p(anchor),
                                              ctypes.byref(rect)):
                self._hide_border()
                return
            if self._user32.IsIconic(ctypes.c_void_p(anchor)):
                self._hide_border()
                return
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width < 120 or height < 120:
                self._hide_border()
                return
            color = _BORDER_COLORS.get(mode, PENDING_COLOR)
            # Legacy pulse flags arrive on status heartbeats. Brightness and
            # motion now come entirely from the continuous presenter clock.
            # The frame lives strictly inside the target window (same size,
            # same position) so it can never spill onto a neighboring
            # monitor; it reads as a border because only the edge band is
            # visible through the window region.
            self._user32.SetWindowPos(
                ctypes.c_void_p(self._border_hwnd), None,
                rect.left, rect.top, width, height,
                SWP_NOZORDER | SWP_NOACTIVATE)
            if self._border_geometry != (width, height):
                outer = self._gdi32.CreateRectRgn(0, 0, width, height)
                inner = self._gdi32.CreateRectRgn(
                    BORDER_PX, BORDER_PX, width - BORDER_PX,
                    height - BORDER_PX)
                frame = self._gdi32.CreateRectRgn(0, 0, 1, 1)
                try:
                    self._gdi32.CombineRgn(frame, outer, inner, 3)  # RGN_DIFF
                finally:
                    for handle in (outer, inner):
                        self._gdi32.DeleteObject(handle)
                if not self._set_region(self._border_hwnd, "_border_rgn", frame):
                    raise OSError("Could not apply the border window shape")
                self._border_geometry = (width, height)
            with self._lock:
                fresh = not self._border_visible
                now = _time.monotonic()
                if fresh or color != self._border_color:
                    self._border_color_from = (tuple(color) if fresh else _border_tint(
                        self._border_color_from, self._border_color,
                        now - self._border_color_t0))
                    self._border_color_t0 = now
                    self._border_color = tuple(color)
                if fresh:
                    self._border_anim_t0 = now
            self._user32.InvalidateRect(
                ctypes.c_void_p(self._border_hwnd), None, False)
            self._user32.ShowWindow(ctypes.c_void_p(self._border_hwnd),
                                    SW_SHOWNOACTIVATE)
            self._border_visible = True
            if not self._border_timer:
                try:
                    if self._user32.SetTimer(
                            ctypes.c_void_p(self._border_hwnd),
                            ctypes.c_void_p(BORDER_TIMER_ID),
                            BORDER_ANIM_MS, None):
                        self._border_timer = True
                except Exception:
                    pass
        except Exception:
            self._note_failure("draw_border")

    def _stop_border_timer(self) -> None:
        try:
            if self._border_timer:
                self._border_timer = False
                if self._border_hwnd:
                    try:
                        self._user32.KillTimer(
                            ctypes.c_void_p(self._border_hwnd),
                            ctypes.c_void_p(BORDER_TIMER_ID))
                    except Exception:
                        pass
        except Exception:
            pass
