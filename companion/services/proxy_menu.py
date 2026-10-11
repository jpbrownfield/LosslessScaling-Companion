"""Visibility and foreground control for Echo LS Add-on Manager."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import psutil
import threading
import time
from typing import List, Optional

from ..core.state import AppState


logger = logging.getLogger("LSCompanion.AddonMenu")


class AddonManagerMenuController:
    """Keep Echo's manager hidden until the user explicitly requests it."""

    WINDOW_CLASS = "LSAddonManagerClass"
    SW_HIDE = 0
    SW_RESTORE = 9
    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    WS_CHILD = 0x40000000
    WS_POPUP = 0x80000000
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_VISIBLE = 0x10000000
    SWP_FRAMECHANGED = 0x0020
    SWP_SHOWWINDOW = 0x0040

    def __init__(self, state: AppState):
        self.state = state
        self._visible_requested = False
        self._lock = threading.RLock()
        self._embedded: Optional[dict] = None

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
        """Watch for Echo's delayed manager without touching LS render windows."""
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
            name=f"EchoAddonMenuSuppressor-{pid}",
            daemon=True,
        ).start()

    def open(self) -> dict:
        """Restore and focus the existing Echo manager window."""
        embedded_hwnd = int(self._embedded.get("hwnd") or 0) if self._embedded else 0
        windows = (
            [embedded_hwnd]
            if embedded_hwnd and ctypes.windll.user32.IsWindow(embedded_hwnd)
            else self._find_windows()
        )
        if not windows:
            return {
                "opened": False,
                "reason": "The Echo add-on menu is not running. Enable Add-on Manager, then restart Lossless Scaling.",
            }

        hwnd = windows[0]
        user32 = ctypes.windll.user32
        show_window = getattr(user32, "ShowWindowAsync", user32.ShowWindow)
        with self._lock:
            self._visible_requested = True
        show_window(hwnd, self.SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        focused = bool(user32.SetForegroundWindow(hwnd))
        logger.info("Opened Echo LS Add-on Manager (HWND %s)", hwnd)
        return {"opened": True, "focused": focused, "hwnd": hwnd}

    @staticmethod
    def _window_long(hwnd: int, index: int) -> int:
        user32 = ctypes.windll.user32
        getter = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        getter.argtypes = (wintypes.HWND, ctypes.c_int)
        getter.restype = ctypes.c_ssize_t
        return int(getter(hwnd, index))

    @staticmethod
    def _set_window_long(hwnd: int, index: int, value: int) -> None:
        user32 = ctypes.windll.user32
        setter = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        setter.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
        setter.restype = ctypes.c_ssize_t
        setter(hwnd, index, value)

    def embed(
        self,
        parent_hwnd: int,
        *,
        left: int,
        top: int,
        width: int,
        height: int,
    ) -> dict:
        """Host Echo's existing native window inside Companion's dashboard."""
        if min(width, height) <= 0 or not parent_hwnd:
            return {"embedded": False, "reason": "The add-on panel has no usable host area."}
        embedded_hwnd = int(self._embedded.get("hwnd") or 0) if self._embedded else 0
        windows = (
            [embedded_hwnd]
            if embedded_hwnd and ctypes.windll.user32.IsWindow(embedded_hwnd)
            else self._find_windows()
        )
        if not windows:
            return {
                "embedded": False,
                "reason": "Echo is not running. Start Lossless Scaling, then reopen this profile.",
            }
        hwnd = windows[0]
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = (wintypes.HWND,)
        user32.GetParent.restype = wintypes.HWND
        user32.SetParent.argtypes = (wintypes.HWND, wintypes.HWND)
        user32.SetParent.restype = wintypes.HWND
        with self._lock:
            if not self._embedded or self._embedded.get("hwnd") != hwnd:
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                self._embedded = {
                    "hwnd": hwnd,
                    "parent": int(user32.GetParent(hwnd) or 0),
                    "style": self._window_long(hwnd, self.GWL_STYLE),
                    "exstyle": self._window_long(hwnd, self.GWL_EXSTYLE),
                    "rect": (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
                }
                style = self._embedded["style"]
                style &= ~(self.WS_POPUP | self.WS_CAPTION | self.WS_THICKFRAME)
                style |= self.WS_CHILD | self.WS_VISIBLE
                user32.ShowWindow(hwnd, self.SW_HIDE)
                ctypes.windll.kernel32.SetLastError(0)
                previous_parent = user32.SetParent(hwnd, parent_hwnd)
                error = int(ctypes.windll.kernel32.GetLastError())
                if not previous_parent and error:
                    self._embedded = None
                    return {"embedded": False, "reason": f"Windows could not attach Echo's panel ({error})."}
                self._set_window_long(hwnd, self.GWL_STYLE, style)
            self._visible_requested = True
            user32.SetWindowPos(
                hwnd, 0, int(left), int(top), int(width), int(height),
                self.SWP_FRAMECHANGED | self.SWP_SHOWWINDOW,
            )
        return {"embedded": True, "hwnd": hwnd}

    def detach(self, *, hide: bool = True) -> bool:
        """Restore Echo's top-level window after it leaves the profile editor."""
        user32 = ctypes.windll.user32
        with self._lock:
            saved = self._embedded
            if not saved:
                self._visible_requested = not hide
                return False
            hwnd = int(saved["hwnd"])
            user32.ShowWindow(hwnd, self.SW_HIDE)
            user32.SetParent(hwnd, int(saved["parent"]))
            self._set_window_long(hwnd, self.GWL_STYLE, int(saved["style"]))
            self._set_window_long(hwnd, self.GWL_EXSTYLE, int(saved["exstyle"]))
            left, top, width, height = saved["rect"]
            user32.SetWindowPos(hwnd, 0, left, top, width, height, self.SWP_FRAMECHANGED)
            self._embedded = None
            self._visible_requested = not hide
            if not hide:
                user32.ShowWindow(hwnd, self.SW_RESTORE)
            logger.info("Detached Echo LS Add-on Manager (HWND %s)", hwnd)
            return True


# Import compatibility for extensions and older tests. Runtime code now uses
# Echo's window class and behavior through this alias as well.
LosslessProxyMenuController = AddonManagerMenuController
