"""Visibility and foreground control for LosslessProxy's add-on manager."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import psutil
import threading
import time
from typing import List, Optional

from ..core.state import AppState


logger = logging.getLogger("LSCompanion.ProxyMenu")


class LosslessProxyMenuController:
    """Keep LosslessProxy's manager hidden until the user explicitly requests it."""

    WINDOW_CLASS = "LosslessProxyV2Class"
    SW_HIDE = 0
    SW_RESTORE = 9

    def __init__(self, state: AppState):
        self.state = state
        self._visible_requested = False
        self._lock = threading.RLock()

    @staticmethod
    def profile_uses_proxy(profile) -> bool:
        if profile is None:
            return False
        neural = getattr(getattr(profile, "graphics", None), "neural_render", None)
        reshade = getattr(profile, "reshade", None)
        return bool(
            getattr(neural, "implementation", "disabled") == "lsp_neural_render"
            or getattr(reshade, "menu_proxy_enabled", False)
        )

    def active_profile_uses_proxy(self) -> bool:
        return self.profile_uses_proxy(self.state.current_active_profile)

    @classmethod
    def _find_windows(cls, pid: Optional[int] = None) -> List[int]:
        user32 = ctypes.windll.user32
        matches: List[int] = []

        def callback(hwnd, _extra):
            class_name = ctypes.create_unicode_buffer(256)
            if not user32.GetClassNameW(hwnd, class_name, len(class_name)):
                return True
            if class_name.value != cls.WINDOW_CLASS:
                return True
            if pid is not None:
                owner_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
                if owner_pid.value != int(pid):
                    return True
            matches.append(int(hwnd))
            return True

        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(callback_type(callback), 0)
        return matches

    def enforce_hidden(self, pid: Optional[int] = None) -> int:
        """Hide manager windows unless one was explicitly opened through Companion."""
        with self._lock:
            windows = self._find_windows(pid)
            if not windows:
                self._visible_requested = False
                return 0
            if self._visible_requested:
                return 0
            show_window = getattr(
                ctypes.windll.user32, "ShowWindowAsync", ctypes.windll.user32.ShowWindow
            )
            for hwnd in windows:
                show_window(hwnd, self.SW_HIDE)
            return len(windows)

    def suppress_startup_window(self, pid: int, timeout: float = 8.0) -> None:
        """Watch for LosslessProxy's delayed manager without touching LS render windows."""
        self._visible_requested = False

        def wait_for_manager() -> None:
            deadline = time.monotonic() + timeout
            poll = threading.Event()
            while time.monotonic() < deadline:
                if not psutil.pid_exists(pid):
                    return
                if self.enforce_hidden(pid):
                    return
                poll.wait(0.01)

        threading.Thread(
            target=wait_for_manager,
            name=f"LosslessProxyMenuSuppressor-{pid}",
            daemon=True,
        ).start()

    def open(self) -> dict:
        """Restore and focus the existing LosslessProxy manager window."""
        windows = self._find_windows()
        if not windows:
            return {
                "opened": False,
                "reason": "The LosslessProxy configuration window is not running. Deploy a proxy-enabled profile, then restart Lossless Scaling.",
            }

        hwnd = windows[0]
        user32 = ctypes.windll.user32
        show_window = getattr(user32, "ShowWindowAsync", user32.ShowWindow)
        with self._lock:
            self._visible_requested = True
        show_window(hwnd, self.SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        focused = bool(user32.SetForegroundWindow(hwnd))
        logger.info("Opened LosslessProxy add-on manager (HWND %s)", hwnd)
        return {"opened": True, "focused": focused, "hwnd": hwnd}
