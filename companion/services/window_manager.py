"""Reversibly minimize other application windows on a target monitor."""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from ctypes import wintypes
from typing import Dict, Optional
import psutil


logger = logging.getLogger("LSCompanion.WindowManager")
user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
GW_OWNER = 4
MONITOR_DEFAULTTONULL = 0
SW_MINIMIZE = 6
SW_RESTORE = 9
WS_EX_TOOLWINDOW = 0x00000080

SHELL_CLASSES = {
    "progman",
    "workerw",
    "shell_traywnd",
    "shell_secondarytraywnd",
    "notifyiconoverflowwindow",
}


class MonitorWindowManager:
    """Tracks only windows it minimized, allowing a bounded later restore."""

    def __init__(self):
        self._lock = threading.RLock()
        self._minimized: Dict[int, int] = {}

    @staticmethod
    def _window_pid(hwnd: int) -> int:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    @staticmethod
    def _class_name(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value.casefold()

    @staticmethod
    def _target_window(preferred_hwnd: Optional[int]) -> Optional[int]:
        if preferred_hwnd and user32.IsWindow(preferred_hwnd) and user32.IsWindowVisible(preferred_hwnd):
            return int(preferred_hwnd)
        foreground = user32.GetForegroundWindow()
        return int(foreground) if foreground else None

    def minimize_others_on_target_monitor(self, target_hwnd: Optional[int] = None) -> int:
        """Minimize eligible peer windows and return the number newly minimized."""
        with self._lock:
            target = self._target_window(target_hwnd)
            if not target:
                logger.warning("Could not identify the scaled application's target window")
                return 0
            target_monitor = user32.MonitorFromWindow(target, MONITOR_DEFAULTTONULL)
            target_pid = self._window_pid(target)
            if not target_monitor or not target_pid:
                logger.warning("Could not identify the scaled application's monitor")
                return 0
            try:
                if psutil.Process(target_pid).name().casefold() == "losslessscaling.exe":
                    logger.warning("Refusing to treat a Lossless Scaling window as the scaled target")
                    return 0
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return 0

            excluded_pids = {target_pid, os.getpid()}
            for process in psutil.process_iter(["pid", "name"]):
                try:
                    if (process.info.get("name") or "").casefold() == "losslessscaling.exe":
                        excluded_pids.add(int(process.info["pid"]))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            minimized = 0

            def enum_callback(hwnd, _extra):
                nonlocal minimized
                handle = int(hwnd)
                if handle == target or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                    return True
                if user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONULL) != target_monitor:
                    return True
                pid = self._window_pid(handle)
                if not pid or pid in excluded_pids:
                    return True
                if user32.GetWindow(hwnd, GW_OWNER):
                    return True
                if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                    return True
                if self._class_name(handle) in SHELL_CLASSES:
                    return True
                if user32.GetWindowTextLengthW(hwnd) <= 0:
                    return True
                try:
                    user32.ShowWindow(hwnd, SW_MINIMIZE)
                    if user32.IsIconic(hwnd):
                        self._minimized[handle] = pid
                        minimized += 1
                except OSError:
                    logger.debug("Could not minimize HWND %s", handle, exc_info=True)
                return True

            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows(callback_type(enum_callback), 0)
            logger.info("Minimized %d other window(s) on the scaled application's monitor", minimized)
            return minimized

    def restore_managed_windows(self) -> int:
        """Restore surviving windows only when HWND and owning PID still match."""
        with self._lock:
            restored = 0
            for hwnd, original_pid in list(self._minimized.items()):
                try:
                    if (
                        user32.IsWindow(hwnd)
                        and self._window_pid(hwnd) == original_pid
                        and user32.IsIconic(hwnd)
                    ):
                        user32.ShowWindow(hwnd, SW_RESTORE)
                        restored += 1
                except OSError:
                    logger.debug("Could not restore HWND %s", hwnd, exc_info=True)
            self._minimized.clear()
            if restored:
                logger.info("Restored %d window(s) minimized for scaling", restored)
            return restored
