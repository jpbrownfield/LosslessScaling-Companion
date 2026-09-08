"""Launch the local dashboard as an app-style desktop window."""

from __future__ import annotations

import logging
import os
import ctypes
from ctypes import wintypes
from pathlib import Path
import shutil
import subprocess
import webbrowser
from typing import Optional, Set

import psutil


logger = logging.getLogger("LSCompanion.DashboardWindow")

_BROWSER_NAMES = {"msedge.exe", "chrome.exe"}


def _browser_profile_directory() -> Path:
    local_root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local_root) / "LosslessScalingHelper" / "DashboardBrowser"


def _dashboard_browser_processes() -> Set[int]:
    """Find the dedicated browser instance, including its child processes."""
    marker = str(_browser_profile_directory().resolve()).casefold()
    pids: Set[int] = set()
    roots = []
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            if (process.info.get("name") or "").casefold() not in _BROWSER_NAMES:
                continue
            command = " ".join(process.info.get("cmdline") or []).casefold()
            if marker in command:
                roots.append(process)
                pids.add(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for process in roots:
        try:
            pids.update(child.pid for child in process.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return pids


def _focus_existing_dashboard() -> bool:
    if os.name != "nt":
        return False
    pids = _dashboard_browser_processes()
    if not pids:
        return False
    user32 = ctypes.windll.user32
    found = []

    def callback(hwnd, _extra):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(callback_type(callback), 0)
    if not found:
        return False
    hwnd = found[0]
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def _find_app_browser() -> Optional[str]:
    """Return an installed Chromium browser that supports ``--app`` windows."""
    for command in ("msedge.exe", "chrome.exe"):
        executable = shutil.which(command)
        if executable:
            return executable

    roots = [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    relative_paths = (
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("Google/Chrome/Application/chrome.exe"),
    )
    for root in filter(None, roots):
        for relative_path in relative_paths:
            candidate = Path(root) / relative_path
            if candidate.is_file():
                return str(candidate)
    return None


def open_dashboard_window(url: str) -> bool:
    """Focus the one dashboard app window or create it without browser chrome."""
    browser = _find_app_browser()
    if browser:
        if _focus_existing_dashboard():
            return True
        try:
            profile_directory = _browser_profile_directory()
            subprocess.Popen(
                [
                    browser,
                    f"--app={url}",
                    "--window-size=1180,820",
                    f"--user-data-dir={profile_directory}",
                    "--no-first-run",
                ],
                close_fds=True,
            )
            return True
        except OSError:
            logger.exception("Could not launch the dashboard app window with %s", browser)

    logger.warning("No app-mode browser was found; opening the dashboard normally")
    return bool(webbrowser.open(url, new=1))
