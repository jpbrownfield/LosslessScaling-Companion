"""Refresh a scaled game's own cursor after focus returns from another window.

Never change the system cursor scheme, the visibility counter, mouse position,
capture, or foreground focus. Games retain control of intentional cursor hiding.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import logging
import time

logger = logging.getLogger("LSCompanion.Cursor")


class _CursorInfo(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD),
                ("hCursor", w.HANDLE), ("ptScreenPos", w.POINT)]


class _CursorApi:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        for name, args, result in (
            ("GetForegroundWindow", [], w.HWND),
            ("GetWindowThreadProcessId", [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            ("IsWindow", [w.HWND], w.BOOL),
            ("IsWindowVisible", [w.HWND], w.BOOL),
            ("IsIconic", [w.HWND], w.BOOL),
            ("IsChild", [w.HWND, w.HWND], w.BOOL),
            ("GetAncestor", [w.HWND, w.UINT], w.HWND),
            ("GetCursorPos", [ctypes.POINTER(w.POINT)], w.BOOL),
            ("GetClientRect", [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            ("ScreenToClient", [w.HWND, ctypes.POINTER(w.POINT)], w.BOOL),
            ("ChildWindowFromPointEx", [w.HWND, w.POINT, w.UINT], w.HWND),
            ("GetCursorInfo", [ctypes.POINTER(_CursorInfo)], w.BOOL),
            ("SendMessageTimeoutW", [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT,
                                      w.UINT, ctypes.POINTER(ctypes.c_size_t)], w.LPARAM),
        ):
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result

    def pid(self, hwnd):
        value = w.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(value))
        return value.value

    def snapshot(self):
        info = _CursorInfo(cbSize=ctypes.sizeof(_CursorInfo))
        if self.user.GetCursorInfo(ctypes.byref(info)):
            return {"showing": bool(info.flags & 1), "handle": int(info.hCursor or 0)}
        return None


def refresh_game_cursor(pid: int, hwnd: int, *, api=None) -> dict:
    """Ask the verified foreground game to refresh the cursor in its client area."""
    api = api or _CursorApi()
    user = api.user
    if (not user.IsWindow(hwnd) or api.pid(hwnd) != pid
            or not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd)):
        return {"outcome": "target_unavailable"}
    foreground = user.GetForegroundWindow()
    if (not foreground or api.pid(foreground) != pid
            or (foreground != hwnd and not user.IsChild(hwnd, foreground)
                and user.GetAncestor(foreground, 3) != hwnd)):
        return {"outcome": "focus_changed"}
    point, rect = w.POINT(), w.RECT()
    if not user.GetCursorPos(ctypes.byref(point)):
        return {"outcome": "desktop_unavailable", "error": ctypes.get_last_error()}
    # Do not change another monitor/app's cursor while the mouse is outside
    # the foreground game's client area. No synthetic movement is required.
    if not user.GetClientRect(foreground, ctypes.byref(rect)) or not user.ScreenToClient(foreground, ctypes.byref(point)):
        return {"outcome": "target_unavailable"}
    if not rect.left <= point.x < rect.right or not rect.top <= point.y < rect.bottom:
        return {"outcome": "pointer_outside_game"}
    recipient = user.ChildWindowFromPointEx(foreground, point, 7) or foreground
    if api.pid(recipient) != pid:
        return {"outcome": "target_changed"}
    before = api.snapshot()
    # Identity/focus can change while examining a hung/recreated game window.
    if user.GetForegroundWindow() != foreground or api.pid(hwnd) != pid or api.pid(recipient) != pid:
        return {"outcome": "focus_changed"}
    reply = ctypes.c_size_t()
    ctypes.set_last_error(0)
    accepted = user.SendMessageTimeoutW(recipient, 0x0020, recipient, 1 | (0x0200 << 16),
                                        0x0001 | 0x0002 | 0x0020, 30, ctypes.byref(reply))
    error = 0 if accepted else ctypes.get_last_error()
    return {"outcome": "refreshed" if accepted else "message_failed", "error": error,
            "handled": bool(reply.value), "recipient": recipient,
            "before": before, "after": api.snapshot()}


class CursorRecovery:
    def __init__(self, refresh=None, clock=None):
        self.refresh = refresh or refresh_game_cursor
        self.clock = clock or time.monotonic
        self._session = None
        self._focused = False
        self._attempts = []

    def update(self, state, foreground_pid: int, foreground_hwnd: int) -> None:
        target = state.current_scaled_target or {}
        pid = state.scaling_target_pid or target.get("pid")
        hwnd = state.scaling_target_hwnd or target.get("hwnd")
        session = ((pid, hwnd, state.last_scale_toggle_time)
                   if state.is_scaling_active and not state.benchmark_mode_active
                   and not state.update_in_progress and type(pid) is int and pid > 0
                   and type(hwnd) is int and hwnd > 0 else None)
        focused = bool(session and foreground_pid == pid and foreground_hwnd)
        if session != self._session:
            self._session, self._focused, self._attempts = session, focused, []
            return
        if not session:
            return
        now = self.clock()
        if focused and not self._focused:
            # LS can resume its cursor watcher after the first focus message.
            # Retry on later monitor ticks, never continuously force a shape.
            self._attempts = [now, now + .2, now + .6]
        elif not focused:
            self._attempts = []
        self._focused = focused
        if self._attempts and now >= self._attempts[0]:
            self._attempts.pop(0)
            try:
                evidence = self.refresh(pid, hwnd)
                if evidence.get("outcome") in ("refreshed", "message_failed"):
                    logger.info("Scaled game cursor refresh after focus return: PID=%s HWND=%s remaining=%s evidence=%s",
                                pid, hwnd, len(self._attempts), evidence)
                else:
                    logger.debug("Skipped scaled game cursor refresh: PID=%s HWND=%s evidence=%s", pid, hwnd, evidence)
            except Exception:
                logger.exception("Could not refresh the scaled game's cursor: PID=%s HWND=%s", pid, hwnd)
