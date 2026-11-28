"""Refresh a scaled game's own cursor after focus returns from another window.

Never change the system cursor scheme, the visibility counter, mouse position,
capture, or foreground focus. Games retain control of intentional cursor hiding.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import logging
import math
import threading
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
    outcome = ("refresh_requested" if reply.value else "unhandled") if accepted else "message_failed"
    return {"outcome": outcome, "error": error,
            "handled": bool(reply.value), "recipient": recipient,
            "before": before, "after": api.snapshot()}


class CursorRecovery:
    def __init__(self, refresh=None, clock=None):
        self.refresh = refresh or refresh_game_cursor
        self.clock = clock or time.monotonic
        self._session = None
        self._focused = False
        self._attempts = []
        self._focus_epoch = 0

    @property
    def next_delay(self):
        """Sleep indefinitely when idle; wake only for the bounded retry burst."""
        return max(0, self._attempts[0] - self.clock()) if self._attempts else None

    def update(self, state, foreground_pid: int, foreground_hwnd: int, *, enabled: bool = True,
               focus_epoch: int = 0) -> None:
        focus_changed = focus_epoch != self._focus_epoch
        self._focus_epoch = focus_epoch
        if not enabled:
            self._session, self._focused, self._attempts = None, False, []
            return
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
        if focused and (not self._focused or focus_changed):
            # A foreground epoch catches an away/return pair between samples.
            # LS may resume after the first request, so retry twice, then stop.
            self._attempts = [now, now + .2, now + .6]
        elif not focused:
            self._attempts = []
        self._focused = focused
        if self._attempts and now >= self._attempts[0]:
            self._attempts.pop(0)
            try:
                evidence = self.refresh(pid, hwnd)
                if evidence.get("outcome") in ("refresh_requested", "unhandled", "message_failed"):
                    logger.info("Scaled game cursor refresh request after focus return: PID=%s HWND=%s remaining=%s evidence=%s",
                                pid, hwnd, len(self._attempts), evidence)
                else:
                    logger.debug("Skipped scaled game cursor refresh: PID=%s HWND=%s evidence=%s", pid, hwnd, evidence)
            except Exception:
                logger.exception("Could not refresh the scaled game's cursor: PID=%s HWND=%s", pid, hwnd)


_ForegroundProc = ctypes.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND,
                                   w.LONG, w.LONG, w.DWORD, w.DWORD)
_WM_WAKE_CURSOR = 0x8002


class _ForegroundApi(_CursorApi):
    def __init__(self):
        super().__init__()
        for name, args, result in (
            ("SetWinEventHook", [w.DWORD, w.DWORD, w.HMODULE, _ForegroundProc,
                                 w.DWORD, w.DWORD, w.DWORD], w.HANDLE),
            ("UnhookWinEvent", [w.HANDLE], w.BOOL),
            ("PeekMessageW", [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT], w.BOOL),
            ("PostThreadMessageW", [w.DWORD, w.UINT, w.WPARAM, w.LPARAM], w.BOOL),
            ("MsgWaitForMultipleObjectsEx", [w.DWORD, ctypes.c_void_p, w.DWORD,
                                            w.DWORD, w.DWORD], w.DWORD),
        ):
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetCurrentThreadId.argtypes = []
        self.kernel.GetCurrentThreadId.restype = w.DWORD


class CursorRecoveryEvents:
    """Observe foreground changes outside games, with no idle fast polling.

    The callback only records an epoch. All requests run later on this thread,
    after checking the current session, preference, foreground and pointer again.
    If the observer cannot start, the caller retains its existing polling fallback.
    """

    def __init__(self, state, enabled, *, api_factory=None):
        self.state, self.enabled = state, enabled
        self._api_factory = api_factory or _ForegroundApi
        self._thread = None
        self._thread_id = None
        self._user = None
        self._active = False
        self._epoch = 0
        self._wake_pending = False
        self._wake_lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()

    @property
    def active(self):
        return self._active

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="CursorRecovery", daemon=True)
        self._thread.start()
        self._ready.wait(1)

    def wake(self):
        # Coalesce notifications while our worker is busy with a bounded send.
        with self._wake_lock:
            if self._user and self._thread_id and not self._wake_pending:
                self._wake_pending = bool(self._user.PostThreadMessageW(
                    self._thread_id, _WM_WAKE_CURSOR, 0, 0))

    def stop(self):
        self._stop.set()
        self.wake()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(1)

    def _on_foreground(self, _hook, event, _hwnd, _object, _child, _thread, _tick):
        if event == 3:  # EVENT_SYSTEM_FOREGROUND; include our own config/overlay.
            self._epoch += 1
            self.wake()

    def _run(self):
        hook = None
        try:
            api = self._api_factory()
            user, message = api.user, w.MSG()
            self._user = user
            self._thread_id = api.kernel.GetCurrentThreadId()
            user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            callback = _ForegroundProc(self._on_foreground)  # Keep alive until unhooked.
            hook = user.SetWinEventHook(3, 3, None, callback, 0, 0, 0)  # OUTOFCONTEXT
            if not hook:
                raise ctypes.WinError(ctypes.get_last_error())
            recovery = CursorRecovery(refresh=lambda pid, hwnd: refresh_game_cursor(pid, hwnd, api=api))
            self._active = True
            self._ready.set()
            while not self._stop.is_set():
                # Dispatch queued foreground notifications before checking the
                # actual foreground, including a complete rapid away/return.
                while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    if message.message == _WM_WAKE_CURSOR:
                        with self._wake_lock:
                            self._wake_pending = False
                if self._stop.is_set():
                    break
                foreground = user.GetForegroundWindow()
                recovery.update(self.state, api.pid(foreground) if foreground else 0,
                                foreground or 0, enabled=self.enabled(), focus_epoch=self._epoch)
                delay = recovery.next_delay
                timeout = 0xFFFFFFFF if delay is None else max(1, math.ceil(delay * 1000))
                # QS_ALLINPUT / MWMO_INPUTAVAILABLE: no per-frame hook or timer.
                if user.MsgWaitForMultipleObjectsEx(0, None, timeout, 0x04FF, 0x0004) == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            logger.warning("Cursor foreground observer unavailable; retaining polling recovery", exc_info=True)
        finally:
            try:
                if hook:
                    self._user.UnhookWinEvent(hook)
            finally:
                with self._wake_lock:
                    self._thread_id = self._user = None
                    self._wake_pending = False
                self._active = False
                self._ready.set()
