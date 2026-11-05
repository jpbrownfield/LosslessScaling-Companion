"""Win32 presenter: scrolling-log pill plus a pulsing status border.

Two click-through topmost layered windows, zero injection, zero hooks:
* pill — terminal-style lines anchored to the target window's top-right;
* border — a frame around the target window that pulses blue while scaling
  is pending and maroon while scaling is active (~25px, per spec).

Never steals focus (NOACTIVATE everywhere, SW_SHOWNOACTIVATE, no foreground
calls). Any failure permanently disables the presenter so overlays can never
break scaling.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Dict, List, Optional

WIDTH = 440
LINE_HEIGHT = 28
PAD_TOP = 12
PAD_LEFT = 44
MAX_DRAWN_LINES = 5
HEIGHT = PAD_TOP * 2 + LINE_HEIGHT * MAX_DRAWN_LINES
BORDER_PX = 25

BG_COLOR = (24, 24, 24)
KEY_COLOR = (255, 0, 255)
TEXT_COLOR = (235, 235, 235)
PENDING_COLOR = (0, 120, 255)
ACTIVE_COLOR = (128, 0, 0)
CONSTANT_ALPHA = 230

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
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
LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002
WM_TIMER = 0x0113
WM_DESTROY = 0x0002
WM_USER_APPLY = 0x8001
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


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p,
                              ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)


class _WndClassEx(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint), ("style", ctypes.c_uint),
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
        u.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p,
                                                 ctypes.c_ulong,
                                                 ctypes.c_ubyte,
                                                 ctypes.c_ulong]
        u.SetLayeredWindowAttributes.restype = ctypes.c_bool
        u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_uint]
        u.SetWindowPos.restype = ctypes.c_bool
        u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.ShowWindow.restype = ctypes.c_bool
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
        u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                  ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.c_uint]
        u.GetMessageW.restype = ctypes.c_int
        u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.TranslateMessage.restype = ctypes.c_bool
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.DispatchMessageW.restype = ctypes.c_longlong
        u.PostThreadMessageW.argtypes = [ctypes.c_ulong, ctypes.c_uint,
                                         ctypes.c_void_p, ctypes.c_void_p]
        u.PostThreadMessageW.restype = ctypes.c_bool
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.PostQuitMessage.restype = None
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
        g.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        g.CreateCompatibleDC.restype = ctypes.c_void_p
        g.DeleteDC.argtypes = [ctypes.c_void_p]
        g.DeleteDC.restype = ctypes.c_bool
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
        g.CreateFontW.argtypes = ([ctypes.c_int] * 5 + [ctypes.c_ulong] * 4
                                  + [ctypes.c_ubyte] * 5 + [ctypes.c_wchar_p])
        g.CreateFontW.restype = ctypes.c_void_p
        g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        g.SelectObject.restype = ctypes.c_void_p
        g.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        g.SetBkMode.restype = ctypes.c_int
        g.SetTextColor.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        g.SetTextColor.restype = ctypes.c_ulong
        g.DeleteObject.argtypes = [ctypes.c_void_p]
        g.DeleteObject.restype = ctypes.c_bool
        _api = {"user32": u, "gdi32": g, "kernel32": kernel32}
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
        self._hwnd = None
        self._border_hwnd = None
        self._thread = None
        self._thread_id = None
        self._pending = None
        self._has_pending = False
        self._pill_visible = False
        self._border_visible = False
        try:
            self._user32, self._gdi32, self._kernel32 = _handles()
        except Exception:
            self._usable = False
            self._user32 = None
            self._gdi32 = None
            self._kernel32 = None

    def __call__(self, payload: Optional[Dict]) -> None:
        try:
            if not self._usable:
                return
            with self._lock:
                self._pending = None if payload is None else dict(payload)
                self._has_pending = True
            self._ensure_thread()
            if self._thread_id:
                try:
                    self._user32.PostThreadMessageW(self._thread_id,
                                                    WM_USER_APPLY, 0, 0)
                except Exception:
                    pass
        except Exception:
            self._usable = False

    def close(self) -> None:
        try:
            thread, pill, border = self._thread, self._hwnd, self._border_hwnd
            self._thread = None
            self._hwnd = None
            self._border_hwnd = None
            if thread is not None and self._thread_id:
                try:
                    self._user32.PostThreadMessageW(self._thread_id,
                                                     WM_QUIT_MESSAGE, 0, 0)
                except Exception:
                    pass
                thread.join(timeout=2.0)
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
                self._usable = False
                return
            message = wintypes.MSG()
            while self._user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_USER_APPLY:
                    self._apply_pending()
                else:
                    self._user32.TranslateMessage(ctypes.byref(message))
                    self._user32.DispatchMessageW(ctypes.byref(message))
        except Exception:
            self._usable = False
        finally:
            self._thread_id = None

    # -- windows ------------------------------------------------------------
    def _make_window(self, class_name: str) -> Optional[int]:
        def _handler(hwnd, message, wparam, lparam):
            try:
                if message == WM_DESTROY:
                    self._user32.PostQuitMessage(0)
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
                WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST |
                WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
                class_name, "LS Companion", WS_POPUP,
                0, 0, WIDTH, HEIGHT, None, None, module, None)
            if not hwnd:
                return None
            key = KEY_COLOR[2] << 16 | KEY_COLOR[1] << 8 | KEY_COLOR[0]
            self._user32.SetLayeredWindowAttributes(
                ctypes.c_void_p(hwnd), key, CONSTANT_ALPHA,
                LWA_COLORKEY | LWA_ALPHA)
            return hwnd
        except Exception:
            return None

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
            self._usable = False

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
            screen = self._user32.GetDC(None)
            memory = self._gdi32.CreateCompatibleDC(screen)
            bitmap_info = (ctypes.c_ubyte * 40)()
            ctypes.memset(bitmap_info, 0, 40)
            ctypes.cast(bitmap_info, ctypes.POINTER(ctypes.c_int32))[0] = 40
            ctypes.cast(bitmap_info, ctypes.POINTER(ctypes.c_int32))[1] = WIDTH
            ctypes.cast(bitmap_info, ctypes.POINTER(ctypes.c_int32))[2] = -HEIGHT
            ctypes.cast(bitmap_info, ctypes.POINTER(ctypes.c_ushort))[6] = 1
            ctypes.cast(bitmap_info, ctypes.POINTER(ctypes.c_ushort))[7] = 32
            ctypes.cast(bitmap_info, ctypes.POINTER(ctypes.c_int32))[9] = 0
            bits = ctypes.c_void_p()
            bitmap = self._gdi32.CreateDIBSection(
                screen, bitmap_info, 0, ctypes.byref(bits), None, 0)
            old_bitmap = self._gdi32.SelectObject(memory, bitmap)
            try:
                self._paint_pill(memory, shown)
                target = self._user32.GetDC(ctypes.c_void_p(self._hwnd))
                try:
                    pos = _Point(0, 0)
                    size = _Point(WIDTH, HEIGHT)
                    blend = _BlendFunction(AC_SRC_OVER, 0, 255, 1)
                    self._user32.UpdateLayeredWindow(
                        ctypes.c_void_p(self._hwnd), target,
                        ctypes.byref(pos), ctypes.byref(size),
                        memory, ctypes.byref(pos),
                        0, ctypes.byref(blend), ULW_ALPHA)
                finally:
                    self._user32.ReleaseDC(ctypes.c_void_p(self._hwnd), target)
            finally:
                self._gdi32.SelectObject(memory, old_bitmap)
                self._gdi32.DeleteObject(bitmap)
                self._gdi32.DeleteDC(memory)
            self._user32.ReleaseDC(None, screen)
            self._place_pill(anchor)
            self._user32.ShowWindow(ctypes.c_void_p(self._hwnd), SW_SHOWNOACTIVATE)
            self._pill_visible = True
        except Exception:
            self._usable = False

    def _paint_pill(self, memory, lines: List[Dict]) -> None:
        key_brush = self._gdi32.CreateSolidBrush(self._rgb(KEY_COLOR))
        bg_brush = self._gdi32.CreateSolidBrush(self._rgb(BG_COLOR))
        font = self._gdi32.CreateFontW(
            -14, 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 0, 0, "Segoe UI")
        try:
            key_rect = _Rect(0, 0, WIDTH, HEIGHT)
            self._user32.FillRect(memory, ctypes.byref(key_rect), key_brush)
            region = self._gdi32.CreateRoundRectRgn(0, 0, WIDTH + 1, HEIGHT + 1, 22, 22)
            try:
                self._gdi32.FillRgn(memory, region, bg_brush)
            finally:
                self._gdi32.DeleteObject(region)
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
            for handle in (font, key_brush, bg_brush):
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
            if pulse:
                color = tuple(min(255, channel + 70) for channel in color)
            screen = self._user32.GetDC(None)
            memory = self._gdi32.CreateCompatibleDC(screen)
            total_w = width + BORDER_PX * 2
            total_h = height + BORDER_PX * 2
            bitmap_info = (ctypes.c_ubyte * 40)()
            ctypes.memset(bitmap_info, 0, 40)
            ctypes.cast(bitmap_info, ctypes.POINTER(ctypes.c_int32))[0] = 40
            ctypes.cast(bitmap_info, ctypes.POINTER(ctypes.c_int32))[1] = total_w
            ctypes.cast(bitmap_info, ctypes.POINTER(ctypes.c_int32))[2] = -total_h
            ctypes.cast(bitmap_info, ctypes.POINTER(ctypes.c_ushort))[6] = 1
            ctypes.cast(bitmap_info, ctypes.POINTER(ctypes.c_ushort))[7] = 32
            ctypes.cast(bitmap_info, ctypes.POINTER(ctypes.c_int32))[9] = 0
            bits = ctypes.c_void_p()
            bitmap = self._gdi32.CreateDIBSection(
                screen, bitmap_info, 0, ctypes.byref(bits), None, 0)
            old_bitmap = self._gdi32.SelectObject(memory, bitmap)
            try:
                self._paint_border(memory, total_w, total_h, color)
                target = self._user32.GetDC(ctypes.c_void_p(self._border_hwnd))
                try:
                    pos = _Point(0, 0)
                    size = _Point(total_w, total_h)
                    blend = _BlendFunction(AC_SRC_OVER, 0, 255, 1)
                    self._user32.UpdateLayeredWindow(
                        ctypes.c_void_p(self._border_hwnd), target,
                        ctypes.byref(pos), ctypes.byref(size),
                        memory, ctypes.byref(pos),
                        0, ctypes.byref(blend), ULW_ALPHA)
                finally:
                    self._user32.ReleaseDC(ctypes.c_void_p(self._border_hwnd),
                                          target)
            finally:
                self._gdi32.SelectObject(memory, old_bitmap)
                self._gdi32.DeleteObject(bitmap)
                self._gdi32.DeleteDC(memory)
            self._user32.ReleaseDC(None, screen)
            self._user32.SetWindowPos(
                ctypes.c_void_p(self._border_hwnd), None,
                rect.left - BORDER_PX, rect.top - BORDER_PX,
                total_w, total_h,
                SWP_NOZORDER | SWP_NOACTIVATE)
            self._user32.ShowWindow(ctypes.c_void_p(self._border_hwnd),
                                    SW_SHOWNOACTIVATE)
            self._border_visible = True
        except Exception:
            self._usable = False

    def _paint_border(self, memory, total_w: int, total_h: int, color) -> None:
        inner = self._gdi32.CreateRectRgn(
            BORDER_PX, BORDER_PX, total_w - BORDER_PX, total_h - BORDER_PX)
        outer = self._gdi32.CreateRectRgn(0, 0, total_w, total_h)
        frame = self._gdi32.CreateRectRgn(0, 0, 1, 1)
        key_brush = self._gdi32.CreateSolidBrush(self._rgb(KEY_COLOR))
        color_brush = self._gdi32.CreateSolidBrush(self._rgb(color))
        try:
            self._gdi32.CombineRgn(frame, outer, inner, 3)  # RGN_DIFF
            full = _Rect(0, 0, total_w, total_h)
            self._user32.FillRect(memory, ctypes.byref(full), key_brush)
            self._gdi32.FillRgn(memory, frame, color_brush)
        finally:
            for handle in (inner, outer, frame, key_brush, color_brush):
                try:
                    self._gdi32.DeleteObject(handle)
                except Exception:
                    pass
