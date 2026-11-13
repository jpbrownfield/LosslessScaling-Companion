"""Win32 presenter: individual fading cards plus a status border with a calm
traveling glow.
Click-through topmost windows, zero injection, zero hooks:
* toasts - rounded cards anchored to the target window's top-right;
* border - a rounded 8px ribbon drawn just inside the target window's edges,
  so it never spills onto a neighboring monitor. While scaling is pending
  the frame is blue, while active it is burgundy. A continuous gradient
  ribbon, a softer opposing highlight, and gentle breathing run on a
  presenter-side clock; state colors ease into one another without flashing.

Cards use ordinary GDI painting with scalar window opacity; the border
stays opaque. Window regions provide rounded silhouettes. This avoids
UpdateLayeredWindow, which failed on multi-GPU / virtual-display systems.

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
from functools import lru_cache
from typing import Dict, List, Optional
from .toast_stack import (ToastStack, TOAST_WIDTH, TOAST_HEIGHT, TOAST_RADIUS,
                         MAX_TOASTS)

logger = logging.getLogger("LSCompanion.OverlayWindow")


def _pump_idle_wait(seconds: float) -> None:
    """Idle wait for the presenter pump; indirection keeps the global time
    module unpatched by tests that mock time.sleep elsewhere."""
    _time.sleep(seconds)

WIDTH = TOAST_WIDTH
HEIGHT = TOAST_HEIGHT
HEADER_WIDTH = 156
HEADER_HEIGHT = 32
HEADER_GAP = 8
HINT_WIDTH = 340
HINT_HEIGHT = 180
GRAPH_WIDTH = 380
GRAPH_HEIGHT = 174
BORDER_PX = 8
BORDER_RADIUS = 10
BORDER_ANIM_MS = 16
BORDER_LAP_SECONDS = 5.2
BORDER_BREATHE_SECONDS = 4.8
BORDER_COLOR_SECONDS = 0.32
BORDER_GRADIENT_STEPS = 128
BORDER_TIMER_ID = 1
TOAST_TIMER_ID = 2

BG_COLOR = (24, 28, 36)
TEXT_COLOR = (235, 235, 235)
PENDING_COLOR = (26, 120, 255)
ACTIVE_COLOR = (145, 24, 62)
RESHADE_COLOR = (235, 183, 42)

WS_POPUP = 0x80000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
SWP_NOZORDER = 0x0004
SWP_NOSIZE = 0x0001
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
    "reshade": RESHADE_COLOR,
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


class _GradientTriangle(ctypes.Structure):
    _fields_ = [("first", ctypes.c_ulong), ("second", ctypes.c_ulong),
                ("third", ctypes.c_ulong)]


@lru_cache(maxsize=8)
def _border_mesh(width: int, height: int):
    """A closed rounded path, with shared vertices through every corner.

    Position follows distance around the whole perimeter, so the highlight
    keeps traveling through corners rather than restarting on each edge.
    Cache geometry across animation frames; only vertex colors change.
    """
    radius = min(BORDER_RADIUS, width / 2, height / 2)
    perimeter = 2 * (width + height - 4 * radius) + 2 * math.pi * radius
    samples = []
    distance = 0.0
    for start, end, center, angle, normal in (
            ((radius, 0), (width - radius, 0), (width - radius, radius), -math.pi / 2, (0, 1)),
            ((width, radius), (width, height - radius), (width - radius, height - radius), 0, (-1, 0)),
            ((width - radius, height), (radius, height), (radius, height - radius), math.pi / 2, (0, -1)),
            ((0, height - radius), (0, radius), (radius, radius), math.pi, (1, 0))):
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        steps = max(1, math.ceil(BORDER_GRADIENT_STEPS * length / perimeter))
        for step in range(steps):
            fraction = step / steps
            samples.append(((distance + length * fraction) / perimeter,
                            start[0] + (end[0] - start[0]) * fraction,
                            start[1] + (end[1] - start[1]) * fraction, *normal))
        distance += length
        length = math.pi * radius / 2
        # Short arc segments keep the rounded silhouette smooth at any size.
        steps = max(16, math.ceil(length / 2))
        for step in range(steps):
            theta = angle + math.pi / 2 * step / steps
            x, y = math.cos(theta), math.sin(theta)
            samples.append(((distance + length * step / steps) / perimeter,
                            center[0] + radius * x, center[1] + radius * y, -x, -y))
        distance += length
    samples.append((1.0, *samples[0][1:]))
    # Soft shoulders around a wider luminous core. Extend the mesh slightly
    # past the clipping region to avoid unpainted pixels at its raster edge.
    bands = ((-1, 0.42), (1, 0.65), (3, 0.95), (5, 1.0), (7, 0.65), (BORDER_PX + 1, 0.42))
    points = tuple((round(x + nx * depth), round(y + ny * depth), shade)
                   for _, x, y, nx, ny in samples for depth, shade in bands)
    triangles = []
    for index in range(len(samples) - 1):
        for band in range(len(bands) - 1):
            a = index * len(bands) + band
            b = a + len(bands)
            for first, second, third in ((a, b, a + 1), (a + 1, b, b + 1)):
                x1, y1, _ = points[first]
                x2, y2, _ = points[second]
                x3, y3, _ = points[third]
                # Tight curves can round adjacent vertices onto the same pixel.
                # GradientFill rejects triangles with no drawable area.
                if (x2 - x1) * (y3 - y1) != (y2 - y1) * (x3 - x1):
                    triangles.append(_GradientTriangle(first, second, third))
    return samples, points, (_GradientTriangle * len(triangles))(*triangles)


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
        u.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                ctypes.c_ubyte, ctypes.c_ulong]
        u.SetLayeredWindowAttributes.restype = wintypes.BOOL
        u.UnregisterClassW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        u.UnregisterClassW.restype = wintypes.BOOL
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
        u.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        u.MonitorFromWindow.restype = ctypes.c_void_p
        u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
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
        g.SaveDC.argtypes = [ctypes.c_void_p]
        g.SaveDC.restype = ctypes.c_int
        g.RestoreDC.argtypes = [ctypes.c_void_p, ctypes.c_int]
        g.RestoreDC.restype = wintypes.BOOL
        g.SetMapMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        g.SetMapMode.restype = ctypes.c_int
        for name in ("SetWindowExtEx", "SetViewportExtEx"):
            function = getattr(g, name)
            function.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            function.restype = wintypes.BOOL
        g.ExtSelectClipRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        g.ExtSelectClipRgn.restype = ctypes.c_int
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
        g.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        g.CreatePen.restype = ctypes.c_void_p
        g.Polyline.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Point), ctypes.c_int]
        g.Polyline.restype = wintypes.BOOL
        msimg32.GradientFill.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GradientVertex),
                                        ctypes.c_ulong, ctypes.POINTER(_GradientTriangle),
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
    """Callable presenter driving independent toast cards and the border."""

    PILL_CLASS = "LSCompanionOverlay"
    BORDER_CLASS = "LSCompanionOverlayBorder"
    HEADER_CLASS = "LSCompanionOverlayHeader"
    HINT_CLASS = "LSCompanionOverlayHints"
    GRAPH_CLASS = "LSCompanionOverlayGraph"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._usable = True
        self._reported_failure = False
        self._hwnd = None
        self._border_hwnd = None
        self._header_hwnd = None
        self._header_visible = False
        self._header_icon = None
        self._hint_hwnd = None
        self._hint_visible = False
        self._hint_lines = []
        self._hint_height = HINT_HEIGHT
        self._ui_scale = 1.0
        self._graph_hwnd = None
        self._graph_visible = False
        self._graph_data = None
        self._thread = None
        self._thread_id = None
        self._pending = None
        self._has_pending = False
        self._pill_visible = False
        self._border_visible = False
        self._pill_rgn = None
        self._border_rgn = None
        self._toast_stack = ToastStack()
        self._toast_hwnds = []
        self._toast_slots = {}
        self._toast_lines = {}
        self._toast_shown = set()
        self._toast_origin = None
        self._toast_timer = False
        self._classes = []
        self._border_color: Optional[tuple] = None
        self._border_timer = False
        self._border_anim_t0 = 0.0
        self._border_color_from = PENDING_COLOR
        self._border_color_t0 = 0.0
        self._border_geometry = None
        self._border_buffer = None
        self._border_clip = None
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
            self._stop_toast_timer()
            with self._lock:
                handles = [*self._toast_hwnds, self._header_hwnd, self._hint_hwnd, self._graph_hwnd, self._border_hwnd]
                self._toast_hwnds = []
                self._toast_slots.clear()
                self._toast_lines.clear()
                self._toast_shown.clear()
                self._toast_stack.clear()
                self._pill_visible = False
                self._header_hwnd = None
                self._header_visible = False
                self._header_icon = None
                self._hint_hwnd = None
                self._hint_visible = False
                self._hint_lines = []
                self._hint_height = HINT_HEIGHT
                self._ui_scale = 1.0
                self._graph_hwnd = None
                self._graph_visible = False
                self._graph_data = None
                self._border_geometry = None
                self._hwnd = None
                self._border_hwnd = None
                self._border_color = None
                self._pending = None
                self._has_pending = False
            for handle in handles:
                if handle:
                    try:
                        if self._user32.IsWindow(ctypes.c_void_p(handle)):
                            self._user32.DestroyWindow(ctypes.c_void_p(handle))
                    except Exception:
                        pass
            module = self._kernel32.GetModuleHandleW(None)
            for class_name in self._classes:
                self._user32.UnregisterClassW(class_name, module)
            self._classes.clear()
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
        previous_dpi_context = None
        try:
            # Keep overlay geometry in physical pixels on mixed-DPI desktops.
            previous_dpi_context = self._user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
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
            if previous_dpi_context:
                self._user32.SetThreadDpiAwarenessContext(previous_dpi_context)

    # -- windows ------------------------------------------------------------
    def _make_window(self, class_name: str, layered: bool = False,
                     width: int = WIDTH, height: int = HEIGHT) -> Optional[int]:
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
                        if wparam == TOAST_TIMER_ID and self._toast_timer and hwnd == self._hwnd:
                            self._animate_toasts()
                        if (wparam == BORDER_TIMER_ID
                                and self._border_timer
                                and self._border_hwnd
                                and hwnd == self._border_hwnd):
                            self._user32.InvalidateRect(
                                ctypes.c_void_p(hwnd), None, False)
                    except Exception:
                        self._note_failure("animation_timer")
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
            # Each registered callback belongs to this presenter. Never reuse
            # a previous presenter's class and its possibly released callback.
            class_name = f"{class_name}_{id(self):x}_{len(self._procs)}"
            module = self._kernel32.GetModuleHandleW(None)
            wnd_class = _WndClassEx()
            wnd_class.cbSize = ctypes.sizeof(_WndClassEx)
            wnd_class.lpfnWndProc = proc
            wnd_class.hInstance = module
            wnd_class.lpszClassName = class_name
            registered = self._user32.RegisterClassExW(ctypes.byref(wnd_class))
            if not registered and ctypes.get_last_error() != 1410:
                return None
            self._classes.append(class_name)
            hwnd = self._user32.CreateWindowExW(
                WS_EX_TRANSPARENT | WS_EX_TOPMOST |
                WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | (WS_EX_LAYERED if layered else 0),
                class_name, "LS Companion", WS_POPUP,
                0, 0, width, height, None, None, module, None)
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
            saved = 0
            try:
                if not hdc:
                    return
                with self._lock:
                    toast_line = self._toast_lines.get(hwnd)
                    border_color = self._border_color
                if hwnd != self._border_hwnd and self._ui_scale != 1.0:
                    saved = self._gdi32.SaveDC(hdc)
                    self._gdi32.SetMapMode(hdc, 8)  # MM_ANISOTROPIC, logical 1080p units
                    self._gdi32.SetWindowExtEx(hdc, 1000, 1000, None)
                    extent = round(1000 * self._ui_scale)
                    self._gdi32.SetViewportExtEx(hdc, extent, extent, None)
                if hwnd == self._header_hwnd:
                    self._paint_header(hdc)
                elif hwnd == self._hint_hwnd:
                    self._paint_hints(hdc)
                elif hwnd == self._graph_hwnd:
                    self._paint_graph(hdc)
                elif toast_line is not None:
                    self._paint_pill(hdc, [toast_line])
                elif hwnd == self._border_hwnd and border_color is not None:
                    self._paint_border_frame(hdc, hwnd, border_color)
            finally:
                if saved:
                    self._gdi32.RestoreDC(hdc, saved)
                try:
                    self._user32.EndPaint(ctypes.c_void_p(hwnd),
                                          ctypes.byref(paint))
                except Exception:
                    pass
        except Exception:
            self._note_failure("paint_window")

    def _release_border_buffer(self) -> None:
        buffer, self._border_buffer = self._border_buffer, None
        clip, self._border_clip = self._border_clip, None
        if clip:
            self._gdi32.DeleteObject(clip)
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
        self._border_clip = self._rounded_border_region(width, height)
        return dc

    def _rounded_border_region(self, width: int, height: int):
        radius = min(BORDER_RADIUS, width // 2, height // 2)
        outer = self._gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1,
                                             2 * radius, 2 * radius)
        inner = self._gdi32.CreateRoundRectRgn(
            BORDER_PX, BORDER_PX, width - BORDER_PX + 1, height - BORDER_PX + 1,
            2 * (radius - BORDER_PX), 2 * (radius - BORDER_PX))
        try:
            if not outer or not inner or not self._gdi32.CombineRgn(outer, outer, inner, 3):
                raise OSError("Could not create the rounded border shape")
            result, outer = outer, None
            return result
        finally:
            for region in (outer, inner):
                if region:
                    self._gdi32.DeleteObject(region)

    def _border_gradients(self, width: int, height: int, color: tuple, elapsed: float):
        samples, points, triangles = _border_mesh(width, height)
        vertices = []
        bands = len(points) // len(samples)
        for index, (position, *_geometry) in enumerate(samples):
            tint = _border_light(color, position, elapsed)
            for x, y, shade in points[index * bands:(index + 1) * bands]:
                channels = [round(channel * shade) << 8 for channel in tint]
                vertices.append(_GradientVertex(x, y, *channels, 0))
        return (_GradientVertex * len(vertices))(*vertices), triangles

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
            vertices, triangles = self._border_gradients(total_w, total_h, tint, elapsed)
            if not gradient(memory, vertices, len(vertices), triangles, len(triangles), 2):
                raise OSError("Could not paint the border gradient")
            saved = self._gdi32.SaveDC(hdc)
            if not saved:
                raise OSError("Could not save the border drawing context")
            try:
                if not self._gdi32.ExtSelectClipRgn(hdc, self._border_clip, 1):
                    raise OSError("Could not clip the rounded border")
                # Copy only the perimeter strips; clipping keeps the rounded
                # corners and the application's center entirely untouched.
                edge = min(BORDER_RADIUS, total_w // 2, total_h // 2)
                for x, y, width, height in (
                        (0, 0, total_w, edge),
                        (0, total_h - edge, total_w, edge),
                        (0, edge, edge, total_h - 2 * edge),
                        (total_w - edge, edge, edge, total_h - 2 * edge)):
                    if not self._gdi32.BitBlt(hdc, x, y, width, height, memory, x, y, 0x00CC0020):
                        raise OSError("Could not present the border frame")
            finally:
                self._gdi32.RestoreDC(hdc, saved)
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
            for _ in range(MAX_TOASTS):
                hwnd = self._make_window(self.PILL_CLASS, layered=True)
                if not hwnd:
                    return False
                self._toast_hwnds.append(hwnd)
                rounded = self._gdi32.CreateRoundRectRgn(
                    0, 0, WIDTH + 1, HEIGHT + 1, 2 * TOAST_RADIUS, 2 * TOAST_RADIUS)
                if not self._set_region(hwnd, "_pill_rgn", rounded):
                    return False
            self._hwnd = self._toast_hwnds[0]
            self._header_hwnd = self._make_window(
                self.HEADER_CLASS, layered=True, width=HEADER_WIDTH, height=HEADER_HEIGHT)
            if not self._header_hwnd:
                return False
            rounded = self._gdi32.CreateRoundRectRgn(
                0, 0, HEADER_WIDTH + 1, HEADER_HEIGHT + 1, 20, 20)
            if not self._set_region(self._header_hwnd, "_header_rgn", rounded):
                return False
            self._hint_hwnd = self._make_window(
                self.HINT_CLASS, layered=True, width=HINT_WIDTH, height=HINT_HEIGHT)
            if not self._hint_hwnd:
                return False
            rounded = self._gdi32.CreateRoundRectRgn(
                0, 0, HINT_WIDTH + 1, HINT_HEIGHT + 1, 24, 24)
            if not self._set_region(self._hint_hwnd, "_hint_rgn", rounded):
                return False
            self._graph_hwnd = self._make_window(
                self.GRAPH_CLASS, layered=True, width=GRAPH_WIDTH, height=GRAPH_HEIGHT)
            if not self._graph_hwnd:
                return False
            rounded = self._gdi32.CreateRoundRectRgn(0, 0, GRAPH_WIDTH + 1, GRAPH_HEIGHT + 1, 24, 24)
            if not self._set_region(self._graph_hwnd, "_graph_rgn", rounded):
                return False
            self._border_hwnd = self._make_window(self.BORDER_CLASS)
            return bool(self._hwnd and self._border_hwnd)
        except Exception:
            return False

    # -- apply --------------------------------------------------------------
    def _px(self, value: float) -> int:
        return round(value * self._ui_scale)

    def _size_ui_window(self, hwnd: int, slot: str, width: int, height: int, radius: int) -> None:
        region = self._gdi32.CreateRoundRectRgn(
            0, 0, self._px(width) + 1, self._px(height) + 1,
            self._px(2 * radius), self._px(2 * radius))
        if not self._set_region(hwnd, slot, region):
            raise OSError("Could not resize overlay surface")
        self._user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0,
            self._px(width), self._px(height), SWP_NOZORDER | SWP_NOACTIVATE | 0x0002)  # SWP_NOMOVE
        self._user32.InvalidateRect(ctypes.c_void_p(hwnd), None, False)

    def _update_ui_scale(self, anchor: Optional[int]) -> None:
        if anchor:
            monitor = self._user32.MonitorFromWindow(ctypes.c_void_p(anchor), MONITOR_DEFAULTTONEAREST)
        else:
            point = _Point()
            self._user32.GetCursorPos(ctypes.byref(point))
            monitor = self._user32.MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST)
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return
        # Use the shorter monitor dimension so portrait rotation keeps the
        # same readable size. 1080p = 1x, 1440p = 1.33x, 4K = 2x.
        resolution = min(info.rcMonitor.right - info.rcMonitor.left,
                         info.rcMonitor.bottom - info.rcMonitor.top)
        scale = max(0.5, resolution / 1080.0)
        if scale == self._ui_scale:
            return
        self._ui_scale = scale
        self._header_icon = None
        for hwnd in self._toast_hwnds:
            self._size_ui_window(hwnd, "_pill_rgn", WIDTH, HEIGHT, TOAST_RADIUS)
        for hwnd, slot, width, height, radius in (
            (self._header_hwnd, "_header_rgn", HEADER_WIDTH, HEADER_HEIGHT, 10),
            (self._hint_hwnd, "_hint_rgn", HINT_WIDTH, self._hint_height, 12),
            (self._graph_hwnd, "_graph_rgn", GRAPH_WIDTH, GRAPH_HEIGHT, 12)):
            if hwnd:
                self._size_ui_window(hwnd, slot, width, height, radius)

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
            def window_handle(value):
                try:
                    return int(value) if value else None
                except (TypeError, ValueError):
                    return None

            anchor = window_handle(has_pending.get("anchor_hwnd"))
            border_anchor = window_handle(has_pending.get("border_anchor_hwnd", anchor))
            if lines:
                self._draw_pill(lines, anchor)
            else:
                self._hide_pill()
            if border in _BORDER_COLORS and border_anchor:
                self._draw_border(border, border_anchor, pulse)
            else:
                self._hide_border()
            hints = has_pending.get("hotkey_hints") or []
            if hints and border == "reshade" and border_anchor:
                self._draw_hints(hints, border_anchor)
            else:
                self._hide_hints()
            graph = has_pending.get("performance_graph")
            if graph is not None and border == "reshade" and border_anchor:
                self._draw_graph(graph, border_anchor)
            else:
                self._hide_graph()
        except Exception:
            self._note_failure("apply_pending")

    def _hide_all(self) -> None:
        self._hide_pill()
        self._hide_border()
        self._hide_hints()
        self._hide_graph()

    def _hide_graph(self) -> None:
        if self._graph_hwnd and self._graph_visible:
            self._user32.ShowWindow(ctypes.c_void_p(self._graph_hwnd), SW_HIDE)
        self._graph_visible = False

    def _draw_graph(self, data: Dict, anchor: int) -> None:
        self._update_ui_scale(anchor)
        rect = _Rect()
        if (not self._user32.GetWindowRect(ctypes.c_void_p(anchor), ctypes.byref(rect))
                or self._user32.IsIconic(ctypes.c_void_p(anchor))):
            self._hide_graph()
            return
        self._graph_data = dict(data)
        self._user32.SetWindowPos(ctypes.c_void_p(self._graph_hwnd), None, rect.left + self._px(24),
            rect.bottom - self._px(GRAPH_HEIGHT + 24) if rect.right - rect.left >= self._px(800) else rect.top + self._px(48),
            0, 0, SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOSIZE)
        self._user32.InvalidateRect(ctypes.c_void_p(self._graph_hwnd), None, False)
        if not self._user32.SetLayeredWindowAttributes(ctypes.c_void_p(self._graph_hwnd), 0, 255, 2):
            raise OSError("Could not show the performance graph")
        if not self._graph_visible:
            self._user32.ShowWindow(ctypes.c_void_p(self._graph_hwnd), SW_SHOWNOACTIVATE)
        self._graph_visible = True

    def _paint_graph(self, memory) -> None:
        self._paint_card(memory, GRAPH_WIDTH, GRAPH_HEIGHT, 12)
        data = self._graph_data or {}
        font = self._gdi32.CreateFontW(-13, 0, 0, 0, 500, 0, 0, 0, 0, 0, 0, 0, 0, "Segoe UI")
        previous = self._gdi32.SelectObject(memory, font)
        pen = self._gdi32.CreatePen(0, 2, self._rgb((66, 192, 225)))
        previous_pen = self._gdi32.SelectObject(memory, pen)
        grid = self._gdi32.CreateSolidBrush(self._rgb((47, 54, 67)))
        try:
            self._gdi32.SetBkMode(memory, TRANSPARENT)
            self._gdi32.SetTextColor(memory, self._rgb(TEXT_COLOR))
            title = "Application FPS / frame time"
            self._user32.DrawTextW(memory, title, -1,
                ctypes.byref(_Rect(16, 10, GRAPH_WIDTH - 16, 32)), DT_SINGLELINE)
            summary = (f"{data['fps']:.1f} FPS   /   {data['frame_ms']:.2f} ms"
                       if data.get("fps") is not None else data.get("status", "Waiting for frames..."))
            self._user32.DrawTextW(memory, summary, -1,
                ctypes.byref(_Rect(16, 32, GRAPH_WIDTH - 16, 54)), DT_SINGLELINE | DT_END_ELLIPSIS)
            values = data.get("samples", [])[-180:]
            ceiling = max(33.3, max(values, default=0) * 1.15)
            for y in (70, 108, 146):
                self._user32.FillRect(memory, ctypes.byref(_Rect(16, y, GRAPH_WIDTH - 16, y + 1)), grid)
            points = []
            for index, value in enumerate(values):
                point = (16 + round(index * (GRAPH_WIDTH - 32) / max(1, len(values) - 1)),
                         146 - round(min(1.0, value / ceiling) * 76))
                points.append(_Point(*point))
            if len(points) >= 2:
                self._gdi32.Polyline(memory, (_Point * len(points))(*points), len(points))
        finally:
            if previous:
                self._gdi32.SelectObject(memory, previous)
            if previous_pen:
                self._gdi32.SelectObject(memory, previous_pen)
            for handle in (font, pen, grid):
                self._gdi32.DeleteObject(handle)

    def _hide_hints(self) -> None:
        if self._hint_hwnd and self._hint_visible:
            self._user32.ShowWindow(ctypes.c_void_p(self._hint_hwnd), SW_HIDE)
        self._hint_visible = False

    def _draw_hints(self, hints: List[Dict], anchor: int) -> None:
        self._update_ui_scale(anchor)
        rect = _Rect()
        if (not self._user32.GetWindowRect(ctypes.c_void_p(anchor), ctypes.byref(rect))
                or self._user32.IsIconic(ctypes.c_void_p(anchor))):
            self._hide_hints()
            return
        lines = [dict(hint) for hint in hints[:6]]
        height = 24 + 26 * len(lines)
        if height != self._hint_height:
            self._size_ui_window(self._hint_hwnd, "_hint_rgn", HINT_WIDTH, height, 12)
            self._hint_height = height
        if lines != self._hint_lines or not self._hint_visible:
            self._hint_lines = lines
            self._user32.InvalidateRect(ctypes.c_void_p(self._hint_hwnd), None, False)
        self._user32.SetWindowPos(
            ctypes.c_void_p(self._hint_hwnd), None,
            max(rect.left + self._px(16), rect.right - self._px(HINT_WIDTH + 16)),
            max(rect.top + self._px(16), rect.bottom - self._px(height + 24)), self._px(HINT_WIDTH), self._px(height),
            SWP_NOZORDER | SWP_NOACTIVATE)
        if not self._user32.SetLayeredWindowAttributes(ctypes.c_void_p(self._hint_hwnd), 0, 255, 2):
            raise OSError("Could not show ReShade hotkey hints")
        if not self._hint_visible:
            self._user32.ShowWindow(ctypes.c_void_p(self._hint_hwnd), SW_SHOWNOACTIVATE)
        self._hint_visible = True

    def _paint_hints(self, memory) -> None:
        self._paint_card(memory, HINT_WIDTH, self._hint_height, 12)
        font = self._gdi32.CreateFontW(
            -13, 0, 0, 0, 500, 0, 0, 0, 0, 0, 0, 0, 0, "Segoe UI")
        previous = self._gdi32.SelectObject(memory, font)
        try:
            self._gdi32.SetBkMode(memory, TRANSPARENT)
            for index, hint in enumerate(self._hint_lines):
                top = 12 + index * 26
                self._gdi32.SetTextColor(memory, self._rgb(RESHADE_COLOR))
                self._user32.DrawTextW(memory, str(hint["key"]), -1,
                    ctypes.byref(_Rect(16, top, 158, top + 26)), DT_SINGLELINE | 0x0004)
                self._gdi32.SetTextColor(memory, self._rgb(TEXT_COLOR))
                self._user32.DrawTextW(memory, str(hint["label"]), -1,
                    ctypes.byref(_Rect(162, top, HINT_WIDTH - 12, top + 26)), DT_END_ELLIPSIS | 0x0004)
        finally:
            if previous:
                self._gdi32.SelectObject(memory, previous)
            self._gdi32.DeleteObject(font)

    def _hide_pill(self) -> None:
        try:
            self._stop_toast_timer()
            self._hide_header()
            for hwnd in self._toast_shown:
                self._user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE)
            self._toast_shown.clear()
            self._toast_slots.clear()
            self._toast_lines.clear()
            self._toast_stack.clear()
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
            now = _time.monotonic()
            self._toast_stack.sync(lines, now)
            self._place_pill(anchor)
            self._animate_toasts(now)
            if self._toast_stack.toasts and not self._toast_timer:
                self._toast_timer = bool(self._user32.SetTimer(
                    ctypes.c_void_p(self._hwnd), ctypes.c_void_p(TOAST_TIMER_ID),
                    BORDER_ANIM_MS, None))
                if not self._toast_timer:
                    raise OSError("Could not start the toast animation timer")
        except Exception:
            self._note_failure("draw_pill")

    def _stop_toast_timer(self) -> None:
        if self._toast_timer and self._hwnd:
            self._user32.KillTimer(ctypes.c_void_p(self._hwnd),
                                   ctypes.c_void_p(TOAST_TIMER_ID))
        self._toast_timer = False

    def _animate_toasts(self, now: Optional[float] = None) -> None:
        """Fade and slide cards on the window thread, independently of status updates."""
        now = _time.monotonic() if now is None else now
        toasts = self._toast_stack.frames(now)
        keys = {toast.key for toast in toasts}
        for key in list(self._toast_slots):
            if key not in keys:
                hwnd = self._toast_slots.pop(key)
                self._user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE)
                self._toast_shown.discard(hwnd)
                self._toast_lines.pop(hwnd, None)
        for toast in toasts:
            hwnd = self._toast_slots.get(toast.key)
            if hwnd is None:
                used = set(self._toast_slots.values())
                hwnd = next(handle for handle in self._toast_hwnds if handle not in used)
                self._toast_slots[toast.key] = hwnd
                self._toast_lines[hwnd] = toast.line
                self._user32.InvalidateRect(ctypes.c_void_p(hwnd), None, False)
            if self._toast_origin is None:
                self._user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE)
                self._toast_shown.discard(hwnd)
                continue
            x, y = self._toast_origin
            self._user32.SetWindowPos(ctypes.c_void_p(hwnd), None,
                                      x, y + self._px(toast.y(now)), 0, 0,
                                      SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOSIZE)
            if not self._user32.SetLayeredWindowAttributes(
                    ctypes.c_void_p(hwnd), 0, toast.alpha(now), 2):  # LWA_ALPHA
                raise OSError("Could not set toast opacity")
            if hwnd not in self._toast_shown:
                self._user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWNOACTIVATE)
                self._toast_shown.add(hwnd)
        self._pill_visible = bool(self._toast_shown)
        if self._pill_visible:
            x, y = self._toast_origin
            self._user32.SetWindowPos(
                ctypes.c_void_p(self._header_hwnd), None,
                x + self._px(WIDTH) - self._px(HEADER_WIDTH),
                y - self._px(HEADER_HEIGHT) - self._px(HEADER_GAP), 0, 0,
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOSIZE)
            if not self._user32.SetLayeredWindowAttributes(
                    ctypes.c_void_p(self._header_hwnd), 0,
                    max(toast.alpha(now) for toast in toasts), 2):
                raise OSError("Could not set overlay header opacity")
            if not self._header_visible:
                self._user32.InvalidateRect(ctypes.c_void_p(self._header_hwnd), None, False)
                self._user32.ShowWindow(ctypes.c_void_p(self._header_hwnd), SW_SHOWNOACTIVATE)
                self._header_visible = True
        else:
            self._hide_header()
        if not toasts:
            self._stop_toast_timer()

    def _hide_header(self) -> None:
        if self._header_hwnd and self._header_visible:
            self._user32.ShowWindow(ctypes.c_void_p(self._header_hwnd), SW_HIDE)
        self._header_visible = False

    def _paint_card(self, memory, width: int, height: int, radius: int) -> None:
        bg_brush = self._gdi32.CreateSolidBrush(self._rgb(BG_COLOR))
        outline = self._gdi32.CreateSolidBrush(self._rgb((47, 54, 67)))
        inner = self._gdi32.CreateRoundRectRgn(
            1, 1, width - 1, height - 1, 2 * radius - 2, 2 * radius - 2)
        try:
            self._user32.FillRect(memory, ctypes.byref(_Rect(0, 0, width, height)), outline)
            self._gdi32.FillRgn(memory, inner, bg_brush)
        finally:
            for handle in (inner, bg_brush, outline):
                self._gdi32.DeleteObject(handle)

    def _paint_header(self, memory) -> None:
        self._paint_card(memory, HEADER_WIDTH, HEADER_HEIGHT, 10)
        if self._header_icon is None:
            from PIL import Image, ImageWin
            from ..ui.icons import create_lightning_icon

            icon_size = self._px(24)
            icon = create_lightning_icon(96, 96).resize((icon_size, icon_size), Image.Resampling.LANCZOS)
            background = Image.new("RGBA", icon.size, (*BG_COLOR, 255))
            background.alpha_composite(icon)
            self._header_icon = ImageWin.Dib(background.convert("RGB"))
        self._header_icon.draw(memory, (8, 4, 32, 28))
        font = self._gdi32.CreateFontW(
            -13, 0, 0, 0, 600, 0, 0, 0, 0, 0, 0, 0, 0, "Segoe UI")
        previous_font = self._gdi32.SelectObject(memory, font)
        try:
            self._gdi32.SetBkMode(memory, TRANSPARENT)
            self._gdi32.SetTextColor(memory, self._rgb(TEXT_COLOR))
            rect = _Rect(39, 0, HEADER_WIDTH - 8, HEADER_HEIGHT)
            self._user32.DrawTextW(memory, "LS Companion", -1, ctypes.byref(rect),
                                   DT_SINGLELINE | 0x0004)
        finally:
            if previous_font:
                self._gdi32.SelectObject(memory, previous_font)
            self._gdi32.DeleteObject(font)

    def _paint_pill(self, memory, lines: List[Dict]) -> None:
        self._paint_card(memory, WIDTH, HEIGHT, TOAST_RADIUS)
        font = self._gdi32.CreateFontW(
            -15, 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 0, 0, "Segoe UI")
        previous_font = None
        try:
            previous_font = self._gdi32.SelectObject(memory, font)
            self._gdi32.SetBkMode(memory, TRANSPARENT)
            for line in lines[:1]:
                text = str(line.get("text") or "")
                if not text:
                    continue
                bullet = self._gdi32.CreateSolidBrush(
                    self._rgb(line.get("accent") or (128, 128, 128)))
                try:
                    dot = self._gdi32.CreateRoundRectRgn(17, 22, 25, 30, 8, 8)
                    try:
                        self._gdi32.FillRgn(memory, dot, bullet)
                    finally:
                        self._gdi32.DeleteObject(dot)
                finally:
                    self._gdi32.DeleteObject(bullet)
                self._gdi32.SetTextColor(memory, self._rgb(TEXT_COLOR))
                rect = _Rect(38, 0, WIDTH - 16, HEIGHT)
                self._user32.DrawTextW(
                    memory, text, -1, ctypes.byref(rect),
                    DT_SINGLELINE | DT_END_ELLIPSIS | 0x0004)  # DT_VCENTER
        finally:
            if previous_font:
                self._gdi32.SelectObject(memory, previous_font)
            for handle in (font,):
                try:
                    self._gdi32.DeleteObject(handle)
                except Exception:
                    pass

    def _place_pill(self, anchor: Optional[int]) -> None:
        try:
            self._update_ui_scale(anchor)
            self._toast_origin = None
            placed = False
            if anchor:
                rect = _Rect()
                if self._user32.GetWindowRect(ctypes.c_void_p(anchor),
                                              ctypes.byref(rect)):
                    if rect.right - rect.left >= 120 and not self._user32.IsIconic(ctypes.c_void_p(anchor)):
                        x = rect.right - self._px(WIDTH + 16)
                        y = rect.top + self._px(48)
                        if x < rect.left:
                            x = rect.left + self._px(16)
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
                    x = work.right - self._px(WIDTH + 16)
                    y = work.top + self._px(48)
                else:
                    x, y = 64, 64
            self._toast_origin = (int(x), int(y))
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
                frame = self._rounded_border_region(width, height)
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
