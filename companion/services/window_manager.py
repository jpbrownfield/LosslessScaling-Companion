"""Reversibly minimize other application windows on a target monitor."""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from ctypes import wintypes
from typing import Optional
import psutil


logger = logging.getLogger("LSCompanion.WindowManager")
user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
GW_OWNER = 4
MONITOR_DEFAULTTONULL = 0
MONITOR_DEFAULTTONEAREST = 2
SW_MINIMIZE = 6
SW_RESTORE = 9
SW_MAXIMIZE = 3
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
WS_CAPTION = 0x00C00000
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS = 9


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]

SHELL_CLASSES = {
    "progman",
    "workerw",
    "shell_traywnd",
    "shell_secondarytraywnd",
    "notifyiconoverflowwindow",
}


class MonitorWindowManager:
    """Minimizes eligible peers without later altering the user's window order."""

    def __init__(self):
        self._lock = threading.RLock()

    @staticmethod
    def _near_monitor_adjustment(
        window_bounds: tuple[int, int, int, int],
        monitor_bounds: tuple[int, int, int, int],
        tolerance_px: int,
    ) -> Optional[tuple[int, int, int, int]]:
        """Return per-edge corrections only for a nearly monitor-sized window."""
        if tolerance_px < 0:
            return None
        deltas = tuple(
            monitor - window
            for window, monitor in zip(window_bounds, monitor_bounds)
        )
        if any(abs(delta) > tolerance_px for delta in deltas):
            return None
        return deltas

    @staticmethod
    def _translated_window_position(
        window_bounds: tuple[int, int, int, int],
        source_work: tuple[int, int, int, int],
        target_work: tuple[int, int, int, int],
    ) -> tuple[int, int]:
        """Preserve a normal window's monitor-relative position and keep it reachable."""
        left, top, right, bottom = window_bounds
        width = max(1, right - left)
        height = max(1, bottom - top)
        offset_x = left - source_work[0]
        offset_y = top - source_work[1]
        target_width = target_work[2] - target_work[0]
        target_height = target_work[3] - target_work[1]
        max_x = max(0, target_width - min(width, target_width))
        max_y = max(0, target_height - min(height, target_height))
        return (
            target_work[0] + min(max(0, offset_x), max_x),
            target_work[1] + min(max(0, offset_y), max_y),
        )

    @staticmethod
    def _monitor_details() -> list[dict]:
        monitors: list[dict] = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
        )

        def collect(handle, _dc, _rect, _data):
            info = _MonitorInfoEx()
            info.cbSize = ctypes.sizeof(_MonitorInfoEx)
            if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                monitors.append({
                    "handle": int(handle),
                    "deviceName": str(info.szDevice),
                    "monitor": (
                        int(info.rcMonitor.left), int(info.rcMonitor.top),
                        int(info.rcMonitor.right), int(info.rcMonitor.bottom),
                    ),
                    "work": (
                        int(info.rcWork.left), int(info.rcWork.top),
                        int(info.rcWork.right), int(info.rcWork.bottom),
                    ),
                })
            return True

        callback = callback_type(collect)
        if not user32.EnumDisplayMonitors(None, None, callback, 0):
            return []
        return monitors

    def move_window_to_display(
        self, target_hwnd: Optional[int], device_name: str
    ) -> bool:
        """Move a target application to an explicit Windows display without resizing it."""
        with self._lock:
            target = self._target_window(target_hwnd)
            if not target or not device_name or user32.IsIconic(target):
                return False
            monitors = self._monitor_details()
            destination = next(
                (
                    item for item in monitors
                    if item["deviceName"].casefold() == device_name.casefold()
                ),
                None,
            )
            source_handle = user32.MonitorFromWindow(target, MONITOR_DEFAULTTONEAREST)
            source = next(
                (item for item in monitors if item["handle"] == int(source_handle or 0)),
                None,
            )
            if not destination or not source:
                return False
            if destination["handle"] == source["handle"]:
                return True

            was_maximized = bool(user32.IsZoomed(target))
            if was_maximized:
                user32.ShowWindow(target, SW_RESTORE)
            bounds = wintypes.RECT()
            if not user32.GetWindowRect(target, ctypes.byref(bounds)):
                if was_maximized:
                    user32.ShowWindow(target, SW_MAXIMIZE)
                return False
            window_bounds = (
                int(bounds.left), int(bounds.top), int(bounds.right), int(bounds.bottom),
            )
            new_left, new_top = self._translated_window_position(
                window_bounds, source["work"], destination["work"]
            )
            moved = bool(user32.SetWindowPos(
                target, None, new_left, new_top,
                window_bounds[2] - window_bounds[0],
                window_bounds[3] - window_bounds[1],
                SWP_NOZORDER | SWP_NOACTIVATE,
            ))
            if was_maximized:
                user32.ShowWindow(target, SW_MAXIMIZE)
            if moved:
                logger.info(
                    "Moved HWND %s from %s to explicit profile display %s (%s)",
                    target, source["deviceName"], destination["deviceName"],
                    destination["monitor"],
                )
            return moved

    def snap_borderless_window_to_monitor(
        self, target_hwnd: Optional[int], *, tolerance_px: int = 8
    ) -> bool:
        """Correct a few-pixel borderless-window overlap without general resizing."""
        with self._lock:
            target = self._target_window(target_hwnd)
            if (
                not target
                or user32.IsIconic(target)
                or user32.IsZoomed(target)
                or user32.GetWindowLongW(target, -16) & WS_CAPTION
            ):
                return False
            monitor = user32.MonitorFromWindow(target, MONITOR_DEFAULTTONEAREST)
            if not monitor:
                return False
            monitor_info = _MonitorInfo()
            monitor_info.cbSize = ctypes.sizeof(_MonitorInfo)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(monitor_info)):
                return False

            outer = wintypes.RECT()
            if not user32.GetWindowRect(target, ctypes.byref(outer)):
                return False
            visible = wintypes.RECT()
            visible_found = False
            try:
                visible_found = (
                    ctypes.windll.dwmapi.DwmGetWindowAttribute(
                        target,
                        DWMWA_EXTENDED_FRAME_BOUNDS,
                        ctypes.byref(visible),
                        ctypes.sizeof(visible),
                    )
                    == 0
                )
            except (AttributeError, OSError):
                visible_found = False
            if not visible_found:
                visible = outer

            window_bounds = (
                int(visible.left),
                int(visible.top),
                int(visible.right),
                int(visible.bottom),
            )
            monitor_bounds = (
                int(monitor_info.rcMonitor.left),
                int(monitor_info.rcMonitor.top),
                int(monitor_info.rcMonitor.right),
                int(monitor_info.rcMonitor.bottom),
            )
            corrections = self._near_monitor_adjustment(
                window_bounds, monitor_bounds, tolerance_px
            )
            if corrections is None or not any(corrections):
                return False
            left_delta, top_delta, right_delta, bottom_delta = corrections
            new_left = int(outer.left) + left_delta
            new_top = int(outer.top) + top_delta
            new_right = int(outer.right) + right_delta
            new_bottom = int(outer.bottom) + bottom_delta
            if new_right <= new_left or new_bottom <= new_top:
                return False
            changed = bool(
                user32.SetWindowPos(
                    target,
                    None,
                    new_left,
                    new_top,
                    new_right - new_left,
                    new_bottom - new_top,
                    SWP_NOZORDER | SWP_NOACTIVATE,
                )
            )
            if changed:
                logger.info(
                    "Snapped borderless HWND %s from %s to monitor bounds %s",
                    target,
                    window_bounds,
                    monitor_bounds,
                )
            return changed

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

    @classmethod
    def _replacement_window_for_pid(cls, expected_pid: int) -> Optional[int]:
        """Resolve a recreated game window without ever borrowing another process."""
        candidates: list[tuple[int, int]] = []

        def callback(hwnd, _extra):
            handle = int(hwnd)
            if (
                cls._window_pid(handle) != expected_pid
                or not user32.IsWindowVisible(hwnd)
                or user32.IsIconic(hwnd)
                or user32.GetWindow(hwnd, GW_OWNER)
                or user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW
                or cls._class_name(handle) in SHELL_CLASSES
                or user32.GetWindowTextLengthW(hwnd) <= 0
            ):
                return True
            bounds = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(bounds)):
                area = max(0, int(bounds.right - bounds.left)) * max(
                    0, int(bounds.bottom - bounds.top)
                )
                if area:
                    candidates.append((area, handle))
            return True

        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(callback_type(callback), 0)
        return max(candidates, default=(0, 0))[1] or None

    @classmethod
    def _target_window(
        cls, preferred_hwnd: Optional[int], expected_pid: Optional[int] = None
    ) -> Optional[int]:
        """Return only a window proven to belong to the expected game process."""
        if (
            preferred_hwnd
            and user32.IsWindow(preferred_hwnd)
            and user32.IsWindowVisible(preferred_hwnd)
        ):
            owner_pid = cls._window_pid(int(preferred_hwnd))
            if owner_pid and (not expected_pid or owner_pid == int(expected_pid)):
                return int(preferred_hwnd)
        if expected_pid:
            replacement = cls._replacement_window_for_pid(int(expected_pid))
            if replacement:
                logger.info(
                    "Resolved recreated scaling target HWND %s for PID %s",
                    replacement,
                    expected_pid,
                )
            return replacement
        return None

    def minimize_others_on_target_monitor(
        self,
        target_hwnd: Optional[int] = None,
        *,
        target_pid: Optional[int] = None,
    ) -> int:
        """Minimize eligible peer windows and return the number newly minimized."""
        with self._lock:
            target = self._target_window(target_hwnd, target_pid)
            if not target:
                logger.warning(
                    "Skipping peer minimization: no verified target window for HWND %s / PID %s",
                    target_hwnd,
                    target_pid,
                )
                return 0
            target_monitor = user32.MonitorFromWindow(target, MONITOR_DEFAULTTONULL)
            resolved_target_pid = self._window_pid(target)
            if (
                not target_monitor
                or not resolved_target_pid
                or (target_pid and resolved_target_pid != int(target_pid))
            ):
                logger.warning("Could not identify the scaled application's monitor")
                return 0
            try:
                if psutil.Process(resolved_target_pid).name().casefold() == "losslessscaling.exe":
                    logger.warning("Refusing to treat a Lossless Scaling window as the scaled target")
                    return 0
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return 0

            excluded_pids = {resolved_target_pid, os.getpid()}
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
                        minimized += 1
                        logger.info(
                            "Minimized peer HWND %s / PID %s on target monitor; "
                            "protected target HWND %s / PID %s",
                            handle,
                            pid,
                            target,
                            resolved_target_pid,
                        )
                except OSError:
                    logger.debug("Could not minimize HWND %s", handle, exc_info=True)
                return True

            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows(callback_type(enum_callback), 0)
            logger.info("Minimized %d other window(s) on the scaled application's monitor", minimized)
            return minimized
